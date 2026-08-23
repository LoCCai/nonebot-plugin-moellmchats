from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import tempfile
import time
from typing import Any, cast

from nonebot.log import logger

from .compat import timeout as timeout_scope
from .config import config_parser
from .runtime_metrics import runtime_metrics
from .tool_artifacts import ToolArtifact
from .tool_contracts import ToolCapabilityV2, ToolResult

_PROTOCOL_VERSION = 1
_MAX_SOURCE_BYTES = 65_536
_MAX_REQUEST_BYTES = 524_288
_CAPABILITY_FIELDS = frozenset(
    {"network", "process", "workspace", "host_filesystem", "secrets"}
)


class GeneratedToolBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceLimits:
    total_bytes: int
    max_files: int
    max_depth: int
    max_file_bytes: int

    @classmethod
    def from_config(cls) -> WorkspaceLimits:
        return cls(
            total_bytes=(config_parser.get_config("generated_tool_workspace_mb", 64) * 1024 * 1024),
            max_files=config_parser.get_config("generated_tool_workspace_max_files", 256),
            max_depth=config_parser.get_config("generated_tool_workspace_max_depth", 8),
            max_file_bytes=config_parser.get_config("generated_tool_workspace_max_file_bytes", 8_388_608),
        )


@dataclass(frozen=True)
class _SourceSnapshot:
    source: bytes
    tests_source: bytes
    filename: str


class GeneratedToolRunner:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(1)
        self._pending = 0
        self._state_lock = asyncio.Lock()
        self._worker_path = Path(__file__).with_name("generated_tool_worker.py")
        self._isolation_path = Path(__file__).with_name(
            "generated_tool_isolation.py"
        )
        self.isolation_status = "not_checked"

    @asynccontextmanager
    async def _slot(self):
        async with self._state_lock:
            maximum = config_parser.get_config("generated_tool_max_pending", 4)
            if self._semaphore.locked() and self._pending >= maximum:
                runtime_metrics.generated_runner_rejected += 1
                raise GeneratedToolBusy("生成工具执行队列已满")
            self._pending += 1
            runtime_metrics.generated_runner_pending = self._pending
        try:
            await self._semaphore.acquire()
        finally:
            async with self._state_lock:
                self._pending -= 1
                runtime_metrics.generated_runner_pending = self._pending
        runtime_metrics.generated_runner_active += 1
        try:
            yield
        finally:
            runtime_metrics.generated_runner_active -= 1
            self._semaphore.release()

    @staticmethod
    def _scan_workspace(path: Path, limits: WorkspaceLimits) -> tuple[int, int]:
        """Inspect a workspace without following links (called via to_thread)."""
        total_bytes = 0
        entries_seen = 0
        stack: list[tuple[Path, int]] = [(path, 0)]
        while stack:
            directory, parent_depth = stack.pop()
            try:
                iterator = os.scandir(directory)
            except FileNotFoundError:
                continue
            with iterator:
                for entry in iterator:
                    entries_seen += 1
                    # Cap all entries so empty-directory bombs are bounded too.
                    if entries_seen > limits.max_files:
                        raise RuntimeError("工具工作目录文件/目录条目数量超过限制")
                    depth = parent_depth + 1
                    if depth > limits.max_depth:
                        raise RuntimeError("工具工作目录层级超过限制")
                    if entry.is_symlink():
                        raise RuntimeError("工具工作目录禁止符号链接")
                    try:
                        item_stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    mode = item_stat.st_mode
                    if stat.S_ISDIR(mode):
                        stack.append((Path(entry.path), depth))
                        continue
                    if not stat.S_ISREG(mode):
                        raise RuntimeError("工具工作目录禁止特殊文件")
                    if item_stat.st_size > limits.max_file_bytes:
                        raise RuntimeError("工具工作目录单文件超过限制")
                    total_bytes += item_stat.st_size
                    if total_bytes > limits.total_bytes:
                        raise RuntimeError("工具工作目录总容量超过限制")
        return total_bytes, entries_seen

    async def _scan_workspace_async(self, path: Path, limits: WorkspaceLimits) -> tuple[int, int]:
        return await asyncio.to_thread(self._scan_workspace, path, limits)

    async def _watch_workspace(self, path: Path, limits: WorkspaceLimits) -> None:
        while True:
            await asyncio.sleep(0.1)
            await self._scan_workspace_async(path, limits)

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
        parts: list[bytes] = []
        size = 0
        while chunk := await stream.read(4096):
            size += len(chunk)
            if size > limit:
                raise RuntimeError("工具 runner 输出超过限制")
            parts.append(chunk)
        return b"".join(parts)

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process, *, forced: bool = True) -> None:
        if forced:
            runtime_metrics.generated_runner_killed += 1
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.returncode is None:
            await proc.wait()

    @staticmethod
    def _read_snapshot_file(path: Path) -> bytes:
        if path.is_symlink():
            raise ValueError(f"兼容工具源码禁止符号链接: {path.name}")
        try:
            item_stat = path.stat()
        except FileNotFoundError as error:
            raise ValueError(f"兼容工具源码不存在: {path.name}") from error
        if not stat.S_ISREG(item_stat.st_mode):
            raise ValueError(f"兼容工具源码不是普通文件: {path.name}")
        if item_stat.st_size > _MAX_SOURCE_BYTES:
            raise ValueError(f"兼容工具源码超过 64 KiB: {path.name}")
        source = path.read_bytes()
        if len(source) > _MAX_SOURCE_BYTES:
            raise ValueError(f"兼容工具源码超过 64 KiB: {path.name}")
        try:
            source.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"兼容工具源码不是 UTF-8: {path.name}") from error
        return source

    @staticmethod
    def _prepare_workspace(allow_workspace: bool) -> tuple[Path, Path]:
        workspace_root = Path(tempfile.mkdtemp(prefix="moellm-tool-"))
        workspace_root.chmod(0o711)
        workspace = workspace_root / "work"
        workspace.mkdir(mode=0o700)
        if allow_workspace:
            if os.geteuid() == 0:
                os.chown(workspace, 65534, 65534)
            workspace.chmod(0o700)
        else:
            # Root-owned read-only cwd; this is not a mount/filesystem sandbox.
            workspace.chmod(0o555)
        return workspace_root, workspace

    @classmethod
    def _snapshot_compatibility_path(cls, bundle: Path, *, include_tests: bool) -> _SourceSnapshot:
        if bundle.is_symlink():
            raise ValueError("兼容工具路径禁止符号链接")
        if bundle.is_dir():
            source_path = bundle / "tool.py"
            tests_source = cls._read_snapshot_file(bundle / "tests.py") if include_tests else b""
        else:
            if include_tests:
                raise ValueError("单文件兼容工具不提供 tests.py")
            source_path = bundle
            tests_source = b""
        return _SourceSnapshot(
            source=cls._read_snapshot_file(source_path),
            tests_source=tests_source,
            filename=source_path.name,
        )

    def _environment(
        self,
        workspace: Path,
        *,
        capabilities: Mapping[str, bool],
        protocol_fd: int,
    ) -> dict[str, str]:
        if set(capabilities) != _CAPABILITY_FIELDS or not all(
            type(value) is bool for value in capabilities.values()
        ):
            raise TypeError("工具 runner 必须传递五个显式布尔 capability")
        allow_process = capabilities["process"]
        memory = config_parser.get_config("generated_tool_memory_mb", 256) * 1024 * 1024
        file_size = config_parser.get_config("generated_tool_workspace_mb", 64) * 1024 * 1024
        # ``secrets`` is a reserved policy ceiling, not permission to inherit
        # the Bot process environment.  Any future secret provider must copy
        # explicitly named values into this allow-listed mapping; until then,
        # secrets=true intentionally injects nothing.
        return {
            # Keep this aligned with the launcher's fixed process allow-list.
            # Inheriting PATH could accidentally grant an application or
            # virtualenv executable tree to a host_filesystem=false tool.
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            # The host may deliberately make /tmp non-traversable to nobody.
            # The worker is already chdir'd into its private directory before
            # dropping privileges, so expose that directory as a relative path.
            "HOME": ".",
            "TMPDIR": ".",
            "MOELLM_RUNNER_WORKSPACE": ".",
            "MOELLM_RUNNER_CPU": str(config_parser.get_config("generated_tool_cpu_seconds", 10)),
            "MOELLM_RUNNER_MEMORY": str(memory),
            "MOELLM_RUNNER_PROCESSES": str(
                config_parser.get_config("generated_tool_max_processes", 16) if allow_process else 1
            ),
            "MOELLM_RUNNER_ALLOW_PROCESS": "1" if allow_process else "0",
            "MOELLM_RUNNER_FILE_SIZE": str(file_size),
            "MOELLM_RUNNER_OUTPUT": str(config_parser.get_config("generated_tool_output_bytes", 65_536)),
            # Worker moves the inherited descriptor to literal FD 3.
            "MOELLM_RUNNER_PROTOCOL_FD": str(protocol_fd),
            "MOELLM_RUNNER_REQUEST_BYTES": str(_MAX_REQUEST_BYTES),
            "MOELLM_RUNNER_UID": "65534",
            "MOELLM_RUNNER_GID": "65534",
            "MOELLM_RUNNER_CAPABILITIES": json.dumps(
                dict(capabilities),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "MOELLM_RUNNER_PARENT_MOUNT_NS": os.readlink(
                "/proc/self/ns/mnt"
            ),
            "MOELLM_RUNNER_PARENT_PID_NS": os.readlink("/proc/self/ns/pid"),
            "MOELLM_RUNNER_PARENT_IPC_NS": os.readlink("/proc/self/ns/ipc"),
            "MOELLM_RUNNER_PARENT_UTS_NS": os.readlink("/proc/self/ns/uts"),
        }

    def _isolation_command(self, *, disable_network: bool) -> list[str]:
        unshare = shutil.which("unshare")
        if not unshare:
            self.isolation_status = "unavailable:no-unshare"
            raise RuntimeError(
                "Generated Tool 强隔离不可用：未找到 unshare，已拒绝执行"
            )
        if not self._isolation_path.is_file():
            self.isolation_status = "unavailable:no-isolation-launcher"
            raise RuntimeError(
                "Generated Tool 强隔离不可用：缺少 isolation launcher，已拒绝执行"
            )
        command = [
            unshare,
            "--mount",
            "--ipc",
            "--uts",
            "--pid",
            "--fork",
            "--kill-child=SIGKILL",
            "--propagation",
            "private",
        ]
        if disable_network:
            command.append("--net")
        return [
            *command,
            sys.executable,
            "-I",
            str(self._isolation_path),
            str(self._worker_path),
        ]

    @staticmethod
    def _request_bytes(
        snapshot: _SourceSnapshot,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        allow_workspace: bool,
        generated_runtime_guard: bool,
        execution: Mapping[str, Any],
    ) -> bytes:
        if type(generated_runtime_guard) is not bool:
            raise TypeError("generated_runtime_guard 必须是显式布尔值")
        try:
            request = json.dumps(
                {
                    "protocol_version": _PROTOCOL_VERSION,
                    "source": snapshot.source.decode("utf-8"),
                    "tests_source": snapshot.tests_source.decode("utf-8"),
                    "filename": snapshot.filename,
                    "handler": handler,
                    "arguments": arguments,
                    "context": context,
                    "workspace_enabled": allow_workspace,
                    "generated_runtime_guard": generated_runtime_guard,
                    "execution": dict(execution),
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            raise ValueError(f"工具 runner 请求无法序列化: {error}") from error
        if len(request) > _MAX_REQUEST_BYTES:
            raise ValueError("工具 runner 请求超过 512 KiB")
        return request

    @staticmethod
    async def _connect_protocol_reader(fd: int):
        protocol_file = os.fdopen(fd, "rb", buffering=0)
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, protocol_file)
        except BaseException:
            protocol_file.close()
            raise
        return reader, transport

    @staticmethod
    def _parse_protocol(payload: bytes, *, expected_execution: Mapping[str, Any]) -> dict[str, Any]:
        if not payload:
            raise RuntimeError("工具未通过 FD3 返回结构化结果")
        try:
            response = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"工具 FD3 协议响应无效: {error}") from error
        if not isinstance(response, dict):
            raise RuntimeError("工具 FD3 协议响应不是对象")
        if response.get("protocol_version") != _PROTOCOL_VERSION:
            raise RuntimeError("工具 FD3 协议版本不匹配")
        if response.get("execution") != dict(expected_execution):
            raise RuntimeError("工具 FD3 响应与请求固定执行快照不匹配")
        ok = response.get("ok")
        if type(ok) is not bool:
            raise RuntimeError("工具 FD3 协议缺少布尔 ok")
        if ok:
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("工具 FD3 协议缺少 result")
            images = result.get("images") or []
            if not isinstance(images, list) or not all(isinstance(item, str) for item in images):
                raise RuntimeError("工具 FD3 协议 images 非法")
            files = result.get("files", [])
            citations = result.get("citations", [])
            metadata = result.get("metadata", {})
            if not isinstance(files, list):
                raise RuntimeError("工具 FD3 协议 files 非法")
            if not isinstance(citations, list):
                raise RuntimeError("工具 FD3 协议 citations 非法")
            if not isinstance(metadata, dict):
                raise RuntimeError("工具 FD3 协议 metadata 非法")
            return {
                "ok": True,
                "text": str(result.get("text") or ""),
                "images": images,
                "files": files,
                "structured": result.get("structured"),
                "citations": citations,
                "metadata": metadata,
            }
        error = response.get("error")
        if not isinstance(error, dict):
            raise RuntimeError("工具 FD3 失败响应缺少 error")
        return {
            "ok": False,
            "error_type": str(error.get("type") or "ToolError"),
            "error": str(error.get("message") or "执行失败"),
        }

    async def _invoke_snapshot(
        self,
        snapshot: _SourceSnapshot,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        disable_network: bool,
        allow_process: bool,
        allow_workspace: bool,
        allow_host_filesystem: bool = False,
        allow_secrets: bool = False,
        generated_runtime_guard: bool,
        execution: Mapping[str, Any],
    ) -> dict[str, Any]:
        capabilities = {
            "network": not disable_network,
            "process": allow_process,
            "workspace": allow_workspace,
            "host_filesystem": allow_host_filesystem,
            "secrets": allow_secrets,
        }
        if not all(type(value) is bool for value in capabilities.values()):
            raise TypeError("工具 runner capability 必须是显式布尔值")
        request = self._request_bytes(
            snapshot,
            handler,
            arguments,
            context,
            allow_workspace=allow_workspace,
            generated_runtime_guard=generated_runtime_guard,
            execution=execution,
        )
        command = self._isolation_command(disable_network=disable_network)

        workspace_root, workspace = await asyncio.to_thread(
            self._prepare_workspace,
            allow_workspace,
        )

        output_limit = config_parser.get_config("generated_tool_output_bytes", 65_536)
        limits = WorkspaceLimits.from_config()
        started = time.monotonic()
        proc: asyncio.subprocess.Process | None = None
        background_tasks: list[asyncio.Task[Any]] = []
        protocol_transport = None
        protocol_read_fd = -1
        protocol_write_fd = -1
        stdout_size = 0
        stderr_size = 0
        try:
            protocol_read_fd, protocol_write_fd = os.pipe()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workspace,
                    env=self._environment(
                        workspace,
                        capabilities=capabilities,
                        protocol_fd=protocol_write_fd,
                    ),
                    start_new_session=True,
                    pass_fds=(protocol_write_fd,),
                )
            finally:
                if protocol_write_fd >= 0:
                    os.close(protocol_write_fd)
                    protocol_write_fd = -1

            assert proc.stdin
            assert proc.stdout
            assert proc.stderr
            owned_protocol_fd = protocol_read_fd
            protocol_read_fd = -1
            protocol_reader, protocol_transport = await self._connect_protocol_reader(
                owned_protocol_fd
            )
            stdout_task = asyncio.create_task(self._read_limited(proc.stdout, output_limit))
            stderr_task = asyncio.create_task(self._read_limited(proc.stderr, output_limit))
            protocol_task = asyncio.create_task(self._read_limited(protocol_reader, output_limit))
            watcher = asyncio.create_task(self._watch_workspace(workspace, limits))
            read_tasks = [stdout_task, stderr_task, protocol_task]
            background_tasks.extend([*read_tasks, watcher])
            try:
                async with timeout_scope(config_parser.get_config("generated_tool_timeout_seconds", 30)):
                    try:
                        proc.stdin.write(request)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        self.isolation_status = "unavailable:isolation-startup"
                        raise RuntimeError(
                            "Generated Tool 强隔离进程启动失败，已拒绝执行"
                        ) from None
                    finally:
                        proc.stdin.close()

                    wait_task = asyncio.create_task(proc.wait())
                    background_tasks.append(wait_task)
                    monitored = {wait_task, watcher, *read_tasks}
                    while True:
                        done, _ = await asyncio.wait(monitored, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            if task is watcher:
                                raise task.exception() or RuntimeError("工具工作目录监测意外停止")
                            if task in read_tasks and task.exception():
                                raise task.exception()  # type: ignore[misc]
                        if wait_task in done:
                            # PID namespace init is the descendant cleanup
                            # boundary.  unshare does not return until it has
                            # exited and the kernel has killed every namespace
                            # descendant, including processes that called setsid.
                            # Only then is a final workspace scan race-free.
                            await self._scan_workspace_async(workspace, limits)
                            break
                        monitored.difference_update(done)
                    stdout, stderr, protocol_payload = await asyncio.gather(*read_tasks)
                    stdout_size = len(stdout)
                    stderr_size = len(stderr)
            except TimeoutError:
                runtime_metrics.generated_runner_timeouts += 1
                raise RuntimeError("生成工具执行超时") from None
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

            if not protocol_payload:
                stderr_text = stderr.decode("utf-8", "replace")[:300]
                if proc.returncode:
                    self.isolation_status = "unavailable:isolation-startup"
                    raise RuntimeError(
                        "Generated Tool 强隔离进程启动失败，已拒绝执行: "
                        + stderr_text
                    )
                raise RuntimeError(f"工具未通过 FD3 返回结构化结果: {stderr_text}")
            response = self._parse_protocol(protocol_payload, expected_execution=execution)
            self.isolation_status = "ready"
            return response
        except asyncio.CancelledError:
            if proc is not None:
                await asyncio.shield(self._kill(proc))
            raise
        except Exception:
            runtime_metrics.generated_runner_failures += 1
            if proc is not None:
                await self._kill(proc)
            raise
        finally:
            if protocol_write_fd >= 0:
                os.close(protocol_write_fd)
            if protocol_read_fd >= 0:
                os.close(protocol_read_fd)
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            if protocol_transport is not None:
                protocol_transport.close()
            if proc is not None:
                # ``asyncio.subprocess.Process`` has no public close method.
                # On Python 3.10, an output-limit failure can otherwise leave
                # the transport/protocol reference cycle alive until after the
                # per-test or application event loop has closed.  Closing the
                # underlying transport here is idempotent after ``wait()`` and
                # also closes every stdin/stdout/stderr pipe deterministically.
                process_transport = getattr(proc, "_transport", None)
                if process_transport is not None:
                    process_transport.close()
            # Pipe ``connection_lost`` callbacks and the subprocess transport's
            # final callback are scheduled in two stages on Python 3.10.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.to_thread(shutil.rmtree, workspace_root, True)
            logger.debug(
                "工具 runner 完成 "
                f"handler={handler} elapsed={time.monotonic() - started:.3f}s "
                f"stdout_bytes={stdout_size} stderr_bytes={stderr_size}"
            )

    async def _invoke(
        self,
        bundle: Path,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        disable_network: bool,
        allow_process: bool,
        allow_workspace: bool = True,
        allow_host_filesystem: bool = False,
        allow_secrets: bool = False,
        generated_runtime_guard: bool = False,
    ) -> dict[str, Any]:
        """Compatibility/test entry that snapshots a Path exactly once."""
        if not shutil.which("unshare"):
            self.isolation_status = "unavailable:no-unshare"
            raise RuntimeError(
                "Generated Tool 强隔离不可用：未找到 unshare，已拒绝执行"
            )
        snapshot = await asyncio.to_thread(
            self._snapshot_compatibility_path,
            Path(bundle),
            include_tests=handler == "__tests__",
        )
        return await self._invoke_snapshot(
            snapshot,
            handler,
            arguments,
            context,
            disable_network=disable_network,
            allow_process=allow_process,
            allow_workspace=allow_workspace,
            allow_host_filesystem=allow_host_filesystem,
            allow_secrets=allow_secrets,
            generated_runtime_guard=generated_runtime_guard,
            execution={
                "mode": "compatibility",
                "artifact_digest": None,
                "bundle_digest": None,
                "generation": None,
            },
        )

    @staticmethod
    def _tool_result(response: Mapping[str, Any]) -> ToolResult:
        if not response.get("ok"):
            raise RuntimeError(
                f"{response.get('error_type', 'ToolError')}: {response.get('error', '执行失败')}"
            )
        images = response.get("images", ())
        files = response.get("files", ())
        citations = response.get("citations", ())
        if not isinstance(images, (list, tuple)):
            raise RuntimeError("工具结构化结果非法: images 不是数组")
        if not isinstance(files, (list, tuple)):
            raise RuntimeError("工具结构化结果非法: files 不是数组")
        if not isinstance(citations, (list, tuple)):
            raise RuntimeError("工具结构化结果非法: citations 不是数组")
        try:
            return ToolResult(
                text=str(response.get("text") or ""),
                images=tuple(images),
                metadata=response.get("metadata", {}),
                files=tuple(files),
                structured=response.get("structured"),
                citations=tuple(citations),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"工具结构化结果非法: {error}") from None

    async def _execute_with_network_policy(
        self,
        bundle: Path,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        disable_network: bool,
        allow_process: bool,
        allow_workspace: bool,
        allow_host_filesystem: bool,
        allow_secrets: bool,
        generated_runtime_guard: bool,
    ) -> ToolResult:
        async with self._slot():
            invoke_options = {
                "disable_network": disable_network,
                "allow_process": allow_process,
            }
            # Preserve the old monkeypatch/compatibility signature for the
            # default workspace=true path while still supporting an explicit
            # workspace=false compatibility probe.
            if not allow_workspace:
                invoke_options["allow_workspace"] = False
            if allow_host_filesystem:
                invoke_options["allow_host_filesystem"] = True
            if allow_secrets:
                invoke_options["allow_secrets"] = True
            if generated_runtime_guard:
                invoke_options["generated_runtime_guard"] = True
            response = await self._invoke(
                bundle,
                handler,
                arguments,
                context,
                **invoke_options,
            )
        return self._tool_result(response)

    @staticmethod
    def _artifact_capabilities(
        artifact: ToolArtifact,
    ) -> tuple[bool, bool, bool, bool, bool]:
        capabilities = artifact.contract.effective_capabilities
        if not isinstance(capabilities, Mapping):
            raise ValueError("ToolArtifact 缺少固化 effective capabilities")
        allow_network = capabilities.get("network")
        allow_process = capabilities.get("process")
        allow_workspace = capabilities.get("workspace")
        allow_host_filesystem = capabilities.get("host_filesystem")
        allow_secrets = capabilities.get("secrets")
        if set(capabilities) != _CAPABILITY_FIELDS or not all(
            type(value) is bool
            for value in (
                allow_network,
                allow_process,
                allow_workspace,
                allow_host_filesystem,
                allow_secrets,
            )
        ):
            raise ValueError("ToolArtifact effective capabilities 非法")
        if artifact.contract.contract_version == 2:
            structured = artifact.contract.effective_capabilities_v2
            if not isinstance(structured, Mapping):
                raise ValueError("ToolArtifact 缺少固化 capability v2 policy")
            effective_v2 = ToolCapabilityV2.from_mapping(structured)
            if effective_v2.to_legacy().as_dict() != dict(capabilities):
                raise ValueError("ToolArtifact capability v1/v2 投影不一致")
            if not effective_v2.legacy_runner_compatible:
                raise ValueError(
                    "ToolArtifact capability v2 尚未迁移到当前 runner consumer"
                )
        if artifact.source_type == "generated" and (
            allow_network
            or allow_process
            or allow_host_filesystem
            or allow_secrets
        ):
            raise ValueError(
                "Generated ToolArtifact 不得放宽 network/process/"
                "host_filesystem/secrets"
            )
        return (
            cast("bool", allow_network),
            cast("bool", allow_process),
            cast("bool", allow_workspace),
            cast("bool", allow_host_filesystem),
            cast("bool", allow_secrets),
        )

    async def execute_artifact(
        self,
        artifact: ToolArtifact,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        expected_artifact_digest: str,
        expected_bundle_digest: str | None,
        generation: int,
    ) -> ToolResult:
        """Formal execution path; only immutable request-pinned source is used."""
        if not isinstance(artifact, ToolArtifact):
            raise TypeError("execute_artifact 仅接受 ToolArtifact")
        artifact.verify(
            expected_artifact_digest=expected_artifact_digest,
            expected_bundle_digest=expected_bundle_digest,
            generation=generation,
        )
        (
            allow_network,
            allow_process,
            allow_workspace,
            allow_host_filesystem,
            allow_secrets,
        ) = self._artifact_capabilities(artifact)
        execution = {
            "mode": "artifact",
            "artifact_digest": expected_artifact_digest,
            "bundle_digest": expected_bundle_digest,
            "generation": generation,
        }
        async with self._slot():
            response = await self._invoke_snapshot(
                _SourceSnapshot(artifact.source, artifact.tests_source, artifact.filename),
                artifact.handler_name,
                arguments,
                context,
                disable_network=not allow_network,
                allow_process=allow_process,
                allow_workspace=allow_workspace,
                allow_host_filesystem=allow_host_filesystem,
                allow_secrets=allow_secrets,
                generated_runtime_guard=artifact.source_type == "generated",
                execution=execution,
            )
        return self._tool_result(response)

    async def test_artifact(
        self,
        artifact: ToolArtifact,
        *,
        expected_artifact_digest: str,
        expected_bundle_digest: str,
        generation: int,
    ) -> str:
        if not artifact.tests_source:
            raise ValueError("ToolArtifact 不包含 tests_source")
        artifact.verify(
            expected_artifact_digest=expected_artifact_digest,
            expected_bundle_digest=expected_bundle_digest,
            generation=generation,
        )
        _, _, allow_workspace, _, _ = self._artifact_capabilities(artifact)
        execution = {
            "mode": "artifact",
            "artifact_digest": expected_artifact_digest,
            "bundle_digest": expected_bundle_digest,
            "generation": generation,
        }
        async with self._slot():
            response = await self._invoke_snapshot(
                _SourceSnapshot(artifact.source, artifact.tests_source, artifact.filename),
                "__tests__",
                {},
                {},
                disable_network=True,
                allow_process=False,
                allow_workspace=allow_workspace,
                allow_host_filesystem=False,
                allow_secrets=False,
                generated_runtime_guard=True,
                execution=execution,
            )
        return self._tool_result(response).text or "ok"

    async def execute_generated(
        self, bundle: Path, handler: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> ToolResult:
        """Compatibility Path snapshot with generated fail-closed policy."""
        return await self._execute_with_network_policy(
            bundle,
            handler,
            arguments,
            context,
            disable_network=True,
            allow_process=False,
            allow_workspace=True,
            allow_host_filesystem=False,
            allow_secrets=False,
            generated_runtime_guard=True,
        )

    async def execute_custom(
        self,
        bundle: Path,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        allow_network: bool,
        allow_process: bool,
        allow_workspace: bool = True,
        allow_host_filesystem: bool = False,
        allow_secrets: bool = False,
    ) -> ToolResult:
        """Compatibility Path snapshot with explicit custom-file policy."""
        if type(allow_network) is not bool:
            raise TypeError("allow_network 必须是显式布尔策略")
        if type(allow_process) is not bool:
            raise TypeError("allow_process 必须是显式布尔策略")
        if type(allow_workspace) is not bool:
            raise TypeError("allow_workspace 必须是显式布尔策略")
        if type(allow_host_filesystem) is not bool:
            raise TypeError("allow_host_filesystem 必须是显式布尔策略")
        if type(allow_secrets) is not bool:
            raise TypeError("allow_secrets 必须是显式布尔策略")
        return await self._execute_with_network_policy(
            bundle,
            handler,
            arguments,
            context,
            disable_network=not allow_network,
            allow_process=allow_process,
            allow_workspace=allow_workspace,
            allow_host_filesystem=allow_host_filesystem,
            allow_secrets=allow_secrets,
            generated_runtime_guard=False,
        )

    async def execute(
        self, bundle: Path, handler: str, arguments: dict[str, Any], context: dict[str, Any]
    ) -> ToolResult:
        """Legacy alias retained only for compatibility and tests."""
        return await self.execute_generated(bundle, handler, arguments, context)

    async def run_tests(self, bundle: Path) -> str:
        async with self._slot():
            response = await self._invoke(
                bundle,
                "__tests__",
                {},
                {},
                disable_network=True,
                allow_process=False,
                allow_workspace=True,
                allow_host_filesystem=False,
                allow_secrets=False,
                generated_runtime_guard=True,
            )
        return self._tool_result(response).text or "ok"

    async def preflight(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="moellm-preflight-"))
        source = root / "probe.py"
        try:
            def prepare_probe() -> None:
                source.write_text(
                    "import os\n"
                    "from pathlib import Path\n\n"
                    "async def probe(parent_net_ns, parent_ipc_ns, parent_uts_ns):\n"
                    "    clean = not bool(os.getenv('QI_WEB_DATABASE_URL'))\n"
                    "    isolated = os.readlink('/proc/self/ns/net') != parent_net_ns\n"
                    "    ipc_isolated = (\n"
                    "        os.readlink('/proc/self/ns/ipc') != parent_ipc_ns\n"
                    "    )\n"
                    "    uts_isolated = (\n"
                    "        os.readlink('/proc/self/ns/uts') != parent_uts_ns\n"
                    "    )\n"
                    "    domainname = Path(\n"
                    "        '/proc/sys/kernel/domainname'\n"
                    "    ).read_text(encoding='utf-8').strip()\n"
                    "    return (\n"
                    "        f'{os.geteuid()}:{os.getegid()}:{clean}:'\n"
                    "        f'{isolated}:{ipc_isolated}:{uts_isolated}:'\n"
                    "        f'{os.uname().nodename}:{domainname}'\n"
                    "    )\n",
                    encoding="utf-8",
                )
                root.chmod(0o755)
                source.chmod(0o644)

            await asyncio.to_thread(prepare_probe)
            parent_net_ns = os.readlink("/proc/self/ns/net")
            parent_ipc_ns = os.readlink("/proc/self/ns/ipc")
            parent_uts_ns = os.readlink("/proc/self/ns/uts")
            result = await self.execute_generated(
                source,
                "probe",
                {
                    "parent_net_ns": parent_net_ns,
                    "parent_ipc_ns": parent_ipc_ns,
                    "parent_uts_ns": parent_uts_ns,
                },
                {},
            )
            if result.text != (
                "65534:65534:True:True:True:True:"
                "moellm-sandbox:localdomain"
            ):
                raise RuntimeError(f"生成工具隔离探针返回异常: {result.text[:100]}")
            self.isolation_status = "ready"
        except Exception as error:
            if not self.isolation_status.startswith("unavailable:"):
                self.isolation_status = f"unavailable:{type(error).__name__}"
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)


generated_tool_runner = GeneratedToolRunner()

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
from typing import Any

from nonebot.log import logger

from .compat import timeout as timeout_scope
from .config import config_parser
from .runtime_metrics import runtime_metrics
from .tool_contracts import ToolResult


class GeneratedToolBusy(RuntimeError):
    pass


class GeneratedToolRunner:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(1)
        self._pending = 0
        self._state_lock = asyncio.Lock()
        self._worker_path = Path(__file__).with_name("generated_tool_worker.py")
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
    def _workspace_size(path: Path) -> int:
        total = 0
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    async def _watch_workspace(self, path: Path, maximum: int) -> None:
        while True:
            await asyncio.sleep(0.1)
            if self._workspace_size(path) > maximum:
                raise RuntimeError("生成工具工作目录超过容量限制")

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
        parts: list[bytes] = []
        size = 0
        while chunk := await stream.read(4096):
            size += len(chunk)
            if size > limit:
                raise RuntimeError("生成工具输出超过限制")
            parts.append(chunk)
        return b"".join(parts)

    @staticmethod
    async def _kill(
        proc: asyncio.subprocess.Process, *, forced: bool = True
    ) -> None:
        if forced:
            runtime_metrics.generated_runner_killed += 1
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.returncode is None:
            await proc.wait()

    @staticmethod
    def _cleanup_descendants(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            runtime_metrics.generated_runner_orphan_cleanups += 1
        except ProcessLookupError:
            pass

    def _environment(self, workspace: Path) -> dict[str, str]:
        memory = config_parser.get_config("generated_tool_memory_mb", 256) * 1024 * 1024
        file_size = config_parser.get_config("generated_tool_workspace_mb", 64) * 1024 * 1024
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "HOME": str(workspace),
            "TMPDIR": str(workspace),
            "MOELLM_RUNNER_WORKSPACE": str(workspace),
            "MOELLM_RUNNER_CPU": str(config_parser.get_config("generated_tool_cpu_seconds", 10)),
            "MOELLM_RUNNER_MEMORY": str(memory),
            "MOELLM_RUNNER_PROCESSES": str(config_parser.get_config("generated_tool_max_processes", 16)),
            "MOELLM_RUNNER_FILE_SIZE": str(file_size),
            "MOELLM_RUNNER_OUTPUT": str(config_parser.get_config("generated_tool_output_bytes", 65536)),
            "MOELLM_RUNNER_UID": "65534",
            "MOELLM_RUNNER_GID": "65534",
        }

    async def _invoke(
        self,
        bundle: Path,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        disable_network: bool = False,
    ) -> dict[str, Any]:
        command = [sys.executable, "-I", str(self._worker_path), str(bundle), handler]
        if disable_network:
            unshare = shutil.which("unshare")
            if not unshare:
                self.isolation_status = "unavailable:no-network-namespace"
                raise RuntimeError("生成工具测试网络隔离不可用")
            command = [unshare, "--net", "--fork", *command]
        workspace_root = Path(tempfile.mkdtemp(prefix="moellm-tool-"))
        os.chmod(workspace_root, 0o711)
        workspace = workspace_root / "work"
        workspace.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(workspace, 65534, 65534)
        output_limit = config_parser.get_config("generated_tool_output_bytes", 65536)
        workspace_limit = config_parser.get_config("generated_tool_workspace_mb", 64) * 1024 * 1024
        request = json.dumps(
            {"arguments": arguments, "context": context}, ensure_ascii=False
        ).encode("utf-8")
        started = time.monotonic()
        proc: asyncio.subprocess.Process | None = None
        background_tasks: list[asyncio.Task] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                env=self._environment(workspace),
                start_new_session=True,
            )
            assert proc.stdin
            assert proc.stdout
            assert proc.stderr
            proc.stdin.write(request)
            await proc.stdin.drain()
            proc.stdin.close()
            read_tasks = [
                asyncio.create_task(self._read_limited(proc.stdout, output_limit)),
                asyncio.create_task(self._read_limited(proc.stderr, output_limit)),
            ]
            watcher = asyncio.create_task(
                self._watch_workspace(workspace, workspace_limit)
            )
            background_tasks.extend([*read_tasks, watcher])
            try:
                async with timeout_scope(
                    config_parser.get_config("generated_tool_timeout_seconds", 30)
                ):
                    wait_task = asyncio.create_task(proc.wait())
                    background_tasks.append(wait_task)
                    monitored = {wait_task, watcher, *read_tasks}
                    while True:
                        done, _ = await asyncio.wait(
                            monitored, return_when=asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            if task is watcher:
                                raise task.exception() or RuntimeError(
                                    "生成工具工作目录监测意外停止"
                                )
                            if task in read_tasks and task.exception():
                                raise task.exception()
                        if wait_task in done:
                            self._cleanup_descendants(proc)
                            break
                        monitored.difference_update(done)
                    stdout, stderr = await asyncio.gather(*read_tasks)
            except TimeoutError:
                runtime_metrics.generated_runner_timeouts += 1
                raise RuntimeError("生成工具执行超时") from None
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            if not stdout:
                raise RuntimeError(
                    f"生成工具未返回结构化结果: {stderr.decode('utf-8', 'replace')[:300]}"
                )
            response = json.loads(stdout)
            if not isinstance(response, dict):
                raise RuntimeError("生成工具返回值不是对象")
            if not response.get("ok"):
                raise RuntimeError(
                    f"{response.get('error_type', 'ToolError')}: {response.get('error', '执行失败')}"
                )
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
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            shutil.rmtree(workspace_root, ignore_errors=True)
            logger.debug(
                f"生成工具 runner 完成 handler={handler} elapsed={time.monotonic() - started:.3f}s"
            )

    async def execute(
        self,
        bundle: Path,
        handler: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ToolResult:
        async with self._slot():
            response = await self._invoke(bundle, handler, arguments, context)
        return ToolResult(
            text=str(response.get("text") or ""),
            images=tuple(response.get("images") or ()),
        )

    async def run_tests(self, bundle: Path) -> str:
        async with self._slot():
            response = await self._invoke(
                bundle, "__tests__", {}, {}, disable_network=True
            )
        return str(response.get("text") or "ok")

    async def preflight(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="moellm-preflight-"))
        source = root / "probe.py"
        try:
            def prepare_probe() -> None:
                source.write_text(
                    "import os\n\n"
                    "async def probe():\n"
                    "    clean = not bool(os.getenv('QI_WEB_DATABASE_URL'))\n"
                    "    return f'{os.geteuid()}:{os.getegid()}:{clean}'\n",
                    encoding="utf-8",
                )
                root.chmod(0o755)
                source.chmod(0o644)

            await asyncio.to_thread(prepare_probe)
            result = await self.execute(source, "probe", {}, {})
            if result.text != "65534:65534:True":
                raise RuntimeError(f"生成工具隔离探针返回异常: {result.text[:100]}")
            self.isolation_status = "ready"
        except Exception as error:
            self.isolation_status = f"unavailable:{type(error).__name__}"
            raise
        finally:
            shutil.rmtree(root, ignore_errors=True)


generated_tool_runner = GeneratedToolRunner()

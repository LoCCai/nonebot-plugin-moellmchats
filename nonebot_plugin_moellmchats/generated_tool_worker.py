"""Small, stdlib-only worker used to execute immutable tool snapshots."""

from __future__ import annotations

import ast
import asyncio
import builtins
import ctypes
import errno
import importlib
import inspect
import json
import os
from pathlib import Path, PurePath
import resource
import sys
import time
import traceback
import types
from typing import Any

_PROTOCOL_VERSION = 1
_MAX_SOURCE_BYTES = 65_536
_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_PROCESS_SYSCALLS = ("execve", "execveat", "fork", "vfork", "clone", "clone3")
_GENERATED_DENIED_BUILTINS = frozenset(
    {
        "__import__",
        "compile",
        "delattr",
        "eval",
        "exec",
        "getattr",
        "globals",
        "locals",
        "setattr",
        "vars",
    }
)
_GENERATED_DENIED_IMPORT_ROOTS = frozenset(
    {
        "aiohttp",
        "builtins",
        "ctypes",
        "ftplib",
        "http",
        "httpx",
        "multiprocessing",
        "pickle",
        "posix",
        "pty",
        "requests",
        "smtplib",
        "socket",
        "subprocess",
        "urllib",
        "websockets",
    }
)


class _FrozenBuiltins(dict[str, Any]):
    """Python 3.10-compatible builtins mapping with no public mutation API."""

    @staticmethod
    def _deny_mutation(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("generated builtins are read-only")

    __delitem__ = _deny_mutation
    __ior__ = _deny_mutation
    __setitem__ = _deny_mutation
    clear = _deny_mutation
    pop = _deny_mutation
    popitem = _deny_mutation
    setdefault = _deny_mutation
    update = _deny_mutation


class _GeneratedRuntimeGuard:
    """Defense in depth for code already accepted by the generated AST policy."""

    def __init__(self, sources: tuple[str, ...]) -> None:
        declared_roots: set[str] = set()
        for source in sources:
            if not source:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    declared_roots.update(
                        alias.name.split(".", 1)[0] for alias in node.names
                    )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module
                ):
                    declared_roots.add(node.module.split(".", 1)[0])
        self.declared_import_roots = frozenset(declared_roots)

        controlled = dict(vars(builtins))
        for name in _GENERATED_DENIED_BUILTINS:
            controlled.pop(name, None)
        controlled["__import__"] = self.guarded_import
        # CPython 3.10's IMPORT_NAME assumes frame builtins are a real dict and
        # raises an internal SystemError for MappingProxyType. Generated AST
        # policy denies every route to __builtins__/function globals, while this
        # fallback also rejects all normal dict mutation APIs. Keep the stronger
        # mapping proxy on runtimes where imports support arbitrary mappings.
        if sys.version_info < (3, 11):
            self.builtins = _FrozenBuiltins(controlled)
        else:
            self.builtins = types.MappingProxyType(controlled)

    @staticmethod
    def _root(name: str) -> str:
        if not isinstance(name, str) or not name:
            raise RuntimeError("generated import name is invalid")
        return name.split(".", 1)[0]

    def _check_import(
        self,
        name: str,
        *,
        level: int,
        require_declaration: bool,
    ) -> None:
        if level != 0 or name.startswith("."):
            raise RuntimeError("generated relative imports are denied")
        root = self._root(name)
        if root in _GENERATED_DENIED_IMPORT_ROOTS:
            raise RuntimeError(f"generated import is denied: {root}")
        if require_declaration and root not in self.declared_import_roots:
            raise RuntimeError(f"generated dynamic import is not declared: {root}")

    def guarded_import(
        self,
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] | list[str] = (),
        level: int = 0,
    ) -> Any:
        self._check_import(
            name,
            level=level,
            require_declaration=True,
        )
        return builtins.__import__(name, globals, locals, fromlist, level)

    def import_module(self, name: str, package: str | None = None) -> Any:
        del package
        self._check_import(
            name,
            level=0,
            require_declaration=True,
        )
        return self._original_import_module(name)

    def find_spec(
        self,
        fullname: str,
        _path: Any = None,
        _target: Any = None,
    ) -> None:
        self._check_import(
            fullname,
            level=0,
            require_declaration=False,
        )
        return None

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        # importlib/runpy bypass the module's ``__import__``. Audit hooks cannot
        # be removed by untrusted code, so keep the deny set effective there too.
        if event != "import" or not args:
            return
        name = args[0]
        if not isinstance(name, str):
            raise RuntimeError("generated audit import name is invalid")
        self._check_import(
            name,
            level=0,
            require_declaration=False,
        )

    def install(self) -> None:
        self._original_import_module = importlib.import_module
        importlib.import_module = self.import_module
        importlib.__import__ = self.guarded_import
        sys.meta_path.insert(0, self)
        sys.addaudithook(self.audit)


def _install_generated_runtime_guard(
    source: str,
    tests_source: str,
) -> _GeneratedRuntimeGuard:
    guard = _GeneratedRuntimeGuard((source, tests_source))
    guard.install()
    return guard


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name, "1" if default else "0")
    if value not in {"0", "1"}:
        raise RuntimeError(f"invalid boolean runner setting: {name}")
    return value == "1"


def _prepare_protocol_fd() -> int:
    """Move the inherited protocol pipe to literal FD 3 inside the worker."""
    raw = os.environ.pop("MOELLM_RUNNER_PROTOCOL_FD", "")
    try:
        inherited_fd = int(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError("protocol FD is unavailable") from error
    if inherited_fd < 3:
        raise RuntimeError("protocol FD is invalid")
    if inherited_fd != 3:
        os.dup2(inherited_fd, 3, inheritable=False)
        os.close(inherited_fd)
    else:
        os.set_inheritable(3, False)
    return 3


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise RuntimeError("protocol FD write failed")
        view = view[written:]


def _emit_protocol(fd: int, response: dict[str, Any]) -> None:
    payload = json.dumps(
        response,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    _write_all(fd, payload)


def _load_seccomp_library():
    errors = []
    for name in ("libseccomp.so.2", "libseccomp.so"):
        try:
            return ctypes.CDLL(name, use_errno=True)
        except OSError as error:
            errors.append(str(error))
    raise RuntimeError(
        "process isolation unavailable: libseccomp could not be loaded: "
        + "; ".join(errors)
    )


def _install_process_filter() -> None:
    """Deny process creation and image replacement for process=false tools."""
    library = _load_seccomp_library()
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_release.restype = None
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_load.restype = ctypes.c_int

    context = library.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise RuntimeError("process isolation unavailable: seccomp_init failed")
    try:
        deny_action = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _PROCESS_SYSCALLS:
            syscall = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if syscall < 0:
                raise RuntimeError(
                    f"process isolation unavailable: syscall is unresolved: {name}"
                )
            result = library.seccomp_rule_add(context, deny_action, syscall, 0)
            if result < 0:
                error_number = -result
                raise RuntimeError(
                    f"process isolation unavailable: cannot deny {name}: "
                    f"[Errno {error_number}] {os.strerror(error_number)}"
                )
        result = library.seccomp_load(context)
        if result < 0:
            error_number = -result
            raise RuntimeError(
                "process isolation unavailable: seccomp_load failed: "
                f"[Errno {error_number}] {os.strerror(error_number)}"
            )
    finally:
        library.seccomp_release(context)


def _apply_limits() -> None:
    cpu = _int_env("MOELLM_RUNNER_CPU", 10)
    memory = _int_env("MOELLM_RUNNER_MEMORY", 256 * 1024 * 1024)
    processes = _int_env("MOELLM_RUNNER_PROCESSES", 16)
    file_size = _int_env("MOELLM_RUNNER_FILE_SIZE", 64 * 1024 * 1024)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    libc = ctypes.CDLL(None)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise RuntimeError("cannot enable no_new_privs")

    target_uid = _int_env("MOELLM_RUNNER_UID", 65534)
    target_gid = _int_env("MOELLM_RUNNER_GID", 65534)
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(target_gid)
        os.setuid(target_uid)
    if os.geteuid() != target_uid or os.getegid() != target_gid:
        raise RuntimeError("generated tool worker could not enter nobody identity")
    if not _bool_env("MOELLM_RUNNER_ALLOW_PROCESS", False):
        _install_process_filter()


def _read_request() -> dict[str, Any]:
    limit = _int_env("MOELLM_RUNNER_REQUEST_BYTES", 524_288)
    payload = sys.stdin.buffer.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("request exceeds configured limit")
    request = json.loads(payload)
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    return request


def _validate_request(request: dict[str, Any]) -> None:
    if request.get("protocol_version") != _PROTOCOL_VERSION:
        raise ValueError("request protocol version mismatch")
    for key in ("source", "tests_source", "filename", "handler"):
        if not isinstance(request.get(key), str):
            raise ValueError(f"request field must be a string: {key}")
    if len(request["source"].encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("source snapshot exceeds 64 KiB")
    if len(request["tests_source"].encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("tests snapshot exceeds 64 KiB")
    filename = request["filename"]
    if (
        not filename
        or PurePath(filename).name != filename
        or filename in {".", ".."}
        or "\0" in filename
    ):
        raise ValueError("filename must be a safe basename")
    handler = request["handler"]
    if handler != "__tests__" and not handler.isidentifier():
        raise ValueError("handler name is invalid")
    if not isinstance(request.get("arguments"), dict):
        raise ValueError("arguments must be an object")
    if not isinstance(request.get("context"), dict):
        raise ValueError("context must be an object")
    if type(request.get("workspace_enabled")) is not bool:
        raise ValueError("workspace_enabled must be boolean")
    if type(request.get("generated_runtime_guard")) is not bool:
        raise ValueError("generated_runtime_guard must be boolean")
    execution = request.get("execution")
    if not isinstance(execution, dict) or execution.get("mode") not in {
        "artifact",
        "compatibility",
    }:
        raise ValueError("execution metadata is invalid")


def _load_module(
    path: Path,
    name: str,
    source: str,
    *,
    generated_guard: _GeneratedRuntimeGuard | None = None,
):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    if generated_guard is not None:
        module.__dict__["__builtins__"] = generated_guard.builtins
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


async def _run_handler(module: Any, request: dict[str, Any]) -> Any:
    handler = getattr(module, request["handler"], None)
    if not callable(handler):
        raise RuntimeError(f"handler not found: {request['handler']}")
    arguments = dict(request["arguments"])
    # Hidden context cannot be forged through model arguments.
    arguments.pop("_tool_context", None)
    arguments.pop("_workspace", None)
    signature = inspect.signature(handler)
    if "_tool_context" in signature.parameters:
        arguments["_tool_context"] = request["context"]
    if request["workspace_enabled"] and "_workspace" in signature.parameters:
        arguments["_workspace"] = os.environ["MOELLM_RUNNER_WORKSPACE"]
    result = handler(**arguments)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _run_tests(
    module: Any,
    tests_source: str,
    *,
    generated_guard: _GeneratedRuntimeGuard | None = None,
) -> Any:
    if not tests_source:
        raise RuntimeError("tests source is unavailable")
    tests = _load_module(
        Path("tests.py"),
        "moellm_generated_tests",
        tests_source,
        generated_guard=generated_guard,
    )
    handler = getattr(tests, "run_tests", None)
    if not callable(handler):
        raise RuntimeError("tests.py must define run_tests(tool_module)")
    result = handler(module)
    if inspect.isawaitable(result):
        result = await result
    return result


def _normalize_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        text = result.get("text") or result.get("content") or result.get("message") or ""
        images = result.get("images") or result.get("image_urls") or []
    else:
        text = str(result)
        images = []
    if isinstance(images, str):
        images = [images]
    return {
        "text": str(text),
        "images": [str(item) for item in images if isinstance(item, str)],
    }


def _runtime_guard_for_request(
    request: dict[str, Any],
) -> _GeneratedRuntimeGuard | None:
    if not request["generated_runtime_guard"]:
        return None
    return _install_generated_runtime_guard(
        request["source"],
        request["tests_source"],
    )


def main() -> int:
    started = time.monotonic()
    protocol_fd = -1
    execution: dict[str, Any] = {}
    ok = False
    try:
        protocol_fd = _prepare_protocol_fd()
        request = _read_request()
        if isinstance(request.get("execution"), dict):
            execution = dict(request["execution"])
        _validate_request(request)
        # Limits, UID drop and seccomp are active before snapshot compilation.
        _apply_limits()
        generated_guard = _runtime_guard_for_request(request)
        module = _load_module(
            Path(request["filename"]),
            "moellm_generated_tool",
            request["source"],
            generated_guard=generated_guard,
        )
        if request["handler"] == "__tests__":
            result = asyncio.run(
                _run_tests(
                    module,
                    request["tests_source"],
                    generated_guard=generated_guard,
                )
            )
        else:
            result = asyncio.run(_run_handler(module, request))
        response = {
            "protocol_version": _PROTOCOL_VERSION,
            "ok": True,
            "execution": execution,
            "result": _normalize_result(result),
            "metrics": {"elapsed_ms": int((time.monotonic() - started) * 1000)},
        }
        ok = True
    except BaseException as error:
        response = {
            "protocol_version": _PROTOCOL_VERSION,
            "ok": False,
            "execution": execution,
            "error": {
                "type": type(error).__name__,
                "message": str(error)[:1000],
                "traceback": "".join(
                    traceback.format_exception_only(type(error), error)
                )[:1000],
            },
            "metrics": {"elapsed_ms": int((time.monotonic() - started) * 1000)},
        }
    if protocol_fd < 0:
        sys.stderr.write(json.dumps(response, ensure_ascii=False)[:2000])
        return 2
    try:
        _emit_protocol(protocol_fd, response)
    except BaseException as error:
        sys.stderr.write(f"protocol emit failed: {error}\n")
        return 2
    finally:
        try:
            os.close(protocol_fd)
        except OSError:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

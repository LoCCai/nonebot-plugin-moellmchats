"""Small, stdlib-only worker used to execute approved generated tools."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import inspect
import io
import json
import os
from pathlib import Path
import resource
import sys
import traceback
import types
from typing import Any


class _BoundedTextIO(io.TextIOBase):
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0

    def write(self, value: str) -> int:
        value = str(value)
        remaining = max(0, self.limit - self.size)
        if remaining:
            self.parts.append(value[:remaining])
            self.size += min(len(value), remaining)
        return len(value)

    def text(self) -> str:
        return "".join(self.parts)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


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

    # PR_SET_NO_NEW_PRIVS is irreversible and prevents privilege-gaining execs.
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


def _load_module(path: Path, name: str, source: str):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


async def _run_handler(module: Any, handler_name: str, request: dict[str, Any]) -> Any:
    handler = getattr(module, handler_name, None)
    if not callable(handler):
        raise RuntimeError(f"handler not found: {handler_name}")
    arguments = request.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    signature = inspect.signature(handler)
    if "_tool_context" in signature.parameters:
        arguments["_tool_context"] = request.get("context") or {}
    if "_workspace" in signature.parameters:
        arguments["_workspace"] = os.environ["MOELLM_RUNNER_WORKSPACE"]
    result = handler(**arguments)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _run_tests(bundle: Path, module: Any, tests_source: str) -> Any:
    tests_path = bundle / "tests.py"
    tests = _load_module(tests_path, "moellm_generated_tests", tests_source)
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
        "ok": True,
        "text": str(text),
        "images": [str(item) for item in images if isinstance(item, str)],
    }


def main() -> int:
    output_limit = _int_env("MOELLM_RUNNER_OUTPUT", 65536)
    captured = _BoundedTextIO(output_limit)
    try:
        bundle = Path(sys.argv[1]).resolve(strict=True)
        bundle_is_file = bundle.is_file()
        tool_path = bundle if bundle_is_file else bundle / "tool.py"
        handler_name = sys.argv[2]
        # Read approved, bounded source before dropping privileges. No untrusted
        # code is compiled or executed until after limits and UID isolation.
        source = tool_path.read_text(encoding="utf-8")
        tests_source = (
            (bundle / "tests.py").read_text(encoding="utf-8")
            if handler_name == "__tests__" and not bundle_is_file
            else ""
        )
        _apply_limits()
        request = json.loads(sys.stdin.read(output_limit + 1))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            module = _load_module(tool_path, "moellm_generated_tool", source)
            if handler_name == "__tests__":
                if bundle_is_file:
                    raise RuntimeError("single-file tools do not provide self tests")
                result = asyncio.run(_run_tests(bundle, module, tests_source))
            else:
                result = asyncio.run(_run_handler(module, handler_name, request))
        response = _normalize_result(result)
        if captured.text():
            response["logs"] = captured.text()
    except BaseException as error:
        response = {
            "ok": False,
            "error_type": type(error).__name__,
            "error": str(error)[:1000],
            "traceback": "".join(traceback.format_exception_only(type(error), error))[:1000],
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

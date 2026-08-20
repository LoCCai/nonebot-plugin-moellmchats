from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from nonebot_plugin_moellmchats import generated_tool_worker


def _valid_request(**updates) -> dict:
    request = {
        "protocol_version": 1,
        "source": "async def probe():\n    return 'ok'\n",
        "tests_source": "",
        "filename": "custom.py",
        "handler": "probe",
        "arguments": {},
        "context": {},
        "workspace_enabled": False,
        "generated_runtime_guard": False,
        "execution": {"mode": "compatibility"},
    }
    request.update(updates)
    return request


def test_process_isolation_fails_closed_without_libseccomp(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise OSError("libseccomp missing")

    monkeypatch.setattr(generated_tool_worker.ctypes, "CDLL", unavailable)

    with pytest.raises(RuntimeError, match=r"process isolation unavailable.*libseccomp"):
        generated_tool_worker._install_process_filter()


def test_generated_runtime_guard_allows_declared_stdlib_and_freezes_builtins() -> None:
    source = (
        "import json\n"
        "from decimal import Decimal\n\n"
        "async def probe():\n"
        "    return json.dumps({'value': str(Decimal('1.5'))}, sort_keys=True)\n"
    )
    guard = generated_tool_worker._GeneratedRuntimeGuard((source, ""))
    module = generated_tool_worker._load_module(
        Path("tool.py"),
        "guarded_allowed_stdlib",
        source,
        generated_guard=guard,
    )

    guarded_builtins = module.__dict__["__builtins__"]
    assert isinstance(guarded_builtins, Mapping)
    assert "eval" not in guarded_builtins
    assert "compile" not in guarded_builtins
    with pytest.raises(TypeError):
        guarded_builtins["eval"] = eval
    assert asyncio.run(module.probe()) == '{"value": "1.5"}'


@pytest.mark.parametrize(
    ("source", "error"),
    [
        (
            "async def probe():\n"
            "    return __builtins__['eval']('40 + 2')\n",
            KeyError,
        ),
        (
            "async def probe():\n"
            "    return __builtins__['__import__']('pty')\n",
            RuntimeError,
        ),
        (
            "async def probe():\n"
            "    return __builtins__['__import__']('math')\n",
            RuntimeError,
        ),
    ],
)
def test_generated_runtime_guard_blocks_dynamic_builtins_and_imports(
    source: str,
    error: type[BaseException],
) -> None:
    guard = generated_tool_worker._GeneratedRuntimeGuard((source, ""))
    module = generated_tool_worker._load_module(
        Path("tool.py"),
        "guarded_dynamic_execution",
        source,
        generated_guard=guard,
    )

    with pytest.raises(error):
        asyncio.run(module.probe())


def test_generated_runtime_audit_hook_blocks_importlib_bypass(monkeypatch) -> None:
    hooks = []
    monkeypatch.setattr(
        generated_tool_worker.sys,
        "meta_path",
        list(generated_tool_worker.sys.meta_path),
    )
    monkeypatch.setattr(
        generated_tool_worker.importlib,
        "import_module",
        generated_tool_worker.importlib.import_module,
    )
    monkeypatch.setattr(
        generated_tool_worker.importlib,
        "__import__",
        generated_tool_worker.importlib.__import__,
    )
    monkeypatch.setattr(generated_tool_worker.sys, "addaudithook", hooks.append)
    guard = generated_tool_worker._install_generated_runtime_guard(
        "import importlib\n",
        "",
    )

    assert hooks == [guard.audit]
    assert generated_tool_worker.sys.meta_path[0] is guard
    with pytest.raises(RuntimeError, match=r"generated import is denied: pty"):
        generated_tool_worker.importlib.import_module("pty")
    with pytest.raises(RuntimeError, match=r"generated import is denied: pty"):
        generated_tool_worker.importlib.__import__("pty")
    with pytest.raises(RuntimeError, match=r"generated import is denied: pty"):
        hooks[0]("import", ("pty", None, None, None, None))
    hooks[0]("import", ("decimal", None, None, None, None))


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", [], {}])
def test_worker_request_rejects_missing_or_non_boolean_runtime_guard(
    invalid: object,
) -> None:
    request = _valid_request(generated_runtime_guard=invalid)
    if invalid is None:
        request.pop("generated_runtime_guard")

    with pytest.raises(
        ValueError,
        match="generated_runtime_guard must be boolean",
    ):
        generated_tool_worker._validate_request(request)


def test_worker_routes_runtime_guard_only_by_explicit_protocol_field(
    monkeypatch,
) -> None:
    installed = []
    marker = object()

    def install(source: str, tests_source: str):
        installed.append((source, tests_source))
        return marker

    monkeypatch.setattr(
        generated_tool_worker,
        "_install_generated_runtime_guard",
        install,
    )
    generated = _valid_request(
        generated_runtime_guard=True,
        filename="custom-looking.py",
        tests_source="",
        execution={"mode": "compatibility", "bundle_digest": None},
    )
    custom = _valid_request(
        generated_runtime_guard=False,
        filename="tool.py",
        tests_source="async def run_tests(tool_module):\n    return 'ok'\n",
        execution={"mode": "artifact", "bundle_digest": "trusted-looking"},
    )

    generated_tool_worker._validate_request(generated)
    generated_tool_worker._validate_request(custom)
    assert generated_tool_worker._runtime_guard_for_request(generated) is marker
    assert generated_tool_worker._runtime_guard_for_request(custom) is None
    assert installed == [(generated["source"], generated["tests_source"])]


def test_custom_worker_module_keeps_regular_builtins() -> None:
    module = generated_tool_worker._load_module(
        Path("custom.py"),
        "custom_regular_builtins",
        "async def probe():\n    return eval('40 + 2')\n",
    )

    assert asyncio.run(module.probe()) == 42

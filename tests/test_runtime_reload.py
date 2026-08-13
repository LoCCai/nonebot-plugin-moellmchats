from __future__ import annotations

from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.runtime_reload import runtime_reloader
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots
from nonebot_plugin_moellmchats.tool_manager import tool_manager


@pytest.mark.asyncio
async def test_invalid_config_retains_previous_generation() -> None:
    await runtime_reloader.reload("test-baseline")
    previous = runtime_snapshots.current()
    path = runtime_reloader.watched_paths()[0]
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text('{"broken":', encoding="utf-8")
        with pytest.raises(Exception, match=r"Expected|JSON|value|object"):
            await runtime_reloader.reload("test-invalid-json")
        assert runtime_snapshots.current() is previous
    finally:
        path.write_text(original, encoding="utf-8")
        await runtime_reloader.reload("test-restore-json")


@pytest.mark.asyncio
async def test_invalid_tool_source_retains_previous_generation() -> None:
    await runtime_reloader.reload("test-tool-baseline")
    previous = runtime_snapshots.current()
    path = tool_manager.custom_tools_dir / "broken_reload_test.py"
    try:
        path.write_text("async def broken(:\n", encoding="utf-8")
        with pytest.raises(Exception, match="自定义工具"):
            await runtime_reloader.reload("test-invalid-tool")
        assert runtime_snapshots.current() is previous
    finally:
        path.unlink(missing_ok=True)
        await runtime_reloader.reload("test-restore-tool")


@pytest.mark.asyncio
async def test_unreachable_mcp_retains_previous_generation(monkeypatch) -> None:
    await runtime_reloader.reload("test-mcp-baseline")
    previous = runtime_snapshots.current()

    from nonebot_plugin_moellmchats import mcp_manager as module

    monkeypatch.setattr(
        module.mcp_manager,
        "load_config_candidate",
        lambda: {
            "broken": {
                "enabled": True,
                "transport": "streamable_http",
                "url": "http://127.0.0.1:1/mcp",
                "discover_timeout": 1,
            }
        },
    )

    async def fail(*args, **kwargs):
        raise ConnectionError("unreachable")

    monkeypatch.setattr(module.mcp_manager, "_list_tools_from_server", fail)
    with pytest.raises(Exception, match="unreachable"):
        await runtime_reloader.reload("test-unreachable-mcp")
    assert runtime_snapshots.current() is previous


def test_watched_paths_include_all_runtime_resources() -> None:
    names = {Path(path).name for path in runtime_reloader.watched_paths()}
    assert {
        "config.json",
        "providers.toml",
        "model_config.json",
        "temperaments.json",
        "temperament_config.json",
        "replies.toml",
        "custom_plugin_info.json",
        "mcp_servers.toml",
    } <= names

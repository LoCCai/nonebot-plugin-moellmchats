from __future__ import annotations

from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.runtime_reload import runtime_reloader
from nonebot_plugin_moellmchats.runtime_snapshot import mutable_value, runtime_snapshots
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


@pytest.mark.asyncio
async def test_commit_failure_restores_all_manager_state(monkeypatch) -> None:
    from nonebot_plugin_moellmchats.config import config_parser
    from nonebot_plugin_moellmchats.mcp_manager import mcp_manager
    from nonebot_plugin_moellmchats.model_selector import model_selector
    from nonebot_plugin_moellmchats.temperament_manager import temperament_manager

    await runtime_reloader.reload("test-transaction-baseline")
    previous_snapshot = runtime_snapshots.current()
    previous = {
        "config": mutable_value(config_parser.config),
        "model": model_selector.capture_state(),
        "temperaments": dict(temperament_manager.temperaments),
        "assignments": dict(temperament_manager.temperament_dict),
        "plugins": tool_manager.plugin_info,
        "tools": tool_manager.custom_tools,
        "dependencies": tool_manager.tool_dependencies,
        "mcp_names": tool_manager.mcp_tool_names,
        "servers": mcp_manager.servers,
        "mapping": mcp_manager.tool_to_server,
    }
    candidate = await runtime_reloader._build_candidate(previous_snapshot.generation + 1)

    def fail_publish(snapshot):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(runtime_snapshots, "publish", fail_publish)
    with pytest.raises(RuntimeError, match="publish failed"):
        runtime_reloader._commit(candidate)
    assert runtime_snapshots.current() is previous_snapshot
    assert mutable_value(config_parser.config) == previous["config"]
    assert model_selector.capture_state() == previous["model"]
    assert temperament_manager.temperaments == previous["temperaments"]
    assert temperament_manager.temperament_dict == previous["assignments"]
    assert tool_manager.plugin_info is previous["plugins"]
    assert tool_manager.custom_tools is previous["tools"]
    assert tool_manager.tool_dependencies is previous["dependencies"]
    assert tool_manager.mcp_tool_names is previous["mcp_names"]
    assert mcp_manager.servers is previous["servers"]
    assert mcp_manager.tool_to_server is previous["mapping"]


@pytest.mark.asyncio
async def test_plugin_tool_collision_retains_previous_generation() -> None:
    await runtime_reloader.reload("test-collision-baseline")
    previous = runtime_snapshots.current()
    path = tool_manager.custom_tools_dir / "collision_test.py"
    try:
        path.write_text(
            "async def nonebot_plugin_localstore():\n"
            "    'collision'\n"
            "    return 'bad'\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="插件冲突"):
            await runtime_reloader.reload("test-plugin-collision")
        assert runtime_snapshots.current() is previous
    finally:
        path.unlink(missing_ok=True)
        await runtime_reloader.reload("test-collision-restore")


@pytest.mark.asyncio
async def test_mcp_tool_collision_retains_previous_generation(monkeypatch) -> None:
    from nonebot_plugin_moellmchats.mcp_manager import mcp_manager

    await runtime_reloader.reload("test-mcp-collision-baseline")
    previous = runtime_snapshots.current()

    async def collide(*args, **kwargs):
        return {
            "nonebot_plugin_localstore": {
                "name": "nonebot_plugin_localstore",
                "description": "collision",
                "parameters": {"type": "object", "properties": {}},
                "func": lambda: None,
            }
        }, {}

    monkeypatch.setattr(mcp_manager, "discover_tools", collide)
    with pytest.raises(ValueError, match="MCP 工具名"):
        await runtime_reloader.reload("test-mcp-collision")
    assert runtime_snapshots.current() is previous

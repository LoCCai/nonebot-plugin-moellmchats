from __future__ import annotations

from types import SimpleNamespace

import pytest

import nonebot_plugin_moellmchats as plugin
from nonebot_plugin_moellmchats import runtime_reload, tool_runtime
from nonebot_plugin_moellmchats.runtime_reload import ReloadResult


class MatcherFinished(RuntimeError):
    pass


class FakeMatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def finish(self, message: str) -> None:
        self.messages.append(str(message))
        raise MatcherFinished


class PlainText:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_plain_text(self) -> str:
        return self.text


@pytest.mark.asyncio
async def test_reload_tools_for_commands_returns_complete_result(monkeypatch) -> None:
    expected = ReloadResult(
        generation=12,
        changed=("tool-command",),
        custom_tools=3,
        mcp_tools=2,
    )
    reasons: list[str] = []

    async def reload(reason: str) -> ReloadResult:
        reasons.append(reason)
        return expected

    monkeypatch.setattr(runtime_reload.runtime_reloader, "reload", reload)

    assert await tool_runtime.reload_tools_for_commands() is expected
    assert reasons == ["tool-command"]


@pytest.mark.asyncio
async def test_reload_tools_for_commands_propagates_failure(monkeypatch) -> None:
    logged: list[str] = []

    async def reload(_reason: str) -> ReloadResult:
        raise RuntimeError("reload failed")

    monkeypatch.setattr(runtime_reload.runtime_reloader, "reload", reload)
    monkeypatch.setattr(tool_runtime.logger, "exception", logged.append)

    with pytest.raises(RuntimeError, match="reload failed"):
        await tool_runtime.reload_tools_for_commands()
    assert logged == ["工具原子重载失败"]


@pytest.mark.asyncio
async def test_refresh_tools_reports_published_result(monkeypatch) -> None:
    matcher = FakeMatcher()
    result = ReloadResult(
        generation=23,
        changed=("tool-command",),
        custom_tools=4,
        mcp_tools=5,
    )

    async def reload() -> ReloadResult:
        return result

    monkeypatch.setattr(plugin, "refresh_tools_matcher", matcher)
    monkeypatch.setattr(plugin, "reload_tools_for_commands", reload)
    monkeypatch.setattr(plugin.tool_manager, "plugin_info", {"one": {}, "two": {}})

    with pytest.raises(MatcherFinished):
        await plugin.refresh_tools_command()

    assert matcher.messages == [
        "✨ 工具重载完成！\n✅ 已发布 generation 23\n✅ 已加载 2 个原生插件\n✅ 已加载 4 个自定义函数\n✅ 已加载 5 个 MCP 工具"
    ]


@pytest.mark.asyncio
async def test_refresh_tools_failure_reports_retained_generation(monkeypatch) -> None:
    matcher = FakeMatcher()

    async def reload() -> ReloadResult:
        raise RuntimeError("api_key=must-not-leak")

    monkeypatch.setattr(plugin, "refresh_tools_matcher", matcher)
    monkeypatch.setattr(plugin, "reload_tools_for_commands", reload)
    monkeypatch.setattr(
        plugin.runtime_snapshots,
        "current",
        lambda: SimpleNamespace(generation=17),
    )

    with pytest.raises(MatcherFinished):
        await plugin.refresh_tools_command()

    message = matcher.messages[0]
    assert "工具重载失败" in message
    assert "旧 generation 17 已保留" in message
    assert "工具重载完成" not in message
    assert "must-not-leak" not in message


@pytest.mark.asyncio
async def test_blacklist_validation_reload_failure_stops_before_write(
    monkeypatch,
) -> None:
    matcher = FakeMatcher()
    writes: list[tuple[str, str]] = []

    async def reload() -> ReloadResult:
        raise RuntimeError("validation reload failed")

    def manage(action: str, plugin_name: str) -> str:
        writes.append((action, plugin_name))
        return "unexpected"

    monkeypatch.setattr(plugin, "manage_blacklist_matcher", matcher)
    monkeypatch.setattr(plugin, "reload_tools_for_commands", reload)
    monkeypatch.setattr(plugin.model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(plugin.model_selector, "manage_tool_blacklist", manage)
    monkeypatch.setattr(
        plugin.runtime_snapshots,
        "current",
        lambda: SimpleNamespace(generation=18),
    )
    event = SimpleNamespace(message=PlainText("添加插件黑名单 demo_tool"))

    with pytest.raises(MatcherFinished):
        await plugin.manage_tool_blacklist_command(
            object(),
            event,
            PlainText("demo_tool"),
        )

    assert writes == []
    assert "黑名单未修改" in matcher.messages[0]
    assert "旧 generation 18 已保留" in matcher.messages[0]


@pytest.mark.asyncio
async def test_blacklist_post_write_reload_failure_reports_runtime_stale(
    monkeypatch,
) -> None:
    matcher = FakeMatcher()
    reload_calls = 0

    async def reload() -> ReloadResult:
        nonlocal reload_calls
        reload_calls += 1
        if reload_calls == 2:
            raise RuntimeError("api_key=must-not-leak")
        return ReloadResult(
            generation=19,
            changed=("tool-command",),
            custom_tools=1,
            mcp_tools=0,
        )

    monkeypatch.setattr(plugin, "manage_blacklist_matcher", matcher)
    monkeypatch.setattr(plugin, "reload_tools_for_commands", reload)
    monkeypatch.setattr(plugin.model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(
        plugin.model_selector,
        "manage_tool_blacklist",
        lambda action, name: f"已将 {name} 加入工具黑名单",
    )
    monkeypatch.setattr(
        plugin.tool_manager,
        "validate_tool_identifier",
        lambda _name: (True, ""),
    )
    monkeypatch.setattr(
        plugin.runtime_snapshots,
        "current",
        lambda: SimpleNamespace(generation=19),
    )
    event = SimpleNamespace(message=PlainText("添加插件黑名单 demo_tool"))

    with pytest.raises(MatcherFinished):
        await plugin.manage_tool_blacklist_command(
            object(),
            event,
            PlainText("demo_tool"),
        )

    message = matcher.messages[0]
    assert reload_calls == 2
    assert "黑名单配置已写入" in message
    assert "工具运行快照同步失败" in message
    assert "旧 generation 19 已保留" in message
    assert "must-not-leak" not in message

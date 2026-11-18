from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
from nonebot import on_command
from nonebot.internal.matcher import matchers
from nonebot.rule import TrieRule
from pygtrie import CharTrie
import pytest

from nonebot_plugin_moellmchats import nonebot_plugin_tools as module
from nonebot_plugin_moellmchats.event_simulator import PluginDispatchResult, PluginDispatchStatus
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    PluginDispatchError,
    command_routing_hint,
    execute_nonebot_plugin,
)


@pytest.fixture
def manga_owner(monkeypatch):
    monkeypatch.setattr(TrieRule, "prefix", CharTrie())
    matcher = on_command("漫画", aliases={"manga"})
    plugin = SimpleNamespace(name="nonebot_plugin_suwayomi_server", matcher={matcher})
    monkeypatch.setattr(nonebot, "get_loaded_plugins", lambda: [plugin])
    yield plugin
    for group in matchers.values():
        if matcher in group:
            group.remove(matcher)


def test_wrong_tool_names_actual_loaded_owner_without_executing(manga_owner):
    error = PluginDispatchError(
        PluginDispatchResult(status=PluginDispatchStatus.NOT_MATCHED),
        command="/漫画 搜索 樱花庄的宠物女孩",
        plugin_name="qi_zmws",
    )
    assert "nonebot_plugin_suwayomi_server" in str(error)
    assert "本次却调用了 qi_zmws" in str(error)
    assert "不能据此宣称" in str(error)
    assert "樱花庄" not in str(error)  # routing feedback needs no user arguments


def test_matching_owner_does_not_propose_another_plugin(manga_owner):
    assert command_routing_hint("/漫画 章节 108", manga_owner.name) == ""


def test_unknown_command_never_invents_an_owner(manga_owner):
    assert command_routing_hint("/不存在的漫画命令", "qi_zmws") == ""


def test_alias_uses_registered_owner(manga_owner):
    assert "nonebot_plugin_suwayomi_server" in command_routing_hint("/manga 源", "qi_zmws")


@pytest.mark.asyncio
async def test_toolspec_dispatch_propagates_routing_feedback_without_second_execution(monkeypatch, manga_owner):
    dispatch = AsyncMock(return_value=PluginDispatchResult(status=PluginDispatchStatus.NOT_MATCHED))
    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    with pytest.raises(PluginDispatchError, match="nonebot_plugin_suwayomi_server"):
        await execute_nonebot_plugin("/漫画 搜索 樱花庄", plugin_name="qi_zmws", bot=object(), event=object())
    dispatch.assert_awaited_once()

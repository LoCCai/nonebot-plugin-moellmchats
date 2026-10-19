from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.exception import FinishedException, StopPropagation
from nonebot.message import check_and_run_matcher
import pytest

import nonebot_plugin_moellmchats as plugin
from nonebot_plugin_moellmchats import chat_runtime, group_switch
from nonebot_plugin_moellmchats import config as config_module
from nonebot_plugin_moellmchats.config import DEFAULT_CONFIG, ConfigParser


@pytest.fixture
def parser(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "config_path", tmp_path)
    parser = ConfigParser()
    monkeypatch.setattr(group_switch, "config_parser", parser)
    return parser


def event(text="hello", *, user_id=1, group_id=456):
    message = Message(text)
    return GroupMessageEvent(
        time=1, self_id=10000, post_type="message", sub_type="normal",
        user_id=user_id, message_type="group", group_id=group_id,
        message_id=789, message=message, original_message=message,
        raw_message=text, font=0, to_me=True,
        sender=Sender(user_id=user_id, nickname="tester", card="tester", role="admin"),
    )


class Adapter:
    @property
    def config(self):
        from nonebot import get_driver
        return get_driver().config

    @staticmethod
    def get_name():
        return "OneBot V11"


class RecordingBot(Bot):
    def __init__(self):
        super().__init__(Adapter(), "10000")
        self.calls = []

    async def call_api(self, api, **data):
        self.calls.append((api, data))
        return {"ok": True}


def test_global_and_per_group_switch_persist_across_reload(parser):
    assert group_switch.group_llm_enabled(event())
    group_switch.manage_group_llm("关闭LLM 全部", event())
    assert not group_switch.group_llm_enabled(event())
    assert not group_switch.group_llm_enabled(event(group_id=567))
    group_switch.manage_group_llm("/开启群聊llm", event())
    assert group_switch.group_llm_enabled(event())
    assert not group_switch.group_llm_enabled(event(group_id=567))
    restored = ConfigParser()
    assert restored.config["group_chat_policy"] == {
        "default_enabled": False, "groups": {"456": True},
    }
    group_switch.manage_group_llm("关闭LLM 全部", event())
    assert parser.config["group_chat_policy"]["groups"] == {}
    assert "默认关闭" in group_switch.manage_group_llm("查看LLM 全部", event())


def test_private_can_target_group_but_must_specify_it(parser):
    private = SimpleNamespace(user_id=1)
    group_switch.manage_group_llm("关闭LLM 456", private)
    assert not group_switch.group_llm_enabled(event())
    assert group_switch.group_llm_enabled(private)
    assert "已关闭" in group_switch.manage_group_llm("查看LLM 456", private)
    with pytest.raises(ValueError, match="私聊需指定"):
        group_switch.manage_group_llm("关闭LLM", private)


@pytest.mark.parametrize("target", ["0", "-1", "１２３", "456 567", "123abc", "9" * 21])
def test_reject_invalid_targets_without_saving(parser, target):
    before = parser.filepath.read_bytes()
    with pytest.raises(ValueError, match="格式"):
        group_switch.manage_group_llm("开启LLM " + target, event())
    assert parser.filepath.read_bytes() == before


@pytest.mark.parametrize("policy", [
    None, True, {}, {"default_enabled": "false", "groups": {}},
    {"default_enabled": True, "groups": {"456": "false"}},
    {"default_enabled": True, "groups": {456: True}},
    {"default_enabled": True, "groups": {"-1": True}},
])
def test_invalid_policy_rejected(policy):
    config = deepcopy(DEFAULT_CONFIG)
    config["group_chat_policy"] = policy
    with pytest.raises(ValueError, match="group_chat_policy"):
        ConfigParser._validate(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/关闭LLM", "!开启群LLM 567", "查看llm 全部"])
async def test_superuser_command_runs_without_model(parser, text):
    bot = RecordingBot()
    with pytest.raises(StopPropagation):
        await check_and_run_matcher(plugin.group_llm_switch_matcher, bot, event(text), {})
    assert len(bot.calls) == 1
    assert bot.calls[0][0] == "send_msg"


@pytest.mark.asyncio
async def test_group_admin_cannot_toggle(parser):
    bot = RecordingBot()
    before = parser.filepath.read_bytes()
    await check_and_run_matcher(
        plugin.group_llm_switch_matcher, bot, event("/关闭LLM 全部", user_id=2), {},
    )
    assert bot.calls == []
    assert parser.filepath.read_bytes() == before


@pytest.mark.asyncio
async def test_disabled_group_rules_and_direct_call_stop_before_work(parser, monkeypatch):
    group_switch.manage_group_llm("关闭LLM", event())
    bot = RecordingBot()
    assert not await chat_runtime.chat_rule(bot, event())
    assert not await plugin.llm_matcher.rule(bot, event(), {})
    from nonebot.rule import TrieRule
    ai_event, state = event("/ai hello"), {}
    TrieRule.get_value(bot, ai_event, state)
    assert not await plugin.ai_matcher.rule(bot, ai_event, state)

    def unexpected(*args, **kwargs):
        pytest.fail("disabled group acquired a slot or formatted attachments")

    monkeypatch.setattr(chat_runtime.conversation_slots, "slot", unexpected)
    matcher = SimpleNamespace(finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_runtime.handle_llm(bot, event(), matcher, unexpected)
    assert "已关闭" in matcher.finish.call_args.args[0]


@pytest.mark.asyncio
async def test_disabled_group_skips_context_and_poke_model(parser, monkeypatch):
    group_switch.manage_group_llm("关闭LLM", event())
    monkeypatch.setattr(plugin, "format_context_message", lambda *a: pytest.fail("recorded context"))
    await plugin.context_dict_func(RecordingBot(), event())
    monkeypatch.setattr(plugin.config_parser, "get_config", lambda key: 1 if key == "poke_llm_rate" else 0)
    monkeypatch.setattr(plugin, "handle_llm", AsyncMock())
    monkeypatch.setattr(plugin.poke_, "send", AsyncMock())
    monkeypatch.setattr(plugin, "get_reply_messages", lambda *a: ["poke"])
    await plugin._poke_event(RecordingBot(), SimpleNamespace(is_tome=True, group_id=456))
    plugin.handle_llm.assert_not_awaited()
    plugin.poke_.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_group_denies_recovery_and_pending_execution(parser):
    from nonebot_plugin_moellmchats.recovery_lifecycle import authorize_recovery, resume_request

    group_switch.manage_group_llm("关闭LLM", event())
    bot = RecordingBot()
    assert await authorize_recovery(bot, {"protocol": "onebot_v11", "event": {"group_id": 456}}) is None
    assert await resume_request(bot, event(), {}) is None
    assert bot.calls == []
    with pytest.raises(plugin.PendingActionError, match="已关闭"):
        await plugin._run_confirmation_code("确认执行", "AABBCC", bot, event())

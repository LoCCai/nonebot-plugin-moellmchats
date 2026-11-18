from __future__ import annotations

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.exception import StopPropagation
from nonebot.message import check_and_run_matcher
import pytest

from nonebot_plugin_moellmchats import config, policy_admin
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots


class Adapter:
    @property
    def config(self):
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


def event(text, user_id=1):
    return GroupMessageEvent(
        time=1,
        self_id=10000,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=456,
        message_id=789,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        to_me=False,
        sender=Sender(user_id=user_id, nickname="tester", role="admin"),
    )


@pytest.fixture
def parser(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "config_path", tmp_path)
    parser = config.ConfigParser()
    monkeypatch.setattr(policy_admin, "config_parser", parser)
    monkeypatch.setattr(runtime_snapshots, "_current", None)
    return parser


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        ("/关闭LLM语音", "speech_enabled", False),
        ("开启LLM语音", "speech_enabled", True),
        ("!关闭llm随机语音", "speech_auto_enabled", False),
        ("开启LLM随机语音", "speech_auto_enabled", True),
    ],
)
async def test_superuser_switch_persists_without_ai(parser, text, key, expected):
    bot = RecordingBot()
    with pytest.raises(StopPropagation):
        await check_and_run_matcher(policy_admin.speech_control, bot, event(text), {})
    assert config.ConfigParser().config[key] is expected
    assert len(bot.calls) == 1
    assert "全部群聊与私聊" in str(bot.calls[0][1]["message"])


@pytest.mark.asyncio
async def test_group_admin_cannot_change_global_speech(parser):
    bot = RecordingBot()
    before = parser.filepath.read_bytes()
    await check_and_run_matcher(policy_admin.speech_control, bot, event("/关闭LLM语音", user_id=2), {})
    assert bot.calls == []
    assert parser.filepath.read_bytes() == before


@pytest.mark.asyncio
async def test_status_does_not_change_switches(parser):
    parser.set_config("speech_enabled", False)
    before = parser.filepath.read_bytes()
    bot = RecordingBot()
    with pytest.raises(StopPropagation):
        await check_and_run_matcher(policy_admin.speech_control, bot, event("/查看LLM语音"), {})
    assert parser.filepath.read_bytes() == before
    assert "语音总开关：关闭" in str(bot.calls[0][1]["message"])


@pytest.mark.asyncio
async def test_failed_save_preserves_setting_and_reports_failure(parser, monkeypatch):
    def fail_write(candidate):
        raise OSError("unavailable")

    monkeypatch.setattr(parser, "_write", fail_write)
    bot = RecordingBot()
    with pytest.raises(StopPropagation):
        await check_and_run_matcher(policy_admin.speech_control, bot, event("/关闭LLM语音"), {})
    assert parser.config["speech_enabled"] is True
    assert "保存失败" in str(bot.calls[0][1]["message"])

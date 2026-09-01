from __future__ import annotations

from nonebot.adapters.onebot.v11 import Bot as V11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.exception import StopPropagation
from nonebot.message import check_and_run_matcher
import pytest

from nonebot_plugin_moellmchats import (
    _parse_llm_cooldown_command,
    _parse_llm_cooldown_seconds,
    _parse_tool_progress_command,
    set_llm_cooldown_matcher,
    set_tool_progress_matcher,
)
from nonebot_plugin_moellmchats.config import MAX_CD_SECONDS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", 0),
        ("00000", 0),
        (" 30 ", 30),
        ("00030", 30),
        (str(MAX_CD_SECONDS), MAX_CD_SECONDS),
    ],
)
def test_parse_llm_cooldown_seconds(raw: str, expected: int) -> None:
    assert _parse_llm_cooldown_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "-1",
        "+1",
        "1.5",
        "０",
        "1 秒",
        str(MAX_CD_SECONDS + 1),
        "9" * 5000,
    ],
)
def test_parse_llm_cooldown_seconds_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError, match="0 到"):
        _parse_llm_cooldown_seconds(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/设置LLM冷却 0", 0),
        ("!设置llm冷却 30", 30),
        ("！设置LLMCD 00060", 60),
        ("设置对话冷却 120", 120),
        ("设置LLM冷却 0 1", None),
        ("请设置LLM冷却 0", None),
    ],
)
def test_parse_llm_cooldown_command(raw: str, expected: int | None) -> None:
    if raw == "设置LLM冷却 0 1":
        with pytest.raises(ValueError, match="0 到"):
            _parse_llm_cooldown_command(raw)
        return
    assert _parse_llm_cooldown_command(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/设置工具进度 开", True),
        ("!设置调用进度 1", True),
        ("！设置工具提示 关", False),
        ("设置工具进度 0", False),
        ("请设置工具进度 关", None),
    ],
)
def test_parse_tool_progress_command(raw: str, expected: bool | None) -> None:
    assert _parse_tool_progress_command(raw) is expected


@pytest.mark.parametrize("raw", ["设置工具进度", "设置工具进度 打开", "设置工具进度 0 1"])
def test_parse_tool_progress_command_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError, match="开、关、1 或 0"):
        _parse_tool_progress_command(raw)


def _group_event(*, user_id: int, text: str) -> GroupMessageEvent:
    message = Message(text)
    return GroupMessageEvent(
        time=1,
        self_id=10000,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=456,
        message_id=789,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender=Sender(user_id=user_id, nickname="tester", card="tester"),
    )


class _Adapter:
    @property
    def config(self):
        from nonebot import get_driver

        return get_driver().config

    @staticmethod
    def get_name() -> str:
        return "OneBot V11"


class _RecordingBot(V11Bot):
    def __init__(self) -> None:
        super().__init__(_Adapter(), "10000")
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **data):
        self.calls.append((api, data))
        return {"ok": True}


@pytest.mark.asyncio
async def test_fixed_cooldown_matcher_routes_superuser_without_llm(monkeypatch) -> None:
    values = {"cd_seconds": 120}
    mutations: list[tuple[str, int]] = []

    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.config_parser.get_config",
        lambda key, default=None: values.get(key, default),
    )

    def set_config(key: str, value: int) -> None:
        mutations.append((key, value))
        values[key] = value

    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.config_parser.set_config",
        set_config,
    )
    bot = _RecordingBot()

    with pytest.raises(StopPropagation):
        await check_and_run_matcher(
            set_llm_cooldown_matcher,
            bot,
            _group_event(user_id=1, text="/设置LLM冷却 0"),
            {},
        )

    assert mutations == [("cd_seconds", 0)]
    assert len(bot.calls) == 1
    assert bot.calls[0][0] == "send_msg"
    assert "已关闭 LLM 对话冷却" in str(bot.calls[0][1]["message"])


@pytest.mark.asyncio
async def test_fixed_cooldown_matcher_rejects_non_superuser(monkeypatch) -> None:
    mutations: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.config_parser.set_config",
        lambda key, value: mutations.append((key, value)),
    )
    bot = _RecordingBot()

    await check_and_run_matcher(
        set_llm_cooldown_matcher,
        bot,
        _group_event(user_id=2, text="/设置LLM冷却 0"),
        {},
    )

    assert mutations == []
    assert bot.calls == []


@pytest.mark.asyncio
async def test_fixed_tool_progress_matcher_updates_config_without_llm(monkeypatch) -> None:
    values = {"tool_progress_messages_enabled": True}
    mutations: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.config_parser.get_config",
        lambda key, default=None: values.get(key, default),
    )

    def set_config(key: str, value: bool) -> None:
        mutations.append((key, value))
        values[key] = value

    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.config_parser.set_config",
        set_config,
    )
    bot = _RecordingBot()

    with pytest.raises(StopPropagation):
        await check_and_run_matcher(
            set_tool_progress_matcher,
            bot,
            _group_event(user_id=1, text="/设置工具进度 关"),
            {},
        )

    assert mutations == [("tool_progress_messages_enabled", False)]
    assert len(bot.calls) == 1
    assert bot.calls[0][0] == "send_msg"
    assert "已关闭工具调用进度提示" in str(bot.calls[0][1]["message"])
    assert "最终回复和后台日志不受影响" in str(bot.calls[0][1]["message"])


@pytest.mark.asyncio
async def test_fixed_tool_progress_matcher_rejects_non_superuser(monkeypatch) -> None:
    mutations: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.config_parser.set_config",
        lambda key, value: mutations.append((key, value)),
    )
    bot = _RecordingBot()

    await check_and_run_matcher(
        set_tool_progress_matcher,
        bot,
        _group_event(user_id=2, text="/设置工具进度 关"),
        {},
    )

    assert mutations == []
    assert bot.calls == []

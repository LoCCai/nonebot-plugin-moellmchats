from __future__ import annotations

from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message
import pytest

from nonebot_plugin_moellmchats.utils import format_context_message, format_message


class ContextEvent:
    self_id = "42"

    def get_message(self):
        return Message("hello[CQ:at,qq=100][CQ:image,file=x]")


def test_context_extraction_is_pure_and_preserves_mentions() -> None:
    assert format_context_message(ContextEvent()) == {
        "text": ["hello", "@100", "[图片]"]
    }


def test_one_hundred_context_messages_need_no_bot_api() -> None:
    event = ContextEvent()
    for _ in range(100):
        assert format_context_message(event)["text"] == ["hello", "@100", "[图片]"]


class _WakeEvent:
    self_id = "42"
    user_id = 42
    time = 1_000
    sender = SimpleNamespace(user_id=42, card="测试用户", nickname="测试用户")

    def __init__(self, text: str) -> None:
        self._text = text

    def get_message(self):
        return Message(self._text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("airpods 怎么样", ["airpods 怎么样"]),
        ("ai助手", ["ai助手"]),
        ("ai 你好", ["你好"]),
        ("ai\u3000你好", ["你好"]),
        ("ai\t你好", ["你好"]),
        ("ai", [""]),
        ("AI 你好", ["你好"]),
    ],
)
async def test_format_message_strips_only_standalone_wake_word(
    raw: str,
    expected: list[str],
) -> None:
    result = await format_message(_WakeEvent(raw), None)
    assert result["text"] == expected

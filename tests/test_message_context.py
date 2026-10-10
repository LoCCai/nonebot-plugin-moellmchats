from __future__ import annotations

from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment
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


@pytest.mark.asyncio
async def test_user_mentions_keep_identity_tokens_in_original_position(monkeypatch) -> None:
    from nonebot_plugin_moellmchats import utils

    event = _WakeEvent("")
    message = Message([
        MessageSegment.at(42), MessageSegment.text(" 战力对比"),
        MessageSegment.at(1969334055), MessageSegment.text(" 1470907075|4399|1 "),
        MessageSegment.at(234),
    ])
    event.get_message = lambda: message
    monkeypatch.setattr(utils, "get_member_name", None)
    result = await format_message(event, SimpleNamespace(self_id="42"))
    # Private messages use QQ as their label and skip member lookup. The bot
    # wake-up mention must not consume one of the user's argument indices.
    assert result["mentions"] == [
        {"qq": "1969334055", "name": "1969334055"}, {"qq": "234", "name": "234"},
    ]
    assert result["text"] == [
        " 战力对比", "1969334055[at:1]", " 1470907075|4399|1 ", "234[at:2]",
    ]


def test_nul_in_quote_does_not_duplicate_previous_assistant_message() -> None:
    from nonebot_plugin_moellmchats.messages_handler import MessagesHandler, messages_dict

    messages_dict.clear()
    try:
        previous = MessagesHandler("10001")
        previous.pre_process({"text": ["你好"]})
        previous.post_process("上次回复🙂")
        current = MessagesHandler("10001")
        assert (
            current.pre_process(
                {
                    "text": ["接着发"],
                    "reply": "上次\x00回复🙂",
                    "reply_user": {"name": "七七"},
                    "current_user": {"name": "白洋"},
                }
            )
            == "接着发"
        )
    finally:
        messages_dict.clear()

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        " 战力对比", "[CQ:at,qq=1969334055]", " 1470907075|4399|1 ", "[CQ:at,qq=234]",
    ]


@pytest.mark.asyncio
async def test_current_and_quoted_image_sources_are_available_to_tools():
    event = _WakeEvent("用图做表情")
    event.reply = SimpleNamespace(
        message=Message([MessageSegment.image("quoted-file")]),
        sender=SimpleNamespace(user_id=12002, nickname="被引用者"),
    )
    event.get_message = lambda: Message([MessageSegment.text("用图做表情"),
                                        MessageSegment.image("https://example.com/current.png")])
    bot = SimpleNamespace(self_id="42", call_api=AsyncMock(return_value={"url": "https://example.com/reply.png"}))
    result = await format_message(event, bot)
    assert result["image_sources"] == [
        {"source": "reply", "url": "https://example.com/reply.png"},
        {"source": "current", "url": "https://example.com/current.png"},
    ]
    assert result["images"] == [item["url"] for item in result["image_sources"]]
    assert result["unresolved_images"] == 0
    assert result["current_user"]["qq"] == "42"
    assert result["reply_user"]["qq"] == "12002"
    bot.call_api.assert_awaited_once_with("get_image", file="quoted-file")


@pytest.mark.asyncio
async def test_reply_segment_fetches_real_quoted_message_then_resolves_image():
    event = _WakeEvent("")
    event.get_message = lambda: Message([MessageSegment.reply(789), MessageSegment.text("做表情")])
    bot = SimpleNamespace(self_id="42", call_api=AsyncMock(side_effect=[
        {"sender": {"user_id": 12002, "nickname": "被引用者"},
         "message": [{"type": "image", "data": {"file": "quoted-hash"}}]},
        {"url": "https://example.com/reply.png"},
    ]))
    result = await format_message(event, bot)
    assert result["reply"] == "[图片]"
    assert result["reply_user"]["qq"] == "12002"
    assert result["image_sources"] == [{"source": "reply", "url": "https://example.com/reply.png"}]
    assert bot.call_api.await_args_list[0].kwargs == {"message_id": 789}
    assert bot.call_api.await_args_list[1].kwargs == {"file": "quoted-hash"}


@pytest.mark.asyncio
async def test_failed_image_lookup_preserves_request_and_does_not_expose_local_path():
    event = _WakeEvent("")
    event.get_message = lambda: Message([MessageSegment.text("继续回答"), MessageSegment.image("hash")])
    for response in ({"file": "/private/cache/image.png"}, TimeoutError()):
        bot = SimpleNamespace(self_id="42", call_api=AsyncMock())
        if isinstance(response, Exception):
            bot.call_api.side_effect = response
        else:
            bot.call_api.return_value = response
        result = await format_message(event, bot)
        assert result["text"] == ["继续回答", "[图片]"]
        assert result["images"] == []
        assert result["unresolved_images"] == 1
        assert "/private" not in str(result)


@pytest.mark.asyncio
async def test_qq_signed_image_is_refreshed_even_when_message_already_has_url():
    event = _WakeEvent("")
    segment = MessageSegment("image", {"file": "original-hash", "url": "https://multimedia.nt.qq.com.cn/download?rkey=expired"})
    event.get_message = lambda: Message([segment])
    bot = SimpleNamespace(self_id="42", call_api=AsyncMock(return_value={"url": "https://example.com/fresh.png"}))
    result = await format_message(event, bot)
    assert result["images"] == ["https://example.com/fresh.png"]
    bot.call_api.assert_awaited_once_with("get_image", file="original-hash")


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

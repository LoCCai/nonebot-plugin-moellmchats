from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message as V11Message
from nonebot.adapters.onebot.v11 import MessageSegment as V11MessageSegment
from nonebot.adapters.onebot.v12 import (
    BotSelf,
    GroupMessageEvent,
    Reply,
)
from nonebot.adapters.onebot.v12 import (
    Message as V12Message,
)
from nonebot.adapters.onebot.v12 import (
    MessageSegment as V12MessageSegment,
)
import pytest

from nonebot_plugin_moellmchats.onebot_facade import (
    NormalizedOneBotEvent,
    bot_self_id,
    event_images,
    event_mentions,
    event_reply_message_id,
    event_time_seconds,
    make_image_segment,
    make_mention_segment,
    make_reply_segment,
    make_text_message,
)
from nonebot_plugin_moellmchats.utils import format_message


class _Adapter:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


class _Bot:
    def __init__(self, protocol: str, self_id: str | None = None) -> None:
        self.adapter = _Adapter(protocol)
        if self_id is not None:
            self.self_id = self_id

    async def get_group_member_info(self, **_kwargs):
        return {"user_displayname": "被提及者"}


def _v12_group_event() -> GroupMessageEvent:
    message = V12Message(
        [
            V12MessageSegment.reply("message-before"),
            V12MessageSegment.text("你好"),
            V12MessageSegment.mention("actor-2"),
            V12MessageSegment.image("file-image-1"),
        ]
    )
    return GroupMessageEvent(
        id="event-1",
        time=datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc),
        type="message",
        detail_type="group",
        sub_type="normal",
        self=BotSelf(platform="qq", user_id="bot-v12"),
        message_id="message-current",
        message=message,
        original_message=message,
        alt_message="你好 @actor-2 [图片]",
        user_id="actor-1",
        group_id="group-1",
        reply=Reply(message_id="message-quoted", user_id="actor-3"),
    )


def test_v11_numeric_event_fields_are_normalized_without_losing_segments() -> None:
    event = SimpleNamespace(
        time=1_725_000_000,
        self_id=10000,
        user_id=123,
        group_id=456,
        message_id=789,
        sender=SimpleNamespace(user_id=123, card="群名片", nickname="昵称"),
        message=V11Message(
            [
                V11MessageSegment.reply(321),
                V11MessageSegment.text("正文"),
                V11MessageSegment.at(234),
                V11MessageSegment.image("https://example.invalid/a.jpg"),
            ]
        ),
    )
    normalized = NormalizedOneBotEvent.capture(
        _Bot("OneBot V11", "10000"),
        event,
    )

    assert normalized.protocol == "onebot_v11"
    assert normalized.bot_id == "10000"
    assert normalized.user_id == "123"
    assert normalized.group_id == "456"
    assert normalized.message_id == "789"
    assert normalized.reply_message_id == "321"
    assert normalized.timestamp == 1_725_000_000
    assert normalized.sender_name == "群名片"
    assert normalized.mentions == ("234",)
    assert normalized.images


def test_v12_datetime_nested_self_string_ids_and_file_id_are_normalized() -> None:
    event = _v12_group_event()
    bot = _Bot("OneBot V12")
    normalized = NormalizedOneBotEvent.capture(bot, event)

    assert normalized.protocol == "onebot_v12"
    assert normalized.bot_id == "bot-v12"
    assert bot_self_id(bot, event) == "bot-v12"
    assert normalized.user_id == "actor-1"
    assert normalized.group_id == "group-1"
    assert normalized.message_id == "message-current"
    assert normalized.reply_message_id == "message-quoted"
    assert (
        normalized.timestamp
        == datetime(
            2026,
            8,
            28,
            4,
            30,
            tzinfo=timezone.utc,
        ).timestamp()
    )
    assert event_time_seconds(event) == normalized.timestamp
    assert event_reply_message_id(event) == "message-quoted"
    assert event_mentions(event) == ("actor-2",)
    assert event_images(event) == ("file-image-1",)


@pytest.mark.asyncio
async def test_v12_format_message_keeps_mention_and_image_file_id() -> None:
    formatted = await format_message(
        _v12_group_event(),
        _Bot("OneBot V12", "bot-v12"),
    )

    assert formatted["current_user"] == {
        "qq": "actor-1",
        "name": "actor-1",
    }
    assert formatted["mentions"] == [{"qq": "actor-2", "name": "被提及者"}]
    assert formatted["image_file_ids"] == ["file-image-1"]
    assert formatted["images"] == []
    assert formatted["reply"] == "[回复消息 message-quoted]"


def test_protocol_specific_message_builders_never_treat_local_v11_image_as_v12_file() -> None:
    assert str(make_text_message("onebot_v11", "正文")) == "正文"
    assert str(make_text_message("onebot_v12", "正文")) == "正文"
    assert make_image_segment("onebot_v12", "/tmp/local-emotion.png") is None
    assert make_mention_segment("onebot_v11", "123").type == "at"
    assert make_mention_segment("onebot_v12", "actor-1").type == "mention"
    assert make_reply_segment("onebot_v11", "123").type == "reply"
    with pytest.raises(ValueError, match="message_id must be an integer"):
        make_reply_segment("onebot_v11", "message-1")
    assert make_reply_segment("onebot_v12", "message-1").type == "reply"

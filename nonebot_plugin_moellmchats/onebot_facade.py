from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def adapter_identity(bot: Any) -> str:
    adapter = getattr(bot, "adapter", None)
    get_name = getattr(adapter, "get_name", None)
    if callable(get_name):
        try:
            return str(get_name())
        except Exception:
            pass
    return f"{type(bot).__module__}.{type(bot).__qualname__}"


def onebot_protocol(bot: Any, event: Any | None = None) -> str | None:
    identity = adapter_identity(bot).lower()
    module = f"{type(bot).__module__} {type(event).__module__ if event is not None else ''}".lower()
    if "onebot v12" in identity or ".onebot.v12" in module:
        return "onebot_v12"
    if "onebot v11" in identity or ".onebot.v11" in module:
        return "onebot_v11"
    return None


def bot_self_id(bot: Any, event: Any | None = None) -> str:
    value = getattr(bot, "self_id", None)
    if value is None and event is not None:
        value = getattr(event, "self_id", None)
    if value is None and event is not None:
        nested_self = getattr(event, "self", None)
        value = getattr(nested_self, "user_id", None)
        if value is None and isinstance(nested_self, Mapping):
            value = nested_self.get("user_id")
    return "" if value is None else str(value)


def event_user_id(event: Any) -> str:
    value = getattr(event, "user_id", None)
    if value is None:
        sender = getattr(event, "sender", None)
        value = getattr(sender, "user_id", None)
        if value is None and isinstance(sender, Mapping):
            value = sender.get("user_id")
    return "" if value is None else str(value)


def event_group_id(event: Any) -> str | None:
    value = getattr(event, "group_id", None)
    return None if value is None else str(value)


def event_scene(event: Any) -> str:
    detail_type = str(getattr(event, "detail_type", "") or "").lower()
    if event_group_id(event) is not None or detail_type == "group":
        return "group"
    if getattr(event, "guild_id", None) is not None or detail_type == "channel":
        return "channel"
    return "private"


def event_time_seconds(event: Any) -> float:
    value = getattr(event, "time", 0)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def event_message(event: Any) -> Any:
    value = getattr(event, "message", None)
    if value is not None:
        return value
    getter = getattr(event, "get_message", None)
    if callable(getter):
        return getter()
    return ()


def message_segments(event: Any) -> tuple[Any, ...]:
    value = event_message(event)
    try:
        return tuple(value)
    except TypeError:
        return ()


def event_plain_text(event: Any) -> str:
    message = event_message(event)
    extract = getattr(message, "extract_plain_text", None)
    if callable(extract):
        return str(extract())
    return "".join(
        str(getattr(segment, "data", {}).get("text", ""))
        for segment in message_segments(event)
        if str(getattr(segment, "type", "")) == "text"
    )


def event_message_id(event: Any) -> str | None:
    value = getattr(event, "message_id", None)
    if value is None:
        value = getattr(event, "id", None)
    return None if value is None else str(value)


def event_reply_message_id(event: Any) -> str | None:
    reply = getattr(event, "reply", None)
    if reply is not None:
        value = getattr(reply, "message_id", None)
        if value is None:
            value = getattr(reply, "id", None)
        if value is not None:
            return str(value)
    for segment in message_segments(event):
        if str(getattr(segment, "type", "")) != "reply":
            continue
        data = getattr(segment, "data", {})
        if not isinstance(data, Mapping):
            continue
        value = data.get("message_id", data.get("id"))
        if value is not None:
            return str(value)
    return None


def event_sender_name(event: Any) -> str:
    sender = getattr(event, "sender", None)
    for name in ("card", "nickname", "user_displayname", "user_name"):
        value = getattr(sender, name, None)
        if value is None and isinstance(sender, Mapping):
            value = sender.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return event_user_id(event) or "未知用户"


def event_mentions(event: Any) -> tuple[str, ...]:
    values: list[str] = []
    for segment in message_segments(event):
        segment_type = str(getattr(segment, "type", ""))
        if segment_type not in {"at", "mention"}:
            continue
        data = getattr(segment, "data", {})
        if not isinstance(data, Mapping):
            continue
        value = data.get("qq") if segment_type == "at" else data.get("user_id")
        if value not in {None, "all"}:
            values.append(str(value))
    return tuple(values)


def event_images(event: Any) -> tuple[str, ...]:
    values: list[str] = []
    for segment in message_segments(event):
        if str(getattr(segment, "type", "")) != "image":
            continue
        data = getattr(segment, "data", {})
        if not isinstance(data, Mapping):
            continue
        value = data.get("url") or data.get("file_id") or data.get("file")
        if value is not None:
            values.append(str(value))
    return tuple(values)


@dataclass(frozen=True)
class NormalizedOneBotEvent:
    protocol: str
    bot_id: str
    user_id: str
    scene: str
    group_id: str | None
    guild_id: str | None
    channel_id: str | None
    message_id: str | None
    reply_message_id: str | None
    timestamp: float
    plain_text: str
    sender_name: str
    mentions: tuple[str, ...]
    images: tuple[str, ...]

    @classmethod
    def capture(cls, bot: Any, event: Any) -> NormalizedOneBotEvent:
        protocol = onebot_protocol(bot, event)
        if protocol is None:
            raise ValueError("当前 Bot/Event 不是受支持的 OneBot v11/v12")
        guild_id = getattr(event, "guild_id", None)
        channel_id = getattr(event, "channel_id", None)
        return cls(
            protocol=protocol,
            bot_id=bot_self_id(bot, event),
            user_id=event_user_id(event),
            scene=event_scene(event),
            group_id=event_group_id(event),
            guild_id=None if guild_id is None else str(guild_id),
            channel_id=None if channel_id is None else str(channel_id),
            message_id=event_message_id(event),
            reply_message_id=event_reply_message_id(event),
            timestamp=event_time_seconds(event),
            plain_text=event_plain_text(event),
            sender_name=event_sender_name(event),
            mentions=event_mentions(event),
            images=event_images(event),
        )


async def get_member_display_name(bot: Any, event: Any, user_id: str) -> str:
    group_id = event_group_id(event)
    if group_id is None:
        return str(user_id)
    protocol = onebot_protocol(bot, event)
    try:
        data = await bot.call_api(
            "get_group_member_info",
            group_id=int(group_id) if protocol == "onebot_v11" and group_id.isdigit() else group_id,
            user_id=int(user_id) if protocol == "onebot_v11" and str(user_id).isdigit() else str(user_id),
            **({"no_cache": False} if protocol == "onebot_v11" else {}),
        )
    except Exception:
        return str(user_id)
    if not isinstance(data, Mapping):
        return str(user_id)
    for field in ("card", "nickname", "user_displayname", "user_name"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(user_id)


def make_text_message(protocol: str, text: str) -> Any:
    if protocol == "onebot_v12":
        from nonebot.adapters.onebot.v12 import Message

        return Message(text)
    from nonebot.adapters.onebot.v11 import Message

    return Message(text)


def make_image_segment(protocol: str, reference: str) -> Any | None:
    if protocol == "onebot_v12":
        # v12 only accepts an implementation-owned file_id.  A local path or
        # URL from the optional v11 emotion directory is not a valid file_id.
        return None
    from nonebot.adapters.onebot.v11 import MessageSegment

    return MessageSegment.image(reference)


def make_mention_segment(protocol: str, user_id: str) -> Any:
    if protocol == "onebot_v12":
        from nonebot.adapters.onebot.v12 import MessageSegment

        return MessageSegment.mention(str(user_id))
    from nonebot.adapters.onebot.v11 import MessageSegment

    value: int | str = int(user_id) if str(user_id).isdigit() else str(user_id)
    return MessageSegment.at(value)


def make_reply_segment(protocol: str, message_id: str) -> Any:
    if protocol == "onebot_v12":
        from nonebot.adapters.onebot.v12 import MessageSegment

        return MessageSegment.reply(str(message_id))
    from nonebot.adapters.onebot.v11 import MessageSegment

    if not str(message_id).lstrip("-").isdigit():
        raise ValueError("OneBot v11 reply message_id must be an integer")
    return MessageSegment.reply(int(message_id))


def coerce_action_identifier(value: str, protocol: str) -> int | str:
    if protocol == "onebot_v11" and value.lstrip("-").isdigit():
        return int(value)
    return value


__all__ = [
    "NormalizedOneBotEvent",
    "adapter_identity",
    "bot_self_id",
    "coerce_action_identifier",
    "event_group_id",
    "event_images",
    "event_mentions",
    "event_message_id",
    "event_plain_text",
    "event_reply_message_id",
    "event_scene",
    "event_sender_name",
    "event_time_seconds",
    "event_user_id",
    "get_member_display_name",
    "make_image_segment",
    "make_mention_segment",
    "make_reply_segment",
    "make_text_message",
    "onebot_protocol",
]

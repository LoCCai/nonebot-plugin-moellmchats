from __future__ import annotations

from nonebot.adapters.onebot.v11 import Message

from nonebot_plugin_moellmchats.utils import format_context_message


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

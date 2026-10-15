from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.adapters.onebot.v11.event import PokeNotifyEvent
import pytest

import nonebot_plugin_moellmchats as plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [None, 0, 12345])
async def test_poke_reply_uses_private_target_without_group_id(monkeypatch, group_id):
    calls = []

    class Adapter:
        @staticmethod
        def get_name():
            return "OneBot V11"

        async def _call_api(self, bot, api, **data):
            calls.append((api, data))
            return {"message_id": 1}

    bot = Bot(Adapter(), "3801365207")
    event = PokeNotifyEvent(
        time=1792074729, self_id=3801365207, post_type="notice",
        notice_type="notify", sub_type="poke", user_id=275549989,
        target_id=3801365207, group_id=group_id,
    )
    group_send = AsyncMock()
    monkeypatch.setattr(plugin, "poke_", SimpleNamespace(send=group_send))
    monkeypatch.setattr(plugin.config_parser, "get_config", lambda key: 0)
    monkeypatch.setattr(plugin, "get_reply_messages", lambda key: ["收到戳戳"])

    await plugin._poke_event(bot, event)

    if group_id:
        group_send.assert_awaited_once_with(Message("收到戳戳"))
        assert calls == []
    else:
        group_send.assert_not_awaited()
        assert calls == [("send_private_msg", {"user_id": 275549989, "message": Message("收到戳戳")})]


@pytest.mark.asyncio
async def test_poke_for_someone_else_is_ignored():
    bot = SimpleNamespace(send_private_msg=AsyncMock())
    event = SimpleNamespace(is_tome=False)
    await plugin._poke_event(bot, event)
    bot.send_private_msg.assert_not_awaited()

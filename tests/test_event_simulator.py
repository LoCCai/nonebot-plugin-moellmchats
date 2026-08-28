from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import Bot as V11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.adapters.onebot.v12 import (
    BotSelf,
)
from nonebot.adapters.onebot.v12 import (
    GroupMessageEvent as V12GroupMessageEvent,
)
from nonebot.adapters.onebot.v12 import (
    Message as V12Message,
)
from nonebot.adapters.onebot.v12 import (
    MessageSegment as V12MessageSegment,
)
from nonebot.internal.matcher import matchers
import pytest

from nonebot_plugin_moellmchats import event_simulator as simulator_module
from nonebot_plugin_moellmchats.admission import AdmissionController


def _event(message_id: int, text: str = "original") -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=10000,
        post_type="message",
        sub_type="normal",
        user_id=123,
        message_type="group",
        group_id=456,
        message_id=message_id,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=123, nickname="tester", card="tester"),
    )


def _v12_event(message_id: str, text: str = "original") -> V12GroupMessageEvent:
    message = V12Message(
        [
            V12MessageSegment.text(text),
            V12MessageSegment.mention("actor-2"),
            V12MessageSegment.image("file-1"),
        ]
    )
    return V12GroupMessageEvent(
        id=f"event-{message_id}",
        time=datetime(2026, 8, 28, tzinfo=timezone.utc),
        type="message",
        detail_type="group",
        sub_type="normal",
        self=BotSelf(platform="qq", user_id="bot-v12"),
        message_id=message_id,
        message=message,
        original_message=message,
        alt_message=text,
        user_id="actor-1",
        group_id="group-1",
    )


@pytest.mark.asyncio
async def test_targeted_dispatch_only_checks_selected_plugin(monkeypatch) -> None:
    selected = [SimpleNamespace(priority=10), SimpleNamespace(priority=20)]
    checked = []
    monkeypatch.setattr(
        simulator_module, "get_plugin", lambda name: SimpleNamespace(matcher=selected)
    )

    async def check(matcher, *args, **kwargs):
        checked.append(matcher)

    monkeypatch.setattr(simulator_module, "check_and_run_matcher", check)
    await simulator_module._dispatch_targeted(object(), _event(1), "selected")
    assert checked == selected


@pytest.mark.asyncio
async def test_capture_contexts_do_not_cross_talk(monkeypatch) -> None:
    gate = AdmissionController(name="dispatch", max_active=2, max_pending=2)
    monkeypatch.setattr(simulator_module, "get_dispatch_controller", lambda: gate)
    original_get_config = simulator_module.config_parser.get_config
    monkeypatch.setattr(
        simulator_module.config_parser,
        "get_config",
        lambda key, default=None: []
        if key == "legacy_full_event_plugins"
        else original_get_config(key, default),
    )

    async def dispatch(bot, event, plugin_name):
        await asyncio.sleep(0.02 if "first" in str(event.message) else 0)
        await simulator_module._capture_outgoing_api(
            bot, "send_group_msg", {"message": Message(str(event.message))}
        )

    monkeypatch.setattr(simulator_module, "_dispatch_targeted", dispatch)
    results = await asyncio.gather(
        simulator_module.event_simulator.dispatch_event(
            object(), _event(1), "first", plugin_name="a"
        ),
        simulator_module.event_simulator.dispatch_event(
            object(), _event(2), "second", plugin_name="b"
        ),
    )
    assert results == [("first", []), ("second", [])]
    assert simulator_module._captures == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api",
    ["send_msg", "send_group_msg", "send_private_msg"],
)
async def test_capture_supports_every_onebot_message_send_action(api: str) -> None:
    capture_id = uuid.uuid4().hex
    simulator_module._captures[capture_id] = {
        "messages": [],
        "original_id": 10,
        "fake_id": 20,
    }
    token = simulator_module._capture_key.set(capture_id)
    data = {"message": Message("正文")}
    try:
        await simulator_module._capture_outgoing_api(object(), api, data)
        assert simulator_module._captures[capture_id]["messages"] == [
            {"text": "正文", "images": []}
        ]
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


@pytest.mark.asyncio
async def test_capture_supports_v12_send_message_and_file_id() -> None:
    capture_id = uuid.uuid4().hex
    simulator_module._captures[capture_id] = {
        "messages": [],
        "original_id": "original-message",
        "fake_id": "fake-message",
        "protocol": "onebot_v12",
    }
    token = simulator_module._capture_key.set(capture_id)
    data = {
        "message": V12Message(
            [
                V12MessageSegment.reply("fake-message"),
                V12MessageSegment.text("正文"),
                V12MessageSegment.mention("actor-2"),
                V12MessageSegment.image("file-1"),
            ]
        )
    }
    try:
        await simulator_module._capture_outgoing_api(
            object(),
            "send_message",
            data,
        )
        assert data["message"][0].data["message_id"] == "original-message"
        assert simulator_module._captures[capture_id]["messages"] == [
            {
                "text": "正文[提及:actor-2][图片]",
                "images": ["file-1"],
            }
        ]
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


def test_v12_synthetic_event_preserves_protocol_and_string_identity() -> None:
    original = _v12_event("original-message")
    fake, fake_id = simulator_module._build_event(
        original,
        "执行命令[at:1]",
        {"mentions": [{"qq": "actor-2"}]},
    )

    assert isinstance(fake, V12GroupMessageEvent)
    assert isinstance(fake_id, str)
    assert fake.message_id == fake_id
    assert fake.self.user_id == "bot-v12"
    assert fake.user_id == "actor-1"
    assert fake.group_id == "group-1"
    assert any(
        segment.type == "mention" and segment.data["user_id"] == "actor-2"
        for segment in fake.message
    )
    assert any(
        segment.type == "image" and segment.data["file_id"] == "file-1"
        for segment in fake.message
    )


@pytest.mark.asyncio
async def test_capture_does_not_consume_send_like_side_effect() -> None:
    capture_id = uuid.uuid4().hex
    simulator_module._captures[capture_id] = {
        "messages": [],
        "original_id": 10,
        "fake_id": 20,
    }
    token = simulator_module._capture_key.set(capture_id)
    try:
        await simulator_module._capture_outgoing_api(
            object(),
            "send_like",
            {"user_id": 123, "times": 1},
        )
        assert simulator_module._captures[capture_id]["messages"] == []
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


@pytest.mark.asyncio
async def test_natural_language_like_runs_real_business_matcher_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        @staticmethod
        def get_name() -> str:
            return "OneBot V11"

    class LikeBot(V11Bot):
        def __init__(self) -> None:
            super().__init__(Adapter(), "10000")
            self.calls: list[tuple[str, dict]] = []

        async def call_api(self, api: str, **data):
            self.calls.append((api, data))
            return {"ok": True}

    business_matcher = on_fullmatch(
        "给我点赞",
        priority=1,
        block=True,
    )

    @business_matcher.handle()
    async def business_like(bot: V11Bot, event: GroupMessageEvent) -> None:
        await bot.send_like(user_id=event.user_id, times=1)

    monkeypatch.setattr(
        simulator_module,
        "get_plugin",
        lambda name: (
            SimpleNamespace(matcher={business_matcher})
            if name == "qi_group_admin"
            else None
        ),
    )
    bot = LikeBot()
    try:
        await simulator_module._dispatch_targeted(
            bot,
            _event(100, "给我点赞"),
            "qi_group_admin",
        )
    finally:
        for priority_matchers in matchers.values():
            if business_matcher in priority_matchers:
                priority_matchers.remove(business_matcher)

    assert bot.calls == [("send_like", {"user_id": 123, "times": 1})]


@pytest.mark.asyncio
async def test_dispatch_timeout_cancels_target(monkeypatch) -> None:
    cancelled = asyncio.Event()
    gate = AdmissionController(name="dispatch", max_active=1, max_pending=1)
    monkeypatch.setattr(simulator_module, "get_dispatch_controller", lambda: gate)

    async def get_config(key, default=None):
        return default

    original_get_config = simulator_module.config_parser.get_config
    monkeypatch.setattr(
        simulator_module.config_parser,
        "get_config",
        lambda key, default=None: 0.01
        if key == "legacy_dispatch_timeout_seconds"
        else []
        if key == "legacy_full_event_plugins"
        else original_get_config(key, default),
    )

    async def dispatch(*args):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(simulator_module, "_dispatch_targeted", dispatch)
    result = await simulator_module.event_simulator.dispatch_event(
        object(), _event(3), "slow", plugin_name="slow"
    )
    assert "超时" in result[0]
    assert cancelled.is_set()

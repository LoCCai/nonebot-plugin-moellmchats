from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
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

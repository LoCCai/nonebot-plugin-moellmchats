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
    Bot as V12Bot,
)
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
from nonebot.exception import ActionFailed, NetworkError
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
        return False

    monkeypatch.setattr(simulator_module, "_observe_and_run_matcher", check)
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
        data = {"message": Message(str(event.message))}
        await simulator_module._capture_outgoing_api(
            bot, "send_group_msg", data
        )
        await simulator_module._confirm_outgoing_api(
            bot, None, "send_group_msg", data, {"message_id": 1}
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
    assert [result.status for result in results] == [
        simulator_module.PluginDispatchStatus.MATCHED_WITH_OUTPUT,
        simulator_module.PluginDispatchStatus.MATCHED_WITH_OUTPUT,
    ]
    assert [result.text for result in results] == ["first", "second"]
    assert simulator_module._captures == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api",
    ["send_msg", "send_group_msg", "send_private_msg"],
)
async def test_capture_supports_every_onebot_message_send_action(api: str) -> None:
    capture_id = uuid.uuid4().hex
    context = simulator_module._empty_dispatch_context()
    context.update({
        "original_id": 10,
        "fake_id": 20,
    })
    simulator_module._captures[capture_id] = context
    token = simulator_module._capture_key.set(capture_id)
    data = {"message": Message("正文")}
    try:
        await simulator_module._capture_outgoing_api(object(), api, data)
        assert simulator_module._captures[capture_id]["messages"] == []
        await simulator_module._confirm_outgoing_api(
            object(), None, api, data, {"message_id": 1}
        )
        assert simulator_module._captures[capture_id]["messages"] == [
            {"text": "正文", "images": []}
        ]
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


@pytest.mark.asyncio
async def test_capture_supports_v12_send_message_and_file_id() -> None:
    capture_id = uuid.uuid4().hex
    context = simulator_module._empty_dispatch_context()
    context.update({
        "original_id": "original-message",
        "fake_id": "fake-message",
        "protocol": "onebot_v12",
    })
    simulator_module._captures[capture_id] = context
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
        assert simulator_module._captures[capture_id]["messages"] == []
        await simulator_module._confirm_outgoing_api(
            object(), None, "send_message", data, {"message_id": "sent"}
        )
        assert simulator_module._captures[capture_id]["messages"] == [
            {
                "text": "正文[提及:actor-2][图片]",
                "images": ["file-1"],
            }
        ]
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


@pytest.mark.asyncio
async def test_v12_matcher_send_message_is_verified_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        @staticmethod
        def get_name() -> str:
            return "OneBot V12"

        @staticmethod
        def get_send(_impl: str, _platform: str):
            async def send(bot, event, message, **_kwargs):
                return await bot.call_api(
                    "send_message",
                    detail_type=event.detail_type,
                    group_id=event.group_id,
                    message=V12Message(message),
                )

            return send

        async def _call_api(self, _bot, api: str, **data):
            self.calls.append((api, data))
            return {"message_id": "sent-v12"}

    matcher = on_fullmatch("v12发送", priority=1, block=True)

    @matcher.handle()
    async def send_v12(bot: V12Bot, event: V12GroupMessageEvent) -> None:
        await bot.send(
            event,
            V12Message(
                [
                    V12MessageSegment.text("v12正文"),
                    V12MessageSegment.image("result-file"),
                ]
            ),
        )

    monkeypatch.setattr(
        simulator_module,
        "get_plugin",
        lambda _name: SimpleNamespace(matcher={matcher}),
    )
    adapter = Adapter()
    try:
        result = await simulator_module.event_simulator.dispatch_event(
            V12Bot(adapter, "bot-v12", "test-impl", "qq"),
            _v12_event("original-v12", "v12发送"),
            "v12发送",
            plugin_name="v12_demo",
        )
    finally:
        for priority_matchers in matchers.values():
            if matcher in priority_matchers:
                priority_matchers.remove(matcher)

    assert [api for api, _data in adapter.calls] == ["send_message"]
    assert result.status is simulator_module.PluginDispatchStatus.MATCHED_WITH_OUTPUT
    assert result.text == "v12正文[图片]"
    assert result.images == ("result-file",)
    assert result.matcher_matched == 1
    assert result.successful_captures == 1
    assert result.api_succeeded == 1


@pytest.mark.asyncio
async def test_verified_output_then_handler_failure_is_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        @staticmethod
        def get_name() -> str:
            return "OneBot V11"

        async def _call_api(self, _bot, api: str, **data):
            self.calls.append((api, data))
            return {"message_id": 1}

    matcher = on_fullmatch("部分成功", priority=1, block=True)

    @matcher.handle()
    async def send_then_fail(bot: V11Bot, event: GroupMessageEvent) -> None:
        await bot.send(event, "已经发送")
        raise RuntimeError("private handler detail")

    monkeypatch.setattr(
        simulator_module,
        "get_plugin",
        lambda _name: SimpleNamespace(matcher={matcher}),
    )
    adapter = Adapter()
    try:
        result = await simulator_module.event_simulator.dispatch_event(
            V11Bot(adapter, "10000"),
            _event(103, "部分成功"),
            "部分成功",
            plugin_name="partial_demo",
        )
    finally:
        for priority_matchers in matchers.values():
            if matcher in priority_matchers:
                priority_matchers.remove(matcher)

    assert [api for api, _data in adapter.calls] == ["send_msg"]
    assert result.status is simulator_module.PluginDispatchStatus.PARTIAL_SUCCESS
    assert result.text == "已经发送"
    assert result.matcher_matched == 1
    assert result.matcher_failed == 1
    assert result.successful_captures == 1
    assert result.api_succeeded == 1


@pytest.mark.asyncio
async def test_read_only_api_without_output_remains_matched_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        @staticmethod
        def get_name() -> str:
            return "OneBot V11"

        async def _call_api(self, _bot, api: str, **_data):
            self.calls.append(api)
            return {"online": True, "good": True}

    matcher = on_fullmatch("读取状态", priority=1, block=True)

    @matcher.handle()
    async def read_status(bot: V11Bot) -> None:
        await bot.get_status()

    monkeypatch.setattr(
        simulator_module,
        "get_plugin",
        lambda _name: SimpleNamespace(matcher={matcher}),
    )
    adapter = Adapter()
    try:
        result = await simulator_module.event_simulator.dispatch_event(
            V11Bot(adapter, "10000"),
            _event(104, "读取状态"),
            "读取状态",
            plugin_name="read_demo",
        )
    finally:
        for priority_matchers in matchers.values():
            if matcher in priority_matchers:
                priority_matchers.remove(matcher)

    assert adapter.calls == ["get_status"]
    assert result.status is simulator_module.PluginDispatchStatus.MATCHED_EMPTY
    assert result.api_succeeded == 1
    assert result.mutating_api_succeeded == 0
    assert result.successful_captures == 0


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
    context = simulator_module._empty_dispatch_context()
    context.update({
        "original_id": 10,
        "fake_id": 20,
    })
    simulator_module._captures[capture_id] = context
    token = simulator_module._capture_key.set(capture_id)
    try:
        data = {"user_id": 123, "times": 1}
        await simulator_module._capture_outgoing_api(object(), "send_like", data)
        await simulator_module._confirm_outgoing_api(
            object(), None, "send_like", data, {"ok": True}
        )
        assert simulator_module._captures[capture_id]["messages"] == []
        assert simulator_module._captures[capture_id]["api_succeeded"] == 1
        assert (
            simulator_module._captures[capture_id]["mutating_api_succeeded"]
            == 1
        )
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


@pytest.mark.asyncio
async def test_natural_language_like_runs_real_business_matcher_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        @staticmethod
        def get_name() -> str:
            return "OneBot V11"

        async def _call_api(self, _bot, api: str, **data):
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
    adapter = Adapter()
    bot = V11Bot(adapter, "10000")
    try:
        result = await simulator_module.event_simulator.dispatch_event(
            bot,
            _event(100, "给我点赞"),
            "给我点赞",
            plugin_name="qi_group_admin",
        )
    finally:
        for priority_matchers in matchers.values():
            if business_matcher in priority_matchers:
                priority_matchers.remove(business_matcher)

    assert adapter.calls == [("send_like", {"user_id": 123, "times": 1})]
    assert result.status is simulator_module.PluginDispatchStatus.MATCHED_SIDE_EFFECT
    assert result.matcher_matched == 1
    assert result.api_succeeded == 1
    assert result.mutating_api_succeeded == 1


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
    assert result.status is simulator_module.PluginDispatchStatus.TIMED_OUT
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_called_api_failure_never_commits_staged_output() -> None:
    capture_id = uuid.uuid4().hex
    context = simulator_module._empty_dispatch_context()
    context.update({"original_id": 10, "fake_id": 20})
    simulator_module._captures[capture_id] = context
    token = simulator_module._capture_key.set(capture_id)
    data = {"message": Message("不应成为成功输出")}
    try:
        await simulator_module._capture_outgoing_api(
            object(), "send_group_msg", data
        )
        await simulator_module._confirm_outgoing_api(
            object(),
            ActionFailed("OneBot V11", "failed"),
            "send_group_msg",
            data,
            None,
        )
        assert context["messages"] == []
        assert context["api_failed"] == 1
        assert context["api_unknown"] == 0
    finally:
        simulator_module._capture_key.reset(token)
        simulator_module._captures.pop(capture_id, None)


def test_dispatch_result_distinguishes_all_execution_truth_states() -> None:
    def status(**updates):
        context = simulator_module._empty_dispatch_context()
        context.update(updates)
        return simulator_module._dispatch_result(
            context,
            started_monotonic=0,
        ).status

    assert status() is simulator_module.PluginDispatchStatus.NOT_MATCHED
    assert status(matcher_matched=1) is simulator_module.PluginDispatchStatus.MATCHED_EMPTY
    assert status(messages=[{"text": "正文", "images": []}]) is (
        simulator_module.PluginDispatchStatus.MATCHED_WITH_OUTPUT
    )
    assert status(matcher_matched=1, mutating_api_succeeded=1) is (
        simulator_module.PluginDispatchStatus.MATCHED_SIDE_EFFECT
    )
    assert status(matcher_matched=1, matcher_failed=1) is (
        simulator_module.PluginDispatchStatus.FAILED
    )
    assert status(matcher_matched=1, api_unknown=1) is (
        simulator_module.PluginDispatchStatus.RESULT_UNKNOWN
    )
    assert status(
        matcher_matched=1,
        api_failed=1,
        messages=[{"text": "已发送", "images": []}],
    ) is simulator_module.PluginDispatchStatus.PARTIAL_SUCCESS


@pytest.mark.asyncio
async def test_network_failure_after_api_call_is_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        @staticmethod
        def get_name() -> str:
            return "OneBot V11"

        async def _call_api(self, _bot, _api: str, **_data):
            raise NetworkError("OneBot V11", "disconnected")

    matcher = on_fullmatch("未知结果", priority=1, block=True)

    @matcher.handle()
    async def send_once(bot: V11Bot, event: GroupMessageEvent) -> None:
        await bot.send(event, "可能已发送")

    monkeypatch.setattr(
        simulator_module,
        "get_plugin",
        lambda _name: SimpleNamespace(matcher={matcher}),
    )
    try:
        result = await simulator_module.event_simulator.dispatch_event(
            V11Bot(Adapter(), "10000"),
            _event(101, "未知结果"),
            "未知结果",
            plugin_name="demo",
        )
    finally:
        for priority_matchers in matchers.values():
            if matcher in priority_matchers:
                priority_matchers.remove(matcher)

    assert result.status is simulator_module.PluginDispatchStatus.RESULT_UNKNOWN
    assert result.api_failed == 1
    assert result.api_unknown == 1
    assert result.successful_captures == 0


@pytest.mark.asyncio
async def test_dispatch_admission_rejection_is_typed(monkeypatch) -> None:
    gate = AdmissionController(name="dispatch", max_active=1, max_pending=0)
    monkeypatch.setattr(simulator_module, "get_dispatch_controller", lambda: gate)
    result = await simulator_module.event_simulator.dispatch_event(
        object(), _event(102), "test", plugin_name="demo"
    )
    assert result.status is simulator_module.PluginDispatchStatus.ADMISSION_REJECTED

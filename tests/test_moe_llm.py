from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

from nonebot.adapters.onebot.v11.exception import (
    ActionFailed,
    ApiNotAvailable,
    NetworkError,
)
import pytest

from nonebot_plugin_moellmchats import moe_llm as module
from nonebot_plugin_moellmchats.moe_llm import MoeLlm
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("objective", [False, True])
async def test_pending_confirmation_releases_request_without_followup_model(monkeypatch, objective):
    from unittest.mock import Mock

    from test_llm_tools import _agent_request_runtime

    from nonebot_plugin_moellmchats.agent_runtime import AgentRunState, ToolCallStatus
    from nonebot_plugin_moellmchats.pending_actions import pending_action_store
    from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolSpec

    await pending_action_store.clear()
    values = {"max_tool_rounds": 6, "max_agent_steps": 6, "max_retry_times": 3, "tool_progress_messages_enabled": False}
    monkeypatch.setattr(module.config_parser, "get_config", lambda key, default=None: values.get(key, default))
    executed = []

    async def mutate():
        executed.append(True)
        return "updated"

    spec = ToolSpec(
        name="update_schedule", description="update schedule",
        parameters={"type": "object", "properties": {}}, handler=mutate, effect=ToolEffect.MUTATING,
    )
    chat = object.__new__(MoeLlm)
    chat.bot = _ScriptedBot([None])
    chat.bot.self_id = "10000"
    chat.bot.adapter = SimpleNamespace(get_name=lambda: "OneBot V11")
    chat.event = SimpleNamespace(user_id=1, group_id=456)
    chat.user_id = "1"
    chat.agent_runtime = await _agent_request_runtime(executing=False)
    chat.is_objective = objective
    chat.emotion_flag = False
    chat.is_superuser = True
    chat.format_message_dict = {"text": ["给定时任务加一个预测"]}
    chat.prompt, chat.dynamic_context = "system", ""
    chat.model_info = {"model": "fake", "url": "https://model.invalid", "key": "test-only-key", "stream": False}
    chat.tool_snapshot = ToolSnapshot(
        generation=1, custom_tools={spec.name: {**spec.as_legacy_schema(), "source": "registered"}},
        plugin_info={}, tool_dependencies={}, mcp_tool_names=set(),
    )
    chat._current_tool_usage = Counter()
    chat._pending_vision_images = []
    entity = SimpleNamespace(tool_messages=[], add_used_plugins=lambda value: None)
    messages = SimpleNamespace(messages_entity=entity, pre_process=lambda message: "加一个预测",
        get_send_message_list=lambda **kwargs: [], post_process=Mock())
    monkeypatch.setattr(module, "MessagesHandler", lambda user: messages)
    monkeypatch.setattr(module.model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(module, "get_session", object)

    async def prepare(*args):
        return None

    chat._validate_runtime_model_config = prepare
    chat._prepare_model_info = prepare
    chat.prompt_handler = lambda: None
    chat._build_payload = lambda history: ({"messages": history, "stream": False, "tools": [{"type": "function"}]}, False)
    chat._sanitize_tool_calls_for_history = lambda calls: calls
    model_calls = []

    async def model(*args):
        model_calls.append(True)
        assert len(model_calls) == 1, "等待确认后不应继续模型请求或网络重试"
        return True, "", [{"id": "update", "type": "function", "function": {"name": spec.name, "arguments": "{}"}}], ""

    chat.none_stream_llm_chat = model
    try:
        assert await chat.get_llm_chat() is True
        assert len(model_calls) == 1
        assert executed == []
        assert len(chat.bot.sent) == 1
        assert "确认执行" in chat.bot.sent[0]
        assert await pending_action_store.size() == 1
        assert chat.agent_runtime.run.state is AgentRunState.COMPLETED
        assert chat.agent_runtime.tool_calls[-1].status is ToolCallStatus.WAITING_CONFIRMATION
        if objective:
            messages.post_process.assert_not_called()
        else:
            assert "尚未执行" in messages.post_process.call_args.kwargs["assistant_msg"]
            assert messages.post_process.call_args.kwargs["tool_messages"]
    finally:
        await pending_action_store.clear()


@pytest.mark.asyncio
async def test_model_can_use_followup_tool_round_then_send_answer(monkeypatch):
    values = {"max_tool_rounds": 6, "max_agent_steps": 6, "max_retry_times": 1, "tool_progress_messages_enabled": False}
    monkeypatch.setattr(module.config_parser, "get_config", lambda key, default=None: values.get(key, default))
    executed, observations = [], []

    async def first():
        executed.append("first")
        return "first-result"

    async def second():
        executed.append("second")
        return "second-result"

    chat = object.__new__(MoeLlm)
    chat.bot = _ScriptedBot([None])
    chat.event = object()
    chat.user_id = "10001"
    chat.agent_runtime = None
    chat.is_objective = True
    chat.emotion_flag = False
    chat.is_superuser = False
    chat.format_message_dict = {"text": ["做两个步骤"]}
    chat.prompt, chat.dynamic_context = "system", ""
    chat.model_info = {"model": "fake", "url": "https://model.invalid", "key": "test-only-key", "stream": False}
    chat.tool_snapshot = ToolSnapshot(
        generation=1, custom_tools={"first": {"func": first}, "second": {"func": second}},
        plugin_info={}, tool_dependencies={}, mcp_tool_names=set(),
    )
    chat._current_tool_usage = Counter()
    chat._pending_vision_images = []
    entity = SimpleNamespace(tool_messages=[], add_used_plugins=lambda value: None)
    monkeypatch.setattr(module, "MessagesHandler", lambda user: SimpleNamespace(
        messages_entity=entity, pre_process=lambda message: "做两个步骤", get_send_message_list=lambda **kwargs: [],
    ))
    monkeypatch.setattr(module.model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(module, "get_session", object)

    async def prepare(*args):
        return None

    chat._validate_runtime_model_config = prepare
    chat._prepare_model_info = prepare
    chat.prompt_handler = lambda: None
    chat._build_payload = lambda messages: ({"messages": messages, "stream": False, "tools": [{"type": "function"}]}, False)
    chat._sanitize_tool_calls_for_history = lambda calls: calls

    async def model(session, url, headers, data, proxy, timeout):
        assert timeout.total == 180
        observations.append([message["content"] for message in data["messages"] if message["role"] == "tool"])
        if len(observations) <= 2:
            name = "first" if len(observations) == 1 else "second"
            return True, "", [{"id": name, "type": "function", "function": {"name": name, "arguments": "{}"}}], ""
        return True, "两步都完成了", None, ""

    chat.none_stream_llm_chat = model
    assert await chat.get_llm_chat() is True
    assert executed == ["first", "second"]
    assert len(observations) == 3
    assert "first-result" in observations[1][0]
    assert "second-result" in observations[2][1]
    assert chat.bot.sent == ["两步都完成了"]


@pytest.mark.asyncio
@pytest.mark.parametrize("vision_available", [True, False])
async def test_plugin_progress_then_image_reaches_vision_answer(monkeypatch, vision_available):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
    from nonebot.adapters.onebot.v11.event import Sender

    from nonebot_plugin_moellmchats import event_simulator as simulator
    from nonebot_plugin_moellmchats.admission import AdmissionController

    values = {"max_tool_rounds": 6, "max_agent_steps": 6, "max_retry_times": 1,
              "tool_progress_messages_enabled": False, "legacy_background_plugins": ["qi_zmws"]}
    monkeypatch.setattr(module.config_parser, "get_config", lambda key, default=None: values.get(key, default))
    gate = AdmissionController(name="dispatch", max_active=1, max_pending=1)
    monkeypatch.setattr(simulator, "get_dispatch_controller", lambda: gate)
    finished = asyncio.Event()

    async def dispatch(bot, event, plugin_name):
        context = simulator._captures[simulator._capture_key.get()]
        context["matcher_matched"] += 1
        for index, message in enumerate((Message("正在查询"), Message("[CQ:image,file=base64://aW1hZ2U=]"))):
            if index:
                await asyncio.sleep(0.15)
            data = {"message": message}
            await simulator._capture_outgoing_api(bot, "send_group_msg", data)
            await simulator._confirm_outgoing_api(bot, None, "send_group_msg", data, {"message_id": index + 1})
        finished.set()

    monkeypatch.setattr(simulator, "_dispatch_targeted", dispatch)
    chat = object.__new__(MoeLlm)
    chat.bot = _ScriptedBot([None])
    chat.bot.self_id = "10000"
    chat.bot.adapter = SimpleNamespace(get_name=lambda: "OneBot V11")
    chat.event = GroupMessageEvent(time=1, self_id=10000, post_type="message", sub_type="normal",
        user_id=123, message_type="group", group_id=456, message_id=200, message=Message("分析角色装备"),
        original_message=Message("分析角色装备"), raw_message="分析角色装备", font=0,
        sender=Sender(user_id=123, nickname="tester"))
    chat.user_id = "123"
    chat.agent_runtime = None
    chat.is_objective = True
    chat.emotion_flag = False
    chat.is_superuser = True
    chat.format_message_dict = {"text": ["分析角色装备"]}
    chat.prompt, chat.dynamic_context = "system", ""
    chat.model_info = {"model": "text-model", "url": "https://text.invalid", "key": "text-test-key", "stream": False}
    chat.tool_snapshot = ToolSnapshot(generation=1, custom_tools={},
        plugin_info={"qi_zmws": {"name": "造梦", "description": "查询资料"}},
        tool_dependencies={}, mcp_tool_names=set())
    chat._current_tool_usage = Counter()
    chat._pending_vision_images = []
    entity = SimpleNamespace(tool_messages=[], add_used_plugins=lambda value: None)
    monkeypatch.setattr(module, "MessagesHandler", lambda user: SimpleNamespace(messages_entity=entity,
        pre_process=lambda message: "分析角色装备",
        get_send_message_list=lambda **kwargs: [{"role": "user", "content": "分析角色装备"}]))
    monkeypatch.setattr(module.model_selector, "get_use_tools", lambda: True)
    vision = {"model": "vision-model", "url": "https://vision.invalid", "key": "vision-test-key",
              "stream": False, "no_tools": True}
    monkeypatch.setattr(module.model_selector, "get_model_for_capabilities", lambda *args: vision if vision_available else None)
    monkeypatch.setattr(module, "get_session", object)

    async def prepare(*args):
        return None

    chat._validate_runtime_model_config = prepare
    chat._prepare_model_info = prepare
    chat.prompt_handler = lambda: None
    chat._build_payload = lambda history: ({"messages": history, "model": "text-model", "stream": False,
                                           "tools": [{"type": "function"}]}, False)
    chat._sanitize_tool_calls_for_history = lambda calls: calls
    observations = []

    async def model(session, url, headers, data, proxy, timeout):
        observations.append(data["model"])
        if len(observations) == 1:
            return True, "", [{"id": "profile", "type": "function", "function": {
                "name": "qi_zmws", "arguments": '{"command":"/造梦资料 123|4399|26"}'}}], ""
        assert finished.is_set()
        assert url == "https://vision.invalid"
        assert headers["Authorization"] == "vision-test-key"
        assert "tools" not in data
        assert {"role": "user", "content": "分析角色装备"} in data["messages"]
        image_messages = [message["content"] for message in data["messages"] if isinstance(message["content"], list)]
        assert len(image_messages) == 1
        assert image_messages[0][-1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1hZ2U="}}
        return True, "根据资料图，装备还可以继续强化。", None, ""

    chat.none_stream_llm_chat = model
    result = await chat.get_llm_chat()
    assert finished.is_set()
    assert simulator._captures == {}
    assert simulator._BACKGROUND_TASKS == set()
    if vision_available:
        assert result is True
        assert observations == ["text-model", "vision-model"]
        assert chat.bot.sent == ["根据资料图，装备还可以继续强化。"]
    else:
        assert "未配置视觉模型" in result
        assert observations == ["text-model"]
        assert chat.bot.sent == []


def _action_failed() -> ActionFailed:
    return ActionFailed(
        status="failed",
        retcode=1200,
        data=None,
        message="Timeout",
        wording="",
    )


def _network_error() -> NetworkError:
    return NetworkError("WebSocket call api send_msg timeout")


def _api_not_available() -> ApiNotAvailable:
    return ApiNotAvailable()


ADAPTER_FAILURE_FACTORIES = (
    _action_failed,
    _network_error,
    _api_not_available,
)


class _ScriptedBot:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[object] = []

    async def send(self, _event, message) -> None:
        self.sent.append(message)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


class _V12ScriptedBot(_ScriptedBot):
    class Adapter:
        @staticmethod
        def get_name() -> str:
            return "OneBot V12"

    adapter = Adapter()


def _llm(bot: _ScriptedBot) -> MoeLlm:
    llm = object.__new__(MoeLlm)
    llm.bot = bot
    llm.event = object()
    llm.emotion_flag = True
    return llm


@pytest.mark.asyncio
async def test_retry_notice_failure_is_bounded_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []

    class Recorder:
        def warning(self, message, *args) -> None:
            records.append(str(message).format(*args))

    class SlowBot:
        async def send(self, _event, _message) -> None:
            await asyncio.sleep(1)

    monkeypatch.setattr(module, "logger", Recorder())
    monkeypatch.setattr(module, "_PROGRESS_NOTICE_TIMEOUT_SECONDS", 0.01)
    llm = _llm(SlowBot())  # type: ignore[arg-type]

    await asyncio.wait_for(llm._send_retry_notice(1), timeout=0.2)

    assert records
    assert any("TimeoutError" in record for record in records)


@pytest.mark.asyncio
async def test_retry_notice_preserves_cancellation() -> None:
    class CancelBot:
        async def send(self, _event, _message) -> None:
            raise asyncio.CancelledError

    llm = _llm(CancelBot())  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await llm._send_retry_notice(1)


@pytest.mark.asyncio
async def test_tool_summary_failure_falls_back_without_private_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []

    class Recorder:
        def warning(self, message, *args) -> None:
            records.append(str(message).format(*args))

    monkeypatch.setattr(module, "logger", Recorder())
    llm = _llm(_ScriptedBot([]))
    llm.agent_runtime = None

    async def fail() -> str:
        raise RuntimeError("SECRET_SUMMARY_PAYLOAD")

    assert await llm._call_tool_summary_safely(fail) == ""
    assert records
    assert all("SECRET_SUMMARY_PAYLOAD" not in record for record in records)
    assert any("RuntimeError" in record for record in records)


@pytest.mark.asyncio
async def test_tool_summary_preserves_cancellation() -> None:
    llm = _llm(_ScriptedBot([]))
    llm.agent_runtime = None

    async def cancel() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await llm._call_tool_summary_safely(cancel)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_emotion_failure_is_isolated_after_body_delivery(
    monkeypatch: pytest.MonkeyPatch,
    failure_factory,
) -> None:
    bot = _ScriptedBot([None, failure_factory(), None])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑", "挥手"]),
    )
    monkeypatch.setattr(
        module,
        "get_emotion",
        lambda name, *, protocol: f"图片:{name}",
    )

    result = await llm.send_emotion_message("正文[微笑][挥手]")

    assert result == "正文"
    assert bot.sent == ["正文", "图片:微笑", "图片:挥手"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_body_delivery_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
    failure_factory,
) -> None:
    failure = failure_factory()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑"]),
    )
    monkeypatch.setattr(
        module,
        "get_emotion",
        lambda _name, *, protocol: "图片",
    )

    with pytest.raises(type(failure)) as captured:
        await llm.send_emotion_message("正文[微笑]")

    assert captured.value is failure
    assert bot.sent == ["正文"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_emotion_only_failure_propagates_when_nothing_was_delivered(
    monkeypatch: pytest.MonkeyPatch,
    failure_factory,
) -> None:
    failure = failure_factory()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("", ["微笑"]),
    )
    monkeypatch.setattr(
        module,
        "get_emotion",
        lambda _name, *, protocol: "图片",
    )

    with pytest.raises(type(failure)) as captured:
        await llm.send_emotion_message("[微笑]")

    assert captured.value is failure
    assert bot.sent == ["图片"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_plain_body_failure_is_never_downgraded(failure_factory) -> None:
    failure = failure_factory()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    llm.emotion_flag = False

    with pytest.raises(type(failure)) as captured:
        await llm.send_emotion_message("正文")

    assert captured.value is failure
    assert bot.sent == ["正文"]


@pytest.mark.asyncio
async def test_v12_body_succeeds_and_local_optional_emotion_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _V12ScriptedBot([None])
    llm = _llm(bot)
    protocols: list[str] = []
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑"]),
    )

    def no_v12_local_file(_name: str, *, protocol: str):
        protocols.append(protocol)
        return None

    monkeypatch.setattr(module, "get_emotion", no_v12_local_file)

    result = await llm.send_emotion_message("正文[微笑]")

    assert result == "正文"
    assert protocols == ["onebot_v12"]
    assert bot.sent == ["正文"]

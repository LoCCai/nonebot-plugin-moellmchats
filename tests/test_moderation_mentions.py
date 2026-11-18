from dataclasses import replace
import json
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message
import pytest
from test_agent_context_runtime import _NOW, _identity, _snapshot
from test_llm_tools import Harness, _agent_request_runtime, _call, _source_progress_view

from nonebot_plugin_moellmchats.agent_context_runtime import AgentGenerationCoordinator, AgentRequestRuntime
from nonebot_plugin_moellmchats.agent_runtime import DeadlineContext, ToolCallStatus
from nonebot_plugin_moellmchats.event_simulator import (
    MentionIdentityRequired,
    PluginDispatchResult,
    PluginDispatchStatus,
    _build_fake_message,
    normalize_plugin_command,
)
from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin
from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
from nonebot_plugin_moellmchats.nonebot_plugin_tools import build_nonebot_plugin_candidate
from nonebot_plugin_moellmchats.runtime_resources import RuntimeResourceBuilder
from nonebot_plugin_moellmchats.tool_manager import LlmToolExecutionRoute
from nonebot_plugin_moellmchats.tool_providers import ToolSource

RAW_NAME = "\n\t $ÿĀD#\x00不留行\n\t $ÿĀ"


@pytest.mark.parametrize("verb", ["ban", "禁言", "解禁", "解除禁言", "取消禁言", "unban"])
def test_copied_current_label_is_bound_to_real_identity_before_control_check(verb):
    source = {"mentions": [{"qq": "70004", "name": RAW_NAME}]}
    command = f"/{verb} @{RAW_NAME}" + (" 0" if verb in {"ban", "禁言"} else "")
    normalized = normalize_plugin_command(command, source)
    assert normalized == f"/{verb} [at:1]" + (" 0" if verb in {"ban", "禁言"} else "")
    message = _build_fake_message(command, source)
    assert [(seg.type, seg.data["qq"]) for seg in message if seg.type == "at"] == [("at", "70004")]
    assert RAW_NAME not in str(message)
    assert "\x00" not in str(message)


def test_moderation_never_guesses_missing_or_ambiguous_identity():
    with pytest.raises(MentionIdentityRequired):
        normalize_plugin_command("/解除禁言 @同名", {})
    with pytest.raises(MentionIdentityRequired):
        normalize_plugin_command(
            "/解除禁言 @同名", {"mentions": [{"qq": "70004", "name": "同名"}, {"qq": "70005", "name": "同名"}]}
        )
    with pytest.raises(MentionIdentityRequired):
        _build_fake_message("/解除禁言 [at:2]", {"mentions": [{"qq": "70004", "name": "同名"}]})


def test_nickname_cannot_override_an_explicit_identity_token():
    message = _build_fake_message(
        "/解除禁言 [at:2]", {"mentions": [{"qq": "70004", "name": "[at:2]"}, {"qq": "70005", "name": "正常目标"}]}
    )
    assert [seg.data["qq"] for seg in message if seg.type == "at"] == ["70005"]


def test_copied_labels_cannot_consume_time_or_guess_between_overlapping_names():
    source = {"mentions": [{"qq": "70004", "name": "甲"}, {"qq": "70005", "name": "甲 0"}]}
    with pytest.raises(MentionIdentityRequired):
        normalize_plugin_command("/ban @甲 0 60", source)
    assert normalize_plugin_command("/ban [at:1] 0", {"mentions": [{"qq": "70004", "name": " 0"}]}) == "/ban [at:1] 0"


def test_multiple_exact_copied_targets_keep_original_identity_and_order():
    command = normalize_plugin_command(
        "/ban @乙 @甲 60", {"mentions": [{"qq": "70004", "name": "甲"}, {"qq": "70005", "name": "乙"}]}
    )
    assert command == "/ban [at:2] [at:1] 60"


@pytest.mark.parametrize("command", ["/demo bad\x00value", "/ban @不存在\x00 0"])
def test_unresolved_control_data_is_rejected_before_dispatch(command):
    with pytest.raises(MentionIdentityRequired):
        _build_fake_message(command, {})


def test_protocol_v12_preview_also_keeps_identity_segment():
    message = _build_fake_message("/解除禁言 [at:1]", {"mentions": [{"qq": "70004", "name": RAW_NAME}]}, protocol="onebot_v12")
    assert [seg.data["user_id"] for seg in message if seg.type == "mention"] == ["70004"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/解除禁言 [at:1]", "/ban [CQ:at,qq=70004] 0", f"/ban @{RAW_NAME} 0"])
async def test_plugin_notice_contains_actual_at_segment_and_never_raw_nickname(command):
    harness = Harness({})
    harness.format_message_dict["mentions"] = [{"qq": "70004", "name": RAW_NAME}]
    harness.bot.send = AsyncMock(return_value={"message_id": 12001})
    view = replace(
        _source_progress_view(ToolSource.REGISTERED), route=LlmToolExecutionRoute.NONEBOT_PLUGIN, source=ToolSource.NONEBOT_PLUGIN
    )
    await harness._send_tool_progress(call=_call(1, "qi_group_admin"), view=view, arguments={"command": command}, result_text="")
    message = harness.bot.send.await_args.args[1]
    assert isinstance(message, Message)
    assert [seg.data["qq"] for seg in message if seg.type == "at"] == ["70004"]
    assert message.extract_plain_text().startswith("七七正在帮你投递：/")
    assert RAW_NAME not in str(message)
    assert "\x00" not in str(message)
    await harness._finish_tool_progress()


def test_prompt_and_old_placeholder_labels_hide_binary_card_data():
    class Payload(LlmPayloadMixin):
        def __init__(self):
            self.format_message_dict = {"mentions": [{"qq": "70004", "name": RAW_NAME}]}

    payload = Payload()
    hint = payload._build_tool_mention_hint()
    assert RAW_NAME not in hint
    assert "\\u0000" not in hint
    assert "ÿĀ" not in hint
    assert "[CQ:at,qq=70004]" in hint
    assert "[at:1]" in hint
    assert payload._render_history_placeholders("/ban [at:1] 0") == "/ban @群友 0"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"command": "/ban @bad\x00 0"}, {"nested": [{"name": "bad\x00"}]}])
async def test_invalid_audit_metadata_cannot_replace_completed_action_status(arguments):
    resources = RuntimeResourceBuilder().build(_snapshot())
    runtime = await AgentRequestRuntime.begin(
        AgentGenerationCoordinator(resources),
        _identity(),
        request_id=1,
        deadline=DeadlineContext.from_timeout(30),
        wall_clock=lambda: _NOW.timestamp(),
        monotonic_clock=lambda: 1.0,
    )
    await runtime.record_tool_outcome(
        tool_name="qi_group_admin",
        source=ToolSource.NONEBOT_PLUGIN,
        bundle_id=None,
        bundle_digest=None,
        arguments=arguments,
        status=ToolCallStatus.COMPLETED,
        created_at=_NOW.timestamp(),
        started_monotonic=1.0,
        result_preview="操作已确认",
    )
    assert runtime._tool_calls[-1].status is ToolCallStatus.COMPLETED
    assert dict(runtime._tool_calls[-1].arguments) == {"_invalid_arguments_omitted": True}
    assert "metadata omitted" in runtime._steps[-1].input_preview


def test_invalid_tool_arguments_are_rejected_before_execution_and_audit_is_still_recordable():
    error = LlmToolsMixin._validate_tool_arguments({"query": "bad\x00"}, {"type": "object"})
    assert "NUL" in error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"/ban @{RAW_NAME} 0", "/ban [at:1] 0"),
        (f"/解除禁言 @{RAW_NAME}", "/解除禁言 [at:1]"),
    ],
)
async def test_execute_tools_normalizes_identity_before_dispatch_notice_and_audit(monkeypatch, command, expected):
    from nonebot_plugin_moellmchats import llm_tools as module

    legacy, _ = build_nonebot_plugin_candidate(
        {"qi_group_admin": {"description": "群管理", "usage": "/ban @成员 分钟\n/解除禁言 @成员"}}
    )
    harness = Harness({}, plugins=legacy)
    harness.format_message_dict["mentions"] = [{"qq": "70004", "name": RAW_NAME}]
    harness.bot.send = AsyncMock(return_value={"message_id": 12001})
    harness.agent_runtime = await _agent_request_runtime()
    dispatched = []

    async def dispatch(_bot, _event, normalized, source, **_kwargs):
        message = _build_fake_message(normalized, source)
        assert [segment.data["qq"] for segment in message if segment.type == "at"] == ["70004"]
        dispatched.append(normalized)
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text="成功解禁 1/1 个用户",
            matcher_matched=1,
            successful_captures=1,
            api_succeeded=2,
            mutating_api_succeeded=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    messages = await harness._execute_tools([_call(1, "qi_group_admin", json.dumps({"command": command}))], "", [], "")
    assert dispatched == [expected]
    notice = harness.bot.send.await_args.args[1]
    assert isinstance(notice, Message)
    assert [segment.data["qq"] for segment in notice if segment.type == "at"] == ["70004"]
    assert RAW_NAME not in str(notice)
    assert "\x00" not in str(notice)
    call = harness.agent_runtime._tool_calls[-1]
    assert call.status is ToolCallStatus.COMPLETED
    assert dict(call.arguments) == {"command": expected}
    assert "成功解禁" in messages[-1]["content"]
    await harness._finish_tool_progress()


@pytest.mark.asyncio
async def test_execute_tools_rejects_unresolved_binary_target_without_dispatch_or_notice(monkeypatch):
    from nonebot_plugin_moellmchats import llm_tools as module

    legacy, _ = build_nonebot_plugin_candidate({"qi_group_admin": {"description": "群管理", "usage": "/ban @成员 分钟"}})
    harness = Harness({}, plugins=legacy)
    harness.bot.send = AsyncMock()
    harness.agent_runtime = await _agent_request_runtime()
    dispatch = AsyncMock(side_effect=AssertionError("invalid command must not execute"))
    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    messages = await harness._execute_tools(
        [_call(1, "qi_group_admin", json.dumps({"command": f"/ban @{RAW_NAME} 0"}))], "", [], ""
    )
    dispatch.assert_not_awaited()
    harness.bot.send.assert_not_awaited()
    call = harness.agent_runtime._tool_calls[-1]
    assert call.status is ToolCallStatus.REJECTED
    assert dict(call.arguments) == {"_invalid_arguments_omitted": True}
    assert "参数错误" in messages[-1]["content"]

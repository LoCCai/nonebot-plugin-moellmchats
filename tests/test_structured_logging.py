from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import importlib
import json
import logging
from types import MappingProxyType

import pytest

from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRun,
    AgentRunState,
    AgentStep,
    AgentStepStatus,
    AgentStepType,
    ToolCall,
    ToolCallStatus,
)
from nonebot_plugin_moellmchats.structured_logging import (
    STRUCTURED_LOG_CONTEXT_FIELDS,
    STRUCTURED_LOG_MAX_BYTES,
    STRUCTURED_LOG_VERSION,
    StructuredLogClockError,
    StructuredLogContext,
    StructuredLogEmitter,
    StructuredLogLevel,
    StructuredLogRecord,
    StructuredLogSinkError,
    structured_log_field_names,
)

_UTC_NOW = datetime(2026, 8, 23, 9, 0, 1, 234567, tzinfo=timezone.utc)


def _run(**overrides: object) -> AgentRun:
    values: dict[str, object] = {
        "run_id": "run-structured-1",
        "request_id": 42,
        "user_id": "qq:10001",
        "group_id": "qq-group:20001",
        "generation": 7,
        "state": AgentRunState.EXECUTING,
        "started_at": 10.0,
    }
    values.update(overrides)
    return AgentRun(**values)  # type: ignore[arg-type]


def _step(**overrides: object) -> AgentStep:
    values: dict[str, object] = {
        "step_id": "step-structured-1",
        "run_id": "run-structured-1",
        "index": 1,
        "type": AgentStepType.TOOL,
        "status": AgentStepStatus.RUNNING,
        "tool": "safe_lookup",
        "input": {"secret": "step-input-must-not-leak"},
        "started_at": 11.0,
    }
    values.update(overrides)
    return AgentStep(**values)  # type: ignore[arg-type]


def _call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "tool_call_id": "call-structured-1",
        "run_id": "run-structured-1",
        "step_id": "step-structured-1",
        "tool_name": "safe_lookup",
        "bundle_digest": "a" * 64,
        "arguments": {"token": "tool-argument-must-not-leak"},
        "status": ToolCallStatus.COMPLETED,
        "confirmed": True,
        "result": {"secret": "tool-result-must-not-leak"},
        "elapsed": 0.25,
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


class _Sink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, line: str, /) -> None:
        self.lines.append(line)


def test_wire_schema_is_closed_and_contains_every_planned_context_field() -> None:
    assert STRUCTURED_LOG_VERSION == 1
    assert STRUCTURED_LOG_MAX_BYTES == 4_096
    assert STRUCTURED_LOG_CONTEXT_FIELDS == (
        "request_id",
        "run_id",
        "step_id",
        "tool_call_id",
        "generation",
        "user_id",
        "group_id",
        "model",
        "tool",
    )
    assert structured_log_field_names() == (
        "version",
        "timestamp",
        "level",
        "event",
        *STRUCTURED_LOG_CONTEXT_FIELDS,
    )


def test_context_is_frozen_and_returns_a_detached_fixed_mapping() -> None:
    context = StructuredLogContext(
        request_id=42,
        run_id="run-1",
        step_id="step-1",
        tool_call_id="call-1",
        generation=7,
        user_id="qq:10001",
        group_id="group:20001",
        model="模型-a",
        tool="safe_lookup",
    )

    payload = context.as_dict()
    assert tuple(payload) == STRUCTURED_LOG_CONTEXT_FIELDS
    assert payload == {
        "request_id": 42,
        "run_id": "run-1",
        "step_id": "step-1",
        "tool_call_id": "call-1",
        "generation": 7,
        "user_id": "qq:10001",
        "group_id": "group:20001",
        "model": "模型-a",
        "tool": "safe_lookup",
    }
    payload["run_id"] = "changed"
    assert context.run_id == "run-1"
    with pytest.raises(FrozenInstanceError):
        context.run_id = "changed"  # type: ignore[misc]


def test_empty_context_still_serializes_all_correlation_fields_as_null() -> None:
    context = StructuredLogContext()

    assert context.as_dict() == dict.fromkeys(STRUCTURED_LOG_CONTEXT_FIELDS)


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": True},
        {"request_id": 0},
        {"request_id": 1 << 63},
        {"generation": True},
        {"generation": -1},
        {"generation": 1 << 63},
        {"run_id": ""},
        {"run_id": "bad:run"},
        {"run_id": "x" * 129},
        {"step_id": "bad step", "run_id": "run-1"},
        {"tool_call_id": "bad/call", "run_id": "run-1", "step_id": "step-1"},
        {"user_id": " leading"},
        {"user_id": "trailing "},
        {"user_id": "line\nbreak"},
        {"group_id": "separator\u2028value"},
        {"group_id": "\ud800"},
        {"model": ""},
        {"model": "x" * 256},
        {"model": b"model"},
        {"tool": "bad.tool"},
        {"tool": "x" * 65},
        {"tool": 1},
    ],
)
def test_context_rejects_noncanonical_unbounded_or_unsafe_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="StructuredLogContext"):
        StructuredLogContext(**changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"step_id": "step-1"},
        {"tool_call_id": "call-1"},
        {"run_id": "run-1", "tool_call_id": "call-1"},
    ],
)
def test_context_rejects_orphan_step_and_tool_call_identity(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"同时绑定"):
        StructuredLogContext(**changes)  # type: ignore[arg-type]


def test_from_agent_run_copies_only_safe_identity_and_generation_fields() -> None:
    run = _run()

    context = StructuredLogContext.from_agent_run(run, model="model-a")

    assert context == StructuredLogContext(
        request_id=42,
        run_id="run-structured-1",
        generation=7,
        user_id="qq:10001",
        group_id="qq-group:20001",
        model="model-a",
    )
    assert not hasattr(context, "state")
    assert not hasattr(context, "started_at")
    with pytest.raises(TypeError, match="AgentRun"):
        StructuredLogContext.from_agent_run(object())  # type: ignore[arg-type]


def test_bind_step_preserves_run_context_and_never_copies_input_or_output() -> None:
    context = StructuredLogContext.from_agent_run(_run()).bind_step(_step())

    assert context.step_id == "step-structured-1"
    assert context.tool == "safe_lookup"
    assert context.request_id == 42
    assert context.generation == 7
    assert "step-input-must-not-leak" not in repr(context)
    assert not hasattr(context, "input")
    assert not hasattr(context, "output")


@pytest.mark.parametrize(
    ("context", "step", "message"),
    [
        (StructuredLogContext(), _step(), "run_id"),
        (StructuredLogContext(run_id="other-run"), _step(), "run_id"),
        (
            StructuredLogContext(run_id="run-structured-1", step_id="other-step"),
            _step(),
            "其他 step_id",
        ),
        (
            StructuredLogContext(run_id="run-structured-1", model="model-a"),
            _step(type=AgentStepType.MODEL, model="model-b", tool=None),
            "model",
        ),
        (
            StructuredLogContext(run_id="run-structured-1", tool="other_tool"),
            _step(),
            "tool",
        ),
    ],
)
def test_bind_step_rejects_cross_run_step_or_semantic_identity_drift(
    context: StructuredLogContext,
    step: AgentStep,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        context.bind_step(step)


def test_bind_tool_call_checks_hierarchy_and_omits_arguments_results_and_digest() -> None:
    context = StructuredLogContext.from_agent_run(_run()).bind_tool_call(_call())
    record = StructuredLogRecord(
        event="tool.call.completed",
        level=StructuredLogLevel.INFO,
        context=context,
        occurred_at=_UTC_NOW,
    )
    rendered = record.to_json_line()

    assert context.step_id == "step-structured-1"
    assert context.tool_call_id == "call-structured-1"
    assert context.tool == "safe_lookup"
    assert "tool-argument-must-not-leak" not in rendered
    assert "tool-result-must-not-leak" not in rendered
    assert "a" * 64 not in rendered
    assert not hasattr(context, "arguments")
    assert not hasattr(context, "result")


@pytest.mark.parametrize(
    ("context", "call", "message"),
    [
        (StructuredLogContext(), _call(), "run_id"),
        (StructuredLogContext(run_id="other-run"), _call(), "run_id"),
        (
            StructuredLogContext(run_id="run-structured-1", step_id="other-step"),
            _call(),
            "step_id",
        ),
        (
            StructuredLogContext(
                run_id="run-structured-1",
                step_id="step-structured-1",
                tool_call_id="other-call",
            ),
            _call(),
            "其他 tool_call_id",
        ),
        (
            StructuredLogContext(run_id="run-structured-1", tool="other_tool"),
            _call(),
            "tool",
        ),
    ],
)
def test_bind_tool_call_rejects_cross_object_identity_drift(
    context: StructuredLogContext,
    call: ToolCall,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        context.bind_tool_call(call)


def test_record_normalizes_utc_and_renders_one_canonical_bounded_json_line() -> None:
    local_time = _UTC_NOW.astimezone(timezone(timedelta(hours=8)))
    context = StructuredLogContext.from_agent_run(_run(), model="模型-a", tool="safe_lookup")
    record = StructuredLogRecord(
        event="agent.run.executing",
        level=StructuredLogLevel.INFO,
        context=context,
        occurred_at=local_time,
    )

    assert record.occurred_at == _UTC_NOW
    assert record.occurred_at.tzinfo is timezone.utc
    payload = record.as_dict()
    assert tuple(payload) == structured_log_field_names()
    assert payload["timestamp"] == "2026-08-23T09:00:01.234567Z"
    assert payload["event"] == "agent.run.executing"
    assert payload["level"] == "info"
    line = record.to_json_line()
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert len(line.encode("utf-8")) <= STRUCTURED_LOG_MAX_BYTES
    decoded = json.loads(line)
    assert decoded == payload


@pytest.mark.parametrize(
    "event",
    ["", "Agent.Run", "agent run", "agent/run", ".agent", "x" * 129, "事件"],
)
def test_record_rejects_noncanonical_event_tokens(event: str) -> None:
    with pytest.raises(ValueError, match="event"):
        StructuredLogRecord(
            event=event,
            level=StructuredLogLevel.INFO,
            context=StructuredLogContext(),
            occurred_at=_UTC_NOW,
        )


@pytest.mark.parametrize(
    "occurred_at",
    [datetime(2026, 8, 23, 9, 0), "2026-08-23T09:00:00Z", None],
)
def test_record_rejects_naive_or_non_datetime_timestamps(occurred_at: object) -> None:
    with pytest.raises(ValueError, match="occurred_at"):
        StructuredLogRecord(
            event="runtime.ready",
            level=StructuredLogLevel.INFO,
            context=StructuredLogContext(),
            occurred_at=occurred_at,  # type: ignore[arg-type]
        )


def test_record_requires_enum_context_and_has_no_free_form_payload_fields() -> None:
    with pytest.raises(ValueError, match="StructuredLogLevel"):
        StructuredLogRecord(
            event="runtime.ready",
            level="info",  # type: ignore[arg-type]
            context=StructuredLogContext(),
            occurred_at=_UTC_NOW,
        )
    with pytest.raises(TypeError, match="StructuredLogContext"):
        StructuredLogRecord(
            event="runtime.ready",
            level=StructuredLogLevel.INFO,
            context={},  # type: ignore[arg-type]
            occurred_at=_UTC_NOW,
        )

    record = StructuredLogRecord(
        event="runtime.ready",
        level=StructuredLogLevel.INFO,
        context=StructuredLogContext(),
        occurred_at=_UTC_NOW,
    )
    assert not hasattr(record, "message")
    assert not hasattr(record, "metadata")
    assert not hasattr(record, "exception")


def test_maximum_safe_fields_remain_within_the_fixed_jsonl_limit() -> None:
    context = StructuredLogContext(
        request_id=(1 << 63) - 1,
        run_id="r" * 128,
        step_id="s" * 128,
        tool_call_id="c" * 128,
        generation=(1 << 63) - 1,
        user_id="\\" * 128,
        group_id='"' * 128,
        model="模" * 85,
        tool="t" * 64,
    )
    record = StructuredLogRecord(
        event="e" * 128,
        level=StructuredLogLevel.CRITICAL,
        context=context,
        occurred_at=_UTC_NOW,
    )

    line = record.to_json_line()
    assert len(line.encode("utf-8")) <= STRUCTURED_LOG_MAX_BYTES
    assert json.loads(line)["model"] == "模" * 85


def test_emitter_uses_explicit_sink_and_clock_and_returns_the_exact_record() -> None:
    sink = _Sink()
    emitter = StructuredLogEmitter(sink=sink, clock=lambda: _UTC_NOW)
    context = StructuredLogContext.from_agent_run(_run()).bind_step(_step())

    record = emitter.emit(
        event="agent.step.started",
        level=StructuredLogLevel.INFO,
        context=context,
    )

    assert sink.lines == [record.to_json_line()]
    assert json.loads(sink.lines[0]) == record.as_dict()
    assert record.context is context


def test_emitter_default_context_is_empty_and_not_shared_between_calls() -> None:
    sink = _Sink()
    ticks = iter((_UTC_NOW, _UTC_NOW + timedelta(seconds=1)))
    emitter = StructuredLogEmitter(sink=sink, clock=lambda: next(ticks))

    first = emitter.emit(event="runtime.first", level=StructuredLogLevel.DEBUG)
    second = emitter.emit(event="runtime.second", level=StructuredLogLevel.WARNING)

    assert first.context == StructuredLogContext()
    assert second.context == StructuredLogContext()
    assert first.context is not second.context
    assert len(sink.lines) == 2


@pytest.mark.parametrize("clock_value", [None, datetime(2026, 8, 23, 9, 0), "bad"])
def test_emitter_clock_failures_are_generic_and_do_not_expose_clock_values(clock_value: object) -> None:
    emitter = StructuredLogEmitter(sink=_Sink(), clock=lambda: clock_value)  # type: ignore[arg-type]

    with pytest.raises(StructuredLogClockError, match="clock failed") as captured:
        emitter.emit(event="runtime.ready", level=StructuredLogLevel.INFO)

    assert repr(clock_value) not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_emitter_sanitizes_sink_failures_without_replaying_the_record() -> None:
    class FailingSink:
        def __init__(self) -> None:
            self.calls = 0

        def emit(self, line: str, /) -> None:
            self.calls += 1
            assert "runtime.ready" in line
            raise RuntimeError("sink-secret-must-not-leak")

    sink = FailingSink()
    emitter = StructuredLogEmitter(sink=sink, clock=lambda: _UTC_NOW)

    with pytest.raises(StructuredLogSinkError, match="sink failed") as captured:
        emitter.emit(event="runtime.ready", level=StructuredLogLevel.ERROR)

    assert sink.calls == 1
    assert "sink-secret-must-not-leak" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_emitter_validates_record_fields_before_calling_clock_or_sink() -> None:
    calls: list[str] = []

    class Sink:
        def emit(self, line: str, /) -> None:
            calls.append(line)

    def clock() -> datetime:
        calls.append("clock")
        return _UTC_NOW

    emitter = StructuredLogEmitter(sink=Sink(), clock=clock)

    with pytest.raises(ValueError, match="event"):
        emitter.emit(event="unsafe event", level=StructuredLogLevel.INFO)
    with pytest.raises(ValueError, match="StructuredLogLevel"):
        emitter.emit(event="runtime.ready", level="info")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="StructuredLogContext"):
        emitter.emit(
            event="runtime.ready",
            level=StructuredLogLevel.INFO,
            context={},  # type: ignore[arg-type]
        )

    assert calls == []


def test_emitter_rejects_async_or_missing_sink_and_async_clock() -> None:
    class AsyncSink:
        async def emit(self, line: str, /) -> None:
            del line

    class MissingSink:
        pass

    async def async_clock() -> datetime:
        return _UTC_NOW

    with pytest.raises(TypeError, match="同步 StructuredLogSink"):
        StructuredLogEmitter(sink=AsyncSink())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="同步 StructuredLogSink"):
        StructuredLogEmitter(sink=MissingSink())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="clock"):
        StructuredLogEmitter(sink=_Sink(), clock=async_clock)  # type: ignore[arg-type]


def test_emitter_closes_dynamic_coroutine_results_and_fails_closed() -> None:
    async def later() -> datetime:
        return _UTC_NOW

    class DynamicSink:
        def __init__(self) -> None:
            self.result: object | None = None

        def emit(self, line: str, /) -> None:
            assert "runtime.ready" in line
            self.result = later()
            return self.result  # type: ignore[return-value]

    sink = DynamicSink()
    emitter = StructuredLogEmitter(sink=sink, clock=lambda: _UTC_NOW)
    with pytest.raises(StructuredLogSinkError, match="sink failed"):
        emitter.emit(event="runtime.ready", level=StructuredLogLevel.INFO)
    assert sink.result is not None
    assert getattr(sink.result, "cr_frame") is None

    clock_result: list[object] = []

    def dynamic_clock() -> datetime:
        result = later()
        clock_result.append(result)
        return result  # type: ignore[return-value]

    clock_emitter = StructuredLogEmitter(sink=_Sink(), clock=dynamic_clock)
    with pytest.raises(StructuredLogClockError, match="clock failed"):
        clock_emitter.emit(event="runtime.ready", level=StructuredLogLevel.INFO)
    assert len(clock_result) == 1
    assert getattr(clock_result[0], "cr_frame") is None


def test_module_reload_does_not_configure_logging_or_create_live_state() -> None:
    root = logging.getLogger()
    handlers_before = tuple(root.handlers)
    level_before = root.level
    disabled_before = root.disabled

    module = importlib.reload(importlib.import_module("nonebot_plugin_moellmchats.structured_logging"))

    assert tuple(root.handlers) == handlers_before
    assert root.level == level_before
    assert root.disabled is disabled_before
    assert "ContextVar" not in vars(module)
    assert not any(
        isinstance(value, (module.StructuredLogEmitter, module.StructuredLogRecord, module.StructuredLogContext))
        for value in vars(module).values()
    )
    assert not any(isinstance(value, MappingProxyType) for value in vars(module).values())

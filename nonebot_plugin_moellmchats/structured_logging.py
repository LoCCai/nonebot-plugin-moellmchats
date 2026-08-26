from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import inspect
import json
import re
from typing import Protocol, runtime_checkable
import unicodedata

from .agent_runtime import AgentRun, AgentStep, ToolCall
from .database_schema import ENTITY_ID_MAX_CHARS, MODEL_NAME_MAX_CHARS

STRUCTURED_LOG_VERSION = 1
STRUCTURED_LOG_MAX_BYTES = 4_096

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LINE_SEPARATOR_CHARACTERS = frozenset({"\u2028", "\u2029"})

STRUCTURED_LOG_CONTEXT_FIELDS = (
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


class StructuredLogError(RuntimeError):
    """Base error for the detached H-06 structured-log boundary."""


class StructuredLogClockError(StructuredLogError):
    """The injected wall clock failed or returned an invalid timestamp."""


class StructuredLogSinkError(StructuredLogError):
    """The injected synchronous sink failed to accept one canonical line."""


class StructuredLogLevel(str, Enum):
    """Stable severity values for the canonical wire representation."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _require_optional_bigint(
    value: object,
    *,
    label: str,
    positive: bool,
) -> int | None:
    if value is None:
        return None
    minimum = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= _POSTGRES_BIGINT_MAX:
        requirement = "正" if positive else "非负"
        raise ValueError(f"{label} 必须是{requirement} PostgreSQL BIGINT 或 None")
    return value


def _require_optional_entity_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ENTITY_ID_RE.fullmatch(value):
        raise ValueError(f"{label} 必须是安全的有界 canonical 标识或 None")
    return value


def _require_optional_text(
    value: object,
    *,
    label: str,
    maximum_bytes: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} 必须是无首尾空白的有界 UTF-8 文本或 None")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} 必须是有效 UTF-8 文本或 None") from None
    if len(encoded) > maximum_bytes or any(
        unicodedata.category(character).startswith("C") or character in _LINE_SEPARATOR_CHARACTERS for character in value
    ):
        raise ValueError(f"{label} 包含控制字符、行分隔符或超过安全上限")
    return value


def _require_optional_tool_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _TOOL_NAME_RE.fullmatch(value):
        raise ValueError("StructuredLogContext.tool 必须是安全工具名或 None")
    return value


def _normalize_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} 必须是带时区的 datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise ValueError(f"{label} 必须是有效的带时区 datetime") from None


def _timestamp_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_event(value: object) -> str:
    if not isinstance(value, str) or not _EVENT_RE.fullmatch(value):
        raise ValueError("StructuredLogRecord.event 必须是 canonical event token")
    return value


def _require_level(value: object) -> StructuredLogLevel:
    if not isinstance(value, StructuredLogLevel):
        raise ValueError("StructuredLogRecord.level 必须是 StructuredLogLevel")
    return value


def _require_context(value: object) -> StructuredLogContext:
    if not isinstance(value, StructuredLogContext):
        raise TypeError("StructuredLogRecord.context 必须是 StructuredLogContext")
    return value


@dataclass(frozen=True)
class StructuredLogContext:
    """Immutable correlation fields shared by every H-06 log record.

    There is intentionally no arbitrary message, exception text, or metadata
    mapping on this object. Callers must use a bounded event token and these
    explicit identities, which prevents arguments, results, prompts, tokens,
    configuration, and traceback contents from silently entering the log wire.
    """

    request_id: int | None = None
    run_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    generation: int | None = None
    user_id: str | None = None
    group_id: str | None = None
    model: str | None = None
    tool: str | None = None

    def __post_init__(self) -> None:
        request_id = _require_optional_bigint(
            self.request_id,
            label="StructuredLogContext.request_id",
            positive=True,
        )
        generation = _require_optional_bigint(
            self.generation,
            label="StructuredLogContext.generation",
            positive=False,
        )
        run_id = _require_optional_entity_id(
            self.run_id,
            label="StructuredLogContext.run_id",
        )
        step_id = _require_optional_entity_id(
            self.step_id,
            label="StructuredLogContext.step_id",
        )
        tool_call_id = _require_optional_entity_id(
            self.tool_call_id,
            label="StructuredLogContext.tool_call_id",
        )
        user_id = _require_optional_text(
            self.user_id,
            label="StructuredLogContext.user_id",
            maximum_bytes=ENTITY_ID_MAX_CHARS,
        )
        group_id = _require_optional_text(
            self.group_id,
            label="StructuredLogContext.group_id",
            maximum_bytes=ENTITY_ID_MAX_CHARS,
        )
        model = _require_optional_text(
            self.model,
            label="StructuredLogContext.model",
            maximum_bytes=MODEL_NAME_MAX_CHARS,
        )
        tool = _require_optional_tool_name(self.tool)

        if step_id is not None and run_id is None:
            raise ValueError("StructuredLogContext.step_id 必须同时绑定 run_id")
        if tool_call_id is not None and (run_id is None or step_id is None):
            raise ValueError("StructuredLogContext.tool_call_id 必须同时绑定 run_id 与 step_id")

        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "tool_call_id", tool_call_id)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "tool", tool)

    @classmethod
    def from_agent_run(
        cls,
        run: AgentRun,
        *,
        model: str | None = None,
        tool: str | None = None,
    ) -> StructuredLogContext:
        """Create a complete correlation context without retaining the run."""

        if not isinstance(run, AgentRun):
            raise TypeError("run 必须是 AgentRun")
        return cls(
            request_id=run.request_id,
            run_id=run.run_id,
            generation=run.generation,
            user_id=run.user_id,
            group_id=run.group_id,
            model=model,
            tool=tool,
        )

    def bind_step(self, step: AgentStep) -> StructuredLogContext:
        """Return a child context after checking the run and model/tool links."""

        if not isinstance(step, AgentStep):
            raise TypeError("step 必须是 AgentStep")
        if self.run_id is None or self.run_id != step.run_id:
            raise ValueError("AgentStep 与 StructuredLogContext.run_id 不一致")
        if self.step_id is not None and self.step_id != step.step_id:
            raise ValueError("StructuredLogContext 已绑定其他 step_id")
        if self.tool_call_id is not None and self.step_id != step.step_id:
            raise ValueError("StructuredLogContext 已绑定其他 tool call step")
        if self.model is not None and step.model is not None and self.model != step.model:
            raise ValueError("AgentStep.model 与 StructuredLogContext.model 不一致")
        if self.tool is not None and step.tool is not None and self.tool != step.tool:
            raise ValueError("AgentStep.tool 与 StructuredLogContext.tool 不一致")
        return replace(
            self,
            step_id=step.step_id,
            model=step.model if step.model is not None else self.model,
            tool=step.tool if step.tool is not None else self.tool,
        )

    def bind_tool_call(self, call: ToolCall) -> StructuredLogContext:
        """Return a child context without retaining arguments or result data."""

        if not isinstance(call, ToolCall):
            raise TypeError("call 必须是 ToolCall")
        if self.run_id is None or self.run_id != call.run_id:
            raise ValueError("ToolCall 与 StructuredLogContext.run_id 不一致")
        if self.step_id is not None and self.step_id != call.step_id:
            raise ValueError("ToolCall 与 StructuredLogContext.step_id 不一致")
        if self.tool_call_id is not None and self.tool_call_id != call.tool_call_id:
            raise ValueError("StructuredLogContext 已绑定其他 tool_call_id")
        if self.tool is not None and self.tool != call.tool_name:
            raise ValueError("ToolCall.tool_name 与 StructuredLogContext.tool 不一致")
        return replace(
            self,
            step_id=call.step_id,
            tool_call_id=call.tool_call_id,
            tool=call.tool_name,
        )

    def as_dict(self) -> dict[str, int | str | None]:
        """Return all nine context fields in their stable wire order."""

        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool_call_id": self.tool_call_id,
            "generation": self.generation,
            "user_id": self.user_id,
            "group_id": self.group_id,
            "model": self.model,
            "tool": self.tool,
        }


@dataclass(frozen=True)
class StructuredLogRecord:
    """One immutable, canonical and payload-free structured log record."""

    event: str
    level: StructuredLogLevel
    context: StructuredLogContext
    occurred_at: datetime

    def __post_init__(self) -> None:
        _require_event(self.event)
        _require_level(self.level)
        _require_context(self.context)
        occurred_at = _normalize_timestamp(
            self.occurred_at,
            label="StructuredLogRecord.occurred_at",
        )
        object.__setattr__(self, "occurred_at", occurred_at)

    def as_dict(self) -> dict[str, int | str | None]:
        """Return a detached representation with a fixed, closed field set."""

        payload: dict[str, int | str | None] = {
            "version": STRUCTURED_LOG_VERSION,
            "timestamp": _timestamp_text(self.occurred_at),
            "level": self.level.value,
            "event": self.event,
        }
        payload.update(self.context.as_dict())
        return payload

    def to_json_line(self) -> str:
        """Render one canonical UTF-8 JSONL record within the wire limit."""

        try:
            rendered = json.dumps(
                self.as_dict(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            encoded = rendered.encode("utf-8")
        except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
            raise ValueError("StructuredLogRecord 无法编码为 canonical UTF-8 JSON") from None
        if not encoded or len(encoded) + 1 > STRUCTURED_LOG_MAX_BYTES:
            raise ValueError("StructuredLogRecord 超过 JSONL 字节上限")
        return f"{rendered}\n"


@runtime_checkable
class StructuredLogSink(Protocol):
    """Explicit synchronous boundary receiving exactly one canonical line."""

    def emit(self, line: str, /) -> None: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StructuredLogEmitter:
    """Explicitly constructed formatter/sink adapter with no global state."""

    __slots__ = ("_clock", "_sink")

    def __init__(
        self,
        *,
        sink: StructuredLogSink,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(sink, StructuredLogSink) or inspect.iscoroutinefunction(sink.emit):
            raise TypeError("sink 必须实现同步 StructuredLogSink.emit")
        selected_clock = _utc_now if clock is None else clock
        if not callable(selected_clock) or inspect.iscoroutinefunction(selected_clock):
            raise TypeError("clock 必须是同步可调用对象")
        self._sink = sink
        self._clock = selected_clock

    def emit(
        self,
        *,
        event: str,
        level: StructuredLogLevel,
        context: StructuredLogContext | None = None,
    ) -> StructuredLogRecord:
        """Build, render, and synchronously hand off one validated record."""

        selected_context = StructuredLogContext() if context is None else context
        _require_event(event)
        _require_level(level)
        _require_context(selected_context)
        clock_failed = False
        occurred_at: datetime | None = None
        try:
            clock_result = self._clock()
            if inspect.isawaitable(clock_result):
                if inspect.iscoroutine(clock_result):
                    clock_result.close()
                raise TypeError("async clock result")
            occurred_at = _normalize_timestamp(
                clock_result,
                label="StructuredLogEmitter.clock",
            )
        except Exception:
            clock_failed = True
        if clock_failed or occurred_at is None:
            raise StructuredLogClockError("structured log clock failed")
        record = StructuredLogRecord(
            event=event,
            level=level,
            context=selected_context,
            occurred_at=occurred_at,
        )
        line = record.to_json_line()
        sink_failed = False
        try:
            result = self._sink.emit(line)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("async sink result")
        except Exception:
            sink_failed = True
        if sink_failed:
            raise StructuredLogSinkError("structured log sink failed")
        return record


def structured_log_field_names() -> tuple[str, ...]:
    """Expose the closed wire schema without creating a logger or record."""

    return (
        "version",
        "timestamp",
        "level",
        "event",
        *STRUCTURED_LOG_CONTEXT_FIELDS,
    )

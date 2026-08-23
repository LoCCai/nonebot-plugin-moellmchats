from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import math
import re
import time
from types import MappingProxyType
from typing import Any, TypeAlias

from .tool_providers import ToolSource

_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SUBJECT_ID_LIMIT = 128
_MODEL_NAME_LIMIT = 255
_PREVIEW_LIMIT = 6_000
_ERROR_TYPE_LIMIT = 128
_AGENT_RUN_ERROR_LIMIT = 6_000
_AGENT_JSON_MAX_DEPTH = 32
_AGENT_JSON_MAX_NODES = 100_000
_TOOL_ARGUMENTS_MAX_BYTES = 65_536
_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_COST_PRECISION = 24
_COST_SCALE = 12
_MAX_COST_EXCLUSIVE = Decimal(10) ** (_COST_PRECISION - _COST_SCALE)

AgentJsonValue: TypeAlias = (
    bool | int | float | str | Mapping[str, "AgentJsonValue"] | list["AgentJsonValue"] | tuple["AgentJsonValue", ...] | None
)


class AgentRunState(str, Enum):
    """Lifecycle states shared by the runtime, audit, API, and repositories."""

    CREATED = "created"
    ADMITTED = "admitted"
    CLASSIFYING = "classifying"
    PLANNING = "planning"
    EXECUTING = "executing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


class AgentStepType(str, Enum):
    """The stable semantic kinds used by Agent runtime steps."""

    CLASSIFICATION = "classification"
    MODEL = "model"
    TOOL = "tool"
    SUMMARY = "summary"
    VISION = "vision"
    CONFIRMATION = "confirmation"
    MEMORY = "memory"


class AgentStepStatus(str, Enum):
    """Execution status independent from the enclosing AgentRun state."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class ToolCallStatus(str, Enum):
    """Lifecycle status for one detached tool invocation record."""

    PENDING = "pending"
    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


_EXCEPTIONAL_RUN_STATES = frozenset(
    {
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
        AgentRunState.TIMED_OUT,
        AgentRunState.REJECTED,
    }
)
_TERMINAL_RUN_STATES = frozenset({AgentRunState.COMPLETED}) | _EXCEPTIONAL_RUN_STATES
_AGENT_RUN_TRANSITIONS: Mapping[
    AgentRunState,
    frozenset[AgentRunState],
] = MappingProxyType(
    {
        AgentRunState.CREATED: (frozenset({AgentRunState.ADMITTED}) | _EXCEPTIONAL_RUN_STATES),
        AgentRunState.ADMITTED: (frozenset({AgentRunState.CLASSIFYING}) | _EXCEPTIONAL_RUN_STATES),
        AgentRunState.CLASSIFYING: (frozenset({AgentRunState.PLANNING}) | _EXCEPTIONAL_RUN_STATES),
        AgentRunState.PLANNING: (frozenset({AgentRunState.EXECUTING}) | _EXCEPTIONAL_RUN_STATES),
        AgentRunState.EXECUTING: (
            frozenset(
                {
                    AgentRunState.WAITING_CONFIRMATION,
                    AgentRunState.SUMMARIZING,
                }
            )
            | _EXCEPTIONAL_RUN_STATES
        ),
        AgentRunState.WAITING_CONFIRMATION: (
            frozenset(
                {
                    AgentRunState.EXECUTING,
                    AgentRunState.SUMMARIZING,
                }
            )
            | _EXCEPTIONAL_RUN_STATES
        ),
        AgentRunState.SUMMARIZING: (frozenset({AgentRunState.COMPLETED}) | _EXCEPTIONAL_RUN_STATES),
        AgentRunState.COMPLETED: frozenset(),
        AgentRunState.FAILED: frozenset(),
        AgentRunState.CANCELLED: frozenset(),
        AgentRunState.TIMED_OUT: frozenset(),
        AgentRunState.REJECTED: frozenset(),
    }
)
_TERMINAL_STEP_STATUSES = frozenset(
    {
        AgentStepStatus.COMPLETED,
        AgentStepStatus.FAILED,
        AgentStepStatus.CANCELLED,
        AgentStepStatus.TIMED_OUT,
        AgentStepStatus.SKIPPED,
    }
)
_TERMINAL_TOOL_CALL_STATUSES = frozenset(
    {
        ToolCallStatus.COMPLETED,
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.TIMED_OUT,
        ToolCallStatus.REJECTED,
    }
)


def _require_entity_id(value: str, *, owner: str, label: str) -> str:
    if not isinstance(value, str) or not _ENTITY_ID_RE.fullmatch(value):
        raise ValueError(f"{owner}.{label} 必须是安全的非空标识")
    return value


def validate_agent_run_id(value: object) -> str:
    """Validate the stable run identity shared with PostgreSQL repositories."""

    if not isinstance(value, str):
        raise ValueError("run_id 必须是安全的非空标识")
    return _require_entity_id(value, owner="AgentRun", label="run_id")


def validate_agent_step_id(value: object) -> str:
    """Validate the stable step identity shared with PostgreSQL repositories."""

    if not isinstance(value, str):
        raise ValueError("step_id 必须是安全的非空标识")
    return _require_entity_id(value, owner="AgentStep", label="step_id")


def validate_tool_call_id(value: object) -> str:
    """Validate the stable tool-call identity shared with repositories."""

    if not isinstance(value, str):
        raise ValueError("tool_call_id 必须是安全的非空标识")
    return _require_entity_id(value, owner="ToolCall", label="tool_call_id")


def _require_subject_id(value: str, *, owner: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner}.{label} 必须是字符串")
    if not value or value != value.strip():
        raise ValueError(f"{owner}.{label} 必须是非空且无首尾空白的字符串")
    if len(value) > _SUBJECT_ID_LIMIT or _CONTROL_CHARACTER_RE.search(value):
        raise ValueError(f"{owner}.{label} 包含非法字符或长度超限")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{owner}.{label} 必须是有效 UTF-8 文本") from None
    return value


def _require_optional_bounded_text(
    value: object,
    *,
    owner: str,
    label: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _CONTROL_CHARACTER_RE.search(value)
    ):
        raise ValueError(f"{owner}.{label} 必须是无首尾空白和控制字符的有界非空字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{owner}.{label} 必须是有效 UTF-8 文本") from None
    return value


def _require_optional_preview(
    value: object,
    *,
    owner: str,
    label: str,
    maximum: int = _PREVIEW_LIMIT,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{owner}.{label} 必须是不含 NUL 的有界非空字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{owner}.{label} 必须是有效 UTF-8 文本") from None
    return value


def _require_nonnegative_bigint(
    value: object,
    *,
    owner: str,
    label: str,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{owner}.{label} 必须是非负 PostgreSQL BIGINT")
    return value


def _require_optional_nonnegative_bigint(
    value: object,
    *,
    owner: str,
    label: str,
) -> int | None:
    if value is None:
        return None
    return _require_nonnegative_bigint(value, owner=owner, label=label)


def _normalize_cost(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("AgentRun.cost 必须是非负有限 Decimal 或 None")
    if value == 0:
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("AgentRun.cost 必须是普通有限 Decimal")
    canonical_digits = digits
    canonical_exponent = exponent
    while canonical_digits[-1] == 0:
        canonical_digits = canonical_digits[:-1]
        canonical_exponent += 1
    normalized = Decimal((sign, canonical_digits, canonical_exponent))
    if normalized >= _MAX_COST_EXCLUSIVE:
        raise ValueError("AgentRun.cost 超出 NUMERIC(24, 12) 整数位上限")
    if canonical_exponent < -_COST_SCALE:
        raise ValueError("AgentRun.cost 超出 NUMERIC(24, 12) 小数位上限")
    return normalized


def _normalize_timestamp(value: float, *, owner: str, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{owner}.{label} 必须是时间戳")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{owner}.{label} 必须是有限非负时间戳")
    return normalized


def _normalize_persisted_timestamp(
    value: float,
    *,
    owner: str,
    label: str,
) -> float:
    normalized = _normalize_timestamp(value, owner=owner, label=label)
    try:
        mapped = datetime.fromtimestamp(normalized, tz=timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError):
        raise ValueError(f"{owner}.{label} 超出 PostgreSQL 时间映射范围") from None
    if not math.isfinite(mapped) or mapped < 0:
        raise ValueError(f"{owner}.{label} 超出 PostgreSQL 时间映射范围")
    return mapped


def _freeze_agent_json(
    value: AgentJsonValue,
    *,
    label: str,
    depth: int = 0,
    active_containers: set[int] | None = None,
    node_budget: list[int] | None = None,
) -> AgentJsonValue:
    if depth > _AGENT_JSON_MAX_DEPTH:
        raise ValueError(f"{label} JSON 嵌套超过安全上限")
    budget = node_budget if node_budget is not None else [0]
    budget[0] += 1
    if budget[0] > _AGENT_JSON_MAX_NODES:
        raise ValueError(f"{label} JSON 节点数超过安全上限")

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -_POSTGRES_BIGINT_MAX - 1 <= value <= _POSTGRES_BIGINT_MAX:
            raise ValueError(f"{label} JSON 整数超出 64-bit 安全范围")
        return value
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{label} JSON 字符串不得包含 NUL")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} JSON 浮点数必须有限")
        return value

    if isinstance(value, Mapping):
        active = active_containers if active_containers is not None else set()
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} JSON 不得包含循环引用")
        active.add(identity)
        try:
            frozen: dict[str, AgentJsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str) or "\x00" in key:
                    raise ValueError(f"{label} JSON 对象键必须是不含 NUL 的字符串")
                frozen[key] = _freeze_agent_json(
                    item,
                    label=label,
                    depth=depth + 1,
                    active_containers=active,
                    node_budget=budget,
                )
        finally:
            active.remove(identity)
        return MappingProxyType(frozen)

    if isinstance(value, (list, tuple)):
        active = active_containers if active_containers is not None else set()
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} JSON 不得包含循环引用")
        active.add(identity)
        try:
            return tuple(
                _freeze_agent_json(
                    item,
                    label=label,
                    depth=depth + 1,
                    active_containers=active,
                    node_budget=budget,
                )
                for item in value
            )
        finally:
            active.remove(identity)

    raise ValueError(f"{label} 必须是 JSON 兼容值")


def mutable_agent_json(value: AgentJsonValue) -> Any:
    """Return a detached JSON tree suitable for a database driver."""

    if isinstance(value, Mapping):
        return {key: mutable_agent_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mutable_agent_json(item) for item in value]
    return value


def _canonical_agent_json_size(value: AgentJsonValue) -> int:
    try:
        encoded = json.dumps(
            mutable_agent_json(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("Agent JSON 无法安全编码") from None
    return len(encoded)


@dataclass(frozen=True)
class AgentRun:
    """One immutable, generation-bound user request.

    This domain object intentionally contains no live Bot/Event objects and no
    database implementation details. Later runtime, audit, API, and repository
    layers can therefore share the same identity and lifecycle vocabulary.
    """

    run_id: str
    request_id: int
    user_id: str
    group_id: str | None
    conversation_id: str
    generation: int
    state: AgentRunState
    started_at: float
    finished_at: float | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        validate_agent_run_id(self.run_id)
        if (
            not isinstance(self.request_id, int)
            or isinstance(self.request_id, bool)
            or not 1 <= self.request_id <= _POSTGRES_BIGINT_MAX
        ):
            raise ValueError("AgentRun.request_id 必须是正 PostgreSQL BIGINT")
        _require_subject_id(self.user_id, owner="AgentRun", label="user_id")
        if self.group_id is not None:
            _require_subject_id(
                self.group_id,
                owner="AgentRun",
                label="group_id",
            )
        _require_subject_id(
            self.conversation_id,
            owner="AgentRun",
            label="conversation_id",
        )
        _require_nonnegative_bigint(
            self.generation,
            owner="AgentRun",
            label="generation",
        )
        if not isinstance(self.state, AgentRunState):
            raise ValueError("AgentRun.state 必须是 AgentRunState")

        model = _require_optional_bounded_text(
            self.model,
            owner="AgentRun",
            label="model",
            maximum=_MODEL_NAME_LIMIT,
        )
        input_tokens = _require_optional_nonnegative_bigint(
            self.input_tokens,
            owner="AgentRun",
            label="input_tokens",
        )
        output_tokens = _require_optional_nonnegative_bigint(
            self.output_tokens,
            owner="AgentRun",
            label="output_tokens",
        )
        cost = _normalize_cost(self.cost)
        error_type = _require_optional_bounded_text(
            self.error_type,
            owner="AgentRun",
            label="error_type",
            maximum=_ERROR_TYPE_LIMIT,
        )
        error_message = _require_optional_preview(
            self.error_message,
            owner="AgentRun",
            label="error_message",
            maximum=_AGENT_RUN_ERROR_LIMIT,
        )
        if (error_type is None) is not (error_message is None):
            raise ValueError("AgentRun.error_type 与 error_message 必须同时存在或同时为空")

        started_at = _normalize_persisted_timestamp(
            self.started_at,
            owner="AgentRun",
            label="started_at",
        )
        finished_at = (
            None
            if self.finished_at is None
            else _normalize_persisted_timestamp(
                self.finished_at,
                owner="AgentRun",
                label="finished_at",
            )
        )
        if finished_at is not None and finished_at < started_at:
            raise ValueError("AgentRun.finished_at 不能早于 started_at")
        if self.state in _TERMINAL_RUN_STATES:
            if finished_at is None:
                raise ValueError("AgentRun 终态必须包含 finished_at")
        elif finished_at is not None:
            raise ValueError("AgentRun 非终态不得包含 finished_at")

        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "input_tokens", input_tokens)
        object.__setattr__(self, "output_tokens", output_tokens)
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "error_type", error_type)
        object.__setattr__(self, "error_message", error_message)

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_RUN_STATES

    @property
    def elapsed(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    def as_dict(self) -> dict[str, Any]:
        """Return a detached, stable primitive representation."""

        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "user_id": self.user_id,
            "group_id": self.group_id,
            "conversation_id": self.conversation_id,
            "generation": self.generation,
            "state": self.state.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": self.cost,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


class AgentStateMachine:
    """Pure, fail-closed transition policy for immutable AgentRun records."""

    @staticmethod
    def allowed_targets(state: AgentRunState) -> frozenset[AgentRunState]:
        if not isinstance(state, AgentRunState):
            raise ValueError("AgentStateMachine.state 必须是 AgentRunState")
        return _AGENT_RUN_TRANSITIONS[state]

    @classmethod
    def can_transition(
        cls,
        source: AgentRunState,
        target: AgentRunState,
    ) -> bool:
        if not isinstance(target, AgentRunState):
            raise ValueError("AgentStateMachine.target 必须是 AgentRunState")
        return target in cls.allowed_targets(source)

    @classmethod
    def transition(
        cls,
        run: AgentRun,
        target: AgentRunState,
        *,
        finished_at: float | None = None,
    ) -> AgentRun:
        """Return the next immutable run without reading a clock or live state."""

        if not isinstance(run, AgentRun):
            raise ValueError("AgentStateMachine.run 必须是 AgentRun")
        if not isinstance(target, AgentRunState):
            raise ValueError("AgentStateMachine.target 必须是 AgentRunState")
        if not cls.can_transition(run.state, target):
            raise ValueError(f"AgentRun 不允许从 {run.state.value} 转换到 {target.value}")
        if target in _TERMINAL_RUN_STATES:
            if finished_at is None:
                raise ValueError("AgentRun 进入终态必须提供 finished_at")
        elif finished_at is not None:
            raise ValueError("AgentRun 进入非终态不得提供 finished_at")
        return replace(run, state=target, finished_at=finished_at)


@dataclass(frozen=True)
class DeadlineContext:
    """One shared, monotonic deadline for an entire Agent request."""

    deadline_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "deadline_at",
            _normalize_timestamp(
                self.deadline_at,
                owner="DeadlineContext",
                label="deadline_at",
            ),
        )

    @staticmethod
    def _now(value: float | None) -> float:
        current = time.monotonic() if value is None else value
        return _normalize_timestamp(
            current,
            owner="DeadlineContext",
            label="now",
        )

    @classmethod
    def from_timeout(
        cls,
        timeout: float,
        *,
        now: float | None = None,
    ) -> DeadlineContext:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("DeadlineContext.timeout 必须是秒数")
        duration = float(timeout)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("DeadlineContext.timeout 必须是有限非负秒数")
        return cls(cls._now(now) + duration)

    def remaining(self, *, now: float | None = None) -> float:
        """Return the shared remaining budget, clamped at zero."""

        return max(0.0, self.deadline_at - self._now(now))


@dataclass(frozen=True)
class AgentStep:
    """One detached, serializable step belonging to an AgentRun."""

    step_id: str
    run_id: str
    index: int
    type: AgentStepType
    status: AgentStepStatus
    model: str | None = None
    tool: str | None = None
    input: AgentJsonValue = None
    output: AgentJsonValue = None
    input_preview: str | None = None
    output_preview: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        validate_agent_step_id(self.step_id)
        validate_agent_run_id(self.run_id)
        if not isinstance(self.index, int) or isinstance(self.index, bool) or not 0 <= self.index <= _POSTGRES_BIGINT_MAX:
            raise ValueError("AgentStep.index 必须是非负 PostgreSQL BIGINT")
        if not isinstance(self.type, AgentStepType):
            raise ValueError("AgentStep.type 必须是 AgentStepType")
        if not isinstance(self.status, AgentStepStatus):
            raise ValueError("AgentStep.status 必须是 AgentStepStatus")
        if self.model is not None:
            _require_optional_bounded_text(
                self.model,
                owner="AgentStep",
                label="model",
                maximum=_MODEL_NAME_LIMIT,
            )
        if self.tool is not None:
            if not isinstance(self.tool, str) or not _TOOL_NAME_RE.fullmatch(self.tool):
                raise ValueError("AgentStep.tool 必须是安全工具名")
        if self.type is AgentStepType.MODEL and self.model is None:
            raise ValueError("AgentStep model 类型必须绑定 model")
        if self.type is AgentStepType.TOOL and self.tool is None:
            raise ValueError("AgentStep tool 类型必须绑定 tool")

        frozen_input = _freeze_agent_json(self.input, label="AgentStep.input")
        frozen_output = _freeze_agent_json(self.output, label="AgentStep.output")
        input_preview = _require_optional_preview(
            self.input_preview,
            owner="AgentStep",
            label="input_preview",
        )
        output_preview = _require_optional_preview(
            self.output_preview,
            owner="AgentStep",
            label="output_preview",
        )
        error = _require_optional_preview(
            self.error,
            owner="AgentStep",
            label="error",
        )
        duration_ms = _require_optional_nonnegative_bigint(
            self.duration_ms,
            owner="AgentStep",
            label="duration_ms",
        )
        started_at = (
            None
            if self.started_at is None
            else _normalize_persisted_timestamp(
                self.started_at,
                owner="AgentStep",
                label="started_at",
            )
        )
        finished_at = (
            None
            if self.finished_at is None
            else _normalize_persisted_timestamp(
                self.finished_at,
                owner="AgentStep",
                label="finished_at",
            )
        )

        if self.status is AgentStepStatus.PENDING:
            if started_at is not None or finished_at is not None or duration_ms is not None:
                raise ValueError("AgentStep pending 状态不得包含起止时间或 duration_ms")
        elif self.status is AgentStepStatus.RUNNING:
            if started_at is None or finished_at is not None or duration_ms is not None:
                raise ValueError("AgentStep running 状态必须只有 started_at，且不得包含 duration_ms")
        else:
            if started_at is None or finished_at is None or duration_ms is None:
                raise ValueError("AgentStep 终态必须包含完整起止时间和 duration_ms")
            if finished_at < started_at:
                raise ValueError("AgentStep.finished_at 不能早于 started_at")
        if self.status not in _TERMINAL_STEP_STATUSES and (frozen_output is not None or output_preview is not None):
            raise ValueError("AgentStep 非终态不得包含 output 或 output_preview")
        if (
            self.status
            not in {
                AgentStepStatus.FAILED,
                AgentStepStatus.CANCELLED,
                AgentStepStatus.TIMED_OUT,
                AgentStepStatus.SKIPPED,
            }
            and error is not None
        ):
            raise ValueError("AgentStep 当前状态不得包含 error")

        object.__setattr__(self, "input", frozen_input)
        object.__setattr__(self, "output", frozen_output)
        object.__setattr__(self, "input_preview", input_preview)
        object.__setattr__(self, "output_preview", output_preview)
        object.__setattr__(self, "error", error)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "duration_ms", duration_ms)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STEP_STATUSES

    @property
    def elapsed(self) -> float | None:
        return None if self.duration_ms is None else self.duration_ms / 1000

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""

        return {
            "step_id": self.step_id,
            "run_id": self.run_id,
            "index": self.index,
            "type": self.type.value,
            "model": self.model,
            "tool": self.tool,
            "status": self.status.value,
            "input": mutable_agent_json(self.input),
            "output": mutable_agent_json(self.output),
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class ToolCall:
    """One immutable, serializable invocation linked to an AgentStep."""

    tool_call_id: str
    run_id: str
    step_id: str
    tool_name: str
    tool_source: ToolSource
    bundle_id: str | None
    bundle_digest: str | None
    arguments: Mapping[str, AgentJsonValue]
    status: ToolCallStatus
    confirmed: bool
    created_at: float
    confirmation_id: str | None = None
    result: AgentJsonValue = None
    result_preview: str | None = None
    duration_ms: int | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        validate_tool_call_id(self.tool_call_id)
        validate_agent_run_id(self.run_id)
        validate_agent_step_id(self.step_id)
        if not isinstance(self.tool_name, str) or not _TOOL_NAME_RE.fullmatch(self.tool_name):
            raise ValueError("ToolCall.tool_name 必须是安全工具名")
        if not isinstance(self.tool_source, ToolSource):
            raise ValueError("ToolCall.tool_source 必须是 ToolSource")
        if self.bundle_id is not None and (not isinstance(self.bundle_id, str) or not _BUNDLE_ID_RE.fullmatch(self.bundle_id)):
            raise ValueError("ToolCall.bundle_id 必须是安全 bundle 标识")
        if self.bundle_digest is not None and (
            not isinstance(self.bundle_digest, str) or not _DIGEST_RE.fullmatch(self.bundle_digest)
        ):
            raise ValueError("ToolCall.bundle_digest 必须是 64 位 SHA-256")
        if self.tool_source is ToolSource.GENERATED:
            if self.bundle_id is None or self.bundle_digest is None:
                raise ValueError("ToolCall generated 来源必须绑定 bundle_id 与 bundle_digest")
        elif self.bundle_id is not None or self.bundle_digest is not None:
            raise ValueError("ToolCall 非 generated 来源不得伪造 bundle identity")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("ToolCall.arguments 必须是 JSON 对象")
        frozen_arguments = _freeze_agent_json(
            self.arguments,
            label="ToolCall.arguments",
        )
        if not isinstance(frozen_arguments, Mapping):
            raise ValueError("ToolCall.arguments 必须是 JSON 对象")
        if _canonical_agent_json_size(frozen_arguments) > _TOOL_ARGUMENTS_MAX_BYTES:
            raise ValueError("ToolCall.arguments 超过持久化安全上限")
        if not isinstance(self.status, ToolCallStatus):
            raise ValueError("ToolCall.status 必须是 ToolCallStatus")
        if type(self.confirmed) is not bool:
            raise ValueError("ToolCall.confirmed 必须是布尔值")
        confirmation_id = (
            None
            if self.confirmation_id is None
            else _require_entity_id(
                self.confirmation_id,
                owner="ToolCall",
                label="confirmation_id",
            )
        )
        if self.confirmed and confirmation_id is None:
            raise ValueError("ToolCall confirmed=true 必须绑定 confirmation_id")
        if self.status is ToolCallStatus.WAITING_CONFIRMATION and (self.confirmed or confirmation_id is None):
            raise ValueError("ToolCall 等待确认时 confirmed 必须为 false 且必须绑定 confirmation_id")

        frozen_result = _freeze_agent_json(self.result, label="ToolCall.result")
        result_preview = _require_optional_preview(
            self.result_preview,
            owner="ToolCall",
            label="result_preview",
        )
        created_at = _normalize_persisted_timestamp(
            self.created_at,
            owner="ToolCall",
            label="created_at",
        )
        finished_at = (
            None
            if self.finished_at is None
            else _normalize_persisted_timestamp(
                self.finished_at,
                owner="ToolCall",
                label="finished_at",
            )
        )
        duration_ms = _require_optional_nonnegative_bigint(
            self.duration_ms,
            owner="ToolCall",
            label="duration_ms",
        )
        if self.status in _TERMINAL_TOOL_CALL_STATUSES:
            if finished_at is None or duration_ms is None:
                raise ValueError("ToolCall 终态必须包含 finished_at 与 duration_ms")
            if finished_at < created_at:
                raise ValueError("ToolCall.finished_at 不能早于 created_at")
            if self.status is ToolCallStatus.COMPLETED and result_preview is None:
                raise ValueError("ToolCall completed 状态必须包含 result_preview")
        elif frozen_result is not None or result_preview is not None or finished_at is not None or duration_ms is not None:
            raise ValueError("ToolCall 非终态不得包含 result、result_preview、finished_at 或 duration_ms")

        object.__setattr__(self, "arguments", frozen_arguments)
        object.__setattr__(self, "confirmation_id", confirmation_id)
        object.__setattr__(self, "result", frozen_result)
        object.__setattr__(self, "result_preview", result_preview)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "duration_ms", duration_ms)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_TOOL_CALL_STATUSES

    @property
    def elapsed(self) -> float | None:
        return None if self.duration_ms is None else self.duration_ms / 1000

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""

        return {
            "tool_call_id": self.tool_call_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "tool_source": self.tool_source.value,
            "bundle_id": self.bundle_id,
            "bundle_digest": self.bundle_digest,
            "arguments": mutable_agent_json(self.arguments),
            "status": self.status.value,
            "confirmed": self.confirmed,
            "confirmation_id": self.confirmation_id,
            "result": mutable_agent_json(self.result),
            "result_preview": self.result_preview,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "elapsed": self.elapsed,
        }

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import math
import re
from types import MappingProxyType
from typing import Any, TypeAlias

_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SUBJECT_ID_LIMIT = 128
_AGENT_JSON_MAX_DEPTH = 32

AgentJsonValue: TypeAlias = (
    bool
    | int
    | float
    | str
    | Mapping[str, "AgentJsonValue"]
    | list["AgentJsonValue"]
    | tuple["AgentJsonValue", ...]
    | None
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


_TERMINAL_RUN_STATES = frozenset(
    {
        AgentRunState.COMPLETED,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
        AgentRunState.TIMED_OUT,
        AgentRunState.REJECTED,
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


def _require_subject_id(value: str, *, owner: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner}.{label} 必须是字符串")
    if not value or value != value.strip():
        raise ValueError(f"{owner}.{label} 必须是非空且无首尾空白的字符串")
    if len(value) > _SUBJECT_ID_LIMIT or _CONTROL_CHARACTER_RE.search(value):
        raise ValueError(f"{owner}.{label} 包含非法字符或长度超限")
    return value


def _normalize_timestamp(value: float, *, owner: str, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{owner}.{label} 必须是时间戳")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{owner}.{label} 必须是有限非负时间戳")
    return normalized


def _normalize_elapsed(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("ToolCall.elapsed 必须是秒数")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("ToolCall.elapsed 必须是有限非负秒数")
    return normalized


def _freeze_agent_json(
    value: AgentJsonValue,
    *,
    label: str,
    depth: int = 0,
    active_containers: set[int] | None = None,
) -> AgentJsonValue:
    if depth > _AGENT_JSON_MAX_DEPTH:
        raise ValueError(f"{label} JSON 嵌套超过安全上限")
    if value is None or isinstance(value, (bool, str, int)):
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
                if not isinstance(key, str):
                    raise ValueError(f"{label} JSON 对象键必须是字符串")
                frozen[key] = _freeze_agent_json(
                    item,
                    label=label,
                    depth=depth + 1,
                    active_containers=active,
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
                )
                for item in value
            )
        finally:
            active.remove(identity)

    raise ValueError(f"{label} 必须是 JSON 兼容值")


def _mutable_agent_json(value: AgentJsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_agent_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_agent_json(item) for item in value]
    return value


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
    generation: int
    state: AgentRunState
    started_at: float
    finished_at: float | None = None

    def __post_init__(self) -> None:
        _require_entity_id(self.run_id, owner="AgentRun", label="run_id")
        if (
            not isinstance(self.request_id, int)
            or isinstance(self.request_id, bool)
            or self.request_id <= 0
        ):
            raise ValueError("AgentRun.request_id 必须是正整数")
        _require_subject_id(self.user_id, owner="AgentRun", label="user_id")
        if self.group_id is not None:
            _require_subject_id(
                self.group_id,
                owner="AgentRun",
                label="group_id",
            )
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("AgentRun.generation 必须是非负整数")
        if not isinstance(self.state, AgentRunState):
            raise ValueError("AgentRun.state 必须是 AgentRunState")

        started_at = _normalize_timestamp(
            self.started_at,
            owner="AgentRun",
            label="started_at",
        )
        finished_at = (
            None
            if self.finished_at is None
            else _normalize_timestamp(
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
            "generation": self.generation,
            "state": self.state.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


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
    started_at: float | None = None
    finished_at: float | None = None

    def __post_init__(self) -> None:
        _require_entity_id(self.step_id, owner="AgentStep", label="step_id")
        _require_entity_id(self.run_id, owner="AgentStep", label="run_id")
        if (
            not isinstance(self.index, int)
            or isinstance(self.index, bool)
            or self.index < 0
        ):
            raise ValueError("AgentStep.index 必须是非负整数")
        if not isinstance(self.type, AgentStepType):
            raise ValueError("AgentStep.type 必须是 AgentStepType")
        if not isinstance(self.status, AgentStepStatus):
            raise ValueError("AgentStep.status 必须是 AgentStepStatus")
        if self.model is not None:
            _require_subject_id(self.model, owner="AgentStep", label="model")
        if self.tool is not None:
            _require_subject_id(self.tool, owner="AgentStep", label="tool")
        if self.type is AgentStepType.MODEL and self.model is None:
            raise ValueError("AgentStep model 类型必须绑定 model")
        if self.type is AgentStepType.TOOL and self.tool is None:
            raise ValueError("AgentStep tool 类型必须绑定 tool")

        frozen_input = _freeze_agent_json(self.input, label="AgentStep.input")
        frozen_output = _freeze_agent_json(self.output, label="AgentStep.output")
        started_at = (
            None
            if self.started_at is None
            else _normalize_timestamp(
                self.started_at,
                owner="AgentStep",
                label="started_at",
            )
        )
        finished_at = (
            None
            if self.finished_at is None
            else _normalize_timestamp(
                self.finished_at,
                owner="AgentStep",
                label="finished_at",
            )
        )

        if self.status is AgentStepStatus.PENDING:
            if started_at is not None or finished_at is not None:
                raise ValueError("AgentStep pending 状态不得包含起止时间")
        elif self.status is AgentStepStatus.RUNNING:
            if started_at is None or finished_at is not None:
                raise ValueError("AgentStep running 状态必须只有 started_at")
        else:
            if started_at is None or finished_at is None:
                raise ValueError("AgentStep 终态必须包含完整起止时间")
            if finished_at < started_at:
                raise ValueError("AgentStep.finished_at 不能早于 started_at")
        if self.status not in _TERMINAL_STEP_STATUSES and frozen_output is not None:
            raise ValueError("AgentStep 非终态不得包含 output")

        object.__setattr__(self, "input", frozen_input)
        object.__setattr__(self, "output", frozen_output)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STEP_STATUSES

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at

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
            "input": _mutable_agent_json(self.input),
            "output": _mutable_agent_json(self.output),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class ToolCall:
    """One immutable, serializable invocation linked to an AgentStep."""

    tool_call_id: str
    run_id: str
    step_id: str
    tool_name: str
    bundle_digest: str | None
    arguments: Mapping[str, AgentJsonValue]
    status: ToolCallStatus
    confirmed: bool
    result: AgentJsonValue = None
    elapsed: float | None = None

    def __post_init__(self) -> None:
        _require_entity_id(
            self.tool_call_id,
            owner="ToolCall",
            label="tool_call_id",
        )
        _require_entity_id(self.run_id, owner="ToolCall", label="run_id")
        _require_entity_id(self.step_id, owner="ToolCall", label="step_id")
        if not isinstance(self.tool_name, str) or not _TOOL_NAME_RE.fullmatch(
            self.tool_name
        ):
            raise ValueError("ToolCall.tool_name 必须是安全工具名")
        if self.bundle_digest is not None and (
            not isinstance(self.bundle_digest, str)
            or not _DIGEST_RE.fullmatch(self.bundle_digest)
        ):
            raise ValueError("ToolCall.bundle_digest 必须是 64 位 SHA-256")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("ToolCall.arguments 必须是 JSON 对象")
        frozen_arguments = _freeze_agent_json(
            self.arguments,
            label="ToolCall.arguments",
        )
        if not isinstance(frozen_arguments, Mapping):
            raise ValueError("ToolCall.arguments 必须是 JSON 对象")
        if not isinstance(self.status, ToolCallStatus):
            raise ValueError("ToolCall.status 必须是 ToolCallStatus")
        if type(self.confirmed) is not bool:
            raise ValueError("ToolCall.confirmed 必须是布尔值")
        if (
            self.status is ToolCallStatus.WAITING_CONFIRMATION
            and self.confirmed
        ):
            raise ValueError("ToolCall 等待确认时 confirmed 必须为 false")

        frozen_result = _freeze_agent_json(self.result, label="ToolCall.result")
        elapsed = (
            None if self.elapsed is None else _normalize_elapsed(self.elapsed)
        )
        if self.status in _TERMINAL_TOOL_CALL_STATUSES:
            if elapsed is None:
                raise ValueError("ToolCall 终态必须包含 elapsed")
            if self.status is ToolCallStatus.COMPLETED and frozen_result is None:
                raise ValueError("ToolCall completed 状态必须包含 result")
        elif frozen_result is not None or elapsed is not None:
            raise ValueError("ToolCall 非终态不得包含 result 或 elapsed")

        object.__setattr__(self, "arguments", frozen_arguments)
        object.__setattr__(self, "result", frozen_result)
        object.__setattr__(self, "elapsed", elapsed)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_TOOL_CALL_STATUSES

    def as_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible representation."""

        return {
            "tool_call_id": self.tool_call_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "tool_name": self.tool_name,
            "bundle_digest": self.bundle_digest,
            "arguments": _mutable_agent_json(self.arguments),
            "status": self.status.value,
            "confirmed": self.confirmed,
            "result": _mutable_agent_json(self.result),
            "elapsed": self.elapsed,
        }

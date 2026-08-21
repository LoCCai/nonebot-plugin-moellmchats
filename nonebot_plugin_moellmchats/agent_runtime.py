from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Any

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SUBJECT_ID_LIMIT = 128


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


_TERMINAL_RUN_STATES = frozenset(
    {
        AgentRunState.COMPLETED,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
        AgentRunState.TIMED_OUT,
        AgentRunState.REJECTED,
    }
)


def _require_subject_id(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"AgentRun.{label} 必须是字符串")
    if not value or value != value.strip():
        raise ValueError(f"AgentRun.{label} 必须是非空且无首尾空白的字符串")
    if len(value) > _SUBJECT_ID_LIMIT or _CONTROL_CHARACTER_RE.search(value):
        raise ValueError(f"AgentRun.{label} 包含非法字符或长度超限")
    return value


def _normalize_timestamp(value: float, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"AgentRun.{label} 必须是时间戳")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"AgentRun.{label} 必须是有限非负时间戳")
    return normalized


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
        if not isinstance(self.run_id, str) or not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("AgentRun.run_id 必须是安全的非空标识")
        if (
            not isinstance(self.request_id, int)
            or isinstance(self.request_id, bool)
            or self.request_id <= 0
        ):
            raise ValueError("AgentRun.request_id 必须是正整数")
        _require_subject_id(self.user_id, label="user_id")
        if self.group_id is not None:
            _require_subject_id(self.group_id, label="group_id")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("AgentRun.generation 必须是非负整数")
        if not isinstance(self.state, AgentRunState):
            raise ValueError("AgentRun.state 必须是 AgentRunState")

        started_at = _normalize_timestamp(self.started_at, label="started_at")
        finished_at = (
            None
            if self.finished_at is None
            else _normalize_timestamp(self.finished_at, label="finished_at")
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

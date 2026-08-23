from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import math
import os
import time

from .model_usage import MAX_MODEL_USAGE_BATCH_SIZE, ModelUsageRecord

DEFAULT_USAGE_BATCH_SIZE = 100
DEFAULT_USAGE_FLUSH_INTERVAL_SECONDS = 1.0
DEFAULT_USAGE_MAX_OUTSTANDING_RECORDS = 1_000


class UsageBatchError(RuntimeError):
    """Base error for the backend-neutral usage batching primitive."""


class UsageBatchClosedError(UsageBatchError):
    """The queue no longer accepts new records."""


class UsageBatchResultUnknownError(UsageBatchError):
    """A leased write has an unknown durable outcome and must not be replayed."""


class UsageBatchOwnershipError(UsageBatchError):
    """The queue was accessed from a different process or event loop."""


class UsageBatchClockError(UsageBatchError):
    """The monotonic clock is invalid or moved backwards."""


class UsageBatchQueueState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True)
class UsageBatchPolicy:
    max_batch_size: int = DEFAULT_USAGE_BATCH_SIZE
    flush_interval_seconds: float = DEFAULT_USAGE_FLUSH_INTERVAL_SECONDS
    max_outstanding_records: int = DEFAULT_USAGE_MAX_OUTSTANDING_RECORDS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_batch_size, int)
            or isinstance(self.max_batch_size, bool)
            or not 1 <= self.max_batch_size <= MAX_MODEL_USAGE_BATCH_SIZE
        ):
            raise ValueError(f"max_batch_size 必须是 1 到 {MAX_MODEL_USAGE_BATCH_SIZE} 的整数")
        if (
            isinstance(self.flush_interval_seconds, bool)
            or not isinstance(self.flush_interval_seconds, (int, float))
            or not math.isfinite(float(self.flush_interval_seconds))
            or not 0.001 <= float(self.flush_interval_seconds) <= 60.0
        ):
            raise ValueError("flush_interval_seconds 必须是 0.001 到 60 秒的有限数值")
        if (
            not isinstance(self.max_outstanding_records, int)
            or isinstance(self.max_outstanding_records, bool)
            or not self.max_batch_size <= self.max_outstanding_records <= 100_000
        ):
            raise ValueError("max_outstanding_records 必须不小于 batch size 且不超过 100000")
        object.__setattr__(
            self,
            "flush_interval_seconds",
            float(self.flush_interval_seconds),
        )


@dataclass(frozen=True)
class UsageBatchLease:
    sequence: int
    records: tuple[ModelUsageRecord, ...] = field(repr=False)
    oldest_enqueued_at: float
    leased_at: float
    _owner_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence <= 0:
            raise ValueError("UsageBatchLease.sequence 必须是正整数")
        if (
            not isinstance(self.records, tuple)
            or not self.records
            or len(self.records) > MAX_MODEL_USAGE_BATCH_SIZE
            or any(not isinstance(record, ModelUsageRecord) or record.persisted for record in self.records)
        ):
            raise ValueError("UsageBatchLease.records 必须是有界的 draft usage 元组")
        for label, value in (
            ("oldest_enqueued_at", self.oldest_enqueued_at),
            ("leased_at", self.leased_at),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"UsageBatchLease.{label} 必须是有限非负时间戳")
        if self.leased_at < self.oldest_enqueued_at:
            raise ValueError("UsageBatchLease 时间顺序无效")

    def safe_diagnostics(self) -> dict[str, int | float]:
        return {
            "sequence": self.sequence,
            "record_count": len(self.records),
            "oldest_enqueued_at": self.oldest_enqueued_at,
            "leased_at": self.leased_at,
        }


class UsageBatchQueue:
    """Bounded lease queue; only a confirmed durable commit may be acknowledged.

    The queue deliberately performs no database I/O and owns no background task.
    A caller leases a ready batch, writes and commits it in an explicit durable
    transaction, then acknowledges that exact lease. An unknown commit result is
    terminal for the queue instance so the batch cannot be replayed implicitly.
    """

    def __init__(
        self,
        policy: UsageBatchPolicy = UsageBatchPolicy(),
        *,
        monotonic: Callable[[], float] = time.monotonic,
        pid_getter: Callable[[], int] = os.getpid,
    ) -> None:
        if not isinstance(policy, UsageBatchPolicy):
            raise TypeError("policy 必须是 UsageBatchPolicy")
        if not callable(monotonic) or not callable(pid_getter):
            raise TypeError("clock 与 pid_getter 必须可调用")
        self._policy = policy
        self._monotonic = monotonic
        self._pid_getter = pid_getter
        self._pending: deque[tuple[ModelUsageRecord, float]] = deque()
        self._leased_entries: tuple[tuple[ModelUsageRecord, float], ...] = ()
        self._lease: UsageBatchLease | None = None
        self._lease_owner_token = object()
        self._next_sequence = 1
        self._state = UsageBatchQueueState.OPEN
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._condition: asyncio.Condition | None = None
        self._last_clock: float | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(state={self._state.value!r}, pending={len(self._pending)}, leased={self._lease is not None})"
        )

    @property
    def policy(self) -> UsageBatchPolicy:
        return self._policy

    @property
    def state(self) -> UsageBatchQueueState:
        return self._state

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def outstanding_count(self) -> int:
        leased_count = 0 if self._lease is None else len(self._lease.records)
        return len(self._pending) + leased_count

    @property
    def active_lease(self) -> UsageBatchLease | None:
        return self._lease

    def safe_diagnostics(self) -> dict[str, int | str | bool]:
        return {
            "state": self._state.value,
            "pending_count": self.pending_count,
            "outstanding_count": self.outstanding_count,
            "has_active_lease": self._lease is not None,
        }

    def _bind_owner(self) -> asyncio.Condition:
        current_pid = self._pid_getter()
        if not isinstance(current_pid, int) or isinstance(current_pid, bool) or current_pid <= 0:
            raise UsageBatchOwnershipError("pid_getter 返回了非法进程标识")
        current_loop = asyncio.get_running_loop()
        if self._owner_pid is None:
            self._owner_pid = current_pid
            self._owner_loop = current_loop
            self._condition = asyncio.Condition()
        elif self._owner_pid != current_pid or self._owner_loop is not current_loop:
            raise UsageBatchOwnershipError("UsageBatchQueue 不得跨进程或 event loop 复用")
        if self._condition is None:
            raise UsageBatchOwnershipError("UsageBatchQueue owner 状态损坏")
        return self._condition

    def _now(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise UsageBatchClockError("monotonic clock 返回了非法时间")
        normalized = float(value)
        if self._last_clock is not None and normalized < self._last_clock:
            raise UsageBatchClockError("monotonic clock 发生回退")
        self._last_clock = normalized
        return normalized

    def _raise_if_unavailable_for_put(self) -> None:
        if self._state is UsageBatchQueueState.RESULT_UNKNOWN:
            raise UsageBatchResultUnknownError("usage batch durable result unknown")
        if self._state is not UsageBatchQueueState.OPEN:
            raise UsageBatchClosedError("usage batch queue 已停止接收新记录")

    def _raise_if_result_unknown(self) -> None:
        if self._state is UsageBatchQueueState.RESULT_UNKNOWN:
            raise UsageBatchResultUnknownError("usage batch durable result unknown")

    def _ready(self, now: float, *, force: bool) -> bool:
        if not self._pending or self._lease is not None:
            return False
        if force or self._state is UsageBatchQueueState.CLOSING:
            return True
        if len(self._pending) >= self._policy.max_batch_size:
            return True
        return now - self._pending[0][1] >= self._policy.flush_interval_seconds

    def _lease_ready_locked(
        self,
        now: float,
        *,
        force: bool,
    ) -> UsageBatchLease | None:
        if not self._ready(now, force=force):
            return None
        count = min(self._policy.max_batch_size, len(self._pending))
        entries = tuple(self._pending.popleft() for _ in range(count))
        lease = UsageBatchLease(
            sequence=self._next_sequence,
            records=tuple(record for record, _ in entries),
            oldest_enqueued_at=entries[0][1],
            leased_at=now,
            _owner_token=self._lease_owner_token,
        )
        self._next_sequence += 1
        self._leased_entries = entries
        self._lease = lease
        return lease

    def _require_active_lease(self, lease: UsageBatchLease) -> None:
        if not isinstance(lease, UsageBatchLease) or lease is not self._lease:
            raise ValueError("lease 不是当前队列的 active lease")
        if lease._owner_token is not self._lease_owner_token:
            raise ValueError("lease owner 不匹配")

    def _finish_close_if_empty(self) -> None:
        if self._state is UsageBatchQueueState.CLOSING and not self._pending and self._lease is None:
            self._state = UsageBatchQueueState.CLOSED

    async def put(self, record: ModelUsageRecord) -> None:
        if not isinstance(record, ModelUsageRecord) or record.persisted:
            raise ValueError("record 必须是未持久化的 ModelUsageRecord")
        condition = self._bind_owner()
        async with condition:
            self._raise_if_unavailable_for_put()
            while self.outstanding_count >= self._policy.max_outstanding_records:
                await condition.wait()
                self._raise_if_unavailable_for_put()
            enqueued_at = self._now()
            self._pending.append((record, enqueued_at))
            condition.notify_all()

    async def lease_ready(self, *, force: bool = False) -> UsageBatchLease | None:
        if type(force) is not bool:
            raise ValueError("force 必须是 bool")
        condition = self._bind_owner()
        async with condition:
            self._raise_if_result_unknown()
            lease = self._lease_ready_locked(self._now(), force=force)
            self._finish_close_if_empty()
            if lease is not None:
                condition.notify_all()
            return lease

    async def wait_for_lease(self) -> UsageBatchLease | None:
        """Wait for size/time/close readiness; return ``None`` once fully closed."""

        condition = self._bind_owner()
        async with condition:
            while True:
                self._raise_if_result_unknown()
                now = self._now()
                lease = self._lease_ready_locked(now, force=False)
                if lease is not None:
                    condition.notify_all()
                    return lease
                self._finish_close_if_empty()
                if self._state is UsageBatchQueueState.CLOSED:
                    condition.notify_all()
                    return None

                timeout: float | None = None
                if self._lease is None and self._pending:
                    due_at = self._pending[0][1] + self._policy.flush_interval_seconds
                    timeout = max(0.0, due_at - now)
                try:
                    if timeout is None:
                        await condition.wait()
                    else:
                        await asyncio.wait_for(condition.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue

    async def acknowledge_committed(self, lease: UsageBatchLease) -> None:
        condition = self._bind_owner()
        async with condition:
            self._raise_if_result_unknown()
            self._require_active_lease(lease)
            self._lease = None
            self._leased_entries = ()
            self._finish_close_if_empty()
            condition.notify_all()

    async def release_unwritten(self, lease: UsageBatchLease) -> None:
        """Requeue only after no write occurred or rollback was definitive."""

        condition = self._bind_owner()
        async with condition:
            self._raise_if_result_unknown()
            self._require_active_lease(lease)
            for entry in reversed(self._leased_entries):
                self._pending.appendleft(entry)
            self._lease = None
            self._leased_entries = ()
            condition.notify_all()

    async def mark_result_unknown(self, lease: UsageBatchLease) -> None:
        condition = self._bind_owner()
        async with condition:
            self._require_active_lease(lease)
            self._state = UsageBatchQueueState.RESULT_UNKNOWN
            condition.notify_all()

    async def begin_close(self) -> None:
        condition = self._bind_owner()
        async with condition:
            self._raise_if_result_unknown()
            if self._state is UsageBatchQueueState.OPEN:
                self._state = UsageBatchQueueState.CLOSING
            self._finish_close_if_empty()
            condition.notify_all()

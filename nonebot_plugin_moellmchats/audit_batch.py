from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
import math
import os
import time

from .audit_event import MAX_AUDIT_BATCH_SIZE, AuditEventRecord

DEFAULT_AUDIT_BATCH_SIZE = 100
DEFAULT_AUDIT_FLUSH_INTERVAL_SECONDS = 1.0
DEFAULT_AUDIT_MAX_OUTSTANDING_RECORDS = 1_000


class AuditBatchError(RuntimeError):
    """Base error for the backend-neutral non-critical audit queue."""


class AuditBatchClosedError(AuditBatchError):
    """The queue no longer accepts new records."""


class AuditBatchResultUnknownError(AuditBatchError):
    """A leased write has an unknown durable outcome and must not be replayed."""


class AuditBatchOwnershipError(AuditBatchError):
    """The queue was accessed from a different process or event loop."""


class AuditBatchClockError(AuditBatchError):
    """The monotonic clock is invalid or moved backwards."""


class AuditBatchQueueState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True)
class AuditBatchPolicy:
    max_batch_size: int = DEFAULT_AUDIT_BATCH_SIZE
    flush_interval_seconds: float = DEFAULT_AUDIT_FLUSH_INTERVAL_SECONDS
    max_outstanding_records: int = DEFAULT_AUDIT_MAX_OUTSTANDING_RECORDS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_batch_size, int)
            or isinstance(self.max_batch_size, bool)
            or not 1 <= self.max_batch_size <= MAX_AUDIT_BATCH_SIZE
        ):
            raise ValueError(f"max_batch_size 必须是 1 到 {MAX_AUDIT_BATCH_SIZE} 的整数")
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
class AuditBatchLease:
    sequence: int
    records: tuple[AuditEventRecord, ...] = field(repr=False)
    oldest_enqueued_at: float
    leased_at: float
    _owner_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence <= 0:
            raise ValueError("AuditBatchLease.sequence 必须是正整数")
        if (
            not isinstance(self.records, tuple)
            or not self.records
            or len(self.records) > MAX_AUDIT_BATCH_SIZE
            or any(
                not isinstance(record, AuditEventRecord) or record.persisted or not record.batchable for record in self.records
            )
        ):
            raise ValueError("AuditBatchLease.records 必须是有界的非关键 draft audit 元组")
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
                raise ValueError(f"AuditBatchLease.{label} 必须是有限非负时间戳")
        if self.leased_at < self.oldest_enqueued_at:
            raise ValueError("AuditBatchLease 时间顺序无效")

    def safe_diagnostics(self) -> dict[str, int | float]:
        return {
            "sequence": self.sequence,
            "record_count": len(self.records),
            "oldest_enqueued_at": self.oldest_enqueued_at,
            "leased_at": self.leased_at,
        }


class AuditBatchQueue:
    """Bounded lease queue for explicitly non-critical audit events only.

    Safety events never enter this queue. The caller writes and commits a leased
    batch in an explicit durable transaction, then acknowledges that exact lease.
    An unknown commit result is terminal because ``audit_events`` has no producer
    idempotency key and implicit replay could create duplicate evidence.
    """

    def __init__(
        self,
        policy: AuditBatchPolicy = AuditBatchPolicy(),
        *,
        monotonic: Callable[[], float] = time.monotonic,
        pid_getter: Callable[[], int] = os.getpid,
    ) -> None:
        if not isinstance(policy, AuditBatchPolicy):
            raise TypeError("policy 必须是 AuditBatchPolicy")
        if not callable(monotonic) or not callable(pid_getter):
            raise TypeError("clock 与 pid_getter 必须可调用")
        self._policy = policy
        self._monotonic = monotonic
        self._pid_getter = pid_getter
        self._pending: deque[tuple[AuditEventRecord, float]] = deque()
        self._leased_entries: tuple[tuple[AuditEventRecord, float], ...] = ()
        self._lease: AuditBatchLease | None = None
        self._lease_owner_token = object()
        self._next_sequence = 1
        self._state = AuditBatchQueueState.OPEN
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._condition: asyncio.Condition | None = None
        self._last_clock: float | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(state={self._state.value!r}, pending={len(self._pending)}, leased={self._lease is not None})"
        )

    @property
    def policy(self) -> AuditBatchPolicy:
        return self._policy

    @property
    def state(self) -> AuditBatchQueueState:
        return self._state

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def outstanding_count(self) -> int:
        leased_count = 0 if self._lease is None else len(self._lease.records)
        return len(self._pending) + leased_count

    @property
    def active_lease(self) -> AuditBatchLease | None:
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
            raise AuditBatchOwnershipError("pid_getter 返回了非法进程标识")
        current_loop = asyncio.get_running_loop()
        if self._owner_pid is None:
            self._owner_pid = current_pid
            self._owner_loop = current_loop
            self._condition = asyncio.Condition()
        elif self._owner_pid != current_pid or self._owner_loop is not current_loop:
            raise AuditBatchOwnershipError("AuditBatchQueue 不得跨进程或 event loop 复用")
        if self._condition is None:
            raise AuditBatchOwnershipError("AuditBatchQueue owner 状态损坏")
        return self._condition

    def _now(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise AuditBatchClockError("monotonic clock 返回了非法时间")
        normalized = float(value)
        if self._last_clock is not None and normalized < self._last_clock:
            raise AuditBatchClockError("monotonic clock 发生回退")
        self._last_clock = normalized
        return normalized

    def _raise_if_unavailable_for_put(self) -> None:
        if self._state is AuditBatchQueueState.RESULT_UNKNOWN:
            raise AuditBatchResultUnknownError("audit batch durable result unknown")
        if self._state is not AuditBatchQueueState.OPEN:
            raise AuditBatchClosedError("audit batch queue 已停止接收新记录")

    def _raise_if_result_unknown(self) -> None:
        if self._state is AuditBatchQueueState.RESULT_UNKNOWN:
            raise AuditBatchResultUnknownError("audit batch durable result unknown")

    def _ready(self, now: float, *, force: bool) -> bool:
        if not self._pending or self._lease is not None:
            return False
        if force or self._state is AuditBatchQueueState.CLOSING:
            return True
        if len(self._pending) >= self._policy.max_batch_size:
            return True
        return now - self._pending[0][1] >= self._policy.flush_interval_seconds

    def _lease_ready_locked(
        self,
        now: float,
        *,
        force: bool,
    ) -> AuditBatchLease | None:
        if not self._ready(now, force=force):
            return None
        count = min(self._policy.max_batch_size, len(self._pending))
        entries = tuple(self._pending.popleft() for _ in range(count))
        lease = AuditBatchLease(
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

    def _require_active_lease(self, lease: AuditBatchLease) -> None:
        if not isinstance(lease, AuditBatchLease) or lease is not self._lease:
            raise ValueError("lease 不是当前队列的 active lease")
        if lease._owner_token is not self._lease_owner_token:
            raise ValueError("lease owner 不匹配")

    def _finish_close_if_empty(self) -> None:
        if self._state is AuditBatchQueueState.CLOSING and not self._pending and self._lease is None:
            self._state = AuditBatchQueueState.CLOSED

    async def put(self, record: AuditEventRecord) -> None:
        if not isinstance(record, AuditEventRecord) or record.persisted:
            raise ValueError("record 必须是未持久化的 AuditEventRecord")
        if not record.batchable:
            raise ValueError("安全或未知 audit event 必须走即时 append，不得进入 batch queue")
        condition = self._bind_owner()
        async with condition:
            self._raise_if_unavailable_for_put()
            while self.outstanding_count >= self._policy.max_outstanding_records:
                await condition.wait()
                self._raise_if_unavailable_for_put()
            enqueued_at = self._now()
            self._pending.append((record, enqueued_at))
            condition.notify_all()

    async def lease_ready(self, *, force: bool = False) -> AuditBatchLease | None:
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

    async def wait_for_lease(self) -> AuditBatchLease | None:
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
                if self._state is AuditBatchQueueState.CLOSED:
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

    async def acknowledge_committed(self, lease: AuditBatchLease) -> None:
        condition = self._bind_owner()
        async with condition:
            self._raise_if_result_unknown()
            self._require_active_lease(lease)
            self._lease = None
            self._leased_entries = ()
            self._finish_close_if_empty()
            condition.notify_all()

    async def release_unwritten(self, lease: AuditBatchLease) -> None:
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

    async def mark_result_unknown(self, lease: AuditBatchLease) -> None:
        condition = self._bind_owner()
        async with condition:
            self._require_active_lease(lease)
            self._state = AuditBatchQueueState.RESULT_UNKNOWN
            condition.notify_all()

    async def begin_close(self) -> None:
        condition = self._bind_owner()
        async with condition:
            self._raise_if_result_unknown()
            if self._state is AuditBatchQueueState.OPEN:
                self._state = AuditBatchQueueState.CLOSING
            self._finish_close_if_empty()
            condition.notify_all()

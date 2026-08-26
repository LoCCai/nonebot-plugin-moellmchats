from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import inspect
import json
import math
import os
from threading import Lock
from typing import Protocol, runtime_checkable

from . import full_metrics as _full_metrics
from .full_metrics import (
    FULL_METRICS_DURATION_BUCKETS_SECONDS,
    FULL_METRICS_MAX_COUNTER,
    FULL_METRICS_MAX_DURATION_SECONDS,
)

PLATFORM_METRICS_VERSION = 1
PLATFORM_METRICS_MAX_JSON_BYTES = 65_536

_MAX_DURATION_TOTAL_SECONDS = FULL_METRICS_MAX_DURATION_SECONDS * FULL_METRICS_MAX_COUNTER


class PlatformMetricsError(RuntimeError):
    """Base error for generation-bound platform metrics."""


class PlatformMetricsOwnershipError(PlatformMetricsError):
    """The platform registry was reused from another process."""


class PlatformMetricsOverflowError(PlatformMetricsError):
    """A fixed counter, gauge, or duration boundary would overflow."""


class PlatformDurationMetric(str, Enum):
    DATABASE_TRANSACTION_DURATION = "database_transaction_duration"
    DATABASE_POOL_WAIT_DURATION = "database_pool_wait_duration"
    SPOOL_FLUSH_DURATION = "spool_flush_duration"


class PlatformCountMetric(str, Enum):
    DATABASE_TRANSACTION_SUCCESS = "database_transaction_success"
    DATABASE_TRANSACTION_FAILURE = "database_transaction_failure"
    SPOOL_ENQUEUED_RECORDS = "spool_enqueued_records"
    SPOOL_COMMITTED_RECORDS = "spool_committed_records"
    SPOOL_FAILURE_TOTAL = "spool_failure_total"
    SPOOL_RESULT_UNKNOWN_TOTAL = "spool_result_unknown_total"
    STRUCTURED_LOG_FAILURE_TOTAL = "structured_log_failure_total"


class PlatformGaugeMetric(str, Enum):
    DATABASE_POOL_ACTIVE = "database_pool_active"
    DATABASE_POOL_PEAK = "database_pool_peak"
    SPOOL_READY_FILES = "spool_ready_files"
    SPOOL_LEASED_FILES = "spool_leased_files"
    SPOOL_RESULT_UNKNOWN_FILES = "spool_result_unknown_files"


def _require_generation(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= FULL_METRICS_MAX_COUNTER:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT")
    return value


def _require_nonnegative(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= FULL_METRICS_MAX_COUNTER:
        raise ValueError(f"{label} 必须是非负 PostgreSQL BIGINT")
    return value


def _require_positive(value: object, *, label: str) -> int:
    selected = _require_nonnegative(value, label=label)
    if selected == 0:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT")
    return selected


def _require_duration(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是有限非负秒数")
    selected = float(value)
    if not math.isfinite(selected) or not 0 <= selected <= FULL_METRICS_MAX_DURATION_SECONDS:
        raise ValueError(f"{label} 必须是有限非负秒数且不超过一天")
    return selected


@dataclass
class _DurationState:
    count: int
    total_seconds: float
    minimum_seconds: float | None
    maximum_seconds: float | None
    bucket_counts: list[int]

    @classmethod
    def empty(cls) -> _DurationState:
        return cls(
            count=0,
            total_seconds=0.0,
            minimum_seconds=None,
            maximum_seconds=None,
            bucket_counts=[0] * (len(FULL_METRICS_DURATION_BUCKETS_SECONDS) + 1),
        )

    def snapshot(self) -> _full_metrics.DurationMetricSnapshot:
        bounds = (*FULL_METRICS_DURATION_BUCKETS_SECONDS, None)
        return _full_metrics.DurationMetricSnapshot(
            count=self.count,
            total_seconds=self.total_seconds,
            minimum_seconds=self.minimum_seconds,
            maximum_seconds=self.maximum_seconds,
            buckets=tuple(
                _full_metrics.DurationBucketSnapshot(upper_bound_seconds=bound, count=count)
                for bound, count in zip(bounds, self.bucket_counts, strict=True)
            ),
        )


@dataclass(frozen=True)
class PlatformMetricsSnapshot:
    """Closed low-cardinality metrics for one runtime generation."""

    version: int
    generation: int
    full: _full_metrics.FullMetricsSnapshot
    database_transaction_duration: _full_metrics.DurationMetricSnapshot
    database_pool_wait_duration: _full_metrics.DurationMetricSnapshot
    spool_flush_duration: _full_metrics.DurationMetricSnapshot
    database_transaction_success: int
    database_transaction_failure: int
    spool_enqueued_records: int
    spool_committed_records: int
    spool_failure_total: int
    spool_result_unknown_total: int
    structured_log_failure_total: int
    database_pool_active: int
    database_pool_peak: int
    spool_ready_files: int
    spool_leased_files: int
    spool_result_unknown_files: int

    def __post_init__(self) -> None:
        if self.version != PLATFORM_METRICS_VERSION:
            raise ValueError("PlatformMetricsSnapshot.version 非法")
        generation = _require_generation(self.generation, label="PlatformMetricsSnapshot.generation")
        if not isinstance(self.full, _full_metrics.FullMetricsSnapshot) or self.full.generation != generation:
            raise ValueError("PlatformMetricsSnapshot.full generation 不一致")
        for metric in PlatformDurationMetric:
            if not isinstance(getattr(self, metric.value), _full_metrics.DurationMetricSnapshot):
                raise TypeError(f"PlatformMetricsSnapshot.{metric.value} 类型非法")
        for metric in (*PlatformCountMetric, *PlatformGaugeMetric):
            _require_nonnegative(
                getattr(self, metric.value),
                label=f"PlatformMetricsSnapshot.{metric.value}",
            )

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "generation": self.generation,
            "full": self.full.as_dict(),
        }
        for metric in PlatformDurationMetric:
            payload[metric.value] = getattr(self, metric.value).as_dict()
        for metric in (*PlatformCountMetric, *PlatformGaugeMetric):
            payload[metric.value] = getattr(self, metric.value)
        return payload

    def to_json(self) -> str:
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
            raise ValueError("PlatformMetricsSnapshot 无法编码为 canonical UTF-8 JSON") from None
        if not encoded or len(encoded) > PLATFORM_METRICS_MAX_JSON_BYTES:
            raise ValueError("PlatformMetricsSnapshot JSON 超过安全上限")
        return rendered


@runtime_checkable
class PlatformMetricsReader(Protocol):
    def snapshot(self) -> PlatformMetricsSnapshot: ...


@runtime_checkable
class PlatformMetricsRecorder(PlatformMetricsReader, Protocol):
    @property
    def generation(self) -> int: ...

    def observe_duration(self, metric: PlatformDurationMetric, seconds: float) -> None: ...

    def increment(self, metric: PlatformCountMetric, amount: int = 1) -> None: ...

    def set_spool_gauges(
        self,
        *,
        ready_files: int,
        leased_files: int,
        result_unknown_files: int,
    ) -> None: ...


class PlatformMetricsRegistry:
    """Process-bound platform accumulator wrapping the unchanged H-07 registry."""

    __slots__ = (
        "_count_values",
        "_duration_values",
        "_full",
        "_gauge_values",
        "_generation",
        "_lock",
        "_owner_pid",
        "_pid_getter",
    )

    def __init__(
        self,
        *,
        generation: int,
        pid_getter: Callable[[], int] = os.getpid,
    ) -> None:
        self._generation = _require_generation(generation, label="PlatformMetricsRegistry.generation")
        if not callable(pid_getter) or inspect.iscoroutinefunction(pid_getter):
            raise TypeError("pid_getter 必须是同步可调用对象")
        self._pid_getter = pid_getter
        self._owner_pid = self._read_pid()
        self._lock = Lock()
        self._duration_values = {metric: _DurationState.empty() for metric in PlatformDurationMetric}
        self._count_values = dict.fromkeys(PlatformCountMetric, 0)
        self._gauge_values = dict.fromkeys(PlatformGaugeMetric, 0)
        self._full = _full_metrics.FullMetricsRegistry(generation=self._generation, pid_getter=pid_getter)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(generation={self.generation!r})"

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def full(self) -> _full_metrics.FullMetricsRegistry:
        return self._full

    def _read_pid(self) -> int:
        failed = False
        value: object | None = None
        try:
            value = self._pid_getter()
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError
        except Exception:
            failed = True
        if failed or not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PlatformMetricsOwnershipError("platform metrics owner unavailable")
        return value

    def _require_owner(self) -> None:
        if self._read_pid() != self._owner_pid:
            raise PlatformMetricsOwnershipError("PlatformMetricsRegistry 不得跨进程复用")

    def observe_duration(self, metric: PlatformDurationMetric, seconds: float) -> None:
        if not isinstance(metric, PlatformDurationMetric):
            raise TypeError("metric 必须是 PlatformDurationMetric")
        duration = _require_duration(seconds, label=f"{metric.value} seconds")
        self._require_owner()
        with self._lock:
            state = self._duration_values[metric]
            if state.count >= FULL_METRICS_MAX_COUNTER:
                raise PlatformMetricsOverflowError(f"{metric.value} count overflow")
            total = math.fsum((state.total_seconds, duration))
            if not math.isfinite(total) or total > _MAX_DURATION_TOTAL_SECONDS:
                raise PlatformMetricsOverflowError(f"{metric.value} total overflow")
            first_bucket = bisect_left(FULL_METRICS_DURATION_BUCKETS_SECONDS, duration)
            updated_buckets = list(state.bucket_counts)
            for index in range(first_bucket, len(updated_buckets)):
                if updated_buckets[index] >= FULL_METRICS_MAX_COUNTER:
                    raise PlatformMetricsOverflowError(f"{metric.value} bucket overflow")
                updated_buckets[index] += 1
            state.count += 1
            state.total_seconds = total
            state.minimum_seconds = duration if state.minimum_seconds is None else min(state.minimum_seconds, duration)
            state.maximum_seconds = duration if state.maximum_seconds is None else max(state.maximum_seconds, duration)
            state.bucket_counts = updated_buckets

    def increment(self, metric: PlatformCountMetric, amount: int = 1) -> None:
        if not isinstance(metric, PlatformCountMetric):
            raise TypeError("metric 必须是 PlatformCountMetric")
        increment = _require_positive(amount, label=f"{metric.value} amount")
        self._require_owner()
        with self._lock:
            current = self._count_values[metric]
            if increment > FULL_METRICS_MAX_COUNTER - current:
                raise PlatformMetricsOverflowError(f"{metric.value} counter overflow")
            self._count_values[metric] = current + increment

    def set_gauge(self, metric: PlatformGaugeMetric, value: int) -> None:
        if not isinstance(metric, PlatformGaugeMetric):
            raise TypeError("metric 必须是 PlatformGaugeMetric")
        selected = _require_nonnegative(value, label=f"{metric.value} value")
        self._require_owner()
        with self._lock:
            self._gauge_values[metric] = selected

    def adjust_gauge(self, metric: PlatformGaugeMetric, amount: int) -> None:
        if not isinstance(metric, PlatformGaugeMetric):
            raise TypeError("metric 必须是 PlatformGaugeMetric")
        if (
            not isinstance(amount, int)
            or isinstance(amount, bool)
            or not -FULL_METRICS_MAX_COUNTER <= amount <= FULL_METRICS_MAX_COUNTER
        ):
            raise ValueError(f"{metric.value} amount 必须是有界整数")
        self._require_owner()
        with self._lock:
            current = self._gauge_values[metric]
            updated = current + amount
            if updated < 0:
                raise ValueError(f"{metric.value} gauge underflow")
            if updated > FULL_METRICS_MAX_COUNTER:
                raise PlatformMetricsOverflowError(f"{metric.value} gauge overflow")
            self._gauge_values[metric] = updated

    def observe_pool_peak(self) -> None:
        self._require_owner()
        with self._lock:
            active = self._gauge_values[PlatformGaugeMetric.DATABASE_POOL_ACTIVE]
            peak = self._gauge_values[PlatformGaugeMetric.DATABASE_POOL_PEAK]
            if active > peak:
                self._gauge_values[PlatformGaugeMetric.DATABASE_POOL_PEAK] = active

    def set_spool_gauges(
        self,
        *,
        ready_files: int,
        leased_files: int,
        result_unknown_files: int,
    ) -> None:
        values = {
            PlatformGaugeMetric.SPOOL_READY_FILES: _require_nonnegative(
                ready_files,
                label="ready_files",
            ),
            PlatformGaugeMetric.SPOOL_LEASED_FILES: _require_nonnegative(
                leased_files,
                label="leased_files",
            ),
            PlatformGaugeMetric.SPOOL_RESULT_UNKNOWN_FILES: _require_nonnegative(
                result_unknown_files,
                label="result_unknown_files",
            ),
        }
        self._require_owner()
        with self._lock:
            self._gauge_values.update(values)

    def snapshot(self) -> PlatformMetricsSnapshot:
        self._require_owner()
        with self._lock:
            durations = {metric: self._duration_values[metric].snapshot() for metric in PlatformDurationMetric}
            counters = dict(self._count_values)
            gauges = dict(self._gauge_values)
            full = self._full.snapshot()
        return PlatformMetricsSnapshot(
            version=PLATFORM_METRICS_VERSION,
            generation=self.generation,
            full=full,
            database_transaction_duration=durations[PlatformDurationMetric.DATABASE_TRANSACTION_DURATION],
            database_pool_wait_duration=durations[PlatformDurationMetric.DATABASE_POOL_WAIT_DURATION],
            spool_flush_duration=durations[PlatformDurationMetric.SPOOL_FLUSH_DURATION],
            database_transaction_success=counters[PlatformCountMetric.DATABASE_TRANSACTION_SUCCESS],
            database_transaction_failure=counters[PlatformCountMetric.DATABASE_TRANSACTION_FAILURE],
            spool_enqueued_records=counters[PlatformCountMetric.SPOOL_ENQUEUED_RECORDS],
            spool_committed_records=counters[PlatformCountMetric.SPOOL_COMMITTED_RECORDS],
            spool_failure_total=counters[PlatformCountMetric.SPOOL_FAILURE_TOTAL],
            spool_result_unknown_total=counters[PlatformCountMetric.SPOOL_RESULT_UNKNOWN_TOTAL],
            structured_log_failure_total=counters[PlatformCountMetric.STRUCTURED_LOG_FAILURE_TOTAL],
            database_pool_active=gauges[PlatformGaugeMetric.DATABASE_POOL_ACTIVE],
            database_pool_peak=gauges[PlatformGaugeMetric.DATABASE_POOL_PEAK],
            spool_ready_files=gauges[PlatformGaugeMetric.SPOOL_READY_FILES],
            spool_leased_files=gauges[PlatformGaugeMetric.SPOOL_LEASED_FILES],
            spool_result_unknown_files=gauges[PlatformGaugeMetric.SPOOL_RESULT_UNKNOWN_FILES],
        )


__all__ = [
    "PLATFORM_METRICS_MAX_JSON_BYTES",
    "PLATFORM_METRICS_VERSION",
    "PlatformCountMetric",
    "PlatformDurationMetric",
    "PlatformGaugeMetric",
    "PlatformMetricsError",
    "PlatformMetricsOverflowError",
    "PlatformMetricsOwnershipError",
    "PlatformMetricsReader",
    "PlatformMetricsRecorder",
    "PlatformMetricsRegistry",
    "PlatformMetricsSnapshot",
]

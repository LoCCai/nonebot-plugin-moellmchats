from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import inspect
import json
import math
import os
import re
from threading import Lock
from typing import Protocol, runtime_checkable

from .database_schema import MODEL_USAGE_COST_PRECISION, MODEL_USAGE_COST_SCALE
from .model_usage import ModelUsageRecord

FULL_METRICS_VERSION = 1
FULL_METRICS_MAX_COUNTER = (1 << 63) - 1
FULL_METRICS_MAX_DURATION_SECONDS = 86_400.0
FULL_METRICS_MAX_JSON_BYTES = 32_768
FULL_METRICS_DURATION_BUCKETS_SECONDS = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    900.0,
    3_600.0,
)

_COST_SCALE_FACTOR = 10**MODEL_USAGE_COST_SCALE
_MAX_COST_UNITS_EXCLUSIVE = 10**MODEL_USAGE_COST_PRECISION
_MAX_COST_EXCLUSIVE = Decimal(10) ** (MODEL_USAGE_COST_PRECISION - MODEL_USAGE_COST_SCALE)
_MAX_DURATION_TOTAL_SECONDS = FULL_METRICS_MAX_DURATION_SECONDS * FULL_METRICS_MAX_COUNTER
_CANONICAL_COST_RE = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?$")


class FullMetricsError(RuntimeError):
    """Base error for the detached H-07 metrics primitive."""


class FullMetricsOwnershipError(FullMetricsError):
    """A registry was accessed from a process other than its owner."""


class FullMetricsOverflowError(FullMetricsError):
    """An observation would exceed a fixed metric boundary."""


class FullDurationMetric(str, Enum):
    """The complete fixed set of H-07 duration histograms."""

    LLM_REQUEST_DURATION = "llm_request_duration"
    CLASSIFICATION_DURATION = "classification_duration"
    QUEUE_DURATION = "queue_duration"
    TOOL_WAIT_DURATION = "tool_wait_duration"
    TOOL_EXECUTION_DURATION = "tool_execution_duration"


class FullCountMetric(str, Enum):
    """The complete fixed set of integer H-07 counters."""

    TOOL_FAILURE_TOTAL = "tool_failure_total"
    TOKEN_INPUT = "token_input"
    TOKEN_OUTPUT = "token_output"
    CACHE_HIT = "cache_hit"
    CACHE_MISS = "cache_miss"
    RELOAD_SUCCESS = "reload_success"
    RELOAD_FAILURE = "reload_failure"


FULL_METRICS_FIELD_NAMES = (
    *(metric.value for metric in FullDurationMetric),
    FullCountMetric.TOOL_FAILURE_TOTAL.value,
    FullCountMetric.TOKEN_INPUT.value,
    FullCountMetric.TOKEN_OUTPUT.value,
    "cost",
    FullCountMetric.CACHE_HIT.value,
    FullCountMetric.CACHE_MISS.value,
    FullCountMetric.RELOAD_SUCCESS.value,
    FullCountMetric.RELOAD_FAILURE.value,
)


def _require_counter(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= FULL_METRICS_MAX_COUNTER:
        raise ValueError(f"{label} 必须是非负 PostgreSQL BIGINT")
    return value


def _require_positive_counter(value: object, *, label: str) -> int:
    normalized = _require_counter(value, label=label)
    if normalized == 0:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT")
    return normalized


def _require_duration(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是有限非负秒数")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= FULL_METRICS_MAX_DURATION_SECONDS:
        raise ValueError(f"{label} 必须是有限非负秒数且不超过一天")
    return normalized


def _cost_to_units(value: object, *, label: str) -> int:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} 必须是非负有限 Decimal")
    if value == 0:
        return 0
    if value >= _MAX_COST_EXCLUSIVE:
        raise ValueError(f"{label} 超出 NUMERIC(24, 12) 整数位上限")

    sign, digits, exponent = value.as_tuple()
    if sign or not isinstance(exponent, int):
        raise ValueError(f"{label} 必须是普通非负有限 Decimal")
    canonical_digits = digits
    canonical_exponent = exponent
    while canonical_digits[-1] == 0:
        canonical_digits = canonical_digits[:-1]
        canonical_exponent += 1
    if canonical_exponent < -MODEL_USAGE_COST_SCALE:
        raise ValueError(f"{label} 超出 NUMERIC(24, 12) 小数位上限")

    coefficient = 0
    for digit in canonical_digits:
        coefficient = coefficient * 10 + digit
    units = coefficient * 10 ** (canonical_exponent + MODEL_USAGE_COST_SCALE)
    if not 0 <= units < _MAX_COST_UNITS_EXCLUSIVE:
        raise ValueError(f"{label} 超出 NUMERIC(24, 12) 上限")
    return units


def _cost_units_text(units: int) -> str:
    if not isinstance(units, int) or isinstance(units, bool) or not 0 <= units < _MAX_COST_UNITS_EXCLUSIVE:
        raise ValueError("cost units 超出 NUMERIC(24, 12) 上限")
    whole, fractional = divmod(units, _COST_SCALE_FACTOR)
    if fractional == 0:
        return str(whole)
    return f"{whole}.{fractional:0{MODEL_USAGE_COST_SCALE}d}".rstrip("0")


def _cost_text_units(value: object) -> int:
    if not isinstance(value, str) or not _CANONICAL_COST_RE.fullmatch(value):
        raise ValueError("FullMetricsSnapshot.cost 必须是 canonical NUMERIC(24, 12) 文本")
    whole_text, separator, fractional_text = value.partition(".")
    if separator and fractional_text.endswith("0"):
        raise ValueError("FullMetricsSnapshot.cost 不得包含非 canonical 尾零")
    units = int(whole_text) * _COST_SCALE_FACTOR
    if separator:
        units += int(fractional_text) * 10 ** (MODEL_USAGE_COST_SCALE - len(fractional_text))
    if _cost_units_text(units) != value:
        raise ValueError("FullMetricsSnapshot.cost 必须是 canonical NUMERIC(24, 12) 文本")
    return units


@dataclass(frozen=True)
class DurationBucketSnapshot:
    """One cumulative duration bucket; ``None`` is the fixed +Inf bucket."""

    upper_bound_seconds: float | None
    count: int

    def __post_init__(self) -> None:
        if self.upper_bound_seconds is not None:
            upper_bound = _require_duration(
                self.upper_bound_seconds,
                label="DurationBucketSnapshot.upper_bound_seconds",
            )
            if upper_bound <= 0:
                raise ValueError("DurationBucketSnapshot.upper_bound_seconds 必须为正")
            object.__setattr__(self, "upper_bound_seconds", upper_bound)
        _require_counter(self.count, label="DurationBucketSnapshot.count")

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "le_seconds": self.upper_bound_seconds,
            "count": self.count,
        }


@dataclass(frozen=True)
class DurationMetricSnapshot:
    """Immutable cumulative histogram with fixed, JSON-safe boundaries."""

    count: int
    total_seconds: float
    minimum_seconds: float | None
    maximum_seconds: float | None
    buckets: tuple[DurationBucketSnapshot, ...]

    def __post_init__(self) -> None:
        count = _require_counter(self.count, label="DurationMetricSnapshot.count")
        total = _require_duration_total(self.total_seconds)
        expected_bounds = (*FULL_METRICS_DURATION_BUCKETS_SECONDS, None)
        if (
            not isinstance(self.buckets, tuple)
            or len(self.buckets) != len(expected_bounds)
            or any(not isinstance(bucket, DurationBucketSnapshot) for bucket in self.buckets)
        ):
            raise ValueError("DurationMetricSnapshot.buckets 必须是 tuple 且精确匹配固定边界")
        observed_bounds = tuple(bucket.upper_bound_seconds for bucket in self.buckets)
        if observed_bounds != expected_bounds:
            raise ValueError("DurationMetricSnapshot.buckets 必须精确匹配固定边界")
        bucket_counts = tuple(bucket.count for bucket in self.buckets)
        if tuple(sorted(bucket_counts)) != bucket_counts or bucket_counts[-1] != count:
            raise ValueError("DurationMetricSnapshot.buckets 必须是以总数结尾的单调累计计数")

        if count == 0:
            if total != 0.0 or self.minimum_seconds is not None or self.maximum_seconds is not None:
                raise ValueError("空 DurationMetricSnapshot 必须使用零总时长且无 min/max")
            return
        if self.minimum_seconds is None or self.maximum_seconds is None:
            raise ValueError("非空 DurationMetricSnapshot 必须提供 min/max")
        minimum = _require_duration(self.minimum_seconds, label="DurationMetricSnapshot.minimum_seconds")
        maximum = _require_duration(self.maximum_seconds, label="DurationMetricSnapshot.maximum_seconds")
        if minimum > maximum:
            raise ValueError("DurationMetricSnapshot min/max 顺序非法")
        tolerance = max(1e-12, abs(total) * 1e-12)
        if total + tolerance < minimum or total - tolerance > maximum * count:
            raise ValueError("DurationMetricSnapshot total 与 count/min/max 不一致")
        object.__setattr__(self, "minimum_seconds", minimum)
        object.__setattr__(self, "maximum_seconds", maximum)

    def as_dict(self) -> dict[str, object]:
        return {
            "count": self.count,
            "total_seconds": self.total_seconds,
            "minimum_seconds": self.minimum_seconds,
            "maximum_seconds": self.maximum_seconds,
            "buckets": [bucket.as_dict() for bucket in self.buckets],
        }


def _require_duration_total(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("DurationMetricSnapshot.total_seconds 必须是有限非负数")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0 <= normalized <= _MAX_DURATION_TOTAL_SECONDS:
        raise ValueError("DurationMetricSnapshot.total_seconds 超出安全上限")
    return normalized


@dataclass(frozen=True)
class FullMetricsSnapshot:
    """Closed, generation-bound H-07 metric snapshot with no labels."""

    version: int
    generation: int
    llm_request_duration: DurationMetricSnapshot
    classification_duration: DurationMetricSnapshot
    queue_duration: DurationMetricSnapshot
    tool_wait_duration: DurationMetricSnapshot
    tool_execution_duration: DurationMetricSnapshot
    tool_failure_total: int
    token_input: int
    token_output: int
    cost: str
    cache_hit: int
    cache_miss: int
    reload_success: int
    reload_failure: int

    def __post_init__(self) -> None:
        if self.version != FULL_METRICS_VERSION:
            raise ValueError("FullMetricsSnapshot.version 非法")
        generation = _require_counter(self.generation, label="FullMetricsSnapshot.generation")
        if generation == 0:
            raise ValueError("FullMetricsSnapshot.generation 必须为正")
        for name in FullDurationMetric:
            if not isinstance(getattr(self, name.value), DurationMetricSnapshot):
                raise TypeError(f"FullMetricsSnapshot.{name.value} 类型非法")
        for name in FullCountMetric:
            _require_counter(getattr(self, name.value), label=f"FullMetricsSnapshot.{name.value}")
        _cost_text_units(self.cost)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "generation": self.generation,
        }
        for metric in FullDurationMetric:
            payload[metric.value] = getattr(self, metric.value).as_dict()
        for metric in (
            FullCountMetric.TOOL_FAILURE_TOTAL,
            FullCountMetric.TOKEN_INPUT,
            FullCountMetric.TOKEN_OUTPUT,
        ):
            payload[metric.value] = getattr(self, metric.value)
        payload["cost"] = self.cost
        for metric in (
            FullCountMetric.CACHE_HIT,
            FullCountMetric.CACHE_MISS,
            FullCountMetric.RELOAD_SUCCESS,
            FullCountMetric.RELOAD_FAILURE,
        ):
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
            raise ValueError("FullMetricsSnapshot 无法编码为 canonical UTF-8 JSON") from None
        if not encoded or len(encoded) > FULL_METRICS_MAX_JSON_BYTES:
            raise ValueError("FullMetricsSnapshot JSON 超过安全上限")
        return rendered


@runtime_checkable
class FullMetricsReader(Protocol):
    """Explicit snapshot-only port for a future H-04 API integration."""

    def snapshot(self) -> FullMetricsSnapshot: ...


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

    def snapshot(self) -> DurationMetricSnapshot:
        bounds = (*FULL_METRICS_DURATION_BUCKETS_SECONDS, None)
        return DurationMetricSnapshot(
            count=self.count,
            total_seconds=self.total_seconds,
            minimum_seconds=self.minimum_seconds,
            maximum_seconds=self.maximum_seconds,
            buckets=tuple(
                DurationBucketSnapshot(upper_bound_seconds=bound, count=count)
                for bound, count in zip(bounds, self.bucket_counts, strict=True)
            ),
        )


class FullMetricsRegistry:
    """Detached, process-bound and thread-safe fixed H-07 accumulator.

    The registry intentionally accepts no labels or arbitrary metric names. It
    performs no I/O, creates no background task, and is never instantiated at
    module import. A runtime integration must explicitly construct one registry
    for one generation and explicitly pass observations to it.
    """

    __slots__ = (
        "_cost_units",
        "_count_values",
        "_duration_values",
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
        selected_generation = _require_counter(generation, label="FullMetricsRegistry.generation")
        if selected_generation == 0:
            raise ValueError("FullMetricsRegistry.generation 必须为正")
        if not callable(pid_getter) or inspect.iscoroutinefunction(pid_getter):
            raise TypeError("pid_getter 必须是同步可调用对象")
        self._generation = selected_generation
        self._pid_getter = pid_getter
        self._owner_pid = self._read_pid()
        self._lock = Lock()
        self._duration_values = {metric: _DurationState.empty() for metric in FullDurationMetric}
        self._count_values = dict.fromkeys(FullCountMetric, 0)
        self._cost_units = 0

    def __repr__(self) -> str:
        return f"{type(self).__name__}(generation={self._generation})"

    @property
    def generation(self) -> int:
        return self._generation

    def _read_pid(self) -> int:
        failed = False
        value: object | None = None
        try:
            value = self._pid_getter()
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError("async pid result")
        except Exception:
            failed = True
        if failed or not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise FullMetricsOwnershipError("full metrics owner unavailable")
        return value

    def _require_owner(self) -> None:
        if self._read_pid() != self._owner_pid:
            raise FullMetricsOwnershipError("FullMetricsRegistry 不得跨进程复用")

    def observe_duration(self, metric: FullDurationMetric, seconds: float) -> None:
        if not isinstance(metric, FullDurationMetric):
            raise TypeError("metric 必须是 FullDurationMetric")
        duration = _require_duration(seconds, label=f"{metric.value} seconds")
        self._require_owner()
        with self._lock:
            state = self._duration_values[metric]
            if state.count >= FULL_METRICS_MAX_COUNTER:
                raise FullMetricsOverflowError(f"{metric.value} count overflow")
            total = math.fsum((state.total_seconds, duration))
            if not math.isfinite(total) or total > _MAX_DURATION_TOTAL_SECONDS:
                raise FullMetricsOverflowError(f"{metric.value} total overflow")
            first_bucket = bisect_left(FULL_METRICS_DURATION_BUCKETS_SECONDS, duration)
            updated_buckets = list(state.bucket_counts)
            for index in range(first_bucket, len(updated_buckets)):
                if updated_buckets[index] >= FULL_METRICS_MAX_COUNTER:
                    raise FullMetricsOverflowError(f"{metric.value} bucket overflow")
                updated_buckets[index] += 1

            state.count += 1
            state.total_seconds = total
            state.minimum_seconds = duration if state.minimum_seconds is None else min(state.minimum_seconds, duration)
            state.maximum_seconds = duration if state.maximum_seconds is None else max(state.maximum_seconds, duration)
            state.bucket_counts = updated_buckets

    def increment(self, metric: FullCountMetric, amount: int = 1) -> None:
        if not isinstance(metric, FullCountMetric):
            raise TypeError("metric 必须是 FullCountMetric")
        increment = _require_positive_counter(amount, label=f"{metric.value} amount")
        self._require_owner()
        with self._lock:
            current = self._count_values[metric]
            if increment > FULL_METRICS_MAX_COUNTER - current:
                raise FullMetricsOverflowError(f"{metric.value} counter overflow")
            self._count_values[metric] = current + increment

    def observe_cost(self, cost: Decimal) -> None:
        units = _cost_to_units(cost, label="cost")
        self._require_owner()
        with self._lock:
            if units >= _MAX_COST_UNITS_EXCLUSIVE - self._cost_units:
                raise FullMetricsOverflowError("cost counter overflow")
            self._cost_units += units

    def observe_usage(self, record: ModelUsageRecord) -> None:
        """Atomically add token and known-cost totals without retaining identity."""

        if not isinstance(record, ModelUsageRecord):
            raise TypeError("record 必须是 ModelUsageRecord")
        cost_units = 0 if record.cost is None else _cost_to_units(record.cost, label="ModelUsageRecord.cost")
        self._require_owner()
        with self._lock:
            input_current = self._count_values[FullCountMetric.TOKEN_INPUT]
            output_current = self._count_values[FullCountMetric.TOKEN_OUTPUT]
            if record.input_tokens > FULL_METRICS_MAX_COUNTER - input_current:
                raise FullMetricsOverflowError("token_input counter overflow")
            if record.output_tokens > FULL_METRICS_MAX_COUNTER - output_current:
                raise FullMetricsOverflowError("token_output counter overflow")
            if cost_units >= _MAX_COST_UNITS_EXCLUSIVE - self._cost_units:
                raise FullMetricsOverflowError("cost counter overflow")
            self._count_values[FullCountMetric.TOKEN_INPUT] = input_current + record.input_tokens
            self._count_values[FullCountMetric.TOKEN_OUTPUT] = output_current + record.output_tokens
            self._cost_units += cost_units

    def observe_cache(self, *, hit: bool) -> None:
        if type(hit) is not bool:
            raise ValueError("hit 必须是 bool")
        self.increment(FullCountMetric.CACHE_HIT if hit else FullCountMetric.CACHE_MISS)

    def observe_reload(self, *, success: bool) -> None:
        if type(success) is not bool:
            raise ValueError("success 必须是 bool")
        self.increment(FullCountMetric.RELOAD_SUCCESS if success else FullCountMetric.RELOAD_FAILURE)

    def observe_tool_failure(self, amount: int = 1) -> None:
        self.increment(FullCountMetric.TOOL_FAILURE_TOTAL, amount)

    def snapshot(self) -> FullMetricsSnapshot:
        self._require_owner()
        with self._lock:
            durations = {metric: self._duration_values[metric].snapshot() for metric in FullDurationMetric}
            counters = dict(self._count_values)
            cost = _cost_units_text(self._cost_units)
        return FullMetricsSnapshot(
            version=FULL_METRICS_VERSION,
            generation=self._generation,
            llm_request_duration=durations[FullDurationMetric.LLM_REQUEST_DURATION],
            classification_duration=durations[FullDurationMetric.CLASSIFICATION_DURATION],
            queue_duration=durations[FullDurationMetric.QUEUE_DURATION],
            tool_wait_duration=durations[FullDurationMetric.TOOL_WAIT_DURATION],
            tool_execution_duration=durations[FullDurationMetric.TOOL_EXECUTION_DURATION],
            tool_failure_total=counters[FullCountMetric.TOOL_FAILURE_TOTAL],
            token_input=counters[FullCountMetric.TOKEN_INPUT],
            token_output=counters[FullCountMetric.TOKEN_OUTPUT],
            cost=cost,
            cache_hit=counters[FullCountMetric.CACHE_HIT],
            cache_miss=counters[FullCountMetric.CACHE_MISS],
            reload_success=counters[FullCountMetric.RELOAD_SUCCESS],
            reload_failure=counters[FullCountMetric.RELOAD_FAILURE],
        )


__all__ = [
    "FULL_METRICS_DURATION_BUCKETS_SECONDS",
    "FULL_METRICS_FIELD_NAMES",
    "FULL_METRICS_MAX_COUNTER",
    "FULL_METRICS_MAX_DURATION_SECONDS",
    "FULL_METRICS_MAX_JSON_BYTES",
    "FULL_METRICS_VERSION",
    "DurationBucketSnapshot",
    "DurationMetricSnapshot",
    "FullCountMetric",
    "FullDurationMetric",
    "FullMetricsError",
    "FullMetricsOverflowError",
    "FullMetricsOwnershipError",
    "FullMetricsReader",
    "FullMetricsRegistry",
    "FullMetricsSnapshot",
]

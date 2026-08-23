from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import importlib
import inspect
import json

import pytest

from nonebot_plugin_moellmchats.full_metrics import (
    FULL_METRICS_DURATION_BUCKETS_SECONDS,
    FULL_METRICS_FIELD_NAMES,
    FULL_METRICS_MAX_COUNTER,
    FULL_METRICS_MAX_DURATION_SECONDS,
    FULL_METRICS_MAX_JSON_BYTES,
    FULL_METRICS_VERSION,
    DurationBucketSnapshot,
    DurationMetricSnapshot,
    FullCountMetric,
    FullDurationMetric,
    FullMetricsOverflowError,
    FullMetricsOwnershipError,
    FullMetricsReader,
    FullMetricsRegistry,
)
from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord


def _usage(
    *,
    input_tokens: int = 11,
    output_tokens: int = 7,
    cost: Decimal | None = Decimal("0.0000012300"),
) -> ModelUsageRecord:
    return ModelUsageRecord(
        usage_id=None,
        run_id="run-full-metrics-secret",
        provider="provider-secret",
        model="model-secret",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=3,
        cached_tokens=2,
        cost=cost,
        created_at=datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
    )


def _empty_duration() -> DurationMetricSnapshot:
    return FullMetricsRegistry(generation=1).snapshot().queue_duration


def test_schema_is_fixed_and_matches_every_planned_full_metric() -> None:
    assert FULL_METRICS_VERSION == 1
    assert FULL_METRICS_MAX_COUNTER == (1 << 63) - 1
    assert FULL_METRICS_MAX_DURATION_SECONDS == 86_400.0
    assert tuple(metric.value for metric in FullDurationMetric) == (
        "llm_request_duration",
        "classification_duration",
        "queue_duration",
        "tool_wait_duration",
        "tool_execution_duration",
    )
    assert tuple(metric.value for metric in FullCountMetric) == (
        "tool_failure_total",
        "token_input",
        "token_output",
        "cache_hit",
        "cache_miss",
        "reload_success",
        "reload_failure",
    )
    assert FULL_METRICS_FIELD_NAMES == (
        "llm_request_duration",
        "classification_duration",
        "queue_duration",
        "tool_wait_duration",
        "tool_execution_duration",
        "tool_failure_total",
        "token_input",
        "token_output",
        "cost",
        "cache_hit",
        "cache_miss",
        "reload_success",
        "reload_failure",
    )


def test_empty_snapshot_is_closed_generation_bound_and_json_safe() -> None:
    registry = FullMetricsRegistry(generation=7)

    snapshot = registry.snapshot()
    payload = snapshot.as_dict()

    assert isinstance(registry, FullMetricsReader)
    assert snapshot.version == FULL_METRICS_VERSION
    assert snapshot.generation == 7
    assert tuple(payload) == ("version", "generation", *FULL_METRICS_FIELD_NAMES)
    assert payload["cost"] == "0"
    for metric in FullDurationMetric:
        duration = getattr(snapshot, metric.value)
        assert duration.count == 0
        assert duration.total_seconds == 0.0
        assert duration.minimum_seconds is None
        assert duration.maximum_seconds is None
        assert tuple(bucket.upper_bound_seconds for bucket in duration.buckets) == (
            *FULL_METRICS_DURATION_BUCKETS_SECONDS,
            None,
        )
        assert all(bucket.count == 0 for bucket in duration.buckets)
    for metric in FullCountMetric:
        assert getattr(snapshot, metric.value) == 0

    rendered = snapshot.to_json()
    assert len(rendered.encode("utf-8")) <= FULL_METRICS_MAX_JSON_BYTES
    assert json.loads(rendered) == payload
    assert rendered == json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


@pytest.mark.parametrize("metric", list(FullDurationMetric))
def test_every_duration_metric_records_fixed_cumulative_histograms(metric: FullDurationMetric) -> None:
    registry = FullMetricsRegistry(generation=3)

    for seconds in (0.0, 0.001, 0.002, 1.0, 4_000.0):
        registry.observe_duration(metric, seconds)

    duration = getattr(registry.snapshot(), metric.value)
    assert duration.count == 5
    assert duration.total_seconds == pytest.approx(4_001.003)
    assert duration.minimum_seconds == 0.0
    assert duration.maximum_seconds == 4_000.0
    assert duration.buckets[0] == DurationBucketSnapshot(upper_bound_seconds=0.001, count=2)
    assert duration.buckets[1] == DurationBucketSnapshot(upper_bound_seconds=0.005, count=3)
    assert next(bucket for bucket in duration.buckets if bucket.upper_bound_seconds == 1.0).count == 4
    assert duration.buckets[-2].count == 4
    assert duration.buckets[-1] == DurationBucketSnapshot(upper_bound_seconds=None, count=5)


@pytest.mark.parametrize(
    "seconds",
    [True, -1, float("nan"), float("inf"), -float("inf"), FULL_METRICS_MAX_DURATION_SECONDS + 0.001, "1"],
)
def test_duration_rejects_invalid_values_without_mutation(seconds: object) -> None:
    registry = FullMetricsRegistry(generation=1)
    before = registry.snapshot()

    with pytest.raises(ValueError, match=r"秒数|一天"):
        registry.observe_duration(FullDurationMetric.QUEUE_DURATION, seconds)  # type: ignore[arg-type]

    assert registry.snapshot() == before


def test_duration_requires_strong_enum_before_reading_registry_owner() -> None:
    pid_calls = 0

    def pid_getter() -> int:
        nonlocal pid_calls
        pid_calls += 1
        return 101

    registry = FullMetricsRegistry(generation=1, pid_getter=pid_getter)
    assert pid_calls == 1

    with pytest.raises(TypeError, match="FullDurationMetric"):
        registry.observe_duration("queue_duration", 0.1)  # type: ignore[arg-type]

    assert pid_calls == 1


@pytest.mark.parametrize("metric", list(FullCountMetric))
def test_every_integer_counter_uses_strong_name_and_positive_bigint_amount(metric: FullCountMetric) -> None:
    registry = FullMetricsRegistry(generation=1)

    registry.increment(metric)
    registry.increment(metric, 4)

    assert getattr(registry.snapshot(), metric.value) == 5


@pytest.mark.parametrize("amount", [0, -1, True, 1 << 63, 1.5, "1"])
def test_counter_rejects_invalid_amount_without_mutation(amount: object) -> None:
    registry = FullMetricsRegistry(generation=1)
    before = registry.snapshot()

    with pytest.raises(ValueError, match="amount"):
        registry.increment(FullCountMetric.CACHE_HIT, amount)  # type: ignore[arg-type]

    assert registry.snapshot() == before


def test_counter_requires_strong_enum_before_reading_registry_owner() -> None:
    pid_calls = 0

    def pid_getter() -> int:
        nonlocal pid_calls
        pid_calls += 1
        return 101

    registry = FullMetricsRegistry(generation=1, pid_getter=pid_getter)

    with pytest.raises(TypeError, match="FullCountMetric"):
        registry.increment("cache_hit")  # type: ignore[arg-type]

    assert pid_calls == 1


def test_counter_overflow_is_fail_closed_and_does_not_wrap() -> None:
    registry = FullMetricsRegistry(generation=1)
    registry.increment(FullCountMetric.TOOL_FAILURE_TOTAL, FULL_METRICS_MAX_COUNTER)
    before = registry.snapshot()

    with pytest.raises(FullMetricsOverflowError, match="tool_failure_total"):
        registry.observe_tool_failure()

    assert registry.snapshot() == before


def test_cost_is_exact_canonical_numeric_24_12_and_ignores_decimal_context_rounding() -> None:
    registry = FullMetricsRegistry(generation=1)

    with localcontext() as context:
        context.prec = 2
        registry.observe_cost(Decimal("0.123456789012"))
        registry.observe_cost(Decimal("1.000000000001"))
        registry.observe_cost(Decimal("0.0000000000070"))

    assert registry.snapshot().cost == "1.12345678902"


@pytest.mark.parametrize(
    "cost",
    [
        0.1,
        True,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-0.1"),
        Decimal("1000000000000"),
        Decimal("0.0000000000001"),
    ],
)
def test_cost_rejects_unsafe_or_out_of_schema_values_without_mutation(cost: object) -> None:
    registry = FullMetricsRegistry(generation=1)
    before = registry.snapshot()

    with pytest.raises(ValueError, match="cost"):
        registry.observe_cost(cost)  # type: ignore[arg-type]

    assert registry.snapshot() == before


def test_cost_accepts_real_zero_and_canonicalizes_negative_zero() -> None:
    registry = FullMetricsRegistry(generation=1)

    registry.observe_cost(Decimal("0.000000000000"))
    registry.observe_cost(Decimal("-0"))

    assert registry.snapshot().cost == "0"


def test_cost_overflow_is_detected_before_mutation() -> None:
    registry = FullMetricsRegistry(generation=1)
    registry.observe_cost(Decimal("999999999999.999999999999"))
    before = registry.snapshot()

    with pytest.raises(FullMetricsOverflowError, match="cost"):
        registry.observe_cost(Decimal("0.000000000001"))

    assert registry.snapshot() == before


def test_usage_observation_is_atomic_exact_and_does_not_retain_high_cardinality_identity() -> None:
    registry = FullMetricsRegistry(generation=9)
    record = _usage()

    registry.observe_usage(record)

    snapshot = registry.snapshot()
    assert snapshot.token_input == 11
    assert snapshot.token_output == 7
    assert snapshot.cost == "0.00000123"
    rendered = snapshot.to_json()
    assert record.run_id not in rendered
    assert record.provider not in rendered
    assert record.model not in rendered
    assert "reasoning_tokens" not in rendered
    assert "cached_tokens" not in rendered
    assert not hasattr(snapshot, "labels")


def test_usage_keeps_unknown_cost_distinct_from_a_cost_failure() -> None:
    registry = FullMetricsRegistry(generation=1)

    registry.observe_usage(_usage(input_tokens=2, output_tokens=3, cost=None))

    snapshot = registry.snapshot()
    assert snapshot.token_input == 2
    assert snapshot.token_output == 3
    assert snapshot.cost == "0"


def test_usage_overflow_does_not_partially_update_tokens_or_cost() -> None:
    registry = FullMetricsRegistry(generation=1)
    registry.increment(FullCountMetric.TOKEN_INPUT, FULL_METRICS_MAX_COUNTER)
    registry.increment(FullCountMetric.TOKEN_OUTPUT, 10)
    registry.observe_cost(Decimal("1"))
    before = registry.snapshot()

    with pytest.raises(FullMetricsOverflowError, match="token_input"):
        registry.observe_usage(_usage(input_tokens=1, output_tokens=5, cost=Decimal("2")))

    assert registry.snapshot() == before


def test_usage_cost_overflow_does_not_partially_update_tokens() -> None:
    registry = FullMetricsRegistry(generation=1)
    registry.observe_cost(Decimal("999999999999.999999999999"))
    before = registry.snapshot()

    with pytest.raises(FullMetricsOverflowError, match="cost"):
        registry.observe_usage(_usage(input_tokens=1, output_tokens=2, cost=Decimal("0.000000000001")))

    assert registry.snapshot() == before


def test_usage_requires_valid_model_usage_record_before_owner_or_mutation() -> None:
    registry = FullMetricsRegistry(generation=1)
    before = registry.snapshot()

    with pytest.raises(TypeError, match="ModelUsageRecord"):
        registry.observe_usage({"input_tokens": 1})  # type: ignore[arg-type]

    assert registry.snapshot() == before


def test_cache_reload_and_tool_failure_helpers_have_no_free_form_labels() -> None:
    registry = FullMetricsRegistry(generation=1)

    registry.observe_cache(hit=True)
    registry.observe_cache(hit=False)
    registry.observe_reload(success=True)
    registry.observe_reload(success=False)
    registry.observe_tool_failure(3)

    snapshot = registry.snapshot()
    assert snapshot.cache_hit == 1
    assert snapshot.cache_miss == 1
    assert snapshot.reload_success == 1
    assert snapshot.reload_failure == 1
    assert snapshot.tool_failure_total == 3


@pytest.mark.parametrize(
    ("method", "keyword"),
    [
        ("observe_cache", {"hit": 1}),
        ("observe_reload", {"success": "yes"}),
    ],
)
def test_boolean_helpers_reject_loose_values(method: str, keyword: dict[str, object]) -> None:
    registry = FullMetricsRegistry(generation=1)
    before = registry.snapshot()

    with pytest.raises(ValueError, match="bool"):
        getattr(registry, method)(**keyword)

    assert registry.snapshot() == before


def test_snapshots_are_frozen_detached_and_prior_snapshots_do_not_change() -> None:
    registry = FullMetricsRegistry(generation=1)
    first = registry.snapshot()

    registry.observe_duration(FullDurationMetric.LLM_REQUEST_DURATION, 0.25)
    registry.increment(FullCountMetric.CACHE_HIT)
    second = registry.snapshot()

    assert first.llm_request_duration.count == 0
    assert first.cache_hit == 0
    assert second.llm_request_duration.count == 1
    assert second.cache_hit == 1
    payload = second.as_dict()
    payload["cache_hit"] = 999
    duration_payload = payload["llm_request_duration"]
    assert isinstance(duration_payload, dict)
    duration_payload["count"] = 999
    assert second.cache_hit == 1
    assert second.llm_request_duration.count == 1
    with pytest.raises(FrozenInstanceError):
        second.cache_hit = 2  # type: ignore[misc]


def test_registry_is_thread_safe_without_accepting_cross_thread_labels() -> None:
    registry = FullMetricsRegistry(generation=1)

    def observe() -> None:
        for _ in range(250):
            registry.increment(FullCountMetric.CACHE_HIT)
            registry.observe_duration(FullDurationMetric.QUEUE_DURATION, 0.01)
            registry.observe_cost(Decimal("0.000000000001"))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(observe) for _ in range(8)]
        for future in futures:
            future.result()

    snapshot = registry.snapshot()
    assert snapshot.cache_hit == 2_000
    assert snapshot.queue_duration.count == 2_000
    assert snapshot.queue_duration.total_seconds == pytest.approx(20.0)
    assert snapshot.cost == "0.000000002"


def test_registry_rejects_cross_process_access_before_mutation() -> None:
    pid = [100]
    registry = FullMetricsRegistry(generation=1, pid_getter=lambda: pid[0])
    pid[0] = 101

    with pytest.raises(FullMetricsOwnershipError, match="跨进程"):
        registry.increment(FullCountMetric.CACHE_HIT)
    with pytest.raises(FullMetricsOwnershipError, match="跨进程"):
        registry.observe_duration(FullDurationMetric.QUEUE_DURATION, 0.1)
    with pytest.raises(FullMetricsOwnershipError, match="跨进程"):
        registry.snapshot()


@pytest.mark.parametrize("generation", [0, -1, True, 1 << 63, 1.5, "1"])
def test_registry_rejects_invalid_generation(generation: object) -> None:
    with pytest.raises(ValueError, match="generation"):
        FullMetricsRegistry(generation=generation)  # type: ignore[arg-type]


def test_pid_getter_failures_are_generic_and_do_not_leak_original_error() -> None:
    def failing_pid() -> int:
        raise RuntimeError("pid-secret-must-not-leak")

    with pytest.raises(FullMetricsOwnershipError, match="owner unavailable") as captured:
        FullMetricsRegistry(generation=1, pid_getter=failing_pid)

    assert "pid-secret-must-not-leak" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dynamic_pid_coroutine_is_closed_and_rejected() -> None:
    created: list[object] = []

    async def later() -> int:
        return 100

    def dynamic_pid() -> int:
        value = later()
        created.append(value)
        return value  # type: ignore[return-value]

    with pytest.raises(FullMetricsOwnershipError, match="owner unavailable"):
        FullMetricsRegistry(generation=1, pid_getter=dynamic_pid)

    assert len(created) == 1
    assert getattr(created[0], "cr_frame") is None


def test_constructor_rejects_async_or_missing_pid_getter() -> None:
    async def async_pid() -> int:
        return 100

    with pytest.raises(TypeError, match="同步"):
        FullMetricsRegistry(generation=1, pid_getter=async_pid)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="同步"):
        FullMetricsRegistry(generation=1, pid_getter=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"generation": 0},
        {"generation": True},
        {"cache_hit": -1},
        {"token_input": True},
        {"cost": "01"},
        {"cost": "1.0"},
        {"cost": "0.0000000000001"},
        {"cost": "1000000000000"},
    ],
)
def test_snapshot_rejects_noncanonical_or_out_of_boundary_fields(changes: dict[str, object]) -> None:
    snapshot = FullMetricsRegistry(generation=1).snapshot()

    with pytest.raises((TypeError, ValueError)):
        replace(snapshot, **changes)


def test_snapshot_requires_every_duration_to_use_strong_snapshot_type() -> None:
    snapshot = FullMetricsRegistry(generation=1).snapshot()

    with pytest.raises(TypeError, match="queue_duration"):
        replace(snapshot, queue_duration={})  # type: ignore[arg-type]


def test_duration_bucket_and_snapshot_reject_noncanonical_shape() -> None:
    with pytest.raises(ValueError, match="正"):
        DurationBucketSnapshot(upper_bound_seconds=0.0, count=0)
    with pytest.raises(ValueError, match="BIGINT"):
        DurationBucketSnapshot(upper_bound_seconds=None, count=True)  # type: ignore[arg-type]

    empty = _empty_duration()
    with pytest.raises(ValueError, match="固定边界"):
        replace(empty, buckets=empty.buckets[:-1])
    with pytest.raises(ValueError, match="累计计数"):
        replace(
            empty,
            count=1,
            total_seconds=0.1,
            minimum_seconds=0.1,
            maximum_seconds=0.1,
            buckets=tuple(replace(bucket, count=1 if index == 0 else 0) for index, bucket in enumerate(empty.buckets)),
        )
    with pytest.raises(ValueError, match="零总时长"):
        replace(empty, total_seconds=1.0)


def test_nonempty_duration_snapshot_requires_consistent_total_min_max() -> None:
    registry = FullMetricsRegistry(generation=1)
    registry.observe_duration(FullDurationMetric.QUEUE_DURATION, 1.0)
    duration = registry.snapshot().queue_duration

    with pytest.raises(ValueError, match="min/max"):
        replace(duration, minimum_seconds=None)
    with pytest.raises(ValueError, match="顺序"):
        replace(duration, minimum_seconds=2.0, maximum_seconds=1.0)
    with pytest.raises(ValueError, match="不一致"):
        replace(duration, total_seconds=2.0)


def test_module_reload_creates_no_registry_or_live_metric_state() -> None:
    module = importlib.reload(importlib.import_module("nonebot_plugin_moellmchats.full_metrics"))

    assert not any(isinstance(value, module.FullMetricsRegistry) for value in vars(module).values())
    assert "runtime_metrics" not in vars(module)
    assert not any(inspect.isawaitable(value) for value in vars(module).values())
    assert module.FULL_METRICS_FIELD_NAMES == FULL_METRICS_FIELD_NAMES

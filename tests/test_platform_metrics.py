from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
import inspect

import pytest

from nonebot_plugin_moellmchats import full_metrics as full_metrics_module
from nonebot_plugin_moellmchats.platform_metrics import (
    PLATFORM_METRICS_VERSION,
    PlatformCountMetric,
    PlatformDurationMetric,
    PlatformGaugeMetric,
    PlatformMetricsReader,
    PlatformMetricsRegistry,
)


def test_platform_metric_schema_is_closed_generation_bound_and_label_free() -> None:
    registry = PlatformMetricsRegistry(generation=7)

    snapshot = registry.snapshot()

    assert isinstance(registry, PlatformMetricsReader)
    assert PLATFORM_METRICS_VERSION == 1
    assert snapshot.generation == 7
    assert snapshot.full.generation == 7
    assert tuple(metric.value for metric in PlatformDurationMetric) == (
        "database_transaction_duration",
        "database_pool_wait_duration",
        "spool_flush_duration",
    )
    assert tuple(metric.value for metric in PlatformCountMetric) == (
        "database_transaction_success",
        "database_transaction_failure",
        "spool_enqueued_records",
        "spool_committed_records",
        "spool_failure_total",
        "spool_result_unknown_total",
        "structured_log_failure_total",
    )
    assert tuple(metric.value for metric in PlatformGaugeMetric) == (
        "database_pool_active",
        "database_pool_peak",
        "spool_ready_files",
        "spool_leased_files",
        "spool_result_unknown_files",
    )
    rendered = snapshot.to_json()
    for forbidden in ("dsn", "sql", "user_id", "group_id", "payload", "labels"):
        assert forbidden not in rendered.lower()


def test_platform_registry_updates_full_and_platform_metrics_atomically_per_call() -> None:
    registry = PlatformMetricsRegistry(generation=3)

    registry.full.observe_duration(
        full_metrics_module.FullDurationMetric.LLM_REQUEST_DURATION,
        0.5,
    )
    registry.observe_duration(PlatformDurationMetric.DATABASE_TRANSACTION_DURATION, 0.25)
    registry.increment(PlatformCountMetric.DATABASE_TRANSACTION_SUCCESS)
    registry.adjust_gauge(PlatformGaugeMetric.DATABASE_POOL_ACTIVE, 1)
    registry.observe_pool_peak()
    registry.set_spool_gauges(ready_files=2, leased_files=1, result_unknown_files=0)

    snapshot = registry.snapshot()
    assert snapshot.full.llm_request_duration.count == 1
    assert snapshot.database_transaction_duration.count == 1
    assert snapshot.database_transaction_success == 1
    assert snapshot.database_pool_active == 1
    assert snapshot.database_pool_peak == 1
    assert snapshot.spool_ready_files == 2
    assert snapshot.spool_leased_files == 1
    with pytest.raises(FrozenInstanceError):
        snapshot.database_pool_active = 2  # type: ignore[misc]


def test_gauges_are_bounded_and_cannot_underflow_or_accept_arbitrary_names() -> None:
    registry = PlatformMetricsRegistry(generation=1)

    with pytest.raises(TypeError, match="PlatformGaugeMetric"):
        registry.adjust_gauge("database_pool_active", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="underflow"):
        registry.adjust_gauge(PlatformGaugeMetric.DATABASE_POOL_ACTIVE, -1)
    with pytest.raises(ValueError, match="非负"):
        registry.set_gauge(PlatformGaugeMetric.SPOOL_READY_FILES, -1)

    assert registry.snapshot().database_pool_active == 0


def test_module_import_has_no_global_registry() -> None:
    module = importlib.reload(importlib.import_module("nonebot_plugin_moellmchats.platform_metrics"))

    assert not any(isinstance(value, module.PlatformMetricsRegistry) for value in vars(module).values())
    assert not any(inspect.isawaitable(value) for value in vars(module).values())

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord
from nonebot_plugin_moellmchats.usage_batch import (
    UsageBatchClockError,
    UsageBatchClosedError,
    UsageBatchOwnershipError,
    UsageBatchPolicy,
    UsageBatchQueue,
    UsageBatchQueueState,
    UsageBatchResultUnknownError,
)

_NOW = datetime(2026, 8, 23, 0, 15, tzinfo=timezone.utc)


def _usage(
    sequence: int = 1,
    *,
    usage_id: int | None = None,
    created_at: datetime = _NOW,
) -> ModelUsageRecord:
    return ModelUsageRecord(
        usage_id=usage_id,
        run_id="run-usage-1",
        provider="openai",
        model=f"gpt-{sequence}",
        input_tokens=sequence,
        output_tokens=sequence + 1,
        reasoning_tokens=0,
        cached_tokens=0,
        cost=Decimal("0.0000012300"),
        created_at=created_at,
    )


def test_model_usage_record_is_immutable_exact_and_utc_normalized() -> None:
    local_time = _NOW.astimezone(timezone(timedelta(hours=8)))
    record = _usage(created_at=local_time)

    assert record.usage_id is None
    assert record.persisted is False
    assert record.created_at == _NOW
    assert record.created_at.tzinfo is timezone.utc
    assert record.cost == Decimal("0.00000123")
    assert record.total_tokens == 3
    assert record.as_dict() == {
        "usage_id": None,
        "run_id": "run-usage-1",
        "provider": "openai",
        "model": "gpt-1",
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cost": Decimal("0.00000123"),
        "created_at": _NOW,
    }
    with pytest.raises(FrozenInstanceError):
        record.model = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"usage_id": True},
        {"usage_id": 0},
        {"run_id": "bad run"},
        {"provider": " openai"},
        {"model": "bad\nmodel"},
        {"input_tokens": -1},
        {"output_tokens": True},
        {"reasoning_tokens": 1 << 63},
        {"cached_tokens": -1},
        {"cost": 0.1},
        {"cost": Decimal("NaN")},
        {"cost": Decimal("-0.1")},
        {"cost": Decimal("1000000000000")},
        {"cost": Decimal("0.0000000000001")},
        {"created_at": datetime(2026, 8, 23, 0, 15)},
    ],
)
def test_model_usage_record_rejects_values_outside_schema_contract(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "usage_id": None,
        "run_id": "run-usage-1",
        "provider": "openai",
        "model": "gpt-5",
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cost": None,
        "created_at": _NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=r"ModelUsageRecord|run_id"):
        ModelUsageRecord(**values)  # type: ignore[arg-type]


def test_model_usage_record_keeps_unknown_and_real_zero_cost_distinct() -> None:
    unknown = ModelUsageRecord(
        None,
        "run-1",
        "provider",
        "model",
        0,
        0,
        0,
        0,
        None,
        _NOW,
    )
    free = ModelUsageRecord(
        None,
        "run-1",
        "provider",
        "model",
        0,
        0,
        0,
        0,
        Decimal("-0.000"),
        _NOW,
    )

    assert unknown.cost is None
    assert free.cost == Decimal(0)
    assert free.cost is not None


def test_model_usage_cost_normalization_never_uses_ambient_decimal_rounding() -> None:
    exact = Decimal("123456789.123456789012")
    with localcontext() as context:
        context.prec = 3
        record = ModelUsageRecord(
            None,
            "run-1",
            "provider",
            "model",
            0,
            0,
            0,
            0,
            exact,
            _NOW,
        )

    assert record.cost == exact


@pytest.mark.parametrize(
    "changes",
    [
        {"max_batch_size": 0},
        {"max_batch_size": 101},
        {"max_batch_size": True},
        {"flush_interval_seconds": 0},
        {"flush_interval_seconds": 61},
        {"flush_interval_seconds": float("nan")},
        {"max_outstanding_records": 1},
        {"max_outstanding_records": 100_001},
    ],
)
def test_usage_batch_policy_is_bounded(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "max_batch_size": 2,
        "flush_interval_seconds": 1.0,
        "max_outstanding_records": 2,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=r"max_batch|flush_interval|max_outstanding"):
        UsageBatchPolicy(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_queue_leases_at_size_and_ack_is_the_only_capacity_release() -> None:
    clock = [10.0]
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=1.0,
            max_outstanding_records=2,
        ),
        monotonic=lambda: clock[0],
    )
    first = _usage(1)
    second = _usage(2)
    third = _usage(3)

    await queue.put(first)
    await queue.put(second)
    lease = await queue.lease_ready()
    assert lease is not None
    assert lease.records == (first, second)
    assert lease.sequence == 1
    assert queue.pending_count == 0
    assert queue.outstanding_count == 2

    blocked_put = asyncio.create_task(queue.put(third))
    await asyncio.sleep(0)
    assert blocked_put.done() is False

    await queue.acknowledge_committed(lease)
    await blocked_put
    assert queue.outstanding_count == 1
    assert queue.pending_count == 1


@pytest.mark.asyncio
async def test_cancelled_backpressure_put_never_appears_after_capacity_returns() -> None:
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=1,
            flush_interval_seconds=1.0,
            max_outstanding_records=1,
        )
    )
    first = _usage(1)
    cancelled = _usage(2)
    await queue.put(first)
    lease = await queue.lease_ready()
    assert lease is not None

    blocked_put = asyncio.create_task(queue.put(cancelled))
    await asyncio.sleep(0)
    blocked_put.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_put
    await queue.acknowledge_committed(lease)

    assert queue.outstanding_count == 0
    assert await queue.lease_ready(force=True) is None


@pytest.mark.asyncio
async def test_queue_time_threshold_and_waiter_flush_partial_batches() -> None:
    clock = [20.0]
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=3,
            flush_interval_seconds=1.0,
            max_outstanding_records=3,
        ),
        monotonic=lambda: clock[0],
    )
    record = _usage()
    await queue.put(record)

    assert await queue.lease_ready() is None
    clock[0] = 21.0
    lease = await queue.lease_ready()
    assert lease is not None
    assert lease.records == (record,)
    await queue.acknowledge_committed(lease)

    real_queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=0.01,
            max_outstanding_records=2,
        )
    )
    await real_queue.put(record)
    timed_lease = await asyncio.wait_for(real_queue.wait_for_lease(), timeout=1)
    assert timed_lease is not None
    assert timed_lease.records == (record,)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_or_deadlock_a_pending_record() -> None:
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=60.0,
            max_outstanding_records=2,
        )
    )
    record = _usage()
    await queue.put(record)

    waiter = asyncio.create_task(queue.wait_for_lease())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert queue.pending_count == 1
    lease = await queue.lease_ready(force=True)
    assert lease is not None
    assert lease.records == (record,)


@pytest.mark.asyncio
async def test_release_unwritten_requeues_in_order_without_renewing_age() -> None:
    clock = [30.0]
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=1.0,
            max_outstanding_records=4,
        ),
        monotonic=lambda: clock[0],
    )
    records = (_usage(1), _usage(2))
    for record in records:
        await queue.put(record)
    first_lease = await queue.lease_ready()
    assert first_lease is not None

    clock[0] = 31.0
    await queue.release_unwritten(first_lease)
    second_lease = await queue.lease_ready()

    assert second_lease is not None
    assert second_lease.sequence == 2
    assert second_lease.records == records
    assert second_lease.oldest_enqueued_at == first_lease.oldest_enqueued_at


@pytest.mark.asyncio
async def test_unknown_commit_result_is_terminal_and_never_requeued() -> None:
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=1,
            flush_interval_seconds=1.0,
            max_outstanding_records=2,
        )
    )
    secret_record = ModelUsageRecord(
        None,
        "run-secret",
        "private-provider",
        "private-model",
        1,
        2,
        0,
        0,
        None,
        _NOW,
    )
    await queue.put(secret_record)
    lease = await queue.lease_ready()
    assert lease is not None

    await queue.mark_result_unknown(lease)

    assert queue.state is UsageBatchQueueState.RESULT_UNKNOWN
    assert queue.active_lease is lease
    assert queue.pending_count == 0
    assert queue.outstanding_count == 1
    for rendered in (repr(queue), repr(lease), str(queue.safe_diagnostics())):
        assert "private-provider" not in rendered
        assert "private-model" not in rendered
        assert "run-secret" not in rendered
    with pytest.raises(UsageBatchResultUnknownError):
        await queue.put(_usage(2))
    with pytest.raises(UsageBatchResultUnknownError):
        await queue.lease_ready(force=True)
    with pytest.raises(UsageBatchResultUnknownError):
        await queue.acknowledge_committed(lease)


@pytest.mark.asyncio
async def test_close_forces_partial_batch_and_reaches_terminal_empty_state() -> None:
    queue = UsageBatchQueue(
        UsageBatchPolicy(
            max_batch_size=10,
            flush_interval_seconds=60.0,
            max_outstanding_records=10,
        )
    )
    await queue.put(_usage())
    await queue.begin_close()

    assert queue.state is UsageBatchQueueState.CLOSING
    lease = await queue.wait_for_lease()
    assert lease is not None
    assert len(lease.records) == 1
    await queue.acknowledge_committed(lease)
    assert await queue.wait_for_lease() is None
    assert queue.state is UsageBatchQueueState.CLOSED
    with pytest.raises(UsageBatchClosedError):
        await queue.put(_usage(2))


@pytest.mark.asyncio
async def test_queue_rejects_persisted_records_clock_rollback_and_pid_drift() -> None:
    clock = [40.0]
    pid = [100]
    queue = UsageBatchQueue(monotonic=lambda: clock[0], pid_getter=lambda: pid[0])

    with pytest.raises(ValueError, match="未持久化"):
        await queue.put(_usage(usage_id=1))
    await queue.put(_usage())
    clock[0] = 39.0
    with pytest.raises(UsageBatchClockError, match="回退"):
        await queue.lease_ready(force=True)

    owner_queue = UsageBatchQueue(pid_getter=lambda: pid[0])
    await owner_queue.put(_usage())
    pid[0] = 101
    with pytest.raises(UsageBatchOwnershipError, match="跨进程"):
        await owner_queue.lease_ready(force=True)


def test_queue_rejects_cross_event_loop_reuse() -> None:
    queue = UsageBatchQueue()
    asyncio.run(queue.put(_usage()))

    with pytest.raises(UsageBatchOwnershipError, match="event loop"):
        asyncio.run(queue.lease_ready(force=True))

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from nonebot_plugin_moellmchats.audit_batch import (
    AuditBatchClockError,
    AuditBatchClosedError,
    AuditBatchOwnershipError,
    AuditBatchPolicy,
    AuditBatchQueue,
    AuditBatchQueueState,
    AuditBatchResultUnknownError,
)
from nonebot_plugin_moellmchats.audit_event import (
    BATCHABLE_AUDIT_EVENT_TYPES,
    AuditEventRecord,
    AuditWriteMode,
    mutable_audit_json,
)

_NOW = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)


def _event(
    sequence: int = 1,
    *,
    event_id: int | None = None,
    event_type: str = "tool_draft_created",
    created_at: datetime = _NOW,
) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=event_id,
        event_type=event_type,
        actor_user_id="qq:10001",
        actor_type="user",
        target_type="tool_bundle",
        target_id=f"bundle-{sequence}",
        run_id="run-audit-1",
        tool_call_id=None,
        metadata_json={"sequence": sequence, "nested": ["safe", {"ok": True}]},
        created_at=created_at,
    )


def test_audit_event_is_detached_deeply_immutable_and_utc_normalized() -> None:
    source = {"nested": ["safe", {"ok": True}]}
    local_time = _NOW.astimezone(timezone(timedelta(hours=8)))
    record = AuditEventRecord(
        None,
        "tool_draft_created",
        "qq:10001",
        "user",
        "tool_bundle",
        "bundle-1",
        "run-audit-1",
        None,
        source,
        local_time,
    )
    source["nested"].append("mutated")  # type: ignore[union-attr]

    assert record.event_id is None
    assert record.persisted is False
    assert record.created_at == _NOW
    assert record.created_at.tzinfo is timezone.utc
    assert isinstance(record.metadata_json, MappingProxyType)
    assert record.metadata_json["nested"] == ("safe", MappingProxyType({"ok": True}))
    assert record.write_mode is AuditWriteMode.BATCH
    assert record.batchable is True
    assert record.as_dict()["metadata_json"] is record.metadata_json
    with pytest.raises(FrozenInstanceError):
        record.target_id = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        record.metadata_json["new"] = True  # type: ignore[index]


def test_mutable_audit_json_returns_a_driver_safe_detached_copy() -> None:
    record = _event()

    materialized = mutable_audit_json(record.metadata_json)
    assert materialized == {"sequence": 1, "nested": ["safe", {"ok": True}]}
    materialized["nested"].append("changed")

    assert record.metadata_json["nested"] == ("safe", MappingProxyType({"ok": True}))


@pytest.mark.parametrize(
    "event_type",
    [
        "tool_approved",
        "tool_activated",
        "tool_deactivated",
        "tool_rollback",
        "mutating_confirmed",
        "mutating_executed",
        "future_security_event",
    ],
)
def test_safety_and_unknown_event_types_fail_closed_to_immediate(event_type: str) -> None:
    record = _event(event_type=event_type)

    assert record.write_mode is AuditWriteMode.IMMEDIATE
    assert record.batchable is False


def test_batch_allowlist_is_small_explicit_and_non_security_sensitive() -> None:
    assert BATCHABLE_AUDIT_EVENT_TYPES == {
        "runtime_reload",
        "runtime_reload_failed",
        "tool_draft_created",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": True},
        {"event_id": 0},
        {"event_type": "Tool_Approved"},
        {"event_type": "x" * 129},
        {"actor_user_id": " bad"},
        {"actor_type": "bad actor"},
        {"target_type": ""},
        {"target_id": ""},
        {"run_id": "bad\nrun"},
        {"tool_call_id": "call-1", "run_id": None},
        {"metadata_json": []},
        {"metadata_json": {"bad": float("nan")}},
        {"metadata_json": {"bad": "nul\x00value"}},
        {"metadata_json": {"bad": "\ud800"}},
        {"created_at": datetime(2026, 8, 23, 1, 0)},
    ],
)
def test_audit_event_rejects_values_outside_schema_and_json_contract(
    changes: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "event_id": None,
        "event_type": "tool_draft_created",
        "actor_user_id": "qq:10001",
        "actor_type": "user",
        "target_type": "tool_bundle",
        "target_id": "bundle-1",
        "run_id": "run-audit-1",
        "tool_call_id": None,
        "metadata_json": {},
        "created_at": _NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=r"AuditEventRecord|run_id|canonical"):
        AuditEventRecord(**values)  # type: ignore[arg-type]


def test_audit_metadata_rejects_cycles_and_oversized_payloads() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError, match="循环"):
        _event_with_metadata(cyclic)
    with pytest.raises(ValueError, match="64 KiB"):
        _event_with_metadata({"oversized": "x" * 65_536})
    with pytest.raises(ValueError, match="64 KiB"):
        _event_with_metadata({"normalized_numbers": [1e-300] * 300})


def test_audit_event_repr_hides_metadata_payload() -> None:
    record = _event_with_metadata({"secret": "top-secret"})

    assert "top-secret" not in repr(record)


def _event_with_metadata(metadata: object) -> AuditEventRecord:
    return AuditEventRecord(
        None,
        "tool_draft_created",
        None,
        "system",
        "tool_bundle",
        "bundle-1",
        None,
        None,
        metadata,  # type: ignore[arg-type]
        _NOW,
    )


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
def test_audit_batch_policy_is_bounded(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "max_batch_size": 2,
        "flush_interval_seconds": 1.0,
        "max_outstanding_records": 2,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=r"max_batch|flush_interval|max_outstanding"):
        AuditBatchPolicy(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_queue_rejects_immediate_events_before_they_can_be_buffered() -> None:
    queue = AuditBatchQueue()

    with pytest.raises(ValueError, match="即时 append"):
        await queue.put(_event(event_type="tool_approved"))
    with pytest.raises(ValueError, match="即时 append"):
        await queue.put(_event(event_type="unknown_future_event"))

    assert queue.pending_count == 0
    assert queue.outstanding_count == 0


@pytest.mark.asyncio
async def test_queue_leases_at_size_and_ack_is_the_only_capacity_release() -> None:
    clock = [10.0]
    queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=1.0,
            max_outstanding_records=2,
        ),
        monotonic=lambda: clock[0],
    )
    first = _event(1)
    second = _event(2)
    third = _event(3)

    await queue.put(first)
    await queue.put(second)
    lease = await queue.lease_ready()
    assert lease is not None
    assert lease.records == (first, second)
    assert queue.outstanding_count == 2

    blocked_put = asyncio.create_task(queue.put(third))
    await asyncio.sleep(0)
    assert blocked_put.done() is False

    await queue.acknowledge_committed(lease)
    await blocked_put
    assert queue.pending_count == 1
    assert queue.outstanding_count == 1


@pytest.mark.asyncio
async def test_cancelled_backpressure_put_never_appears_after_capacity_returns() -> None:
    queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=1,
            flush_interval_seconds=1.0,
            max_outstanding_records=1,
        )
    )
    first = _event(1)
    cancelled = _event(2)
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
async def test_time_threshold_waiter_and_cancelled_waiter_are_safe() -> None:
    queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=0.01,
            max_outstanding_records=2,
        )
    )
    record = _event()
    await queue.put(record)
    lease = await asyncio.wait_for(queue.wait_for_lease(), timeout=1)
    assert lease is not None
    assert lease.records == (record,)
    await queue.acknowledge_committed(lease)

    slow_queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=60.0,
            max_outstanding_records=2,
        )
    )
    await slow_queue.put(record)
    waiter = asyncio.create_task(slow_queue.wait_for_lease())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slow_queue.pending_count == 1


@pytest.mark.asyncio
async def test_release_unwritten_preserves_order_and_original_age() -> None:
    clock = [30.0]
    queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=2,
            flush_interval_seconds=1.0,
            max_outstanding_records=4,
        ),
        monotonic=lambda: clock[0],
    )
    records = (_event(1), _event(2))
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
async def test_unknown_commit_result_is_terminal_redacted_and_never_replayed() -> None:
    queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=1,
            flush_interval_seconds=1.0,
            max_outstanding_records=2,
        )
    )
    secret = AuditEventRecord(
        None,
        "tool_draft_created",
        "qq:private-actor",
        "user",
        "tool_bundle",
        "private-bundle",
        "run-secret",
        None,
        {"secret": "top-secret"},
        _NOW,
    )
    await queue.put(secret)
    lease = await queue.lease_ready()
    assert lease is not None

    await queue.mark_result_unknown(lease)

    assert queue.state is AuditBatchQueueState.RESULT_UNKNOWN
    assert queue.active_lease is lease
    for rendered in (repr(queue), repr(lease), str(queue.safe_diagnostics()), str(lease.safe_diagnostics())):
        for secret_text in ("private-actor", "private-bundle", "run-secret", "top-secret"):
            assert secret_text not in rendered
    with pytest.raises(AuditBatchResultUnknownError):
        await queue.put(_event(2))
    with pytest.raises(AuditBatchResultUnknownError):
        await queue.lease_ready(force=True)
    with pytest.raises(AuditBatchResultUnknownError):
        await queue.acknowledge_committed(lease)


@pytest.mark.asyncio
async def test_close_flushes_partial_batch_then_reaches_closed_state() -> None:
    queue = AuditBatchQueue(
        AuditBatchPolicy(
            max_batch_size=10,
            flush_interval_seconds=60.0,
            max_outstanding_records=10,
        )
    )
    await queue.put(_event())
    await queue.begin_close()

    lease = await queue.wait_for_lease()
    assert lease is not None
    await queue.acknowledge_committed(lease)
    assert await queue.wait_for_lease() is None
    assert queue.state is AuditBatchQueueState.CLOSED
    with pytest.raises(AuditBatchClosedError):
        await queue.put(_event(2))


@pytest.mark.asyncio
async def test_queue_rejects_persisted_records_clock_rollback_and_pid_drift() -> None:
    clock = [40.0]
    pid = [100]
    queue = AuditBatchQueue(monotonic=lambda: clock[0], pid_getter=lambda: pid[0])

    with pytest.raises(ValueError, match="未持久化"):
        await queue.put(_event(event_id=1))
    await queue.put(_event())
    clock[0] = 39.0
    with pytest.raises(AuditBatchClockError, match="回退"):
        await queue.lease_ready(force=True)

    owner_queue = AuditBatchQueue(pid_getter=lambda: pid[0])
    await owner_queue.put(_event())
    pid[0] = 101
    with pytest.raises(AuditBatchOwnershipError, match="跨进程"):
        await owner_queue.lease_ready(force=True)


def test_queue_rejects_cross_event_loop_reuse() -> None:
    queue = AuditBatchQueue()
    asyncio.run(queue.put(_event()))

    with pytest.raises(AuditBatchOwnershipError, match="event loop"):
        asyncio.run(queue.lease_ready(force=True))

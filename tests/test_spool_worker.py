from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import inspect
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nonebot_plugin_moellmchats.audit_batch import AuditBatchPolicy, AuditBatchQueue
from nonebot_plugin_moellmchats.audit_event import AuditEventRecord
from nonebot_plugin_moellmchats.database_engine import (
    DatabaseEngineManager,
    DatabaseEngineSettings,
)
from nonebot_plugin_moellmchats.local_spool import LocalSpoolSettings, LocalUsageAuditSpool
from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord
from nonebot_plugin_moellmchats.platform_metrics import PlatformMetricsRegistry
from nonebot_plugin_moellmchats.spool_worker import (
    PostgresSpoolRecordWriter,
    SpoolWorkerDrainRequiredError,
    SpoolWorkerPolicy,
    SpoolWorkerResultUnknownError,
    SpoolWorkerState,
    SpoolWriteCommittedCleanupError,
    SpoolWriteResultUnknownError,
    SpoolWriteUnwrittenError,
    UsageAuditSpoolWorker,
)
from nonebot_plugin_moellmchats.usage_batch import UsageBatchPolicy, UsageBatchQueue

NOW = datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc)


def _usage(run_id: str = "run_1") -> ModelUsageRecord:
    return ModelUsageRecord(
        usage_id=None,
        run_id=run_id,
        provider="provider",
        model="model",
        input_tokens=10,
        output_tokens=4,
        reasoning_tokens=2,
        cached_tokens=1,
        cost=Decimal("0.1"),
        created_at=NOW,
    )


def _audit(event_type: str = "tool_bundle.approved") -> AuditEventRecord:
    return AuditEventRecord(
        event_id=None,
        event_type=event_type,
        actor_user_id=None,
        actor_type="system",
        target_type="runtime",
        target_id="runtime",
        run_id=None,
        tool_call_id=None,
        metadata_json={"generation": 7},
        created_at=NOW,
    )


class _Writer:
    def __init__(self, mode: str = "commit") -> None:
        self.mode = mode
        self.usage: list[tuple[ModelUsageRecord, ...]] = []
        self.audit: list[tuple[AuditEventRecord, ...]] = []
        self.usage_called = asyncio.Event()
        self.audit_called = asyncio.Event()

    def _finish(self) -> None:
        if self.mode == "unwritten":
            raise SpoolWriteUnwrittenError("definite rollback")
        if self.mode == "unknown":
            raise SpoolWriteResultUnknownError("commit unknown")
        if self.mode == "cleanup":
            raise SpoolWriteCommittedCleanupError("commit confirmed")

    async def write_usage(self, records: tuple[ModelUsageRecord, ...]) -> None:
        self.usage.append(records)
        self.usage_called.set()
        self._finish()

    async def write_audit(self, records: tuple[AuditEventRecord, ...]) -> None:
        self.audit.append(records)
        self.audit_called.set()
        self._finish()


class _BlockingCleanupSession(AsyncSession):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.rollback_started = asyncio.Event()
        self.rollback_release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def begin(self):
        self.events.append("begin")
        return super().begin()

    async def rollback(self) -> None:
        self.events.append("rollback.start")
        self.rollback_started.set()
        await self.rollback_release.wait()
        self.events.append("rollback.done")

    async def close(self) -> None:
        self.events.append("close.start")
        self.close_started.set()
        await self.close_release.wait()
        self.events.append("close.done")


def _worker(
    tmp_path: Path,
    writer: _Writer,
    *,
    generation: int = 7,
) -> UsageAuditSpoolWorker:
    return UsageAuditSpoolWorker(
        generation=generation,
        spool=LocalUsageAuditSpool(
            generation=generation,
            settings=LocalSpoolSettings(root=tmp_path / "spool"),
        ),
        usage_queue=UsageBatchQueue(
            UsageBatchPolicy(max_batch_size=1, flush_interval_seconds=0.001),
        ),
        audit_queue=AuditBatchQueue(
            AuditBatchPolicy(max_batch_size=1, flush_interval_seconds=0.001),
        ),
        writer=writer,
        metrics=PlatformMetricsRegistry(generation=generation),
        policy=SpoolWorkerPolicy(retry_delay_seconds=0.01),
    )


@pytest.mark.asyncio
async def test_worker_durably_enqueues_flushes_and_closes_both_kinds(tmp_path: Path) -> None:
    writer = _Writer()
    worker = _worker(tmp_path, writer)
    usage = _usage()
    audit = _audit()
    await worker.start()

    await worker.enqueue_usage((usage,))
    await worker.enqueue_audit((audit,))
    await asyncio.wait_for(writer.usage_called.wait(), timeout=1)
    await asyncio.wait_for(writer.audit_called.wait(), timeout=1)
    await worker.close()

    assert writer.usage == [(usage,)]
    assert writer.audit == [(audit,)]
    assert worker.state is SpoolWorkerState.CLOSED
    snapshot = worker.metrics.snapshot()
    assert snapshot.spool_enqueued_records == 2
    assert snapshot.spool_committed_records == 2
    assert snapshot.spool_failure_total == 0
    assert snapshot.spool_ready_files == 0
    assert snapshot.spool_leased_files == 0


@pytest.mark.asyncio
async def test_worker_pumps_existing_bounded_queue_into_durable_spool(tmp_path: Path) -> None:
    writer = _Writer()
    worker = _worker(tmp_path, writer)
    usage = _usage()
    await worker.start()

    await worker._usage_queue.put(usage)
    await asyncio.wait_for(writer.usage_called.wait(), timeout=1)
    await worker.close()

    assert writer.usage == [(usage,)]
    assert worker._usage_queue.outstanding_count == 0


@pytest.mark.asyncio
async def test_definite_rollback_returns_file_to_ready_and_close_fails_closed(tmp_path: Path) -> None:
    writer = _Writer("unwritten")
    worker = _worker(tmp_path, writer)
    await worker.start()
    await worker.enqueue_usage((_usage(),))
    await asyncio.wait_for(writer.usage_called.wait(), timeout=1)

    with pytest.raises(SpoolWorkerDrainRequiredError, match="durable"):
        await worker.close()

    root = tmp_path / "spool" / "generation-7"
    assert worker.state is SpoolWorkerState.FAILED
    assert len(tuple(root.glob("ready.usage.*.json"))) == 1
    assert not tuple(root.glob("leased.usage.*.json"))
    assert worker.metrics.snapshot().spool_failure_total >= 1
    worker.spool._release_generation_lock_sync()


@pytest.mark.asyncio
async def test_unknown_commit_is_quarantined_and_never_replayed(tmp_path: Path) -> None:
    writer = _Writer("unknown")
    worker = _worker(tmp_path, writer)
    await worker.start()
    await worker.enqueue_usage((_usage(),))
    await asyncio.wait_for(writer.usage_called.wait(), timeout=1)

    supervisor = worker._supervisor
    assert supervisor is not None
    with pytest.raises(SpoolWorkerResultUnknownError):
        await asyncio.wait_for(asyncio.shield(supervisor), timeout=1)
    assert worker.state is SpoolWorkerState.RESULT_UNKNOWN
    with pytest.raises(SpoolWorkerResultUnknownError):
        await worker.close()

    root = tmp_path / "spool" / "generation-7"
    assert len(tuple(root.glob("unknown.usage.*.json"))) == 1
    assert writer.usage == [(_usage(),)]
    assert worker.metrics.snapshot().spool_result_unknown_total == 1
    worker.spool._release_generation_lock_sync()


@pytest.mark.asyncio
async def test_confirmed_commit_with_cleanup_failure_is_acknowledged_once(tmp_path: Path) -> None:
    writer = _Writer("cleanup")
    worker = _worker(tmp_path, writer)
    await worker.start()
    await worker.enqueue_usage((_usage(),))
    await asyncio.wait_for(writer.usage_called.wait(), timeout=1)
    await worker.close()

    assert writer.usage == [(_usage(),)]
    snapshot = worker.metrics.snapshot()
    assert snapshot.spool_committed_records == 1
    assert snapshot.spool_failure_total == 1


@pytest.mark.asyncio
async def test_postgres_spool_repeated_cancellation_still_rolls_back_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DatabaseEngineManager(
        DatabaseEngineSettings(
            database_url="postgresql+asyncpg://local:local@db.invalid/local",
        )
    )
    session = _BlockingCleanupSession()
    writer = PostgresSpoolRecordWriter(
        manager,
        sessionmaker_factory=lambda *_args, **_kwargs: lambda: session,
    )

    async def cancelled_append(_repository, _records) -> None:
        session.events.append("append")
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.spool_worker.PostgresUsageRepository.append_batch",
        cancelled_append,
    )
    task = asyncio.create_task(writer.write_usage((_usage(),)))
    await asyncio.wait_for(session.rollback_started.wait(), timeout=1)
    task.cancel()
    session.rollback_release.set()
    await asyncio.wait_for(session.close_started.wait(), timeout=1)
    task.cancel()
    session.close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert session.events == [
        "begin",
        "append",
        "rollback.start",
        "rollback.done",
        "close.start",
        "close.done",
    ]
    await manager.dispose()


def test_spool_worker_module_has_no_import_time_worker_or_awaitable() -> None:
    module = importlib.reload(importlib.import_module("nonebot_plugin_moellmchats.spool_worker"))

    assert not any(isinstance(value, module.UsageAuditSpoolWorker) for value in vars(module).values())
    assert not any(inspect.isawaitable(value) for value in vars(module).values())

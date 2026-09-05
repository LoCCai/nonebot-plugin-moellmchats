from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
import inspect
import math
import time
from typing import Any, Protocol, TypeAlias, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import platform_metrics as _platform_metrics
from .audit_batch import AuditBatchQueue
from .audit_event import AuditEventRecord
from .compat import settle_awaitable
from .database_engine import DatabaseEngineManager
from .local_spool import (
    LocalSpoolDrainRequiredError,
    LocalSpoolKind,
    LocalSpoolLease,
    LocalSpoolResultUnknownError,
    LocalUsageAuditSpool,
)
from .model_usage import ModelUsageRecord
from .platform_metrics import (
    PlatformMetricsRecorder,
    PlatformMetricsRegistry,
)
from .postgres_audit_repository import PostgresAuditRepository
from .postgres_usage_repository import PostgresUsageRepository
from .usage_batch import UsageBatchQueue


class SpoolWorkerError(RuntimeError):
    """Base error for the durable Usage/Audit flush lifecycle."""


class SpoolWorkerConfigurationError(SpoolWorkerError):
    """Injected generation resources do not form a safe worker."""


class SpoolWorkerLifecycleError(SpoolWorkerError):
    """A worker operation does not match its explicit lifecycle."""


class SpoolWorkerDrainRequiredError(SpoolWorkerError):
    """Durable records remain and cannot be silently discarded."""


class SpoolWorkerResultUnknownError(SpoolWorkerDrainRequiredError):
    """A database result is unknown and automatic replay is forbidden."""


class SpoolWriteError(RuntimeError):
    """Base contract error from one database spool write."""


class SpoolWriteUnwrittenError(SpoolWriteError):
    """No durable write occurred, or rollback was definitively confirmed."""


class SpoolWriteResultUnknownError(SpoolWriteError):
    """Commit was attempted but its durable result cannot be confirmed."""


class SpoolWriteCancellationUnknownError(
    SpoolWriteResultUnknownError,
    asyncio.CancelledError,
):
    """Cancellation interrupted commit and therefore also carries unknown-result semantics."""


class SpoolWriteCancellationCleanupError(
    SpoolWriteError,
    asyncio.CancelledError,
):
    """Cancellation remains primary, but pre-commit cleanup was not confirmed."""


class SpoolWriteCommittedCleanupError(SpoolWriteError):
    """Commit succeeded, but transaction resource cleanup failed."""


class SpoolWorkerState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True)
class SpoolWorkerPolicy:
    retry_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if (
            isinstance(self.retry_delay_seconds, bool)
            or not isinstance(self.retry_delay_seconds, (int, float))
            or not math.isfinite(float(self.retry_delay_seconds))
            or not 0.001 <= float(self.retry_delay_seconds) <= 60.0
        ):
            raise ValueError("retry_delay_seconds 必须是 0.001 到 60 秒的有限数值")
        object.__setattr__(self, "retry_delay_seconds", float(self.retry_delay_seconds))


@runtime_checkable
class SpoolRecordWriter(Protocol):
    async def write_usage(self, records: tuple[ModelUsageRecord, ...]) -> None: ...

    async def write_audit(self, records: tuple[AuditEventRecord, ...]) -> None: ...


SessionMakerFactory = Callable[..., Any]
SpoolRecord: TypeAlias = ModelUsageRecord | AuditEventRecord


def _safe_error_type(error: BaseException) -> str:
    name = type(error).__name__
    if name.isidentifier() and len(name) <= 128:
        return name
    return "BackendError"


class PostgresSpoolRecordWriter(SpoolRecordWriter):
    """Write one leased spool file in one caller-owned short transaction."""

    def __init__(
        self,
        manager: DatabaseEngineManager,
        *,
        sessionmaker_factory: SessionMakerFactory = async_sessionmaker,
    ) -> None:
        if not isinstance(manager, DatabaseEngineManager):
            raise TypeError("manager 必须是 DatabaseEngineManager")
        if not callable(sessionmaker_factory) or inspect.iscoroutinefunction(sessionmaker_factory):
            raise TypeError("sessionmaker_factory 必须是同步 callable")
        self._manager = manager
        self._sessionmaker_factory = sessionmaker_factory
        self._session_factory: Callable[[], object] | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(initialized={self._session_factory is not None!r})"

    def _factory(self) -> Callable[[], object]:
        factory = self._session_factory
        if factory is not None:
            return factory
        try:
            engine = self._manager.get_engine()
            candidate = self._sessionmaker_factory(engine, expire_on_commit=False)
        except Exception as error:
            raise SpoolWriteUnwrittenError(f"PostgreSQL spool session factory 初始化失败 ({_safe_error_type(error)})") from None
        if not callable(candidate):
            raise SpoolWorkerConfigurationError("sessionmaker_factory 返回非法对象")
        self._session_factory = candidate
        return candidate

    @staticmethod
    async def _cleanup_session(
        session: AsyncSession,
        *,
        rollback: bool,
    ) -> tuple[tuple[BaseException, ...], bool]:
        errors: list[BaseException] = []
        interrupted = False
        if rollback:
            rollback_outcome = await settle_awaitable(session.rollback())
            interrupted = rollback_outcome.interrupted
            if rollback_outcome.error is not None:
                errors.append(rollback_outcome.error)
        close_outcome = await settle_awaitable(session.close())
        interrupted = interrupted or close_outcome.interrupted
        if close_outcome.error is not None:
            errors.append(close_outcome.error)
        return tuple(errors), interrupted

    @staticmethod
    def _cleanup_suffix(errors: tuple[BaseException, ...]) -> str:
        if not errors:
            return ""
        kinds = ",".join(_safe_error_type(error) for error in errors)
        return f"；cleanup 未确认 ({kinds})"

    async def _write(
        self,
        kind: LocalSpoolKind,
        records: tuple[SpoolRecord, ...],
    ) -> None:
        try:
            session = self._factory()()
        except SpoolWriteError:
            raise
        except Exception as error:
            raise SpoolWriteUnwrittenError(f"PostgreSQL spool session 创建失败 ({_safe_error_type(error)})") from None
        if not isinstance(session, AsyncSession):
            raise SpoolWorkerConfigurationError("spool session factory 未返回 AsyncSession")

        try:
            await session.begin()
            if kind is LocalSpoolKind.USAGE:
                usage_records = tuple(record for record in records if isinstance(record, ModelUsageRecord))
                if len(usage_records) != len(records):
                    raise SpoolWorkerConfigurationError("usage spool records 类型漂移")
                await PostgresUsageRepository(session).append_batch(usage_records)
            else:
                audit_records = tuple(record for record in records if isinstance(record, AuditEventRecord))
                if len(audit_records) != len(records):
                    raise SpoolWorkerConfigurationError("audit spool records 类型漂移")
                repository = PostgresAuditRepository(session)
                if all(record.batchable for record in audit_records):
                    await repository.append_batch(audit_records)
                else:
                    for record in audit_records:
                        await repository.append(record)
        except asyncio.CancelledError:
            cleanup_errors, _ = await self._cleanup_session(
                session,
                rollback=True,
            )
            if cleanup_errors:
                raise SpoolWriteCancellationCleanupError(
                    "PostgreSQL spool transaction 被取消且 cleanup 未确认"
                    + self._cleanup_suffix(cleanup_errors)
                ) from None
            raise
        except SpoolWorkerConfigurationError:
            await self._cleanup_session(session, rollback=True)
            raise
        except Exception as error:
            cleanup_errors, _ = await self._cleanup_session(
                session,
                rollback=True,
            )
            raise SpoolWriteUnwrittenError(
                f"PostgreSQL spool transaction 未写入 ({_safe_error_type(error)})"
                + self._cleanup_suffix(cleanup_errors)
            ) from None

        try:
            await session.commit()
        except asyncio.CancelledError as error:
            cleanup_errors, _ = await self._cleanup_session(
                session,
                rollback=False,
            )
            raise SpoolWriteCancellationUnknownError(
                f"PostgreSQL spool commit 被取消且结果不可确认 ({_safe_error_type(error)})"
                + self._cleanup_suffix(cleanup_errors)
            ) from None
        except Exception as error:
            cleanup_errors, _ = await self._cleanup_session(
                session,
                rollback=False,
            )
            raise SpoolWriteResultUnknownError(
                f"PostgreSQL spool commit 结果不可确认 ({_safe_error_type(error)})"
                + self._cleanup_suffix(cleanup_errors)
            ) from None

        cleanup_errors, interrupted = await self._cleanup_session(
            session,
            rollback=False,
        )
        if interrupted:
            raise asyncio.CancelledError
        if cleanup_errors:
            raise SpoolWriteCommittedCleanupError(
                "PostgreSQL spool commit 已确认但 session 关闭失败"
                + self._cleanup_suffix(cleanup_errors)
            ) from None

    async def write_usage(self, records: tuple[ModelUsageRecord, ...]) -> None:
        if not isinstance(records, tuple) or not records:
            raise ValueError("records 必须是非空 usage 元组")
        await self._write(LocalSpoolKind.USAGE, records)

    async def write_audit(self, records: tuple[AuditEventRecord, ...]) -> None:
        if not isinstance(records, tuple) or not records:
            raise ValueError("records 必须是非空 audit 元组")
        await self._write(LocalSpoolKind.AUDIT, records)


class UsageAuditSpoolWorker:
    """Generation-local queue-to-spool and spool-to-database worker."""

    def __init__(
        self,
        *,
        generation: int,
        spool: LocalUsageAuditSpool,
        usage_queue: UsageBatchQueue,
        audit_queue: AuditBatchQueue,
        writer: SpoolRecordWriter,
        metrics: PlatformMetricsRegistry,
        policy: SpoolWorkerPolicy = SpoolWorkerPolicy(),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            raise ValueError("generation 必须是正整数")
        if not isinstance(spool, LocalUsageAuditSpool) or spool.generation != generation:
            raise SpoolWorkerConfigurationError("spool generation 不一致")
        if not isinstance(usage_queue, UsageBatchQueue):
            raise TypeError("usage_queue 必须是 UsageBatchQueue")
        if not isinstance(audit_queue, AuditBatchQueue):
            raise TypeError("audit_queue 必须是 AuditBatchQueue")
        if not isinstance(writer, SpoolRecordWriter):
            raise TypeError("writer 必须实现 SpoolRecordWriter")
        if not isinstance(metrics, PlatformMetricsRecorder) or metrics.generation != generation:
            raise SpoolWorkerConfigurationError("platform metrics generation 不一致")
        policy_type = type(policy)
        if not isinstance(policy, SpoolWorkerPolicy) and not (
            policy_type.__module__ == __name__ and policy_type.__qualname__ == SpoolWorkerPolicy.__qualname__
        ):
            raise TypeError("policy 必须是 SpoolWorkerPolicy")
        if not callable(monotonic) or inspect.iscoroutinefunction(monotonic):
            raise TypeError("monotonic 必须是同步 callable")
        self._generation = generation
        self._spool = spool
        self._usage_queue = usage_queue
        self._audit_queue = audit_queue
        self._writer = writer
        self._metrics = metrics
        self._policy = SpoolWorkerPolicy(
            retry_delay_seconds=policy.retry_delay_seconds,
        )
        self._monotonic = monotonic
        self._state = SpoolWorkerState.CREATED
        self._lifecycle_lock = asyncio.Lock()
        self._closing_event: asyncio.Event | None = None
        self._ingest_done: asyncio.Event | None = None
        self._flush_event: asyncio.Event | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._terminal_error: BaseException | None = None
        self._next_kind = LocalSpoolKind.USAGE

    def __repr__(self) -> str:
        return f"{type(self).__name__}(generation={self.generation!r}, state={self.state.value!r})"

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def state(self) -> SpoolWorkerState:
        return self._state

    @property
    def spool(self) -> LocalUsageAuditSpool:
        return self._spool

    @property
    def metrics(self) -> PlatformMetricsRegistry:
        return self._metrics

    def safe_diagnostics(self) -> dict[str, int | str]:
        diagnostics = self._spool.safe_diagnostics()
        return {
            "generation": self.generation,
            "state": self.state.value,
            "ready_files": int(diagnostics["ready_files"]),
            "leased_files": int(diagnostics["leased_files"]),
            "result_unknown_files": int(diagnostics["result_unknown_files"]),
        }

    def _events(self) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
        if self._closing_event is None or self._ingest_done is None or self._flush_event is None:
            raise SpoolWorkerLifecycleError("spool worker 尚未启动")
        return self._closing_event, self._ingest_done, self._flush_event

    def _now(self) -> float:
        try:
            value = self._monotonic()
        except Exception as error:
            raise SpoolWorkerConfigurationError(f"spool worker clock 失败 ({_safe_error_type(error)})") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
            raise SpoolWorkerConfigurationError("spool worker clock 返回非法值")
        return float(value)

    def _metric(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except Exception:
            return

    def _refresh_gauges(self) -> None:
        diagnostics = self._spool.safe_diagnostics()
        self._metric(
            lambda: self._metrics.set_spool_gauges(
                ready_files=int(diagnostics["ready_files"]),
                leased_files=int(diagnostics["leased_files"]),
                result_unknown_files=int(diagnostics["result_unknown_files"]),
            )
        )

    def _observe_flush_duration(self, started_at: float) -> None:
        duration = max(0.0, self._now() - started_at)
        self._metric(
            lambda: self._metrics.observe_duration(
                _platform_metrics.PlatformDurationMetric.SPOOL_FLUSH_DURATION,
                duration,
            )
        )

    async def _append_usage(self, records: tuple[ModelUsageRecord, ...]) -> None:
        await self._spool.append_usage(records)
        self._metric(
            lambda: self._metrics.increment(
                _platform_metrics.PlatformCountMetric.SPOOL_ENQUEUED_RECORDS,
                len(records),
            )
        )
        self._refresh_gauges()
        self._events()[2].set()

    async def _append_audit(self, records: tuple[AuditEventRecord, ...]) -> None:
        await self._spool.append_audit(records)
        self._metric(
            lambda: self._metrics.increment(
                _platform_metrics.PlatformCountMetric.SPOOL_ENQUEUED_RECORDS,
                len(records),
            )
        )
        self._refresh_gauges()
        self._events()[2].set()

    async def enqueue_usage(self, records: tuple[ModelUsageRecord, ...]) -> None:
        if self._state is not SpoolWorkerState.RUNNING:
            raise SpoolWorkerLifecycleError("spool worker 当前不接受 usage")
        await self._append_usage(records)

    async def enqueue_audit(self, records: tuple[AuditEventRecord, ...]) -> None:
        if self._state is not SpoolWorkerState.RUNNING:
            raise SpoolWorkerLifecycleError("spool worker 当前不接受 audit")
        await self._append_audit(records)

    async def _pump_usage(self) -> None:
        while True:
            lease = await self._usage_queue.wait_for_lease()
            if lease is None:
                return
            try:
                await self._append_usage(lease.records)
            except BaseException:
                await self._usage_queue.release_unwritten(lease)
                raise
            await self._usage_queue.acknowledge_committed(lease)

    async def _pump_audit(self) -> None:
        while True:
            lease = await self._audit_queue.wait_for_lease()
            if lease is None:
                return
            try:
                await self._append_audit(lease.records)
            except BaseException:
                await self._audit_queue.release_unwritten(lease)
                raise
            await self._audit_queue.acknowledge_committed(lease)

    async def _run_ingesters(self) -> None:
        _closing, ingest_done, flush_event = self._events()
        tasks = (
            asyncio.create_task(self._pump_usage(), name=f"moellm-spool-usage-{self.generation}"),
            asyncio.create_task(self._pump_audit(), name=f"moellm-spool-audit-{self.generation}"),
        )
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            ingest_done.set()
            flush_event.set()

    async def _mark_unknown(self, lease: LocalSpoolLease) -> None:
        try:
            await self._spool.mark_result_unknown(lease)
        except (ValueError, LocalSpoolDrainRequiredError):
            pass
        self._metric(lambda: self._metrics.increment(_platform_metrics.PlatformCountMetric.SPOOL_RESULT_UNKNOWN_TOTAL))
        self._refresh_gauges()

    async def _retry_or_stop(self) -> None:
        closing, _ingest_done, flush_event = self._events()
        if closing.is_set():
            raise SpoolWorkerDrainRequiredError("关闭期间数据库仍不可用；durable spool 已保留")
        try:
            await asyncio.wait_for(closing.wait(), timeout=self._policy.retry_delay_seconds)
        except asyncio.TimeoutError:
            flush_event.set()
            return
        raise SpoolWorkerDrainRequiredError("关闭期间数据库仍不可用；durable spool 已保留")

    async def _flush_lease(self, lease: LocalSpoolLease) -> None:
        started_at = self._now()
        try:
            if lease.kind is LocalSpoolKind.USAGE:
                usage_records = tuple(record for record in lease.records if isinstance(record, ModelUsageRecord))
                if len(usage_records) != len(lease.records):
                    raise SpoolWorkerConfigurationError("usage lease 类型漂移")
                await self._writer.write_usage(usage_records)
            else:
                audit_records = tuple(record for record in lease.records if isinstance(record, AuditEventRecord))
                if len(audit_records) != len(lease.records):
                    raise SpoolWorkerConfigurationError("audit lease 类型漂移")
                await self._writer.write_audit(audit_records)
        except SpoolWriteCommittedCleanupError:
            self._metric(lambda: self._metrics.increment(_platform_metrics.PlatformCountMetric.SPOOL_FAILURE_TOTAL))
            await self._spool.acknowledge_committed(lease)
        except SpoolWriteCancellationUnknownError as error:
            await self._mark_unknown(lease)
            raise SpoolWorkerResultUnknownError("spool commit cancellation result unknown") from error
        except SpoolWriteResultUnknownError as error:
            await self._mark_unknown(lease)
            raise SpoolWorkerResultUnknownError("spool commit result unknown") from error
        except SpoolWriteUnwrittenError:
            self._metric(lambda: self._metrics.increment(_platform_metrics.PlatformCountMetric.SPOOL_FAILURE_TOTAL))
            await self._spool.release_unwritten(lease)
            self._refresh_gauges()
            await self._retry_or_stop()
            return
        except asyncio.CancelledError:
            await self._spool.release_unwritten(lease)
            self._refresh_gauges()
            raise
        except Exception as error:
            await self._mark_unknown(lease)
            raise SpoolWorkerResultUnknownError(f"spool writer 未声明 durable 结果 ({_safe_error_type(error)})") from None
        else:
            await self._spool.acknowledge_committed(lease)
        finally:
            self._observe_flush_duration(started_at)

        self._metric(
            lambda: self._metrics.increment(
                _platform_metrics.PlatformCountMetric.SPOOL_COMMITTED_RECORDS,
                len(lease.records),
            )
        )
        self._refresh_gauges()

    async def _flush_loop(self) -> None:
        _closing, ingest_done, flush_event = self._events()
        while True:
            flush_event.clear()
            kinds = (
                self._next_kind,
                LocalSpoolKind.AUDIT if self._next_kind is LocalSpoolKind.USAGE else LocalSpoolKind.USAGE,
            )
            processed = False
            for kind in kinds:
                lease = await self._spool.lease_next(kind)
                if lease is None:
                    continue
                self._next_kind = LocalSpoolKind.AUDIT if kind is LocalSpoolKind.USAGE else LocalSpoolKind.USAGE
                self._refresh_gauges()
                await self._flush_lease(lease)
                processed = True
                break
            if processed:
                continue
            if ingest_done.is_set():
                return
            await flush_event.wait()

    async def _supervise(self) -> None:
        ingesters = asyncio.create_task(self._run_ingesters(), name=f"moellm-spool-ingest-{self.generation}")
        flusher = asyncio.create_task(self._flush_loop(), name=f"moellm-spool-flush-{self.generation}")
        tasks = (ingesters, flusher)
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            failure: BaseException | None = None
            for task in done:
                if task.cancelled():
                    failure = asyncio.CancelledError()
                    break
                error = task.exception()
                if error is not None:
                    failure = error
                    break
            if failure is not None:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise failure
            await asyncio.gather(*pending)
        except SpoolWorkerResultUnknownError as error:
            self._terminal_error = error
            self._state = SpoolWorkerState.RESULT_UNKNOWN
            raise
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except BaseException as error:
            self._terminal_error = error
            self._state = SpoolWorkerState.FAILED
            raise

    async def start(self) -> UsageAuditSpoolWorker:
        async with self._lifecycle_lock:
            if self._state is SpoolWorkerState.RUNNING:
                return self
            if self._state is not SpoolWorkerState.CREATED:
                raise SpoolWorkerLifecycleError("spool worker 当前不可启动")
            self._state = SpoolWorkerState.STARTING
            try:
                await self._spool.start()
            except BaseException:
                self._state = SpoolWorkerState.FAILED
                raise
            self._closing_event = asyncio.Event()
            self._ingest_done = asyncio.Event()
            self._flush_event = asyncio.Event()
            self._flush_event.set()
            self._refresh_gauges()
            self._supervisor = asyncio.create_task(
                self._supervise(),
                name=f"moellm-spool-worker-{self.generation}",
            )
            self._state = SpoolWorkerState.RUNNING
            return self

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._state is SpoolWorkerState.CLOSED:
                return
            if self._state is SpoolWorkerState.CREATED:
                await self._spool.close()
                self._state = SpoolWorkerState.CLOSED
                return
            if self._state is SpoolWorkerState.STARTING:
                raise SpoolWorkerLifecycleError("spool worker 正在启动")
            if self._state is SpoolWorkerState.RESULT_UNKNOWN:
                supervisor = self._supervisor
                if supervisor is not None and supervisor.done():
                    try:
                        supervisor.result()
                    except BaseException:
                        pass
                raise SpoolWorkerResultUnknownError("spool worker durable result unknown")

            retry_failed_close = self._state is SpoolWorkerState.FAILED
            self._state = SpoolWorkerState.CLOSING
            closing, _ingest_done, flush_event = self._events()
            closing.set()
            flush_event.set()
            await self._usage_queue.begin_close()
            await self._audit_queue.begin_close()
            if retry_failed_close:
                self._ingest_done = asyncio.Event()
                self._flush_event = asyncio.Event()
                self._flush_event.set()
                self._terminal_error = None
                self._supervisor = asyncio.create_task(
                    self._supervise(),
                    name=f"moellm-spool-worker-retry-{self.generation}",
                )
            supervisor = self._supervisor
            if supervisor is None:
                self._state = SpoolWorkerState.FAILED
                raise SpoolWorkerLifecycleError("spool worker supervisor 缺失")
            try:
                await asyncio.shield(supervisor)
            except asyncio.CancelledError:
                if not supervisor.done():
                    await asyncio.shield(supervisor)
                raise
            except BaseException as error:
                if isinstance(error, SpoolWorkerResultUnknownError):
                    self._state = SpoolWorkerState.RESULT_UNKNOWN
                    raise
                self._state = SpoolWorkerState.FAILED
                raise SpoolWorkerDrainRequiredError(
                    f"spool worker 未能排空 durable records ({_safe_error_type(error)})"
                ) from None

            try:
                await self._spool.close()
            except LocalSpoolResultUnknownError as error:
                self._state = SpoolWorkerState.RESULT_UNKNOWN
                raise SpoolWorkerResultUnknownError("spool worker durable result unknown") from error
            except LocalSpoolDrainRequiredError as error:
                self._state = SpoolWorkerState.FAILED
                raise SpoolWorkerDrainRequiredError("spool worker 仍有 durable records") from error
            self._refresh_gauges()
            self._state = SpoolWorkerState.CLOSED


__all__ = [
    "PostgresSpoolRecordWriter",
    "SpoolRecordWriter",
    "SpoolWorkerConfigurationError",
    "SpoolWorkerDrainRequiredError",
    "SpoolWorkerError",
    "SpoolWorkerLifecycleError",
    "SpoolWorkerPolicy",
    "SpoolWorkerResultUnknownError",
    "SpoolWorkerState",
    "SpoolWriteCancellationCleanupError",
    "SpoolWriteCancellationUnknownError",
    "SpoolWriteCommittedCleanupError",
    "SpoolWriteError",
    "SpoolWriteResultUnknownError",
    "SpoolWriteUnwrittenError",
    "UsageAuditSpoolWorker",
]

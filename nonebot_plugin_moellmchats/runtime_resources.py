from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
import inspect
import os
import re
from typing import Protocol, TypeVar, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from .audit_batch import AuditBatchPolicy, AuditBatchQueue, AuditBatchQueueState
from .classification_cache import (
    ClassificationCacheProtocol,
    MemoryClassificationCache,
    MemoryClassificationCacheSettings,
)
from .database_engine import (
    DatabaseEngineManager,
    DatabaseEngineSettings,
)
from .full_metrics import FullMetricsRegistry
from .history_hot_cache import (
    HistoryCacheLoadToken,
    HistoryCacheLookup,
    HistoryHotCacheProtocol,
    HistoryWindow,
    MemoryHistoryHotCache,
    MemoryHistoryHotCacheSettings,
)
from .postgres_agent_repository import (
    PostgresAgentRunRepository,
    PostgresAgentStepRepository,
    PostgresToolCallRepository,
)
from .postgres_audit_repository import PostgresAuditRepository
from .postgres_history_repository import (
    PostgresConversationRepository,
    PostgresMessageRepository,
    PostgresUserRepository,
)
from .postgres_session_summary_repository import (
    PostgresSessionSummaryRepository,
)
from .postgres_usage_repository import PostgresUsageRepository
from .redis_client import RedisClientManager, RedisClientSettings
from .redis_history_hot_cache import (
    RedisHistoryHotCache,
    RedisHistoryHotCacheSettings,
)
from .runtime_api import RuntimeApiHandler
from .runtime_snapshot import RuntimeSnapshot
from .structured_logging import StructuredLogEmitter, StructuredLogSink
from .tool_catalog_cache import (
    MemoryToolCatalogCache,
    MemoryToolCatalogCacheSettings,
    ToolCatalogCacheProtocol,
)
from .tool_graph import ToolGraph
from .tool_schema_cache import (
    MemoryToolSchemaCache,
    MemoryToolSchemaCacheSettings,
    ToolSchemaCacheProtocol,
)
from .trusted_runner_pool import (
    TrustedRunnerPool,
    TrustedRunnerPoolPolicy,
)
from .usage_batch import UsageBatchPolicy, UsageBatchQueue, UsageBatchQueueState

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_LIFECYCLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


class RuntimeResourceError(RuntimeError):
    """Base error for one explicit runtime-resource lifecycle."""


class RuntimeResourceConfigurationError(RuntimeResourceError):
    """The requested backend or port composition is not safe to construct."""


class RuntimeResourceLifecycleError(RuntimeResourceError):
    """A resource operation does not match the current lifecycle state."""


class RuntimeResourceStartupError(RuntimeResourceError):
    """A generation failed to start and its partial resources were rolled back."""


class RuntimeResourceReloadError(RuntimeResourceError):
    """A replacement generation could not complete a safe handoff."""


class RuntimeResourceCloseError(RuntimeResourceError):
    """A generation could not close every explicitly owned resource."""


class RuntimeResourceDrainRequiredError(RuntimeResourceCloseError):
    """A queue still contains records whose durable result must not be guessed."""


class RuntimeResourceOwnershipError(RuntimeResourceError):
    """A manager was reused across its bound process or event loop."""


class RuntimeResourceGenerationError(RuntimeResourceError):
    """A requested snapshot does not advance the active generation."""


class RuntimeResourceHistoryBackend(str, Enum):
    MEMORY = "memory"
    REDIS = "redis"


class RuntimeGenerationResourceState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class RuntimeResourceManagerState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    RELOADING = "reloading"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


def _validate_generation(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT")
    return value


def _validate_factory(value: object, *, label: str) -> None:
    if not callable(value) or inspect.iscoroutinefunction(value):
        raise TypeError(f"{label} 必须是同步 factory")


@dataclass(frozen=True, repr=False)
class RuntimeResourceSettings:
    """Explicit, validated settings for one generation's replaceable resources.

    A missing database/Redis settings object means the backend is disabled.  No
    ambient environment variable or plugin config is inspected here.
    """

    database: DatabaseEngineSettings | None = None
    redis: RedisClientSettings | None = None
    history_backend: RuntimeResourceHistoryBackend = RuntimeResourceHistoryBackend.MEMORY
    memory_history: MemoryHistoryHotCacheSettings = field(default_factory=MemoryHistoryHotCacheSettings)
    redis_history: RedisHistoryHotCacheSettings = field(default_factory=RedisHistoryHotCacheSettings)
    tool_catalog: MemoryToolCatalogCacheSettings = field(default_factory=MemoryToolCatalogCacheSettings)
    tool_schema: MemoryToolSchemaCacheSettings = field(default_factory=MemoryToolSchemaCacheSettings)
    classification: MemoryClassificationCacheSettings = field(default_factory=MemoryClassificationCacheSettings)
    usage_batch: UsageBatchPolicy = field(default_factory=UsageBatchPolicy)
    audit_batch: AuditBatchPolicy = field(default_factory=AuditBatchPolicy)
    trusted_runner_tools: tuple[str, ...] = ()
    trusted_runner_policy: TrustedRunnerPoolPolicy = field(default_factory=TrustedRunnerPoolPolicy)
    parallel_tool_graph: ToolGraph | None = None

    def __post_init__(self) -> None:
        if self.database is not None and not isinstance(
            self.database,
            DatabaseEngineSettings,
        ):
            raise TypeError("database 必须是 DatabaseEngineSettings 或 None")
        if self.redis is not None and not isinstance(
            self.redis,
            RedisClientSettings,
        ):
            raise TypeError("redis 必须是 RedisClientSettings 或 None")
        if not isinstance(self.history_backend, RuntimeResourceHistoryBackend):
            raise TypeError("history_backend 必须是 RuntimeResourceHistoryBackend")
        if self.history_backend is RuntimeResourceHistoryBackend.REDIS and self.redis is None:
            raise RuntimeResourceConfigurationError("Redis history backend 必须显式提供 RedisClientSettings")
        for value, expected_type, label in (
            (
                self.memory_history,
                MemoryHistoryHotCacheSettings,
                "memory_history",
            ),
            (
                self.redis_history,
                RedisHistoryHotCacheSettings,
                "redis_history",
            ),
            (
                self.tool_catalog,
                MemoryToolCatalogCacheSettings,
                "tool_catalog",
            ),
            (
                self.tool_schema,
                MemoryToolSchemaCacheSettings,
                "tool_schema",
            ),
            (
                self.classification,
                MemoryClassificationCacheSettings,
                "classification",
            ),
            (self.usage_batch, UsageBatchPolicy, "usage_batch"),
            (self.audit_batch, AuditBatchPolicy, "audit_batch"),
            (
                self.trusted_runner_policy,
                TrustedRunnerPoolPolicy,
                "trusted_runner_policy",
            ),
        ):
            if not isinstance(value, expected_type):
                raise TypeError(f"{label} 类型非法")
        if not isinstance(self.trusted_runner_tools, tuple):
            raise TypeError("trusted_runner_tools 必须是元组")
        if any(
            not isinstance(tool_name, str) or not _TOOL_NAME_RE.fullmatch(tool_name) for tool_name in self.trusted_runner_tools
        ):
            raise ValueError("trusted_runner_tools 包含非法工具名")
        if len(set(self.trusted_runner_tools)) != len(self.trusted_runner_tools):
            raise ValueError("trusted_runner_tools 不得重复")
        object.__setattr__(
            self,
            "trusted_runner_tools",
            tuple(sorted(self.trusted_runner_tools)),
        )
        if self.parallel_tool_graph is not None and not isinstance(
            self.parallel_tool_graph,
            ToolGraph,
        ):
            raise TypeError("parallel_tool_graph 必须是 ToolGraph 或 None")
        if self.parallel_tool_graph is not None:
            if not self.trusted_runner_tools:
                raise RuntimeResourceConfigurationError("parallel_tool_graph 必须显式配置 trusted_runner_tools")
            missing_tools = set(self.trusted_runner_tools) - set(self.parallel_tool_graph.tools)
            if missing_tools:
                raise RuntimeResourceConfigurationError(
                    "parallel_tool_graph 缺少 trusted runner 工具: " + ", ".join(sorted(missing_tools))
                )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"database_configured={self.database is not None!r}, "
            f"redis_configured={self.redis is not None!r}, "
            f"history_backend={self.history_backend.value!r}, "
            f"trusted_runner_tools={len(self.trusted_runner_tools)!r})"
        )

    def safe_diagnostics(self) -> dict[str, bool | int | str]:
        return {
            "database_configured": self.database is not None,
            "redis_configured": self.redis is not None,
            "history_backend": self.history_backend.value,
            "trusted_runner_tool_count": len(self.trusted_runner_tools),
            "parallel_tool_graph_configured": self.parallel_tool_graph is not None,
        }


@dataclass(frozen=True)
class RuntimePostgresRepositories:
    """One detached repository set bound to one caller-owned AsyncSession."""

    user: PostgresUserRepository
    conversation: PostgresConversationRepository
    message: PostgresMessageRepository
    session_summary: PostgresSessionSummaryRepository
    agent_run: PostgresAgentRunRepository
    agent_step: PostgresAgentStepRepository
    tool_call: PostgresToolCallRepository
    usage: PostgresUsageRepository
    audit: PostgresAuditRepository


class PostgresRuntimeRepositoryProvider:
    """Construct repositories without creating or owning a session/transaction."""

    def for_session(
        self,
        session: AsyncSession,
    ) -> RuntimePostgresRepositories:
        if not isinstance(session, AsyncSession):
            raise TypeError("session 必须是调用方显式持有的 AsyncSession")
        return RuntimePostgresRepositories(
            user=PostgresUserRepository(session),
            conversation=PostgresConversationRepository(session),
            message=PostgresMessageRepository(session),
            session_summary=PostgresSessionSummaryRepository(session),
            agent_run=PostgresAgentRunRepository(session),
            agent_step=PostgresAgentStepRepository(session),
            tool_call=PostgresToolCallRepository(session),
            usage=PostgresUsageRepository(session),
            audit=PostgresAuditRepository(session),
        )


@dataclass(frozen=True)
class RuntimeApiPorts:
    """Generation-local API handlers; mounting remains an I-08 concern."""

    handlers: tuple[RuntimeApiHandler, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.handlers, tuple):
            raise TypeError("RuntimeApiPorts.handlers 必须是元组")
        if any(not isinstance(handler, RuntimeApiHandler) for handler in self.handlers):
            raise TypeError("RuntimeApiPorts.handlers 包含非法 handler")
        if len({id(handler) for handler in self.handlers}) != len(self.handlers):
            raise ValueError("RuntimeApiPorts.handlers 不得重复")


@runtime_checkable
class RuntimeLifecyclePort(Protocol):
    """One generation-local resource with deterministic start/close hooks."""

    @property
    def lifecycle_name(self) -> str: ...

    async def start(self, generation: int) -> None: ...

    async def close(self) -> None: ...


def _validate_lifecycle_port(port: object) -> RuntimeLifecyclePort:
    if not isinstance(port, RuntimeLifecyclePort):
        raise TypeError("managed port 必须实现 RuntimeLifecyclePort")
    name = port.lifecycle_name
    if not isinstance(name, str) or not _LIFECYCLE_NAME_RE.fullmatch(name):
        raise ValueError("managed port lifecycle_name 非法")
    if not inspect.iscoroutinefunction(port.start) or not inspect.iscoroutinefunction(port.close):
        raise TypeError("managed port start/close 必须是 async 方法")
    return port


class LazyRedisHistoryHotCache(HistoryHotCacheProtocol):
    """Create the Redis cache/client only on the first explicit cache operation."""

    def __init__(
        self,
        manager: RedisClientManager,
        *,
        settings: RedisHistoryHotCacheSettings,
    ) -> None:
        if not isinstance(manager, RedisClientManager):
            raise TypeError("manager 必须是 RedisClientManager")
        if not isinstance(settings, RedisHistoryHotCacheSettings):
            raise TypeError("settings 必须是 RedisHistoryHotCacheSettings")
        self._manager = manager
        self._settings = settings
        self._cache: RedisHistoryHotCache | None = None

    @property
    def initialized(self) -> bool:
        return self._cache is not None

    def _get_cache(self) -> RedisHistoryHotCache:
        cache = self._cache
        if cache is None:
            cache = RedisHistoryHotCache(
                self._manager.get_client(),
                settings=self._settings,
            )
            self._cache = cache
        return cache

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        return {
            "backend": "redis",
            "configured": True,
            "initialized": self.initialized,
            **self._settings.safe_diagnostics(),
        }

    async def lookup(
        self,
        conversation_id: str,
        *,
        limit: int,
    ) -> HistoryCacheLookup:
        return await self._get_cache().lookup(conversation_id, limit=limit)

    async def publish(
        self,
        load_token: HistoryCacheLoadToken,
        window: HistoryWindow,
    ) -> bool:
        return await self._get_cache().publish(load_token, window)

    async def invalidate(self, conversation_id: str) -> None:
        await self._get_cache().invalidate(conversation_id)


class _DatabaseLifecyclePort:
    lifecycle_name = "database-manager"

    def __init__(self, manager: DatabaseEngineManager) -> None:
        self._manager = manager

    async def start(self, generation: int) -> None:
        _validate_generation(generation, label="generation")

    async def close(self) -> None:
        await self._manager.dispose()


class _RedisLifecyclePort:
    lifecycle_name = "redis-manager"

    def __init__(self, manager: RedisClientManager) -> None:
        self._manager = manager

    async def start(self, generation: int) -> None:
        _validate_generation(generation, label="generation")

    async def close(self) -> None:
        await self._manager.aclose()


class _UsageQueueLifecyclePort:
    lifecycle_name = "usage-queue"

    def __init__(self, queue: UsageBatchQueue) -> None:
        self._queue = queue

    async def start(self, generation: int) -> None:
        _validate_generation(generation, label="generation")

    async def close(self) -> None:
        await self._queue.begin_close()
        if self._queue.state is not UsageBatchQueueState.CLOSED:
            raise RuntimeResourceDrainRequiredError("usage queue 仍含未确认 durable 结果")


class _AuditQueueLifecyclePort:
    lifecycle_name = "audit-queue"

    def __init__(self, queue: AuditBatchQueue) -> None:
        self._queue = queue

    async def start(self, generation: int) -> None:
        _validate_generation(generation, label="generation")

    async def close(self) -> None:
        await self._queue.begin_close()
        if self._queue.state is not AuditBatchQueueState.CLOSED:
            raise RuntimeResourceDrainRequiredError("audit queue 仍含未确认 durable 结果")


class _TrustedRunnerLifecyclePort:
    lifecycle_name = "trusted-runner"

    def __init__(self, pool: TrustedRunnerPool) -> None:
        self._pool = pool

    async def start(self, generation: int) -> None:
        if self._pool.generation != generation:
            raise RuntimeResourceGenerationError("trusted runner generation 与资源代际不一致")
        await self._pool.start()

    async def close(self) -> None:
        await self._pool.close()


T = TypeVar("T")


async def _settle_task(
    task: asyncio.Task[T],
) -> tuple[T | None, asyncio.CancelledError | None, BaseException | None]:
    """Settle finalization despite caller cancellation and report both outcomes."""

    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
    try:
        return task.result(), cancellation, None
    except BaseException as error:
        return None, cancellation, error


async def _close_ports(
    ports: tuple[RuntimeLifecyclePort, ...],
) -> tuple[RuntimeLifecyclePort, ...]:
    failed: list[RuntimeLifecyclePort] = []
    for port in reversed(ports):
        try:
            await port.close()
        except BaseException:
            failed.append(port)
    failed.reverse()
    return tuple(failed)


class RuntimeGenerationResources:
    """All generation-bound primitive ports published and retired together."""

    def __init__(
        self,
        *,
        snapshot: RuntimeSnapshot,
        settings: RuntimeResourceSettings,
        database_manager: DatabaseEngineManager | None,
        redis_manager: RedisClientManager | None,
        repositories: PostgresRuntimeRepositoryProvider | None,
        history_cache: HistoryHotCacheProtocol,
        tool_catalog_cache: ToolCatalogCacheProtocol,
        tool_schema_cache: ToolSchemaCacheProtocol,
        classification_cache: ClassificationCacheProtocol,
        usage_queue: UsageBatchQueue,
        audit_queue: AuditBatchQueue,
        metrics: FullMetricsRegistry,
        structured_logger: StructuredLogEmitter | None,
        api_ports: RuntimeApiPorts,
        trusted_runner: TrustedRunnerPool | None,
        parallel_tool_graph: ToolGraph | None,
        managed_ports: tuple[RuntimeLifecyclePort, ...],
    ) -> None:
        if not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("snapshot 必须是 RuntimeSnapshot")
        generation = _validate_generation(
            snapshot.generation,
            label="RuntimeSnapshot.generation",
        )
        if not isinstance(settings, RuntimeResourceSettings):
            raise TypeError("settings 必须是 RuntimeResourceSettings")
        if metrics.generation != generation:
            raise RuntimeResourceGenerationError("FullMetrics generation 与 RuntimeSnapshot 不一致")
        if not isinstance(api_ports, RuntimeApiPorts):
            raise TypeError("api_ports 必须是 RuntimeApiPorts")
        if parallel_tool_graph is not None and not isinstance(
            parallel_tool_graph,
            ToolGraph,
        ):
            raise TypeError("parallel_tool_graph 必须是 ToolGraph 或 None")
        if parallel_tool_graph is not settings.parallel_tool_graph:
            raise RuntimeResourceConfigurationError("parallel_tool_graph 必须与 generation settings 绑定同一快照")
        if not isinstance(managed_ports, tuple):
            raise TypeError("managed_ports 必须是元组")
        normalized_ports = tuple(_validate_lifecycle_port(port) for port in managed_ports)
        names = tuple(port.lifecycle_name for port in normalized_ports)
        if len(set(names)) != len(names):
            raise ValueError("managed_ports lifecycle_name 不得重复")

        self._snapshot = snapshot
        self._settings = settings
        self._database_manager = database_manager
        self._redis_manager = redis_manager
        self._repositories = repositories
        self._history_cache = history_cache
        self._tool_catalog_cache = tool_catalog_cache
        self._tool_schema_cache = tool_schema_cache
        self._classification_cache = classification_cache
        self._usage_queue = usage_queue
        self._audit_queue = audit_queue
        self._metrics = metrics
        self._structured_logger = structured_logger
        self._api_ports = api_ports
        self._trusted_runner = trusted_runner
        self._parallel_tool_graph = parallel_tool_graph
        self._managed_ports = normalized_ports
        self._started_ports: tuple[RuntimeLifecyclePort, ...] = ()
        self._state = RuntimeGenerationResourceState.CREATED
        self._lifecycle_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(generation={self.generation!r}, "
            f"state={self.state.value!r}, "
            f"database={self._database_manager is not None!r}, "
            f"redis={self._redis_manager is not None!r})"
        )

    @property
    def generation(self) -> int:
        return self._snapshot.generation

    @property
    def state(self) -> RuntimeGenerationResourceState:
        return self._state

    @property
    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    @property
    def settings(self) -> RuntimeResourceSettings:
        return self._settings

    @property
    def database_manager(self) -> DatabaseEngineManager | None:
        return self._database_manager

    @property
    def redis_manager(self) -> RedisClientManager | None:
        return self._redis_manager

    @property
    def repositories(self) -> PostgresRuntimeRepositoryProvider | None:
        return self._repositories

    @property
    def history_cache(self) -> HistoryHotCacheProtocol:
        return self._history_cache

    @property
    def tool_catalog_cache(self) -> ToolCatalogCacheProtocol:
        return self._tool_catalog_cache

    @property
    def tool_schema_cache(self) -> ToolSchemaCacheProtocol:
        return self._tool_schema_cache

    @property
    def classification_cache(self) -> ClassificationCacheProtocol:
        return self._classification_cache

    @property
    def usage_queue(self) -> UsageBatchQueue:
        return self._usage_queue

    @property
    def audit_queue(self) -> AuditBatchQueue:
        return self._audit_queue

    @property
    def metrics(self) -> FullMetricsRegistry:
        return self._metrics

    @property
    def structured_logger(self) -> StructuredLogEmitter | None:
        return self._structured_logger

    @property
    def api_ports(self) -> RuntimeApiPorts:
        return self._api_ports

    @property
    def trusted_runner(self) -> TrustedRunnerPool | None:
        return self._trusted_runner

    @property
    def parallel_tool_graph(self) -> ToolGraph | None:
        return self._parallel_tool_graph

    def safe_diagnostics(self) -> dict[str, bool | int | str]:
        return {
            "generation": self.generation,
            "state": self.state.value,
            "database_configured": self._database_manager is not None,
            "database_initialized": (False if self._database_manager is None else self._database_manager.initialized),
            "redis_configured": self._redis_manager is not None,
            "redis_initialized": (False if self._redis_manager is None else self._redis_manager.initialized),
            "history_backend": self._settings.history_backend.value,
            "api_handler_count": len(self._api_ports.handlers),
            "trusted_runner_configured": self._trusted_runner is not None,
            "parallel_tool_graph_configured": self._parallel_tool_graph is not None,
        }

    async def start(self) -> RuntimeGenerationResources:
        async with self._lifecycle_lock:
            if self._state is RuntimeGenerationResourceState.RUNNING:
                return self
            if self._state is not RuntimeGenerationResourceState.CREATED:
                raise RuntimeResourceLifecycleError("runtime generation resources 当前不可启动")
            self._state = RuntimeGenerationResourceState.STARTING
            started: list[RuntimeLifecyclePort] = []
            try:
                for port in self._managed_ports:
                    started.append(port)
                    await port.start(self.generation)
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(
                    _close_ports(tuple(started)),
                    name=f"moellm-resource-start-rollback-{self.generation}",
                )
                failed, _extra_cancel, cleanup_error = await _settle_task(cleanup)
                self._started_ports = () if failed is None else failed
                self._state = RuntimeGenerationResourceState.FAILED
                if cleanup_error is not None:
                    self._started_ports = tuple(started)
                raise
            except BaseException:
                cleanup = asyncio.create_task(
                    _close_ports(tuple(started)),
                    name=f"moellm-resource-start-rollback-{self.generation}",
                )
                failed, cancellation, cleanup_error = await _settle_task(cleanup)
                self._started_ports = () if failed is None else failed
                self._state = RuntimeGenerationResourceState.FAILED
                if cleanup_error is not None:
                    self._started_ports = tuple(started)
                if cancellation is not None:
                    raise cancellation
                raise RuntimeResourceStartupError("runtime generation resources 启动失败，已执行逆序回滚") from None
            self._started_ports = tuple(started)
            self._state = RuntimeGenerationResourceState.RUNNING
            return self

    async def close(self) -> None:
        async with self._lifecycle_lock:
            if self._state is RuntimeGenerationResourceState.CLOSED:
                return
            if self._state is RuntimeGenerationResourceState.CREATED:
                self._state = RuntimeGenerationResourceState.CLOSED
                return
            if self._state is RuntimeGenerationResourceState.STARTING:
                raise RuntimeResourceLifecycleError("runtime generation resources 正在启动")
            self._state = RuntimeGenerationResourceState.CLOSING
            finalization = asyncio.create_task(
                _close_ports(self._started_ports),
                name=f"moellm-resource-close-{self.generation}",
            )
            failed, cancellation, finalization_error = await _settle_task(finalization)
            if finalization_error is not None or failed is None:
                self._state = RuntimeGenerationResourceState.FAILED
            elif failed:
                self._started_ports = failed
                self._state = RuntimeGenerationResourceState.FAILED
            else:
                self._started_ports = ()
                self._state = RuntimeGenerationResourceState.CLOSED
            if cancellation is not None:
                raise cancellation
            if self._state is RuntimeGenerationResourceState.FAILED:
                raise RuntimeResourceCloseError("runtime generation resources 未能关闭全部资源")


DatabaseManagerFactory = Callable[
    [DatabaseEngineSettings],
    DatabaseEngineManager,
]
RedisManagerFactory = Callable[[RedisClientSettings], RedisClientManager]
LogSinkFactory = Callable[[RuntimeSnapshot], StructuredLogSink]
ApiPortsFactory = Callable[
    [RuntimeSnapshot, FullMetricsRegistry],
    RuntimeApiPorts,
]
LifecyclePortsFactory = Callable[
    [RuntimeSnapshot],
    tuple[RuntimeLifecyclePort, ...],
]
RunnerPoolFactory = Callable[
    [RuntimeSnapshot, tuple[str, ...], TrustedRunnerPoolPolicy],
    TrustedRunnerPool,
]


def _default_database_manager_factory(
    settings: DatabaseEngineSettings,
) -> DatabaseEngineManager:
    return DatabaseEngineManager(settings)


def _default_redis_manager_factory(
    settings: RedisClientSettings,
) -> RedisClientManager:
    return RedisClientManager(settings)


def _default_runner_pool_factory(
    snapshot: RuntimeSnapshot,
    tools: tuple[str, ...],
    policy: TrustedRunnerPoolPolicy,
) -> TrustedRunnerPool:
    tool_snapshot = snapshot.tool_snapshot
    catalog = None if tool_snapshot is None else tool_snapshot.provider_catalog
    if catalog is None:
        raise RuntimeResourceConfigurationError("trusted runner 必须绑定 ProviderCatalogSnapshot")
    return TrustedRunnerPool(
        catalog=catalog,
        eligible_tools=tools,
        policy=policy,
    )


class RuntimeResourceBuilder:
    """Build one detached generation without reading ambient config or doing I/O."""

    def __init__(
        self,
        settings: RuntimeResourceSettings | None = None,
        *,
        database_manager_factory: DatabaseManagerFactory = (_default_database_manager_factory),
        redis_manager_factory: RedisManagerFactory = (_default_redis_manager_factory),
        log_sink_factory: LogSinkFactory | None = None,
        api_ports_factory: ApiPortsFactory | None = None,
        lifecycle_ports_factory: LifecyclePortsFactory | None = None,
        runner_pool_factory: RunnerPoolFactory = _default_runner_pool_factory,
        pid_provider: Callable[[], int] = os.getpid,
        loop_provider: Callable[[], asyncio.AbstractEventLoop] = (asyncio.get_running_loop),
    ) -> None:
        if settings is None:
            settings = RuntimeResourceSettings()
        if not isinstance(settings, RuntimeResourceSettings):
            raise TypeError("settings 必须是 RuntimeResourceSettings")
        for value, label in (
            (database_manager_factory, "database_manager_factory"),
            (redis_manager_factory, "redis_manager_factory"),
            (runner_pool_factory, "runner_pool_factory"),
            (pid_provider, "pid_provider"),
            (loop_provider, "loop_provider"),
        ):
            _validate_factory(value, label=label)
        for value, label in (
            (log_sink_factory, "log_sink_factory"),
            (api_ports_factory, "api_ports_factory"),
            (lifecycle_ports_factory, "lifecycle_ports_factory"),
        ):
            if value is not None:
                _validate_factory(value, label=label)
        self._settings = settings
        self._database_manager_factory = database_manager_factory
        self._redis_manager_factory = redis_manager_factory
        self._log_sink_factory = log_sink_factory
        self._api_ports_factory = api_ports_factory
        self._lifecycle_ports_factory = lifecycle_ports_factory
        self._runner_pool_factory = runner_pool_factory
        self._pid_provider = pid_provider
        self._loop_provider = loop_provider

    @property
    def settings(self) -> RuntimeResourceSettings:
        return self._settings

    def build(self, snapshot: RuntimeSnapshot) -> RuntimeGenerationResources:
        if not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("snapshot 必须是 RuntimeSnapshot")
        generation = _validate_generation(
            snapshot.generation,
            label="RuntimeSnapshot.generation",
        )

        database_manager: DatabaseEngineManager | None = None
        repositories: PostgresRuntimeRepositoryProvider | None = None
        if self._settings.database is not None:
            database_manager = self._database_manager_factory(self._settings.database)
            if not isinstance(database_manager, DatabaseEngineManager):
                raise RuntimeResourceConfigurationError("database manager factory 返回非法对象")
            repositories = PostgresRuntimeRepositoryProvider()

        redis_manager: RedisClientManager | None = None
        if self._settings.redis is not None:
            redis_manager = self._redis_manager_factory(self._settings.redis)
            if not isinstance(redis_manager, RedisClientManager):
                raise RuntimeResourceConfigurationError("Redis manager factory 返回非法对象")

        if self._settings.history_backend is RuntimeResourceHistoryBackend.MEMORY:
            history_cache: HistoryHotCacheProtocol = MemoryHistoryHotCache(
                settings=self._settings.memory_history,
                pid_provider=self._pid_provider,
                loop_provider=self._loop_provider,
            )
        else:
            if redis_manager is None:
                raise RuntimeResourceConfigurationError("Redis history backend 缺少 Redis manager")
            history_cache = LazyRedisHistoryHotCache(
                redis_manager,
                settings=self._settings.redis_history,
            )

        tool_catalog_cache = MemoryToolCatalogCache(
            settings=self._settings.tool_catalog,
            pid_provider=self._pid_provider,
            loop_provider=self._loop_provider,
        )
        tool_schema_cache = MemoryToolSchemaCache(
            settings=self._settings.tool_schema,
            pid_provider=self._pid_provider,
            loop_provider=self._loop_provider,
        )
        classification_cache = MemoryClassificationCache(
            settings=self._settings.classification,
            pid_provider=self._pid_provider,
            loop_provider=self._loop_provider,
        )
        usage_queue = UsageBatchQueue(
            self._settings.usage_batch,
            pid_getter=self._pid_provider,
        )
        audit_queue = AuditBatchQueue(
            self._settings.audit_batch,
            pid_getter=self._pid_provider,
        )
        metrics = FullMetricsRegistry(
            generation=generation,
            pid_getter=self._pid_provider,
        )

        structured_logger: StructuredLogEmitter | None = None
        if self._log_sink_factory is not None:
            sink = self._log_sink_factory(snapshot)
            if not isinstance(sink, StructuredLogSink):
                raise RuntimeResourceConfigurationError("structured log sink factory 返回非法对象")
            structured_logger = StructuredLogEmitter(sink=sink)

        api_ports = RuntimeApiPorts()
        if self._api_ports_factory is not None:
            api_ports = self._api_ports_factory(snapshot, metrics)
            if not isinstance(api_ports, RuntimeApiPorts):
                raise RuntimeResourceConfigurationError("API ports factory 返回非法对象")

        trusted_runner: TrustedRunnerPool | None = None
        if self._settings.trusted_runner_tools:
            trusted_runner = self._runner_pool_factory(
                snapshot,
                self._settings.trusted_runner_tools,
                self._settings.trusted_runner_policy,
            )
            if not isinstance(trusted_runner, TrustedRunnerPool):
                raise RuntimeResourceConfigurationError("trusted runner factory 返回非法对象")

        managed: list[RuntimeLifecyclePort] = []
        if database_manager is not None:
            managed.append(_DatabaseLifecyclePort(database_manager))
        if redis_manager is not None:
            managed.append(_RedisLifecyclePort(redis_manager))
        managed.extend(
            (
                _UsageQueueLifecyclePort(usage_queue),
                _AuditQueueLifecyclePort(audit_queue),
            )
        )
        if trusted_runner is not None:
            managed.append(_TrustedRunnerLifecyclePort(trusted_runner))
        if self._lifecycle_ports_factory is not None:
            extra_ports = self._lifecycle_ports_factory(snapshot)
            if not isinstance(extra_ports, tuple):
                raise RuntimeResourceConfigurationError("lifecycle ports factory 必须返回元组")
            managed.extend(extra_ports)

        return RuntimeGenerationResources(
            snapshot=snapshot,
            settings=self._settings,
            database_manager=database_manager,
            redis_manager=redis_manager,
            repositories=repositories,
            history_cache=history_cache,
            tool_catalog_cache=tool_catalog_cache,
            tool_schema_cache=tool_schema_cache,
            classification_cache=classification_cache,
            usage_queue=usage_queue,
            audit_queue=audit_queue,
            metrics=metrics,
            structured_logger=structured_logger,
            api_ports=api_ports,
            trusted_runner=trusted_runner,
            parallel_tool_graph=self._settings.parallel_tool_graph,
            managed_ports=tuple(managed),
        )


@runtime_checkable
class RuntimeResourceFactory(Protocol):
    def build(self, snapshot: RuntimeSnapshot) -> RuntimeGenerationResources: ...


@dataclass(slots=True)
class _RuntimeResourceLeaseBinding:
    resources: RuntimeGenerationResources
    released: bool = False


class RuntimeResourceManager:
    """Atomically hand off generation resources after pinned requests drain."""

    def __init__(
        self,
        factory: RuntimeResourceFactory,
        *,
        pid_provider: Callable[[], int] = os.getpid,
        loop_provider: Callable[[], asyncio.AbstractEventLoop] = (asyncio.get_running_loop),
    ) -> None:
        if not isinstance(factory, RuntimeResourceFactory):
            raise TypeError("factory 必须实现 RuntimeResourceFactory")
        for value, label in (
            (pid_provider, "pid_provider"),
            (loop_provider, "loop_provider"),
        ):
            _validate_factory(value, label=label)
        self._factory = factory
        self._pid_provider = pid_provider
        self._loop_provider = loop_provider
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._operation_lock: asyncio.Lock | None = None
        self._condition: asyncio.Condition | None = None
        self._active: RuntimeGenerationResources | None = None
        self._retired: list[RuntimeGenerationResources] = []
        self._leases: dict[RuntimeGenerationResources, int] = {}
        self._state = RuntimeResourceManagerState.CREATED
        self._bound: ContextVar[_RuntimeResourceLeaseBinding | None] = ContextVar(
            f"moellm-runtime-resources-{id(self)}",
            default=None,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(state={self.state.value!r}, generation={self.current_generation!r})"

    @property
    def state(self) -> RuntimeResourceManagerState:
        return self._state

    @property
    def current_generation(self) -> int | None:
        return None if self._active is None else self._active.generation

    def active(self) -> RuntimeGenerationResources | None:
        binding = self._bound.get()
        if binding is None:
            return self._active
        if binding.released:
            return None
        return binding.resources

    def safe_diagnostics(self) -> dict[str, bool | int | str | None]:
        active = self._active
        return {
            "state": self._state.value,
            "generation": None if active is None else active.generation,
            "active_lease_count": (0 if active is None else self._leases.get(active, 0)),
            "retired_generation_count": len(self._retired),
            "database_configured": (False if active is None else active.database_manager is not None),
            "redis_configured": (False if active is None else active.redis_manager is not None),
        }

    def _claim_owner(
        self,
    ) -> tuple[asyncio.Lock, asyncio.Condition]:
        try:
            pid = self._pid_provider()
            loop = self._loop_provider()
        except Exception:
            raise RuntimeResourceOwnershipError("runtime resource manager 无法确认 owner") from None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(loop, asyncio.AbstractEventLoop):
            raise RuntimeResourceOwnershipError("runtime resource manager owner 非法")
        if self._owner_pid is None:
            self._owner_pid = pid
            self._owner_loop = loop
            self._operation_lock = asyncio.Lock()
            self._condition = asyncio.Condition()
        elif self._owner_pid != pid:
            raise RuntimeResourceOwnershipError("runtime resource manager 不得跨进程复用")
        elif self._owner_loop is not loop:
            raise RuntimeResourceOwnershipError("runtime resource manager 不得跨 event loop 复用")
        assert self._operation_lock is not None
        assert self._condition is not None
        return self._operation_lock, self._condition

    def _build(
        self,
        snapshot: RuntimeSnapshot,
    ) -> RuntimeGenerationResources:
        try:
            resources = self._factory.build(snapshot)
        except RuntimeResourceError:
            raise
        except BaseException:
            raise RuntimeResourceConfigurationError("runtime resource factory 构建失败") from None
        if not isinstance(resources, RuntimeGenerationResources):
            raise RuntimeResourceConfigurationError("runtime resource factory 返回非法对象")
        if resources.snapshot is not snapshot:
            raise RuntimeResourceConfigurationError("runtime resources 必须绑定调用方提供的 snapshot identity")
        return resources

    async def _finish_start_rollback(
        self,
        *,
        resources: RuntimeGenerationResources,
        condition: asyncio.Condition,
    ) -> None:
        close_failed = False
        try:
            await resources.close()
        except BaseException:
            close_failed = True
        async with condition:
            if self._active is resources:
                self._active = None
            if close_failed:
                if resources not in self._retired:
                    self._retired.append(resources)
                self._leases.setdefault(resources, 0)
                self._state = RuntimeResourceManagerState.FAILED
            else:
                self._leases.pop(resources, None)
                self._state = RuntimeResourceManagerState.CREATED
            condition.notify_all()

    async def _finish_reload_candidate_rollback(
        self,
        *,
        previous: RuntimeGenerationResources,
        candidate: RuntimeGenerationResources,
        condition: asyncio.Condition,
    ) -> None:
        close_failed = False
        try:
            await candidate.close()
        except BaseException:
            close_failed = True
        async with condition:
            active_drifted = self._active is not previous
            if close_failed:
                if candidate not in self._retired:
                    self._retired.append(candidate)
                self._leases.setdefault(candidate, 0)
            else:
                self._leases.pop(candidate, None)
            self._state = (
                RuntimeResourceManagerState.FAILED if close_failed or active_drifted else RuntimeResourceManagerState.RUNNING
            )
            condition.notify_all()

    async def start(
        self,
        snapshot: RuntimeSnapshot,
    ) -> RuntimeGenerationResources:
        operation_lock, condition = self._claim_owner()
        async with operation_lock:
            if self._state is RuntimeResourceManagerState.RUNNING:
                if self._active is not None and self._active.snapshot is snapshot:
                    return self._active
                raise RuntimeResourceLifecycleError("runtime resource manager 已启动")
            if self._state is not RuntimeResourceManagerState.CREATED:
                raise RuntimeResourceLifecycleError("runtime resource manager 当前不可启动")
            self._state = RuntimeResourceManagerState.STARTING
            resources: RuntimeGenerationResources | None = None
            try:
                resources = self._build(snapshot)
                await resources.start()
                async with condition:
                    self._active = resources
                    self._leases[resources] = 0
                    self._state = RuntimeResourceManagerState.RUNNING
                    condition.notify_all()
            except asyncio.CancelledError as cancellation:
                if resources is None:
                    self._state = RuntimeResourceManagerState.CREATED
                else:
                    rollback = asyncio.create_task(
                        self._finish_start_rollback(
                            resources=resources,
                            condition=condition,
                        ),
                        name=f"moellm-resource-manager-start-rollback-{snapshot.generation}",
                    )
                    await _settle_task(rollback)
                raise cancellation
            except BaseException:
                cleanup_cancellation: asyncio.CancelledError | None = None
                if resources is None:
                    self._state = RuntimeResourceManagerState.CREATED
                else:
                    rollback = asyncio.create_task(
                        self._finish_start_rollback(
                            resources=resources,
                            condition=condition,
                        ),
                        name=f"moellm-resource-manager-start-rollback-{snapshot.generation}",
                    )
                    _result, cleanup_cancellation, _cleanup_error = await _settle_task(rollback)
                if cleanup_cancellation is not None:
                    raise cleanup_cancellation
                raise RuntimeResourceStartupError("runtime resource manager 启动候选失败") from None
            return resources

    @asynccontextmanager
    async def lease(
        self,
        *,
        expected_generation: int | None = None,
    ) -> AsyncIterator[RuntimeGenerationResources]:
        _operation_lock, condition = self._claim_owner()
        if expected_generation is not None:
            _validate_generation(
                expected_generation,
                label="expected_generation",
            )
        inherited = self._bound.get()
        if inherited is not None and inherited.released:
            raise RuntimeResourceLifecycleError("继承的 runtime resource lease 已释放")
        async with condition:
            if inherited is None:
                if self._state not in {
                    RuntimeResourceManagerState.RUNNING,
                    RuntimeResourceManagerState.RELOADING,
                }:
                    raise RuntimeResourceLifecycleError("runtime resource manager 当前不接受请求")
                resources = self._active
                if resources is None:
                    raise RuntimeResourceLifecycleError("runtime resource manager 缺少 active generation")
            else:
                if inherited.released:
                    raise RuntimeResourceLifecycleError("继承的 runtime resource lease 已释放")
                resources = inherited.resources
                if self._leases.get(resources, 0) <= 0:
                    self._state = RuntimeResourceManagerState.FAILED
                    raise RuntimeResourceLifecycleError("继承的 runtime resource lease 计数损坏")
            if expected_generation is not None and resources.generation != expected_generation:
                raise RuntimeResourceGenerationError("active runtime resource generation 不匹配")
            self._leases[resources] = self._leases.get(resources, 0) + 1
            binding = _RuntimeResourceLeaseBinding(resources)
            token = self._bound.set(binding)
        try:
            yield resources
        finally:
            binding.released = True
            self._bound.reset(token)
            release = asyncio.create_task(
                self._finish_lease_release(
                    resources=resources,
                    condition=condition,
                ),
                name=f"moellm-resource-lease-release-{resources.generation}",
            )
            _result, cancellation, release_error = await _settle_task(release)
            if cancellation is not None:
                raise cancellation
            if release_error is not None:
                raise RuntimeResourceLifecycleError("runtime resource lease 释放失败") from None

    async def _finish_lease_release(
        self,
        *,
        resources: RuntimeGenerationResources,
        condition: asyncio.Condition,
    ) -> None:
        async with condition:
            count = self._leases.get(resources, 0)
            if count <= 0:
                self._state = RuntimeResourceManagerState.FAILED
                condition.notify_all()
                raise RuntimeResourceLifecycleError("runtime resource lease 计数损坏")
            self._leases[resources] = count - 1
            condition.notify_all()

    async def _finish_reload(
        self,
        *,
        previous: RuntimeGenerationResources,
        candidate: RuntimeGenerationResources,
        condition: asyncio.Condition,
    ) -> RuntimeGenerationResources:
        async with condition:
            if self._active is not previous:
                self._state = RuntimeResourceManagerState.FAILED
                raise RuntimeResourceReloadError("active runtime resource identity 在切换前漂移")
            self._active = candidate
            self._retired.append(previous)
            self._leases.setdefault(candidate, 0)
            condition.notify_all()
            while self._leases.get(previous, 0) > 0:
                await condition.wait()
        try:
            await previous.close()
        except BaseException:
            async with condition:
                self._state = RuntimeResourceManagerState.FAILED
                condition.notify_all()
            raise RuntimeResourceReloadError("旧 runtime generation 未能安全关闭") from None
        async with condition:
            self._leases.pop(previous, None)
            self._retired.remove(previous)
            self._state = RuntimeResourceManagerState.RUNNING
            condition.notify_all()
        return candidate

    async def reload(
        self,
        snapshot: RuntimeSnapshot,
    ) -> RuntimeGenerationResources:
        if self._bound.get() is not None:
            raise RuntimeResourceLifecycleError("runtime resource lease 内不得执行 reload")
        operation_lock, condition = self._claim_owner()
        async with operation_lock:
            if self._state is not RuntimeResourceManagerState.RUNNING:
                raise RuntimeResourceLifecycleError("runtime resource manager 当前不可重载")
            previous = self._active
            if previous is None:
                raise RuntimeResourceLifecycleError("runtime resource manager 缺少 active generation")
            if snapshot is previous.snapshot:
                return previous
            _validate_generation(
                snapshot.generation,
                label="RuntimeSnapshot.generation",
            )
            if snapshot.generation <= previous.generation:
                raise RuntimeResourceGenerationError("runtime resource generation 必须严格递增")
            self._state = RuntimeResourceManagerState.RELOADING
            candidate: RuntimeGenerationResources | None = None
            try:
                candidate = self._build(snapshot)
                await candidate.start()
            except asyncio.CancelledError as cancellation:
                if candidate is None:
                    self._state = RuntimeResourceManagerState.RUNNING
                else:
                    rollback = asyncio.create_task(
                        self._finish_reload_candidate_rollback(
                            previous=previous,
                            candidate=candidate,
                            condition=condition,
                        ),
                        name=f"moellm-resource-candidate-rollback-{snapshot.generation}",
                    )
                    await _settle_task(rollback)
                raise cancellation
            except BaseException:
                cleanup_cancellation: asyncio.CancelledError | None = None
                if candidate is None:
                    self._state = RuntimeResourceManagerState.RUNNING
                else:
                    rollback = asyncio.create_task(
                        self._finish_reload_candidate_rollback(
                            previous=previous,
                            candidate=candidate,
                            condition=condition,
                        ),
                        name=f"moellm-resource-candidate-rollback-{snapshot.generation}",
                    )
                    _result, cleanup_cancellation, _cleanup_error = await _settle_task(rollback)
                if cleanup_cancellation is not None:
                    raise cleanup_cancellation
                raise RuntimeResourceReloadError("新 runtime generation 启动失败，旧代保持 active") from None

            finalization = asyncio.create_task(
                self._finish_reload(
                    previous=previous,
                    candidate=candidate,
                    condition=condition,
                ),
                name=(f"moellm-resource-handoff-{previous.generation}-{candidate.generation}"),
            )
            result, cancellation, finalization_error = await _settle_task(finalization)
            if cancellation is not None:
                raise cancellation
            if finalization_error is not None or result is None:
                raise RuntimeResourceReloadError("runtime resource generation handoff 失败") from None
            return result

    async def _finish_close(
        self,
        *,
        active: RuntimeGenerationResources | None,
        condition: asyncio.Condition,
    ) -> None:
        async with condition:
            self._state = RuntimeResourceManagerState.CLOSING
            self._active = None
            condition.notify_all()
            targets = tuple(resource for resource in (active, *self._retired) if resource is not None)
            while any(self._leases.get(resource, 0) > 0 for resource in targets):
                await condition.wait()
        failed: list[RuntimeGenerationResources] = []
        for resource in targets:
            try:
                await resource.close()
            except BaseException:
                failed.append(resource)
        async with condition:
            for resource in targets:
                if resource not in failed:
                    self._leases.pop(resource, None)
            self._retired = list(failed)
            self._state = RuntimeResourceManagerState.FAILED if failed else RuntimeResourceManagerState.CLOSED
            condition.notify_all()
        if failed:
            raise RuntimeResourceCloseError("runtime generations 未能全部安全关闭")

    async def close(self) -> None:
        if self._state is RuntimeResourceManagerState.CLOSED:
            return
        if self._state is RuntimeResourceManagerState.CREATED:
            self._state = RuntimeResourceManagerState.CLOSED
            return
        if self._bound.get() is not None:
            raise RuntimeResourceLifecycleError("runtime resource lease 内不得执行 close")
        operation_lock, condition = self._claim_owner()
        async with operation_lock:
            if self._state is RuntimeResourceManagerState.CLOSED:
                return
            if self._state not in {
                RuntimeResourceManagerState.RUNNING,
                RuntimeResourceManagerState.FAILED,
            }:
                raise RuntimeResourceLifecycleError("runtime resource manager 当前不可关闭")
            resources = self._active
            if resources is None and not self._retired:
                self._state = RuntimeResourceManagerState.CLOSED
                return
            generation = resources.generation if resources is not None else self._retired[-1].generation
            finalization = asyncio.create_task(
                self._finish_close(
                    active=resources,
                    condition=condition,
                ),
                name=f"moellm-resource-manager-close-{generation}",
            )
            _result, cancellation, finalization_error = await _settle_task(finalization)
            if cancellation is not None:
                raise cancellation
            if finalization_error is not None:
                raise RuntimeResourceCloseError("runtime resource manager 关闭失败") from None


__all__ = [
    "LazyRedisHistoryHotCache",
    "PostgresRuntimeRepositoryProvider",
    "RuntimeApiPorts",
    "RuntimeGenerationResourceState",
    "RuntimeGenerationResources",
    "RuntimeLifecyclePort",
    "RuntimePostgresRepositories",
    "RuntimeResourceBuilder",
    "RuntimeResourceCloseError",
    "RuntimeResourceConfigurationError",
    "RuntimeResourceDrainRequiredError",
    "RuntimeResourceError",
    "RuntimeResourceFactory",
    "RuntimeResourceGenerationError",
    "RuntimeResourceHistoryBackend",
    "RuntimeResourceLifecycleError",
    "RuntimeResourceManager",
    "RuntimeResourceManagerState",
    "RuntimeResourceOwnershipError",
    "RuntimeResourceReloadError",
    "RuntimeResourceSettings",
    "RuntimeResourceStartupError",
]

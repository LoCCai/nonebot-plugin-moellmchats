from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import socket
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from nonebot_plugin_moellmchats.classification_cache import (
    MemoryClassificationCache,
)
from nonebot_plugin_moellmchats.database_engine import (
    DatabaseEngineManager,
    DatabaseEngineSettings,
)
from nonebot_plugin_moellmchats.history_hot_cache import (
    HistoryCacheLoadToken,
    HistoryCacheLookup,
    MemoryHistoryHotCache,
)
from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord
from nonebot_plugin_moellmchats.redis_client import (
    RedisClientManager,
    RedisClientSettings,
)
from nonebot_plugin_moellmchats.redis_history_hot_cache import (
    RedisHistoryHotCache,
)
from nonebot_plugin_moellmchats.runtime_api import (
    RuntimeApiRequest,
    RuntimeApiResponse,
)
import nonebot_plugin_moellmchats.runtime_resources as resources_module
from nonebot_plugin_moellmchats.runtime_resources import (
    LazyRedisHistoryHotCache,
    PostgresRuntimeRepositoryProvider,
    RuntimeApiPorts,
    RuntimeGenerationResources,
    RuntimeGenerationResourceState,
    RuntimePostgresRepositories,
    RuntimeResourceBuilder,
    RuntimeResourceCloseError,
    RuntimeResourceConfigurationError,
    RuntimeResourceGenerationError,
    RuntimeResourceHistoryBackend,
    RuntimeResourceLifecycleError,
    RuntimeResourceManager,
    RuntimeResourceManagerState,
    RuntimeResourceOwnershipError,
    RuntimeResourceReloadError,
    RuntimeResourceSettings,
    RuntimeResourceStartupError,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot
from nonebot_plugin_moellmchats.structured_logging import (
    StructuredLogLevel,
)
from nonebot_plugin_moellmchats.tool_catalog_cache import (
    MemoryToolCatalogCache,
)
from nonebot_plugin_moellmchats.tool_graph import ToolGraph
from nonebot_plugin_moellmchats.tool_schema_cache import MemoryToolSchemaCache
from nonebot_plugin_moellmchats.usage_batch import UsageBatchQueueState


def _snapshot(generation: int) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config={"request_timeout_seconds": 30},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=float(generation),
    )


class _RecordingLifecyclePort:
    def __init__(
        self,
        name: str,
        events: list[tuple[str, str, int | None]],
        *,
        fail_start: bool = False,
        fail_close_count: int = 0,
        start_entered: asyncio.Event | None = None,
        start_release: asyncio.Event | None = None,
        close_entered: asyncio.Event | None = None,
        close_release: asyncio.Event | None = None,
    ) -> None:
        self.lifecycle_name = name
        self.events = events
        self.fail_start = fail_start
        self.fail_close_count = fail_close_count
        self.start_entered = start_entered
        self.start_release = start_release
        self.close_entered = close_entered
        self.close_release = close_release

    async def start(self, generation: int) -> None:
        self.events.append((self.lifecycle_name, "start", generation))
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.fail_start:
            raise RuntimeError("sensitive startup detail")

    async def close(self) -> None:
        self.events.append((self.lifecycle_name, "close", None))
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            await self.close_release.wait()
        if self.fail_close_count:
            self.fail_close_count -= 1
            raise RuntimeError("sensitive close detail")


class _CapturingFactory:
    def __init__(self, builder: RuntimeResourceBuilder) -> None:
        self.builder = builder
        self.built: list[RuntimeGenerationResources] = []

    def build(self, snapshot: RuntimeSnapshot) -> RuntimeGenerationResources:
        resources = self.builder.build(snapshot)
        self.built.append(resources)
        return resources


class _FailingFactory:
    def build(self, snapshot: RuntimeSnapshot) -> RuntimeGenerationResources:
        del snapshot
        raise RuntimeError("sensitive factory detail")


class _Sink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, line: str, /) -> None:
        self.lines.append(line)


class _Handler:
    async def handle(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiResponse:
        del request
        return RuntimeApiResponse(200, {"ok": True})


class _ConstructorOnlySession(AsyncSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls = {
            "execute": 0,
            "begin": 0,
            "commit": 0,
            "rollback": 0,
            "flush": 0,
            "close": 0,
        }

    async def execute(self, *_args: object, **_kwargs: object) -> Any:
        self.calls["execute"] += 1
        raise AssertionError("repository construction executed SQL")

    def begin(self, *_args: object, **_kwargs: object) -> Any:
        self.calls["begin"] += 1
        raise AssertionError("repository construction opened a transaction")

    async def commit(self) -> None:
        self.calls["commit"] += 1
        raise AssertionError("repository construction committed")

    async def rollback(self) -> None:
        self.calls["rollback"] += 1
        raise AssertionError("repository construction rolled back")

    async def flush(self, objects: object = None) -> None:
        del objects
        self.calls["flush"] += 1
        raise AssertionError("repository construction flushed")

    async def close(self) -> None:
        self.calls["close"] += 1
        raise AssertionError("repository construction closed the session")


def _usage_record() -> ModelUsageRecord:
    return ModelUsageRecord(
        usage_id=None,
        run_id="run_runtime_resources",
        provider="provider",
        model="model",
        input_tokens=10,
        output_tokens=2,
        reasoning_tokens=0,
        cached_tokens=0,
        cost=Decimal("0.0001"),
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
    )


async def _wait_for_manager_generation(
    manager: RuntimeResourceManager,
    generation: int,
) -> None:
    _operation_lock, condition = manager._claim_owner()
    async with condition:
        await condition.wait_for(lambda: manager.current_generation == generation)


async def _wait_for_manager_state(
    manager: RuntimeResourceManager,
    state: RuntimeResourceManagerState,
) -> None:
    _operation_lock, condition = manager._claim_owner()
    async with condition:
        await condition.wait_for(lambda: manager.state is state)


def test_default_settings_are_memory_only_and_safe() -> None:
    settings = RuntimeResourceSettings()

    assert settings.database is None
    assert settings.redis is None
    assert settings.history_backend is RuntimeResourceHistoryBackend.MEMORY
    assert settings.trusted_runner_tools == ()
    assert settings.safe_diagnostics() == {
        "database_configured": False,
        "redis_configured": False,
        "history_backend": "memory",
        "trusted_runner_tool_count": 0,
        "parallel_tool_graph_configured": False,
    }
    assert "url" not in repr(settings).lower()


def test_settings_require_explicit_redis_for_redis_history() -> None:
    with pytest.raises(
        RuntimeResourceConfigurationError,
        match="显式提供",
    ):
        RuntimeResourceSettings(
            history_backend=RuntimeResourceHistoryBackend.REDIS,
        )


def test_settings_sort_and_reject_duplicate_runner_tools() -> None:
    settings = RuntimeResourceSettings(
        trusted_runner_tools=("z_tool", "a_tool"),
    )
    assert settings.trusted_runner_tools == ("a_tool", "z_tool")

    with pytest.raises(ValueError, match="不得重复"):
        RuntimeResourceSettings(
            trusted_runner_tools=("a_tool", "a_tool"),
        )


def test_parallel_graph_requires_an_explicit_covering_runner_allowlist() -> None:
    graph = ToolGraph(tools=("safe_tool",))

    with pytest.raises(RuntimeResourceConfigurationError, match="trusted_runner"):
        RuntimeResourceSettings(parallel_tool_graph=graph)

    with pytest.raises(RuntimeResourceConfigurationError, match="缺少"):
        RuntimeResourceSettings(
            trusted_runner_tools=("other_tool",),
            parallel_tool_graph=graph,
        )

    settings = RuntimeResourceSettings(
        trusted_runner_tools=("safe_tool",),
        parallel_tool_graph=graph,
    )
    assert settings.parallel_tool_graph is graph
    assert settings.safe_diagnostics()["parallel_tool_graph_configured"] is True


@pytest.mark.asyncio
async def test_default_builder_composes_memory_primitives_with_zero_backend_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_calls = 0

    def reject_socket(self: socket.socket, _address: object) -> None:
        nonlocal socket_calls
        del self
        socket_calls += 1
        raise AssertionError("unexpected socket I/O")

    monkeypatch.setattr(socket.socket, "connect", reject_socket)
    resources = RuntimeResourceBuilder().build(_snapshot(1))

    assert resources.generation == 1
    assert resources.database_manager is None
    assert resources.redis_manager is None
    assert resources.repositories is None
    assert isinstance(resources.history_cache, MemoryHistoryHotCache)
    assert isinstance(resources.tool_catalog_cache, MemoryToolCatalogCache)
    assert isinstance(resources.tool_schema_cache, MemoryToolSchemaCache)
    assert isinstance(
        resources.classification_cache,
        MemoryClassificationCache,
    )
    assert resources.metrics.generation == 1
    assert resources.structured_logger is None
    assert resources.api_ports == RuntimeApiPorts()
    assert resources.trusted_runner is None
    assert resources.parallel_tool_graph is None
    assert resources.state is RuntimeGenerationResourceState.CREATED

    await resources.start()
    assert resources.state is RuntimeGenerationResourceState.RUNNING
    assert socket_calls == 0
    await resources.close()
    assert resources.state is RuntimeGenerationResourceState.CLOSED
    assert socket_calls == 0


def test_module_does_not_create_global_resource_manager_or_generation() -> None:
    assert not any(
        isinstance(
            value,
            (RuntimeResourceManager, RuntimeGenerationResources),
        )
        for value in vars(resources_module).values()
    )


@pytest.mark.asyncio
async def test_explicit_database_and_redis_managers_remain_lazy_and_redacted() -> None:
    database_settings = DatabaseEngineSettings(database_url=("postgresql+asyncpg://smoke-user:smoke-secret@db.invalid/smoke"))
    redis_settings = RedisClientSettings(redis_url="rediss://smoke-user:redis-secret@redis.invalid:6380/11")
    engine_calls = 0
    client_calls = 0

    def engine_factory(*_args: object, **_kwargs: object) -> AsyncEngine:
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("engine must stay lazy")

    def client_factory(*_args: object, **_kwargs: object) -> Redis:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("Redis client must stay lazy")

    def database_manager_factory(
        settings: DatabaseEngineSettings,
    ) -> DatabaseEngineManager:
        return DatabaseEngineManager(settings, engine_factory=engine_factory)

    def redis_manager_factory(
        settings: RedisClientSettings,
    ) -> RedisClientManager:
        return RedisClientManager(settings, client_factory=client_factory)

    settings = RuntimeResourceSettings(
        database=database_settings,
        redis=redis_settings,
    )
    resources = RuntimeResourceBuilder(
        settings,
        database_manager_factory=database_manager_factory,
        redis_manager_factory=redis_manager_factory,
    ).build(_snapshot(3))

    assert isinstance(resources.database_manager, DatabaseEngineManager)
    assert isinstance(resources.redis_manager, RedisClientManager)
    assert isinstance(
        resources.repositories,
        PostgresRuntimeRepositoryProvider,
    )
    assert engine_calls == client_calls == 0
    assert "smoke-secret" not in repr(settings)
    assert "redis-secret" not in repr(settings)
    assert "db.invalid" not in repr(resources)
    assert "redis.invalid" not in repr(resources.safe_diagnostics())

    await resources.start()
    assert engine_calls == client_calls == 0
    await resources.close()
    assert engine_calls == client_calls == 0


def test_postgres_repository_provider_constructs_all_ports_without_session_io() -> None:
    session = _ConstructorOnlySession()
    repositories = PostgresRuntimeRepositoryProvider().for_session(session)

    assert isinstance(repositories, RuntimePostgresRepositories)
    assert {
        repository._session
        for repository in (
            repositories.user,
            repositories.conversation,
            repositories.message,
            repositories.session_summary,
            repositories.agent_run,
            repositories.agent_step,
            repositories.tool_call,
            repositories.usage,
            repositories.audit,
        )
    } == {session}
    assert session.calls == {
        "execute": 0,
        "begin": 0,
        "commit": 0,
        "rollback": 0,
        "flush": 0,
        "close": 0,
    }


@pytest.mark.asyncio
async def test_redis_history_backend_is_lazy_until_first_explicit_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_settings = RedisClientSettings(redis_url="redis://127.0.0.1:1/0")
    client_calls = 0
    client = Redis.from_url("redis://127.0.0.1:1/0")

    def client_factory(*_args: object, **_kwargs: object) -> Redis:
        nonlocal client_calls
        client_calls += 1
        return client

    def redis_manager_factory(
        settings: RedisClientSettings,
    ) -> RedisClientManager:
        return RedisClientManager(settings, client_factory=client_factory)

    token = HistoryCacheLoadToken(
        conversation_fingerprint="a" * 64,
        generation="b" * 32,
        expires_at=10.0,
    )

    async def fake_lookup(
        self: RedisHistoryHotCache,
        conversation_id: str,
        *,
        limit: int,
    ) -> HistoryCacheLookup:
        del self
        assert conversation_id == "conversation-1"
        assert limit == 20
        return HistoryCacheLookup(load_token=token)

    monkeypatch.setattr(RedisHistoryHotCache, "lookup", fake_lookup)
    settings = RuntimeResourceSettings(
        redis=redis_settings,
        history_backend=RuntimeResourceHistoryBackend.REDIS,
    )
    resources = RuntimeResourceBuilder(
        settings,
        redis_manager_factory=redis_manager_factory,
    ).build(_snapshot(4))
    history = resources.history_cache

    assert isinstance(history, LazyRedisHistoryHotCache)
    assert history.initialized is False
    assert client_calls == 0
    await resources.start()
    assert client_calls == 0
    result = await history.lookup("conversation-1", limit=20)
    assert result.load_token is token
    assert history.initialized is True
    assert client_calls == 1
    await resources.close()


@pytest.mark.asyncio
async def test_generation_resources_start_and_close_in_reverse_order() -> None:
    events: list[tuple[str, str, int | None]] = []
    first = _RecordingLifecyclePort("first", events)
    second = _RecordingLifecyclePort("second", events)
    builder = RuntimeResourceBuilder(
        lifecycle_ports_factory=lambda _snapshot: (first, second),
    )
    resources = builder.build(_snapshot(5))

    await resources.start()
    await resources.start()
    await resources.close()
    await resources.close()

    assert events == [
        ("first", "start", 5),
        ("second", "start", 5),
        ("second", "close", None),
        ("first", "close", None),
    ]


@pytest.mark.asyncio
async def test_partial_start_failure_rolls_back_failed_port_and_prior_ports() -> None:
    events: list[tuple[str, str, int | None]] = []
    first = _RecordingLifecyclePort("first", events)
    second = _RecordingLifecyclePort("second", events, fail_start=True)
    resources = RuntimeResourceBuilder(
        lifecycle_ports_factory=lambda _snapshot: (first, second),
    ).build(_snapshot(6))

    with pytest.raises(RuntimeResourceStartupError) as raised:
        await resources.start()

    assert "sensitive" not in str(raised.value)
    assert resources.state is RuntimeGenerationResourceState.FAILED
    assert events == [
        ("first", "start", 6),
        ("second", "start", 6),
        ("second", "close", None),
        ("first", "close", None),
    ]
    await resources.close()
    assert resources.state is RuntimeGenerationResourceState.CLOSED


@pytest.mark.asyncio
async def test_start_cancellation_settles_reverse_rollback_before_returning() -> None:
    events: list[tuple[str, str, int | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    port = _RecordingLifecyclePort(
        "blocking-start",
        events,
        start_entered=entered,
        start_release=release,
    )
    resources = RuntimeResourceBuilder(
        lifecycle_ports_factory=lambda _snapshot: (port,),
    ).build(_snapshot(7))
    task = asyncio.create_task(resources.start())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert resources.state is RuntimeGenerationResourceState.FAILED
    assert events[-1] == ("blocking-start", "close", None)
    await resources.close()


@pytest.mark.asyncio
async def test_cancellation_during_failed_start_rollback_is_reraised_after_cleanup() -> None:
    events: list[tuple[str, str, int | None]] = []
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    first = _RecordingLifecyclePort(
        "first",
        events,
        close_entered=close_entered,
        close_release=close_release,
    )
    failing = _RecordingLifecyclePort("failing", events, fail_start=True)
    resources = RuntimeResourceBuilder(
        lifecycle_ports_factory=lambda _snapshot: (first, failing),
    ).build(_snapshot(8))
    task = asyncio.create_task(resources.start())
    await close_entered.wait()

    task.cancel()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert resources.state is RuntimeGenerationResourceState.FAILED
    assert events[-2:] == [
        ("failing", "close", None),
        ("first", "close", None),
    ]
    await resources.close()


@pytest.mark.asyncio
async def test_close_cancellation_finishes_cleanup_then_reraises() -> None:
    events: list[tuple[str, str, int | None]] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    port = _RecordingLifecyclePort(
        "blocking-close",
        events,
        close_entered=entered,
        close_release=release,
    )
    resources = RuntimeResourceBuilder(
        lifecycle_ports_factory=lambda _snapshot: (port,),
    ).build(_snapshot(8))
    await resources.start()

    task = asyncio.create_task(resources.close())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert resources.state is RuntimeGenerationResourceState.CLOSED
    assert events[-1] == ("blocking-close", "close", None)
    await resources.close()


@pytest.mark.asyncio
async def test_close_failure_is_retryable_without_reclosing_successful_ports() -> None:
    events: list[tuple[str, str, int | None]] = []
    first = _RecordingLifecyclePort("first", events)
    flaky = _RecordingLifecyclePort(
        "flaky",
        events,
        fail_close_count=1,
    )
    resources = RuntimeResourceBuilder(
        lifecycle_ports_factory=lambda _snapshot: (first, flaky),
    ).build(_snapshot(9))
    await resources.start()

    with pytest.raises(RuntimeResourceCloseError) as raised:
        await resources.close()
    assert "sensitive" not in str(raised.value)
    assert resources.state is RuntimeGenerationResourceState.FAILED

    await resources.close()
    assert resources.state is RuntimeGenerationResourceState.CLOSED
    assert events.count(("flaky", "close", None)) == 2
    assert events.count(("first", "close", None)) == 1


@pytest.mark.asyncio
async def test_nonempty_usage_queue_blocks_retirement_without_dropping_record() -> None:
    resources = RuntimeResourceBuilder().build(_snapshot(10))
    await resources.start()
    await resources.usage_queue.put(_usage_record())

    with pytest.raises(RuntimeResourceCloseError):
        await resources.close()

    assert resources.state is RuntimeGenerationResourceState.FAILED
    assert resources.usage_queue.state is UsageBatchQueueState.CLOSING
    assert resources.usage_queue.pending_count == 1
    lease = await resources.usage_queue.lease_ready(force=True)
    assert lease is not None
    assert len(lease.records) == 1
    await resources.usage_queue.acknowledge_committed(lease)
    assert resources.usage_queue.state is UsageBatchQueueState.CLOSED
    await resources.close()
    assert resources.state is RuntimeGenerationResourceState.CLOSED


def test_duplicate_lifecycle_names_are_rejected_before_start() -> None:
    events: list[tuple[str, str, int | None]] = []
    first = _RecordingLifecyclePort("duplicate", events)
    second = _RecordingLifecyclePort("duplicate", events)
    with pytest.raises(ValueError, match="不得重复"):
        RuntimeResourceBuilder(
            lifecycle_ports_factory=lambda _snapshot: (first, second),
        ).build(_snapshot(11))
    assert events == []


def test_api_and_structured_log_ports_are_explicitly_composed() -> None:
    sink = _Sink()
    handler = _Handler()
    resources = RuntimeResourceBuilder(
        log_sink_factory=lambda _snapshot: sink,
        api_ports_factory=lambda _snapshot, _metrics: RuntimeApiPorts((handler,)),
    ).build(_snapshot(12))

    assert resources.api_ports.handlers == (handler,)
    assert resources.structured_logger is not None
    resources.structured_logger.emit(
        event="runtime_resources_composed",
        level=StructuredLogLevel.INFO,
    )
    assert len(sink.lines) == 1
    assert '"event":"runtime_resources_composed"' in sink.lines[0]


def test_runner_configuration_requires_a_snapshot_catalog() -> None:
    builder = RuntimeResourceBuilder(
        RuntimeResourceSettings(trusted_runner_tools=("safe_tool",)),
    )
    with pytest.raises(RuntimeResourceConfigurationError, match="Provider"):
        builder.build(_snapshot(13))


@pytest.mark.asyncio
async def test_manager_start_is_idempotent_only_for_same_snapshot_identity() -> None:
    snapshot = _snapshot(14)
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    first = await manager.start(snapshot)

    assert await manager.start(snapshot) is first
    with pytest.raises(RuntimeResourceLifecycleError, match="已启动"):
        await manager.start(_snapshot(14))
    await manager.close()


@pytest.mark.asyncio
async def test_manager_build_failure_restores_created_state_and_redacts_error() -> None:
    manager = RuntimeResourceManager(_FailingFactory())

    with pytest.raises(RuntimeResourceStartupError) as raised:
        await manager.start(_snapshot(15))

    assert "sensitive" not in str(raised.value)
    assert manager.state is RuntimeResourceManagerState.CREATED
    assert manager.current_generation is None


@pytest.mark.asyncio
async def test_manager_start_cancellation_before_publish_closes_candidate() -> None:
    events: list[tuple[str, str, int | None]] = []
    start_entered = asyncio.Event()
    port = _RecordingLifecyclePort(
        "candidate",
        events,
        start_entered=start_entered,
    )
    manager = RuntimeResourceManager(RuntimeResourceBuilder(lifecycle_ports_factory=lambda _snapshot: (port,)))
    _operation_lock, condition = manager._claim_owner()
    await condition.acquire()
    task = asyncio.create_task(manager.start(_snapshot(16)))
    await start_entered.wait()

    task.cancel()
    condition.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.state is RuntimeResourceManagerState.CREATED
    assert manager.current_generation is None
    assert events == [
        ("candidate", "start", 16),
        ("candidate", "close", None),
    ]
    await manager.close()


@pytest.mark.asyncio
async def test_manager_lease_pins_old_generation_until_reload_drains() -> None:
    events: list[tuple[str, str, int | None]] = []
    ports: dict[int, _RecordingLifecyclePort] = {}

    def lifecycle_ports(
        snapshot: RuntimeSnapshot,
    ) -> tuple[_RecordingLifecyclePort, ...]:
        port = _RecordingLifecyclePort(
            f"generation-{snapshot.generation}",
            events,
        )
        ports[snapshot.generation] = port
        return (port,)

    manager = RuntimeResourceManager(RuntimeResourceBuilder(lifecycle_ports_factory=lifecycle_ports))
    first = await manager.start(_snapshot(16))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_first() -> None:
        async with manager.lease(expected_generation=16) as pinned:
            assert pinned is first
            assert manager.active() is first
            entered.set()
            await release.wait()
            assert manager.active() is first
            async with manager.lease(expected_generation=16) as nested:
                assert nested is first
                assert manager.active() is first

    holder = asyncio.create_task(hold_first())
    await entered.wait()
    reload_task = asyncio.create_task(manager.reload(_snapshot(17)))
    await _wait_for_manager_generation(manager, 17)

    assert manager.state is RuntimeResourceManagerState.RELOADING
    assert not reload_task.done()
    async with manager.lease(expected_generation=17) as current:
        assert current.generation == 17
        assert manager.active() is current
    assert ("generation-16", "close", None) not in events

    release.set()
    await holder
    second = await reload_task
    assert second.generation == 17
    assert manager.state is RuntimeResourceManagerState.RUNNING
    assert events.count(("generation-16", "close", None)) == 1
    await manager.close()
    assert events.count(("generation-17", "close", None)) == 1


@pytest.mark.asyncio
async def test_reload_candidate_failure_keeps_previous_generation_active() -> None:
    events: list[tuple[str, str, int | None]] = []

    def lifecycle_ports(
        snapshot: RuntimeSnapshot,
    ) -> tuple[_RecordingLifecyclePort, ...]:
        return (
            _RecordingLifecyclePort(
                f"generation-{snapshot.generation}",
                events,
                fail_start=snapshot.generation == 19,
            ),
        )

    manager = RuntimeResourceManager(RuntimeResourceBuilder(lifecycle_ports_factory=lifecycle_ports))
    first = await manager.start(_snapshot(18))

    with pytest.raises(RuntimeResourceReloadError) as raised:
        await manager.reload(_snapshot(19))

    assert "sensitive" not in str(raised.value)
    assert manager.state is RuntimeResourceManagerState.RUNNING
    assert manager.active() is first
    assert manager.current_generation == 18
    assert ("generation-18", "close", None) not in events
    await manager.close()


@pytest.mark.asyncio
async def test_reload_rejects_stale_or_equal_generation() -> None:
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    await manager.start(_snapshot(20))

    with pytest.raises(RuntimeResourceGenerationError, match="严格递增"):
        await manager.reload(_snapshot(20))
    with pytest.raises(RuntimeResourceGenerationError, match="严格递增"):
        await manager.reload(_snapshot(19))
    assert manager.current_generation == 20
    await manager.close()


@pytest.mark.asyncio
async def test_reload_cancellation_after_publish_settles_handoff_then_reraises() -> None:
    events: list[tuple[str, str, int | None]] = []

    def lifecycle_ports(
        snapshot: RuntimeSnapshot,
    ) -> tuple[_RecordingLifecyclePort, ...]:
        return (
            _RecordingLifecyclePort(
                f"generation-{snapshot.generation}",
                events,
            ),
        )

    manager = RuntimeResourceManager(RuntimeResourceBuilder(lifecycle_ports_factory=lifecycle_ports))
    await manager.start(_snapshot(21))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_old() -> None:
        async with manager.lease(expected_generation=21):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_old())
    await entered.wait()
    reload_task = asyncio.create_task(manager.reload(_snapshot(22)))
    await _wait_for_manager_generation(manager, 22)
    reload_task.cancel()
    release.set()
    await holder

    with pytest.raises(asyncio.CancelledError):
        await reload_task

    assert manager.state is RuntimeResourceManagerState.RUNNING
    assert manager.current_generation == 22
    assert events.count(("generation-21", "close", None)) == 1
    await manager.close()


@pytest.mark.asyncio
async def test_manager_close_waits_for_leases_and_rejects_new_requests() -> None:
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    await manager.start(_snapshot(23))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with manager.lease() as pinned:
            entered.set()
            await release.wait()
            assert manager.active() is pinned

    holder = asyncio.create_task(hold())
    await entered.wait()
    close_task = asyncio.create_task(manager.close())
    await _wait_for_manager_state(manager, RuntimeResourceManagerState.CLOSING)
    assert not close_task.done()
    with pytest.raises(RuntimeResourceLifecycleError, match="不接受"):
        async with manager.lease():
            pass
    release.set()
    await holder
    await close_task
    assert manager.state is RuntimeResourceManagerState.CLOSED
    assert manager.active() is None
    await manager.close()


@pytest.mark.asyncio
async def test_manager_close_cancellation_finishes_shutdown_then_reraises() -> None:
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    await manager.start(_snapshot(24))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with manager.lease():
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await entered.wait()
    close_task = asyncio.create_task(manager.close())
    await _wait_for_manager_state(manager, RuntimeResourceManagerState.CLOSING)
    close_task.cancel()
    release.set()
    await holder

    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert manager.state is RuntimeResourceManagerState.CLOSED
    await manager.close()


@pytest.mark.asyncio
async def test_manager_rejects_cross_process_reuse() -> None:
    pid = [100]
    manager = RuntimeResourceManager(
        RuntimeResourceBuilder(pid_provider=lambda: pid[0]),
        pid_provider=lambda: pid[0],
    )
    await manager.start(_snapshot(25))
    pid[0] = 101

    with pytest.raises(RuntimeResourceOwnershipError, match="跨进程"):
        async with manager.lease():
            pass

    pid[0] = 100
    await manager.close()


@pytest.mark.asyncio
async def test_expected_generation_mismatch_does_not_create_a_lease() -> None:
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    await manager.start(_snapshot(26))

    with pytest.raises(RuntimeResourceGenerationError, match="不匹配"):
        async with manager.lease(expected_generation=27):
            pass

    assert manager.safe_diagnostics()["active_lease_count"] == 0
    await manager.close()


@pytest.mark.asyncio
async def test_lifecycle_operations_inside_lease_fail_fast_without_deadlock() -> None:
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    await manager.start(_snapshot(27))

    async with manager.lease(expected_generation=27):
        with pytest.raises(RuntimeResourceLifecycleError, match="lease 内"):
            await manager.reload(_snapshot(28))
        with pytest.raises(RuntimeResourceLifecycleError, match="lease 内"):
            await manager.close()

    assert manager.state is RuntimeResourceManagerState.RUNNING
    assert manager.current_generation == 27
    await manager.close()


@pytest.mark.asyncio
async def test_child_context_escaping_lease_cannot_reuse_retired_binding() -> None:
    manager = RuntimeResourceManager(RuntimeResourceBuilder())
    first = await manager.start(_snapshot(29))
    inherited = asyncio.Event()
    inspect_released = asyncio.Event()

    async def escaped_child() -> None:
        assert manager.active() is first
        inherited.set()
        await inspect_released.wait()
        assert manager.active() is None
        with pytest.raises(RuntimeResourceLifecycleError, match="已释放"):
            async with manager.lease():
                pass

    async with manager.lease(expected_generation=29):
        child = asyncio.create_task(escaped_child())
        await inherited.wait()

    second = await manager.reload(_snapshot(30))
    assert second.generation == 30
    inspect_released.set()
    await child
    await manager.close()


@pytest.mark.asyncio
async def test_failed_retired_generation_can_be_retried_during_manager_close() -> None:
    events: list[tuple[str, str, int | None]] = []
    ports: dict[int, _RecordingLifecyclePort] = {}

    def lifecycle_ports(
        snapshot: RuntimeSnapshot,
    ) -> tuple[_RecordingLifecyclePort, ...]:
        port = _RecordingLifecyclePort(
            f"generation-{snapshot.generation}",
            events,
            fail_close_count=1 if snapshot.generation == 27 else 0,
        )
        ports[snapshot.generation] = port
        return (port,)

    manager = RuntimeResourceManager(RuntimeResourceBuilder(lifecycle_ports_factory=lifecycle_ports))
    await manager.start(_snapshot(27))
    with pytest.raises(RuntimeResourceReloadError):
        await manager.reload(_snapshot(28))

    assert manager.state is RuntimeResourceManagerState.FAILED
    assert manager.current_generation == 28
    assert manager.safe_diagnostics()["retired_generation_count"] == 1
    await manager.close()
    assert manager.state is RuntimeResourceManagerState.CLOSED
    assert events.count(("generation-27", "close", None)) == 2
    assert events.count(("generation-28", "close", None)) == 1


@pytest.mark.asyncio
async def test_failed_active_generation_is_retained_for_manager_close_retry() -> None:
    events: list[tuple[str, str, int | None]] = []
    port = _RecordingLifecyclePort(
        "active",
        events,
        fail_close_count=1,
    )
    manager = RuntimeResourceManager(RuntimeResourceBuilder(lifecycle_ports_factory=lambda _snapshot: (port,)))
    await manager.start(_snapshot(29))

    with pytest.raises(RuntimeResourceCloseError):
        await manager.close()

    assert manager.state is RuntimeResourceManagerState.FAILED
    assert manager.current_generation is None
    assert manager.safe_diagnostics()["retired_generation_count"] == 1
    await manager.close()
    assert manager.state is RuntimeResourceManagerState.CLOSED
    assert events.count(("active", "close", None)) == 2


def test_invalid_generation_is_rejected_before_any_lifecycle_start() -> None:
    events: list[tuple[str, str, int | None]] = []
    port = _RecordingLifecyclePort("never-started", events)
    with pytest.raises(ValueError, match="正 PostgreSQL BIGINT"):
        RuntimeResourceBuilder(
            lifecycle_ports_factory=lambda _snapshot: (port,),
        ).build(_snapshot(0))
    assert events == []

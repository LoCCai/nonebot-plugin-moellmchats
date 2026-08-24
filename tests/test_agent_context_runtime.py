from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    create_async_engine,
)

from nonebot_plugin_moellmchats import moe_llm as moe_llm_module
from nonebot_plugin_moellmchats.agent_context_runtime import (
    AgentContextCommitCancellationUnknownError,
    AgentContextCommitUnknownError,
    AgentContextPersistenceError,
    AgentGenerationCoordinator,
    AgentPromptContext,
    AgentRequestIdentity,
    AgentRequestRuntime,
    PostgresTransactionFactory,
    RuntimeResourceHost,
    SessionSummaryGeneration,
)
from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRunState,
    AgentStepStatus,
    DeadlineContext,
    ToolCallStatus,
)
from nonebot_plugin_moellmchats.chat_history import MessageRecord
from nonebot_plugin_moellmchats.database_engine import (
    DatabaseEngineManager,
    DatabaseEngineSettings,
)
from nonebot_plugin_moellmchats.history_hot_cache import (
    HistoryCacheLoadToken,
    HistoryCacheLookup,
)
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin
from nonebot_plugin_moellmchats.llm_state import token_usage_history
from nonebot_plugin_moellmchats.long_term_memory import (
    LongTermMemoryKind,
    LongTermMemoryMatch,
    LongTermMemoryQuery,
    LongTermMemoryRecord,
    LongTermMemoryScope,
    LongTermMemoryScopeKind,
    LongTermMemoryService,
)
import nonebot_plugin_moellmchats.messages_handler as messages_handler_module
from nonebot_plugin_moellmchats.messages_handler import (
    MessagesHandler,
    messages_dict,
)
from nonebot_plugin_moellmchats.repositories import RepositoryPage
from nonebot_plugin_moellmchats.runtime_resources import (
    RuntimeGenerationResources,
    RuntimePostgresRepositories,
    RuntimeResourceBuilder,
    RuntimeResourceSettings,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot, runtime_snapshots
from nonebot_plugin_moellmchats.session_summary import (
    SessionSummaryPlan,
    SessionSummaryPolicy,
)
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot
from nonebot_plugin_moellmchats.tool_providers import ToolSource

if TYPE_CHECKING:
    from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord

_NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)


def _snapshot(generation: int = 1) -> RuntimeSnapshot:
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


def _identity(*, group_id: str | None = "20001") -> AgentRequestIdentity:
    return AgentRequestIdentity(
        platform="onebot-v11",
        platform_user_id="10001",
        group_id=group_id,
        display_name="Moe",
        platform_message_id="30001",
    )


def _message(message_id: int, *, role: str = "user") -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        conversation_id=_identity().conversation_id,
        platform_message_id=f"platform-{message_id}",
        role=role,
        sender_id=_identity().user_id if role == "user" else None,
        content=f"message-{message_id}",
        created_at=_NOW + timedelta(seconds=message_id),
    )


def _database_resources(
    generation: int = 1,
) -> tuple[RuntimeGenerationResources, AsyncEngine]:
    engine = create_async_engine(
        "postgresql+asyncpg://user:password@db.invalid/database",
    )

    def manager_factory(
        settings: DatabaseEngineSettings,
    ) -> DatabaseEngineManager:
        return DatabaseEngineManager(
            settings,
            engine_factory=lambda *_args, **_kwargs: engine,
        )

    settings = RuntimeResourceSettings(
        database=DatabaseEngineSettings(database_url="postgresql+asyncpg://user:password@db.invalid/database")
    )
    resources = RuntimeResourceBuilder(
        settings,
        database_manager_factory=manager_factory,
    ).build(_snapshot(generation))
    return resources, engine


class _RecordingSession(AsyncSession):
    def __init__(
        self,
        events: list[str],
        *,
        commit_error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self.events = events
        self.commit_error = commit_error

    def begin(self) -> AsyncSessionTransaction:
        self.events.append("begin")
        return super().begin()

    async def commit(self) -> None:
        self.events.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.events.append("close")


class _FakeTransactions(PostgresTransactionFactory):
    def __init__(self, repositories: object, events: list[str]) -> None:
        self.repositories = repositories
        self.events = events

    async def execute(self, operation):
        self.events.append("transaction.begin")
        result = await operation(cast("RuntimePostgresRepositories", self.repositories))
        self.events.append("transaction.commit")
        return result


class _StatefulTransactions(PostgresTransactionFactory):
    def __init__(self, repositories: object, events: list[str]) -> None:
        self.repositories = repositories
        self.events = events
        self.active = False

    async def execute(self, operation):
        assert self.active is False
        self.active = True
        self.events.append("transaction.begin")
        try:
            result = await operation(cast("RuntimePostgresRepositories", self.repositories))
        except asyncio.CancelledError:
            self.events.append("transaction.rollback")
            raise
        except Exception as error:
            self.events.append("transaction.rollback")
            raise AgentContextPersistenceError(f"fake transaction failed ({type(error).__name__})") from None
        finally:
            self.active = False
        self.events.append("transaction.commit")
        return result


class _HistoryCache:
    def __init__(
        self,
        events: list[str],
        *,
        lookup_error: BaseException | None = None,
        invalidate_error: BaseException | None = None,
        publish_result: bool = True,
    ) -> None:
        self.events = events
        self.lookup_error = lookup_error
        self.invalidate_error = invalidate_error
        self.publish_result = publish_result
        self.lookup_calls = 0

    async def lookup(self, conversation_id: str, *, limit: int):
        del conversation_id, limit
        self.events.append("cache.lookup")
        self.lookup_calls += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        return HistoryCacheLookup(
            load_token=HistoryCacheLoadToken(
                conversation_fingerprint="a" * 64,
                generation="b" * 32,
                expires_at=10_000,
            )
        )

    async def publish(self, load_token, window):
        del load_token, window
        self.events.append("cache.publish")
        return self.publish_result

    async def invalidate(self, conversation_id: str) -> None:
        del conversation_id
        self.events.append("cache.invalidate")
        if self.invalidate_error is not None:
            raise self.invalidate_error


def test_request_identity_is_deterministic_redacted_and_exactly_scoped() -> None:
    group = _identity()
    private = _identity(group_id=None)

    assert group.user_id == private.user_id
    assert group.conversation_type == "group"
    assert private.conversation_type == "private"
    assert group.conversation_id != private.conversation_id
    assert "10001" not in repr(group)
    assert "20001" not in repr(group)

    event_identity = AgentRequestIdentity.from_event(
        SimpleNamespace(
            user_id=10001,
            group_id=20001,
            message_id=30001,
            sender=SimpleNamespace(card="  Moe  ", nickname="fallback"),
        )
    )
    assert event_identity == group


def test_prompt_context_labels_summary_and_memory_as_untrusted_data() -> None:
    context = AgentPromptContext(
        conversation_id=_identity().conversation_id,
        history=(_message(1), _message(2, role="assistant")),
    )
    assert context.render_untrusted_prompt() == ""

    with pytest.raises(ValueError, match="严格递增"):
        AgentPromptContext(
            conversation_id=_identity().conversation_id,
            history=(_message(2), _message(1)),
        )


def test_messages_handler_uses_committed_history_without_memory_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages_dict.clear()
    monkeypatch.setattr(messages_handler_module.time, "time", lambda: _NOW.timestamp())
    first = _message(1)
    assistant = MessageRecord(
        message_id=2,
        conversation_id=first.conversation_id,
        role="assistant",
        content="done",
        structured_content={
            "tool_messages": [
                {
                    "role": "assistant",
                    "content": "calling",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "search", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
            ]
        },
        created_at=first.created_at + timedelta(seconds=1),
    )
    handler = MessagesHandler("10001")

    handler.bind_committed_history((first, assistant))
    handler.pre_process(
        {
            "images": [],
            "reply": "",
            "reply_user": None,
            "current_user": {"qq": "10001"},
            "text": ["new question"],
        }
    )

    assert handler.durable_history_bound is True
    assert handler.get_all_used_plugins() == {"search"}
    assert handler.get_send_message_list()[-1] == {
        "role": "user",
        "content": "new question",
    }
    # Durable mode never appends its in-flight response to the process-local
    # compatibility dictionary; the next request must reload PostgreSQL truth.
    handler.post_process("new response")
    assert messages_dict.get("10001") is None


@pytest.mark.asyncio
async def test_default_host_memory_request_has_zero_database_or_redis_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_calls = 0

    def reject_engine(self: DatabaseEngineManager) -> AsyncEngine:
        del self
        nonlocal database_calls
        database_calls += 1
        raise AssertionError("unexpected database engine")

    monkeypatch.setattr(DatabaseEngineManager, "get_engine", reject_engine)
    host = RuntimeResourceHost()
    snapshot = _snapshot()
    await host.start(snapshot)

    async with host.lease(snapshot) as coordinator:
        runtime = await AgentRequestRuntime.begin(
            coordinator,
            _identity(),
            request_id=1,
            deadline=DeadlineContext.from_timeout(30),
            wall_clock=lambda: _NOW.timestamp(),
        )
        assert runtime.run.state is AgentRunState.CLASSIFYING
        assert [run.state for run in runtime.run_history] == [
            AgentRunState.CREATED,
            AgentRunState.ADMITTED,
            AgentRunState.CLASSIFYING,
        ]
        await runtime.advance(AgentRunState.PLANNING, model="chat-model")
        await runtime.advance(AgentRunState.EXECUTING)
        context = await runtime.prepare_context("hello", history_limit=8)
        assert context.history == ()
        await runtime.persist_assistant_message("world")
        runtime.capture_usage(
            provider="provider",
            model="chat-model",
            input_tokens=3,
            output_tokens=2,
            reasoning_tokens=0,
            cached_tokens=0,
        )
        await runtime.record_model_step(
            model="chat-model",
            status=AgentStepStatus.COMPLETED,
            started_at=_NOW.timestamp(),
            started_monotonic=0,
            output_preview="model response accepted",
        )
        await runtime.finish_success()
        assert runtime.run.state is AgentRunState.COMPLETED
        assert runtime.run.input_tokens == 3
        assert runtime.run.output_tokens == 2
        assert len(runtime.steps) == 1

    await host.close()
    assert database_calls == 0


@pytest.mark.asyncio
async def test_host_accepts_same_generation_patched_request_snapshot() -> None:
    host = RuntimeResourceHost()
    original = _snapshot(1)
    resources = await host.start(original)
    patched = replace(
        original,
        config={"request_timeout_seconds": 45},
    )

    try:
        async with host.lease(patched) as coordinator:
            assert coordinator.generation == patched.generation
            assert coordinator.resources is resources
            assert coordinator.resources.snapshot is original
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_postgres_transaction_is_short_and_commits_once() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    session = _RecordingSession(events)
    factory = PostgresTransactionFactory(
        resources,
        sessionmaker_factory=lambda *_args, **_kwargs: lambda: session,
    )

    async def operation(repositories) -> str:
        assert repositories is not None
        events.append("operation")
        return "done"

    assert await factory.execute(operation) == "done"
    assert events == ["begin", "operation", "commit", "close"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_commit_unknown_is_not_rolled_back_or_replayed() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    session = _RecordingSession(
        events,
        commit_error=RuntimeError("private commit detail"),
    )
    factory = PostgresTransactionFactory(
        resources,
        sessionmaker_factory=lambda *_args, **_kwargs: lambda: session,
    )
    operation_calls = 0

    async def operation(_repositories) -> None:
        nonlocal operation_calls
        operation_calls += 1
        events.append("operation")

    with pytest.raises(AgentContextCommitUnknownError) as error:
        await factory.execute(operation)

    assert operation_calls == 1
    assert events == ["begin", "operation", "commit", "close"]
    assert "private commit detail" not in str(error.value)
    await engine.dispose()


@pytest.mark.asyncio
async def test_commit_cancellation_is_unknown_and_still_propagates_cancellation() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    session = _RecordingSession(
        events,
        commit_error=asyncio.CancelledError(),
    )
    factory = PostgresTransactionFactory(
        resources,
        sessionmaker_factory=lambda *_args, **_kwargs: lambda: session,
    )
    operation_calls = 0

    async def operation(_repositories) -> None:
        nonlocal operation_calls
        operation_calls += 1
        events.append("operation")

    with pytest.raises(AgentContextCommitCancellationUnknownError) as error:
        await factory.execute(operation)

    assert isinstance(error.value, AgentContextCommitUnknownError)
    assert isinstance(error.value, asyncio.CancelledError)
    assert operation_calls == 1
    assert events == ["begin", "operation", "commit", "close"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_write_failure_rolls_back_once_and_is_sanitized() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    session = _RecordingSession(events)
    factory = PostgresTransactionFactory(
        resources,
        sessionmaker_factory=lambda *_args, **_kwargs: lambda: session,
    )
    operation_calls = 0

    async def operation(_repositories) -> None:
        nonlocal operation_calls
        operation_calls += 1
        events.append("operation")
        raise RuntimeError("private write detail")

    with pytest.raises(AgentContextPersistenceError) as error:
        await factory.execute(operation)

    assert operation_calls == 1
    assert events == ["begin", "operation", "rollback", "close"]
    assert "private write detail" not in str(error.value)
    await engine.dispose()


@pytest.mark.asyncio
async def test_transaction_cancellation_rolls_back_closes_and_propagates() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    session = _RecordingSession(events)
    factory = PostgresTransactionFactory(
        resources,
        sessionmaker_factory=lambda *_args, **_kwargs: lambda: session,
    )

    async def operation(_repositories) -> None:
        events.append("operation")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await factory.execute(operation)

    assert events == ["begin", "operation", "rollback", "close"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_cache_failure_bypasses_entire_generation() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    cache = _HistoryCache(events, lookup_error=RuntimeError("redis secret"))
    resources._history_cache = cache

    class Messages:
        async def list_recent(self, conversation_id, page):
            del conversation_id, page
            events.append("repository.list_recent")
            return RepositoryPage((_message(1),))

    repositories = SimpleNamespace(message=Messages())
    transactions = _FakeTransactions(repositories, events)
    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: transactions,
    )

    first = await coordinator.load_history(_identity().conversation_id, limit=8)
    second = await coordinator.load_history(_identity().conversation_id, limit=8)

    assert first == second == (_message(1),)
    assert cache.lookup_calls == 1
    assert coordinator.cache_trusted is False
    assert events == [
        "cache.lookup",
        "transaction.begin",
        "repository.list_recent",
        "transaction.commit",
        "transaction.begin",
        "repository.list_recent",
        "transaction.commit",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_request_identity_and_state_use_separate_short_transactions() -> None:
    resources, engine = _database_resources()
    events: list[str] = []

    class Users:
        async def resolve(self, user):
            events.append("user.resolve")
            return user

    class Conversations:
        async def resolve(self, conversation):
            events.append("conversation.resolve")
            return conversation

    class Runs:
        async def create(self, run):
            events.append(f"run.create:{run.state.value}")

        async def replace(self, run, *, expected_state, expected_generation):
            del expected_generation
            events.append(f"run.replace:{expected_state.value}->{run.state.value}")

    class Audits:
        async def append(self, audit):
            events.append(f"audit.append:{audit.event_type}")

    repositories = SimpleNamespace(
        user=Users(),
        conversation=Conversations(),
        agent_run=Runs(),
        audit=Audits(),
    )
    transactions = _StatefulTransactions(repositories, events)
    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: transactions,
    )

    runtime = await AgentRequestRuntime.begin(
        coordinator,
        _identity(group_id=None),
        request_id=7,
        deadline=DeadlineContext.from_timeout(30),
        wall_clock=lambda: _NOW.timestamp(),
    )

    assert runtime.run.state is AgentRunState.CLASSIFYING
    assert events == [
        "transaction.begin",
        "user.resolve",
        "conversation.resolve",
        "run.create:created",
        "audit.append:agent_run.created",
        "transaction.commit",
        "transaction.begin",
        "run.replace:created->admitted",
        "audit.append:agent_run.admitted",
        "transaction.commit",
        "transaction.begin",
        "run.replace:admitted->classifying",
        "audit.append:agent_run.classifying",
        "transaction.commit",
    ]
    assert transactions.active is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_cache_publish_rejection_bypasses_generation() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    cache = _HistoryCache(events, publish_result=False)
    resources._history_cache = cache

    class Messages:
        async def list_recent(self, conversation_id, page):
            del conversation_id, page
            events.append("repository.list_recent")
            return RepositoryPage((_message(1),))

    transactions = _FakeTransactions(
        SimpleNamespace(message=Messages()),
        events,
    )
    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: transactions,
    )

    assert await coordinator.load_history(
        _identity().conversation_id,
        limit=8,
    ) == (_message(1),)
    assert coordinator.cache_trusted is False
    assert events[-1] == "cache.publish"
    await engine.dispose()


@pytest.mark.asyncio
async def test_durable_message_commit_precedes_cache_invalidation() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    cache = _HistoryCache(events)
    resources._history_cache = cache

    class Messages:
        async def append(self, message: MessageRecord) -> None:
            assert not message.persisted
            events.append("repository.append")

    transactions = _FakeTransactions(
        SimpleNamespace(message=Messages()),
        events,
    )
    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: transactions,
    )
    draft = MessageRecord(
        message_id=None,
        conversation_id=_identity().conversation_id,
        role="assistant",
        content="done",
        created_at=_NOW,
    )

    await coordinator.append_message(draft)

    assert events == [
        "transaction.begin",
        "repository.append",
        "transaction.commit",
        "cache.invalidate",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_message_commit_bypasses_cache_without_replay() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    cache = _HistoryCache(events)
    resources._history_cache = cache
    append_calls = 0

    class Messages:
        async def append(self, _message: MessageRecord) -> None:
            nonlocal append_calls
            append_calls += 1
            events.append("repository.append")

    class UnknownTransactions(PostgresTransactionFactory):
        def __init__(self) -> None:
            pass

        async def execute(self, operation):
            events.append("transaction.begin")
            await operation(
                cast(
                    "RuntimePostgresRepositories",
                    SimpleNamespace(message=Messages()),
                )
            )
            events.append("transaction.commit_unknown")
            raise AgentContextCommitUnknownError("commit unknown")

    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: UnknownTransactions(),
    )
    draft = MessageRecord(
        message_id=None,
        conversation_id=_identity().conversation_id,
        role="assistant",
        content="done",
        created_at=_NOW,
    )

    with pytest.raises(AgentContextCommitUnknownError):
        await coordinator.append_message(draft)

    assert append_calls == 1
    assert coordinator.cache_trusted is False
    assert events == [
        "transaction.begin",
        "repository.append",
        "transaction.commit_unknown",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_cache_invalidation_cancellation_bypasses_generation() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    cache = _HistoryCache(
        events,
        invalidate_error=asyncio.CancelledError(),
    )
    resources._history_cache = cache

    class Messages:
        async def append(self, _message: MessageRecord) -> None:
            events.append("repository.append")

    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: _FakeTransactions(
            SimpleNamespace(message=Messages()),
            events,
        ),
    )
    draft = MessageRecord(
        message_id=None,
        conversation_id=_identity().conversation_id,
        role="assistant",
        content="done",
        created_at=_NOW,
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.append_message(draft)

    assert coordinator.cache_trusted is False
    assert events == [
        "transaction.begin",
        "repository.append",
        "transaction.commit",
        "cache.invalidate",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_long_term_memory_is_omitted_when_no_service_is_injected() -> None:
    resources = RuntimeResourceBuilder().build(_snapshot())
    coordinator = AgentGenerationCoordinator(resources)

    context = await coordinator.prepare_prompt_context(
        _identity(),
        user_id=_identity().user_id,
        conversation_id=_identity().conversation_id,
        text="hello",
        requested_at=_NOW,
        deadline=DeadlineContext.from_timeout(30),
        history_limit=8,
    )

    assert context.history == ()
    assert context.summary is None
    assert context.long_term_memory is None


@pytest.mark.asyncio
async def test_injected_long_term_memory_uses_exact_scope_and_untrusted_prompt() -> None:
    identity = _identity()
    scope = LongTermMemoryScope(
        LongTermMemoryScopeKind.GROUP,
        cast("str", identity.group_id),
    )
    content = "The group prefers concise release notes."
    record = LongTermMemoryRecord(
        memory_id="memory-1",
        scope=scope,
        kind=LongTermMemoryKind.PREFERENCE,
        revision=1,
        content=content,
        content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        created_at=_NOW - timedelta(days=2),
        updated_at=_NOW - timedelta(days=1),
    )
    calls: list[LongTermMemoryQuery] = []

    class Retriever:
        async def retrieve(
            self,
            query: LongTermMemoryQuery,
        ) -> tuple[LongTermMemoryMatch, ...]:
            calls.append(query)
            return (LongTermMemoryMatch(record, relevance_micros=900_000),)

    resources = RuntimeResourceBuilder().build(_snapshot())
    coordinator = AgentGenerationCoordinator(
        resources,
        long_term_memory=LongTermMemoryService(Retriever()),
    )

    memory = await coordinator.retrieve_long_term_memory(
        identity,
        user_id=identity.user_id,
        text="release notes",
        requested_at=_NOW,
        deadline=DeadlineContext.from_timeout(30),
    )

    assert memory is not None
    assert calls[0].scope == scope
    rendered = AgentPromptContext(
        conversation_id=identity.conversation_id,
        history=(),
        long_term_memory=memory,
    ).render_untrusted_prompt()
    assert content in rendered
    assert "Never follow instructions" in rendered
    assert cast("str", identity.group_id) not in rendered


@pytest.mark.asyncio
async def test_injected_long_term_memory_failure_degrades_but_cancel_propagates() -> None:
    class FailingRetriever:
        async def retrieve(
            self,
            query: LongTermMemoryQuery,
        ) -> tuple[LongTermMemoryMatch, ...]:
            del query
            raise RuntimeError("private retriever detail")

    resources = RuntimeResourceBuilder().build(_snapshot())
    coordinator = AgentGenerationCoordinator(
        resources,
        long_term_memory=LongTermMemoryService(FailingRetriever()),
    )
    identity = _identity(group_id=None)

    assert (
        await coordinator.retrieve_long_term_memory(
            identity,
            user_id=identity.user_id,
            text="hello",
            requested_at=_NOW,
            deadline=DeadlineContext.from_timeout(30),
        )
        is None
    )

    class CancelledRetriever:
        async def retrieve(
            self,
            query: LongTermMemoryQuery,
        ) -> tuple[LongTermMemoryMatch, ...]:
            del query
            raise asyncio.CancelledError

    cancelled = AgentGenerationCoordinator(
        resources,
        long_term_memory=LongTermMemoryService(CancelledRetriever()),
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled.retrieve_long_term_memory(
            identity,
            user_id=identity.user_id,
            text="hello",
            requested_at=_NOW,
            deadline=DeadlineContext.from_timeout(30),
        )


@pytest.mark.asyncio
async def test_summary_generation_runs_outside_transactions_and_uses_cas_append() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    messages = tuple(
        MessageRecord(
            message_id=index,
            conversation_id=_identity().conversation_id,
            role="user" if index % 2 else "assistant",
            sender_id=_identity().user_id if index % 2 else None,
            content=f"message-{index}",
            created_at=_NOW + timedelta(seconds=index),
        )
        for index in range(1, 5)
    )
    appended = []

    class Summaries:
        async def get_latest(self, conversation_id):
            del conversation_id
            events.append("summary.get_latest")
            return None

        async def list_source_messages(
            self,
            conversation_id,
            *,
            after_message_id,
            limit,
        ):
            del conversation_id, after_message_id
            assert limit == 4
            events.append("summary.list_source_messages")
            return messages

        async def append(self, summary, *, expected_previous_summary_id):
            assert expected_previous_summary_id is None
            events.append("summary.append")
            appended.append(summary)

    repositories = SimpleNamespace(session_summary=Summaries())
    transactions = _StatefulTransactions(repositories, events)

    class Generator:
        async def generate(self, plan, deadline):
            assert transactions.active is False
            assert plan.source_messages
            assert deadline.remaining() > 0
            events.append("generator.generate")
            return SessionSummaryGeneration(
                provider="provider",
                model="summary-model",
                content="bounded summary",
            )

    coordinator = AgentGenerationCoordinator(
        resources,
        summary_generator=Generator(),
        summary_policy=SessionSummaryPolicy(
            trigger_message_count=4,
            keep_recent_message_count=1,
        ),
        transaction_factory_factory=lambda _resources: transactions,
    )

    result = await coordinator.maybe_generate_summary(
        _identity().conversation_id,
        deadline=DeadlineContext.from_timeout(30),
        now=lambda: (_NOW + timedelta(minutes=2)).timestamp(),
    )

    assert result is appended[0]
    assert events == [
        "transaction.begin",
        "summary.get_latest",
        "summary.list_source_messages",
        "transaction.commit",
        "generator.generate",
        "transaction.begin",
        "summary.append",
        "transaction.commit",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_summary_generator_failure_degrades_without_advancing_watermark() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    messages = tuple(_message(index) for index in range(1, 5))

    class Summaries:
        async def get_latest(self, _conversation_id):
            events.append("summary.get_latest")
            return None

        async def list_source_messages(
            self,
            _conversation_id,
            *,
            after_message_id,
            limit,
        ):
            assert after_message_id is None
            assert limit == 4
            events.append("summary.list_source_messages")
            return messages

        async def append(self, _summary, *, expected_previous_summary_id):
            del expected_previous_summary_id
            events.append("summary.append")

    transactions = _StatefulTransactions(
        SimpleNamespace(session_summary=Summaries()),
        events,
    )

    class Generator:
        async def generate(
            self,
            plan: SessionSummaryPlan,
            deadline: DeadlineContext,
        ) -> SessionSummaryGeneration:
            del plan, deadline
            assert transactions.active is False
            events.append("generator.generate")
            raise RuntimeError("private summary provider detail")

    coordinator = AgentGenerationCoordinator(
        resources,
        summary_generator=Generator(),
        summary_policy=SessionSummaryPolicy(
            trigger_message_count=4,
            keep_recent_message_count=1,
        ),
        transaction_factory_factory=lambda _resources: transactions,
    )

    assert (
        await coordinator.maybe_generate_summary(
            _identity().conversation_id,
            deadline=DeadlineContext.from_timeout(30),
        )
        is None
    )
    assert events == [
        "transaction.begin",
        "summary.get_latest",
        "summary.list_source_messages",
        "transaction.commit",
        "generator.generate",
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_summary_generator_cancellation_propagates_without_append() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    messages = tuple(_message(index) for index in range(1, 5))

    class Summaries:
        async def get_latest(self, _conversation_id):
            return None

        async def list_source_messages(
            self,
            _conversation_id,
            *,
            after_message_id,
            limit,
        ):
            del after_message_id, limit
            return messages

        async def append(self, _summary, *, expected_previous_summary_id):
            del expected_previous_summary_id
            events.append("summary.append")

    transactions = _StatefulTransactions(
        SimpleNamespace(session_summary=Summaries()),
        events,
    )

    class Generator:
        async def generate(
            self,
            plan: SessionSummaryPlan,
            deadline: DeadlineContext,
        ) -> SessionSummaryGeneration:
            del plan, deadline
            raise asyncio.CancelledError

    coordinator = AgentGenerationCoordinator(
        resources,
        summary_generator=Generator(),
        summary_policy=SessionSummaryPolicy(
            trigger_message_count=4,
            keep_recent_message_count=1,
        ),
        transaction_factory_factory=lambda _resources: transactions,
    )

    with pytest.raises(asyncio.CancelledError):
        await coordinator.maybe_generate_summary(
            _identity().conversation_id,
            deadline=DeadlineContext.from_timeout(30),
        )
    assert "summary.append" not in events
    await engine.dispose()


@pytest.mark.asyncio
async def test_persistent_steps_tools_usage_and_audit_are_transaction_bound() -> None:
    resources, engine = _database_resources()
    events: list[str] = []
    usage_batches: list[tuple[object, ...]] = []

    class Users:
        async def resolve(self, user):
            return user

    class Conversations:
        async def resolve(self, conversation):
            return conversation

    class Runs:
        async def create(self, run):
            events.append(f"run.create:{run.state.value}")

        async def replace(self, run, *, expected_state, expected_generation):
            del expected_generation
            events.append(f"run.replace:{expected_state.value}->{run.state.value}")

    class Steps:
        async def append(self, step):
            events.append(f"step.append:{step.index}:{step.status.value}")

    class Calls:
        async def create(self, call):
            events.append(f"tool.create:{call.status.value}")

    class Usage:
        async def append_batch(self, records):
            usage_batches.append(tuple(records))
            events.append(f"usage.append_batch:{len(records)}")

    class Audits:
        async def append(self, audit):
            events.append(f"audit.append:{audit.event_type}")

    transactions = _StatefulTransactions(
        SimpleNamespace(
            user=Users(),
            conversation=Conversations(),
            agent_run=Runs(),
            agent_step=Steps(),
            tool_call=Calls(),
            usage=Usage(),
            audit=Audits(),
        ),
        events,
    )
    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: transactions,
    )
    runtime = await AgentRequestRuntime.begin(
        coordinator,
        _identity(),
        request_id=8,
        deadline=DeadlineContext.from_timeout(30),
        wall_clock=lambda: _NOW.timestamp(),
        monotonic_clock=lambda: 11.0,
    )
    await runtime.advance(AgentRunState.PLANNING, model="model")
    await runtime.advance(AgentRunState.EXECUTING)
    events.clear()

    await runtime.record_model_step(
        model="model",
        status=AgentStepStatus.COMPLETED,
        started_at=_NOW.timestamp(),
        started_monotonic=10.0,
        output_preview="accepted",
    )
    await runtime.record_tool_outcome(
        tool_name="safe_tool",
        source=ToolSource.REGISTERED,
        bundle_id=None,
        bundle_digest=None,
        arguments={"query": "detached"},
        status=ToolCallStatus.FAILED,
        created_at=_NOW.timestamp(),
        started_monotonic=10.0,
        error_type="ToolExecutionError",
    )
    runtime.capture_usage(
        provider="provider",
        model="model",
        input_tokens=7,
        output_tokens=3,
        reasoning_tokens=1,
        cached_tokens=2,
    )
    await runtime.finish_exception(
        AgentRunState.FAILED,
        RuntimeError("private request failure"),
    )

    assert events == [
        "transaction.begin",
        "step.append:0:completed",
        "audit.append:agent_step.completed",
        "transaction.commit",
        "transaction.begin",
        "step.append:1:failed",
        "tool.create:failed",
        "audit.append:agent_tool.failed",
        "transaction.commit",
        "transaction.begin",
        "usage.append_batch:1",
        "transaction.commit",
        "transaction.begin",
        "run.replace:executing->failed",
        "audit.append:agent_run.failed",
        "transaction.commit",
    ]
    assert len(usage_batches) == 1
    assert runtime.run.state is AgentRunState.FAILED
    assert runtime.run.error_type == "RuntimeError"
    assert runtime.run.error_message is not None
    assert "private request failure" not in runtime.run.error_message
    assert runtime.tool_calls[0].tool_source is ToolSource.REGISTERED
    assert transactions.active is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_persistence_failure_never_fakes_local_completion() -> None:
    resources, engine = _database_resources()
    events: list[str] = []

    class Users:
        async def resolve(self, user):
            return user

    class Conversations:
        async def resolve(self, conversation):
            return conversation

    class Runs:
        async def create(self, run):
            events.append(f"run.create:{run.state.value}")

        async def replace(self, run, *, expected_state, expected_generation):
            del expected_generation
            events.append(f"run.replace:{expected_state.value}->{run.state.value}")
            if run.state is AgentRunState.COMPLETED:
                raise RuntimeError("private terminal write detail")

    class Audits:
        async def append(self, audit):
            events.append(f"audit.append:{audit.event_type}")

    transactions = _StatefulTransactions(
        SimpleNamespace(
            user=Users(),
            conversation=Conversations(),
            agent_run=Runs(),
            audit=Audits(),
        ),
        events,
    )
    coordinator = AgentGenerationCoordinator(
        resources,
        transaction_factory_factory=lambda _resources: transactions,
    )
    runtime = await AgentRequestRuntime.begin(
        coordinator,
        _identity(),
        request_id=9,
        deadline=DeadlineContext.from_timeout(30),
        wall_clock=lambda: _NOW.timestamp(),
    )
    await runtime.advance(AgentRunState.PLANNING, model="model")
    await runtime.advance(AgentRunState.EXECUTING)
    events.clear()

    with pytest.raises(AgentContextPersistenceError) as error:
        await runtime.finish_success()

    assert "private terminal write detail" not in str(error.value)
    assert runtime.run.state is AgentRunState.SUMMARIZING
    assert runtime.run_history[-1].state is AgentRunState.SUMMARIZING
    assert events == [
        "transaction.begin",
        "run.replace:executing->summarizing",
        "audit.append:agent_run.summarizing",
        "transaction.commit",
        "transaction.begin",
        "run.replace:summarizing->completed",
        "transaction.rollback",
    ]
    assert transactions.active is False
    await engine.dispose()


def test_untrusted_usage_is_normalized_and_never_breaks_chat() -> None:
    captured: list[dict[str, object]] = []

    class Runtime:
        def capture_usage(self, **values: object) -> None:
            captured.append(values)

    class Harness(LlmApiMixin):
        def __init__(self) -> None:
            self.model_info = {"provider": "provider", "model": "model"}
            self.agent_runtime = Runtime()

    token_usage_history.clear()
    harness = Harness()

    harness._record_token_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": "forged",
            "prompt_tokens_details": {"cached_tokens": 999},
            "completion_tokens_details": {"reasoning_tokens": 999},
        }
    )
    harness._record_token_usage({"prompt_tokens": True, "completion_tokens": -1})
    harness._record_token_usage(object())

    assert captured[0] == {
        "provider": "provider",
        "model": "model",
        "input_tokens": 10,
        "output_tokens": 4,
        "reasoning_tokens": 4,
        "cached_tokens": 10,
    }
    assert token_usage_history[-1]["total"] == 14
    assert token_usage_history[0]["total"] == 0


@pytest.mark.asyncio
async def test_moe_llm_full_fake_model_call_flushes_usage_and_finishes_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages_dict.clear()
    current = runtime_snapshots.current()
    generation = 1 if current is None else current.generation + 1
    tool_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    snapshot = RuntimeSnapshot(
        generation=generation,
        config={},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=tool_snapshot,
        emotions=(),
        reloaded_at=float(generation),
    )
    runtime_snapshots.publish(snapshot)
    usage_batches: list[tuple[ModelUsageRecord, ...]] = []
    durable_messages: list[MessageRecord] = []

    class RecordingCoordinator(AgentGenerationCoordinator):
        async def persist_usage(
            self,
            records: tuple[ModelUsageRecord, ...],
        ) -> bool:
            usage_batches.append(records)
            return True

        async def append_message(self, message: MessageRecord) -> None:
            durable_messages.append(message)

    coordinator = RecordingCoordinator(RuntimeResourceBuilder().build(snapshot))
    runtime = await AgentRequestRuntime.begin(
        coordinator,
        AgentRequestIdentity(
            platform="onebot-v11",
            platform_user_id="10001",
            group_id=None,
            display_name="Moe",
            platform_message_id="30001",
        ),
        request_id=10,
        deadline=DeadlineContext.from_timeout(30),
    )
    config_values = {
        "emotions_enabled": False,
        "max_agent_steps": 6,
        "max_history_chars": 16_000,
        "max_history_tokens": 4_000,
        "max_retry_times": 1,
        "max_tool_rounds": 2,
        "max_user_history": 8,
        "request_timeout_seconds": 30,
        "show_datetime": False,
        "user_history_expire_seconds": 3_600,
    }
    monkeypatch.setattr(
        moe_llm_module.config_parser,
        "get_config",
        lambda key, default=None: config_values.get(key, default),
    )
    model_info = {
        "provider": "fake-provider",
        "model": "fake-model",
        "key": "test-only-key",
        "url": "https://model.invalid/chat",
        "stream": False,
    }
    monkeypatch.setattr(
        moe_llm_module.model_selector,
        "get_model",
        lambda name: model_info if name == "selected_model" else None,
    )
    monkeypatch.setattr(moe_llm_module.model_selector, "get_moe", lambda: False)
    monkeypatch.setattr(
        moe_llm_module.model_selector,
        "get_web_search",
        lambda: False,
    )
    monkeypatch.setattr(
        moe_llm_module.model_selector,
        "get_use_tools",
        lambda: False,
    )
    monkeypatch.setattr(
        moe_llm_module.model_selector,
        "get_resident_plugins",
        lambda: [],
    )
    monkeypatch.setattr(
        moe_llm_module.temperament_manager,
        "get_temperament_prompt",
        lambda _temperament: "system prompt",
    )
    monkeypatch.setattr(moe_llm_module, "get_session", object)
    payloads: list[dict[str, object]] = []

    async def fake_none_stream(
        chat,
        session,
        url,
        headers,
        data,
        proxy,
        timeout=None,
    ):
        del session, url, headers, proxy, timeout
        payloads.append(data)
        chat._record_token_usage(
            {
                "prompt_tokens": 11,
                "completion_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            }
        )
        return True, "fake answer", None, ""

    monkeypatch.setattr(
        moe_llm_module.MoeLlm,
        "none_stream_llm_chat",
        fake_none_stream,
    )

    class Bot:
        config = SimpleNamespace(superusers=set())

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, _event, message) -> None:
            self.sent.append(str(message))

    bot = Bot()
    event = SimpleNamespace(
        user_id=10_001,
        sender=SimpleNamespace(card="Moe", nickname="Moe"),
    )
    chat = moe_llm_module.MoeLlm(
        bot,
        event,
        {
            "text": ["hello"],
            "images": [],
            "reply": "",
            "reply_user": None,
            "current_user": {"qq": "10001"},
            "mentions": [],
        },
        temperament="test",
        agent_runtime=runtime,
    )

    try:
        assert await chat.get_llm_chat() is True
    finally:
        messages_dict.clear()

    assert bot.sent == ["fake answer"]
    assert payloads[0]["model"] == "fake-model"
    assert payloads[0]["stream"] is False
    assert runtime.run.state is AgentRunState.COMPLETED
    assert runtime.run.input_tokens == 11
    assert runtime.run.output_tokens == 4
    assert [step.status for step in runtime.steps] == [
        AgentStepStatus.COMPLETED,
        AgentStepStatus.COMPLETED,
    ]
    assert len(usage_batches) == 1
    assert usage_batches[0][0].cached_tokens == 2
    assert [message.role for message in durable_messages] == [
        "user",
        "assistant",
    ]

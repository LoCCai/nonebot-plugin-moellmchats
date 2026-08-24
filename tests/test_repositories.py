from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect

import pytest

from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRun,
    AgentRunState,
    AgentStep,
    ToolCall,
    ToolCallStatus,
)
from nonebot_plugin_moellmchats.repositories import (
    AgentRunRepository,
    AgentStepRepository,
    AuditRepository,
    BatchAuditRepository,
    BatchUsageRepository,
    ConversationRepository,
    MessageRepository,
    RepositoryConflictError,
    RepositoryError,
    RepositoryPage,
    RepositoryPageRequest,
    RepositoryTransaction,
    RepositoryUnavailableError,
    ToolCallRepository,
    ToolRepository,
    UsageRepository,
    UserRepository,
)


class _Transaction:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _ConversationRepository:
    def __init__(self) -> None:
        self.records: dict[str, str] = {}

    async def create(self, conversation: str) -> None:
        self.records[conversation] = conversation

    async def get(self, conversation_id: str) -> str | None:
        return self.records.get(conversation_id)

    async def replace(self, conversation: str) -> None:
        self.records[conversation] = conversation


class _UserRepository:
    def __init__(self) -> None:
        self.records: dict[str, str] = {}

    async def resolve(self, user: str) -> str:
        return self.records.setdefault(user, user)

    async def get(self, user_id: str) -> str | None:
        return self.records.get(user_id)


class _MessageRepository:
    def __init__(self) -> None:
        self.records: list[str] = []

    async def append(self, message: str) -> None:
        self.records.append(message)

    async def list_recent(
        self,
        conversation_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[str]:
        del conversation_id
        return RepositoryPage(tuple(self.records[-page.limit :]))


class _AgentRunRepository:
    def __init__(self) -> None:
        self.records: dict[str, AgentRun] = {}

    async def create(self, run: AgentRun) -> None:
        self.records[run.run_id] = run

    async def get(self, run_id: str) -> AgentRun | None:
        return self.records.get(run_id)

    async def replace(
        self,
        run: AgentRun,
        *,
        expected_state: AgentRunState,
        expected_generation: int,
    ) -> None:
        current = self.records.get(run.run_id)
        if current is None or current.state is not expected_state or current.generation != expected_generation:
            raise RepositoryConflictError("stale run")
        self.records[run.run_id] = run


class _AgentStepRepository:
    async def append(self, step: AgentStep) -> None:
        del step

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[AgentStep]:
        del run_id, page
        return RepositoryPage(())


class _ToolCallRepository:
    async def create(self, call: ToolCall) -> None:
        del call

    async def get(self, tool_call_id: str) -> ToolCall | None:
        del tool_call_id
        return None

    async def replace(
        self,
        call: ToolCall,
        *,
        expected_status: ToolCallStatus,
    ) -> None:
        del call, expected_status

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[ToolCall]:
        del run_id, page
        return RepositoryPage(())


class _ToolRepository:
    async def create(self, tool: str) -> None:
        del tool

    async def get(self, tool_id: str) -> str | None:
        del tool_id
        return None

    async def replace(self, tool: str) -> None:
        del tool


class _UsageRepository:
    async def append(self, usage: str) -> None:
        del usage

    async def append_batch(self, usages: tuple[str, ...]) -> None:
        del usages

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[str]:
        del run_id, page
        return RepositoryPage(())


class _LegacyUsageRepository:
    async def append(self, usage: str) -> None:
        del usage

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[str]:
        del run_id, page
        return RepositoryPage(())


class _AuditRepository:
    async def append(self, event: str) -> None:
        del event

    async def append_batch(self, events: tuple[str, ...]) -> None:
        del events

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[str]:
        del run_id, page
        return RepositoryPage(())


class _LegacyAuditRepository:
    async def append(self, event: str) -> None:
        del event

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[str]:
        del run_id, page
        return RepositoryPage(())


def _run(*, state: AgentRunState = AgentRunState.CREATED) -> AgentRun:
    return AgentRun(
        run_id="run_01HZX7Y95Z8QW8D4WTV4VCZZY2",
        request_id=17,
        user_id="qq:10001",
        group_id="qq-group:20002",
        conversation_id="conversation_0001",
        generation=9,
        state=state,
        started_at=100.25,
        finished_at=None,
    )


def test_repository_page_request_defaults_and_serializes_fresh_copies() -> None:
    request = RepositoryPageRequest()
    serialized = request.as_dict()

    assert request.limit == 20
    assert request.cursor is None
    assert serialized == {"limit": 20, "cursor": None}
    serialized["limit"] = 1
    assert request.as_dict() == {"limit": 20, "cursor": None}


def test_repository_page_request_accepts_bounded_opaque_cursors() -> None:
    request = RepositoryPageRequest(
        limit=200,
        cursor="eyJpZCI6MTIzfQ==._~+/=-",
    )

    assert request.as_dict() == {
        "limit": 200,
        "cursor": "eyJpZCI6MTIzfQ==._~+/=-",
    }


@pytest.mark.parametrize("limit", [0, -1, 201, True, 1.5, "20"])
def test_repository_page_request_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        RepositoryPageRequest(limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "cursor with space",
        "cursor\n",
        "🙂",
        "x" * 513,
        123,
    ],
)
def test_repository_page_request_rejects_invalid_cursors(cursor: object) -> None:
    with pytest.raises(ValueError, match="cursor"):
        RepositoryPageRequest(cursor=cursor)  # type: ignore[arg-type]


def test_repository_page_is_an_immutable_bounded_container() -> None:
    page = RepositoryPage(("first", "second"), next_cursor="next_01")

    assert page.items == ("first", "second")
    assert page.next_cursor == "next_01"
    assert page.has_more is True
    with pytest.raises(FrozenInstanceError):
        page.items = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        RepositoryPageRequest().limit = 1  # type: ignore[misc]


def test_repository_page_supports_a_terminal_empty_page() -> None:
    page: RepositoryPage[str] = RepositoryPage(())

    assert page.items == ()
    assert page.next_cursor is None
    assert page.has_more is False


def test_repository_page_rejects_invalid_containers_and_cursors() -> None:
    with pytest.raises(ValueError, match="items"):
        RepositoryPage(["item"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="next_cursor"):
        RepositoryPage(("item",), next_cursor="bad cursor")
    with pytest.raises(ValueError, match="空 RepositoryPage"):
        RepositoryPage((), next_cursor="next")


def test_repository_errors_preserve_conflict_and_availability_categories() -> None:
    conflict = RepositoryConflictError("conflict")
    unavailable = RepositoryUnavailableError("unavailable")

    assert isinstance(conflict, RepositoryError)
    assert isinstance(unavailable, RepositoryError)
    assert isinstance(conflict, RuntimeError)
    assert type(conflict) is not type(unavailable)


def test_repository_protocols_are_runtime_checkable() -> None:
    users: UserRepository[str] = _UserRepository()
    conversation: ConversationRepository[str] = _ConversationRepository()
    messages: MessageRepository[str] = _MessageRepository()
    runs: AgentRunRepository = _AgentRunRepository()
    steps: AgentStepRepository = _AgentStepRepository()
    calls: ToolCallRepository = _ToolCallRepository()
    tools: ToolRepository[str] = _ToolRepository()
    usage: UsageRepository[str] = _UsageRepository()
    batch_usage: BatchUsageRepository[str] = _UsageRepository()
    audit: AuditRepository[str] = _AuditRepository()
    batch_audit: BatchAuditRepository[str] = _AuditRepository()
    transaction: RepositoryTransaction = _Transaction()

    assert isinstance(users, UserRepository)
    assert isinstance(conversation, ConversationRepository)
    assert isinstance(messages, MessageRepository)
    assert isinstance(runs, AgentRunRepository)
    assert isinstance(steps, AgentStepRepository)
    assert isinstance(calls, ToolCallRepository)
    assert isinstance(tools, ToolRepository)
    assert isinstance(usage, UsageRepository)
    assert isinstance(batch_usage, BatchUsageRepository)
    assert isinstance(audit, AuditRepository)
    assert isinstance(batch_audit, BatchAuditRepository)
    assert isinstance(transaction, RepositoryTransaction)
    assert not isinstance(object(), AgentRunRepository)
    assert not isinstance(object(), RepositoryTransaction)


def test_batch_usage_extension_preserves_legacy_usage_implementations() -> None:
    legacy = _LegacyUsageRepository()

    assert isinstance(legacy, UsageRepository)
    assert not isinstance(legacy, BatchUsageRepository)


def test_batch_audit_extension_preserves_legacy_audit_implementations() -> None:
    legacy = _LegacyAuditRepository()

    assert isinstance(legacy, AuditRepository)
    assert not isinstance(legacy, BatchAuditRepository)


@pytest.mark.parametrize(
    ("protocol", "method_names"),
    [
        (RepositoryTransaction, ("commit", "rollback")),
        (UserRepository, ("resolve", "get")),
        (ConversationRepository, ("create", "get", "replace")),
        (MessageRepository, ("append", "list_recent")),
        (AgentRunRepository, ("create", "get", "replace")),
        (AgentStepRepository, ("append", "list_for_run")),
        (ToolCallRepository, ("create", "get", "replace", "list_for_run")),
        (ToolRepository, ("create", "get", "replace")),
        (UsageRepository, ("append", "list_for_run")),
        (BatchUsageRepository, ("append", "append_batch", "list_for_run")),
        (AuditRepository, ("append", "list_for_run")),
        (BatchAuditRepository, ("append", "append_batch", "list_for_run")),
    ],
)
def test_repository_protocol_methods_are_async(
    protocol: type[object],
    method_names: tuple[str, ...],
) -> None:
    for method_name in method_names:
        assert inspect.iscoroutinefunction(getattr(protocol, method_name))


def test_agent_run_and_tool_call_replace_contracts_require_cas_fields() -> None:
    run_parameters = inspect.signature(AgentRunRepository.replace).parameters
    call_parameters = inspect.signature(ToolCallRepository.replace).parameters

    assert tuple(run_parameters) == (
        "self",
        "run",
        "expected_state",
        "expected_generation",
    )
    assert run_parameters["expected_state"].kind is inspect.Parameter.KEYWORD_ONLY
    assert run_parameters["expected_generation"].kind is (inspect.Parameter.KEYWORD_ONLY)
    assert tuple(call_parameters) == (
        "self",
        "call",
        "expected_status",
    )
    assert call_parameters["expected_status"].kind is (inspect.Parameter.KEYWORD_ONLY)


@pytest.mark.asyncio
async def test_structural_repositories_can_implement_the_contracts() -> None:
    conversation: ConversationRepository[str] = _ConversationRepository()
    messages: MessageRepository[str] = _MessageRepository()
    transaction: RepositoryTransaction = _Transaction()

    await conversation.create("conversation_1")
    assert await conversation.get("conversation_1") == "conversation_1"
    await conversation.replace("conversation_1")
    await messages.append("oldest")
    await messages.append("newest")
    page = await messages.list_recent(
        "conversation_1",
        RepositoryPageRequest(limit=1),
    )
    await transaction.commit()
    await transaction.rollback()

    assert page.items == ("newest",)
    assert isinstance(transaction, _Transaction)
    assert transaction.committed is True
    assert transaction.rolled_back is True


@pytest.mark.asyncio
async def test_agent_run_repository_cas_conflict_is_explicit() -> None:
    repository: AgentRunRepository = _AgentRunRepository()
    run = _run()

    await repository.create(run)
    await repository.replace(
        run,
        expected_state=AgentRunState.CREATED,
        expected_generation=9,
    )
    with pytest.raises(RepositoryConflictError, match="stale"):
        await repository.replace(
            run,
            expected_state=AgentRunState.EXECUTING,
            expected_generation=9,
        )

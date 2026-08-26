from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from .agent_runtime import (
        AgentRun,
        AgentRunState,
        AgentStep,
        ToolCall,
        ToolCallStatus,
    )

_CURSOR_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,512}$")

ConversationRecordT = TypeVar("ConversationRecordT")
UserRecordT = TypeVar("UserRecordT")
MessageRecordT = TypeVar("MessageRecordT")
ToolRecordT = TypeVar("ToolRecordT")
UsageRecordT = TypeVar("UsageRecordT")
AuditRecordT = TypeVar("AuditRecordT")
SessionSummaryRecordT = TypeVar("SessionSummaryRecordT")
SessionSummaryMessageRecordT = TypeVar(
    "SessionSummaryMessageRecordT",
    covariant=True,
)
PageRecordT = TypeVar("PageRecordT")


class RepositoryError(RuntimeError):
    """Base error for a repository operation."""


class RepositoryConflictError(RepositoryError):
    """An optimistic write precondition no longer matches durable state."""


class RepositoryUnavailableError(RepositoryError):
    """The durable backend is unavailable without implying a safe retry."""


def _validate_cursor(value: str | None, *, label: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not _CURSOR_RE.fullmatch(value)):
        raise ValueError(f"{label} 必须是安全 opaque cursor")
    return value


@dataclass(frozen=True)
class RepositoryPageRequest:
    """Backend-neutral bounded cursor request."""

    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or not 1 <= self.limit <= 200:
            raise ValueError("RepositoryPageRequest.limit 必须是 1 到 200 的整数")
        _validate_cursor(self.cursor, label="RepositoryPageRequest.cursor")

    def as_dict(self) -> dict[str, int | str | None]:
        return {"limit": self.limit, "cursor": self.cursor}


@dataclass(frozen=True)
class RepositoryPage(Generic[PageRecordT]):
    """Immutable page container; record immutability remains a domain concern."""

    items: tuple[PageRecordT, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("RepositoryPage.items 必须是元组")
        _validate_cursor(self.next_cursor, label="RepositoryPage.next_cursor")
        if not self.items and self.next_cursor is not None:
            raise ValueError("空 RepositoryPage 不得提供 next_cursor")

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@runtime_checkable
class RepositoryTransaction(Protocol):
    """Explicit transaction boundary; implementations own session lifecycle."""

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@runtime_checkable
class UserRepository(Protocol[UserRecordT]):
    async def resolve(self, user: UserRecordT) -> UserRecordT: ...

    async def get(self, user_id: str) -> UserRecordT | None: ...


@runtime_checkable
class ConversationRepository(Protocol[ConversationRecordT]):
    async def create(self, conversation: ConversationRecordT) -> None: ...

    async def get(self, conversation_id: str) -> ConversationRecordT | None: ...

    async def replace(self, conversation: ConversationRecordT) -> None: ...


@runtime_checkable
class MessageRepository(Protocol[MessageRecordT]):
    async def append(self, message: MessageRecordT) -> None: ...

    async def list_recent(
        self,
        conversation_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[MessageRecordT]: ...


@runtime_checkable
class SessionSummaryRepository(
    Protocol[SessionSummaryRecordT, SessionSummaryMessageRecordT],
):
    async def append(
        self,
        summary: SessionSummaryRecordT,
        *,
        expected_previous_summary_id: str | None,
    ) -> None: ...

    async def get(
        self,
        conversation_id: str,
        summary_id: str,
    ) -> SessionSummaryRecordT | None: ...

    async def get_latest(
        self,
        conversation_id: str,
    ) -> SessionSummaryRecordT | None: ...

    async def list_source_messages(
        self,
        conversation_id: str,
        *,
        after_message_id: int | None,
        limit: int,
    ) -> tuple[SessionSummaryMessageRecordT, ...]: ...


@runtime_checkable
class AgentRunRepository(Protocol):
    async def create(self, run: AgentRun) -> None: ...

    async def get(self, run_id: str) -> AgentRun | None: ...

    async def replace(
        self,
        run: AgentRun,
        *,
        expected_state: AgentRunState,
        expected_generation: int,
    ) -> None: ...


@runtime_checkable
class AgentStepRepository(Protocol):
    async def append(self, step: AgentStep) -> None: ...

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[AgentStep]: ...


@runtime_checkable
class ToolCallRepository(Protocol):
    async def create(self, call: ToolCall) -> None: ...

    async def get(self, tool_call_id: str) -> ToolCall | None: ...

    async def replace(
        self,
        call: ToolCall,
        *,
        expected_status: ToolCallStatus,
    ) -> None: ...

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[ToolCall]: ...


@runtime_checkable
class ToolRepository(Protocol[ToolRecordT]):
    async def create(self, tool: ToolRecordT) -> None: ...

    async def get(self, tool_id: str) -> ToolRecordT | None: ...

    async def replace(self, tool: ToolRecordT) -> None: ...


@runtime_checkable
class UsageRepository(Protocol[UsageRecordT]):
    async def append(self, usage: UsageRecordT) -> None: ...

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[UsageRecordT]: ...


@runtime_checkable
class BatchUsageRepository(
    UsageRepository[UsageRecordT],
    Protocol[UsageRecordT],
):
    """Optional batch extension that preserves the base usage contract."""

    async def append_batch(self, usages: tuple[UsageRecordT, ...]) -> None: ...


@runtime_checkable
class AuditRepository(Protocol[AuditRecordT]):
    async def append(self, event: AuditRecordT) -> None: ...

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[AuditRecordT]: ...


@runtime_checkable
class BatchAuditRepository(
    AuditRepository[AuditRecordT],
    Protocol[AuditRecordT],
):
    """Optional non-critical batch extension preserving the base audit contract."""

    async def append_batch(self, events: tuple[AuditRecordT, ...]) -> None: ...

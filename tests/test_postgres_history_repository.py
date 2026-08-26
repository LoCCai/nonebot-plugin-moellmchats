from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import inspect
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nonebot_plugin_moellmchats.chat_history import (
    ConversationRecord,
    MessageRecord,
    UserRecord,
)
import nonebot_plugin_moellmchats.postgres_history_repository as repository_module
from nonebot_plugin_moellmchats.postgres_history_repository import (
    PostgresConversationRepository,
    PostgresMessageRepository,
    PostgresUserRepository,
)
from nonebot_plugin_moellmchats.repositories import (
    ConversationRepository,
    MessageRepository,
    RepositoryConflictError,
    RepositoryPageRequest,
    RepositoryUnavailableError,
    UserRepository,
)

_NOW = datetime(2026, 8, 22, 20, 0, tzinfo=timezone.utc)
_UNSET = object()


class _Result:
    def __init__(
        self,
        *,
        scalar_one: object = _UNSET,
        scalar_optional: object = _UNSET,
        rows: object = _UNSET,
    ) -> None:
        self._scalar_one = scalar_one
        self._scalar_optional = scalar_optional
        self._rows = rows

    @staticmethod
    def _resolve(value: object) -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

    def scalar_one(self) -> Any:
        if self._scalar_one is _UNSET:
            raise AssertionError("scalar_one was not configured")
        return self._resolve(self._scalar_one)

    def scalar_one_or_none(self) -> Any:
        if self._scalar_optional is _UNSET:
            raise AssertionError("scalar_one_or_none was not configured")
        return self._resolve(self._scalar_optional)

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Any:
        if self._rows is _UNSET:
            raise AssertionError("mapping rows were not configured")
        rows = self._resolve(self._rows)
        if not isinstance(rows, (list, tuple)):
            raise TypeError("rows must be a sequence")
        if len(rows) > 1:
            raise RuntimeError("multiple rows")
        return rows[0] if rows else None

    def one(self) -> Any:
        if self._rows is _UNSET:
            raise AssertionError("mapping rows were not configured")
        rows = self._resolve(self._rows)
        if not isinstance(rows, (list, tuple)):
            raise TypeError("rows must be a sequence")
        if len(rows) != 1:
            raise RuntimeError("expected exactly one row")
        return rows[0]

    def all(self) -> Any:
        if self._rows is _UNSET:
            raise AssertionError("mapping rows were not configured")
        return self._resolve(self._rows)


def _session(*results: object) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = results
    return session


def _conversation(
    conversation_id: str = "conversation-1",
    *,
    group_id: str | None = None,
    user_id: str | None = "user-1",
) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=conversation_id,
        conversation_type="private" if group_id is None else "group",
        platform="onebot-v11",
        group_id=group_id,
        user_id=user_id,
        created_at=_NOW,
        updated_at=_NOW + timedelta(seconds=1),
        last_message_at=_NOW + timedelta(seconds=1),
    )


def _user(
    user_id: str = "user-1",
    *,
    platform: str = "onebot-v11",
    platform_user_id: str = "10001",
    display_name: str | None = "Moe",
) -> UserRecord:
    return UserRecord(
        user_id=user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        display_name=display_name,
        created_at=_NOW,
        updated_at=_NOW + timedelta(seconds=1),
    )


def _message(
    message_id: int | None = None,
    *,
    conversation_id: str = "conversation-1",
    content: str | None = "hello",
    structured_content: Any = None,
    created_at: datetime = _NOW,
) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        platform_message_id=f"platform-{message_id}" if message_id is not None else "platform-new",
        role="user",
        sender_id="user-1",
        content=content,
        structured_content=structured_content,
        created_at=created_at,
    )


def _conversation_row(record: ConversationRecord) -> dict[str, Any]:
    return {
        "id": record.conversation_id,
        "type": record.conversation_type,
        "platform": record.platform,
        "group_id": record.group_id,
        "user_id": record.user_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_message_at": record.last_message_at,
    }


def _user_row(record: UserRecord) -> dict[str, Any]:
    values = record.as_dict()
    return {
        "id": values["user_id"],
        "platform": values["platform"],
        "platform_user_id": values["platform_user_id"],
        "display_name": values["display_name"],
        "created_at": values["created_at"],
        "updated_at": values["updated_at"],
    }


def _message_row(record: MessageRecord) -> dict[str, Any]:
    values = record.as_dict()
    return {
        "id": values["message_id"],
        "conversation_id": values["conversation_id"],
        "platform_message_id": values["platform_message_id"],
        "role": values["role"],
        "sender_id": values["sender_id"],
        "content": values["content"],
        "structured_content": values["structured_content"],
        "created_at": values["created_at"],
    }


def _compile(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()), dict(compiled.params)


def test_history_records_are_detached_deeply_immutable_and_utc_normalized() -> None:
    source = {"parts": [{"text": "hello"}], "scores": [1, 2.5]}
    local_time = _NOW.astimezone(timezone(timedelta(hours=8)))
    conversation = ConversationRecord(
        conversation_id="conversation-1",
        conversation_type="private",
        platform="onebot-v11",
        group_id=None,
        user_id="user-1",
        created_at=local_time,
        updated_at=local_time,
    )
    message = _message(
        content=None,
        structured_content=source,
        created_at=local_time,
    )

    source["parts"][0]["text"] = "mutated"
    source["scores"].append(3)

    assert conversation.created_at == _NOW
    assert conversation.created_at.tzinfo is timezone.utc
    structured: Any = message.structured_content
    assert isinstance(structured, MappingProxyType)
    assert structured["parts"][0]["text"] == "hello"
    assert structured["scores"] == (1, 2.5)
    assert message.persisted is False

    detached = message.as_dict()
    detached["structured_content"]["parts"][0]["text"] = "changed"
    assert structured["parts"][0]["text"] == "hello"
    with pytest.raises(FrozenInstanceError):
        message.content = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        structured["new"] = True  # type: ignore[index]


def test_user_record_is_immutable_and_utc_normalized() -> None:
    local_time = _NOW.astimezone(timezone(timedelta(hours=8)))
    user = UserRecord(
        user_id="user-1",
        platform="onebot-v11",
        platform_user_id="10001",
        display_name="Moe",
        created_at=local_time,
        updated_at=local_time,
    )

    assert user.created_at == _NOW
    assert user.updated_at.tzinfo is timezone.utc
    assert user.as_dict() == {
        "user_id": "user-1",
        "platform": "onebot-v11",
        "platform_user_id": "10001",
        "display_name": "Moe",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    with pytest.raises(FrozenInstanceError):
        user.display_name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": ""},
        {"platform": " onebot-v11"},
        {"platform_user_id": "10001\n"},
        {"display_name": ""},
        {"created_at": datetime(2026, 8, 22, 20, 0)},
        {
            "created_at": _NOW + timedelta(seconds=1),
            "updated_at": _NOW,
        },
    ],
)
def test_user_record_rejects_invalid_durable_values(
    changes: dict[str, Any],
) -> None:
    values = {
        "user_id": "user-1",
        "platform": "onebot-v11",
        "platform_user_id": "10001",
        "display_name": "Moe",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=r"UserRecord|user_id"):
        UserRecord(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"conversation_id": ""},
        {"conversation_type": " private"},
        {"platform": "onebot\n"},
        {"group_id": None, "user_id": None},
        {"created_at": datetime(2026, 8, 22, 20, 0)},
        {
            "created_at": _NOW + timedelta(seconds=1),
            "updated_at": _NOW,
        },
    ],
)
def test_conversation_record_rejects_invalid_durable_values(
    changes: dict[str, Any],
) -> None:
    values = {
        "conversation_id": "conversation-1",
        "conversation_type": "private",
        "platform": "onebot-v11",
        "group_id": None,
        "user_id": "user-1",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=r"ConversationRecord|conversation_id"):
        ConversationRecord(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"message_id": True},
        {"message_id": 0},
        {"role": ""},
        {"created_at": datetime(2026, 8, 22, 20, 0)},
        {"content": None, "structured_content": None},
        {"content": "bad\x00content"},
        {"content": None, "structured_content": {"number": float("nan")}},
        {"content": None, "structured_content": {1: "bad-key"}},
    ],
)
def test_message_record_rejects_invalid_durable_values(
    changes: dict[str, Any],
) -> None:
    values: dict[str, Any] = {
        "message_id": None,
        "conversation_id": "conversation-1",
        "role": "user",
        "created_at": _NOW,
        "platform_message_id": None,
        "sender_id": "user-1",
        "content": "hello",
        "structured_content": None,
    }
    values.update(changes)

    with pytest.raises(ValueError, match="MessageRecord"):
        MessageRecord(**values)


def test_message_record_rejects_cyclic_structured_content() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="循环"):
        _message(content=None, structured_content=cyclic)


def test_postgres_repositories_require_an_explicit_async_session() -> None:
    with pytest.raises(TypeError, match="AsyncSession"):
        PostgresUserRepository(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AsyncSession"):
        PostgresConversationRepository(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AsyncSession"):
        PostgresMessageRepository(object())  # type: ignore[arg-type]

    session = _session()
    users = PostgresUserRepository(session)
    conversations = PostgresConversationRepository(session)
    messages = PostgresMessageRepository(session)

    assert isinstance(users, UserRepository)
    assert isinstance(conversations, ConversationRepository)
    assert isinstance(messages, MessageRepository)
    assert inspect.iscoroutinefunction(users.resolve)
    assert inspect.iscoroutinefunction(conversations.create)
    assert inspect.iscoroutinefunction(messages.list_recent)


@pytest.mark.asyncio
async def test_user_repository_resolves_and_gets_with_one_statement_each() -> None:
    proposed = _user()
    canonical = _user(user_id="canonical-user")
    session = _session(
        _Result(rows=[_user_row(canonical)]),
        _Result(rows=[_user_row(canonical)]),
    )
    repository = PostgresUserRepository(session)

    assert await repository.resolve(proposed) == canonical
    assert await repository.get(canonical.user_id) == canonical

    upsert_sql, upsert_params = _compile(session.execute.await_args_list[0].args[0])
    select_sql, select_params = _compile(session.execute.await_args_list[1].args[0])

    assert upsert_sql.startswith("INSERT INTO users")
    assert "ON CONFLICT (platform, platform_user_id) DO UPDATE" in upsert_sql
    assert "coalesce(excluded.display_name, users.display_name)" in upsert_sql
    assert "greatest(users.updated_at, excluded.updated_at)" in upsert_sql
    assert "RETURNING users.id, users.platform" in upsert_sql
    assert proposed.user_id in upsert_params.values()
    assert select_sql.startswith("SELECT users.id, users.platform")
    assert "SELECT *" not in select_sql
    assert "WHERE users.id =" in select_sql
    assert canonical.user_id in select_params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation", "conflict_target", "conflict_predicate"),
    [
        (
            _conversation(group_id="group-1", user_id=None),
            "platform, type, group_id",
            "group_id IS NOT NULL",
        ),
        (
            _conversation(),
            "platform, type, user_id",
            "group_id IS NULL AND user_id IS NOT NULL",
        ),
    ],
)
async def test_conversation_repository_resolves_scope_with_atomic_upsert(
    conversation: ConversationRecord,
    conflict_target: str,
    conflict_predicate: str,
) -> None:
    canonical = ConversationRecord(
        **{
            **conversation.as_dict(),
            "conversation_id": "canonical-conversation",
        }
    )
    session = _session(_Result(rows=[_conversation_row(canonical)]))
    repository = PostgresConversationRepository(session)

    assert await repository.resolve(conversation) == canonical

    statement_sql, params = _compile(session.execute.await_args.args[0])
    assert statement_sql.startswith("INSERT INTO conversations")
    assert f"ON CONFLICT ({conflict_target})" in statement_sql
    assert conflict_predicate in statement_sql
    assert "DO UPDATE SET updated_at = greatest(" in statement_sql
    assert "RETURNING conversations.id, conversations.type" in statement_sql
    assert conversation.conversation_id in params.values()
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_rejects_drifted_or_malformed_returned_identity() -> None:
    proposed_user = _user()
    drifted_user = _user(platform_user_id="other")
    proposed_conversation = _conversation()
    drifted_conversation = _conversation(user_id="other-user")
    session = _session(
        _Result(rows=[_user_row(drifted_user)]),
        _Result(rows=[_conversation_row(drifted_conversation)]),
    )

    with pytest.raises(RepositoryUnavailableError, match=r"user\.resolve"):
        await PostgresUserRepository(session).resolve(proposed_user)
    with pytest.raises(RepositoryUnavailableError, match=r"conversation\.resolve"):
        await PostgresConversationRepository(session).resolve(
            proposed_conversation,
        )

    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_user_resolve_sanitizes_conflict_and_never_retries() -> None:
    backend_error = IntegrityError(
        "INSERT INTO users VALUES ('private-user')",
        {"password": "top-secret"},
        RuntimeError("postgresql://user:top-secret@db.internal/private"),
    )
    session = _session(backend_error)

    with pytest.raises(RepositoryConflictError, match="IntegrityError") as error:
        await PostgresUserRepository(session).resolve(_user())

    rendered = str(error.value)
    for secret in ("private-user", "password", "top-secret", "db.internal"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_resolve_preserves_cancellation_without_retry() -> None:
    session = _session(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await PostgresUserRepository(session).resolve(_user())

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_conversation_repository_uses_explicit_returning_sql_without_transaction_ownership() -> None:
    conversation = _conversation()
    session = _session(
        _Result(scalar_one=conversation.conversation_id),
        _Result(rows=[_conversation_row(conversation)]),
        _Result(scalar_optional=conversation.conversation_id),
    )
    repository = PostgresConversationRepository(session)

    assert await repository.create(conversation) is None
    assert await repository.get(conversation.conversation_id) == conversation
    assert await repository.replace(conversation) is None

    insert_sql, insert_params = _compile(session.execute.await_args_list[0].args[0])
    select_sql, select_params = _compile(session.execute.await_args_list[1].args[0])
    update_sql, update_params = _compile(session.execute.await_args_list[2].args[0])

    assert insert_sql.startswith("INSERT INTO conversations")
    assert "RETURNING conversations.id" in insert_sql
    assert conversation.conversation_id in insert_params.values()
    assert select_sql.startswith("SELECT conversations.id, conversations.type, conversations.platform,")
    assert "SELECT *" not in select_sql
    assert "WHERE conversations.id =" in select_sql
    assert conversation.conversation_id in select_params.values()
    assert update_sql.startswith("UPDATE conversations SET")
    assert "RETURNING conversations.id" in update_sql
    assert conversation.conversation_id in update_params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_get_none_and_replace_missing_are_distinct() -> None:
    conversation = _conversation()
    session = _session(
        _Result(rows=[]),
        _Result(scalar_optional=None),
    )
    repository = PostgresConversationRepository(session)

    assert await repository.get(conversation.conversation_id) is None
    with pytest.raises(RepositoryConflictError, match=r"conversation\.replace"):
        await repository.replace(conversation)

    assert session.execute.await_count == 2
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_repository_appends_and_pages_with_stable_keyset_order() -> None:
    draft = _message(
        content=None,
        structured_content={"parts": [{"text": "new"}]},
    )
    newest = _message(10, created_at=_NOW + timedelta(seconds=10))
    middle = _message(9, created_at=_NOW + timedelta(seconds=9))
    extra = _message(8, created_at=_NOW + timedelta(seconds=8))
    older = _message(7, created_at=_NOW + timedelta(seconds=7))
    session = _session(
        _Result(scalar_one=11),
        _Result(rows=[_message_row(newest), _message_row(middle), _message_row(extra)]),
        _Result(rows=[_message_row(extra), _message_row(older)]),
    )
    repository = PostgresMessageRepository(session)

    assert await repository.append(draft) is None
    first = await repository.list_recent(
        "conversation-1",
        RepositoryPageRequest(limit=2),
    )
    second = await repository.list_recent(
        "conversation-1",
        RepositoryPageRequest(limit=2, cursor=first.next_cursor),
    )

    assert tuple(record.message_id for record in first.items) == (9, 10)
    assert first.next_cursor is not None
    assert first.has_more is True
    assert "conversation-1" not in first.next_cursor
    assert tuple(record.message_id for record in second.items) == (7, 8)
    assert second.next_cursor is None
    assert second.has_more is False

    insert_sql, insert_params = _compile(session.execute.await_args_list[0].args[0])
    first_sql, first_params = _compile(session.execute.await_args_list[1].args[0])
    second_sql, second_params = _compile(session.execute.await_args_list[2].args[0])

    assert insert_sql.startswith("INSERT INTO messages")
    assert "messages.id" not in insert_sql.partition("VALUES")[0]
    assert "RETURNING messages.id" in insert_sql
    structured_values = [value for value in insert_params.values() if isinstance(value, dict)]
    assert structured_values == [{"parts": [{"text": "new"}]}]
    assert "SELECT *" not in first_sql
    assert "ORDER BY messages.id DESC" in first_sql
    assert " LIMIT " in first_sql
    assert " OFFSET " not in first_sql
    assert "messages.id <" not in first_sql
    assert "conversation-1" in first_params.values()
    assert 3 in first_params.values()
    assert "messages.id <" in second_sql
    assert 9 in second_params.values()
    assert 3 in second_params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_cursor_is_bound_to_conversation_and_rejected_before_sql() -> None:
    newest = _message(10)
    older = _message(9)
    extra = _message(8)
    session = _session(
        _Result(rows=[_message_row(newest), _message_row(older), _message_row(extra)]),
    )
    repository = PostgresMessageRepository(session)
    page = await repository.list_recent(
        "conversation-1",
        RepositoryPageRequest(limit=2),
    )
    assert page.next_cursor is not None

    with pytest.raises(ValueError, match="当前会话"):
        await repository.list_recent(
            "conversation-2",
            RepositoryPageRequest(limit=2, cursor=page.next_cursor),
        )
    with pytest.raises(ValueError, match="有效消息游标"):
        await repository.list_recent(
            "conversation-1",
            RepositoryPageRequest(limit=2, cursor="message-v1.Zm9v"),
        )

    assert session.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_message_row(_message(9)), _message_row(_message(10))],
        [_message_row(_message(10)), _message_row(_message(10))],
        [_message_row(_message(10, conversation_id="conversation-2"))],
        [_message_row(_message(10))] * 4,
        [object()],
    ],
)
async def test_message_repository_rejects_corrupt_or_contract_breaking_results(
    rows: list[Any],
) -> None:
    session = _session(_Result(rows=rows))
    repository = PostgresMessageRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"message\.list_recent") as error:
        await repository.list_recent(
            "conversation-1",
            RepositoryPageRequest(limit=2),
        )

    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_write_integrity_conflicts_are_sanitized_and_never_retried() -> None:
    backend_error = IntegrityError(
        "INSERT INTO messages VALUES ('private-content')",
        {"password": "top-secret"},
        RuntimeError("postgresql://user:top-secret@db.internal/private"),
    )
    session = _session(backend_error)
    repository = PostgresMessageRepository(session)

    with pytest.raises(RepositoryConflictError, match="IntegrityError") as error:
        await repository.append(_message())

    rendered = str(error.value)
    for secret in ("private-content", "password", "top-secret", "db.internal"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_write_and_read_failures_are_sanitized_and_never_retried() -> None:
    backend_error = RuntimeError("timeout after INSERT for secret-message at db.internal with top-secret")
    session = _session(backend_error)
    repository = PostgresConversationRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="RuntimeError") as error:
        await repository.create(_conversation())

    rendered = str(error.value)
    for secret in ("secret-message", "db.internal", "top-secret"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_id", [None, 0, True, -1, 1 << 63, "11"])
async def test_message_append_requires_a_confirmed_database_identity(
    returned_id: object,
) -> None:
    session = _session(_Result(scalar_one=returned_id))
    repository = PostgresMessageRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"message\.append"):
        await repository.append(_message())

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_repository_preserves_cancellation_without_wrapping_or_retrying() -> None:
    session = _session(asyncio.CancelledError())
    repository = PostgresConversationRepository(session)

    with pytest.raises(asyncio.CancelledError):
        await repository.get("conversation-1")

    assert session.execute.await_count == 1


def test_history_repository_module_has_no_global_session_or_engine() -> None:
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import Session

    forbidden = (Engine, AsyncEngine, Session, AsyncSession)
    assert not any(isinstance(value, forbidden) for value in vars(repository_module).values())

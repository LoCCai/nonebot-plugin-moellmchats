from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nonebot_plugin_moellmchats.chat_history import MessageRecord
import nonebot_plugin_moellmchats.postgres_session_summary_repository as repository_module
from nonebot_plugin_moellmchats.postgres_session_summary_repository import (
    PostgresSessionSummaryRepository,
)
from nonebot_plugin_moellmchats.repositories import (
    RepositoryConflictError,
    RepositoryUnavailableError,
    SessionSummaryRepository,
)
from nonebot_plugin_moellmchats.session_summary import (
    SessionSummaryPolicy,
    SessionSummaryRecord,
)

_NOW = datetime(2026, 8, 22, 21, 30, tzinfo=timezone.utc)
_UNSET = object()


class _Result:
    def __init__(
        self,
        *,
        scalar_optional: object = _UNSET,
        rows: object = _UNSET,
    ) -> None:
        self._scalar_optional = scalar_optional
        self._rows = rows

    @staticmethod
    def _resolve(value: object) -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

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

    def all(self) -> Any:
        if self._rows is _UNSET:
            raise AssertionError("mapping rows were not configured")
        return self._resolve(self._rows)


def _session(*results: object) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = results
    return session


def _message(
    message_id: int,
    *,
    conversation_id: str = "conversation-1",
) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        platform_message_id=f"platform-{message_id}",
        role="user" if message_id % 2 else "assistant",
        sender_id="user-1",
        content=f"message-{message_id}",
        structured_content=None,
        created_at=_NOW + timedelta(seconds=message_id),
    )


def _messages(start: int, count: int) -> tuple[MessageRecord, ...]:
    return tuple(_message(message_id) for message_id in range(start, start + count))


def _record() -> SessionSummaryRecord:
    plan = SessionSummaryPolicy().plan(
        "conversation-1",
        previous_summary=None,
        messages=_messages(1, 50),
    )
    assert plan is not None
    return plan.complete(
        summary_id="summary-1",
        model_provider="deepseek",
        model="deepseek-chat",
        content="Summary through message forty.",
        created_at=_NOW + timedelta(minutes=2),
    )


def _next_record(previous: SessionSummaryRecord) -> SessionSummaryRecord:
    plan = SessionSummaryPolicy().plan(
        "conversation-1",
        previous_summary=previous,
        messages=_messages(41, 50),
    )
    assert plan is not None
    return plan.complete(
        summary_id="summary-2",
        model_provider="deepseek",
        model="deepseek-chat",
        content="Summary through message eighty.",
        created_at=_NOW + timedelta(minutes=4),
    )


def _summary_row(record: SessionSummaryRecord) -> dict[str, Any]:
    values = record.as_dict()
    return {
        "id": values["summary_id"],
        "conversation_id": values["conversation_id"],
        "generation": values["generation"],
        "previous_summary_id": values["previous_summary_id"],
        "covered_from_message_id": values["covered_from_message_id"],
        "covered_through_message_id": values["covered_through_message_id"],
        "covered_message_count": values["covered_message_count"],
        "source_message_count": values["source_message_count"],
        "source_digest": values["source_digest"],
        "policy_version": values["policy_version"],
        "trigger_message_count": values["trigger_message_count"],
        "keep_recent_message_count": values["keep_recent_message_count"],
        "max_source_chars": values["max_source_chars"],
        "source_char_count": values["source_char_count"],
        "model_provider": values["model_provider"],
        "model": values["model"],
        "content": values["content"],
        "created_at": values["created_at"],
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


def test_repository_requires_explicit_async_session_and_satisfies_protocol() -> None:
    with pytest.raises(TypeError, match="AsyncSession"):
        PostgresSessionSummaryRepository(object())  # type: ignore[arg-type]

    repository = PostgresSessionSummaryRepository(_session())
    assert isinstance(repository, SessionSummaryRepository)
    assert inspect.iscoroutinefunction(repository.append)
    assert inspect.iscoroutinefunction(repository.list_source_messages)


@pytest.mark.asyncio
async def test_initial_append_is_one_conditional_insert_without_transaction_ownership() -> None:
    record = _record()
    session = _session(_Result(scalar_optional=record.summary_id))
    repository = PostgresSessionSummaryRepository(session)

    assert (
        await repository.append(
            record,
            expected_previous_summary_id=None,
        )
        is None
    )

    sql, params = _compile(session.execute.await_args.args[0])
    assert sql.startswith("INSERT INTO session_summaries")
    assert "SELECT" in sql
    assert "NOT (EXISTS" in sql
    assert "current_summary.conversation_id" in sql
    assert "RETURNING session_summaries.id" in sql
    assert record.summary_id in params.values()
    assert record.source_digest in params.values()
    assert record.content in params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_successor_append_uses_previous_head_and_cumulative_watermark_cas() -> None:
    previous = _record()
    record = _next_record(previous)
    session = _session(_Result(scalar_optional=record.summary_id))
    repository = PostgresSessionSummaryRepository(session)

    await repository.append(
        record,
        expected_previous_summary_id=previous.summary_id,
    )

    sql, params = _compile(session.execute.await_args.args[0])
    assert "previous_summary.id" in sql
    assert "previous_summary.generation" in sql
    assert "previous_summary.covered_through_message_id <" in sql
    assert "previous_summary.covered_message_count +" in sql
    assert "later_summary.generation >=" in sql
    assert previous.summary_id in params.values()
    assert record.generation in params.values()
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_append_rejects_mismatched_expected_head_before_sql() -> None:
    record = _record()
    session = _session()
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(ValueError, match="summary chain"):
        await repository.append(
            record,
            expected_previous_summary_id="wrong-summary",
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_head_returns_conflict_and_is_never_retried() -> None:
    record = _record()
    session = _session(_Result(scalar_optional=None))
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(RepositoryConflictError, match=r"summary\.append"):
        await repository.append(record, expected_previous_summary_id=None)

    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_and_get_latest_select_explicit_columns_with_stable_head_order() -> None:
    record = _record()
    session = _session(
        _Result(rows=[_summary_row(record)]),
        _Result(rows=[_summary_row(record)]),
        _Result(rows=[]),
    )
    repository = PostgresSessionSummaryRepository(session)

    assert await repository.get("conversation-1", "summary-1") == record
    assert await repository.get_latest("conversation-1") == record
    assert await repository.get("conversation-1", "missing") is None

    get_sql, get_params = _compile(session.execute.await_args_list[0].args[0])
    latest_sql, latest_params = _compile(session.execute.await_args_list[1].args[0])
    assert get_sql.startswith("SELECT session_summaries.id, session_summaries.conversation_id")
    assert "SELECT *" not in get_sql
    assert "session_summaries.id =" in get_sql
    assert "summary-1" in get_params.values()
    assert "ORDER BY session_summaries.generation DESC" in latest_sql
    assert " LIMIT " in latest_sql
    assert 1 in latest_params.values()


@pytest.mark.asyncio
async def test_source_query_reads_oldest_messages_after_watermark_with_a_hard_limit() -> None:
    records = (_message(41), _message(42), _message(43))
    session = _session(_Result(rows=[_message_row(record) for record in records]))
    repository = PostgresSessionSummaryRepository(session)

    loaded = await repository.list_source_messages(
        "conversation-1",
        after_message_id=40,
        limit=50,
    )

    assert loaded == records
    sql, params = _compile(session.execute.await_args.args[0])
    assert sql.startswith("SELECT messages.id, messages.conversation_id")
    assert "SELECT *" not in sql
    assert "messages.id >" in sql
    assert "ORDER BY messages.id ASC" in sql
    assert " OFFSET " not in sql
    assert 40 in params.values()
    assert 50 in params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_message_row(_message(42)), _message_row(_message(41))],
        [_message_row(_message(41)), _message_row(_message(41))],
        [_message_row(_message(41, conversation_id="conversation-2"))],
        [object()],
    ],
)
async def test_source_query_rejects_corrupt_cross_conversation_or_non_monotonic_results(
    rows: list[Any],
) -> None:
    session = _session(_Result(rows=rows))
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="list_source_messages") as error:
        await repository.list_source_messages(
            "conversation-1",
            after_message_id=40,
            limit=50,
        )

    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_summary_rows_with_wrong_identity_or_invalid_content_are_unavailable() -> None:
    record = _record()
    wrong_conversation = _summary_row(record)
    wrong_conversation["conversation_id"] = "conversation-2"
    invalid_content = _summary_row(record)
    invalid_content["content"] = ""
    session = _session(
        _Result(rows=[wrong_conversation]),
        _Result(rows=[invalid_content]),
    )
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"summary\.get"):
        await repository.get("conversation-1", "summary-1")
    with pytest.raises(RepositoryUnavailableError, match="get_latest"):
        await repository.get_latest("conversation-1")


@pytest.mark.asyncio
async def test_integrity_conflict_is_sanitized_and_never_retried() -> None:
    backend_error = IntegrityError(
        "INSERT INTO session_summaries VALUES ('private-summary')",
        {"password": "top-secret"},
        RuntimeError("postgresql://user:top-secret@db.internal/private"),
    )
    session = _session(backend_error)
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(RepositoryConflictError, match="IntegrityError") as error:
        await repository.append(_record(), expected_previous_summary_id=None)

    rendered = str(error.value)
    for secret in ("private-summary", "password", "top-secret", "db.internal"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_unknown_write_result_is_unavailable_sanitized_and_not_replayed() -> None:
    backend_error = RuntimeError("timeout after private-summary at db.internal with top-secret")
    session = _session(backend_error)
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="RuntimeError") as error:
        await repository.append(_record(), expected_previous_summary_id=None)

    rendered = str(error.value)
    for secret in ("private-summary", "db.internal", "top-secret"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_repository_preserves_cancellation_without_wrapping_or_retrying() -> None:
    session = _session(asyncio.CancelledError())
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(asyncio.CancelledError):
        await repository.get_latest("conversation-1")

    assert session.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_message_id", "limit"),
    [
        (0, 50),
        (True, 50),
        (None, 0),
        (None, 201),
        (None, True),
    ],
)
async def test_source_query_rejects_invalid_bounds_before_sql(
    after_message_id: object,
    limit: object,
) -> None:
    session = _session()
    repository = PostgresSessionSummaryRepository(session)

    with pytest.raises(ValueError, match=r"after_message_id|limit"):
        await repository.list_source_messages(
            "conversation-1",
            after_message_id=after_message_id,  # type: ignore[arg-type]
            limit=limit,  # type: ignore[arg-type]
        )

    session.execute.assert_not_awaited()


def test_repository_module_has_no_global_session_engine_or_summary_instance() -> None:
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import Session

    forbidden = (Engine, AsyncEngine, Session, AsyncSession, SessionSummaryRecord)
    assert not any(isinstance(value, forbidden) for value in vars(repository_module).values())

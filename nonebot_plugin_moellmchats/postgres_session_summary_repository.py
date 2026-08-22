from __future__ import annotations

import asyncio
from collections.abc import Mapping
from itertools import pairwise
import re
from typing import Any, TypeGuard

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .chat_history import MessageRecord, validate_conversation_id
from .database_schema import (
    ENTITY_ID_MAX_CHARS,
    messages_table,
    session_summaries_table,
)
from .repositories import (
    RepositoryConflictError,
    RepositoryUnavailableError,
    SessionSummaryRepository,
)
from .session_summary import SessionSummaryRecord

_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_MAX_SOURCE_QUERY_MESSAGES = 200

_SUMMARY_COLUMNS = (
    session_summaries_table.c.id,
    session_summaries_table.c.conversation_id,
    session_summaries_table.c.generation,
    session_summaries_table.c.previous_summary_id,
    session_summaries_table.c.covered_from_message_id,
    session_summaries_table.c.covered_through_message_id,
    session_summaries_table.c.covered_message_count,
    session_summaries_table.c.source_message_count,
    session_summaries_table.c.source_digest,
    session_summaries_table.c.policy_version,
    session_summaries_table.c.trigger_message_count,
    session_summaries_table.c.keep_recent_message_count,
    session_summaries_table.c.max_source_chars,
    session_summaries_table.c.source_char_count,
    session_summaries_table.c.model_provider,
    session_summaries_table.c.model,
    session_summaries_table.c.content,
    session_summaries_table.c.created_at,
)
_MESSAGE_COLUMNS = (
    messages_table.c.id,
    messages_table.c.conversation_id,
    messages_table.c.platform_message_id,
    messages_table.c.role,
    messages_table.c.sender_id,
    messages_table.c.content,
    messages_table.c.structured_content,
    messages_table.c.created_at,
)


def _safe_error_type(error: BaseException) -> str:
    error_type = type(error).__name__
    return error_type if _ERROR_TYPE_RE.fullmatch(error_type) else "BackendError"


def _unavailable(
    operation: str,
    *,
    error: BaseException | None = None,
) -> RepositoryUnavailableError:
    suffix = "" if error is None else f" ({_safe_error_type(error)})"
    return RepositoryUnavailableError(f"PostgreSQL session summary {operation} 结果不可确认{suffix}")


def _conflict(
    operation: str,
    error: BaseException | None = None,
) -> RepositoryConflictError:
    suffix = "" if error is None else f" ({_safe_error_type(error)})"
    return RepositoryConflictError(f"PostgreSQL session summary {operation} 发生持久化冲突{suffix}")


def _valid_message_id(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _POSTGRES_BIGINT_MAX


def _validate_summary_id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > ENTITY_ID_MAX_CHARS
        or _CONTROL_CHARACTER_RE.search(value)
    ):
        raise ValueError(f"{label} 必须是安全有界标识")
    return value


def _summary_values(record: SessionSummaryRecord) -> dict[str, Any]:
    return {
        "id": record.summary_id,
        "conversation_id": record.conversation_id,
        "generation": record.generation,
        "previous_summary_id": record.previous_summary_id,
        "covered_from_message_id": record.covered_from_message_id,
        "covered_through_message_id": record.covered_through_message_id,
        "covered_message_count": record.covered_message_count,
        "source_message_count": record.source_message_count,
        "source_digest": record.source_digest,
        "policy_version": record.policy_version,
        "trigger_message_count": record.trigger_message_count,
        "keep_recent_message_count": record.keep_recent_message_count,
        "max_source_chars": record.max_source_chars,
        "source_char_count": record.source_char_count,
        "model_provider": record.model_provider,
        "model": record.model,
        "content": record.content,
        "created_at": record.created_at,
    }


def _summary_from_row(row: Mapping[str, Any]) -> SessionSummaryRecord:
    return SessionSummaryRecord(
        summary_id=row["id"],
        conversation_id=row["conversation_id"],
        generation=row["generation"],
        previous_summary_id=row["previous_summary_id"],
        covered_from_message_id=row["covered_from_message_id"],
        covered_through_message_id=row["covered_through_message_id"],
        covered_message_count=row["covered_message_count"],
        source_message_count=row["source_message_count"],
        source_digest=row["source_digest"],
        policy_version=row["policy_version"],
        trigger_message_count=row["trigger_message_count"],
        keep_recent_message_count=row["keep_recent_message_count"],
        max_source_chars=row["max_source_chars"],
        source_char_count=row["source_char_count"],
        model_provider=row["model_provider"],
        model=row["model"],
        content=row["content"],
        created_at=row["created_at"],
    )


def _message_from_row(row: Mapping[str, Any]) -> MessageRecord:
    record = MessageRecord(
        message_id=row["id"],
        conversation_id=row["conversation_id"],
        platform_message_id=row["platform_message_id"],
        role=row["role"],
        sender_id=row["sender_id"],
        content=row["content"],
        structured_content=row["structured_content"],
        created_at=row["created_at"],
    )
    if not record.persisted:
        raise ValueError("durable message row 缺少 identity")
    return record


class PostgresSessionSummaryRepository(
    SessionSummaryRepository[SessionSummaryRecord, MessageRecord],
):
    """Summary-chain access using one caller-owned SQLAlchemy transaction."""

    def __init__(self, session: AsyncSession) -> None:
        if not isinstance(session, AsyncSession):
            raise TypeError("session 必须是调用方显式持有的 SQLAlchemy AsyncSession")
        self._session = session

    async def _execute(
        self,
        statement: Any,
        *,
        operation: str,
        integrity_is_conflict: bool = False,
    ) -> Any:
        try:
            return await self._session.execute(statement)
        except asyncio.CancelledError:
            raise
        except IntegrityError as error:
            if integrity_is_conflict:
                raise _conflict(operation, error) from None
            raise _unavailable(operation, error=error) from None
        except Exception as error:
            raise _unavailable(operation, error=error) from None

    async def append(
        self,
        summary: SessionSummaryRecord,
        *,
        expected_previous_summary_id: str | None,
    ) -> None:
        if not isinstance(summary, SessionSummaryRecord):
            raise TypeError("summary 必须是 SessionSummaryRecord")
        if expected_previous_summary_id is not None:
            expected_previous_summary_id = _validate_summary_id(
                expected_previous_summary_id,
                label="expected_previous_summary_id",
            )
        if summary.previous_summary_id != expected_previous_summary_id:
            raise ValueError("expected_previous_summary_id 与 summary chain 不匹配")

        values = _summary_values(summary)
        selected_values = tuple(
            sa.literal(values[column.name], type_=column.type).label(column.name) for column in _SUMMARY_COLUMNS
        )
        source = sa.select(*selected_values)
        current = session_summaries_table.alias("current_summary")
        if expected_previous_summary_id is None:
            precondition = ~sa.exists(
                sa.select(sa.literal(1)).select_from(current).where(current.c.conversation_id == summary.conversation_id)
            )
        else:
            previous = session_summaries_table.alias("previous_summary")
            later = session_summaries_table.alias("later_summary")
            precondition = sa.exists(
                sa.select(sa.literal(1))
                .select_from(previous)
                .where(
                    previous.c.conversation_id == summary.conversation_id,
                    previous.c.id == expected_previous_summary_id,
                    previous.c.generation == summary.generation - 1,
                    previous.c.covered_through_message_id < summary.covered_from_message_id,
                    previous.c.covered_message_count + summary.source_message_count == summary.covered_message_count,
                    previous.c.created_at <= summary.created_at,
                    ~sa.exists(
                        sa.select(sa.literal(1))
                        .select_from(later)
                        .where(
                            later.c.conversation_id == summary.conversation_id,
                            later.c.generation >= summary.generation,
                        )
                    ),
                )
            )
        source = source.where(precondition)
        statement = (
            sa.insert(session_summaries_table)
            .from_select(
                tuple(column.name for column in _SUMMARY_COLUMNS),
                source,
            )
            .returning(session_summaries_table.c.id)
        )
        result = await self._execute(
            statement,
            operation="summary.append",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one_or_none()
        except Exception as error:
            raise _unavailable("summary.append", error=error) from None
        if returned_id is None:
            raise _conflict("summary.append")
        if type(returned_id) is not str or returned_id != summary.summary_id:
            raise _unavailable("summary.append")

    async def get(
        self,
        conversation_id: str,
        summary_id: str,
    ) -> SessionSummaryRecord | None:
        conversation_id = validate_conversation_id(conversation_id)
        summary_id = _validate_summary_id(summary_id, label="summary_id")
        statement = sa.select(*_SUMMARY_COLUMNS).where(
            session_summaries_table.c.conversation_id == conversation_id,
            session_summaries_table.c.id == summary_id,
        )
        result = await self._execute(statement, operation="summary.get")
        try:
            row = result.mappings().one_or_none()
            if row is None:
                return None
            if not isinstance(row, Mapping):
                raise TypeError("row is not a mapping")
            record = _summary_from_row(row)
        except Exception as error:
            raise _unavailable("summary.get", error=error) from None
        if record.conversation_id != conversation_id or record.summary_id != summary_id:
            raise _unavailable("summary.get")
        return record

    async def get_latest(
        self,
        conversation_id: str,
    ) -> SessionSummaryRecord | None:
        conversation_id = validate_conversation_id(conversation_id)
        statement = (
            sa.select(*_SUMMARY_COLUMNS)
            .where(session_summaries_table.c.conversation_id == conversation_id)
            .order_by(session_summaries_table.c.generation.desc())
            .limit(1)
        )
        result = await self._execute(statement, operation="summary.get_latest")
        try:
            row = result.mappings().one_or_none()
            if row is None:
                return None
            if not isinstance(row, Mapping):
                raise TypeError("row is not a mapping")
            record = _summary_from_row(row)
        except Exception as error:
            raise _unavailable("summary.get_latest", error=error) from None
        if record.conversation_id != conversation_id:
            raise _unavailable("summary.get_latest")
        return record

    async def list_source_messages(
        self,
        conversation_id: str,
        *,
        after_message_id: int | None,
        limit: int,
    ) -> tuple[MessageRecord, ...]:
        conversation_id = validate_conversation_id(conversation_id)
        if after_message_id is not None and not _valid_message_id(after_message_id):
            raise ValueError("after_message_id 必须是正 PostgreSQL BIGINT 或 None")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_SOURCE_QUERY_MESSAGES:
            raise ValueError("limit 必须是 1 到 200 的整数")

        statement = sa.select(*_MESSAGE_COLUMNS).where(messages_table.c.conversation_id == conversation_id)
        if after_message_id is not None:
            statement = statement.where(messages_table.c.id > after_message_id)
        statement = statement.order_by(messages_table.c.id.asc()).limit(limit)
        result = await self._execute(statement, operation="summary.list_source_messages")
        try:
            rows = tuple(result.mappings().all())
            if len(rows) > limit or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("unexpected row collection")
            records = tuple(_message_from_row(row) for row in rows)
        except Exception as error:
            raise _unavailable("summary.list_source_messages", error=error) from None

        if any(record.conversation_id != conversation_id for record in records):
            raise _unavailable("summary.list_source_messages")
        message_ids: list[int] = []
        for record in records:
            if not _valid_message_id(record.message_id):
                raise _unavailable("summary.list_source_messages")
            message_ids.append(record.message_id)
        if any(older >= newer for older, newer in pairwise(message_ids)):
            raise _unavailable("summary.list_source_messages")
        if after_message_id is not None and message_ids and message_ids[0] <= after_message_id:
            raise _unavailable("summary.list_source_messages")
        return records

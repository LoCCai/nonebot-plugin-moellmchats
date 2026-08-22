from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
import hashlib
import hmac
from itertools import pairwise
import json
import re
from typing import Any, TypeGuard

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .chat_history import (
    ConversationRecord,
    MessageRecord,
    mutable_history_json,
    validate_conversation_id,
)
from .database_schema import conversations_table, messages_table
from .repositories import (
    ConversationRepository,
    MessageRepository,
    RepositoryConflictError,
    RepositoryPage,
    RepositoryPageRequest,
    RepositoryUnavailableError,
)

_CURSOR_PREFIX = "message-v1."
_CURSOR_PAYLOAD_MAX_BYTES = 256
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

_CONVERSATION_COLUMNS = (
    conversations_table.c.id,
    conversations_table.c.type,
    conversations_table.c.platform,
    conversations_table.c.group_id,
    conversations_table.c.user_id,
    conversations_table.c.created_at,
    conversations_table.c.updated_at,
    conversations_table.c.last_message_at,
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
    return RepositoryUnavailableError(f"PostgreSQL chat history {operation} 结果不可确认{suffix}")


def _conflict(operation: str, error: BaseException | None = None) -> RepositoryConflictError:
    suffix = "" if error is None else f" ({_safe_error_type(error)})"
    return RepositoryConflictError(f"PostgreSQL chat history {operation} 发生持久化冲突{suffix}")


def _conversation_fingerprint(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()


def _valid_message_id(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _POSTGRES_BIGINT_MAX


def _encode_message_cursor(conversation_id: str, before_message_id: int) -> str:
    if not _valid_message_id(before_message_id):
        raise ValueError("before_message_id 必须是正 PostgreSQL BIGINT")
    payload = json.dumps(
        [1, _conversation_fingerprint(conversation_id), before_message_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    cursor = _CURSOR_PREFIX + encoded
    if len(payload) > _CURSOR_PAYLOAD_MAX_BYTES or len(cursor) > 512:
        raise RuntimeError("message cursor 超过内部安全上限")
    return cursor


def _decode_message_cursor(cursor: str, conversation_id: str) -> int:
    message = "RepositoryPageRequest.cursor 不是当前会话的有效消息游标"
    if not cursor.startswith(_CURSOR_PREFIX):
        raise ValueError(message)
    encoded = cursor.removeprefix(_CURSOR_PREFIX)
    if not encoded or "=" in encoded:
        raise ValueError(message)
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        if len(payload) > _CURSOR_PAYLOAD_MAX_BYTES or base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=") != encoded:
            raise ValueError
        decoded = json.loads(payload.decode("ascii"))
    except Exception:
        raise ValueError(message) from None
    if (
        not isinstance(decoded, list)
        or len(decoded) != 3
        or type(decoded[0]) is not int
        or decoded[0] != 1
        or not isinstance(decoded[1], str)
        or not _SHA256_RE.fullmatch(decoded[1])
        or not _valid_message_id(decoded[2])
        or not hmac.compare_digest(decoded[1], _conversation_fingerprint(conversation_id))
    ):
        raise ValueError(message)
    return decoded[2]


def _conversation_values(record: ConversationRecord) -> dict[str, Any]:
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


def _message_values(record: MessageRecord) -> dict[str, Any]:
    return {
        "conversation_id": record.conversation_id,
        "platform_message_id": record.platform_message_id,
        "role": record.role,
        "sender_id": record.sender_id,
        "content": record.content,
        "structured_content": mutable_history_json(record.structured_content),
        "created_at": record.created_at,
    }


def _conversation_from_row(row: Mapping[str, Any]) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=row["id"],
        conversation_type=row["type"],
        platform=row["platform"],
        group_id=row["group_id"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_message_at=row["last_message_at"],
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


class _PostgresRepository:
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


class PostgresConversationRepository(
    _PostgresRepository,
    ConversationRepository[ConversationRecord],
):
    """PostgreSQL conversation access using a caller-owned transaction."""

    async def create(self, conversation: ConversationRecord) -> None:
        if not isinstance(conversation, ConversationRecord):
            raise TypeError("conversation 必须是 ConversationRecord")
        statement = (
            sa.insert(conversations_table).values(**_conversation_values(conversation)).returning(conversations_table.c.id)
        )
        result = await self._execute(
            statement,
            operation="conversation.create",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one()
        except Exception as error:
            raise _unavailable("conversation.create", error=error) from None
        if type(returned_id) is not str or returned_id != conversation.conversation_id:
            raise _unavailable("conversation.create")

    async def get(self, conversation_id: str) -> ConversationRecord | None:
        conversation_id = validate_conversation_id(conversation_id)
        statement = sa.select(*_CONVERSATION_COLUMNS).where(conversations_table.c.id == conversation_id)
        result = await self._execute(statement, operation="conversation.get")
        try:
            row = result.mappings().one_or_none()
            if row is None:
                return None
            if not isinstance(row, Mapping):
                raise TypeError("row is not a mapping")
            record = _conversation_from_row(row)
        except Exception as error:
            raise _unavailable("conversation.get", error=error) from None
        if record.conversation_id != conversation_id:
            raise _unavailable("conversation.get")
        return record

    async def replace(self, conversation: ConversationRecord) -> None:
        if not isinstance(conversation, ConversationRecord):
            raise TypeError("conversation 必须是 ConversationRecord")
        values = _conversation_values(conversation)
        del values["id"]
        statement = (
            sa.update(conversations_table)
            .where(conversations_table.c.id == conversation.conversation_id)
            .values(**values)
            .returning(conversations_table.c.id)
        )
        result = await self._execute(
            statement,
            operation="conversation.replace",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one_or_none()
        except Exception as error:
            raise _unavailable("conversation.replace", error=error) from None
        if returned_id is None:
            raise _conflict("conversation.replace")
        if type(returned_id) is not str or returned_id != conversation.conversation_id:
            raise _unavailable("conversation.replace")


class PostgresMessageRepository(
    _PostgresRepository,
    MessageRepository[MessageRecord],
):
    """PostgreSQL message append and bounded recent-history queries."""

    async def append(self, message: MessageRecord) -> None:
        if not isinstance(message, MessageRecord):
            raise TypeError("message 必须是 MessageRecord")
        if message.persisted:
            raise ValueError("MessageRecord.append 只接受 message_id=None 的草稿")
        statement = sa.insert(messages_table).values(**_message_values(message)).returning(messages_table.c.id)
        result = await self._execute(
            statement,
            operation="message.append",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one()
        except Exception as error:
            raise _unavailable("message.append", error=error) from None
        if not _valid_message_id(returned_id):
            raise _unavailable("message.append")

    async def list_recent(
        self,
        conversation_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[MessageRecord]:
        conversation_id = validate_conversation_id(conversation_id)
        if not isinstance(page, RepositoryPageRequest):
            raise TypeError("page 必须是 RepositoryPageRequest")
        before_message_id = None if page.cursor is None else _decode_message_cursor(page.cursor, conversation_id)

        statement = sa.select(*_MESSAGE_COLUMNS).where(messages_table.c.conversation_id == conversation_id)
        if before_message_id is not None:
            statement = statement.where(messages_table.c.id < before_message_id)
        statement = statement.order_by(messages_table.c.id.desc()).limit(page.limit + 1)

        result = await self._execute(statement, operation="message.list_recent")
        try:
            rows = tuple(result.mappings().all())
            if len(rows) > page.limit + 1 or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("unexpected row collection")
            records = tuple(_message_from_row(row) for row in rows)
        except Exception as error:
            raise _unavailable("message.list_recent", error=error) from None

        if any(record.conversation_id != conversation_id for record in records):
            raise _unavailable("message.list_recent")
        message_ids: list[int] = []
        for record in records:
            if not _valid_message_id(record.message_id):
                raise _unavailable("message.list_recent")
            message_ids.append(record.message_id)
        if any(newer <= older for newer, older in pairwise(message_ids)):
            raise _unavailable("message.list_recent")

        visible_descending = records[: page.limit]
        next_cursor = None
        if len(records) > page.limit:
            oldest_visible_id = visible_descending[-1].message_id
            if not _valid_message_id(oldest_visible_id):
                raise _unavailable("message.list_recent")
            next_cursor = _encode_message_cursor(
                conversation_id,
                oldest_visible_id,
            )
        return RepositoryPage(tuple(reversed(visible_descending)), next_cursor=next_cursor)

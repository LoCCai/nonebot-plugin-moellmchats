from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
from itertools import pairwise
import json
import re
from typing import Any, TypeGuard

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .audit_event import (
    MAX_AUDIT_BATCH_SIZE,
    AuditEventRecord,
    mutable_audit_json,
    validate_audit_run_id,
)
from .database_schema import audit_events_table
from .repositories import (
    BatchAuditRepository,
    RepositoryConflictError,
    RepositoryPage,
    RepositoryPageRequest,
    RepositoryUnavailableError,
)

_CURSOR_PREFIX = "audit-v1."
_CURSOR_PAYLOAD_MAX_BYTES = 384
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

_AUDIT_COLUMNS = (
    audit_events_table.c.id,
    audit_events_table.c.event_type,
    audit_events_table.c.actor_user_id,
    audit_events_table.c.actor_type,
    audit_events_table.c.target_type,
    audit_events_table.c.target_id,
    audit_events_table.c.run_id,
    audit_events_table.c.tool_call_id,
    audit_events_table.c.metadata_json,
    audit_events_table.c.created_at,
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
    return RepositoryUnavailableError(f"PostgreSQL audit {operation} 结果不可确认{suffix}")


def _conflict(
    operation: str,
    error: BaseException | None = None,
) -> RepositoryConflictError:
    suffix = "" if error is None else f" ({_safe_error_type(error)})"
    return RepositoryConflictError(f"PostgreSQL audit {operation} 发生持久化冲突{suffix}")


def _valid_event_id(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _POSTGRES_BIGINT_MAX


def _run_fingerprint(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_cursor_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    normalized = parsed.astimezone(timezone.utc)
    if _datetime_text(normalized) != value:
        raise ValueError
    return normalized


def _encode_audit_cursor(run_id: str, created_at: datetime, event_id: int) -> str:
    if not _valid_event_id(event_id):
        raise ValueError("event_id 必须是正 PostgreSQL BIGINT")
    payload = json.dumps(
        [1, _run_fingerprint(run_id), _datetime_text(created_at), event_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    cursor = _CURSOR_PREFIX + encoded
    if len(payload) > _CURSOR_PAYLOAD_MAX_BYTES or len(cursor) > 512:
        raise RuntimeError("audit cursor 超过内部安全上限")
    return cursor


def _decode_audit_cursor(
    cursor: str,
    run_id: str,
) -> tuple[datetime, int]:
    message = "RepositoryPageRequest.cursor 不是当前 run 的有效 audit 游标"
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
        if json.dumps(decoded, ensure_ascii=True, separators=(",", ":")).encode("ascii") != payload:
            raise ValueError
        if (
            not isinstance(decoded, list)
            or len(decoded) != 4
            or type(decoded[0]) is not int
            or decoded[0] != 1
            or not isinstance(decoded[1], str)
            or not _SHA256_RE.fullmatch(decoded[1])
            or not hmac.compare_digest(decoded[1], _run_fingerprint(run_id))
            or not _valid_event_id(decoded[3])
        ):
            raise ValueError
        created_at = _parse_cursor_datetime(decoded[2])
    except Exception:
        raise ValueError(message) from None
    return created_at, decoded[3]


def _audit_values(record: AuditEventRecord) -> dict[str, object]:
    return {
        "event_type": record.event_type,
        "actor_user_id": record.actor_user_id,
        "actor_type": record.actor_type,
        "target_type": record.target_type,
        "target_id": record.target_id,
        "run_id": record.run_id,
        "tool_call_id": record.tool_call_id,
        "metadata_json": mutable_audit_json(record.metadata_json),
        "created_at": record.created_at,
    }


def _audit_from_row(row: Mapping[str, Any]) -> AuditEventRecord:
    record = AuditEventRecord(
        event_id=row["id"],
        event_type=row["event_type"],
        actor_user_id=row["actor_user_id"],
        actor_type=row["actor_type"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        run_id=row["run_id"],
        tool_call_id=row["tool_call_id"],
        metadata_json=row["metadata_json"],
        created_at=row["created_at"],
    )
    if not record.persisted:
        raise ValueError("durable audit row 缺少 identity")
    return record


class PostgresAuditRepository(BatchAuditRepository[AuditEventRecord]):
    """Audit access using one explicit caller-owned SQLAlchemy transaction."""

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

    async def _append_records(
        self,
        events: tuple[AuditEventRecord, ...],
        *,
        operation: str,
    ) -> None:
        statement = (
            sa.insert(audit_events_table).values([_audit_values(event) for event in events]).returning(audit_events_table.c.id)
        )
        result = await self._execute(
            statement,
            operation=operation,
            integrity_is_conflict=True,
        )
        try:
            returned_ids = tuple(result.scalars().all())
        except Exception as error:
            raise _unavailable(operation, error=error) from None
        if (
            len(returned_ids) != len(events)
            or any(not _valid_event_id(value) for value in returned_ids)
            or len(set(returned_ids)) != len(returned_ids)
        ):
            raise _unavailable(operation)

    async def append(self, event: AuditEventRecord) -> None:
        if not isinstance(event, AuditEventRecord) or event.persisted:
            raise ValueError("append 只接受 draft AuditEventRecord")
        await self._append_records((event,), operation="audit.append")

    async def append_batch(self, events: tuple[AuditEventRecord, ...]) -> None:
        if not isinstance(events, tuple) or not events or len(events) > MAX_AUDIT_BATCH_SIZE:
            raise ValueError(f"events 必须是 1 到 {MAX_AUDIT_BATCH_SIZE} 条记录的元组")
        if any(not isinstance(event, AuditEventRecord) or event.persisted for event in events):
            raise ValueError("append_batch 只接受 draft AuditEventRecord")
        if any(not event.batchable for event in events):
            raise ValueError("安全或未知 audit event 必须走即时 append，不得批量写入")
        await self._append_records(events, operation="audit.append_batch")

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[AuditEventRecord]:
        run_id = validate_audit_run_id(run_id)
        if not isinstance(page, RepositoryPageRequest):
            raise TypeError("page 必须是 RepositoryPageRequest")
        boundary: tuple[datetime, int] | None = None
        if page.cursor is not None:
            boundary = _decode_audit_cursor(page.cursor, run_id)

        statement = sa.select(*_AUDIT_COLUMNS).where(audit_events_table.c.run_id == run_id)
        if boundary is not None:
            boundary_created_at, boundary_id = boundary
            statement = statement.where(
                sa.or_(
                    audit_events_table.c.created_at < boundary_created_at,
                    sa.and_(
                        audit_events_table.c.created_at == boundary_created_at,
                        audit_events_table.c.id < boundary_id,
                    ),
                )
            )
        statement = statement.order_by(
            audit_events_table.c.created_at.desc(),
            audit_events_table.c.id.desc(),
        ).limit(page.limit + 1)

        result = await self._execute(statement, operation="audit.list_for_run")
        try:
            rows = tuple(result.mappings().all())
            if len(rows) > page.limit + 1 or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("unexpected row collection")
            records = tuple(_audit_from_row(row) for row in rows)
        except Exception as error:
            raise _unavailable("audit.list_for_run", error=error) from None

        if any(record.run_id != run_id for record in records):
            raise _unavailable("audit.list_for_run")
        keys: list[tuple[datetime, int]] = []
        for record in records:
            if not _valid_event_id(record.event_id):
                raise _unavailable("audit.list_for_run")
            keys.append((record.created_at, record.event_id))
        if any(older <= newer for older, newer in pairwise(keys)):
            raise _unavailable("audit.list_for_run")
        if boundary is not None and keys and keys[0] >= boundary:
            raise _unavailable("audit.list_for_run")

        visible = records[: page.limit]
        next_cursor = None
        if len(records) > page.limit:
            tail = visible[-1]
            if not _valid_event_id(tail.event_id):
                raise _unavailable("audit.list_for_run")
            next_cursor = _encode_audit_cursor(
                run_id,
                tail.created_at,
                tail.event_id,
            )
        return RepositoryPage(visible, next_cursor=next_cursor)

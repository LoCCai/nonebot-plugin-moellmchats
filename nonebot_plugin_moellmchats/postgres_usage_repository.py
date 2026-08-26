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

from .database_schema import model_usage_table
from .model_usage import (
    MAX_MODEL_USAGE_BATCH_SIZE,
    ModelUsageRecord,
    validate_usage_run_id,
)
from .repositories import (
    BatchUsageRepository,
    RepositoryConflictError,
    RepositoryPage,
    RepositoryPageRequest,
    RepositoryUnavailableError,
)

_CURSOR_PREFIX = "usage-v1."
_CURSOR_PAYLOAD_MAX_BYTES = 384
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

_USAGE_COLUMNS = (
    model_usage_table.c.id,
    model_usage_table.c.run_id,
    model_usage_table.c.provider,
    model_usage_table.c.model,
    model_usage_table.c.input_tokens,
    model_usage_table.c.output_tokens,
    model_usage_table.c.reasoning_tokens,
    model_usage_table.c.cached_tokens,
    model_usage_table.c.cost,
    model_usage_table.c.created_at,
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
    return RepositoryUnavailableError(f"PostgreSQL model usage {operation} 结果不可确认{suffix}")


def _conflict(
    operation: str,
    error: BaseException | None = None,
) -> RepositoryConflictError:
    suffix = "" if error is None else f" ({_safe_error_type(error)})"
    return RepositoryConflictError(f"PostgreSQL model usage {operation} 发生持久化冲突{suffix}")


def _valid_usage_id(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _POSTGRES_BIGINT_MAX


def _run_fingerprint(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _datetime_text(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace(
            "+00:00",
            "Z",
        )
    )


def _parse_cursor_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    normalized = parsed.astimezone(timezone.utc)
    if _datetime_text(normalized) != value:
        raise ValueError
    return normalized


def _encode_usage_cursor(run_id: str, created_at: datetime, usage_id: int) -> str:
    if not _valid_usage_id(usage_id):
        raise ValueError("usage_id 必须是正 PostgreSQL BIGINT")
    payload = json.dumps(
        [1, _run_fingerprint(run_id), _datetime_text(created_at), usage_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    cursor = _CURSOR_PREFIX + encoded
    if len(payload) > _CURSOR_PAYLOAD_MAX_BYTES or len(cursor) > 512:
        raise RuntimeError("usage cursor 超过内部安全上限")
    return cursor


def _decode_usage_cursor(
    cursor: str,
    run_id: str,
) -> tuple[datetime, int]:
    message = "RepositoryPageRequest.cursor 不是当前 run 的有效 usage 游标"
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
        if (
            json.dumps(
                decoded,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            != payload
        ):
            raise ValueError
        if (
            not isinstance(decoded, list)
            or len(decoded) != 4
            or type(decoded[0]) is not int
            or decoded[0] != 1
            or not isinstance(decoded[1], str)
            or not _SHA256_RE.fullmatch(decoded[1])
            or not hmac.compare_digest(decoded[1], _run_fingerprint(run_id))
            or not _valid_usage_id(decoded[3])
        ):
            raise ValueError
        created_at = _parse_cursor_datetime(decoded[2])
    except Exception:
        raise ValueError(message) from None
    return created_at, decoded[3]


def _usage_values(record: ModelUsageRecord) -> dict[str, object]:
    return {
        "run_id": record.run_id,
        "provider": record.provider,
        "model": record.model,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "reasoning_tokens": record.reasoning_tokens,
        "cached_tokens": record.cached_tokens,
        "cost": record.cost,
        "created_at": record.created_at,
    }


def _usage_from_row(row: Mapping[str, Any]) -> ModelUsageRecord:
    record = ModelUsageRecord(
        usage_id=row["id"],
        run_id=row["run_id"],
        provider=row["provider"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        cached_tokens=row["cached_tokens"],
        cost=row["cost"],
        created_at=row["created_at"],
    )
    if not record.persisted:
        raise ValueError("durable usage row 缺少 identity")
    return record


class PostgresUsageRepository(BatchUsageRepository[ModelUsageRecord]):
    """Batch model-usage access using one caller-owned transaction."""

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

    async def append(self, usage: ModelUsageRecord) -> None:
        await self.append_batch((usage,))

    async def append_batch(self, usages: tuple[ModelUsageRecord, ...]) -> None:
        if not isinstance(usages, tuple) or not usages or len(usages) > MAX_MODEL_USAGE_BATCH_SIZE:
            raise ValueError(f"usages 必须是 1 到 {MAX_MODEL_USAGE_BATCH_SIZE} 条记录的元组")
        if any(not isinstance(usage, ModelUsageRecord) or usage.persisted for usage in usages):
            raise ValueError("append_batch 只接受 draft ModelUsageRecord")

        statement = (
            sa.insert(model_usage_table).values([_usage_values(usage) for usage in usages]).returning(model_usage_table.c.id)
        )
        result = await self._execute(
            statement,
            operation="usage.append_batch",
            integrity_is_conflict=True,
        )
        try:
            returned_ids = tuple(result.scalars().all())
        except Exception as error:
            raise _unavailable("usage.append_batch", error=error) from None
        if (
            len(returned_ids) != len(usages)
            or any(not _valid_usage_id(value) for value in returned_ids)
            or len(set(returned_ids)) != len(returned_ids)
        ):
            raise _unavailable("usage.append_batch")

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[ModelUsageRecord]:
        run_id = validate_usage_run_id(run_id)
        if not isinstance(page, RepositoryPageRequest):
            raise TypeError("page 必须是 RepositoryPageRequest")
        boundary: tuple[datetime, int] | None = None
        if page.cursor is not None:
            boundary = _decode_usage_cursor(page.cursor, run_id)

        statement = sa.select(*_USAGE_COLUMNS).where(model_usage_table.c.run_id == run_id)
        if boundary is not None:
            boundary_created_at, boundary_id = boundary
            statement = statement.where(
                sa.or_(
                    model_usage_table.c.created_at < boundary_created_at,
                    sa.and_(
                        model_usage_table.c.created_at == boundary_created_at,
                        model_usage_table.c.id < boundary_id,
                    ),
                )
            )
        statement = statement.order_by(
            model_usage_table.c.created_at.desc(),
            model_usage_table.c.id.desc(),
        ).limit(page.limit + 1)

        result = await self._execute(statement, operation="usage.list_for_run")
        try:
            rows = tuple(result.mappings().all())
            if len(rows) > page.limit + 1 or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("unexpected row collection")
            records = tuple(_usage_from_row(row) for row in rows)
        except Exception as error:
            raise _unavailable("usage.list_for_run", error=error) from None

        if any(record.run_id != run_id for record in records):
            raise _unavailable("usage.list_for_run")
        keys: list[tuple[datetime, int]] = []
        for record in records:
            if not _valid_usage_id(record.usage_id):
                raise _unavailable("usage.list_for_run")
            keys.append((record.created_at, record.usage_id))
        if any(older <= newer for older, newer in pairwise(keys)):
            raise _unavailable("usage.list_for_run")
        if boundary is not None and keys and keys[0] >= boundary:
            raise _unavailable("usage.list_for_run")

        visible = records[: page.limit]
        next_cursor = None
        if len(records) > page.limit:
            tail = visible[-1]
            if not _valid_usage_id(tail.usage_id):
                raise _unavailable("usage.list_for_run")
            next_cursor = _encode_usage_cursor(
                run_id,
                tail.created_at,
                tail.usage_id,
            )
        return RepositoryPage(visible, next_cursor=next_cursor)

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from datetime import datetime, timezone
import hashlib
import hmac
from itertools import pairwise
import json
import math
import re
from typing import Any, TypeGuard

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .agent_runtime import (
    AgentRun,
    AgentRunState,
    AgentStep,
    AgentStepStatus,
    AgentStepType,
    ToolCall,
    ToolCallStatus,
    mutable_agent_json,
    validate_agent_run_id,
    validate_agent_step_id,
    validate_tool_call_id,
)
from .database_schema import agent_runs_table, agent_steps_table, tool_calls_table
from .repositories import (
    AgentRunRepository,
    AgentStepRepository,
    RepositoryConflictError,
    RepositoryPage,
    RepositoryPageRequest,
    RepositoryUnavailableError,
    ToolCallRepository,
)
from .tool_providers import ToolSource

_STEP_CURSOR_PREFIX = "agent-step-v1."
_TOOL_CALL_CURSOR_PREFIX = "tool-call-v1."
_CURSOR_PAYLOAD_MAX_BYTES = 384
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

_AGENT_RUN_COLUMNS = (
    agent_runs_table.c.id,
    agent_runs_table.c.request_id,
    agent_runs_table.c.user_id,
    agent_runs_table.c.group_id,
    agent_runs_table.c.conversation_id,
    agent_runs_table.c.generation,
    agent_runs_table.c.model,
    agent_runs_table.c.status,
    agent_runs_table.c.started_at,
    agent_runs_table.c.finished_at,
    agent_runs_table.c.input_tokens,
    agent_runs_table.c.output_tokens,
    agent_runs_table.c.cost,
    agent_runs_table.c.error_type,
    agent_runs_table.c.error_message,
)
_AGENT_STEP_COLUMNS = (
    agent_steps_table.c.id,
    agent_steps_table.c.run_id,
    agent_steps_table.c.step_index,
    agent_steps_table.c.step_type,
    agent_steps_table.c.model,
    agent_steps_table.c.tool_name,
    agent_steps_table.c.status,
    agent_steps_table.c.started_at,
    agent_steps_table.c.finished_at,
    agent_steps_table.c.duration_ms,
    agent_steps_table.c.input_preview,
    agent_steps_table.c.output_preview,
    agent_steps_table.c.error,
)
_TOOL_CALL_COLUMNS = (
    tool_calls_table.c.id,
    tool_calls_table.c.run_id,
    tool_calls_table.c.step_id,
    tool_calls_table.c.tool_name,
    tool_calls_table.c.tool_source,
    tool_calls_table.c.bundle_id,
    tool_calls_table.c.bundle_digest,
    tool_calls_table.c.arguments_json,
    tool_calls_table.c.result_preview,
    tool_calls_table.c.confirmed,
    tool_calls_table.c.confirmation_id,
    tool_calls_table.c.status,
    tool_calls_table.c.duration_ms,
    tool_calls_table.c.created_at,
    tool_calls_table.c.finished_at,
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
    return RepositoryUnavailableError(f"PostgreSQL agent runtime {operation} 结果不可确认{suffix}")


def _conflict(
    operation: str,
    error: BaseException | None = None,
) -> RepositoryConflictError:
    suffix = "" if error is None else f" ({_safe_error_type(error)})"
    return RepositoryConflictError(f"PostgreSQL agent runtime {operation} 发生持久化冲突{suffix}")


def _valid_bigint(value: object, *, positive: bool = False) -> TypeGuard[int]:
    minimum = 1 if positive else 0
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= _POSTGRES_BIGINT_MAX


def _run_fingerprint(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _timestamp_to_datetime(value: float) -> datetime:
    try:
        converted = datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("Agent timestamp 超出 PostgreSQL 映射范围") from None
    if not math.isfinite(converted.timestamp()) or converted.timestamp() < 0:
        raise ValueError("Agent timestamp 超出 PostgreSQL 映射范围")
    return converted


def _timestamp_from_row(value: object) -> float:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("PostgreSQL Agent timestamp 必须带时区")
    try:
        if value.utcoffset() is None:
            raise ValueError
        normalized = value.astimezone(timezone.utc).timestamp()
    except Exception:
        raise ValueError("PostgreSQL Agent timestamp 非法") from None
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("PostgreSQL Agent timestamp 非法")
    return normalized


def _encode_cursor(prefix: str, payload_value: list[object]) -> str:
    payload = json.dumps(
        payload_value,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    cursor = prefix + encoded
    if len(payload) > _CURSOR_PAYLOAD_MAX_BYTES or len(cursor) > 512:
        raise RuntimeError("Agent repository cursor 超过内部安全上限")
    return cursor


def _decode_cursor(cursor: str, *, prefix: str, message: str) -> list[object]:
    if not cursor.startswith(prefix):
        raise ValueError(message)
    encoded = cursor.removeprefix(prefix)
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
            not isinstance(decoded, list)
            or json.dumps(
                decoded,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            != payload
        ):
            raise ValueError
    except Exception:
        raise ValueError(message) from None
    return decoded


def _encode_step_cursor(run_id: str, index: int, step_id: str) -> str:
    if not _valid_bigint(index):
        raise ValueError("step index 必须是非负 PostgreSQL BIGINT")
    validate_agent_step_id(step_id)
    return _encode_cursor(
        _STEP_CURSOR_PREFIX,
        [1, _run_fingerprint(run_id), index, step_id],
    )


def _decode_step_cursor(cursor: str, run_id: str) -> tuple[int, str]:
    message = "RepositoryPageRequest.cursor 不是当前 run 的有效 AgentStep 游标"
    decoded = _decode_cursor(
        cursor,
        prefix=_STEP_CURSOR_PREFIX,
        message=message,
    )
    try:
        if (
            len(decoded) != 4
            or type(decoded[0]) is not int
            or decoded[0] != 1
            or not isinstance(decoded[1], str)
            or not _SHA256_RE.fullmatch(decoded[1])
            or not hmac.compare_digest(decoded[1], _run_fingerprint(run_id))
            or not _valid_bigint(decoded[2])
            or not isinstance(decoded[3], str)
        ):
            raise ValueError
        step_id = validate_agent_step_id(decoded[3])
    except ValueError:
        raise ValueError(message) from None
    return decoded[2], step_id


def _parse_cursor_timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError
    parsed = float.fromhex(value)
    if not math.isfinite(parsed) or parsed < 0 or parsed.hex() != value:
        raise ValueError
    _timestamp_to_datetime(parsed)
    return parsed


def _encode_tool_call_cursor(
    run_id: str,
    created_at: float,
    tool_call_id: str,
) -> str:
    validate_tool_call_id(tool_call_id)
    _timestamp_to_datetime(created_at)
    return _encode_cursor(
        _TOOL_CALL_CURSOR_PREFIX,
        [1, _run_fingerprint(run_id), created_at.hex(), tool_call_id],
    )


def _decode_tool_call_cursor(cursor: str, run_id: str) -> tuple[float, str]:
    message = "RepositoryPageRequest.cursor 不是当前 run 的有效 ToolCall 游标"
    decoded = _decode_cursor(
        cursor,
        prefix=_TOOL_CALL_CURSOR_PREFIX,
        message=message,
    )
    try:
        if (
            len(decoded) != 4
            or type(decoded[0]) is not int
            or decoded[0] != 1
            or not isinstance(decoded[1], str)
            or not _SHA256_RE.fullmatch(decoded[1])
            or not hmac.compare_digest(decoded[1], _run_fingerprint(run_id))
            or not isinstance(decoded[3], str)
        ):
            raise ValueError
        created_at = _parse_cursor_timestamp(decoded[2])
        tool_call_id = validate_tool_call_id(decoded[3])
    except ValueError:
        raise ValueError(message) from None
    return created_at, tool_call_id


def _run_values(run: AgentRun) -> dict[str, object]:
    return {
        "id": run.run_id,
        "request_id": run.request_id,
        "user_id": run.user_id,
        "group_id": run.group_id,
        "conversation_id": run.conversation_id,
        "generation": run.generation,
        "model": run.model,
        "status": run.state.value,
        "started_at": _timestamp_to_datetime(run.started_at),
        "finished_at": (None if run.finished_at is None else _timestamp_to_datetime(run.finished_at)),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost": run.cost,
        "error_type": run.error_type,
        "error_message": run.error_message,
    }


def _step_values(step: AgentStep) -> dict[str, object]:
    return {
        "id": step.step_id,
        "run_id": step.run_id,
        "step_index": step.index,
        "step_type": step.type.value,
        "model": step.model,
        "tool_name": step.tool,
        "status": step.status.value,
        "started_at": (None if step.started_at is None else _timestamp_to_datetime(step.started_at)),
        "finished_at": (None if step.finished_at is None else _timestamp_to_datetime(step.finished_at)),
        "duration_ms": step.duration_ms,
        "input_preview": step.input_preview,
        "output_preview": step.output_preview,
        "error": step.error,
    }


def _tool_call_values(call: ToolCall) -> dict[str, object]:
    return {
        "id": call.tool_call_id,
        "run_id": call.run_id,
        "step_id": call.step_id,
        "tool_name": call.tool_name,
        "tool_source": call.tool_source.value,
        "bundle_id": call.bundle_id,
        "bundle_digest": call.bundle_digest,
        "arguments_json": mutable_agent_json(call.arguments),
        "result_preview": call.result_preview,
        "confirmed": call.confirmed,
        "confirmation_id": call.confirmation_id,
        "status": call.status.value,
        "duration_ms": call.duration_ms,
        "created_at": _timestamp_to_datetime(call.created_at),
        "finished_at": (None if call.finished_at is None else _timestamp_to_datetime(call.finished_at)),
    }


def _run_from_row(row: Mapping[str, Any]) -> AgentRun:
    return AgentRun(
        run_id=row["id"],
        request_id=row["request_id"],
        user_id=row["user_id"],
        group_id=row["group_id"],
        conversation_id=row["conversation_id"],
        generation=row["generation"],
        model=row["model"],
        state=AgentRunState(row["status"]),
        started_at=_timestamp_from_row(row["started_at"]),
        finished_at=(None if row["finished_at"] is None else _timestamp_from_row(row["finished_at"])),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost=row["cost"],
        error_type=row["error_type"],
        error_message=row["error_message"],
    )


def _step_from_row(row: Mapping[str, Any]) -> AgentStep:
    return AgentStep(
        step_id=row["id"],
        run_id=row["run_id"],
        index=row["step_index"],
        type=AgentStepType(row["step_type"]),
        status=AgentStepStatus(row["status"]),
        model=row["model"],
        tool=row["tool_name"],
        input_preview=row["input_preview"],
        output_preview=row["output_preview"],
        error=row["error"],
        started_at=(None if row["started_at"] is None else _timestamp_from_row(row["started_at"])),
        finished_at=(None if row["finished_at"] is None else _timestamp_from_row(row["finished_at"])),
        duration_ms=row["duration_ms"],
    )


def _tool_call_from_row(row: Mapping[str, Any]) -> ToolCall:
    return ToolCall(
        tool_call_id=row["id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        tool_name=row["tool_name"],
        tool_source=ToolSource(row["tool_source"]),
        bundle_id=row["bundle_id"],
        bundle_digest=row["bundle_digest"],
        arguments=row["arguments_json"],
        status=ToolCallStatus(row["status"]),
        confirmed=row["confirmed"],
        confirmation_id=row["confirmation_id"],
        result_preview=row["result_preview"],
        created_at=_timestamp_from_row(row["created_at"]),
        duration_ms=row["duration_ms"],
        finished_at=(None if row["finished_at"] is None else _timestamp_from_row(row["finished_at"])),
    )


def _nullable_match(column: Any, value: object) -> Any:
    return column.is_(None) if value is None else column == value


class _PostgresAgentRepository:
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


class PostgresAgentRunRepository(
    _PostgresAgentRepository,
    AgentRunRepository,
):
    """AgentRun access using one explicit caller-owned transaction."""

    async def create(self, run: AgentRun) -> None:
        if not isinstance(run, AgentRun):
            raise TypeError("run 必须是 AgentRun")
        statement = sa.insert(agent_runs_table).values(**_run_values(run)).returning(agent_runs_table.c.id)
        result = await self._execute(
            statement,
            operation="agent_run.create",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one()
        except Exception as error:
            raise _unavailable("agent_run.create", error=error) from None
        if type(returned_id) is not str or returned_id != run.run_id:
            raise _unavailable("agent_run.create")

    async def get(self, run_id: str) -> AgentRun | None:
        run_id = validate_agent_run_id(run_id)
        statement = sa.select(*_AGENT_RUN_COLUMNS).where(agent_runs_table.c.id == run_id)
        result = await self._execute(statement, operation="agent_run.get")
        try:
            row = result.mappings().one_or_none()
            if row is None:
                return None
            if not isinstance(row, Mapping):
                raise TypeError("row is not a mapping")
            run = _run_from_row(row)
        except Exception as error:
            raise _unavailable("agent_run.get", error=error) from None
        if run.run_id != run_id:
            raise _unavailable("agent_run.get")
        return run

    async def replace(
        self,
        run: AgentRun,
        *,
        expected_state: AgentRunState,
        expected_generation: int,
    ) -> None:
        if not isinstance(run, AgentRun):
            raise TypeError("run 必须是 AgentRun")
        if not isinstance(expected_state, AgentRunState):
            raise ValueError("expected_state 必须是 AgentRunState")
        if not _valid_bigint(expected_generation):
            raise ValueError("expected_generation 必须是非负 PostgreSQL BIGINT")
        if run.generation != expected_generation:
            raise ValueError("run.generation 必须匹配 expected_generation")

        values = _run_values(run)
        statement = (
            sa.update(agent_runs_table)
            .where(
                agent_runs_table.c.id == run.run_id,
                agent_runs_table.c.request_id == run.request_id,
                agent_runs_table.c.user_id == run.user_id,
                _nullable_match(agent_runs_table.c.group_id, run.group_id),
                agent_runs_table.c.conversation_id == run.conversation_id,
                agent_runs_table.c.generation == expected_generation,
                agent_runs_table.c.status == expected_state.value,
                agent_runs_table.c.started_at == values["started_at"],
            )
            .values(
                model=run.model,
                status=run.state.value,
                finished_at=values["finished_at"],
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                cost=run.cost,
                error_type=run.error_type,
                error_message=run.error_message,
            )
            .returning(
                agent_runs_table.c.id,
                agent_runs_table.c.status,
                agent_runs_table.c.generation,
            )
        )
        result = await self._execute(
            statement,
            operation="agent_run.replace",
            integrity_is_conflict=True,
        )
        try:
            row = result.mappings().one_or_none()
        except Exception as error:
            raise _unavailable("agent_run.replace", error=error) from None
        if row is None:
            raise _conflict("agent_run.replace")
        if (
            not isinstance(row, Mapping)
            or row.get("id") != run.run_id
            or row.get("status") != run.state.value
            or row.get("generation") != run.generation
        ):
            raise _unavailable("agent_run.replace")


class PostgresAgentStepRepository(
    _PostgresAgentRepository,
    AgentStepRepository,
):
    """Append-only AgentStep snapshots and stable per-run pagination."""

    async def append(self, step: AgentStep) -> None:
        if not isinstance(step, AgentStep):
            raise TypeError("step 必须是 AgentStep")
        statement = sa.insert(agent_steps_table).values(**_step_values(step)).returning(agent_steps_table.c.id)
        result = await self._execute(
            statement,
            operation="agent_step.append",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one()
        except Exception as error:
            raise _unavailable("agent_step.append", error=error) from None
        if type(returned_id) is not str or returned_id != step.step_id:
            raise _unavailable("agent_step.append")

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[AgentStep]:
        run_id = validate_agent_run_id(run_id)
        if not isinstance(page, RepositoryPageRequest):
            raise TypeError("page 必须是 RepositoryPageRequest")
        boundary: tuple[int, str] | None = None
        if page.cursor is not None:
            boundary = _decode_step_cursor(page.cursor, run_id)

        statement = sa.select(*_AGENT_STEP_COLUMNS).where(agent_steps_table.c.run_id == run_id)
        if boundary is not None:
            boundary_index, boundary_id = boundary
            statement = statement.where(
                sa.or_(
                    agent_steps_table.c.step_index > boundary_index,
                    sa.and_(
                        agent_steps_table.c.step_index == boundary_index,
                        agent_steps_table.c.id > boundary_id,
                    ),
                )
            )
        statement = statement.order_by(
            agent_steps_table.c.step_index.asc(),
            agent_steps_table.c.id.asc(),
        ).limit(page.limit + 1)

        result = await self._execute(statement, operation="agent_step.list_for_run")
        try:
            rows = tuple(result.mappings().all())
            if len(rows) > page.limit + 1 or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("unexpected row collection")
            steps = tuple(_step_from_row(row) for row in rows)
        except Exception as error:
            raise _unavailable("agent_step.list_for_run", error=error) from None

        if any(step.run_id != run_id for step in steps):
            raise _unavailable("agent_step.list_for_run")
        keys = tuple((step.index, step.step_id) for step in steps)
        if any(earlier >= later for earlier, later in pairwise(keys)):
            raise _unavailable("agent_step.list_for_run")
        if boundary is not None and keys and keys[0] <= boundary:
            raise _unavailable("agent_step.list_for_run")

        visible = steps[: page.limit]
        next_cursor = None
        if len(steps) > page.limit:
            tail = visible[-1]
            next_cursor = _encode_step_cursor(run_id, tail.index, tail.step_id)
        return RepositoryPage(visible, next_cursor=next_cursor)


class PostgresToolCallRepository(
    _PostgresAgentRepository,
    ToolCallRepository,
):
    """ToolCall persistence with composite identity and status CAS."""

    async def create(self, call: ToolCall) -> None:
        if not isinstance(call, ToolCall):
            raise TypeError("call 必须是 ToolCall")
        statement = sa.insert(tool_calls_table).values(**_tool_call_values(call)).returning(tool_calls_table.c.id)
        result = await self._execute(
            statement,
            operation="tool_call.create",
            integrity_is_conflict=True,
        )
        try:
            returned_id = result.scalar_one()
        except Exception as error:
            raise _unavailable("tool_call.create", error=error) from None
        if type(returned_id) is not str or returned_id != call.tool_call_id:
            raise _unavailable("tool_call.create")

    async def get(self, tool_call_id: str) -> ToolCall | None:
        tool_call_id = validate_tool_call_id(tool_call_id)
        statement = sa.select(*_TOOL_CALL_COLUMNS).where(tool_calls_table.c.id == tool_call_id)
        result = await self._execute(statement, operation="tool_call.get")
        try:
            row = result.mappings().one_or_none()
            if row is None:
                return None
            if not isinstance(row, Mapping):
                raise TypeError("row is not a mapping")
            call = _tool_call_from_row(row)
        except Exception as error:
            raise _unavailable("tool_call.get", error=error) from None
        if call.tool_call_id != tool_call_id:
            raise _unavailable("tool_call.get")
        return call

    async def replace(
        self,
        call: ToolCall,
        *,
        expected_status: ToolCallStatus,
    ) -> None:
        if not isinstance(call, ToolCall):
            raise TypeError("call 必须是 ToolCall")
        if not isinstance(expected_status, ToolCallStatus):
            raise ValueError("expected_status 必须是 ToolCallStatus")
        values = _tool_call_values(call)
        statement = (
            sa.update(tool_calls_table)
            .where(
                tool_calls_table.c.id == call.tool_call_id,
                tool_calls_table.c.run_id == call.run_id,
                tool_calls_table.c.step_id == call.step_id,
                tool_calls_table.c.tool_name == call.tool_name,
                tool_calls_table.c.tool_source == call.tool_source.value,
                _nullable_match(tool_calls_table.c.bundle_id, call.bundle_id),
                _nullable_match(
                    tool_calls_table.c.bundle_digest,
                    call.bundle_digest,
                ),
                tool_calls_table.c.arguments_json == mutable_agent_json(call.arguments),
                tool_calls_table.c.created_at == values["created_at"],
                tool_calls_table.c.status == expected_status.value,
            )
            .values(
                result_preview=call.result_preview,
                confirmed=call.confirmed,
                confirmation_id=call.confirmation_id,
                status=call.status.value,
                duration_ms=call.duration_ms,
                finished_at=values["finished_at"],
            )
            .returning(tool_calls_table.c.id, tool_calls_table.c.status)
        )
        result = await self._execute(
            statement,
            operation="tool_call.replace",
            integrity_is_conflict=True,
        )
        try:
            row = result.mappings().one_or_none()
        except Exception as error:
            raise _unavailable("tool_call.replace", error=error) from None
        if row is None:
            raise _conflict("tool_call.replace")
        if not isinstance(row, Mapping) or row.get("id") != call.tool_call_id or row.get("status") != call.status.value:
            raise _unavailable("tool_call.replace")

    async def list_for_run(
        self,
        run_id: str,
        page: RepositoryPageRequest,
    ) -> RepositoryPage[ToolCall]:
        run_id = validate_agent_run_id(run_id)
        if not isinstance(page, RepositoryPageRequest):
            raise TypeError("page 必须是 RepositoryPageRequest")
        boundary: tuple[float, str] | None = None
        if page.cursor is not None:
            boundary = _decode_tool_call_cursor(page.cursor, run_id)

        statement = sa.select(*_TOOL_CALL_COLUMNS).where(tool_calls_table.c.run_id == run_id)
        if boundary is not None:
            boundary_created_at, boundary_id = boundary
            boundary_datetime = _timestamp_to_datetime(boundary_created_at)
            statement = statement.where(
                sa.or_(
                    tool_calls_table.c.created_at < boundary_datetime,
                    sa.and_(
                        tool_calls_table.c.created_at == boundary_datetime,
                        tool_calls_table.c.id < boundary_id,
                    ),
                )
            )
        statement = statement.order_by(
            tool_calls_table.c.created_at.desc(),
            tool_calls_table.c.id.desc(),
        ).limit(page.limit + 1)

        result = await self._execute(statement, operation="tool_call.list_for_run")
        try:
            rows = tuple(result.mappings().all())
            if len(rows) > page.limit + 1 or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("unexpected row collection")
            calls = tuple(_tool_call_from_row(row) for row in rows)
        except Exception as error:
            raise _unavailable("tool_call.list_for_run", error=error) from None

        if any(call.run_id != run_id for call in calls):
            raise _unavailable("tool_call.list_for_run")
        keys = tuple((call.created_at, call.tool_call_id) for call in calls)
        if any(older >= newer for newer, older in pairwise(keys)):
            raise _unavailable("tool_call.list_for_run")
        if boundary is not None and keys and keys[0] >= boundary:
            raise _unavailable("tool_call.list_for_run")

        visible = calls[: page.limit]
        next_cursor = None
        if len(calls) > page.limit:
            tail = visible[-1]
            next_cursor = _encode_tool_call_cursor(
                run_id,
                tail.created_at,
                tail.tool_call_id,
            )
        return RepositoryPage(visible, next_cursor=next_cursor)

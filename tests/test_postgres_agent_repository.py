from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRun,
    AgentRunState,
    AgentStep,
    AgentStepStatus,
    AgentStepType,
    ToolCall,
    ToolCallStatus,
)
from nonebot_plugin_moellmchats.database_schema import (
    agent_runs_table,
    agent_steps_table,
    tool_calls_table,
)
import nonebot_plugin_moellmchats.postgres_agent_repository as repository_module
from nonebot_plugin_moellmchats.postgres_agent_repository import (
    PostgresAgentRunRepository,
    PostgresAgentStepRepository,
    PostgresToolCallRepository,
)
from nonebot_plugin_moellmchats.repositories import (
    AgentRunRepository,
    AgentStepRepository,
    RepositoryConflictError,
    RepositoryPageRequest,
    RepositoryUnavailableError,
    ToolCallRepository,
)
from nonebot_plugin_moellmchats.tool_providers import ToolSource

_NOW = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)
_NOW_TS = _NOW.timestamp()
_UNSET = object()


class _Result:
    def __init__(
        self,
        *,
        scalar_one: object = _UNSET,
        row: object = _UNSET,
        rows: object = _UNSET,
    ) -> None:
        self._scalar_one = scalar_one
        self._row = row
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

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> Any:
        if self._row is _UNSET:
            raise AssertionError("mapping row was not configured")
        return self._resolve(self._row)

    def all(self) -> Any:
        if self._rows is _UNSET:
            raise AssertionError("mapping rows were not configured")
        return self._resolve(self._rows)


def _session(*results: object) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = results
    return session


def _run(
    *,
    state: AgentRunState = AgentRunState.EXECUTING,
    finished_at: float | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> AgentRun:
    if (
        state
        in {
            AgentRunState.COMPLETED,
            AgentRunState.FAILED,
            AgentRunState.CANCELLED,
            AgentRunState.TIMED_OUT,
            AgentRunState.REJECTED,
        }
        and finished_at is None
    ):
        finished_at = _NOW_TS + 2
    return AgentRun(
        run_id="run_agent_0001",
        request_id=17,
        user_id="user-1",
        group_id="group-1",
        conversation_id="conversation-1",
        generation=9,
        model="provider/model-1",
        state=state,
        started_at=_NOW_TS,
        finished_at=finished_at,
        input_tokens=123,
        output_tokens=45,
        cost=Decimal("0.001250000000"),
        error_type=error_type,
        error_message=error_message,
    )


def _step(
    index: int,
    *,
    run_id: str = "run_agent_0001",
    step_id: str | None = None,
) -> AgentStep:
    return AgentStep(
        step_id=step_id or f"step_agent_{index:04d}",
        run_id=run_id,
        index=index,
        type=AgentStepType.TOOL,
        status=AgentStepStatus.COMPLETED,
        tool="safe_lookup",
        input={"secret": "runtime-only"},
        output={"secret": "runtime-only-result"},
        input_preview=f"input-{index}",
        output_preview=f"output-{index}",
        started_at=_NOW_TS + index,
        finished_at=_NOW_TS + index + 0.25,
        duration_ms=250,
    )


def _call(
    sequence: int,
    *,
    run_id: str = "run_agent_0001",
    tool_call_id: str | None = None,
    status: ToolCallStatus = ToolCallStatus.COMPLETED,
) -> ToolCall:
    terminal = status in {
        ToolCallStatus.COMPLETED,
        ToolCallStatus.FAILED,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.TIMED_OUT,
        ToolCallStatus.REJECTED,
    }
    return ToolCall(
        tool_call_id=tool_call_id or f"call_agent_{sequence:04d}",
        run_id=run_id,
        step_id="step_agent_0001",
        tool_name="safe_lookup",
        tool_source=ToolSource.GENERATED,
        bundle_id="safe_bundle",
        bundle_digest="a" * 64,
        arguments={"sequence": sequence, "secret": "argument-not-in-preview"},
        status=status,
        confirmed=status is ToolCallStatus.COMPLETED,
        confirmation_id="confirmation_agent_0001",
        created_at=_NOW_TS + sequence,
        result=({"secret": "full-result-not-persisted", "sequence": sequence} if terminal else None),
        result_preview=f"result-{sequence}" if terminal else None,
        duration_ms=250 if terminal else None,
        finished_at=_NOW_TS + sequence + 0.25 if terminal else None,
    )


def _run_row(run: AgentRun) -> dict[str, Any]:
    return {
        "id": run.run_id,
        "request_id": run.request_id,
        "user_id": run.user_id,
        "group_id": run.group_id,
        "conversation_id": run.conversation_id,
        "generation": run.generation,
        "model": run.model,
        "status": run.state.value,
        "started_at": datetime.fromtimestamp(run.started_at, tz=timezone.utc),
        "finished_at": (None if run.finished_at is None else datetime.fromtimestamp(run.finished_at, tz=timezone.utc)),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost": run.cost,
        "error_type": run.error_type,
        "error_message": run.error_message,
    }


def _step_row(step: AgentStep) -> dict[str, Any]:
    return {
        "id": step.step_id,
        "run_id": step.run_id,
        "step_index": step.index,
        "step_type": step.type.value,
        "model": step.model,
        "tool_name": step.tool,
        "status": step.status.value,
        "started_at": (None if step.started_at is None else datetime.fromtimestamp(step.started_at, tz=timezone.utc)),
        "finished_at": (None if step.finished_at is None else datetime.fromtimestamp(step.finished_at, tz=timezone.utc)),
        "duration_ms": step.duration_ms,
        "input_preview": step.input_preview,
        "output_preview": step.output_preview,
        "error": step.error,
    }


def _call_row(call: ToolCall) -> dict[str, Any]:
    values = call.as_dict()
    return {
        "id": call.tool_call_id,
        "run_id": call.run_id,
        "step_id": call.step_id,
        "tool_name": call.tool_name,
        "tool_source": call.tool_source.value,
        "bundle_id": call.bundle_id,
        "bundle_digest": call.bundle_digest,
        "arguments_json": values["arguments"],
        "result_preview": call.result_preview,
        "confirmed": call.confirmed,
        "confirmation_id": call.confirmation_id,
        "status": call.status.value,
        "duration_ms": call.duration_ms,
        "created_at": datetime.fromtimestamp(call.created_at, tz=timezone.utc),
        "finished_at": (None if call.finished_at is None else datetime.fromtimestamp(call.finished_at, tz=timezone.utc)),
    }


def _compile(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()), dict(compiled.params)


def test_i04_existing_schema_is_complete_and_no_empty_revision_exists() -> None:
    assert set(agent_runs_table.c.keys()) == {
        "id",
        "request_id",
        "user_id",
        "group_id",
        "conversation_id",
        "generation",
        "model",
        "status",
        "started_at",
        "finished_at",
        "input_tokens",
        "output_tokens",
        "cost",
        "error_type",
        "error_message",
    }
    assert {"input_preview", "output_preview", "error", "duration_ms"} <= set(agent_steps_table.c.keys())
    assert {
        "tool_source",
        "bundle_id",
        "bundle_digest",
        "confirmation_id",
        "created_at",
        "finished_at",
        "duration_ms",
    } <= set(tool_calls_table.c.keys())

    version_root = Path(repository_module.__file__).parent / "migrations" / "versions"
    revisions = sorted(path.name for path in version_root.glob("[0-9][0-9][0-9][0-9]_*.py"))
    assert len(revisions) == 8
    assert revisions[-1].startswith("0008_")


def test_agent_domain_covers_every_persisted_field_without_storing_full_payloads() -> None:
    run = _run(
        state=AgentRunState.FAILED,
        error_type="ProviderError",
        error_message="sanitized failure",
    )
    step = _step(1)
    call = _call(1)

    assert run.conversation_id == "conversation-1"
    assert run.model == "provider/model-1"
    assert run.cost == Decimal("0.00125")
    assert run.error_type == "ProviderError"
    assert step.input_preview == "input-1"
    assert step.output_preview == "output-1"
    assert step.duration_ms == 250
    assert call.tool_source is ToolSource.GENERATED
    assert call.bundle_id == "safe_bundle"
    assert call.confirmation_id == "confirmation_agent_0001"
    assert call.duration_ms == 250

    persisted_step = AgentStep(
        step_id=step.step_id,
        run_id=step.run_id,
        index=step.index,
        type=step.type,
        status=step.status,
        tool=step.tool,
        input_preview=step.input_preview,
        output_preview=step.output_preview,
        started_at=step.started_at,
        finished_at=step.finished_at,
        duration_ms=step.duration_ms,
    )
    persisted_call = ToolCall(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        step_id=call.step_id,
        tool_name=call.tool_name,
        tool_source=call.tool_source,
        bundle_id=call.bundle_id,
        bundle_digest=call.bundle_digest,
        arguments=call.arguments,
        status=call.status,
        confirmed=call.confirmed,
        confirmation_id=call.confirmation_id,
        created_at=call.created_at,
        result_preview=call.result_preview,
        duration_ms=call.duration_ms,
        finished_at=call.finished_at,
    )
    assert persisted_step.input is None
    assert persisted_step.output is None
    assert persisted_call.result is None
    assert "full-result-not-persisted" not in str(persisted_call.as_dict())


def test_postgres_agent_repositories_require_explicit_async_sessions() -> None:
    for repository_type in (
        PostgresAgentRunRepository,
        PostgresAgentStepRepository,
        PostgresToolCallRepository,
    ):
        with pytest.raises(TypeError, match="AsyncSession"):
            repository_type(object())  # type: ignore[arg-type]

    session = _session()
    runs = PostgresAgentRunRepository(session)
    steps = PostgresAgentStepRepository(session)
    calls = PostgresToolCallRepository(session)
    assert isinstance(runs, AgentRunRepository)
    assert isinstance(steps, AgentStepRepository)
    assert isinstance(calls, ToolCallRepository)
    assert inspect.iscoroutinefunction(runs.replace)
    assert inspect.iscoroutinefunction(steps.list_for_run)
    assert inspect.iscoroutinefunction(calls.replace)


@pytest.mark.asyncio
async def test_agent_run_create_get_and_cas_replace_use_explicit_columns() -> None:
    original = _run()
    completed = replace(
        original,
        state=AgentRunState.COMPLETED,
        finished_at=_NOW_TS + 2,
        input_tokens=200,
        output_tokens=80,
        cost=Decimal("0.0025"),
    )
    session = _session(
        _Result(scalar_one=original.run_id),
        _Result(row=_run_row(original)),
        _Result(
            row={
                "id": completed.run_id,
                "status": completed.state.value,
                "generation": completed.generation,
            }
        ),
    )
    repository = PostgresAgentRunRepository(session)

    assert await repository.create(original) is None
    assert await repository.get(original.run_id) == original
    assert (
        await repository.replace(
            completed,
            expected_state=AgentRunState.EXECUTING,
            expected_generation=9,
        )
        is None
    )

    insert_sql, insert_params = _compile(session.execute.await_args_list[0].args[0])
    select_sql, select_params = _compile(session.execute.await_args_list[1].args[0])
    update_sql, update_params = _compile(session.execute.await_args_list[2].args[0])

    assert insert_sql.startswith("INSERT INTO agent_runs")
    assert "RETURNING agent_runs.id" in insert_sql
    assert original.conversation_id in insert_params.values()
    assert select_sql.startswith("SELECT agent_runs.id, agent_runs.request_id")
    assert "SELECT *" not in select_sql
    assert original.run_id in select_params.values()
    assert update_sql.startswith("UPDATE agent_runs SET")
    assert "agent_runs.status =" in update_sql
    assert "agent_runs.generation =" in update_sql
    assert "agent_runs.conversation_id =" in update_sql
    assert "agent_runs.started_at =" in update_sql
    assert "RETURNING agent_runs.id, agent_runs.status, agent_runs.generation" in update_sql
    assert AgentRunState.EXECUTING.value in update_params.values()
    assert AgentRunState.COMPLETED.value in update_params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_run_cas_conflict_and_invalid_generation_stop_after_one_statement() -> None:
    run = _run()
    session = _session(_Result(row=None))
    repository = PostgresAgentRunRepository(session)

    with pytest.raises(RepositoryConflictError, match=r"agent_run\.replace"):
        await repository.replace(
            run,
            expected_state=AgentRunState.PLANNING,
            expected_generation=9,
        )
    with pytest.raises(ValueError, match="匹配"):
        await repository.replace(
            run,
            expected_state=AgentRunState.EXECUTING,
            expected_generation=8,
        )

    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_step_append_and_stable_forward_keyset_pagination() -> None:
    step0 = _step(0)
    step1 = _step(1)
    step2 = _step(2)
    step3 = _step(3)
    session = _session(
        _Result(scalar_one=step0.step_id),
        _Result(rows=[_step_row(step0), _step_row(step1), _step_row(step2)]),
        _Result(rows=[_step_row(step2), _step_row(step3)]),
    )
    repository = PostgresAgentStepRepository(session)

    assert await repository.append(step0) is None
    first = await repository.list_for_run(
        step0.run_id,
        RepositoryPageRequest(limit=2),
    )
    second = await repository.list_for_run(
        step0.run_id,
        RepositoryPageRequest(limit=2, cursor=first.next_cursor),
    )

    assert tuple(step.index for step in first.items) == (0, 1)
    assert first.next_cursor is not None
    assert step0.run_id not in first.next_cursor
    assert tuple(step.index for step in second.items) == (2, 3)
    assert second.next_cursor is None
    assert all(step.input is None and step.output is None for step in first.items)

    insert_sql, insert_params = _compile(session.execute.await_args_list[0].args[0])
    first_sql, first_params = _compile(session.execute.await_args_list[1].args[0])
    second_sql, second_params = _compile(session.execute.await_args_list[2].args[0])
    assert insert_sql.startswith("INSERT INTO agent_steps")
    assert "input_preview" in insert_sql
    assert "output_preview" in insert_sql
    assert "runtime-only" not in str(insert_params)
    assert "ORDER BY agent_steps.step_index ASC, agent_steps.id ASC" in first_sql
    assert " OFFSET " not in first_sql
    assert step0.run_id in first_params.values()
    assert "agent_steps.step_index >" in second_sql
    assert 1 in second_params.values()
    assert step1.step_id in second_params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_step_cursor_is_run_bound_and_rejected_before_sql() -> None:
    steps = (_step(0), _step(1), _step(2))
    session = _session(_Result(rows=[_step_row(step) for step in steps]))
    repository = PostgresAgentStepRepository(session)
    page = await repository.list_for_run(
        "run_agent_0001",
        RepositoryPageRequest(limit=2),
    )
    assert page.next_cursor is not None

    with pytest.raises(ValueError, match="当前 run"):
        await repository.list_for_run(
            "run_agent_0002",
            RepositoryPageRequest(limit=2, cursor=page.next_cursor),
        )
    with pytest.raises(ValueError, match="有效 AgentStep"):
        await repository.list_for_run(
            "run_agent_0001",
            RepositoryPageRequest(limit=2, cursor="agent-step-v1.Zm9v"),
        )
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_tool_call_create_get_and_status_cas_preserve_composite_identity() -> None:
    pending = _call(1, status=ToolCallStatus.PENDING)
    completed = replace(
        pending,
        status=ToolCallStatus.COMPLETED,
        confirmed=True,
        result={"secret": "full-result-not-persisted"},
        result_preview="safe result",
        duration_ms=250,
        finished_at=pending.created_at + 0.25,
    )
    session = _session(
        _Result(scalar_one=pending.tool_call_id),
        _Result(row=_call_row(pending)),
        _Result(row={"id": completed.tool_call_id, "status": "completed"}),
    )
    repository = PostgresToolCallRepository(session)

    assert await repository.create(pending) is None
    assert await repository.get(pending.tool_call_id) == pending
    assert (
        await repository.replace(
            completed,
            expected_status=ToolCallStatus.PENDING,
        )
        is None
    )

    insert_sql, insert_params = _compile(session.execute.await_args_list[0].args[0])
    select_sql, _ = _compile(session.execute.await_args_list[1].args[0])
    update_sql, update_params = _compile(session.execute.await_args_list[2].args[0])
    assert insert_sql.startswith("INSERT INTO tool_calls")
    assert {"sequence": 1, "secret": "argument-not-in-preview"} in insert_params.values()
    assert "full-result-not-persisted" not in str(insert_params)
    assert select_sql.startswith("SELECT tool_calls.id, tool_calls.run_id")
    assert "SELECT *" not in select_sql
    assert update_sql.startswith("UPDATE tool_calls SET")
    assert "tool_calls.run_id =" in update_sql
    assert "tool_calls.step_id =" in update_sql
    assert "tool_calls.arguments_json =" in update_sql
    assert "tool_calls.created_at =" in update_sql
    assert "tool_calls.status =" in update_sql
    assert ToolCallStatus.PENDING.value in update_params.values()
    assert ToolCallStatus.COMPLETED.value in update_params.values()
    assert "full-result-not-persisted" not in str(update_params)
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_call_descending_keyset_and_cursor_are_stable_and_run_bound() -> None:
    newest = _call(4)
    middle = _call(3)
    extra = _call(2)
    oldest = _call(1)
    session = _session(
        _Result(rows=[_call_row(newest), _call_row(middle), _call_row(extra)]),
        _Result(rows=[_call_row(extra), _call_row(oldest)]),
    )
    repository = PostgresToolCallRepository(session)

    first = await repository.list_for_run(
        newest.run_id,
        RepositoryPageRequest(limit=2),
    )
    second = await repository.list_for_run(
        newest.run_id,
        RepositoryPageRequest(limit=2, cursor=first.next_cursor),
    )

    assert tuple(call.tool_call_id for call in first.items) == (
        newest.tool_call_id,
        middle.tool_call_id,
    )
    assert first.next_cursor is not None
    assert newest.run_id not in first.next_cursor
    assert tuple(call.tool_call_id for call in second.items) == (
        extra.tool_call_id,
        oldest.tool_call_id,
    )
    assert all(call.result is None for call in (*first.items, *second.items))

    first_sql, first_params = _compile(session.execute.await_args_list[0].args[0])
    second_sql, second_params = _compile(session.execute.await_args_list[1].args[0])
    assert "ORDER BY tool_calls.created_at DESC, tool_calls.id DESC" in first_sql
    assert " OFFSET " not in first_sql
    assert newest.run_id in first_params.values()
    assert "tool_calls.created_at <" in second_sql
    assert middle.tool_call_id in second_params.values()

    with pytest.raises(ValueError, match="当前 run"):
        await repository.list_for_run(
            "run_agent_0002",
            RepositoryPageRequest(limit=2, cursor=first.next_cursor),
        )
    assert session.execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_step_row(_step(1)), _step_row(_step(0))],
        [_step_row(_step(0)), _step_row(_step(0))],
        [_step_row(_step(0, run_id="run_agent_0002"))],
        [_step_row(_step(index)) for index in range(4)],
        [object()],
    ],
)
async def test_step_repository_rejects_corrupt_or_contract_breaking_rows(
    rows: list[Any],
) -> None:
    session = _session(_Result(rows=rows))
    repository = PostgresAgentStepRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"agent_step\.list_for_run"):
        await repository.list_for_run(
            "run_agent_0001",
            RepositoryPageRequest(limit=2),
        )
    assert session.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_call_row(_call(1)), _call_row(_call(2))],
        [_call_row(_call(2)), _call_row(_call(2))],
        [_call_row(_call(2, run_id="run_agent_0002"))],
        [_call_row(_call(index)) for index in range(4)],
        [object()],
    ],
)
async def test_tool_call_repository_rejects_corrupt_or_contract_breaking_rows(
    rows: list[Any],
) -> None:
    session = _session(_Result(rows=rows))
    repository = PostgresToolCallRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"tool_call\.list_for_run"):
        await repository.list_for_run(
            "run_agent_0001",
            RepositoryPageRequest(limit=2),
        )
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_integrity_conflict_is_sanitized_and_unknown_write_is_not_replayed() -> None:
    integrity_error = IntegrityError(
        "INSERT secret-result",
        {"password": "top-secret"},
        RuntimeError("postgresql://user:top-secret@db.internal/private"),
    )
    session = _session(integrity_error)
    repository = PostgresToolCallRepository(session)

    with pytest.raises(RepositoryConflictError, match="IntegrityError") as error:
        await repository.create(_call(1))
    rendered = str(error.value)
    for secret in ("secret-result", "password", "top-secret", "db.internal"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()

    unknown = RuntimeError("timeout after UPDATE with secret-result at db.internal")
    unknown_session = _session(unknown)
    unknown_repository = PostgresAgentRunRepository(unknown_session)
    with pytest.raises(RepositoryUnavailableError, match="RuntimeError") as unknown_error:
        await unknown_repository.create(_run())
    assert "secret-result" not in str(unknown_error.value)
    assert "db.internal" not in str(unknown_error.value)
    assert unknown_error.value.__cause__ is None
    assert unknown_session.execute.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_and_repository_never_retries() -> None:
    session = _session(asyncio.CancelledError())
    repository = PostgresAgentRunRepository(session)

    with pytest.raises(asyncio.CancelledError):
        await repository.get("run_agent_0001")
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


def test_agent_repository_module_has_no_global_session_or_engine() -> None:
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import Session

    forbidden = (Engine, AsyncEngine, Session, AsyncSession)
    assert not any(isinstance(value, forbidden) for value in vars(repository_module).values())

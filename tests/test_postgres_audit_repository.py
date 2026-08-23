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

from nonebot_plugin_moellmchats.audit_event import AuditEventRecord
import nonebot_plugin_moellmchats.postgres_audit_repository as repository_module
from nonebot_plugin_moellmchats.postgres_audit_repository import (
    PostgresAuditRepository,
)
from nonebot_plugin_moellmchats.repositories import (
    AuditRepository,
    BatchAuditRepository,
    RepositoryConflictError,
    RepositoryPageRequest,
    RepositoryUnavailableError,
)

_NOW = datetime(2026, 8, 23, 1, 5, tzinfo=timezone.utc)
_UNSET = object()


class _Result:
    def __init__(
        self,
        *,
        scalar_rows: object = _UNSET,
        rows: object = _UNSET,
    ) -> None:
        self._scalar_rows = scalar_rows
        self._rows = rows

    @staticmethod
    def _resolve(value: object) -> Any:
        if isinstance(value, BaseException):
            raise value
        return value

    def scalars(self) -> _Result:
        return self

    def mappings(self) -> _Result:
        return self

    def all(self) -> Any:
        value = self._scalar_rows if self._scalar_rows is not _UNSET else self._rows
        if value is _UNSET:
            raise AssertionError("rows were not configured")
        return self._resolve(value)


def _session(*results: object) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = results
    return session


def _event(
    event_id: int | None = None,
    *,
    sequence: int = 1,
    event_type: str = "tool_draft_created",
    run_id: str = "run-audit-1",
    created_at: datetime = _NOW,
) -> AuditEventRecord:
    return AuditEventRecord(
        event_id=event_id,
        event_type=event_type,
        actor_user_id="qq:10001",
        actor_type="user",
        target_type="tool_bundle",
        target_id=f"bundle-{sequence}",
        run_id=run_id,
        tool_call_id=None,
        metadata_json={"sequence": sequence, "nested": ["safe"]},
        created_at=created_at,
    )


def _row(record: AuditEventRecord) -> dict[str, object]:
    values = record.as_dict()
    return {
        "id": values["event_id"],
        "event_type": values["event_type"],
        "actor_user_id": values["actor_user_id"],
        "actor_type": values["actor_type"],
        "target_type": values["target_type"],
        "target_id": values["target_id"],
        "run_id": values["run_id"],
        "tool_call_id": values["tool_call_id"],
        "metadata_json": values["metadata_json"],
        "created_at": values["created_at"],
    }


def _compile(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()), dict(compiled.params)


def test_repository_requires_explicit_session_and_satisfies_batch_protocol() -> None:
    with pytest.raises(TypeError, match="AsyncSession"):
        PostgresAuditRepository(object())  # type: ignore[arg-type]

    repository = PostgresAuditRepository(_session())
    assert isinstance(repository, AuditRepository)
    assert isinstance(repository, BatchAuditRepository)
    assert inspect.iscoroutinefunction(repository.append)
    assert inspect.iscoroutinefunction(repository.append_batch)
    assert inspect.iscoroutinefunction(repository.list_for_run)


@pytest.mark.asyncio
async def test_batch_append_is_one_multirow_statement_without_transaction_ownership() -> None:
    records = (_event(sequence=1), _event(sequence=2, event_type="runtime_reload"))
    session = _session(_Result(scalar_rows=[101, 102]))
    repository = PostgresAuditRepository(session)

    assert await repository.append_batch(records) is None

    assert session.execute.await_count == 1
    sql, params = _compile(session.execute.await_args.args[0])
    assert sql.startswith("INSERT INTO audit_events")
    assert sql.count("), (") == 1
    assert "RETURNING audit_events.id" in sql
    for expected in (
        "tool_draft_created",
        "runtime_reload",
        "qq:10001",
        "bundle-1",
        "bundle-2",
        "run-audit-1",
        _NOW,
    ):
        assert expected in params.values()
    assert {"sequence": 1, "nested": ["safe"]} in params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_immediate_append_accepts_security_event_but_public_batch_rejects_it() -> None:
    immediate = _event(event_type="tool_approved")
    session = _session(_Result(scalar_rows=[1]))
    repository = PostgresAuditRepository(session)

    assert await repository.append(immediate) is None
    sql, _ = _compile(session.execute.await_args.args[0])
    assert sql.startswith("INSERT INTO audit_events")
    assert "RETURNING audit_events.id" in sql
    assert session.execute.await_count == 1

    rejected_session = _session()
    rejected_repository = PostgresAuditRepository(rejected_session)
    with pytest.raises(ValueError, match="即时 append"):
        await rejected_repository.append_batch((immediate,))
    rejected_session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        (),
        [_event()],
        tuple(_event(sequence=index + 1) for index in range(101)),
        (_event(event_id=1),),
        (object(),),
    ],
)
async def test_batch_append_rejects_invalid_batches_before_sql(events: object) -> None:
    session = _session()
    repository = PostgresAuditRepository(session)

    with pytest.raises(ValueError, match=r"events|draft"):
        await repository.append_batch(events)  # type: ignore[arg-type]

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "returned_ids",
    [
        [],
        [1],
        [1, 1],
        [1, 0],
        [1, True],
        [1, 1 << 63],
        RuntimeError("bad returning rows with private-bundle"),
    ],
)
async def test_batch_append_rejects_unverifiable_returning_rows(
    returned_ids: object,
) -> None:
    session = _session(_Result(scalar_rows=returned_ids))
    repository = PostgresAuditRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"audit\.append_batch") as error:
        await repository.append_batch((_event(sequence=1), _event(sequence=2)))

    assert "private-bundle" not in str(error.value)
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_list_for_run_uses_stable_created_at_and_id_keyset_cursor() -> None:
    newest = _event(30, sequence=3, created_at=_NOW + timedelta(seconds=2))
    middle = _event(20, sequence=2, created_at=_NOW + timedelta(seconds=1))
    extra = _event(10, sequence=1, created_at=_NOW)
    older = _event(9, sequence=4, created_at=_NOW - timedelta(seconds=1))
    session = _session(
        _Result(rows=[_row(newest), _row(middle), _row(extra)]),
        _Result(rows=[_row(extra), _row(older)]),
    )
    repository = PostgresAuditRepository(session)

    first = await repository.list_for_run(
        "run-audit-1",
        RepositoryPageRequest(limit=2),
    )
    assert first.items == (newest, middle)
    assert first.next_cursor is not None

    second = await repository.list_for_run(
        "run-audit-1",
        RepositoryPageRequest(limit=2, cursor=first.next_cursor),
    )
    assert second.items == (extra, older)
    assert second.next_cursor is None

    first_sql, first_params = _compile(session.execute.await_args_list[0].args[0])
    second_sql, second_params = _compile(session.execute.await_args_list[1].args[0])
    assert first_sql.startswith("SELECT audit_events.id, audit_events.event_type")
    assert "SELECT *" not in first_sql
    assert "WHERE audit_events.run_id =" in first_sql
    assert "ORDER BY audit_events.created_at DESC, audit_events.id DESC" in first_sql
    assert " OFFSET " not in first_sql
    assert 3 in first_params.values()
    assert "audit_events.created_at <" in second_sql
    assert "audit_events.created_at =" in second_sql
    assert "audit_events.id <" in second_sql
    assert middle.created_at in second_params.values()
    assert middle.event_id in second_params.values()


@pytest.mark.asyncio
async def test_cursor_is_bound_to_run_and_tampering_is_rejected_before_sql() -> None:
    valid_session = _session(
        _Result(
            rows=[
                _row(_event(2)),
                _row(_event(1, created_at=_NOW - timedelta(seconds=1))),
            ]
        ),
    )
    valid_repository = PostgresAuditRepository(valid_session)
    page = await valid_repository.list_for_run(
        "run-audit-1",
        RepositoryPageRequest(limit=1),
    )
    assert page.next_cursor is not None

    session = _session()
    repository = PostgresAuditRepository(session)
    with pytest.raises(ValueError, match="当前 run"):
        await repository.list_for_run(
            "run-audit-2",
            RepositoryPageRequest(limit=1, cursor=page.next_cursor),
        )
    tampered = page.next_cursor[:-1] + ("A" if page.next_cursor[-1] != "A" else "B")
    with pytest.raises(ValueError, match="有效 audit 游标"):
        await repository.list_for_run(
            "run-audit-1",
            RepositoryPageRequest(limit=1, cursor=tampered),
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_row(_event(1, created_at=_NOW)), _row(_event(2, created_at=_NOW))],
        [_row(_event(1)), _row(_event(1))],
        [_row(_event(1, run_id="run-audit-2"))],
        [{**_row(_event(1)), "metadata_json": ["not-an-object"]}],
        [object()],
    ],
)
async def test_list_rejects_corrupt_cross_run_or_non_descending_rows(
    rows: list[object],
) -> None:
    session = _session(_Result(rows=rows))
    repository = PostgresAuditRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="list_for_run") as error:
        await repository.list_for_run(
            "run-audit-1",
            RepositoryPageRequest(limit=2),
        )

    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_integrity_conflict_is_sanitized_and_never_retried() -> None:
    backend_error = IntegrityError(
        "INSERT INTO audit_events VALUES ('private-bundle')",
        {"password": "top-secret"},
        RuntimeError("postgresql://user:top-secret@db.internal/private"),
    )
    session = _session(backend_error)
    repository = PostgresAuditRepository(session)

    with pytest.raises(RepositoryConflictError, match="IntegrityError") as error:
        await repository.append(_event(event_type="tool_approved"))

    rendered = str(error.value)
    for secret in ("private-bundle", "password", "top-secret", "db.internal"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_unknown_write_result_is_unavailable_and_never_replayed() -> None:
    session = _session(RuntimeError("timeout after private-bundle at db.internal with top-secret"))
    repository = PostgresAuditRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="RuntimeError") as error:
        await repository.append_batch((_event(),))

    rendered = str(error.value)
    for secret in ("private-bundle", "db.internal", "top-secret"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_repository_preserves_cancellation_without_wrapping_or_retrying() -> None:
    session = _session(asyncio.CancelledError())
    repository = PostgresAuditRepository(session)

    with pytest.raises(asyncio.CancelledError):
        await repository.list_for_run(
            "run-audit-1",
            RepositoryPageRequest(),
        )

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_repository_does_not_own_commit_after_verified_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(_Result(scalar_rows=[1]))
    repository = PostgresAuditRepository(session)
    execute_calls = 0

    original_execute = repository_module.PostgresAuditRepository._execute

    async def counted_execute(*args: Any, **kwargs: Any) -> Any:
        nonlocal execute_calls
        execute_calls += 1
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(
        repository_module.PostgresAuditRepository,
        "_execute",
        counted_execute,
    )

    await repository.append(_event(event_type="tool_approved"))

    assert execute_calls == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nonebot_plugin_moellmchats.model_usage import ModelUsageRecord
import nonebot_plugin_moellmchats.postgres_usage_repository as repository_module
from nonebot_plugin_moellmchats.postgres_usage_repository import (
    PostgresUsageRepository,
)
from nonebot_plugin_moellmchats.repositories import (
    BatchUsageRepository,
    RepositoryConflictError,
    RepositoryPageRequest,
    RepositoryUnavailableError,
    UsageRepository,
)

_NOW = datetime(2026, 8, 23, 0, 20, tzinfo=timezone.utc)
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


def _usage(
    usage_id: int | None = None,
    *,
    sequence: int = 1,
    run_id: str = "run-usage-1",
    created_at: datetime = _NOW,
) -> ModelUsageRecord:
    return ModelUsageRecord(
        usage_id=usage_id,
        run_id=run_id,
        provider="openai",
        model=f"gpt-{sequence}",
        input_tokens=sequence,
        output_tokens=sequence + 1,
        reasoning_tokens=sequence - 1,
        cached_tokens=0,
        cost=Decimal(f"0.00000{sequence}"),
        created_at=created_at,
    )


def _row(record: ModelUsageRecord) -> dict[str, object]:
    values = record.as_dict()
    return {
        "id": values["usage_id"],
        "run_id": values["run_id"],
        "provider": values["provider"],
        "model": values["model"],
        "input_tokens": values["input_tokens"],
        "output_tokens": values["output_tokens"],
        "reasoning_tokens": values["reasoning_tokens"],
        "cached_tokens": values["cached_tokens"],
        "cost": values["cost"],
        "created_at": values["created_at"],
    }


def _compile(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()), dict(compiled.params)


def test_repository_requires_explicit_session_and_satisfies_batch_protocol() -> None:
    with pytest.raises(TypeError, match="AsyncSession"):
        PostgresUsageRepository(object())  # type: ignore[arg-type]

    repository = PostgresUsageRepository(_session())
    assert isinstance(repository, UsageRepository)
    assert isinstance(repository, BatchUsageRepository)
    assert inspect.iscoroutinefunction(repository.append)
    assert inspect.iscoroutinefunction(repository.append_batch)
    assert inspect.iscoroutinefunction(repository.list_for_run)


@pytest.mark.asyncio
async def test_batch_append_is_one_multirow_statement_without_transaction_ownership() -> None:
    records = (_usage(sequence=1), _usage(sequence=2))
    session = _session(_Result(scalar_rows=[101, 102]))
    repository = PostgresUsageRepository(session)

    assert await repository.append_batch(records) is None

    assert session.execute.await_count == 1
    sql, params = _compile(session.execute.await_args.args[0])
    assert sql.startswith("INSERT INTO model_usage")
    assert sql.count("), (") == 1
    assert "RETURNING model_usage.id" in sql
    for expected in (
        "run-usage-1",
        "openai",
        "gpt-1",
        "gpt-2",
        Decimal("0.000001"),
        Decimal("0.000002"),
        _NOW,
    ):
        assert expected in params.values()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.flush.assert_not_awaited()
    session.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_append_uses_the_same_batch_statement_path() -> None:
    session = _session(_Result(scalar_rows=[1]))
    repository = PostgresUsageRepository(session)

    assert await repository.append(_usage()) is None

    sql, _ = _compile(session.execute.await_args.args[0])
    assert sql.startswith("INSERT INTO model_usage")
    assert "RETURNING model_usage.id" in sql
    assert session.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usages",
    [
        (),
        [_usage()],
        tuple(_usage(sequence=index + 1) for index in range(101)),
        (_usage(usage_id=1),),
        (object(),),
    ],
)
async def test_batch_append_rejects_invalid_batches_before_sql(usages: object) -> None:
    session = _session()
    repository = PostgresUsageRepository(session)

    with pytest.raises(ValueError, match=r"usages|draft"):
        await repository.append_batch(usages)  # type: ignore[arg-type]

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
        RuntimeError("bad returning rows with secret-model"),
    ],
)
async def test_batch_append_rejects_unverifiable_returning_rows(
    returned_ids: object,
) -> None:
    session = _session(_Result(scalar_rows=returned_ids))
    repository = PostgresUsageRepository(session)

    with pytest.raises(RepositoryUnavailableError, match=r"usage\.append_batch") as error:
        await repository.append_batch((_usage(sequence=1), _usage(sequence=2)))

    assert "secret-model" not in str(error.value)
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_list_for_run_uses_stable_created_at_and_id_keyset_cursor() -> None:
    newest = _usage(30, sequence=3, created_at=_NOW + timedelta(seconds=2))
    middle = _usage(20, sequence=2, created_at=_NOW + timedelta(seconds=1))
    extra = _usage(10, sequence=1, created_at=_NOW)
    older = _usage(9, sequence=4, created_at=_NOW - timedelta(seconds=1))
    session = _session(
        _Result(rows=[_row(newest), _row(middle), _row(extra)]),
        _Result(rows=[_row(extra), _row(older)]),
    )
    repository = PostgresUsageRepository(session)

    first = await repository.list_for_run(
        "run-usage-1",
        RepositoryPageRequest(limit=2),
    )
    assert first.items == (newest, middle)
    assert first.next_cursor is not None

    second = await repository.list_for_run(
        "run-usage-1",
        RepositoryPageRequest(limit=2, cursor=first.next_cursor),
    )
    assert second.items == (extra, older)
    assert second.next_cursor is None

    first_sql, first_params = _compile(session.execute.await_args_list[0].args[0])
    second_sql, second_params = _compile(session.execute.await_args_list[1].args[0])
    assert first_sql.startswith("SELECT model_usage.id, model_usage.run_id")
    assert "SELECT *" not in first_sql
    assert "WHERE model_usage.run_id =" in first_sql
    assert "ORDER BY model_usage.created_at DESC, model_usage.id DESC" in first_sql
    assert " OFFSET " not in first_sql
    assert 3 in first_params.values()
    assert "model_usage.created_at <" in second_sql
    assert "model_usage.created_at =" in second_sql
    assert "model_usage.id <" in second_sql
    assert middle.created_at in second_params.values()
    assert middle.usage_id in second_params.values()


@pytest.mark.asyncio
async def test_cursor_is_bound_to_run_and_tampering_is_rejected_before_sql() -> None:
    record = _usage(1)
    first_session = _session(_Result(rows=[_row(record), _row(record)]))
    first_repository = PostgresUsageRepository(first_session)

    with pytest.raises(RepositoryUnavailableError):
        await first_repository.list_for_run(
            "run-usage-1",
            RepositoryPageRequest(limit=1),
        )

    valid_session = _session(
        _Result(rows=[_row(_usage(2)), _row(_usage(1, created_at=_NOW - timedelta(seconds=1)))]),
    )
    valid_repository = PostgresUsageRepository(valid_session)
    page = await valid_repository.list_for_run(
        "run-usage-1",
        RepositoryPageRequest(limit=1),
    )
    assert page.next_cursor is not None

    session = _session()
    repository = PostgresUsageRepository(session)
    with pytest.raises(ValueError, match="当前 run"):
        await repository.list_for_run(
            "run-usage-2",
            RepositoryPageRequest(limit=1, cursor=page.next_cursor),
        )
    tampered = page.next_cursor[:-1] + ("A" if page.next_cursor[-1] != "A" else "B")
    with pytest.raises(ValueError, match="有效 usage 游标"):
        await repository.list_for_run(
            "run-usage-1",
            RepositoryPageRequest(limit=1, cursor=tampered),
        )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [_row(_usage(1, created_at=_NOW)), _row(_usage(2, created_at=_NOW))],
        [_row(_usage(1)), _row(_usage(1))],
        [_row(_usage(1, run_id="run-usage-2"))],
        [{**_row(_usage(1)), "cost": float("nan")}],
        [object()],
    ],
)
async def test_list_rejects_corrupt_cross_run_or_non_descending_rows(
    rows: list[object],
) -> None:
    session = _session(_Result(rows=rows))
    repository = PostgresUsageRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="list_for_run") as error:
        await repository.list_for_run(
            "run-usage-1",
            RepositoryPageRequest(limit=2),
        )

    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_integrity_conflict_is_sanitized_and_never_retried() -> None:
    backend_error = IntegrityError(
        "INSERT INTO model_usage VALUES ('private-model')",
        {"password": "top-secret"},
        RuntimeError("postgresql://user:top-secret@db.internal/private"),
    )
    session = _session(backend_error)
    repository = PostgresUsageRepository(session)

    with pytest.raises(RepositoryConflictError, match="IntegrityError") as error:
        await repository.append_batch((_usage(),))

    rendered = str(error.value)
    for secret in ("private-model", "password", "top-secret", "db.internal"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_unknown_write_result_is_unavailable_and_never_replayed() -> None:
    session = _session(RuntimeError("timeout after private-model at db.internal with top-secret"))
    repository = PostgresUsageRepository(session)

    with pytest.raises(RepositoryUnavailableError, match="RuntimeError") as error:
        await repository.append_batch((_usage(),))

    rendered = str(error.value)
    for secret in ("private-model", "db.internal", "top-secret"):
        assert secret not in rendered
    assert error.value.__cause__ is None
    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_repository_preserves_cancellation_without_wrapping_or_retrying() -> None:
    session = _session(asyncio.CancelledError())
    repository = PostgresUsageRepository(session)

    with pytest.raises(asyncio.CancelledError):
        await repository.list_for_run(
            "run-usage-1",
            RepositoryPageRequest(),
        )

    assert session.execute.await_count == 1


@pytest.mark.asyncio
async def test_repository_does_not_own_commit_even_after_verified_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(_Result(scalar_rows=[1]))
    repository = PostgresUsageRepository(session)
    execute_calls = 0

    original_execute = repository_module.PostgresUsageRepository._execute

    async def counted_execute(*args: Any, **kwargs: Any) -> Any:
        nonlocal execute_calls
        execute_calls += 1
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(
        repository_module.PostgresUsageRepository,
        "_execute",
        counted_execute,
    )

    await repository.append_batch((_usage(),))

    assert execute_calls == 1
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()

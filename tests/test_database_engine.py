from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from math import inf, nan
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from sqlalchemy.engine import URL

from nonebot_plugin_moellmchats.database_engine import (
    DatabaseEngineBusyError,
    DatabaseEngineDisposalError,
    DatabaseEngineInitializationError,
    DatabaseEngineManager,
    DatabaseEngineOwnershipError,
    DatabaseEngineSettings,
)

_DATABASE_URL = "postgresql+asyncpg://db-user:top-secret@db.internal/private_db"


def _settings(**changes: Any) -> DatabaseEngineSettings:
    values: dict[str, Any] = {"database_url": _DATABASE_URL}
    values.update(changes)
    return DatabaseEngineSettings(**values)


@contextmanager
def _new_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def test_database_engine_settings_build_safe_bounded_options() -> None:
    settings = _settings(
        pool_size=7,
        max_overflow=11,
        pool_timeout_seconds=12.5,
        pool_recycle_seconds=900,
        connect_timeout_seconds=4.5,
        statement_timeout_seconds=8.25,
        application_name="moellmchats.test",
    )
    options = settings.engine_options()

    assert options == {
        "pool_size": 7,
        "max_overflow": 11,
        "pool_timeout": 12.5,
        "pool_recycle": 900,
        "pool_pre_ping": True,
        "pool_use_lifo": True,
        "echo": False,
        "hide_parameters": True,
        "connect_args": {
            "timeout": 4.5,
            "command_timeout": 8.25,
            "server_settings": {
                "application_name": "moellmchats.test",
                "statement_timeout": "8250",
            },
        },
    }
    options["connect_args"]["server_settings"]["statement_timeout"] = "1"
    assert settings.engine_options()["connect_args"]["server_settings"] == {
        "application_name": "moellmchats.test",
        "statement_timeout": "8250",
    }


def test_database_engine_settings_never_render_credentials() -> None:
    settings = _settings()
    diagnostics = settings.safe_diagnostics()
    rendered = repr(settings)
    stored = repr(vars(settings))

    assert diagnostics == {
        "configured": True,
        "driver": "postgresql+asyncpg",
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout_seconds": 30.0,
        "pool_recycle_seconds": 1800,
        "connect_timeout_seconds": 10.0,
        "statement_timeout_seconds": 30.0,
        "application_name": "nonebot-plugin-moellmchats",
    }
    diagnostics["driver"] = "changed"
    assert settings.safe_diagnostics()["driver"] == "postgresql+asyncpg"
    for secret in ("db-user", "top-secret", "db.internal", "private_db"):
        assert secret not in rendered
        assert secret not in repr(diagnostics)
        assert secret not in stored
    assert "database_url" not in vars(settings)
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        123,
        "",
        "x" * 4097,
        "postgresql+asyncpg://user:pass@host/db\n",
        "postgresql://user:pass@host/db",
        "postgresql+psycopg://user:pass@host/db",
        "sqlite+aiosqlite:///tmp/test.db",
        "postgresql+asyncpg://user:pass@host",
    ],
)
def test_database_engine_settings_reject_invalid_or_sync_urls(
    database_url: object,
) -> None:
    with pytest.raises(ValueError, match="database_url"):
        DatabaseEngineSettings(database_url=database_url)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "query",
    [
        "password=leak",
        "passwd=leak",
        "token=leak",
        "api-key=leak",
        "credentials=leak",
    ],
)
def test_database_engine_settings_reject_query_credentials(query: str) -> None:
    with pytest.raises(ValueError, match="敏感凭据字段") as error:
        _settings(database_url=f"{_DATABASE_URL}?{query}")

    assert "leak" not in str(error.value)
    assert "top-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pool_size", 0),
        ("pool_size", 101),
        ("pool_size", True),
        ("max_overflow", -1),
        ("max_overflow", 101),
        ("max_overflow", False),
        ("pool_timeout_seconds", 0),
        ("pool_timeout_seconds", 301),
        ("pool_timeout_seconds", nan),
        ("pool_recycle_seconds", 29),
        ("pool_recycle_seconds", 86_401),
        ("connect_timeout_seconds", 0),
        ("connect_timeout_seconds", 61),
        ("connect_timeout_seconds", inf),
        ("statement_timeout_seconds", 0),
        ("statement_timeout_seconds", 3_601),
        ("statement_timeout_seconds", True),
    ],
)
def test_database_engine_settings_reject_unbounded_pool_and_timeouts(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


def test_database_engine_settings_bound_total_connections() -> None:
    with pytest.raises(ValueError, match=r"pool_size \+ max_overflow"):
        _settings(pool_size=100, max_overflow=51)


@pytest.mark.parametrize(
    "application_name",
    ["", "1starts-with-number", "contains space", "🙂", "x" * 64, 123],
)
def test_database_engine_settings_reject_unsafe_application_names(
    application_name: object,
) -> None:
    with pytest.raises(ValueError, match="application_name"):
        _settings(application_name=application_name)


def test_database_engine_manager_requires_typed_settings() -> None:
    with pytest.raises(TypeError, match="DatabaseEngineSettings"):
        DatabaseEngineManager(object())  # type: ignore[arg-type]


def test_database_engine_manager_requires_a_running_loop() -> None:
    manager = DatabaseEngineManager(_settings())

    with pytest.raises(DatabaseEngineOwnershipError, match="event loop"):
        manager.get_engine()

    assert manager.initialized is False


@pytest.mark.asyncio
async def test_database_engine_manager_is_lazy_singleton_and_disposes() -> None:
    settings = _settings(pool_size=3, max_overflow=4)
    calls: list[tuple[URL, dict[str, Any]]] = []

    def factory(url: URL, **options: Any) -> AsyncEngine:
        calls.append((url, options))
        return create_async_engine(url, **options)

    manager = DatabaseEngineManager(settings, engine_factory=factory)
    assert manager.initialized is False
    assert calls == []

    first = manager.get_engine()
    second = manager.get_engine()

    assert first is second
    assert len(calls) == 1
    assert calls[0][0].drivername == "postgresql+asyncpg"
    assert calls[0][0].password == "top-secret"
    assert calls[0][1] == settings.engine_options()
    assert manager.initialized is True
    assert manager.safe_diagnostics()["initialized"] is True
    assert "top-secret" not in repr(manager)
    assert "top-secret" not in repr(manager.safe_diagnostics())
    assert await manager.dispose() is True
    assert await manager.dispose() is False
    assert manager.initialized is False


@pytest.mark.asyncio
async def test_database_engine_manager_can_recreate_after_clean_dispose() -> None:
    manager = DatabaseEngineManager(_settings())

    first = manager.get_engine()
    assert await manager.dispose() is True
    second = manager.get_engine()

    assert first is not second
    assert await manager.dispose() is True


def test_database_engine_manager_rejects_cross_loop_reuse() -> None:
    loops: list[asyncio.AbstractEventLoop]
    with _new_event_loop() as first_loop, _new_event_loop() as second_loop:
        loops = [first_loop]
        manager = DatabaseEngineManager(_settings(), loop_provider=lambda: loops[0])
        engine = manager.get_engine()
        loops[0] = second_loop

        with pytest.raises(DatabaseEngineOwnershipError, match="跨 event loop"):
            manager.get_engine()

        loops[0] = first_loop
        first_loop.run_until_complete(manager.dispose())
        assert manager.initialized is False
        assert engine is not manager.get_engine()
        first_loop.run_until_complete(manager.dispose())


def test_database_engine_manager_rejects_cross_process_reuse() -> None:
    pids = [101]
    with _new_event_loop() as loop:
        manager = DatabaseEngineManager(
            _settings(),
            pid_provider=lambda: pids[0],
            loop_provider=lambda: loop,
        )
        manager.get_engine()
        pids[0] = 202

        with pytest.raises(DatabaseEngineOwnershipError, match="跨进程"):
            manager.get_engine()

        pids[0] = 101
        loop.run_until_complete(manager.dispose())


@pytest.mark.asyncio
async def test_database_engine_factory_failure_is_sanitized() -> None:
    def broken_factory(_url: URL, **_options: Any) -> AsyncEngine:
        raise RuntimeError(f"could not connect to {_DATABASE_URL}")

    manager = DatabaseEngineManager(_settings(), engine_factory=broken_factory)

    with pytest.raises(DatabaseEngineInitializationError) as error:
        manager.get_engine()

    assert "RuntimeError" in str(error.value)
    assert "top-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert manager.initialized is False


@pytest.mark.asyncio
async def test_database_engine_factory_must_return_async_engine() -> None:
    manager = DatabaseEngineManager(
        _settings(),
        engine_factory=lambda _url, **_options: object(),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(DatabaseEngineInitializationError, match="AsyncEngine"):
        manager.get_engine()

    assert manager.initialized is False


@pytest.mark.asyncio
async def test_database_engine_disposal_failure_is_retryable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DatabaseEngineManager(_settings())
    manager.get_engine()
    original_dispose = AsyncEngine.dispose

    async def broken_dispose(self: AsyncEngine, close: bool = True) -> None:
        del self, close
        raise RuntimeError(f"dispose leaked {_DATABASE_URL}")

    monkeypatch.setattr(AsyncEngine, "dispose", broken_dispose)
    with pytest.raises(DatabaseEngineDisposalError) as error:
        await manager.dispose()

    assert "RuntimeError" in str(error.value)
    assert "top-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert manager.initialized is True
    assert manager.safe_diagnostics()["disposing"] is False

    monkeypatch.setattr(AsyncEngine, "dispose", original_dispose)
    assert await manager.dispose() is True


@pytest.mark.asyncio
async def test_database_engine_rejects_access_while_disposing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DatabaseEngineManager(_settings())
    manager.get_engine()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_dispose = AsyncEngine.dispose

    async def delayed_dispose(self: AsyncEngine, close: bool = True) -> None:
        entered.set()
        await release.wait()
        await original_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", delayed_dispose)
    task = asyncio.create_task(manager.dispose())
    await entered.wait()

    with pytest.raises(DatabaseEngineBusyError, match="正在释放"):
        manager.get_engine()
    with pytest.raises(DatabaseEngineBusyError, match="已在释放"):
        await manager.dispose()

    release.set()
    assert await task is True

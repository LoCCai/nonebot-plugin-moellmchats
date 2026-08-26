from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from math import inf, nan
from typing import Any

import pytest
from redis.asyncio import Redis
from redis.asyncio.connection import AbstractConnection

from nonebot_plugin_moellmchats.redis_client import (
    RedisClientBusyError,
    RedisClientCloseError,
    RedisClientInitializationError,
    RedisClientManager,
    RedisClientOwnershipError,
    RedisClientSettings,
)

_REDIS_URL = "redis://cache-user:top-secret@cache.internal:6380/7"


def _settings(**changes: Any) -> RedisClientSettings:
    values: dict[str, Any] = {"redis_url": _REDIS_URL}
    values.update(changes)
    return RedisClientSettings(**values)


@contextmanager
def _new_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def test_redis_client_settings_build_safe_bounded_options() -> None:
    settings = _settings(
        max_connections=17,
        socket_connect_timeout_seconds=3.5,
        socket_timeout_seconds=9.25,
        health_check_interval_seconds=45,
        client_name="moellmchats.test",
    )
    options = settings.client_options()

    assert options == {
        "max_connections": 17,
        "socket_connect_timeout": 3.5,
        "socket_timeout": 9.25,
        "socket_keepalive": True,
        "health_check_interval": 45,
        "encoding": "utf-8",
        "encoding_errors": "strict",
        "decode_responses": False,
        "protocol": 2,
        "client_name": "moellmchats.test",
    }
    options["max_connections"] = 999
    assert settings.client_options()["max_connections"] == 17


def test_redis_client_settings_force_tls_verification_for_rediss() -> None:
    settings = _settings(redis_url="rediss://cache-user:top-secret@cache.internal:6380/9")

    assert settings.client_options()["ssl_cert_reqs"] == "required"
    assert settings.client_options()["ssl_check_hostname"] is True
    assert settings.safe_diagnostics()["tls"] is True
    assert settings.safe_diagnostics()["database"] == 9


def test_redis_client_settings_never_render_credentials_or_endpoint() -> None:
    settings = _settings()
    diagnostics = settings.safe_diagnostics()
    rendered = repr(settings)
    stored = repr(vars(settings))

    assert diagnostics == {
        "configured": True,
        "scheme": "redis",
        "tls": False,
        "database": 7,
        "max_connections": 50,
        "socket_connect_timeout_seconds": 5.0,
        "socket_timeout_seconds": 10.0,
        "health_check_interval_seconds": 30,
        "client_name": "nonebot-plugin-moellmchats",
    }
    diagnostics["scheme"] = "changed"
    assert settings.safe_diagnostics()["scheme"] == "redis"
    for secret in ("cache-user", "top-secret", "cache.internal", "6380"):
        assert secret not in rendered
        assert secret not in repr(diagnostics)
        assert secret not in stored
    assert "redis_url" not in vars(settings)
    assert "<redacted>" in rendered


@pytest.mark.parametrize(
    "redis_url",
    [
        None,
        123,
        "",
        "x" * 4097,
        "redis://host/0\n",
        "http://host/0",
        "unix:///tmp/redis.sock",
        "redis:///0",
        "redis://host/not-a-db",
        "redis://host/1/2",
        "redis://host/-1",
        "redis://host/65536",
        "redis://host:0/0",
        "redis://host:65536/0",
        "redis://host/0?socket_timeout=9999",
        "redis://host/0#fragment",
        "redis://host/%ZZ",
        "redis://user:%0Asecret@host/0",
    ],
)
def test_redis_client_settings_reject_invalid_or_ambiguous_urls(
    redis_url: object,
) -> None:
    with pytest.raises(ValueError, match="redis_url"):
        RedisClientSettings(redis_url=redis_url)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_connections", 0),
        ("max_connections", 1_001),
        ("max_connections", True),
        ("socket_connect_timeout_seconds", 0),
        ("socket_connect_timeout_seconds", 61),
        ("socket_connect_timeout_seconds", inf),
        ("socket_timeout_seconds", 0),
        ("socket_timeout_seconds", 301),
        ("socket_timeout_seconds", nan),
        ("socket_timeout_seconds", False),
        ("health_check_interval_seconds", -1),
        ("health_check_interval_seconds", 301),
        ("health_check_interval_seconds", True),
    ],
)
def test_redis_client_settings_reject_unbounded_pool_and_timeouts(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


@pytest.mark.parametrize(
    "client_name",
    ["", "1starts-with-number", "contains space", "🙂", "x" * 64, 123],
)
def test_redis_client_settings_reject_unsafe_client_names(
    client_name: object,
) -> None:
    with pytest.raises(ValueError, match="client_name"):
        _settings(client_name=client_name)


def test_redis_client_manager_requires_typed_settings() -> None:
    with pytest.raises(TypeError, match="RedisClientSettings"):
        RedisClientManager(object())  # type: ignore[arg-type]


def test_redis_client_manager_requires_a_running_loop() -> None:
    manager = RedisClientManager(_settings())

    with pytest.raises(RedisClientOwnershipError, match="event loop"):
        manager.get_client()

    assert manager.initialized is False


@pytest.mark.asyncio
async def test_redis_client_manager_is_lazy_singleton_and_closes_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    connect_calls = 0

    async def reject_connect(self: AbstractConnection) -> None:
        nonlocal connect_calls
        del self
        connect_calls += 1
        raise AssertionError("Redis connection was opened during lazy construction")

    def factory(url: str, **options: Any) -> Redis:
        calls.append((url, options))
        return Redis.from_url(url, **options)

    monkeypatch.setattr(AbstractConnection, "connect", reject_connect)
    settings = _settings(max_connections=7)
    manager = RedisClientManager(settings, client_factory=factory)
    assert manager.initialized is False
    assert calls == []

    first = manager.get_client()
    second = manager.get_client()

    assert first is second
    assert len(calls) == 1
    assert calls[0] == (_REDIS_URL, settings.client_options())
    assert first.connection_pool.max_connections == 7
    assert connect_calls == 0
    assert manager.initialized is True
    assert manager.safe_diagnostics()["initialized"] is True
    assert "top-secret" not in repr(manager)
    assert "top-secret" not in repr(manager.safe_diagnostics())
    assert await manager.aclose() is True
    assert await manager.aclose() is False
    assert connect_calls == 0
    assert manager.initialized is False


@pytest.mark.asyncio
async def test_redis_client_manager_can_recreate_after_clean_close() -> None:
    manager = RedisClientManager(_settings())

    first = manager.get_client()
    assert await manager.aclose() is True
    second = manager.get_client()

    assert first is not second
    assert await manager.aclose() is True


def test_redis_client_manager_rejects_cross_loop_reuse() -> None:
    loops: list[asyncio.AbstractEventLoop]
    with _new_event_loop() as first_loop, _new_event_loop() as second_loop:
        loops = [first_loop]
        manager = RedisClientManager(_settings(), loop_provider=lambda: loops[0])
        client = manager.get_client()
        loops[0] = second_loop

        with pytest.raises(RedisClientOwnershipError, match="跨 event loop"):
            manager.get_client()

        loops[0] = first_loop
        first_loop.run_until_complete(manager.aclose())
        assert manager.initialized is False
        assert client is not manager.get_client()
        first_loop.run_until_complete(manager.aclose())


def test_redis_client_manager_rejects_cross_process_reuse() -> None:
    pids = [101]
    with _new_event_loop() as loop:
        manager = RedisClientManager(
            _settings(),
            pid_provider=lambda: pids[0],
            loop_provider=lambda: loop,
        )
        manager.get_client()
        pids[0] = 202

        with pytest.raises(RedisClientOwnershipError, match="跨进程"):
            manager.get_client()

        pids[0] = 101
        loop.run_until_complete(manager.aclose())


@pytest.mark.asyncio
async def test_redis_client_factory_failure_is_retryable_and_sanitized() -> None:
    def broken_factory(_url: str, **_options: Any) -> Redis:
        raise RuntimeError(f"could not connect to {_REDIS_URL}")

    manager = RedisClientManager(_settings(), client_factory=broken_factory)

    with pytest.raises(RedisClientInitializationError) as error:
        manager.get_client()

    assert "RuntimeError" in str(error.value)
    assert "top-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert manager.initialized is False


@pytest.mark.asyncio
async def test_redis_client_factory_must_return_async_redis() -> None:
    manager = RedisClientManager(
        _settings(),
        client_factory=lambda _url, **_options: object(),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(RedisClientInitializationError, match="Redis client"):
        manager.get_client()

    assert manager.initialized is False


@pytest.mark.asyncio
async def test_redis_client_close_failure_is_retryable_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RedisClientManager(_settings())
    manager.get_client()
    original_aclose = Redis.aclose

    async def broken_aclose(self: Redis, close_connection_pool: bool | None = None) -> None:
        del self, close_connection_pool
        raise RuntimeError(f"close leaked {_REDIS_URL}")

    monkeypatch.setattr(Redis, "aclose", broken_aclose)
    with pytest.raises(RedisClientCloseError) as error:
        await manager.aclose()

    assert "RuntimeError" in str(error.value)
    assert "top-secret" not in str(error.value)
    assert error.value.__cause__ is None
    assert manager.initialized is True
    assert manager.safe_diagnostics()["closing"] is False

    monkeypatch.setattr(Redis, "aclose", original_aclose)
    assert await manager.aclose() is True


@pytest.mark.asyncio
async def test_redis_client_rejects_access_while_closing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RedisClientManager(_settings())
    manager.get_client()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_aclose = Redis.aclose

    async def delayed_aclose(self: Redis, close_connection_pool: bool | None = None) -> None:
        entered.set()
        await release.wait()
        await original_aclose(self, close_connection_pool=close_connection_pool)

    monkeypatch.setattr(Redis, "aclose", delayed_aclose)
    task = asyncio.create_task(manager.aclose())
    await entered.wait()

    with pytest.raises(RedisClientBusyError, match="正在关闭"):
        manager.get_client()
    with pytest.raises(RedisClientBusyError, match="已在关闭"):
        await manager.aclose()

    release.set()
    assert await task is True


@pytest.mark.asyncio
async def test_redis_client_close_cancellation_restores_retryable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RedisClientManager(_settings())
    manager.get_client()
    entered = asyncio.Event()
    never_release = asyncio.Event()
    original_aclose = Redis.aclose

    async def blocked_aclose(self: Redis, close_connection_pool: bool | None = None) -> None:
        del self, close_connection_pool
        entered.set()
        await never_release.wait()

    monkeypatch.setattr(Redis, "aclose", blocked_aclose)
    task = asyncio.create_task(manager.aclose())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert manager.initialized is True
    assert manager.safe_diagnostics()["closing"] is False
    monkeypatch.setattr(Redis, "aclose", original_aclose)
    assert await manager.aclose() is True

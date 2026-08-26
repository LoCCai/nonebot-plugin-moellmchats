from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
import hashlib
from math import inf, nan
from typing import Any

from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
import pytest
import pytest_asyncio
from redis.asyncio.client import Pipeline
from redis.exceptions import WatchError

from nonebot_plugin_moellmchats.chat_history import MessageRecord, mutable_history_json
from nonebot_plugin_moellmchats.history_hot_cache import (
    HistoryCacheLoadToken,
    HistoryHotCacheProtocol,
    HistoryHotCacheUnavailableError,
    HistoryWindow,
)
import nonebot_plugin_moellmchats.redis_history_hot_cache as redis_history_hot_cache
from nonebot_plugin_moellmchats.redis_history_hot_cache import (
    RedisHistoryHotCache,
    RedisHistoryHotCacheConflictError,
    RedisHistoryHotCacheSettings,
    RedisHistoryHotCacheUnavailableError,
)

_GENERATION_A = "a" * 32
_GENERATION_B = "b" * 32
_GENERATION_C = "c" * 32


class ManualClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _generation_factory(*values: str) -> Callable[[], str]:
    generations = iter(values)
    return lambda: next(generations)


def _settings(**changes: Any) -> RedisHistoryHotCacheSettings:
    values: dict[str, Any] = {
        "key_prefix": "moellm-test",
        "ttl_seconds": 60.0,
        "load_timeout_seconds": 10.0,
        "max_messages": 4,
        "max_payload_bytes": 65_536,
        "operation_retries": 8,
    }
    values.update(changes)
    return RedisHistoryHotCacheSettings(**values)


def _key(
    conversation_id: str = "conversation-alpha",
    *,
    prefix: str = "moellm-test",
) -> str:
    fingerprint = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
    return f"{prefix}:history:{{{fingerprint}}}"


def _message(
    message_id: int | None,
    *,
    conversation_id: str = "conversation-alpha",
    content: str | None = None,
    structured_content: Any = None,
) -> MessageRecord:
    return MessageRecord(
        message_id=message_id,
        conversation_id=conversation_id,
        platform_message_id=f"platform-{message_id}" if message_id is not None else None,
        role="user" if message_id is None or message_id % 2 else "assistant",
        sender_id="user-1",
        content=content if content is not None else f"message-{message_id}",
        structured_content=structured_content,
        created_at=datetime(2026, 8, 22, 12, 0, message_id or 0, tzinfo=timezone.utc),
    )


def _window(
    *message_ids: int,
    conversation_id: str = "conversation-alpha",
    has_older: bool = False,
    content: str | None = None,
) -> HistoryWindow:
    return HistoryWindow(
        conversation_id=conversation_id,
        messages=tuple(
            _message(
                message_id,
                conversation_id=conversation_id,
                content=content,
                structured_content={"parts": [message_id, {"ok": True}]},
            )
            for message_id in message_ids
        ),
        has_older=has_older,
    )


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[FakeRedis]:
    client = FakeRedis(
        server=FakeServer(version=(6, 2)),
        decode_responses=False,
    )
    try:
        yield client
    finally:
        await client.aclose()


def test_redis_history_hot_cache_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        key_prefix="tenant.alpha",
        ttl_seconds=90,
        load_timeout_seconds=12.5,
        max_messages=19,
        max_payload_bytes=32_768,
        operation_retries=7,
    )

    assert settings.ttl_milliseconds == 90_000
    assert settings.load_timeout_milliseconds == 12_500
    assert settings.safe_diagnostics() == {
        "key_prefix": "tenant.alpha",
        "ttl_seconds": 90.0,
        "load_timeout_seconds": 12.5,
        "max_messages": 19,
        "max_payload_bytes": 32_768,
        "operation_retries": 7,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key_prefix", ""),
        ("key_prefix", "1invalid"),
        ("key_prefix", "bad prefix"),
        ("key_prefix", "x" * 97),
        ("ttl_seconds", 0),
        ("ttl_seconds", 86_401),
        ("ttl_seconds", inf),
        ("load_timeout_seconds", 0),
        ("load_timeout_seconds", 301),
        ("load_timeout_seconds", nan),
        ("max_messages", 0),
        ("max_messages", 201),
        ("max_payload_bytes", 1_023),
        ("max_payload_bytes", 16_777_217),
        ("operation_retries", 0),
        ("operation_retries", 65),
        ("operation_retries", True),
    ],
)
def test_redis_history_hot_cache_settings_reject_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


def test_redis_load_timeout_cannot_outlive_ready_ttl() -> None:
    with pytest.raises(ValueError, match="load_timeout_seconds"):
        _settings(ttl_seconds=5, load_timeout_seconds=6)


def test_redis_history_hot_cache_requires_explicit_typed_dependencies(
    redis_client: FakeRedis,
) -> None:
    with pytest.raises(TypeError, match="redis-py"):
        RedisHistoryHotCache(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RedisHistoryHotCacheSettings"):
        RedisHistoryHotCache(redis_client, settings=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="clock"):
        RedisHistoryHotCache(redis_client, clock=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="generation_factory"):
        RedisHistoryHotCache(redis_client, generation_factory=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lookup_reserves_hashed_key_with_ttl_and_no_raw_identity(
    redis_client: FakeRedis,
) -> None:
    conversation_id = "conversation-secret-用户"
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(key_prefix="tenant.alpha"),
        clock=ManualClock(),
        generation_factory=lambda: _GENERATION_A,
    )

    lookup = await cache.lookup(conversation_id, limit=2)
    key = _key(conversation_id, prefix="tenant.alpha")
    keys = await redis_client.keys("*")
    raw = await redis_client.get(key)
    ttl = await redis_client.pttl(key)

    assert isinstance(cache, HistoryHotCacheProtocol)
    assert lookup.hit is False
    assert lookup.load_token is not None
    assert lookup.load_token.generation == _GENERATION_A
    assert lookup.load_token.expires_at == 110.0
    assert keys == [key.encode("utf-8")]
    assert conversation_id.encode("utf-8") not in key.encode("utf-8")
    assert raw is not None
    assert conversation_id.encode("utf-8") not in raw
    assert 0 < ttl <= 10_000
    assert "conversation-secret" not in repr(cache)
    assert cache.safe_diagnostics() == {
        "backend": "redis",
        "configured": True,
        **_settings(key_prefix="tenant.alpha").safe_diagnostics(),
    }


@pytest.mark.asyncio
async def test_publish_round_trips_immutable_messages_and_recent_suffix(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=4)
    assert lookup.load_token is not None
    window = _window(1, 2, 3, 4, content="机密消息\nline two")

    assert await cache.publish(lookup.load_token, window) is True
    assert await cache.publish(lookup.load_token, window) is False
    hit = await cache.lookup("conversation-alpha", limit=2)

    assert hit.window is not None
    assert [message.message_id for message in hit.window.messages] == [3, 4]
    assert hit.window.has_older is True
    assert hit.window.messages[-1].content == "机密消息\nline two"
    structured: Any = mutable_history_json(hit.window.messages[-1].structured_content)
    assert structured["parts"][1]["ok"] is True
    assert 0 < await redis_client.pttl(_key()) <= 60_000


@pytest.mark.asyncio
async def test_complete_short_window_remains_complete(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=4)
    assert lookup.load_token is not None
    assert await cache.publish(lookup.load_token, _window(1, 2)) is True

    hit = await cache.lookup("conversation-alpha", limit=4)
    assert hit.window == _window(1, 2)
    assert hit.window is not None
    assert hit.window.has_older is False


@pytest.mark.asyncio
async def test_invalidation_rejects_late_publish_and_reserves_new_generation(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=_generation_factory(_GENERATION_A, _GENERATION_B),
    )
    first = await cache.lookup("conversation-alpha", limit=4)
    assert first.load_token is not None

    await cache.invalidate("conversation-alpha")
    assert await cache.publish(first.load_token, _window(1)) is False
    second = await cache.lookup("conversation-alpha", limit=4)
    assert second.load_token is not None
    assert second.load_token.generation == _GENERATION_B
    assert await cache.publish(second.load_token, _window(1, 2)) is True


@pytest.mark.asyncio
async def test_evicted_reservation_cannot_be_replayed(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=_generation_factory(_GENERATION_A, _GENERATION_B),
    )
    first = await cache.lookup("conversation-alpha", limit=4)
    assert first.load_token is not None
    await redis_client.delete(_key())

    assert await cache.publish(first.load_token, _window(1)) is False
    second = await cache.lookup("conversation-alpha", limit=4)
    assert second.load_token is not None
    assert second.load_token.generation == _GENERATION_B


@pytest.mark.asyncio
async def test_concurrent_misses_share_reservation_and_only_one_publish_wins(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    first, second = await asyncio.gather(
        cache.lookup("conversation-alpha", limit=4),
        cache.lookup("conversation-alpha", limit=4),
    )
    assert first.load_token is not None
    assert second.load_token is not None
    assert second.load_token.generation == first.load_token.generation
    assert second.load_token.conversation_fingerprint == first.load_token.conversation_fingerprint

    results = await asyncio.gather(
        cache.publish(first.load_token, _window(1)),
        cache.publish(second.load_token, _window(1)),
    )
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_publish_rejects_expired_wrong_conversation_and_oversized_windows(
    redis_client: FakeRedis,
) -> None:
    clock = ManualClock()
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(max_messages=2),
        clock=clock,
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=2)
    assert lookup.load_token is not None

    with pytest.raises(ValueError, match="max_messages"):
        await cache.publish(lookup.load_token, _window(1, 2, 3))
    with pytest.raises(ValueError, match="不匹配"):
        await cache.publish(
            lookup.load_token,
            _window(1, conversation_id="conversation-beta"),
        )
    clock.value = lookup.load_token.expires_at
    assert await cache.publish(lookup.load_token, _window(1)) is False


@pytest.mark.asyncio
async def test_publish_rejects_payload_over_byte_limit_without_leaking_content(
    redis_client: FakeRedis,
) -> None:
    secret = "sensitive-body-" * 200
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(max_payload_bytes=1_024),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=1)
    assert lookup.load_token is not None

    with pytest.raises(RedisHistoryHotCacheUnavailableError) as error_info:
        await cache.publish(lookup.load_token, _window(1, content=secret))
    assert secret not in str(error_info.value)


@pytest.mark.asyncio
async def test_lookup_rejects_invalid_limits_and_publish_types(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(max_messages=2),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=2)
    assert lookup.load_token is not None

    for limit in (0, 3, True):
        with pytest.raises(ValueError, match="limit"):
            await cache.lookup("conversation-alpha", limit=limit)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="load_token"):
        await cache.publish(object(), _window(1))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="window"):
        await cache.publish(lookup.load_token, object())  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b'{"conversation":"bad"}',
        b'{"version":1,"kind":"loading","generation":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","conversation":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}',
        b"\xff",
    ],
)
async def test_corrupt_cache_payload_is_never_returned_as_a_hit(
    redis_client: FakeRedis,
    raw: bytes,
) -> None:
    await redis_client.set(_key(), raw, px=10_000)
    cache = RedisHistoryHotCache(redis_client, settings=_settings())

    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="payload"):
        await cache.lookup("conversation-alpha", limit=2)


@pytest.mark.asyncio
async def test_oversized_or_persistent_cache_payload_is_rejected(
    redis_client: FakeRedis,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(max_payload_bytes=1_024),
    )
    await redis_client.set(_key(), b"x" * 1_025, px=10_000)
    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="大小"):
        await cache.lookup("conversation-alpha", limit=2)

    await redis_client.set(
        _key(),
        redis_history_hot_cache._wire_bytes(
            redis_history_hot_cache._loading_payload(
                conversation_fingerprint=hashlib.sha256(b"conversation-alpha").hexdigest(),
                generation=_GENERATION_A,
            )
        ),
    )
    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="缺少 TTL"):
        await cache.lookup("conversation-alpha", limit=2)


@pytest.mark.asyncio
async def test_ttl_above_configured_bound_is_rejected(
    redis_client: FakeRedis,
) -> None:
    payload = redis_history_hot_cache._wire_bytes(
        redis_history_hot_cache._loading_payload(
            conversation_fingerprint=hashlib.sha256(b"conversation-alpha").hexdigest(),
            generation=_GENERATION_A,
        )
    )
    await redis_client.set(_key(), payload, px=20_000)
    cache = RedisHistoryHotCache(redis_client, settings=_settings(load_timeout_seconds=10))

    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="TTL"):
        await cache.lookup("conversation-alpha", limit=2)


@pytest.mark.asyncio
async def test_publish_rejects_reservation_without_bounded_ttl(
    redis_client: FakeRedis,
) -> None:
    fingerprint = hashlib.sha256(b"conversation-alpha").hexdigest()
    payload = redis_history_hot_cache._wire_bytes(
        redis_history_hot_cache._loading_payload(
            conversation_fingerprint=fingerprint,
            generation=_GENERATION_A,
        )
    )
    await redis_client.set(_key(), payload)
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        clock=ManualClock(),
    )
    token = HistoryCacheLoadToken(
        conversation_fingerprint=fingerprint,
        generation=_GENERATION_A,
        expires_at=110.0,
    )

    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="reservation TTL"):
        await cache.publish(token, _window(1))


@pytest.mark.asyncio
async def test_backend_failure_is_sanitized_and_cancellation_propagates(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = RedisHistoryHotCache(redis_client, settings=_settings())

    async def failed_get(_key: str) -> bytes:
        raise RuntimeError("redis://user:password@secret-host/private-message")

    monkeypatch.setattr(redis_client, "get", failed_get)
    with pytest.raises(RedisHistoryHotCacheUnavailableError) as error_info:
        await cache.lookup("conversation-secret", limit=2)
    rendered = str(error_info.value)
    assert "RuntimeError" in rendered
    assert "password" not in rendered
    assert "secret-host" not in rendered
    assert "conversation-secret" not in rendered

    async def cancelled_get(_key: str) -> bytes:
        raise asyncio.CancelledError

    monkeypatch.setattr(redis_client, "get", cancelled_get)
    with pytest.raises(asyncio.CancelledError):
        await cache.lookup("conversation-secret", limit=2)


@pytest.mark.asyncio
async def test_invalid_reserve_and_invalidate_responses_are_rejected(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    original_set = redis_client.set

    async def invalid_set(*args: Any, **kwargs: Any) -> bytes:
        return b"OK"

    monkeypatch.setattr(redis_client, "set", invalid_set)
    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="reserve"):
        await cache.lookup("conversation-alpha", limit=2)
    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="invalidate"):
        await cache.invalidate("conversation-alpha")
    monkeypatch.setattr(redis_client, "set", original_set)


@pytest.mark.asyncio
async def test_publish_watch_conflicts_are_bounded(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(operation_retries=2),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=2)
    assert lookup.load_token is not None

    async def always_conflict(_self: Pipeline, *args: Any, **kwargs: Any) -> list[object]:
        raise WatchError

    monkeypatch.setattr(Pipeline, "execute", always_conflict)
    with pytest.raises(RedisHistoryHotCacheConflictError, match="冲突过多"):
        await cache.publish(lookup.load_token, _window(1))


@pytest.mark.asyncio
async def test_invalid_pipeline_result_is_unavailable(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=lambda: _GENERATION_A,
    )
    lookup = await cache.lookup("conversation-alpha", limit=2)
    assert lookup.load_token is not None

    async def invalid_result(_self: Pipeline, *args: Any, **kwargs: Any) -> list[object]:
        return [b"OK"]

    monkeypatch.setattr(Pipeline, "execute", invalid_result)
    with pytest.raises(RedisHistoryHotCacheUnavailableError, match="publish"):
        await cache.publish(lookup.load_token, _window(1))


@pytest.mark.asyncio
async def test_generation_factory_failure_and_cancellation_are_safe(
    redis_client: FakeRedis,
) -> None:
    def failed() -> str:
        raise RuntimeError("secret-generation-value")

    cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(),
        generation_factory=failed,
    )
    with pytest.raises(HistoryHotCacheUnavailableError) as error_info:
        await cache.lookup("conversation-secret", limit=2)
    assert "secret-generation-value" not in str(error_info.value)
    assert "conversation-secret" not in str(error_info.value)

    def cancelled() -> str:
        raise asyncio.CancelledError

    cancelled_cache = RedisHistoryHotCache(
        redis_client,
        settings=_settings(key_prefix="cancelled"),
        generation_factory=cancelled,
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled_cache.lookup("conversation-alpha", limit=2)


def test_foreign_or_expired_token_is_not_rendered_with_identity() -> None:
    token = HistoryCacheLoadToken(
        conversation_fingerprint="f" * 64,
        generation=_GENERATION_C,
        expires_at=0.0,
    )
    assert "f" * 64 not in repr(token)

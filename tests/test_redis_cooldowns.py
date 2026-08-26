from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
from math import inf, nan
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
import pytest
import pytest_asyncio
from redis.asyncio.client import Pipeline
from redis.exceptions import WatchError

from nonebot_plugin_moellmchats import chat_runtime
from nonebot_plugin_moellmchats.admission import AdmissionRejected
from nonebot_plugin_moellmchats.cooldowns import CooldownError, CooldownLease
import nonebot_plugin_moellmchats.redis_cooldowns as redis_cooldowns
from nonebot_plugin_moellmchats.redis_cooldowns import (
    RedisCooldownConflictError,
    RedisCooldownSettings,
    RedisCooldownStore,
    RedisCooldownUnavailableError,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, MessageEvent

_TOKEN_A = "a" * 32
_TOKEN_B = "b" * 32


class MatcherFinished(RuntimeError):
    pass


class FakeMatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def finish(self, message: str) -> None:
        self.messages.append(str(message))
        raise MatcherFinished


class FalseyRedisCooldownStore(RedisCooldownStore):
    def __bool__(self) -> bool:
        return False


def _settings(**changes: Any) -> RedisCooldownSettings:
    values: dict[str, Any] = {
        "key_prefix": "moellm-test",
        "max_cooldown_seconds": 3_600,
        "operation_retries": 8,
    }
    values.update(changes)
    return RedisCooldownSettings(**values)


def _key(user_id: int | str = 42, *, prefix: str = "moellm-test") -> str:
    fingerprint = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()
    return f"{prefix}:cd:{{{fingerprint}}}"


def _event(*, user_id: int = 42, timestamp: int = 1_000) -> SimpleNamespace:
    return SimpleNamespace(
        time=timestamp,
        sender=SimpleNamespace(
            user_id=user_id,
            card="测试用户",
            nickname="测试用户",
        ),
    )


def _typed_bot() -> Bot:
    return cast("Bot", object())


def _typed_event(*, user_id: int = 42, timestamp: int = 1_000) -> MessageEvent:
    return cast("MessageEvent", _event(user_id=user_id, timestamp=timestamp))


def _config(monkeypatch: pytest.MonkeyPatch, *, cooldown: int, timeout: float) -> None:
    values = {"cd_seconds": cooldown, "request_timeout_seconds": timeout}
    monkeypatch.setattr(
        chat_runtime.config_parser,
        "get_config",
        lambda key, default=None: values.get(key, default),
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


def test_redis_cooldown_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        key_prefix="tenant.alpha",
        max_cooldown_seconds=720,
        operation_retries=7,
    )

    assert settings.max_cooldown_milliseconds == 720_000
    assert settings.safe_diagnostics() == {
        "key_prefix": "tenant.alpha",
        "max_cooldown_seconds": 720,
        "operation_retries": 7,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key_prefix", ""),
        ("key_prefix", "1invalid"),
        ("key_prefix", "bad prefix"),
        ("key_prefix", "x" * 97),
        ("max_cooldown_seconds", 0),
        ("max_cooldown_seconds", 86_401),
        ("max_cooldown_seconds", True),
        ("operation_retries", 0),
        ("operation_retries", 65),
        ("operation_retries", nan),
    ],
)
def test_redis_cooldown_settings_reject_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


def test_redis_cooldown_store_requires_explicit_typed_dependencies(
    redis_client: FakeRedis,
) -> None:
    with pytest.raises(TypeError, match="redis-py"):
        RedisCooldownStore(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RedisCooldownSettings"):
        RedisCooldownStore(redis_client, settings=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="token_factory"):
        RedisCooldownStore(redis_client, token_factory=object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_claim_uses_hashed_per_user_key_set_nx_and_ttl(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(key_prefix="tenant.alpha"),
        token_factory=lambda: _TOKEN_A,
    )

    claim = await store.claim(
        user_id=42,
        event_time=1_000,
        cooldown_seconds=120,
    )
    key = _key(42, prefix="tenant.alpha")
    keys = await redis_client.keys("*")

    assert claim.acquired is True
    assert claim.lease == CooldownLease(
        user_id="42",
        token=_TOKEN_A,
        claimed_at=1_000,
    )
    assert keys == [key.encode()]
    assert b"42" not in keys[0]
    assert await redis_client.get(key) == _TOKEN_A.encode()
    assert 0 < await redis_client.pttl(key) <= 120_000
    assert store.safe_diagnostics()["backend"] == "redis"
    assert "FakeRedis" not in repr(store)
    assert "localhost" not in repr(store)


@pytest.mark.asyncio
async def test_existing_claim_returns_rounded_up_server_ttl(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )
    first = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    assert first.lease is not None
    await redis_client.pexpire(_key(), 1_501)

    denied = await store.claim(user_id="42", event_time=99_999, cooldown_seconds=120)

    assert denied.acquired is False
    assert denied.lease is None
    assert denied.retry_after_seconds == 2
    assert await redis_client.get(_key()) == _TOKEN_A.encode()


@pytest.mark.asyncio
async def test_zero_cooldown_short_circuits_without_redis_state(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(redis_client, settings=_settings())

    claim = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=0)

    assert claim.acquired is True
    assert claim.lease is None
    assert await redis_client.keys("*") == []


@pytest.mark.asyncio
async def test_user_identity_scope_matches_legacy_user_id_only_semantics(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )

    first = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    same_identity = await store.claim(
        user_id="42",
        event_time=1_001,
        cooldown_seconds=120,
    )
    other_user = await store.claim(
        user_id=43,
        event_time=1_001,
        cooldown_seconds=120,
    )

    assert first.acquired is True
    assert same_identity.acquired is False
    assert other_user.acquired is True
    assert len(await redis_client.keys("*")) == 2


@pytest.mark.asyncio
async def test_concurrent_claims_have_exactly_one_winner(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
    )

    claims = await asyncio.gather(
        *(
            store.claim(
                user_id=42,
                event_time=1_000,
                cooldown_seconds=120,
            )
            for _index in range(32)
        )
    )

    assert sum(claim.acquired for claim in claims) == 1
    assert all(claim.retry_after_seconds in {0, 120} for claim in claims)


@pytest.mark.asyncio
async def test_release_is_owner_bound_and_idempotent(redis_client: FakeRedis) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )
    claim = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    assert claim.lease is not None

    assert await store.release(claim.lease) is True
    assert await redis_client.get(_key()) is None
    assert await store.release(claim.lease) is False


@pytest.mark.asyncio
async def test_stale_release_never_deletes_a_replacement_claim(
    redis_client: FakeRedis,
) -> None:
    tokens = iter((_TOKEN_A, _TOKEN_B))
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: next(tokens),
    )
    first = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    assert first.lease is not None
    await redis_client.delete(_key())
    second = await store.claim(user_id=42, event_time=1_121, cooldown_seconds=120)
    assert second.lease is not None

    assert await store.release(first.lease) is False
    assert await redis_client.get(_key()) == _TOKEN_B.encode()
    assert await store.release(second.lease) is True


@pytest.mark.asyncio
async def test_claim_fails_closed_for_missing_or_excessive_ttl(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(max_cooldown_seconds=2),
        token_factory=lambda: _TOKEN_A,
    )
    await redis_client.set(_key(), _TOKEN_B)
    with pytest.raises(CooldownError, match="缺少 TTL"):
        await store.claim(user_id=42, event_time=1_000, cooldown_seconds=2)

    await redis_client.pexpire(_key(), 5_000)
    with pytest.raises(CooldownError, match="超过安全上限"):
        await store.claim(user_id=42, event_time=1_000, cooldown_seconds=2)


@pytest.mark.asyncio
async def test_release_rejects_corrupt_redis_token(redis_client: FakeRedis) -> None:
    store = RedisCooldownStore(redis_client, settings=_settings())
    await redis_client.set(_key(), b"not-a-valid-token", px=120_000)
    lease = CooldownLease(user_id="42", token=_TOKEN_A, claimed_at=1_000)

    with pytest.raises(CooldownError, match="token 已损坏"):
        await store.release(lease)

    assert await redis_client.get(_key()) == b"not-a-valid-token"


@pytest.mark.asyncio
async def test_claim_rejects_invalid_inputs_without_creating_keys(
    redis_client: FakeRedis,
) -> None:
    store = RedisCooldownStore(redis_client, settings=_settings(max_cooldown_seconds=120))
    invalid_calls = [
        {"user_id": True, "event_time": 1_000, "cooldown_seconds": 120},
        {"user_id": "bad\nuser", "event_time": 1_000, "cooldown_seconds": 120},
        {"user_id": 42, "event_time": inf, "cooldown_seconds": 120},
        {"user_id": 42, "event_time": 1_000, "cooldown_seconds": True},
        {"user_id": 42, "event_time": 1_000, "cooldown_seconds": 121},
    ]

    for values in invalid_calls:
        with pytest.raises(CooldownError):
            await store.claim(**values)  # type: ignore[arg-type]

    assert await redis_client.keys("*") == []


@pytest.mark.asyncio
async def test_only_explicit_expiry_race_is_retried(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(operation_retries=3),
        token_factory=lambda: _TOKEN_A,
    )
    set_calls = 0

    async def always_contended(*_args: Any, **_kwargs: Any) -> None:
        nonlocal set_calls
        set_calls += 1
        return None

    async def always_expired(*_args: Any, **_kwargs: Any) -> int:
        return -2

    monkeypatch.setattr(redis_client, "set", always_contended)
    monkeypatch.setattr(redis_client, "pttl", always_expired)

    with pytest.raises(RedisCooldownConflictError, match="并发冲突过多"):
        await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)

    assert set_calls == 3


@pytest.mark.asyncio
async def test_backend_errors_are_sanitized_and_never_chained(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )

    async def fail_with_secret(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("redis://user:top-secret@internal-host/0")

    monkeypatch.setattr(redis_client, "set", fail_with_secret)

    with pytest.raises(RedisCooldownUnavailableError) as captured:
        await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)

    assert "RuntimeError" in str(captured.value)
    assert "top-secret" not in str(captured.value)
    assert "internal-host" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_unknown_claim_result_never_returns_a_lease(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )
    original_set = redis_client.set

    async def commit_then_lose_response(*args: Any, **kwargs: Any) -> None:
        await original_set(*args, **kwargs)
        raise RuntimeError("redis://user:top-secret@internal-host/0")

    monkeypatch.setattr(redis_client, "set", commit_then_lose_response)

    with pytest.raises(RedisCooldownUnavailableError) as captured:
        await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)

    assert captured.value.__cause__ is None
    assert "top-secret" not in str(captured.value)
    assert await redis_client.get(_key()) == _TOKEN_A.encode()


@pytest.mark.asyncio
async def test_watch_error_is_the_only_retryable_release_result(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )
    claim = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    assert claim.lease is not None
    original_execute = Pipeline.execute
    attempts = 0

    async def conflict_once(
        self: Pipeline,
        raise_on_error: bool = True,
    ) -> list[Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WatchError("simulated conflict")
        return await original_execute(self, raise_on_error=raise_on_error)

    monkeypatch.setattr(Pipeline, "execute", conflict_once)

    assert await store.release(claim.lease) is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_unknown_release_result_is_fail_closed(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )
    claim = await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)
    assert claim.lease is not None
    original_execute = Pipeline.execute

    async def commit_then_lose_response(
        self: Pipeline,
        raise_on_error: bool = True,
    ) -> list[Any]:
        await original_execute(self, raise_on_error=raise_on_error)
        raise RuntimeError("redis://user:top-secret@internal-host/0")

    monkeypatch.setattr(Pipeline, "execute", commit_then_lose_response)

    with pytest.raises(RedisCooldownUnavailableError) as captured:
        await store.release(claim.lease)

    assert captured.value.__cause__ is None
    assert "top-secret" not in str(captured.value)
    assert await redis_client.get(_key()) is None


@pytest.mark.asyncio
async def test_cancellation_is_never_translated(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )

    async def cancelled(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(redis_client, "set", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await store.claim(user_id=42, event_time=1_000, cooldown_seconds=120)


@pytest.mark.asyncio
async def test_handle_llm_accepts_explicit_falsey_redis_store_and_releases_claim(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=180)
    store = FalseyRedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )

    class Controller:
        @asynccontextmanager
        async def slot(self, _key: object):
            raise AdmissionRejected("full")
            yield

    monkeypatch.setattr(chat_runtime, "get_llm_controller", lambda: Controller())
    matcher = FakeMatcher()

    with pytest.raises(MatcherFinished):
        await chat_runtime.handle_llm(
            _typed_bot(),
            _typed_event(),
            matcher,
            {},
            cooldown_store=store,
        )

    assert matcher.messages == ["当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。"]
    assert await redis_client.get(_key()) is None
    assert chat_runtime.cd.get(42) is None


@pytest.mark.asyncio
async def test_explicit_redis_failure_blocks_before_admission_without_memory_fallback(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=180)
    store = RedisCooldownStore(
        redis_client,
        settings=_settings(),
        token_factory=lambda: _TOKEN_A,
    )
    entered = False

    class Controller:
        @asynccontextmanager
        async def slot(self, _key: object):
            nonlocal entered
            entered = True
            yield

    async def fail_with_secret(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("redis://user:top-secret@internal-host/0")

    monkeypatch.setattr(redis_client, "set", fail_with_secret)
    monkeypatch.setattr(chat_runtime, "get_llm_controller", lambda: Controller())

    with pytest.raises(RedisCooldownUnavailableError) as captured:
        await chat_runtime.handle_llm(
            _typed_bot(),
            _typed_event(),
            FakeMatcher(),
            {},
            cooldown_store=store,
        )

    assert entered is False
    assert "top-secret" not in str(captured.value)
    assert chat_runtime.cd.get(42) is None


def test_redis_cooldown_module_has_no_global_client_store_or_runtime_wiring() -> None:
    values = tuple(vars(redis_cooldowns).values())

    assert not any(isinstance(value, RedisCooldownStore) for value in values)
    assert not any(isinstance(value, FakeRedis) for value in values)
    assert chat_runtime.default_cooldown_store.safe_diagnostics()["backend"] == "memory"

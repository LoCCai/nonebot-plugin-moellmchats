from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
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
from nonebot_plugin_moellmchats.admission_store import (
    AdmissionActivationStatus,
    AdmissionLease,
    AdmissionLeaseLostError,
    AdmissionReservation,
    AdmissionSnapshot,
    AdmissionStoreError,
    AdmissionStoreProtocol,
)
import nonebot_plugin_moellmchats.redis_admission as redis_admission
from nonebot_plugin_moellmchats.redis_admission import (
    RedisAdmissionConflictError,
    RedisAdmissionController,
    RedisAdmissionSettings,
    RedisAdmissionStateError,
    RedisAdmissionStore,
    RedisAdmissionUnavailableError,
)

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot, MessageEvent


class MatcherFinished(RuntimeError):
    pass


class FakeMatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def finish(self, message: str) -> None:
        self.messages.append(str(message))
        raise MatcherFinished


class LeaseIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.value:032x}"


class FalseyRedisAdmissionController(RedisAdmissionController):
    def __bool__(self) -> bool:
        return False


def _settings(**changes: Any) -> RedisAdmissionSettings:
    values: dict[str, Any] = {
        "key_prefix": "moellm-test",
        "name": "llm",
        "max_active": 2,
        "max_pending": 4,
        "max_per_key": 2,
        "pending_lease_seconds": 1.0,
        "active_lease_seconds": 1.0,
        "poll_interval_seconds": 0.01,
        "heartbeat_interval_seconds": 0.1,
        "transaction_retries": 32,
        "max_state_bytes": 65_536,
    }
    values.update(changes)
    return RedisAdmissionSettings(**values)


def _state_key(*, prefix: str = "moellm-test", name: str = "llm") -> str:
    return f"{prefix}:{{admission:{name}}}:state"


def _store(
    client: FakeRedis,
    *,
    settings: RedisAdmissionSettings | None = None,
    lease_ids: Callable[[], str] | None = None,
) -> RedisAdmissionStore:
    return RedisAdmissionStore(
        client,
        settings=_settings() if settings is None else settings,
        lease_id_factory=LeaseIds() if lease_ids is None else lease_ids,
    )


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


async def _wait_for_snapshot(
    store: RedisAdmissionStore,
    expected: AdmissionSnapshot,
    *,
    timeout: float = 1.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        snapshot = await store.snapshot()
        if snapshot == expected:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"snapshot 未收敛: {snapshot!r} != {expected!r}")
        await asyncio.sleep(0.01)


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


def test_redis_admission_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        key_prefix="tenant.alpha",
        name="dispatch",
        max_active=3,
        max_pending=7,
        max_per_key=None,
        pending_lease_seconds=9.0,
        active_lease_seconds=12.0,
        poll_interval_seconds=0.5,
        heartbeat_interval_seconds=2.0,
        transaction_retries=7,
        max_state_bytes=99_999,
    )

    assert settings.max_records == 10
    assert settings.pending_lease_milliseconds == 9_000
    assert settings.active_lease_milliseconds == 12_000
    assert settings.maximum_key_ttl_milliseconds == 12_000
    assert settings.safe_diagnostics() == {
        "key_prefix": "tenant.alpha",
        "name": "dispatch",
        "max_active": 3,
        "max_pending": 7,
        "max_per_key": None,
        "pending_lease_seconds": 9.0,
        "active_lease_seconds": 12.0,
        "poll_interval_seconds": 0.5,
        "heartbeat_interval_seconds": 2.0,
        "transaction_retries": 7,
        "max_state_bytes": 99_999,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key_prefix", ""),
        ("key_prefix", "1invalid"),
        ("key_prefix", "bad prefix"),
        ("key_prefix", "x" * 97),
        ("name", ""),
        ("name", "bad name"),
        ("name", "x" * 64),
        ("max_active", 0),
        ("max_active", 1_001),
        ("max_active", True),
        ("max_pending", 0),
        ("max_pending", 10_001),
        ("max_per_key", 0),
        ("max_per_key", False),
        ("pending_lease_seconds", 0.5),
        ("pending_lease_seconds", nan),
        ("active_lease_seconds", 86_401),
        ("active_lease_seconds", inf),
        ("poll_interval_seconds", 0),
        ("poll_interval_seconds", 0.34),
        ("heartbeat_interval_seconds", 0.09),
        ("heartbeat_interval_seconds", 0.34),
        ("transaction_retries", 0),
        ("transaction_retries", 65),
        ("max_state_bytes", 4_095),
        ("max_state_bytes", 8_388_609),
    ],
)
def test_redis_admission_settings_reject_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


def test_admission_value_objects_reject_ambiguous_types() -> None:
    with pytest.raises(ValueError, match="active"):
        AdmissionSnapshot(active=True, pending=0)
    with pytest.raises(ValueError, match="namespace_fingerprint"):
        AdmissionLease("x", "a" * 32, None)
    with pytest.raises(ValueError, match="lease_id"):
        AdmissionLease("a" * 64, "A" * 32, None)
    with pytest.raises(ValueError, match="key_fingerprint"):
        AdmissionLease("a" * 64, "b" * 32, "raw-user-id")


@pytest.mark.asyncio
async def test_store_requires_explicit_typed_dependencies(
    redis_client: FakeRedis,
) -> None:
    with pytest.raises(TypeError, match="redis-py"):
        RedisAdmissionStore(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RedisAdmissionSettings"):
        RedisAdmissionStore(redis_client, settings=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="lease_id_factory"):
        RedisAdmissionStore(redis_client, lease_id_factory=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RedisAdmissionStore"):
        RedisAdmissionController(object())  # type: ignore[arg-type]

    store = _store(redis_client)
    assert isinstance(store, AdmissionStoreProtocol)


@pytest.mark.asyncio
async def test_reserve_activate_release_uses_one_hashed_ttl_state_key(
    redis_client: FakeRedis,
) -> None:
    store = _store(redis_client)

    reservation = await store.reserve(42)
    assert reservation.snapshot == AdmissionSnapshot(active=0, pending=1)
    activation = await store.try_activate(reservation.lease)
    assert activation.status is AdmissionActivationStatus.ACTIVATED
    assert activation.snapshot == AdmissionSnapshot(active=1, pending=0)

    keys = await redis_client.keys("*")
    assert keys == [_state_key().encode()]
    raw_state = await redis_client.get(_state_key())
    assert isinstance(raw_state, bytes)
    assert b'"42"' not in raw_state
    assert 0 < await redis_client.pttl(_state_key()) <= 1_000
    assert store.safe_diagnostics()["backend"] == "redis"
    assert "FakeRedis" not in repr(store)
    assert "localhost" not in repr(store)

    released = await store.release(reservation.lease)
    assert released.released is True
    assert released.snapshot == AdmissionSnapshot(active=0, pending=0)
    assert await redis_client.get(_state_key()) is None


@pytest.mark.parametrize(
    "key",
    [True, 1.5, "", "bad\nkey", "x" * 513, 9_223_372_036_854_775_808],
)
@pytest.mark.asyncio
async def test_store_rejects_unsafe_or_ambiguous_keys_without_redis_state(
    redis_client: FakeRedis,
    key: object,
) -> None:
    store = _store(redis_client)

    with pytest.raises(AdmissionStoreError):
        await store.reserve(key)  # type: ignore[arg-type]

    assert await redis_client.keys("*") == []


@pytest.mark.asyncio
async def test_integer_and_string_identities_have_distinct_active_scopes(
    redis_client: FakeRedis,
) -> None:
    store = _store(redis_client)
    integer = await store.reserve(42)
    text = await store.reserve("42")

    assert integer.lease.key_fingerprint != text.lease.key_fingerprint
    assert (await store.try_activate(integer.lease)).status is AdmissionActivationStatus.ACTIVATED
    assert (await store.try_activate(text.lease)).status is AdmissionActivationStatus.ACTIVATED
    assert await store.snapshot() == AdmissionSnapshot(active=2, pending=0)

    await store.release(integer.lease)
    await store.release(text.lease)


@pytest.mark.asyncio
async def test_none_identity_preserves_unkeyed_local_semantics(
    redis_client: FakeRedis,
) -> None:
    store = _store(redis_client, settings=_settings(max_per_key=1))
    first = await store.reserve()
    second = await store.reserve()

    assert (await store.try_activate(first.lease)).status is AdmissionActivationStatus.ACTIVATED
    assert (await store.try_activate(second.lease)).status is AdmissionActivationStatus.ACTIVATED

    await store.release(first.lease)
    await store.release(second.lease)


@pytest.mark.asyncio
async def test_capacity_and_per_key_limits_count_active_plus_pending(
    redis_client: FakeRedis,
) -> None:
    store = _store(
        redis_client,
        settings=_settings(max_active=1, max_pending=2, max_per_key=2),
    )
    active = await store.reserve(1)
    await store.try_activate(active.lease)
    same_key_pending = await store.reserve(1)

    with pytest.raises(AdmissionRejected, match="per-user"):
        await store.reserve(1)

    other_pending = await store.reserve(2)
    with pytest.raises(AdmissionRejected, match="queue is full"):
        await store.reserve(3)

    assert await store.snapshot() == AdmissionSnapshot(active=1, pending=2)
    await store.release(active.lease)
    await store.release(same_key_pending.lease)
    await store.release(other_pending.lease)


@pytest.mark.asyncio
async def test_earliest_eligible_pending_does_not_block_other_identity(
    redis_client: FakeRedis,
) -> None:
    store = _store(redis_client)
    active = await store.reserve(1)
    await store.try_activate(active.lease)
    blocked_earlier = await store.reserve(1)
    eligible_later = await store.reserve(2)

    assert (await store.try_activate(blocked_earlier.lease)).status is AdmissionActivationStatus.WAITING
    assert (await store.try_activate(eligible_later.lease)).status is AdmissionActivationStatus.ACTIVATED
    assert await store.snapshot() == AdmissionSnapshot(active=2, pending=1)

    await store.release(active.lease)
    assert (await store.try_activate(blocked_earlier.lease)).status is AdmissionActivationStatus.ACTIVATED
    await store.release(blocked_earlier.lease)
    await store.release(eligible_later.lease)


@pytest.mark.asyncio
async def test_concurrent_reservations_never_exceed_pending_capacity(
    redis_client: FakeRedis,
) -> None:
    store = _store(
        redis_client,
        settings=_settings(max_active=1, max_pending=5, max_per_key=None),
    )

    results = await asyncio.gather(
        *(store.reserve(index) for index in range(20)),
        return_exceptions=True,
    )
    reservations = [result for result in results if isinstance(result, AdmissionReservation)]
    rejections = [result for result in results if isinstance(result, AdmissionRejected)]

    assert len(reservations) == 5
    assert len(rejections) == 15
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=5)
    await asyncio.gather(*(store.release(item.lease) for item in reservations))


@pytest.mark.asyncio
async def test_two_controllers_share_global_active_and_pending_limits(
    redis_client: FakeRedis,
) -> None:
    settings = _settings(max_active=2, max_pending=4, max_per_key=2)
    store_a = _store(redis_client, settings=settings)
    store_b = _store(redis_client, settings=settings)
    controller_a = RedisAdmissionController(store_a)
    controller_b = RedisAdmissionController(store_b)
    release = asyncio.Event()
    two_entered = asyncio.Event()
    active = 0
    peak_active = 0

    async def hold(controller: RedisAdmissionController, key: int) -> None:
        nonlocal active, peak_active
        async with controller.slot(key):
            active += 1
            peak_active = max(peak_active, active)
            if active == 2:
                two_entered.set()
            await release.wait()
            active -= 1

    tasks = [
        asyncio.create_task(hold(controller_a, 1)),
        asyncio.create_task(hold(controller_b, 2)),
        asyncio.create_task(hold(controller_a, 3)),
        asyncio.create_task(hold(controller_b, 4)),
    ]
    await asyncio.wait_for(two_entered.wait(), 1)
    await _wait_for_snapshot(store_a, AdmissionSnapshot(active=2, pending=2))
    assert peak_active == 2

    release.set()
    await asyncio.gather(*tasks)
    assert peak_active == 2
    assert await store_a.snapshot() == AdmissionSnapshot(active=0, pending=0)


@pytest.mark.asyncio
async def test_cancelled_pending_controller_slot_releases_reservation(
    redis_client: FakeRedis,
) -> None:
    settings = _settings(max_active=1, max_pending=2)
    store = _store(redis_client, settings=settings)
    controller = RedisAdmissionController(store)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold() -> None:
        async with controller.slot(1):
            entered.set()
            await release.wait()

    active = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), 1)
    pending = asyncio.create_task(controller.slot(2).__aenter__())
    await _wait_for_snapshot(store, AdmissionSnapshot(active=1, pending=1))

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    await _wait_for_snapshot(store, AdmissionSnapshot(active=1, pending=0))
    release.set()
    await active
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=0)


@pytest.mark.asyncio
async def test_pending_and_active_ttl_recover_abandoned_capacity(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(redis_client, settings=_settings(max_active=1, max_pending=1))
    now_ms = 10_000

    async def server_now_ms() -> int:
        return now_ms

    monkeypatch.setattr(store, "_server_now_ms", server_now_ms)
    pending = await store.reserve(1)
    now_ms += 1_001
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=0)
    assert (await store.release(pending.lease)).released is False

    active = await store.reserve(1)
    await store.try_activate(active.lease)
    now_ms += 1_001
    replacement = await store.reserve(1)
    assert (await store.release(active.lease)).released is False
    assert (await store.try_activate(replacement.lease)).status is AdmissionActivationStatus.ACTIVATED
    await store.release(replacement.lease)


@pytest.mark.asyncio
async def test_controller_heartbeat_keeps_active_lease_alive(
    redis_client: FakeRedis,
) -> None:
    store = _store(
        redis_client,
        settings=_settings(max_active=1, max_pending=1, heartbeat_interval_seconds=0.1),
    )
    controller = RedisAdmissionController(store)

    async with controller.slot(1):
        await asyncio.sleep(1.15)
        assert await store.snapshot() == AdmissionSnapshot(active=1, pending=0)

    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=0)


@pytest.mark.asyncio
async def test_controller_polling_renews_pending_lease_until_capacity_opens(
    redis_client: FakeRedis,
) -> None:
    store = _store(
        redis_client,
        settings=_settings(max_active=1, max_pending=1),
    )
    controller = RedisAdmissionController(store)
    release = asyncio.Event()
    active_entered = asyncio.Event()
    pending_entered = asyncio.Event()

    async def hold_active() -> None:
        async with controller.slot(1):
            active_entered.set()
            await release.wait()

    async def wait_pending() -> None:
        async with controller.slot(2):
            pending_entered.set()

    active = asyncio.create_task(hold_active())
    await asyncio.wait_for(active_entered.wait(), 1)
    pending = asyncio.create_task(wait_pending())
    await _wait_for_snapshot(store, AdmissionSnapshot(active=1, pending=1))
    await asyncio.sleep(1.15)
    assert await store.snapshot() == AdmissionSnapshot(active=1, pending=1)

    release.set()
    await asyncio.wait_for(pending_entered.wait(), 1)
    await asyncio.gather(active, pending)
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=0)


@pytest.mark.asyncio
async def test_lost_active_lease_aborts_controller_owner(
    redis_client: FakeRedis,
) -> None:
    store = _store(redis_client, settings=_settings(max_active=1, max_pending=1))
    controller = RedisAdmissionController(store)
    entered = asyncio.Event()

    async def work() -> None:
        async with controller.slot(1):
            entered.set()
            await asyncio.sleep(5)

    task = asyncio.create_task(work())
    await asyncio.wait_for(entered.wait(), 1)
    await redis_client.delete(_state_key())

    with pytest.raises(AdmissionLeaseLostError, match="active lease"):
        await asyncio.wait_for(task, 1)
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=0)


@pytest.mark.asyncio
async def test_foreign_namespace_and_old_lease_cannot_release_current_owner(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_ids = LeaseIds()
    store = _store(redis_client, lease_ids=lease_ids)
    foreign = _store(redis_client, settings=_settings(name="dispatch"))
    now_ms = 20_000

    async def server_now_ms() -> int:
        return now_ms

    monkeypatch.setattr(store, "_server_now_ms", server_now_ms)
    old = await store.reserve(7)
    await store.try_activate(old.lease)
    with pytest.raises(AdmissionStoreError, match="namespace"):
        await foreign.release(old.lease)

    now_ms += 1_001
    current = await store.reserve(7)
    assert current.lease.lease_id != old.lease.lease_id
    assert (await store.release(old.lease)).released is False
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=1)
    await store.release(current.lease)


@pytest.mark.asyncio
async def test_state_schema_version_requires_an_exact_integer(
    redis_client: FakeRedis,
) -> None:
    payload = {"schema_version": True, "next_sequence": 1, "leases": []}
    await redis_client.set(_state_key(), json.dumps(payload), px=1_000)
    store = _store(redis_client)

    with pytest.raises(RedisAdmissionStateError, match="schema version"):
        await store.snapshot()


@pytest.mark.asyncio
async def test_sequence_exhaustion_sentinel_is_decodable_but_fails_closed(
    redis_client: FakeRedis,
) -> None:
    seconds, microseconds = await redis_client.time()
    now_ms = seconds * 1_000 + microseconds // 1_000
    maximum_sequence = 9_223_372_036_854_775_807
    payload = {
        "schema_version": 1,
        "next_sequence": maximum_sequence,
        "leases": [
            {
                "lease_id": "a" * 32,
                "key_fingerprint": None,
                "state": "pending",
                "sequence": maximum_sequence - 1,
                "created_ms": now_ms,
                "updated_ms": now_ms,
                "expires_ms": now_ms + 1_000,
            }
        ],
    }
    await redis_client.set(_state_key(), json.dumps(payload), px=1_000)
    store = _store(redis_client)

    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=1)
    with pytest.raises(AdmissionStoreError, match="sequence 已耗尽"):
        await store.reserve(1)


@pytest.mark.asyncio
async def test_corrupt_oversized_or_nonexpiring_state_fails_closed(
    redis_client: FakeRedis,
) -> None:
    settings = _settings(max_state_bytes=4_096)
    store = _store(redis_client, settings=settings)

    await redis_client.set(_state_key(), b"x" * 4_097, px=1_000)
    with pytest.raises(RedisAdmissionStateError, match="大小限制"):
        await store.snapshot()

    await redis_client.set(
        _state_key(),
        json.dumps({"schema_version": 1, "next_sequence": 1, "leases": []}),
    )
    with pytest.raises(RedisAdmissionStateError, match="缺少 TTL"):
        await store.snapshot()


@pytest.mark.asyncio
async def test_watch_error_is_the_only_retryable_transaction_result(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(redis_client)
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
    reservation = await store.reserve(1)

    assert attempts == 2
    await store.release(reservation.lease)


@pytest.mark.asyncio
async def test_watch_retry_budget_is_bounded(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(redis_client, settings=_settings(transaction_retries=2))
    attempts = 0

    async def always_conflict(
        _self: Pipeline,
        raise_on_error: bool = True,
    ) -> list[Any]:
        del raise_on_error
        nonlocal attempts
        attempts += 1
        raise WatchError("simulated conflict")

    monkeypatch.setattr(Pipeline, "execute", always_conflict)

    with pytest.raises(RedisAdmissionConflictError):
        await store.reserve(1)
    assert attempts == 2
    assert await redis_client.get(_state_key()) is None


@pytest.mark.asyncio
async def test_committed_reserve_with_lost_exec_response_never_returns_lease(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(redis_client)
    original_execute = Pipeline.execute

    async def commit_then_lose_response(
        self: Pipeline,
        raise_on_error: bool = True,
    ) -> list[Any]:
        await original_execute(self, raise_on_error=raise_on_error)
        raise RuntimeError("redis://admin:top-secret@cache.internal/0")

    with monkeypatch.context() as patcher:
        patcher.setattr(Pipeline, "execute", commit_then_lose_response)
        with pytest.raises(RedisAdmissionUnavailableError) as captured:
            await store.reserve(1)

    assert "top-secret" not in str(captured.value)
    assert "cache.internal" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=1)


@pytest.mark.asyncio
async def test_committed_release_with_lost_exec_response_is_fail_closed(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(redis_client)
    reservation = await store.reserve(1)
    original_execute = Pipeline.execute

    async def commit_then_lose_response(
        self: Pipeline,
        raise_on_error: bool = True,
    ) -> list[Any]:
        await original_execute(self, raise_on_error=raise_on_error)
        raise RuntimeError("redis://admin:top-secret@cache.internal/0")

    with monkeypatch.context() as patcher:
        patcher.setattr(Pipeline, "execute", commit_then_lose_response)
        with pytest.raises(RedisAdmissionUnavailableError) as captured:
            await store.release(reservation.lease)

    assert "top-secret" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=0)


@pytest.mark.asyncio
async def test_backend_errors_are_sanitized_and_never_fallback(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "redis://admin:top-secret@cache.internal:6380/9"

    async def broken_time() -> tuple[int, int]:
        raise RuntimeError(f"connection failed for {secret}")

    monkeypatch.setattr(redis_client, "time", broken_time)
    store = _store(redis_client)

    with pytest.raises(RedisAdmissionUnavailableError) as captured:
        await store.snapshot()

    assert "RuntimeError" in str(captured.value)
    assert "top-secret" not in str(captured.value)
    assert "cache.internal" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize("response", [None, [1], [True, 0], [-1, 0], [1, 1_000_000]])
@pytest.mark.asyncio
async def test_invalid_redis_time_response_fails_closed(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    async def invalid_time() -> object:
        return response

    monkeypatch.setattr(redis_client, "time", invalid_time)
    store = _store(redis_client)

    with pytest.raises(RedisAdmissionUnavailableError, match="TIME"):
        await store.snapshot()


@pytest.mark.asyncio
async def test_cancellation_is_never_translated(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(redis_client, "time", cancelled)
    store = _store(redis_client)

    with pytest.raises(asyncio.CancelledError):
        await store.snapshot()


@pytest.mark.asyncio
async def test_controller_rejects_nonportable_hashable_key_without_redis_access(
    redis_client: FakeRedis,
) -> None:
    controller = RedisAdmissionController(_store(redis_client))

    with pytest.raises(AdmissionStoreError, match="整数、字符串或 None"):
        async with controller.slot((1, 2)):
            pass

    assert await redis_client.keys("*") == []


@pytest.mark.asyncio
async def test_handle_llm_accepts_explicit_falsey_redis_controller(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=180)
    store = _store(
        redis_client,
        settings=_settings(max_active=1, max_pending=1, max_per_key=1),
    )
    occupied = await store.reserve(99)
    controller = FalseyRedisAdmissionController(store)

    def unexpected_default() -> None:
        raise AssertionError("显式 falsey admission backend 不得回退默认 controller")

    monkeypatch.setattr(chat_runtime, "get_llm_controller", unexpected_default)
    matcher = FakeMatcher()

    with pytest.raises(MatcherFinished):
        await chat_runtime.handle_llm(
            _typed_bot(),
            _typed_event(),
            matcher,
            {},
            admission_controller=controller,
        )

    assert matcher.messages == ["当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。"]
    assert chat_runtime.cd[42] == 0
    assert await store.snapshot() == AdmissionSnapshot(active=0, pending=1)
    await store.release(occupied.lease)


@pytest.mark.asyncio
async def test_explicit_redis_failure_blocks_without_memory_admission_fallback(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=180)
    store = _store(redis_client)
    controller = RedisAdmissionController(store)

    async def broken_time() -> tuple[int, int]:
        raise RuntimeError("redis://admin:top-secret@cache.internal/0")

    def unexpected_default() -> None:
        raise AssertionError("Redis admission 失败不得回退默认 controller")

    monkeypatch.setattr(redis_client, "time", broken_time)
    monkeypatch.setattr(chat_runtime, "get_llm_controller", unexpected_default)
    matcher = FakeMatcher()

    with pytest.raises(MatcherFinished):
        await chat_runtime.handle_llm(
            _typed_bot(),
            _typed_event(),
            matcher,
            {},
            admission_controller=controller,
        )

    assert matcher.messages == ["当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。"]
    assert chat_runtime.cd[42] == 0
    assert await redis_client.keys("*") == []


def test_redis_admission_module_has_no_global_client_store_or_runtime_wiring() -> None:
    values = tuple(vars(redis_admission).values())

    assert not any(isinstance(value, RedisAdmissionStore) for value in values)
    assert not any(isinstance(value, RedisAdmissionController) for value in values)
    assert not any(isinstance(value, FakeRedis) for value in values)

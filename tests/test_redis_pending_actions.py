from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import json
from math import inf, nan
from types import SimpleNamespace
from typing import Any

from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import WatchError

from nonebot_plugin_moellmchats.pending_actions import (
    PendingAction,
    PendingActionError,
    execute_pending_action,
)
from nonebot_plugin_moellmchats.redis_client import RedisClientManager, RedisClientSettings
import nonebot_plugin_moellmchats.redis_pending_actions as redis_pending_actions
from nonebot_plugin_moellmchats.redis_pending_actions import (
    RedisPendingActionSettings,
    RedisPendingActionStore,
    RedisPendingActionUnavailableError,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot, immutable_mapping
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot


class FakeBot:
    def __init__(self, self_id: str = "10000", adapter_name: str = "fake") -> None:
        self.self_id = self_id
        self.adapter = SimpleNamespace(get_name=lambda: adapter_name)
        self.config = SimpleNamespace(superusers={"1"})


class FalseyRedisPendingActionStore(RedisPendingActionStore):
    def __bool__(self) -> bool:
        return False


def _event(user_id: int = 1, group_id: int | None = 10) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, group_id=group_id)


def _settings(**changes: Any) -> RedisPendingActionSettings:
    values: dict[str, Any] = {
        "key_prefix": "moellm-test",
        "ttl_seconds": 120,
        "max_entries": 16,
        "max_argument_bytes": 16_384,
        "failure_window_seconds": 60,
        "max_failures": 8,
        "max_failure_keys": 32,
        "transaction_retries": 32,
    }
    values.update(changes)
    return RedisPendingActionSettings(**values)


def _nonce_factory(*values: str) -> Callable[[], str]:
    nonces = iter(values)
    return lambda: next(nonces)


def _root(prefix: str = "moellm-test") -> str:
    return f"{prefix}:{{pending-action}}"


def _action_key(nonce: str, *, prefix: str = "moellm-test") -> str:
    return f"{_root(prefix)}:action:{nonce}"


async def _decoded_keys(client: FakeRedis, pattern: str = "*") -> set[str]:
    values = await client.keys(pattern)
    return {value.decode("utf-8") if isinstance(value, bytes) else str(value) for value in values}


def _runtime_snapshot(generation: int, custom_tools: dict[str, dict[str, Any]]) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config=immutable_mapping({}),
        model_state=None,
        temperaments=immutable_mapping({}),
        temperament_assignments=immutable_mapping({}),
        replies=immutable_mapping({}),
        tool_snapshot=ToolSnapshot(
            generation=generation,
            plugin_info=immutable_mapping({}),
            custom_tools=immutable_mapping(custom_tools),
            tool_dependencies=immutable_mapping({}),
            mcp_tool_names=frozenset(),
        ),
        emotions=(),
        reloaded_at=1.0,
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


def test_redis_pending_action_settings_are_bounded_and_safe() -> None:
    settings = _settings(
        key_prefix="tenant.alpha",
        ttl_seconds=90.5,
        max_entries=17,
        max_argument_bytes=2_048,
        failure_window_seconds=45.25,
        max_failures=6,
        max_failure_keys=19,
        transaction_retries=7,
    )

    assert settings.ttl_milliseconds == 90_500
    assert settings.failure_window_milliseconds == 45_250
    assert settings.safe_diagnostics() == {
        "key_prefix": "tenant.alpha",
        "ttl_seconds": 90.5,
        "max_entries": 17,
        "max_argument_bytes": 2_048,
        "failure_window_seconds": 45.25,
        "max_failures": 6,
        "max_failure_keys": 19,
        "transaction_retries": 7,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key_prefix", ""),
        ("key_prefix", "1invalid"),
        ("key_prefix", "bad prefix"),
        ("key_prefix", "x" * 97),
        ("ttl_seconds", 0),
        ("ttl_seconds", 3_601),
        ("ttl_seconds", inf),
        ("max_entries", 0),
        ("max_entries", 10_001),
        ("max_argument_bytes", 0),
        ("max_argument_bytes", 1_048_577),
        ("failure_window_seconds", nan),
        ("max_failures", True),
        ("max_failure_keys", 100_001),
        ("transaction_retries", 0),
        ("transaction_retries", 65),
    ],
)
def test_redis_pending_action_settings_reject_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _settings(**{field: value})


@pytest.mark.asyncio
async def test_redis_pending_action_keyspace_is_cluster_safe_and_repr_is_sanitized(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(key_prefix="tenant.alpha"),
        nonce_factory=lambda: "ABC123",
    )
    action = await store.create(
        bot=FakeBot(),
        event=_event(),
        tool_name="mutate",
        arguments={"value": 1},
        generation=3,
    )

    keys = await _decoded_keys(redis_client)
    assert action.nonce == "ABC123"
    assert keys
    assert all(key.startswith("tenant.alpha:{pending-action}:") for key in keys)
    assert all("{pending-action}" in key for key in keys)
    assert store.safe_diagnostics()["backend"] == "redis"
    assert "FakeRedis" not in repr(store)
    assert "localhost" not in repr(store)


@pytest.mark.asyncio
async def test_maximum_multibyte_identities_round_trip_within_record_bound(redis_client: FakeRedis) -> None:
    identity = "🙂" * 512
    tool_name = "界" * 256
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "ABC123")
    bot = FakeBot(self_id=identity, adapter_name=identity)
    event = SimpleNamespace(user_id=identity, group_id=identity)

    created = await store.create(
        bot=bot,
        event=event,
        tool_name=tool_name,
        arguments={"value": "fixed"},
        generation=1,
    )
    consumed = await store.consume("ABC123", bot=bot, event=event, generation=1)

    assert consumed == created
    assert consumed.tool_name == tool_name


def test_redis_pending_action_module_has_no_global_client_or_store() -> None:
    values = tuple(vars(redis_pending_actions).values())

    assert not any(isinstance(value, Redis) for value in values)
    assert not any(isinstance(value, RedisPendingActionStore) for value in values)
    assert FakeRedis.__module__.startswith("fakeredis")


@pytest.mark.asyncio
async def test_store_accepts_only_an_explicit_client_from_redis_manager() -> None:
    fake_server = FakeServer(version=(6, 2))
    manager = RedisClientManager(
        RedisClientSettings(redis_url="redis://user:secret@cache.invalid/0"),
        client_factory=lambda _url, **_options: FakeRedis(
            server=fake_server,
            decode_responses=False,
        ),
    )
    client = manager.get_client()
    store = RedisPendingActionStore(client, settings=_settings(), nonce_factory=lambda: "ABC123")

    action = await store.create(
        bot=FakeBot(),
        event=_event(),
        tool_name="mutate",
        arguments={},
        generation=1,
    )

    assert action.nonce == "ABC123"
    assert manager.initialized is True
    assert "secret" not in repr(store)
    assert await manager.aclose() is True


@pytest.mark.asyncio
async def test_create_canonicalizes_reuses_and_replaces_one_caller_tool_slot(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(),
        nonce_factory=_nonce_factory("111111", "222222"),
    )
    bot = FakeBot()
    event = _event()
    first = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"z": 1, "a": "fixed"},
        generation=7,
        bundle_digest="d" * 64,
    )
    duplicate = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"a": "fixed", "z": 1},
        generation=7,
        bundle_digest="d" * 64,
    )
    replacement = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"a": "changed", "z": 1},
        generation=7,
        bundle_digest="d" * 64,
    )

    assert first.arguments_json == '{"a":"fixed","z":1}'
    assert duplicate == first
    assert replacement.nonce == "222222"
    assert replacement.action_id != first.action_id
    assert await redis_client.get(_action_key("111111")) is None
    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("111111", bot=bot, event=event, generation=7)
    assert (await store.consume("222222", bot=bot, event=event, generation=7)).arguments() == {
        "a": "changed",
        "z": 1,
    }


@pytest.mark.asyncio
async def test_create_enforces_capacity_but_allows_atomic_slot_replacement(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(max_entries=2),
        nonce_factory=_nonce_factory("A00001", "B00002", "C00003"),
    )
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="first", arguments={"v": 1}, generation=1)
    await store.create(bot=bot, event=event, tool_name="second", arguments={}, generation=1)

    with pytest.raises(PendingActionError, match="队列已满"):
        await store.create(bot=bot, event=event, tool_name="third", arguments={}, generation=1)

    replacement = await store.create(bot=bot, event=event, tool_name="first", arguments={"v": 2}, generation=1)
    assert replacement.nonce == "C00003"
    assert await store.size() == 2


@pytest.mark.asyncio
async def test_replacement_never_reuses_the_previous_confirmation_code(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(),
        nonce_factory=lambda: "ABC123",
    )
    bot = FakeBot()
    event = _event()
    original = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": 1},
        generation=1,
    )

    with pytest.raises(PendingActionError, match="无法生成安全确认码"):
        await store.create(
            bot=bot,
            event=event,
            tool_name="mutate",
            arguments={"value": 2},
            generation=1,
        )

    assert await store.consume("ABC123", bot=bot, event=event, generation=1) == original


@pytest.mark.asyncio
async def test_redis_ttl_expires_action_and_indexes(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(ttl_seconds=1),
        nonce_factory=lambda: "ABC123",
    )
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)

    ttl = await redis_client.pttl(_action_key("ABC123"))
    assert 0 < ttl <= 1_000
    await asyncio.sleep(1.05)

    with pytest.raises(PendingActionError, match=r"不存在|过期"):
        await store.consume("ABC123", bot=bot, event=event, generation=1)
    assert await store.size() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other_bot", "other_event"),
    [
        (FakeBot(self_id="20000"), _event()),
        (FakeBot(adapter_name="other-adapter"), _event()),
        (FakeBot(), _event(user_id=2)),
        (FakeBot(), _event(group_id=11)),
        (FakeBot(), _event(group_id=None)),
    ],
    ids=["bot", "adapter", "user", "group", "private"],
)
async def test_consume_is_bound_to_bot_adapter_user_and_group(
    redis_client: FakeRedis,
    other_bot: FakeBot,
    other_event: SimpleNamespace,
) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "B07B07")
    owner_bot = FakeBot()
    owner_event = _event()
    await store.create(bot=owner_bot, event=owner_event, tool_name="mutate", arguments={}, generation=2)

    with pytest.raises(PendingActionError, match="不匹配"):
        await store.consume("B07B07", bot=other_bot, event=other_event, generation=2)

    action = await store.consume("B07B07", bot=owner_bot, event=owner_event, generation=2)
    assert action.tool_name == "mutate"


@pytest.mark.asyncio
async def test_generation_change_invalidates_action_once(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "DA6A6E")
    bot = FakeBot()
    event = _event()
    action = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=4,
        bundle_digest="a" * 64,
    )

    assert action.bundle_digest == "a" * 64
    with pytest.raises(PendingActionError, match="已重载"):
        await store.consume("DA6A6E", bot=bot, event=event, generation=5)
    with pytest.raises(PendingActionError, match="已过期或已使用"):
        await store.consume("DA6A6E", bot=bot, event=event, generation=4)


@pytest.mark.asyncio
async def test_concurrent_consumers_return_at_most_one_action(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(max_failures=100),
        nonce_factory=lambda: "C0DE42",
    )
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)

    results = await asyncio.gather(
        *(store.consume("C0DE42", bot=bot, event=event, generation=1) for _ in range(12)),
        return_exceptions=True,
    )

    assert sum(isinstance(result, PendingAction) for result in results) == 1
    assert sum(isinstance(result, PendingActionError) for result in results) == 11
    assert await store.size() == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_creates_share_one_action(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(),
        nonce_factory=_nonce_factory("A00001", "B00002", "C00003", "D00004", "E00005", "F00006"),
    )
    bot = FakeBot()
    event = _event()

    actions = await asyncio.gather(
        *(
            store.create(
                bot=bot,
                event=event,
                tool_name="mutate",
                arguments={"value": 1},
                generation=1,
            )
            for _ in range(6)
        )
    )

    assert len({action.action_id for action in actions}) == 1
    assert len({action.nonce for action in actions}) == 1
    assert await store.size() == 1


@pytest.mark.asyncio
async def test_cancel_and_clear_only_touch_the_selected_namespace(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(key_prefix="tenant-one"),
        nonce_factory=_nonce_factory("A00001", "B00002"),
    )
    bot = FakeBot()
    owner = _event()
    await store.create(bot=bot, event=owner, tool_name="first", arguments={}, generation=1)
    await store.create(bot=bot, event=owner, tool_name="second", arguments={}, generation=1)

    with pytest.raises(PendingActionError, match="不匹配"):
        await store.cancel("A00001", bot=bot, event=_event(user_id=2))
    await store.cancel("A00001", bot=bot, event=owner)
    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("A00001", bot=bot, event=owner, generation=1)
    assert await store.size() == 1

    await redis_client.set("tenant-one:unrelated", "keep")
    await redis_client.set("tenant-two:{pending-action}:action:ABC123", "keep")
    await store.clear()

    keys = await _decoded_keys(redis_client)
    assert not any(key.startswith("tenant-one:{pending-action}:") for key in keys)
    assert "tenant-one:unrelated" in keys
    assert "tenant-two:{pending-action}:action:ABC123" in keys


@pytest.mark.asyncio
async def test_failure_budget_blocks_valid_nonce_then_recovers_after_window(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(ttl_seconds=10, failure_window_seconds=1, max_failures=2),
        nonce_factory=lambda: "ABC123",
    )
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    for invalid in ("FFFFFF", "EEEEEE"):
        with pytest.raises(PendingActionError, match="不存在"):
            await store.consume(invalid, bot=bot, event=event, generation=1)

    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.consume("ABC123", bot=bot, event=event, generation=1)
    assert await redis_client.get(_action_key("ABC123")) is not None

    await asyncio.sleep(1.05)
    assert (await store.consume("ABC123", bot=bot, event=event, generation=1)).nonce == "ABC123"


@pytest.mark.asyncio
async def test_failure_budget_isolated_by_full_caller_identity(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(max_failures=1))
    blocked_bot = FakeBot()
    blocked_event = _event()
    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("FFFFFF", bot=blocked_bot, event=blocked_event, generation=1)
    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.consume("EEEEEE", bot=blocked_bot, event=blocked_event, generation=1)

    isolated_callers = [
        (FakeBot(self_id="20000"), _event()),
        (FakeBot(adapter_name="other"), _event()),
        (FakeBot(), _event(user_id=2)),
        (FakeBot(), _event(group_id=11)),
    ]
    for bot, event in isolated_callers:
        with pytest.raises(PendingActionError, match="不存在"):
            await store.consume("FFFFFF", bot=bot, event=event, generation=1)


@pytest.mark.asyncio
async def test_failure_key_index_evicts_oldest_caller_at_bound(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(max_failures=1, max_failure_keys=2),
    )
    bot = FakeBot()
    for user_id in (1, 2, 3):
        with pytest.raises(PendingActionError, match="不存在"):
            await store.consume("FFFFFF", bot=bot, event=_event(user_id=user_id), generation=1)

    failure_keys = await _decoded_keys(redis_client, f"{_root()}:failure:*")
    assert len(failure_keys) == 2
    assert await redis_client.zcard(f"{_root()}:failures") == 2

    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("EEEEEE", bot=bot, event=_event(user_id=1), generation=1)
    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.consume("DDDDDD", bot=bot, event=_event(user_id=3), generation=1)


@pytest.mark.asyncio
async def test_failure_eviction_excludes_exactly_expired_boundary_member(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(max_failure_keys=1),
    )

    async def fixed_now() -> float:
        return 1_000.0

    monkeypatch.setattr(store, "_server_now", fixed_now)
    expired_fingerprint = "a" * 64
    active_fingerprint = "b" * 64
    await redis_client.set(store._keyspace.failure(expired_fingerprint), "1", px=60_000)
    await redis_client.set(store._keyspace.failure(active_fingerprint), "1", px=60_000)
    await redis_client.zadd(
        store._keyspace.failure_index,
        {expired_fingerprint: 1_000.0, active_fingerprint: 1_001.0},
    )

    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("FFFFFF", bot=FakeBot(), event=_event(), generation=1)

    assert await redis_client.zcard(store._keyspace.failure_index) == 1
    assert await redis_client.get(store._keyspace.failure(expired_fingerprint)) is None
    assert await redis_client.get(store._keyspace.failure(active_fingerprint)) is None


@pytest.mark.asyncio
async def test_arguments_tampering_is_consumed_without_returning_action(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "FACE00")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"amount": 1},
        generation=1,
    )
    key = _action_key("FACE00")
    raw = await redis_client.get(key)
    assert isinstance(raw, bytes)
    payload = json.loads(raw)
    payload["arguments_json"] = '{"amount":999}'
    await redis_client.set(key, json.dumps(payload, ensure_ascii=False), keepttl=True)

    with pytest.raises(PendingActionError, match="参数校验失败"):
        await store.consume("FACE00", bot=bot, event=event, generation=1)

    assert await redis_client.get(key) is None
    assert await store.size() == 0


@pytest.mark.asyncio
async def test_corrupted_record_is_deleted_and_stale_slot_can_recover(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(),
        nonce_factory=_nonce_factory("C0FFEE", "D0FFEE"),
    )
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    key = _action_key("C0FFEE")
    await redis_client.set(key, b"not-json", keepttl=True)

    with pytest.raises(PendingActionError, match="记录已损坏"):
        await store.consume("C0FFEE", bot=bot, event=event, generation=1)
    assert await redis_client.get(key) is None

    replacement = await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    assert replacement.nonce == "D0FFEE"
    assert await store.size() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_version", [True, 1.0, "1", 2])
async def test_record_schema_version_requires_exact_integer(
    redis_client: FakeRedis,
    invalid_version: object,
) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "ABC123")
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    key = _action_key("ABC123")
    raw = await redis_client.get(key)
    assert isinstance(raw, bytes)
    payload = json.loads(raw)
    payload["schema_version"] = invalid_version
    await redis_client.set(key, json.dumps(payload, ensure_ascii=False), keepttl=True)

    with pytest.raises(PendingActionError, match="记录已损坏"):
        await store.consume("ABC123", bot=bot, event=event, generation=1)

    assert await redis_client.get(key) is None


@pytest.mark.asyncio
async def test_expired_record_payload_fails_closed_even_if_redis_key_survives(redis_client: FakeRedis) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "AB12CD")
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    key = _action_key("AB12CD")
    raw = await redis_client.get(key)
    assert isinstance(raw, bytes)
    payload = json.loads(raw)
    payload["created_at"] -= 1_000
    payload["expires_at"] -= 1_000
    await redis_client.set(key, json.dumps(payload, ensure_ascii=False), keepttl=True)

    with pytest.raises(PendingActionError, match=r"过期|时间边界"):
        await store.consume("AB12CD", bot=bot, event=event, generation=1)
    assert await redis_client.get(key) is None


@pytest.mark.asyncio
async def test_backend_errors_are_sanitized_and_never_fallback(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "redis://admin:top-secret@cache.internal:6380/9"

    async def broken_time() -> tuple[int, int]:
        raise RuntimeError(f"connection failed for {secret}")

    monkeypatch.setattr(redis_client, "time", broken_time)
    store = RedisPendingActionStore(redis_client, settings=_settings())

    with pytest.raises(RedisPendingActionUnavailableError) as captured:
        await store.size()

    assert "RuntimeError" in str(captured.value)
    assert "top-secret" not in str(captured.value)
    assert "cache.internal" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
async def test_committed_consume_with_lost_exec_response_never_returns_action(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "ABC123")
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    original_execute = Pipeline.execute

    async def execute_then_lose_response(self: Pipeline, raise_on_error: bool = True) -> list[Any]:
        await original_execute(self, raise_on_error=raise_on_error)
        raise RuntimeError("lost response redis://admin:top-secret@cache.internal/0")

    with monkeypatch.context() as patcher:
        patcher.setattr(Pipeline, "execute", execute_then_lose_response)
        with pytest.raises(RedisPendingActionUnavailableError) as captured:
            await store.consume("ABC123", bot=bot, event=event, generation=1)

    assert "top-secret" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert await redis_client.get(_action_key("ABC123")) is None
    with pytest.raises(PendingActionError, match="已过期或已使用"):
        await store.consume("ABC123", bot=bot, event=event, generation=1)


@pytest.mark.asyncio
async def test_watch_error_is_the_only_retryable_transaction_result(
    redis_client: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisPendingActionStore(redis_client, settings=_settings(), nonce_factory=lambda: "ABC123")
    bot = FakeBot()
    event = _event()
    await store.create(bot=bot, event=event, tool_name="mutate", arguments={}, generation=1)
    original_execute = Pipeline.execute
    attempts = 0

    async def conflict_once(self: Pipeline, raise_on_error: bool = True) -> list[Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WatchError("simulated conflict")
        return await original_execute(self, raise_on_error=raise_on_error)

    monkeypatch.setattr(Pipeline, "execute", conflict_once)
    action = await store.consume("ABC123", bot=bot, event=event, generation=1)

    assert action.nonce == "ABC123"
    assert attempts == 2


@pytest.mark.asyncio
async def test_execute_pending_action_accepts_explicit_falsey_redis_store(redis_client: FakeRedis) -> None:
    executions: list[tuple[str, bool]] = []

    async def mutate(value: str, _tool_context: Any = None) -> str:
        executions.append((value, bool(_tool_context.confirmed)))
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change state",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=mutate,
        effect=ToolEffect.MUTATING,
        permission="user",
    )
    store = FalseyRedisPendingActionStore(
        redis_client,
        settings=_settings(),
        nonce_factory=lambda: "F00D42",
    )
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": "fixed"},
        generation=9,
    )

    action, result = await execute_pending_action(
        "F00D42",
        bot=bot,
        event=event,
        runtime_snapshot=_runtime_snapshot(9, {"mutate": spec.as_legacy_schema()}),
        store=store,
    )

    assert action.nonce == "F00D42"
    assert result.text == "changed"
    assert executions == [("fixed", True)]
    with pytest.raises(PendingActionError, match="已过期或已使用"):
        await store.consume("F00D42", bot=bot, event=event, generation=9)


@pytest.mark.asyncio
async def test_remaining_ttl_uses_only_injected_display_clock(redis_client: FakeRedis) -> None:
    display_now = [0.0]
    store = RedisPendingActionStore(
        redis_client,
        settings=_settings(),
        nonce_factory=lambda: "ABC123",
        wall_clock=lambda: display_now[0],
    )
    action = await store.create(
        bot=FakeBot(),
        event=_event(),
        tool_name="mutate",
        arguments={},
        generation=1,
    )
    display_now[0] = action.expires_at - 0.2

    assert store.remaining_ttl_seconds(action) == 1
    display_now[0] = action.expires_at + 1
    assert store.remaining_ttl_seconds(action) == 0

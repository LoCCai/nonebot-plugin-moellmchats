from __future__ import annotations

from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager
import socket
from typing import TYPE_CHECKING

import pytest

from nonebot_plugin_moellmchats.admission import AdmissionRejected
from nonebot_plugin_moellmchats.admission_store import AdmissionStoreError
from nonebot_plugin_moellmchats.redis_admission import RedisAdmissionSettings
from nonebot_plugin_moellmchats.redis_client import (
    RedisClientManager,
    RedisClientSettings,
)
from nonebot_plugin_moellmchats.redis_cooldowns import (
    RedisCooldownSettings,
    RedisCooldownUnavailableError,
)
from nonebot_plugin_moellmchats.redis_failure_policy import (
    RedisFailurePolicy,
    RedisFailurePolicyError,
    SingleInstanceAdmissionFallbackGate,
    build_redis_component_ports,
)
from nonebot_plugin_moellmchats.redis_pending_actions import (
    RedisPendingActionSettings,
    RedisPendingActionUnavailableError,
)
from nonebot_plugin_moellmchats.runtime_resources import (
    RuntimeGenerationResourceState,
    RuntimeResourceBuilder,
    RuntimeResourceConfigurationError,
    RuntimeResourceSettings,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from redis.asyncio import Redis


class _Adapter:
    @staticmethod
    def get_name() -> str:
        return "test-adapter"


class _Bot:
    self_id = "bot-1"
    adapter = _Adapter()


class _Event:
    user_id = 10001
    group_id = 20002


def _snapshot(generation: int = 1) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config={},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=float(generation),
    )


def _failing_manager(
    *,
    calls: list[str] | None = None,
) -> RedisClientManager:
    def client_factory(*_args: object, **_kwargs: object) -> Redis:
        if calls is not None:
            calls.append("client_factory")
        raise RuntimeError("redis://credential@private.invalid/0")

    return RedisClientManager(
        RedisClientSettings(
            redis_url="redis://credential@private.invalid/0",
        ),
        client_factory=client_factory,
    )


def test_fallback_requires_explicit_single_instance_safety() -> None:
    with pytest.raises(RedisFailurePolicyError, match="single-instance-safe"):
        RedisFailurePolicy(cooldown_memory_fallback=True)
    with pytest.raises(RedisFailurePolicyError, match="single-instance-safe"):
        RedisFailurePolicy(admission_memory_fallback=True)

    policy = RedisFailurePolicy(
        single_instance_safe=True,
        cooldown_memory_fallback=True,
        admission_memory_fallback=True,
        fallback_max_entries=64,
    )
    assert policy.safe_diagnostics() == {
        "single_instance_safe": True,
        "cooldown_memory_fallback": True,
        "admission_memory_fallback": True,
        "fallback_max_entries": 64,
        "pending_action_fail_closed": True,
        "history_generation_bypass": True,
    }


@pytest.mark.asyncio
async def test_pending_action_redis_failure_never_falls_back_or_confirms() -> None:
    calls: list[str] = []
    ports = build_redis_component_ports(
        _failing_manager(calls=calls),
        pending_actions=RedisPendingActionSettings(),
        policy=RedisFailurePolicy(
            single_instance_safe=True,
            cooldown_memory_fallback=False,
            admission_memory_fallback=False,
        ),
    )
    store = ports.pending_actions
    assert store is not None

    with pytest.raises(RedisPendingActionUnavailableError) as created:
        await store.create(
            bot=_Bot(),
            event=_Event(),
            tool_name="mutating_tool",
            arguments={"value": "must-not-run"},
            generation=1,
        )
    with pytest.raises(RedisPendingActionUnavailableError) as consumed:
        await store.consume(
            "ABC123",
            bot=_Bot(),
            event=_Event(),
            generation=1,
        )

    assert calls == ["client_factory", "client_factory"]
    assert "credential" not in str(created.value)
    assert "private.invalid" not in str(created.value)
    assert "credential" not in str(consumed.value)
    assert ports.policy.safe_diagnostics()["pending_action_fail_closed"] is True


@pytest.mark.asyncio
async def test_strict_cooldown_and_admission_fail_closed() -> None:
    ports = build_redis_component_ports(
        _failing_manager(),
        cooldowns=RedisCooldownSettings(),
        admission=RedisAdmissionSettings(),
    )
    assert ports.cooldowns is not None
    assert ports.admission is not None

    with pytest.raises(RedisCooldownUnavailableError):
        await ports.cooldowns.claim(
            user_id=10001,
            event_time=100.0,
            cooldown_seconds=30,
        )
    with pytest.raises(AdmissionStoreError):
        async with ports.admission.slot(10001):
            raise AssertionError("strict Redis admission must not yield")


@pytest.mark.asyncio
async def test_explicit_single_instance_policy_allows_bounded_local_fallbacks() -> None:
    ports = build_redis_component_ports(
        _failing_manager(),
        cooldowns=RedisCooldownSettings(),
        admission=RedisAdmissionSettings(
            max_active=1,
            max_pending=2,
            max_per_key=1,
        ),
        policy=RedisFailurePolicy(
            single_instance_safe=True,
            cooldown_memory_fallback=True,
            admission_memory_fallback=True,
            fallback_max_entries=8,
        ),
    )
    assert ports.cooldowns is not None
    assert ports.admission is not None

    first = await ports.cooldowns.claim(
        user_id=10001,
        event_time=100.0,
        cooldown_seconds=30,
    )
    assert first.lease is not None
    second = await ports.cooldowns.claim(
        user_id=10001,
        event_time=101.0,
        cooldown_seconds=30,
    )
    assert second.lease is None
    assert second.retry_after_seconds == 29
    assert await ports.cooldowns.release(first.lease) is True

    entered = False
    async with ports.admission.slot(10001):
        entered = True
    assert entered is True


class _ExitFailureGate:
    def __init__(self) -> None:
        self.entries = 0

    @asynccontextmanager
    async def slot(
        self,
        key: Hashable | None = None,
    ) -> AsyncIterator[None]:
        del key
        self.entries += 1
        try:
            yield
        finally:
            raise AdmissionStoreError("result unknown after admission")


class _RecordingGate:
    def __init__(self) -> None:
        self.entries = 0

    @asynccontextmanager
    async def slot(
        self,
        key: Hashable | None = None,
    ) -> AsyncIterator[None]:
        del key
        self.entries += 1
        yield


class _RejectedGate:
    @asynccontextmanager
    async def slot(
        self,
        key: Hashable | None = None,
    ) -> AsyncIterator[None]:
        del key
        raise AdmissionRejected("queue full")
        yield


@pytest.mark.asyncio
async def test_admission_never_switches_backend_after_primary_yields() -> None:
    primary = _ExitFailureGate()
    fallback = _RecordingGate()
    gate = SingleInstanceAdmissionFallbackGate(primary, fallback)

    with pytest.raises(AdmissionStoreError, match="result unknown"):
        async with gate.slot("user"):
            pass
    assert primary.entries == 1
    assert fallback.entries == 0


@pytest.mark.asyncio
async def test_admission_capacity_rejection_is_not_backend_fallback() -> None:
    fallback = _RecordingGate()
    gate = SingleInstanceAdmissionFallbackGate(_RejectedGate(), fallback)

    with pytest.raises(AdmissionRejected, match="queue full"):
        async with gate.slot("user"):
            raise AssertionError("rejected primary must not yield")
    assert fallback.entries == 0


def test_runtime_settings_require_explicit_redis_and_component_pairing() -> None:
    with pytest.raises(RuntimeResourceConfigurationError, match="RedisClientSettings"):
        RuntimeResourceSettings(
            redis_pending_actions=RedisPendingActionSettings(),
        )
    with pytest.raises(RuntimeResourceConfigurationError, match="cooldown"):
        RuntimeResourceSettings(
            redis=RedisClientSettings(redis_url="redis://redis.invalid/0"),
            redis_failure_policy=RedisFailurePolicy(
                single_instance_safe=True,
                cooldown_memory_fallback=True,
            ),
        )


@pytest.mark.asyncio
async def test_runtime_composition_stays_lazy_and_zero_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_calls = 0
    socket_calls = 0

    def client_factory(*_args: object, **_kwargs: object) -> Redis:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError("generation construction created a Redis client")

    def reject_socket(self: socket.socket, _address: object) -> None:
        nonlocal socket_calls
        del self
        socket_calls += 1
        raise AssertionError("generation construction performed network I/O")

    def manager_factory(settings: RedisClientSettings) -> RedisClientManager:
        return RedisClientManager(settings, client_factory=client_factory)

    monkeypatch.setattr(socket.socket, "connect", reject_socket)
    settings = RuntimeResourceSettings(
        redis=RedisClientSettings(redis_url="redis://redis.invalid/0"),
        redis_pending_actions=RedisPendingActionSettings(),
        redis_cooldowns=RedisCooldownSettings(),
        redis_admission=RedisAdmissionSettings(),
    )
    resources = RuntimeResourceBuilder(
        settings,
        redis_manager_factory=manager_factory,
    ).build(_snapshot())

    assert resources.redis_manager is not None
    assert resources.redis_manager.initialized is False
    assert resources.redis_components is not None
    assert resources.pending_action_store is not None
    assert resources.cooldown_store is not None
    assert resources.admission_gate is not None
    assert resources.safe_diagnostics()["redis_single_instance_safe"] is False
    assert client_calls == socket_calls == 0

    await resources.start()
    assert resources.state is RuntimeGenerationResourceState.RUNNING
    assert client_calls == socket_calls == 0
    await resources.close()
    assert resources.state is RuntimeGenerationResourceState.CLOSED
    assert client_calls == socket_calls == 0

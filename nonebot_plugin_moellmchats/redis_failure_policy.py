from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Hashable, Iterator, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from .admission import AdmissionController, AdmissionGateProtocol
from .admission_store import AdmissionStoreError
from .cooldowns import (
    CooldownClaim,
    CooldownLease,
    CooldownStoreProtocol,
    CooldownUserId,
    MemoryCooldownStore,
)
from .pending_actions import PendingAction, PendingActionStoreProtocol
from .redis_admission import (
    RedisAdmissionController,
    RedisAdmissionSettings,
    RedisAdmissionStore,
)
from .redis_client import RedisClientManager
from .redis_cooldowns import (
    RedisCooldownConflictError,
    RedisCooldownSettings,
    RedisCooldownStore,
    RedisCooldownUnavailableError,
)
from .redis_pending_actions import (
    RedisPendingActionSettings,
    RedisPendingActionStore,
    RedisPendingActionUnavailableError,
)


class RedisFailurePolicyError(RuntimeError):
    """A composed Redis failure policy cannot preserve its safety contract."""


@dataclass(frozen=True)
class RedisFailurePolicy:
    """Explicitly authorize only bounded, single-process-safe fallbacks.

    PendingAction deliberately has no fallback switch. Once Redis is selected
    for confirmations, every create/consume/cancel failure remains fail closed.
    """

    single_instance_safe: bool = False
    cooldown_memory_fallback: bool = False
    admission_memory_fallback: bool = False
    fallback_max_entries: int = 1_000

    def __post_init__(self) -> None:
        for label, value in (
            ("single_instance_safe", self.single_instance_safe),
            ("cooldown_memory_fallback", self.cooldown_memory_fallback),
            ("admission_memory_fallback", self.admission_memory_fallback),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} 必须是 bool")
        if (self.cooldown_memory_fallback or self.admission_memory_fallback) and not self.single_instance_safe:
            raise RedisFailurePolicyError("Redis Memory fallback 只允许显式 single-instance-safe 部署")
        if (
            not isinstance(self.fallback_max_entries, int)
            or isinstance(self.fallback_max_entries, bool)
            or not 1 <= self.fallback_max_entries <= 10_000
        ):
            raise ValueError("fallback_max_entries 必须是 1 到 10000 的整数")

    def safe_diagnostics(self) -> dict[str, bool | int]:
        return {
            "single_instance_safe": self.single_instance_safe,
            "cooldown_memory_fallback": self.cooldown_memory_fallback,
            "admission_memory_fallback": self.admission_memory_fallback,
            "fallback_max_entries": self.fallback_max_entries,
            "pending_action_fail_closed": True,
            "history_generation_bypass": True,
        }


class _BoundedCooldownValues(MutableMapping[CooldownUserId, object]):
    """Small LRU mapping used only by an explicitly safe local fallback."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._values: OrderedDict[CooldownUserId, object] = OrderedDict()

    def __getitem__(self, key: CooldownUserId) -> object:
        value = self._values[key]
        self._values.move_to_end(key)
        return value

    def __setitem__(self, key: CooldownUserId, value: object) -> None:
        self._values[key] = value
        self._values.move_to_end(key)
        while len(self._values) > self._maximum:
            self._values.popitem(last=False)

    def __delitem__(self, key: CooldownUserId) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[CooldownUserId]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(
        self,
        key: CooldownUserId,
        default: object = None,
    ) -> object:
        try:
            return self[key]
        except KeyError:
            return default

    def clear(self) -> None:
        self._values.clear()


class LazyRedisPendingActionStore(PendingActionStoreProtocol):
    """Create the explicit Redis store lazily and never fall back to Memory."""

    def __init__(
        self,
        manager: RedisClientManager,
        *,
        settings: RedisPendingActionSettings,
    ) -> None:
        if not isinstance(manager, RedisClientManager):
            raise TypeError("manager 必须是 RedisClientManager")
        if not isinstance(settings, RedisPendingActionSettings):
            raise TypeError("settings 必须是 RedisPendingActionSettings")
        self._manager = manager
        self._settings = settings
        self._backend: RedisPendingActionStore | None = None

    def __repr__(self) -> str:
        return f"LazyRedisPendingActionStore(initialized={self._backend is not None!r}, fail_closed=True)"

    def safe_diagnostics(self) -> dict[str, bool | str]:
        return {
            "backend": "redis",
            "configured": True,
            "initialized": self._backend is not None,
            "failure_mode": "fail_closed",
        }

    def _store(self) -> RedisPendingActionStore:
        backend = self._backend
        if backend is not None:
            return backend
        try:
            backend = RedisPendingActionStore(
                self._manager.get_client(),
                settings=self._settings,
            )
        except Exception as error:
            raise RedisPendingActionUnavailableError(
                f"Redis PendingAction backend 不可用，危险操作已拒绝 ({type(error).__name__})"
            ) from None
        self._backend = backend
        return backend

    async def create(
        self,
        *,
        bot: Any,
        event: Any,
        tool_name: str,
        arguments: dict[str, Any],
        generation: int,
        bundle_digest: str | None = None,
    ) -> PendingAction:
        return await self._store().create(
            bot=bot,
            event=event,
            tool_name=tool_name,
            arguments=arguments,
            generation=generation,
            bundle_digest=bundle_digest,
        )

    async def consume(
        self,
        nonce: str,
        *,
        bot: Any,
        event: Any,
        generation: int,
    ) -> PendingAction:
        return await self._store().consume(
            nonce,
            bot=bot,
            event=event,
            generation=generation,
        )

    async def cancel(
        self,
        nonce: str,
        *,
        bot: Any,
        event: Any,
    ) -> None:
        await self._store().cancel(nonce, bot=bot, event=event)

    async def clear(self) -> None:
        await self._store().clear()

    def remaining_ttl_seconds(self, action: PendingAction) -> int:
        return self._store().remaining_ttl_seconds(action)

    async def size(self) -> int:
        return await self._store().size()


class LazyRedisCooldownStore(CooldownStoreProtocol):
    def __init__(
        self,
        manager: RedisClientManager,
        *,
        settings: RedisCooldownSettings,
    ) -> None:
        if not isinstance(manager, RedisClientManager):
            raise TypeError("manager 必须是 RedisClientManager")
        if not isinstance(settings, RedisCooldownSettings):
            raise TypeError("settings 必须是 RedisCooldownSettings")
        self._manager = manager
        self._settings = settings
        self._backend: RedisCooldownStore | None = None

    def __repr__(self) -> str:
        return f"LazyRedisCooldownStore(initialized={self._backend is not None!r})"

    def _store(self) -> RedisCooldownStore:
        backend = self._backend
        if backend is not None:
            return backend
        try:
            backend = RedisCooldownStore(
                self._manager.get_client(),
                settings=self._settings,
            )
        except Exception as error:
            raise RedisCooldownUnavailableError(f"Redis cooldown backend 不可用，操作已拒绝 ({type(error).__name__})") from None
        self._backend = backend
        return backend

    async def claim(
        self,
        *,
        user_id: CooldownUserId,
        event_time: float,
        cooldown_seconds: int,
    ) -> CooldownClaim:
        return await self._store().claim(
            user_id=user_id,
            event_time=event_time,
            cooldown_seconds=cooldown_seconds,
        )

    async def release(self, lease: CooldownLease) -> bool:
        return await self._store().release(lease)


class SingleInstanceCooldownFallbackStore(CooldownStoreProtocol):
    """Fallback only when a Redis claim never produced a known result."""

    def __init__(
        self,
        primary: CooldownStoreProtocol,
        fallback: CooldownStoreProtocol,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_leases: set[CooldownLease] = set()

    def __repr__(self) -> str:
        return "SingleInstanceCooldownFallbackStore(single_instance_safe=True)"

    async def claim(
        self,
        *,
        user_id: CooldownUserId,
        event_time: float,
        cooldown_seconds: int,
    ) -> CooldownClaim:
        try:
            return await self._primary.claim(
                user_id=user_id,
                event_time=event_time,
                cooldown_seconds=cooldown_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (
            RedisCooldownUnavailableError,
            RedisCooldownConflictError,
        ):
            claim = await self._fallback.claim(
                user_id=user_id,
                event_time=event_time,
                cooldown_seconds=cooldown_seconds,
            )
            if claim.lease is not None:
                self._fallback_leases.add(claim.lease)
            return claim

    async def release(self, lease: CooldownLease) -> bool:
        if lease in self._fallback_leases:
            try:
                return await self._fallback.release(lease)
            finally:
                self._fallback_leases.discard(lease)
        return await self._primary.release(lease)


class LazyRedisAdmissionGate(AdmissionGateProtocol):
    def __init__(
        self,
        manager: RedisClientManager,
        *,
        settings: RedisAdmissionSettings,
    ) -> None:
        if not isinstance(manager, RedisClientManager):
            raise TypeError("manager 必须是 RedisClientManager")
        if not isinstance(settings, RedisAdmissionSettings):
            raise TypeError("settings 必须是 RedisAdmissionSettings")
        self._manager = manager
        self._settings = settings
        self._backend: RedisAdmissionController | None = None

    def __repr__(self) -> str:
        return f"LazyRedisAdmissionGate(initialized={self._backend is not None!r})"

    def _controller(self) -> RedisAdmissionController:
        backend = self._backend
        if backend is not None:
            return backend
        try:
            backend = RedisAdmissionController(
                RedisAdmissionStore(
                    self._manager.get_client(),
                    settings=self._settings,
                )
            )
        except Exception as error:
            raise AdmissionStoreError(f"Redis admission backend 不可用，操作已拒绝 ({type(error).__name__})") from None
        self._backend = backend
        return backend

    @asynccontextmanager
    async def slot(
        self,
        key: Hashable | None = None,
    ) -> AsyncIterator[None]:
        async with self._controller().slot(key):
            yield


class SingleInstanceAdmissionFallbackGate(AdmissionGateProtocol):
    """Fallback only if the Redis gate failed before admitting the caller."""

    def __init__(
        self,
        primary: AdmissionGateProtocol,
        fallback: AdmissionGateProtocol,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def __repr__(self) -> str:
        return "SingleInstanceAdmissionFallbackGate(single_instance_safe=True)"

    @asynccontextmanager
    async def slot(
        self,
        key: Hashable | None = None,
    ) -> AsyncIterator[None]:
        entered = False
        try:
            async with self._primary.slot(key):
                entered = True
                yield
        except asyncio.CancelledError:
            raise
        except AdmissionStoreError:
            if entered:
                raise
            async with self._fallback.slot(key):
                yield


@dataclass(frozen=True)
class RedisComponentPorts:
    pending_actions: PendingActionStoreProtocol | None
    cooldowns: CooldownStoreProtocol | None
    admission: AdmissionGateProtocol | None
    policy: RedisFailurePolicy

    def safe_diagnostics(self) -> dict[str, bool | int]:
        return {
            **self.policy.safe_diagnostics(),
            "pending_actions_configured": self.pending_actions is not None,
            "cooldowns_configured": self.cooldowns is not None,
            "admission_configured": self.admission is not None,
        }


def build_redis_component_ports(
    manager: RedisClientManager,
    *,
    pending_actions: RedisPendingActionSettings | None = None,
    cooldowns: RedisCooldownSettings | None = None,
    admission: RedisAdmissionSettings | None = None,
    policy: RedisFailurePolicy = RedisFailurePolicy(),
) -> RedisComponentPorts:
    if not isinstance(manager, RedisClientManager):
        raise TypeError("manager 必须是 RedisClientManager")
    if not isinstance(policy, RedisFailurePolicy):
        raise TypeError("policy 必须是 RedisFailurePolicy")
    if pending_actions is not None and not isinstance(
        pending_actions,
        RedisPendingActionSettings,
    ):
        raise TypeError("pending_actions 必须是 RedisPendingActionSettings 或 None")
    if cooldowns is not None and not isinstance(cooldowns, RedisCooldownSettings):
        raise TypeError("cooldowns 必须是 RedisCooldownSettings 或 None")
    if admission is not None and not isinstance(admission, RedisAdmissionSettings):
        raise TypeError("admission 必须是 RedisAdmissionSettings 或 None")
    if policy.cooldown_memory_fallback and cooldowns is None:
        raise RedisFailurePolicyError("cooldown fallback 缺少显式 Redis cooldown 配置")
    if policy.admission_memory_fallback and admission is None:
        raise RedisFailurePolicyError("admission fallback 缺少显式 Redis admission 配置")

    pending_port = None if pending_actions is None else LazyRedisPendingActionStore(manager, settings=pending_actions)
    cooldown_port: CooldownStoreProtocol | None = None
    if cooldowns is not None:
        cooldown_port = LazyRedisCooldownStore(manager, settings=cooldowns)
        if policy.cooldown_memory_fallback:
            cooldown_port = SingleInstanceCooldownFallbackStore(
                cooldown_port,
                MemoryCooldownStore(_BoundedCooldownValues(policy.fallback_max_entries)),
            )
    admission_port: AdmissionGateProtocol | None = None
    if admission is not None:
        admission_port = LazyRedisAdmissionGate(manager, settings=admission)
        if policy.admission_memory_fallback:
            admission_port = SingleInstanceAdmissionFallbackGate(
                admission_port,
                AdmissionController(
                    name=admission.name,
                    max_active=admission.max_active,
                    max_pending=admission.max_pending,
                    max_per_key=admission.max_per_key,
                ),
            )
    return RedisComponentPorts(
        pending_actions=pending_port,
        cooldowns=cooldown_port,
        admission=admission_port,
        policy=policy,
    )


__all__ = [
    "LazyRedisAdmissionGate",
    "LazyRedisCooldownStore",
    "LazyRedisPendingActionStore",
    "RedisComponentPorts",
    "RedisFailurePolicy",
    "RedisFailurePolicyError",
    "SingleInstanceAdmissionFallbackGate",
    "SingleInstanceCooldownFallbackStore",
    "build_redis_component_ports",
]

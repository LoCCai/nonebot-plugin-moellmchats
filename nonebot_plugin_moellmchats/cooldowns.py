from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
import math
import re
import secrets
from typing import Protocol

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_USER_ID_CHARS = 512
_TOKEN_ATTEMPTS = 32

CooldownUserId = int | str
TokenFactory = Callable[[], str]


class CooldownError(RuntimeError):
    """A cooldown claim could not be resolved safely."""


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _normalize_user_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise CooldownError("cooldown user_id 必须是整数或字符串")
    normalized = str(value)
    if not normalized or len(normalized) > _MAX_USER_ID_CHARS or _has_control_characters(normalized):
        raise CooldownError("cooldown user_id 无效")
    return normalized


def _normalize_event_time(value: object) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise CooldownError("cooldown event_time 必须是有限时间")
    return float(value)


def _normalize_cooldown_seconds(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CooldownError("cooldown_seconds 必须是整数")
    return max(0, value)


def _new_token(factory: TokenFactory) -> str:
    for _attempt in range(_TOKEN_ATTEMPTS):
        try:
            token = str(factory()).strip().lower()
        except Exception as error:
            raise CooldownError(f"无法生成安全 cooldown claim token ({type(error).__name__})") from None
        if _TOKEN_RE.fullmatch(token):
            return token
    raise CooldownError("无法生成安全 cooldown claim token")


@dataclass(frozen=True)
class CooldownLease:
    """Opaque ownership proof for releasing exactly one cooldown claim."""

    user_id: CooldownUserId
    token: str
    claimed_at: float

    def __post_init__(self) -> None:
        _normalize_user_id(self.user_id)
        if not isinstance(self.token, str) or not _TOKEN_RE.fullmatch(self.token):
            raise ValueError("token 必须是 32 位小写十六进制字符串")
        if (
            not isinstance(self.claimed_at, (int, float))
            or isinstance(self.claimed_at, bool)
            or not math.isfinite(self.claimed_at)
        ):
            raise ValueError("claimed_at 必须是有限时间")


@dataclass(frozen=True)
class CooldownClaim:
    """A successful lease or the rounded-up time before the next retry."""

    lease: CooldownLease | None
    retry_after_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.retry_after_seconds, int)
            or isinstance(self.retry_after_seconds, bool)
            or self.retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds 必须是非负整数")
        if self.retry_after_seconds and self.lease is not None:
            raise ValueError("被拒绝的 cooldown claim 不得携带 lease")

    @property
    def acquired(self) -> bool:
        return self.retry_after_seconds == 0


class CooldownStoreProtocol(Protocol):
    """Backend-neutral atomic claim/release contract for chat cooldowns."""

    async def claim(
        self,
        *,
        user_id: CooldownUserId,
        event_time: float,
        cooldown_seconds: int,
    ) -> CooldownClaim: ...

    async def release(self, lease: CooldownLease) -> bool: ...


@dataclass(frozen=True)
class _MemoryOwner:
    token: str
    claimed_at: float


class MemoryCooldownStore:
    """Atomic adapter over the legacy timestamp mapping used by one process."""

    def __init__(
        self,
        values: MutableMapping[CooldownUserId, object],
        *,
        token_factory: TokenFactory | None = None,
    ) -> None:
        if not isinstance(values, MutableMapping):
            raise TypeError("values 必须是 MutableMapping")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("token_factory 必须可调用")
        self._values = values
        self._owners: OrderedDict[CooldownUserId, _MemoryOwner] = OrderedDict()
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"MemoryCooldownStore(active_owners={len(self._owners)!r})"

    def safe_diagnostics(self) -> dict[str, bool | str]:
        return {"backend": "memory", "configured": True}

    def _prune_owners_locked(self) -> None:
        missing = object()
        for user_id, owner in list(self._owners.items()):
            if self._values.get(user_id, missing) != owner.claimed_at:
                self._owners.pop(user_id, None)

    async def claim(
        self,
        *,
        user_id: CooldownUserId,
        event_time: float,
        cooldown_seconds: int,
    ) -> CooldownClaim:
        _normalize_user_id(user_id)
        normalized_time = _normalize_event_time(event_time)
        normalized_cooldown = _normalize_cooldown_seconds(cooldown_seconds)
        async with self._lock:
            self._prune_owners_locked()
            missing = object()
            previous = self._values.get(user_id, missing)
            if previous is missing:
                previous = 0
                self._values[user_id] = previous
            if not isinstance(previous, (int, float)) or isinstance(previous, bool) or not math.isfinite(previous):
                raise CooldownError("内存 cooldown 时间戳已损坏")
            elapsed = normalized_time - float(previous)
            if elapsed < normalized_cooldown:
                return CooldownClaim(
                    lease=None,
                    retry_after_seconds=max(
                        1,
                        math.ceil(normalized_cooldown - elapsed),
                    ),
                )
            token = _new_token(self._token_factory)
            lease = CooldownLease(
                user_id=user_id,
                token=token,
                claimed_at=normalized_time,
            )
            self._values[user_id] = normalized_time
            self._owners[user_id] = _MemoryOwner(
                token=token,
                claimed_at=normalized_time,
            )
            self._owners.move_to_end(user_id)
            self._prune_owners_locked()
            return CooldownClaim(lease=lease, retry_after_seconds=0)

    async def release(self, lease: CooldownLease) -> bool:
        if not isinstance(lease, CooldownLease):
            raise TypeError("lease 必须是 CooldownLease")
        async with self._lock:
            owner = self._owners.get(lease.user_id)
            if owner != _MemoryOwner(lease.token, float(lease.claimed_at)):
                return False
            current = self._values.get(lease.user_id)
            if current != lease.claimed_at:
                self._owners.pop(lease.user_id, None)
                return False
            self._values[lease.user_id] = 0
            self._owners.pop(lease.user_id, None)
            return True

    def reset_user(self, user_id: CooldownUserId) -> None:
        _normalize_user_id(user_id)
        self._values[user_id] = 0
        self._owners.pop(user_id, None)

    def clear(self) -> None:
        self._values.clear()
        self._owners.clear()

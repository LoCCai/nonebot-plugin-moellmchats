from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import math
import re
import secrets
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .cooldowns import (
    CooldownClaim,
    CooldownError,
    CooldownLease,
    CooldownUserId,
    _new_token,
    _normalize_cooldown_seconds,
    _normalize_event_time,
    _normalize_user_id,
)

_KEY_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

TokenFactory = Callable[[], str]


class RedisCooldownUnavailableError(CooldownError):
    """The Redis backend could not establish a known cooldown result."""


class RedisCooldownConflictError(CooldownError):
    """A bounded Redis cooldown retry budget was exhausted."""


def _validate_integer(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须是 {minimum} 到 {maximum} 的整数")
    return value


@dataclass(frozen=True)
class RedisCooldownSettings:
    """Explicit bounded settings for one Redis cooldown namespace."""

    key_prefix: str = "moellm"
    max_cooldown_seconds: int = 3_600
    operation_retries: int = 32

    def __post_init__(self) -> None:
        if not isinstance(self.key_prefix, str) or not _KEY_PREFIX_RE.fullmatch(self.key_prefix):
            raise ValueError("key_prefix 必须是 1 到 96 位安全 Redis key 前缀")
        _validate_integer(
            self.max_cooldown_seconds,
            label="max_cooldown_seconds",
            minimum=1,
            maximum=86_400,
        )
        _validate_integer(
            self.operation_retries,
            label="operation_retries",
            minimum=1,
            maximum=64,
        )

    @property
    def max_cooldown_milliseconds(self) -> int:
        return self.max_cooldown_seconds * 1_000

    def safe_diagnostics(self) -> dict[str, int | str]:
        return {
            "key_prefix": self.key_prefix,
            "max_cooldown_seconds": self.max_cooldown_seconds,
            "operation_retries": self.operation_retries,
        }


def _user_fingerprint(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _decode_token(value: object) -> str:
    if isinstance(value, bytes):
        if len(value) > 32:
            raise CooldownError("Redis cooldown claim token 超过安全大小限制")
        try:
            token = value.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            raise CooldownError("Redis cooldown claim token 编码无效") from None
    elif isinstance(value, str):
        token = value
    else:
        raise CooldownError("Redis cooldown claim token 类型无效")
    if not _TOKEN_RE.fullmatch(token):
        raise CooldownError("Redis cooldown claim token 已损坏")
    return token


class RedisCooldownStore:
    """Explicit Redis SET-NX-TTL backend with owner-bound release."""

    def __init__(
        self,
        client: Redis,
        *,
        settings: RedisCooldownSettings | None = None,
        token_factory: TokenFactory | None = None,
    ) -> None:
        if not isinstance(client, Redis):
            raise TypeError("client 必须是 redis-py asyncio Redis client")
        if settings is not None and not isinstance(settings, RedisCooldownSettings):
            raise TypeError("settings 必须是 RedisCooldownSettings")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("token_factory 必须可调用")
        self._client = client
        self._settings = RedisCooldownSettings() if settings is None else settings
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))

    @property
    def settings(self) -> RedisCooldownSettings:
        return self._settings

    def __repr__(self) -> str:
        return (
            "RedisCooldownStore("
            f"key_prefix={self._settings.key_prefix!r}, "
            f"max_cooldown_seconds={self._settings.max_cooldown_seconds!r})"
        )

    def safe_diagnostics(self) -> dict[str, bool | int | str]:
        return {
            "backend": "redis",
            "configured": True,
            **self._settings.safe_diagnostics(),
        }

    def _key(self, user_id: str) -> str:
        fingerprint = _user_fingerprint(user_id)
        return f"{self._settings.key_prefix}:cd:{{{fingerprint}}}"

    @staticmethod
    async def _unwatch(pipe: Any) -> None:
        await pipe.unwatch()

    @staticmethod
    def _translate_backend_error(
        operation: str,
        error: Exception,
    ) -> RedisCooldownUnavailableError:
        return RedisCooldownUnavailableError(f"Redis cooldown {operation}结果未知，操作已拒绝 ({type(error).__name__})")

    async def claim(
        self,
        *,
        user_id: CooldownUserId,
        event_time: float,
        cooldown_seconds: int,
    ) -> CooldownClaim:
        normalized_user_id = _normalize_user_id(user_id)
        _normalize_event_time(event_time)
        normalized_cooldown = _normalize_cooldown_seconds(cooldown_seconds)
        if normalized_cooldown > self._settings.max_cooldown_seconds:
            raise CooldownError("cooldown_seconds 超过 Redis backend 安全上限")
        if normalized_cooldown == 0:
            return CooldownClaim(lease=None, retry_after_seconds=0)

        token = _new_token(self._token_factory)
        key = self._key(normalized_user_id)
        ttl_milliseconds = normalized_cooldown * 1_000
        try:
            for _attempt in range(self._settings.operation_retries):
                result = await self._client.set(
                    key,
                    token,
                    nx=True,
                    px=ttl_milliseconds,
                )
                if result is True:
                    return CooldownClaim(
                        lease=CooldownLease(
                            user_id=normalized_user_id,
                            token=token,
                            claimed_at=float(event_time),
                        ),
                        retry_after_seconds=0,
                    )
                if result is not None:
                    raise RedisCooldownUnavailableError("Redis cooldown SET NX 响应无效，操作已拒绝")
                remaining_milliseconds = await self._client.pttl(key)
                if not isinstance(remaining_milliseconds, int) or isinstance(remaining_milliseconds, bool):
                    raise RedisCooldownUnavailableError("Redis cooldown PTTL 响应无效，操作已拒绝")
                if remaining_milliseconds > 0:
                    if remaining_milliseconds > self._settings.max_cooldown_milliseconds:
                        raise CooldownError("Redis cooldown TTL 超过安全上限")
                    return CooldownClaim(
                        lease=None,
                        retry_after_seconds=max(
                            1,
                            math.ceil(remaining_milliseconds / 1_000),
                        ),
                    )
                if remaining_milliseconds in {-2, 0}:
                    continue
                if remaining_milliseconds == -1:
                    raise CooldownError("Redis cooldown key 缺少 TTL，操作已拒绝")
                raise RedisCooldownUnavailableError("Redis cooldown PTTL 响应无效，操作已拒绝")
            raise RedisCooldownConflictError("Redis cooldown claim 并发冲突过多，操作已拒绝")
        except asyncio.CancelledError:
            raise
        except CooldownError:
            raise
        except Exception as error:
            raise self._translate_backend_error("claim", error) from None

    async def release(self, lease: CooldownLease) -> bool:
        if not isinstance(lease, CooldownLease):
            raise TypeError("lease 必须是 CooldownLease")
        normalized_user_id = _normalize_user_id(lease.user_id)
        key = self._key(normalized_user_id)
        try:
            for _attempt in range(self._settings.operation_retries):
                async with self._client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        raw_token = await pipe.get(key)
                        if raw_token is None:
                            await self._unwatch(pipe)
                            return False
                        current_token = _decode_token(raw_token)
                        if current_token != lease.token:
                            await self._unwatch(pipe)
                            return False
                        pipe.multi()
                        pipe.delete(key)
                        results = await pipe.execute()
                        if (
                            not isinstance(results, (list, tuple))
                            or len(results) != 1
                            or not isinstance(results[0], int)
                            or isinstance(results[0], bool)
                            or results[0] not in {0, 1}
                        ):
                            raise RedisCooldownUnavailableError("Redis cooldown release 响应无效，结果未知")
                        return results[0] == 1
                    except WatchError:
                        continue
            raise RedisCooldownConflictError("Redis cooldown release 并发冲突过多，操作已拒绝")
        except asyncio.CancelledError:
            raise
        except CooldownError:
            raise
        except Exception as error:
            raise self._translate_backend_error("release", error) from None

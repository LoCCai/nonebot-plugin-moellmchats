from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import InitVar, dataclass, field
import math
import os
import re
from threading import RLock
from typing import Any
from urllib.parse import unquote, urlsplit

from redis.asyncio import Redis

_REDIS_URL_MAX_CHARS = 4_096
_REDIS_DATABASE_MAX = 65_535
_CLIENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,62}$")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

ClientFactory = Callable[..., Redis]
PidProvider = Callable[[], int]
LoopProvider = Callable[[], asyncio.AbstractEventLoop]


class RedisClientError(RuntimeError):
    """Base error for the explicit async Redis client lifecycle."""


class RedisClientInitializationError(RedisClientError):
    """The lazy Redis client and its bounded pool could not be created safely."""


class RedisClientOwnershipError(RedisClientError):
    """A Redis client was accessed from a different process or event loop."""


class RedisClientBusyError(RedisClientError):
    """A Redis client lifecycle transition is already in progress."""


class RedisClientCloseError(RedisClientError):
    """An initialized Redis client and its pool could not be closed."""


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


def _validate_seconds(
    value: object,
    *,
    label: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} 必须是 {minimum:g} 到 {maximum:g} 的有限秒数")
    return float(value)


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _parse_redis_url(redis_url: object) -> tuple[str, int]:
    if (
        not isinstance(redis_url, str)
        or not redis_url
        or len(redis_url) > _REDIS_URL_MAX_CHARS
        or _contains_control_characters(redis_url)
        or _INVALID_PERCENT_ESCAPE_RE.search(redis_url)
    ):
        raise ValueError("redis_url 必须是非空、百分号编码合法且无控制字符的有限长度字符串")
    try:
        parsed = urlsplit(redis_url)
        port = parsed.port
    except ValueError:
        raise ValueError("redis_url 不是合法 Redis URL") from None
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("redis_url 必须显式使用 redis 或 rediss scheme")
    if not parsed.hostname:
        raise ValueError("redis_url 必须指定 Redis host")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("redis_url port 必须是 1 到 65535")
    if parsed.query:
        raise ValueError("redis_url query 不得覆盖显式 Redis client settings")
    if parsed.fragment:
        raise ValueError("redis_url 不得携带 fragment")

    decoded_components = [
        unquote(component) for component in (parsed.username, parsed.password, parsed.hostname) if component is not None
    ]
    if any(_contains_control_characters(component) for component in decoded_components):
        raise ValueError("redis_url 编码后的身份或 host 不得包含控制字符")

    database_text = unquote(parsed.path[1:]) if parsed.path.startswith("/") else parsed.path
    if database_text:
        if not database_text.isascii() or not database_text.isdigit():
            raise ValueError("redis_url path 必须是单一非负 Redis database 编号")
        database = int(database_text)
    else:
        database = 0
    if not 0 <= database <= _REDIS_DATABASE_MAX:
        raise ValueError(f"redis_url database 必须是 0 到 {_REDIS_DATABASE_MAX}")
    return parsed.scheme, database


class _PrivateRedisURL:
    """Keep the URL usable without rendering its endpoint or credentials."""

    __slots__ = ("_url",)

    def __init__(self, url: str) -> None:
        self._url = url

    def __repr__(self) -> str:
        return "<redis-url:redacted>"

    def __deepcopy__(self, _memo: dict[int, object]) -> _PrivateRedisURL:
        return self

    def reveal(self) -> str:
        return self._url


@dataclass(frozen=True, repr=False)
class RedisClientSettings:
    """Validated settings for one lazy, bounded redis-py asyncio client."""

    redis_url: InitVar[str]
    max_connections: int = 50
    socket_connect_timeout_seconds: float = 5.0
    socket_timeout_seconds: float = 10.0
    health_check_interval_seconds: int = 30
    client_name: str = "nonebot-plugin-moellmchats"
    _url: _PrivateRedisURL = field(init=False, repr=False, compare=False)
    _scheme: str = field(init=False, repr=False)
    _database: int = field(init=False, repr=False)

    def __post_init__(self, redis_url: str) -> None:
        scheme, database = _parse_redis_url(redis_url)
        _validate_integer(
            self.max_connections,
            label="max_connections",
            minimum=1,
            maximum=1_000,
        )
        _validate_seconds(
            self.socket_connect_timeout_seconds,
            label="socket_connect_timeout_seconds",
            minimum=0.1,
            maximum=60.0,
        )
        _validate_seconds(
            self.socket_timeout_seconds,
            label="socket_timeout_seconds",
            minimum=0.1,
            maximum=300.0,
        )
        _validate_integer(
            self.health_check_interval_seconds,
            label="health_check_interval_seconds",
            minimum=0,
            maximum=300,
        )
        if not isinstance(self.client_name, str) or not _CLIENT_NAME_RE.fullmatch(self.client_name):
            raise ValueError("client_name 必须是 1 到 63 位安全标识")
        object.__setattr__(self, "_url", _PrivateRedisURL(redis_url))
        object.__setattr__(self, "_scheme", scheme)
        object.__setattr__(self, "_database", database)

    def __repr__(self) -> str:
        return (
            "RedisClientSettings("
            f"max_connections={self.max_connections!r}, "
            f"socket_connect_timeout_seconds={self.socket_connect_timeout_seconds!r}, "
            f"socket_timeout_seconds={self.socket_timeout_seconds!r}, "
            f"health_check_interval_seconds={self.health_check_interval_seconds!r}, "
            f"client_name={self.client_name!r}, "
            "redis_url=<redacted>)"
        )

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        """Return fresh non-secret primitives suitable for logs and health data."""

        return {
            "configured": True,
            "scheme": self._scheme,
            "tls": self._scheme == "rediss",
            "database": self._database,
            "max_connections": self.max_connections,
            "socket_connect_timeout_seconds": float(self.socket_connect_timeout_seconds),
            "socket_timeout_seconds": float(self.socket_timeout_seconds),
            "health_check_interval_seconds": self.health_check_interval_seconds,
            "client_name": self.client_name,
        }

    def client_options(self) -> dict[str, Any]:
        """Build a fresh redis-py option mapping with bounded pool and socket behavior."""

        options: dict[str, Any] = {
            "max_connections": self.max_connections,
            "socket_connect_timeout": float(self.socket_connect_timeout_seconds),
            "socket_timeout": float(self.socket_timeout_seconds),
            "socket_keepalive": True,
            "health_check_interval": self.health_check_interval_seconds,
            "encoding": "utf-8",
            "encoding_errors": "strict",
            "decode_responses": False,
            "protocol": 2,
            "client_name": self.client_name,
        }
        if self._scheme == "rediss":
            options.update(
                {
                    "ssl_cert_reqs": "required",
                    "ssl_check_hostname": True,
                }
            )
        return options


class RedisClientManager:
    """Own exactly one lazy redis-py asyncio client in one process and loop."""

    def __init__(
        self,
        settings: RedisClientSettings,
        *,
        client_factory: ClientFactory | None = None,
        pid_provider: PidProvider | None = None,
        loop_provider: LoopProvider | None = None,
    ) -> None:
        if not isinstance(settings, RedisClientSettings):
            raise TypeError("settings 必须是 RedisClientSettings")
        self._settings = settings
        self._client_factory = client_factory or Redis.from_url
        self._pid_provider = pid_provider or os.getpid
        self._loop_provider = loop_provider or asyncio.get_running_loop
        self._client: Redis | None = None
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        self._lock = RLock()

    @property
    def settings(self) -> RedisClientSettings:
        return self._settings

    @property
    def initialized(self) -> bool:
        with self._lock:
            return self._client is not None

    def __repr__(self) -> str:
        with self._lock:
            return f"RedisClientManager(initialized={self._client is not None!r}, closing={self._closing!r})"

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        with self._lock:
            lifecycle = {
                "initialized": self._client is not None,
                "closing": self._closing,
            }
        return {**self._settings.safe_diagnostics(), **lifecycle}

    def _current_owner(self) -> tuple[int, asyncio.AbstractEventLoop]:
        pid = self._pid_provider()
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RedisClientOwnershipError("Redis client 无法确认当前进程")
        try:
            loop = self._loop_provider()
        except RuntimeError:
            raise RedisClientOwnershipError("Redis client 只能在运行中的 event loop 内访问") from None
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise RedisClientOwnershipError("Redis client 无法确认当前 event loop")
        return pid, loop

    def _require_owner(
        self,
        *,
        pid: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        if self._owner_pid != pid:
            raise RedisClientOwnershipError("Redis client 不得跨进程复用；子进程必须创建独立 manager")
        if self._owner_loop is not loop:
            raise RedisClientOwnershipError("Redis client 不得跨 event loop 复用")

    def get_client(self) -> Redis:
        """Create at most one client and pool without opening a Redis connection."""

        pid, loop = self._current_owner()
        with self._lock:
            if self._closing:
                raise RedisClientBusyError("Redis client 正在关闭")
            if self._client is not None:
                self._require_owner(pid=pid, loop=loop)
                return self._client
            try:
                client = self._client_factory(
                    self._settings._url.reveal(),
                    **self._settings.client_options(),
                )
            except Exception as error:
                raise RedisClientInitializationError(f"redis-py asyncio client 初始化失败 ({type(error).__name__})") from None
            if not isinstance(client, Redis):
                raise RedisClientInitializationError("client_factory 未返回 redis-py asyncio Redis client")
            self._client = client
            self._owner_pid = pid
            self._owner_loop = loop
            return client

    async def aclose(self) -> bool:
        """Close the owned client and pool once; return False when uninitialized."""

        pid, loop = self._current_owner()
        with self._lock:
            if self._client is None:
                return False
            self._require_owner(pid=pid, loop=loop)
            if self._closing:
                raise RedisClientBusyError("Redis client 已在关闭")
            client = self._client
            self._closing = True
        try:
            await client.aclose(close_connection_pool=True)
        except asyncio.CancelledError:
            with self._lock:
                self._closing = False
            raise
        except Exception as error:
            with self._lock:
                self._closing = False
            raise RedisClientCloseError(f"redis-py asyncio client 关闭失败 ({type(error).__name__})") from None
        with self._lock:
            self._client = None
            self._owner_pid = None
            self._owner_loop = None
            self._closing = False
        return True

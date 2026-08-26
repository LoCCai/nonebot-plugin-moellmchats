from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import InitVar, dataclass, field
import math
import os
import re
from threading import RLock
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_DATABASE_URL_MAX_CHARS = 4_096
_APPLICATION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,62}$")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "pass",
        "passwd",
        "password",
        "secret",
        "token",
    }
)

EngineFactory = Callable[..., AsyncEngine]
PidProvider = Callable[[], int]
LoopProvider = Callable[[], asyncio.AbstractEventLoop]


class DatabaseEngineError(RuntimeError):
    """Base error for the explicit async database engine lifecycle."""


class DatabaseEngineInitializationError(DatabaseEngineError):
    """The lazy SQLAlchemy engine could not be created safely."""


class DatabaseEngineOwnershipError(DatabaseEngineError):
    """An engine was accessed from a different process or event loop."""


class DatabaseEngineBusyError(DatabaseEngineError):
    """An engine lifecycle transition is already in progress."""


class DatabaseEngineDisposalError(DatabaseEngineError):
    """An initialized engine could not be disposed."""


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


def _parse_database_url(database_url: object) -> URL:
    if (
        not isinstance(database_url, str)
        or not database_url
        or len(database_url) > _DATABASE_URL_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in database_url)
    ):
        raise ValueError("database_url 必须是非空且无控制字符的有限长度字符串")
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise ValueError("database_url 不是合法 SQLAlchemy URL") from None
    if url.drivername != "postgresql+asyncpg":
        raise ValueError("database_url 必须显式使用 postgresql+asyncpg driver")
    if not isinstance(url.database, str) or not url.database.strip():
        raise ValueError("database_url 必须指定数据库名")
    sensitive_keys = sorted(
        key for key in url.query if re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") in _SENSITIVE_QUERY_KEYS
    )
    if sensitive_keys:
        raise ValueError("database_url query 不得携带敏感凭据字段: " + ", ".join(sensitive_keys))
    return url


class _PrivateDatabaseURL:
    """Keep the parsed URL usable without rendering its endpoint or credentials."""

    __slots__ = ("_url",)

    def __init__(self, url: URL) -> None:
        self._url = url

    def __repr__(self) -> str:
        return "<database-url:redacted>"

    def __deepcopy__(self, _memo: dict[int, object]) -> _PrivateDatabaseURL:
        return self

    @property
    def drivername(self) -> str:
        return self._url.drivername

    def reveal(self) -> URL:
        return self._url


@dataclass(frozen=True, repr=False)
class DatabaseEngineSettings:
    """Validated settings for one lazy, bounded SQLAlchemy async engine."""

    database_url: InitVar[str]
    pool_size: int = 10
    max_overflow: int = 20
    pool_timeout_seconds: float = 30.0
    pool_recycle_seconds: int = 1_800
    connect_timeout_seconds: float = 10.0
    statement_timeout_seconds: float = 30.0
    application_name: str = "nonebot-plugin-moellmchats"
    _url: _PrivateDatabaseURL = field(init=False, repr=False, compare=False)

    def __post_init__(self, database_url: str) -> None:
        url = _parse_database_url(database_url)
        pool_size = _validate_integer(
            self.pool_size,
            label="pool_size",
            minimum=1,
            maximum=100,
        )
        max_overflow = _validate_integer(
            self.max_overflow,
            label="max_overflow",
            minimum=0,
            maximum=100,
        )
        if pool_size + max_overflow > 150:
            raise ValueError("pool_size + max_overflow 不得超过 150")
        _validate_seconds(
            self.pool_timeout_seconds,
            label="pool_timeout_seconds",
            minimum=0.1,
            maximum=300.0,
        )
        _validate_integer(
            self.pool_recycle_seconds,
            label="pool_recycle_seconds",
            minimum=30,
            maximum=86_400,
        )
        _validate_seconds(
            self.connect_timeout_seconds,
            label="connect_timeout_seconds",
            minimum=0.1,
            maximum=60.0,
        )
        _validate_seconds(
            self.statement_timeout_seconds,
            label="statement_timeout_seconds",
            minimum=0.1,
            maximum=3_600.0,
        )
        if not isinstance(self.application_name, str) or not _APPLICATION_NAME_RE.fullmatch(self.application_name):
            raise ValueError("application_name 必须是 1 到 63 位安全标识")
        object.__setattr__(self, "_url", _PrivateDatabaseURL(url))

    def __repr__(self) -> str:
        return (
            "DatabaseEngineSettings("
            f"pool_size={self.pool_size!r}, "
            f"max_overflow={self.max_overflow!r}, "
            f"pool_timeout_seconds={self.pool_timeout_seconds!r}, "
            f"pool_recycle_seconds={self.pool_recycle_seconds!r}, "
            f"connect_timeout_seconds={self.connect_timeout_seconds!r}, "
            f"statement_timeout_seconds={self.statement_timeout_seconds!r}, "
            f"application_name={self.application_name!r}, "
            "database_url=<redacted>)"
        )

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        """Return fresh non-secret primitives suitable for logs and health data."""

        return {
            "configured": True,
            "driver": self._url.drivername,
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
            "pool_timeout_seconds": float(self.pool_timeout_seconds),
            "pool_recycle_seconds": self.pool_recycle_seconds,
            "connect_timeout_seconds": float(self.connect_timeout_seconds),
            "statement_timeout_seconds": float(self.statement_timeout_seconds),
            "application_name": self.application_name,
        }

    def engine_options(self) -> dict[str, Any]:
        """Build a fresh SQLAlchemy option tree without rendering the URL."""

        statement_timeout_ms = int(float(self.statement_timeout_seconds) * 1_000)
        return {
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
            "pool_timeout": float(self.pool_timeout_seconds),
            "pool_recycle": self.pool_recycle_seconds,
            "pool_pre_ping": True,
            "pool_use_lifo": True,
            "echo": False,
            "hide_parameters": True,
            "connect_args": {
                "timeout": float(self.connect_timeout_seconds),
                "command_timeout": float(self.statement_timeout_seconds),
                "server_settings": {
                    "application_name": self.application_name,
                    "statement_timeout": str(statement_timeout_ms),
                },
            },
        }


class DatabaseEngineManager:
    """Own exactly one lazy AsyncEngine in one process and event loop."""

    def __init__(
        self,
        settings: DatabaseEngineSettings,
        *,
        engine_factory: EngineFactory | None = None,
        pid_provider: PidProvider | None = None,
        loop_provider: LoopProvider | None = None,
    ) -> None:
        if not isinstance(settings, DatabaseEngineSettings):
            raise TypeError("settings 必须是 DatabaseEngineSettings")
        self._settings = settings
        self._engine_factory = engine_factory or create_async_engine
        self._pid_provider = pid_provider or os.getpid
        self._loop_provider = loop_provider or asyncio.get_running_loop
        self._engine: AsyncEngine | None = None
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._disposing = False
        self._lock = RLock()

    @property
    def settings(self) -> DatabaseEngineSettings:
        return self._settings

    @property
    def initialized(self) -> bool:
        with self._lock:
            return self._engine is not None

    def __repr__(self) -> str:
        with self._lock:
            return f"DatabaseEngineManager(initialized={self._engine is not None!r}, disposing={self._disposing!r})"

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        with self._lock:
            lifecycle = {
                "initialized": self._engine is not None,
                "disposing": self._disposing,
            }
        return {**self._settings.safe_diagnostics(), **lifecycle}

    def _current_owner(self) -> tuple[int, asyncio.AbstractEventLoop]:
        pid = self._pid_provider()
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise DatabaseEngineOwnershipError("数据库 engine 无法确认当前进程")
        try:
            loop = self._loop_provider()
        except RuntimeError:
            raise DatabaseEngineOwnershipError("数据库 engine 只能在运行中的 event loop 内访问") from None
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise DatabaseEngineOwnershipError("数据库 engine 无法确认当前 event loop")
        return pid, loop

    def _require_owner(
        self,
        *,
        pid: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        if self._owner_pid != pid:
            raise DatabaseEngineOwnershipError("数据库 engine 不得跨进程复用；子进程必须创建独立 manager")
        if self._owner_loop is not loop:
            raise DatabaseEngineOwnershipError("数据库 engine 不得跨 event loop 复用")

    def get_engine(self) -> AsyncEngine:
        """Create at most one engine; SQL connections remain lazy."""

        pid, loop = self._current_owner()
        with self._lock:
            if self._disposing:
                raise DatabaseEngineBusyError("数据库 engine 正在释放")
            if self._engine is not None:
                self._require_owner(pid=pid, loop=loop)
                return self._engine
            try:
                engine = self._engine_factory(
                    self._settings._url.reveal(),
                    **self._settings.engine_options(),
                )
            except Exception as error:
                raise DatabaseEngineInitializationError(f"SQLAlchemy async engine 初始化失败 ({type(error).__name__})") from None
            if not isinstance(engine, AsyncEngine):
                raise DatabaseEngineInitializationError("engine_factory 未返回 SQLAlchemy AsyncEngine")
            self._engine = engine
            self._owner_pid = pid
            self._owner_loop = loop
            return engine

    async def dispose(self) -> bool:
        """Dispose the owned pool once; return False when never initialized."""

        pid, loop = self._current_owner()
        with self._lock:
            if self._engine is None:
                return False
            self._require_owner(pid=pid, loop=loop)
            if self._disposing:
                raise DatabaseEngineBusyError("数据库 engine 已在释放")
            engine = self._engine
            self._disposing = True
        try:
            await engine.dispose()
        except asyncio.CancelledError:
            with self._lock:
                self._disposing = False
            raise
        except Exception as error:
            with self._lock:
                self._disposing = False
            raise DatabaseEngineDisposalError(f"SQLAlchemy async engine 释放失败 ({type(error).__name__})") from None
        with self._lock:
            self._engine = None
            self._owner_pid = None
            self._owner_loop = None
            self._disposing = False
        return True

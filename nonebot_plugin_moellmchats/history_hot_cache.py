from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import secrets
import time
from typing import Protocol, runtime_checkable

from .chat_history import MessageRecord, mutable_history_json, validate_conversation_id

_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_GENERATION_ATTEMPTS = 32
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

Clock = Callable[[], float]
GenerationFactory = Callable[[], str]
LoopProvider = Callable[[], asyncio.AbstractEventLoop]
PidProvider = Callable[[], int]


class HistoryHotCacheError(RuntimeError):
    """Base error for a replaceable recent-history cache."""


class HistoryHotCacheUnavailableError(HistoryHotCacheError):
    """The cache could not establish a trustworthy result."""


class HistoryHotCacheOwnershipError(HistoryHotCacheError):
    """A process-local cache was accessed by a different process or loop."""


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


def _conversation_fingerprint(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()


def _new_generation(factory: GenerationFactory) -> str:
    for _attempt in range(_GENERATION_ATTEMPTS):
        try:
            generation = factory()
        except Exception as error:
            raise HistoryHotCacheUnavailableError(f"history hot cache 无法生成失效代际 ({type(error).__name__})") from None
        if isinstance(generation, str) and _GENERATION_RE.fullmatch(generation):
            return generation
    raise HistoryHotCacheUnavailableError("history hot cache 无法生成有效失效代际")


def _read_clock(clock: Clock) -> float:
    try:
        value = clock()
    except Exception as error:
        raise HistoryHotCacheUnavailableError(f"history hot cache 时钟不可用 ({type(error).__name__})") from None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise HistoryHotCacheUnavailableError("history hot cache 时钟返回无效值")
    return float(value)


def _validate_limit(value: object, *, maximum: int) -> int:
    return _validate_integer(
        value,
        label="history hot cache limit",
        minimum=1,
        maximum=maximum,
    )


def _window_payload_bytes(window: HistoryWindow) -> int:
    payload = {
        "conversation_id": window.conversation_id,
        "has_older": window.has_older,
        "messages": [
            {
                "content": message.content,
                "created_at": message.created_at.isoformat(timespec="microseconds"),
                "id": message.message_id,
                "platform_message_id": message.platform_message_id,
                "role": message.role,
                "sender_id": message.sender_id,
                "structured_content": mutable_history_json(message.structured_content),
            }
            for message in window.messages
        ],
    }
    try:
        return len(
            json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
    except Exception as error:
        raise HistoryHotCacheUnavailableError(f"history hot cache window 无法计算安全大小 ({type(error).__name__})") from None


@dataclass(frozen=True)
class HistoryWindow:
    """An immutable, ascending window of committed messages from one conversation."""

    conversation_id: str
    messages: tuple[MessageRecord, ...]
    has_older: bool

    def __post_init__(self) -> None:
        validate_conversation_id(self.conversation_id)
        if not isinstance(self.messages, tuple):
            raise ValueError("HistoryWindow.messages 必须是元组")
        if not isinstance(self.has_older, bool):
            raise ValueError("HistoryWindow.has_older 必须是布尔值")
        if not self.messages and self.has_older:
            raise ValueError("空 HistoryWindow 不得声明存在更早消息")

        previous_id = 0
        for message in self.messages:
            if not isinstance(message, MessageRecord):
                raise ValueError("HistoryWindow.messages 只能包含 MessageRecord")
            if message.conversation_id != self.conversation_id:
                raise ValueError("HistoryWindow.messages 必须属于同一会话")
            message_id = message.message_id
            if not isinstance(message_id, int) or isinstance(message_id, bool) or not 1 <= message_id <= _POSTGRES_BIGINT_MAX:
                raise ValueError("HistoryWindow 只能缓存已持久化消息")
            if message_id <= previous_id:
                raise ValueError("HistoryWindow.messages 必须按 message_id 严格递增")
            previous_id = message_id

    @property
    def oldest_message_id(self) -> int | None:
        return None if not self.messages else self.messages[0].message_id

    @property
    def newest_message_id(self) -> int | None:
        return None if not self.messages else self.messages[-1].message_id

    def recent(self, limit: int) -> HistoryWindow:
        """Return the newest bounded suffix without weakening older-history metadata."""

        normalized_limit = _validate_integer(
            limit,
            label="HistoryWindow.recent limit",
            minimum=1,
            maximum=200,
        )
        if len(self.messages) <= normalized_limit:
            return self
        return HistoryWindow(
            conversation_id=self.conversation_id,
            messages=self.messages[-normalized_limit:],
            has_older=True,
        )


@dataclass(frozen=True, repr=False)
class HistoryCacheLoadToken:
    """Opaque proof that a cache miss may still publish into one reserved generation."""

    conversation_fingerprint: str
    generation: str
    expires_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", self.conversation_fingerprint):
            raise ValueError("conversation_fingerprint 必须是 SHA-256")
        if not isinstance(self.generation, str) or not _GENERATION_RE.fullmatch(self.generation):
            raise ValueError("generation 必须是 128-bit 小写十六进制失效代际")
        if (
            not isinstance(self.expires_at, (int, float))
            or isinstance(self.expires_at, bool)
            or not math.isfinite(self.expires_at)
        ):
            raise ValueError("expires_at 必须是有限时间")

    def __repr__(self) -> str:
        return "HistoryCacheLoadToken(<redacted>)"


@dataclass(frozen=True)
class HistoryCacheLookup:
    """Exactly one cache hit or a bounded token authorizing a source reload."""

    window: HistoryWindow | None = None
    load_token: HistoryCacheLoadToken | None = None

    def __post_init__(self) -> None:
        if (self.window is None) == (self.load_token is None):
            raise ValueError("HistoryCacheLookup 必须且只能包含 window 或 load_token")
        if self.window is not None and not isinstance(self.window, HistoryWindow):
            raise ValueError("HistoryCacheLookup.window 必须是 HistoryWindow")
        if self.load_token is not None and not isinstance(self.load_token, HistoryCacheLoadToken):
            raise ValueError("HistoryCacheLookup.load_token 必须是 HistoryCacheLoadToken")

    @property
    def hit(self) -> bool:
        return self.window is not None


@runtime_checkable
class HistoryHotCacheProtocol(Protocol):
    """Backend-neutral cache contract; PostgreSQL remains the source of truth."""

    async def lookup(
        self,
        conversation_id: str,
        *,
        limit: int,
    ) -> HistoryCacheLookup: ...

    async def publish(
        self,
        load_token: HistoryCacheLoadToken,
        window: HistoryWindow,
    ) -> bool:
        """Publish a window only after the caller established a committed source view."""
        ...

    async def invalidate(self, conversation_id: str) -> None:
        """Invalidate only after a durable source write commits successfully."""
        ...


@dataclass(frozen=True)
class MemoryHistoryHotCacheSettings:
    """Bounded process-local history cache policy."""

    ttl_seconds: float = 600.0
    load_timeout_seconds: float = 30.0
    max_conversations: int = 1_000
    max_messages: int = 200
    max_payload_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        ttl_seconds = _validate_seconds(
            self.ttl_seconds,
            label="ttl_seconds",
            minimum=1.0,
            maximum=86_400.0,
        )
        load_timeout_seconds = _validate_seconds(
            self.load_timeout_seconds,
            label="load_timeout_seconds",
            minimum=0.1,
            maximum=300.0,
        )
        if load_timeout_seconds > ttl_seconds:
            raise ValueError("load_timeout_seconds 不得超过 ttl_seconds")
        _validate_integer(
            self.max_conversations,
            label="max_conversations",
            minimum=1,
            maximum=100_000,
        )
        _validate_integer(
            self.max_messages,
            label="max_messages",
            minimum=1,
            maximum=200,
        )
        _validate_integer(
            self.max_payload_bytes,
            label="max_payload_bytes",
            minimum=1_024,
            maximum=16_777_216,
        )
        object.__setattr__(self, "ttl_seconds", ttl_seconds)
        object.__setattr__(self, "load_timeout_seconds", load_timeout_seconds)

    def safe_diagnostics(self) -> dict[str, float | int]:
        return {
            "ttl_seconds": self.ttl_seconds,
            "load_timeout_seconds": self.load_timeout_seconds,
            "max_conversations": self.max_conversations,
            "max_messages": self.max_messages,
            "max_payload_bytes": self.max_payload_bytes,
        }


@dataclass(frozen=True)
class _MemoryCacheState:
    generation: str
    window: HistoryWindow | None
    expires_at: float


class MemoryHistoryHotCache:
    """TTL/LRU recent-history cache with generation-bound late-load rejection."""

    def __init__(
        self,
        *,
        settings: MemoryHistoryHotCacheSettings | None = None,
        clock: Clock | None = None,
        generation_factory: GenerationFactory | None = None,
        pid_provider: PidProvider | None = None,
        loop_provider: LoopProvider | None = None,
    ) -> None:
        if settings is not None and not isinstance(settings, MemoryHistoryHotCacheSettings):
            raise TypeError("settings 必须是 MemoryHistoryHotCacheSettings")
        for dependency, label in (
            (clock, "clock"),
            (generation_factory, "generation_factory"),
            (pid_provider, "pid_provider"),
            (loop_provider, "loop_provider"),
        ):
            if dependency is not None and not callable(dependency):
                raise TypeError(f"{label} 必须可调用")
        self._settings = MemoryHistoryHotCacheSettings() if settings is None else settings
        self._clock = clock or time.monotonic
        self._generation_factory = generation_factory or (lambda: secrets.token_hex(16))
        self._pid_provider = pid_provider or os.getpid
        self._loop_provider = loop_provider or asyncio.get_running_loop
        self._states: OrderedDict[str, _MemoryCacheState] = OrderedDict()
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> MemoryHistoryHotCacheSettings:
        return self._settings

    def __repr__(self) -> str:
        return f"MemoryHistoryHotCache(entries={len(self._states)!r})"

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        return {
            "backend": "memory",
            "configured": True,
            **self._settings.safe_diagnostics(),
        }

    def _require_owner(self) -> None:
        try:
            pid = self._pid_provider()
            loop = self._loop_provider()
        except RuntimeError:
            raise HistoryHotCacheOwnershipError("Memory history hot cache 只能在运行中的 event loop 内访问") from None
        except Exception as error:
            raise HistoryHotCacheOwnershipError(f"Memory history hot cache 无法确认 owner ({type(error).__name__})") from None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise HistoryHotCacheOwnershipError("Memory history hot cache 无法确认当前进程")
        if not isinstance(loop, asyncio.AbstractEventLoop):
            raise HistoryHotCacheOwnershipError("Memory history hot cache 无法确认当前 event loop")
        if self._owner_pid is None:
            self._owner_pid = pid
            self._owner_loop = loop
            return
        if self._owner_pid != pid:
            raise HistoryHotCacheOwnershipError("Memory history hot cache 不得跨进程复用")
        if self._owner_loop is not loop:
            raise HistoryHotCacheOwnershipError("Memory history hot cache 不得跨 event loop 复用")

    def _prune_locked(self, now: float) -> None:
        for fingerprint in [fingerprint for fingerprint, state in self._states.items() if now >= state.expires_at]:
            self._states.pop(fingerprint, None)

    def _evict_locked(self) -> None:
        while len(self._states) > self._settings.max_conversations:
            self._states.popitem(last=False)

    def _token(
        self,
        fingerprint: str,
        state: _MemoryCacheState,
    ) -> HistoryCacheLoadToken:
        return HistoryCacheLoadToken(
            conversation_fingerprint=fingerprint,
            generation=state.generation,
            expires_at=state.expires_at,
        )

    async def lookup(
        self,
        conversation_id: str,
        *,
        limit: int,
    ) -> HistoryCacheLookup:
        conversation_id = validate_conversation_id(conversation_id)
        normalized_limit = _validate_limit(limit, maximum=self._settings.max_messages)
        self._require_owner()
        async with self._lock:
            now = _read_clock(self._clock)
            self._prune_locked(now)
            fingerprint = _conversation_fingerprint(conversation_id)
            state = self._states.get(fingerprint)
            if state is None:
                state = _MemoryCacheState(
                    generation=_new_generation(self._generation_factory),
                    window=None,
                    expires_at=now + self._settings.load_timeout_seconds,
                )
                self._states[fingerprint] = state
                self._evict_locked()
            else:
                self._states.move_to_end(fingerprint)
            if state.window is None:
                return HistoryCacheLookup(load_token=self._token(fingerprint, state))
            return HistoryCacheLookup(window=state.window.recent(normalized_limit))

    async def publish(
        self,
        load_token: HistoryCacheLoadToken,
        window: HistoryWindow,
    ) -> bool:
        if not isinstance(load_token, HistoryCacheLoadToken):
            raise TypeError("load_token 必须是 HistoryCacheLoadToken")
        if not isinstance(window, HistoryWindow):
            raise TypeError("window 必须是 HistoryWindow")
        if len(window.messages) > self._settings.max_messages:
            raise ValueError("HistoryWindow.messages 超过 cache max_messages")
        if _window_payload_bytes(window) > self._settings.max_payload_bytes:
            raise HistoryHotCacheUnavailableError("Memory history hot cache window 超过安全大小限制")
        if _conversation_fingerprint(window.conversation_id) != load_token.conversation_fingerprint:
            raise ValueError("load_token 与 HistoryWindow 会话不匹配")
        self._require_owner()
        async with self._lock:
            now = _read_clock(self._clock)
            if now >= load_token.expires_at:
                return False
            self._prune_locked(now)
            state = self._states.get(load_token.conversation_fingerprint)
            if (
                state is None
                or state.window is not None
                or state.generation != load_token.generation
                or state.expires_at != load_token.expires_at
            ):
                return False
            self._states[load_token.conversation_fingerprint] = _MemoryCacheState(
                generation=state.generation,
                window=window,
                expires_at=now + self._settings.ttl_seconds,
            )
            self._states.move_to_end(load_token.conversation_fingerprint)
            self._evict_locked()
            return True

    async def invalidate(self, conversation_id: str) -> None:
        conversation_id = validate_conversation_id(conversation_id)
        self._require_owner()
        fingerprint = _conversation_fingerprint(conversation_id)
        async with self._lock:
            now = _read_clock(self._clock)
            state = _MemoryCacheState(
                generation=_new_generation(self._generation_factory),
                window=None,
                expires_at=now + self._settings.load_timeout_seconds,
            )
            self._prune_locked(now)
            self._states[fingerprint] = state
            self._states.move_to_end(fingerprint)
            self._evict_locked()

    async def clear(self) -> None:
        """Discard all process-local state; outstanding load tokens become invalid."""

        self._require_owner()
        async with self._lock:
            self._states.clear()

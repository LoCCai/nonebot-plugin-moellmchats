from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import secrets
import time
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .chat_history import MessageRecord, mutable_history_json, validate_conversation_id
from .history_hot_cache import (
    Clock,
    GenerationFactory,
    HistoryCacheLoadToken,
    HistoryCacheLookup,
    HistoryHotCacheError,
    HistoryHotCacheUnavailableError,
    HistoryWindow,
    _conversation_fingerprint,
    _new_generation,
    _read_clock,
    _validate_integer,
    _validate_limit,
    _validate_seconds,
)

_KEY_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_UTC_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_WIRE_VERSION = 1


class RedisHistoryHotCacheUnavailableError(HistoryHotCacheUnavailableError):
    """Redis could not establish a trustworthy cache result."""


class RedisHistoryHotCacheConflictError(HistoryHotCacheError):
    """A bounded Redis reservation retry budget was exhausted."""


@dataclass(frozen=True)
class RedisHistoryHotCacheSettings:
    """Explicit bounded settings for one Redis recent-history namespace."""

    key_prefix: str = "moellm"
    ttl_seconds: float = 600.0
    load_timeout_seconds: float = 30.0
    max_messages: int = 200
    max_payload_bytes: int = 8_388_608
    operation_retries: int = 32

    def __post_init__(self) -> None:
        if not isinstance(self.key_prefix, str) or not _KEY_PREFIX_RE.fullmatch(self.key_prefix):
            raise ValueError("key_prefix 必须是 1 到 96 位安全 Redis key 前缀")
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
        _validate_integer(
            self.operation_retries,
            label="operation_retries",
            minimum=1,
            maximum=64,
        )
        object.__setattr__(self, "ttl_seconds", ttl_seconds)
        object.__setattr__(self, "load_timeout_seconds", load_timeout_seconds)

    @property
    def ttl_milliseconds(self) -> int:
        return int(self.ttl_seconds * 1_000)

    @property
    def load_timeout_milliseconds(self) -> int:
        return int(self.load_timeout_seconds * 1_000)

    def safe_diagnostics(self) -> dict[str, float | int | str]:
        return {
            "key_prefix": self.key_prefix,
            "ttl_seconds": self.ttl_seconds,
            "load_timeout_seconds": self.load_timeout_seconds,
            "max_messages": self.max_messages,
            "max_payload_bytes": self.max_payload_bytes,
            "operation_retries": self.operation_retries,
        }


@dataclass(frozen=True)
class _RedisCacheState:
    conversation_fingerprint: str
    generation: str
    window: HistoryWindow | None


def _datetime_to_wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime_from_wire(value: object) -> datetime:
    if not isinstance(value, str) or not _UTC_DATETIME_RE.fullmatch(value):
        raise ValueError("created_at is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("created_at is invalid") from None
    if _datetime_to_wire(parsed) != value:
        raise ValueError("created_at is not canonical UTC")
    return parsed


def _message_to_wire(message: MessageRecord) -> dict[str, Any]:
    return {
        "content": message.content,
        "created_at": _datetime_to_wire(message.created_at),
        "id": message.message_id,
        "platform_message_id": message.platform_message_id,
        "role": message.role,
        "sender_id": message.sender_id,
        "structured_content": mutable_history_json(message.structured_content),
    }


def _message_from_wire(
    payload: object,
    *,
    conversation_id: str,
) -> MessageRecord:
    expected_keys = {
        "content",
        "created_at",
        "id",
        "platform_message_id",
        "role",
        "sender_id",
        "structured_content",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_keys:
        raise ValueError("message payload shape is invalid")
    return MessageRecord(
        message_id=payload["id"],
        conversation_id=conversation_id,
        platform_message_id=payload["platform_message_id"],
        role=payload["role"],
        sender_id=payload["sender_id"],
        content=payload["content"],
        structured_content=payload["structured_content"],
        created_at=_datetime_from_wire(payload["created_at"]),
    )


def _wire_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _loading_payload(
    *,
    conversation_fingerprint: str,
    generation: str,
) -> dict[str, Any]:
    return {
        "conversation": conversation_fingerprint,
        "generation": generation,
        "kind": "loading",
        "version": _WIRE_VERSION,
    }


def _ready_payload(
    *,
    conversation_fingerprint: str,
    generation: str,
    window: HistoryWindow,
) -> dict[str, Any]:
    return {
        "conversation": conversation_fingerprint,
        "generation": generation,
        "has_older": window.has_older,
        "kind": "ready",
        "messages": [_message_to_wire(message) for message in window.messages],
        "version": _WIRE_VERSION,
    }


def _encode_payload(
    payload: Mapping[str, Any],
    *,
    maximum_bytes: int,
) -> bytes:
    try:
        encoded = _wire_bytes(payload)
    except Exception as error:
        raise RedisHistoryHotCacheUnavailableError(f"Redis history hot cache payload 无法编码 ({type(error).__name__})") from None
    if len(encoded) > maximum_bytes:
        raise RedisHistoryHotCacheUnavailableError("Redis history hot cache payload 超过安全大小限制")
    return encoded


def _raw_payload_bytes(raw: object, *, maximum_bytes: int) -> bytes:
    if isinstance(raw, bytes):
        encoded = raw
    elif isinstance(raw, str):
        try:
            encoded = raw.encode("ascii", errors="strict")
        except UnicodeEncodeError:
            raise RedisHistoryHotCacheUnavailableError("Redis history hot cache payload 编码无效") from None
    else:
        raise RedisHistoryHotCacheUnavailableError("Redis history hot cache payload 类型无效")
    if len(encoded) > maximum_bytes:
        raise RedisHistoryHotCacheUnavailableError("Redis history hot cache payload 超过安全大小限制")
    return encoded


def _decode_state(
    raw: object,
    *,
    conversation_id: str,
    expected_fingerprint: str,
    maximum_bytes: int,
    maximum_messages: int,
) -> _RedisCacheState:
    encoded = _raw_payload_bytes(raw, maximum_bytes=maximum_bytes)
    try:
        payload = json.loads(encoded.decode("ascii", errors="strict"))
        if not isinstance(payload, Mapping) or _wire_bytes(payload) != encoded:
            raise ValueError("payload is not canonical")
        kind = payload.get("kind")
        common_keys = {"conversation", "generation", "kind", "version"}
        expected_keys = common_keys if kind == "loading" else common_keys | {"has_older", "messages"}
        if set(payload) != expected_keys:
            raise ValueError("payload shape is invalid")
        fingerprint = payload["conversation"]
        generation = payload["generation"]
        if type(payload["version"]) is not int or payload["version"] != _WIRE_VERSION or kind not in {"loading", "ready"}:
            raise ValueError("payload version or kind is invalid")
        if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint) or fingerprint != expected_fingerprint:
            raise ValueError("payload conversation is invalid")
        if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
            raise ValueError("payload generation is invalid")
        if kind == "loading":
            return _RedisCacheState(
                conversation_fingerprint=fingerprint,
                generation=generation,
                window=None,
            )
        messages_payload = payload["messages"]
        if not isinstance(messages_payload, list) or len(messages_payload) > maximum_messages:
            raise ValueError("payload messages are invalid")
        has_older = payload["has_older"]
        if not isinstance(has_older, bool):
            raise ValueError("payload has_older is invalid")
        window = HistoryWindow(
            conversation_id=conversation_id,
            messages=tuple(_message_from_wire(message, conversation_id=conversation_id) for message in messages_payload),
            has_older=has_older,
        )
    except HistoryHotCacheError:
        raise
    except Exception as error:
        raise RedisHistoryHotCacheUnavailableError(f"Redis history hot cache payload 无法验证 ({type(error).__name__})") from None
    return _RedisCacheState(
        conversation_fingerprint=expected_fingerprint,
        generation=generation,
        window=window,
    )


class RedisHistoryHotCache:
    """Redis-backed history window cache with explicit client injection and CAS publish."""

    def __init__(
        self,
        client: Redis,
        *,
        settings: RedisHistoryHotCacheSettings | None = None,
        clock: Clock | None = None,
        generation_factory: GenerationFactory | None = None,
    ) -> None:
        if not isinstance(client, Redis):
            raise TypeError("client 必须是 redis-py asyncio Redis client")
        if settings is not None and not isinstance(settings, RedisHistoryHotCacheSettings):
            raise TypeError("settings 必须是 RedisHistoryHotCacheSettings")
        if clock is not None and not callable(clock):
            raise TypeError("clock 必须可调用")
        if generation_factory is not None and not callable(generation_factory):
            raise TypeError("generation_factory 必须可调用")
        self._client = client
        self._settings = RedisHistoryHotCacheSettings() if settings is None else settings
        self._clock = clock or time.monotonic
        self._generation_factory = generation_factory or (lambda: secrets.token_hex(16))

    @property
    def settings(self) -> RedisHistoryHotCacheSettings:
        return self._settings

    def __repr__(self) -> str:
        return (
            "RedisHistoryHotCache("
            f"key_prefix={self._settings.key_prefix!r}, "
            f"ttl_seconds={self._settings.ttl_seconds!r}, "
            f"max_messages={self._settings.max_messages!r})"
        )

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        return {
            "backend": "redis",
            "configured": True,
            **self._settings.safe_diagnostics(),
        }

    def _key_from_fingerprint(self, fingerprint: str) -> str:
        return f"{self._settings.key_prefix}:history:{{{fingerprint}}}"

    def _key(self, conversation_id: str) -> tuple[str, str]:
        fingerprint = _conversation_fingerprint(conversation_id)
        return self._key_from_fingerprint(fingerprint), fingerprint

    @staticmethod
    async def _unwatch(pipe: Any) -> None:
        await pipe.unwatch()

    @staticmethod
    def _translate_backend_error(
        operation: str,
        error: Exception,
    ) -> RedisHistoryHotCacheUnavailableError:
        return RedisHistoryHotCacheUnavailableError(f"Redis history hot cache {operation} 结果未知 ({type(error).__name__})")

    def _loading_bytes(self, *, fingerprint: str, generation: str) -> bytes:
        return _encode_payload(
            _loading_payload(
                conversation_fingerprint=fingerprint,
                generation=generation,
            ),
            maximum_bytes=self._settings.max_payload_bytes,
        )

    def _ready_bytes(
        self,
        *,
        token: HistoryCacheLoadToken,
        window: HistoryWindow,
    ) -> bytes:
        return _encode_payload(
            _ready_payload(
                conversation_fingerprint=token.conversation_fingerprint,
                generation=token.generation,
                window=window,
            ),
            maximum_bytes=self._settings.max_payload_bytes,
        )

    def _decode(
        self,
        raw: object,
        *,
        conversation_id: str,
        fingerprint: str,
    ) -> _RedisCacheState:
        return _decode_state(
            raw,
            conversation_id=conversation_id,
            expected_fingerprint=fingerprint,
            maximum_bytes=self._settings.max_payload_bytes,
            maximum_messages=self._settings.max_messages,
        )

    async def lookup(
        self,
        conversation_id: str,
        *,
        limit: int,
    ) -> HistoryCacheLookup:
        conversation_id = validate_conversation_id(conversation_id)
        normalized_limit = _validate_limit(limit, maximum=self._settings.max_messages)
        key, fingerprint = self._key(conversation_id)
        try:
            for _attempt in range(self._settings.operation_retries):
                now = _read_clock(self._clock)
                raw = await self._client.get(key)
                if raw is None:
                    generation = _new_generation(self._generation_factory)
                    created = await self._client.set(
                        key,
                        self._loading_bytes(
                            fingerprint=fingerprint,
                            generation=generation,
                        ),
                        nx=True,
                        px=self._settings.load_timeout_milliseconds,
                    )
                    if created is True:
                        return HistoryCacheLookup(
                            load_token=HistoryCacheLoadToken(
                                conversation_fingerprint=fingerprint,
                                generation=generation,
                                expires_at=now + self._settings.load_timeout_seconds,
                            )
                        )
                    if created is None:
                        continue
                    raise RedisHistoryHotCacheUnavailableError("Redis history hot cache reserve 响应无效")

                state = self._decode(
                    raw,
                    conversation_id=conversation_id,
                    fingerprint=fingerprint,
                )
                remaining_milliseconds = await self._client.pttl(key)
                if not isinstance(remaining_milliseconds, int) or isinstance(remaining_milliseconds, bool):
                    raise RedisHistoryHotCacheUnavailableError("Redis history hot cache PTTL 响应无效")
                if remaining_milliseconds in {-2, 0}:
                    continue
                if remaining_milliseconds == -1:
                    raise RedisHistoryHotCacheUnavailableError("Redis history hot cache key 缺少 TTL")
                if remaining_milliseconds < -2:
                    raise RedisHistoryHotCacheUnavailableError("Redis history hot cache PTTL 响应无效")
                maximum_ttl = (
                    self._settings.load_timeout_milliseconds if state.window is None else self._settings.ttl_milliseconds
                )
                if remaining_milliseconds > maximum_ttl:
                    raise RedisHistoryHotCacheUnavailableError("Redis history hot cache TTL 超过安全上限")
                if state.window is not None:
                    return HistoryCacheLookup(window=state.window.recent(normalized_limit))
                return HistoryCacheLookup(
                    load_token=HistoryCacheLoadToken(
                        conversation_fingerprint=fingerprint,
                        generation=state.generation,
                        expires_at=now
                        + min(
                            self._settings.load_timeout_seconds,
                            remaining_milliseconds / 1_000,
                        ),
                    )
                )
            raise RedisHistoryHotCacheConflictError("Redis history hot cache lookup 并发冲突过多")
        except asyncio.CancelledError:
            raise
        except HistoryHotCacheError:
            raise
        except Exception as error:
            raise self._translate_backend_error("lookup", error) from None

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
        if _conversation_fingerprint(window.conversation_id) != load_token.conversation_fingerprint:
            raise ValueError("load_token 与 HistoryWindow 会话不匹配")
        if _read_clock(self._clock) >= load_token.expires_at:
            return False
        key = self._key_from_fingerprint(load_token.conversation_fingerprint)
        ready = self._ready_bytes(token=load_token, window=window)
        try:
            for _attempt in range(self._settings.operation_retries):
                if _read_clock(self._clock) >= load_token.expires_at:
                    return False
                async with self._client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        raw = await pipe.get(key)
                        if raw is None:
                            await self._unwatch(pipe)
                            return False
                        state = self._decode(
                            raw,
                            conversation_id=window.conversation_id,
                            fingerprint=load_token.conversation_fingerprint,
                        )
                        if state.window is not None or state.generation != load_token.generation:
                            await self._unwatch(pipe)
                            return False
                        remaining_milliseconds = await pipe.pttl(key)
                        if remaining_milliseconds in {-2, 0}:
                            await self._unwatch(pipe)
                            return False
                        if (
                            not isinstance(remaining_milliseconds, int)
                            or isinstance(remaining_milliseconds, bool)
                            or remaining_milliseconds == -1
                            or remaining_milliseconds < -2
                            or remaining_milliseconds > self._settings.load_timeout_milliseconds
                        ):
                            raise RedisHistoryHotCacheUnavailableError("Redis history hot cache publish reservation TTL 无效")
                        pipe.multi()
                        pipe.set(
                            key,
                            ready,
                            px=self._settings.ttl_milliseconds,
                        )
                        results = await pipe.execute()
                        if not isinstance(results, (list, tuple)) or len(results) != 1 or results[0] is not True:
                            raise RedisHistoryHotCacheUnavailableError("Redis history hot cache publish 响应无效")
                        return True
                    except WatchError:
                        continue
            raise RedisHistoryHotCacheConflictError("Redis history hot cache publish 并发冲突过多")
        except asyncio.CancelledError:
            raise
        except HistoryHotCacheError:
            raise
        except Exception as error:
            raise self._translate_backend_error("publish", error) from None

    async def invalidate(self, conversation_id: str) -> None:
        conversation_id = validate_conversation_id(conversation_id)
        key, fingerprint = self._key(conversation_id)
        generation = _new_generation(self._generation_factory)
        loading = self._loading_bytes(
            fingerprint=fingerprint,
            generation=generation,
        )
        try:
            result = await self._client.set(
                key,
                loading,
                px=self._settings.load_timeout_milliseconds,
            )
            if result is not True:
                raise RedisHistoryHotCacheUnavailableError("Redis history hot cache invalidate 响应无效")
        except asyncio.CancelledError:
            raise
        except HistoryHotCacheError:
            raise
        except Exception as error:
            raise self._translate_backend_error("invalidate", error) from None

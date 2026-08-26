from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any
import uuid

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .pending_actions import (
    PendingAction,
    PendingActionError,
    _adapter_id,
    _bot_id,
    _group_id,
    canonicalize_arguments,
    hash_arguments,
)

_NONCE_RE = re.compile(r"^[A-F0-9]{6}$")
_LOWER_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_GENERATION_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_SCHEMA_VERSION = 1
_MAX_GENERATION = 9_223_372_036_854_775_807
_MAX_IDENTITY_CHARS = 512
_MAX_TOOL_NAME_CHARS = 256
_RECORD_OVERHEAD_MAX_BYTES = 16_384
_INVALID_ACTION_MEMBER = "INVALID"
_NONCE_ATTEMPTS = 32

NonceFactory = Callable[[], str]
WallClock = Callable[[], float]


class RedisPendingActionUnavailableError(PendingActionError):
    """The explicit Redis backend could not establish a known storage result."""


class RedisPendingActionConflictError(PendingActionError):
    """A bounded Redis WATCH/MULTI retry budget was exhausted."""


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


@dataclass(frozen=True)
class RedisPendingActionSettings:
    """Explicit bounded settings for a Redis PendingAction namespace."""

    key_prefix: str = "moellm"
    ttl_seconds: float = 120.0
    max_entries: int = 256
    max_argument_bytes: int = 16_384
    failure_window_seconds: float = 60.0
    max_failures: int = 8
    max_failure_keys: int = 4_096
    transaction_retries: int = 32

    def __post_init__(self) -> None:
        if not isinstance(self.key_prefix, str) or not _KEY_PREFIX_RE.fullmatch(self.key_prefix):
            raise ValueError("key_prefix 必须是 1 到 96 位安全 Redis key 前缀")
        _validate_seconds(
            self.ttl_seconds,
            label="ttl_seconds",
            minimum=1.0,
            maximum=3_600.0,
        )
        _validate_integer(
            self.max_entries,
            label="max_entries",
            minimum=1,
            maximum=10_000,
        )
        _validate_integer(
            self.max_argument_bytes,
            label="max_argument_bytes",
            minimum=1,
            maximum=1_048_576,
        )
        _validate_seconds(
            self.failure_window_seconds,
            label="failure_window_seconds",
            minimum=1.0,
            maximum=3_600.0,
        )
        _validate_integer(
            self.max_failures,
            label="max_failures",
            minimum=1,
            maximum=100,
        )
        _validate_integer(
            self.max_failure_keys,
            label="max_failure_keys",
            minimum=1,
            maximum=100_000,
        )
        _validate_integer(
            self.transaction_retries,
            label="transaction_retries",
            minimum=1,
            maximum=64,
        )

    @property
    def ttl_milliseconds(self) -> int:
        return math.ceil(float(self.ttl_seconds) * 1_000)

    @property
    def failure_window_milliseconds(self) -> int:
        return math.ceil(float(self.failure_window_seconds) * 1_000)

    def safe_diagnostics(self) -> dict[str, float | int | str]:
        return {
            "key_prefix": self.key_prefix,
            "ttl_seconds": float(self.ttl_seconds),
            "max_entries": self.max_entries,
            "max_argument_bytes": self.max_argument_bytes,
            "failure_window_seconds": float(self.failure_window_seconds),
            "max_failures": self.max_failures,
            "max_failure_keys": self.max_failure_keys,
            "transaction_retries": self.transaction_retries,
        }


@dataclass(frozen=True)
class _RedisPendingActionKeyspace:
    root: str
    action_prefix: str
    slot_prefix: str
    failure_prefix: str
    action_index: str
    slot_index: str
    failure_index: str

    @classmethod
    def from_prefix(cls, prefix: str) -> _RedisPendingActionKeyspace:
        root = f"{prefix}:{{pending-action}}"
        return cls(
            root=root,
            action_prefix=f"{root}:action:",
            slot_prefix=f"{root}:slot:",
            failure_prefix=f"{root}:failure:",
            action_index=f"{root}:actions",
            slot_index=f"{root}:slots",
            failure_index=f"{root}:failures",
        )

    def action(self, nonce: str) -> str:
        return f"{self.action_prefix}{nonce}"

    def slot(self, fingerprint: str) -> str:
        return f"{self.slot_prefix}{fingerprint}"

    def failure(self, fingerprint: str) -> str:
        return f"{self.failure_prefix}{fingerprint}"


@dataclass(frozen=True)
class _RedisPendingActionRecord:
    action: PendingAction
    caller_fingerprint: str
    slot_fingerprint: str


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_identity(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or _has_control_characters(value):
        raise PendingActionError(f"{label} 无效，危险操作已拒绝")
    return value


def _validate_group_id(value: object) -> str | None:
    if value is None:
        return None
    return _validate_identity(value, label="group_id", maximum=_MAX_IDENTITY_CHARS)


def _validate_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_GENERATION:
        raise PendingActionError("运行 generation 无效，危险操作已拒绝")
    return value


def _validate_bundle_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _LOWER_HEX_64_RE.fullmatch(value):
        raise PendingActionError("工具 bundle digest 无效，危险操作已拒绝")
    return value


def _fingerprint(parts: tuple[str | None, ...]) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _caller_identity(bot: Any, event: Any) -> tuple[str, str, str, str | None]:
    return (
        _validate_identity(_bot_id(bot), label="bot_id", maximum=_MAX_IDENTITY_CHARS),
        _validate_identity(_adapter_id(bot), label="adapter_id", maximum=_MAX_IDENTITY_CHARS),
        _validate_identity(
            str(getattr(event, "user_id", "")),
            label="user_id",
            maximum=_MAX_IDENTITY_CHARS,
        ),
        _validate_group_id(_group_id(event)),
    )


def _decode_redis_text(value: object, *, label: str, maximum_bytes: int) -> str:
    if isinstance(value, bytes):
        if len(value) > maximum_bytes:
            raise PendingActionError(f"Redis {label} 超过安全大小限制")
        try:
            decoded = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise PendingActionError(f"Redis {label} 不是合法 UTF-8") from None
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > maximum_bytes:
            raise PendingActionError(f"Redis {label} 超过安全大小限制")
        decoded = value
    else:
        raise PendingActionError(f"Redis {label} 类型无效")
    if _has_control_characters(decoded):
        raise PendingActionError(f"Redis {label} 包含控制字符")
    return decoded


def _decode_optional_nonce(value: object) -> str | None:
    if value is None:
        return None
    decoded = _decode_redis_text(value, label="slot nonce", maximum_bytes=6)
    if not _NONCE_RE.fullmatch(decoded):
        raise PendingActionError("Redis PendingAction slot 已损坏，危险操作已拒绝")
    return decoded


def _decode_nonce_members(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise PendingActionError("Redis PendingAction index 响应无效")
    decoded = tuple(_decode_redis_text(value, label="action index member", maximum_bytes=6) for value in values)
    if any(not _NONCE_RE.fullmatch(value) for value in decoded):
        raise PendingActionError("Redis PendingAction index 已损坏，危险操作已拒绝")
    return decoded


def _decode_fingerprint_members(values: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise PendingActionError(f"Redis {label} index 响应无效")
    decoded = tuple(_decode_redis_text(value, label=f"{label} index member", maximum_bytes=64) for value in values)
    if any(not _LOWER_HEX_64_RE.fullmatch(value) for value in decoded):
        raise PendingActionError(f"Redis {label} index 已损坏，危险操作已拒绝")
    return decoded


def _decode_nonnegative_integer(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PendingActionError(f"Redis {label} 响应无效")
    return value


def _encode_record(record: _RedisPendingActionRecord) -> str:
    action = record.action
    return json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "action_id": action.action_id,
            "bot_id": action.bot_id,
            "adapter_id": action.adapter_id,
            "user_id": action.user_id,
            "group_id": action.group_id,
            "tool_name": action.tool_name,
            "arguments_json": action.arguments_json,
            "arguments_hash": action.arguments_hash,
            "generation": str(action.generation),
            "bundle_digest": action.bundle_digest,
            "created_at": action.created_at,
            "expires_at": action.expires_at,
            "nonce": action.nonce,
            "caller_fingerprint": record.caller_fingerprint,
            "slot_fingerprint": record.slot_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_record(
    raw: object,
    *,
    expected_nonce: str,
    max_argument_bytes: int,
    expected_ttl_seconds: float,
) -> _RedisPendingActionRecord:
    encoded = _decode_redis_text(
        raw,
        label="PendingAction record",
        maximum_bytes=max_argument_bytes + _RECORD_OVERHEAD_MAX_BYTES,
    )
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        raise PendingActionError("Redis PendingAction 记录已损坏，危险操作已拒绝") from None
    expected_fields = {
        "schema_version",
        "action_id",
        "bot_id",
        "adapter_id",
        "user_id",
        "group_id",
        "tool_name",
        "arguments_json",
        "arguments_hash",
        "generation",
        "bundle_digest",
        "created_at",
        "expires_at",
        "nonce",
        "caller_fingerprint",
        "slot_fingerprint",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise PendingActionError("Redis PendingAction 记录 schema 无效，危险操作已拒绝")
    if (
        not isinstance(payload["schema_version"], int)
        or isinstance(payload["schema_version"], bool)
        or payload["schema_version"] != _SCHEMA_VERSION
    ):
        raise PendingActionError("Redis PendingAction 记录版本无效，危险操作已拒绝")

    action_id = payload["action_id"]
    nonce = payload["nonce"]
    arguments_hash = payload["arguments_hash"]
    caller_fingerprint = payload["caller_fingerprint"]
    slot_fingerprint = payload["slot_fingerprint"]
    generation_text = payload["generation"]
    if not isinstance(action_id, str) or not _LOWER_HEX_32_RE.fullmatch(action_id):
        raise PendingActionError("Redis PendingAction action_id 无效，危险操作已拒绝")
    if not isinstance(nonce, str) or nonce != expected_nonce or not _NONCE_RE.fullmatch(nonce):
        raise PendingActionError("Redis PendingAction nonce 无效，危险操作已拒绝")
    if not isinstance(arguments_hash, str) or not _LOWER_HEX_64_RE.fullmatch(arguments_hash):
        raise PendingActionError("Redis PendingAction arguments hash 无效，危险操作已拒绝")
    if not isinstance(caller_fingerprint, str) or not _LOWER_HEX_64_RE.fullmatch(caller_fingerprint):
        raise PendingActionError("Redis PendingAction caller fingerprint 无效，危险操作已拒绝")
    if not isinstance(slot_fingerprint, str) or not _LOWER_HEX_64_RE.fullmatch(slot_fingerprint):
        raise PendingActionError("Redis PendingAction slot fingerprint 无效，危险操作已拒绝")
    if not isinstance(generation_text, str) or not _GENERATION_RE.fullmatch(generation_text):
        raise PendingActionError("Redis PendingAction generation 无效，危险操作已拒绝")
    generation = int(generation_text)
    if generation > _MAX_GENERATION:
        raise PendingActionError("Redis PendingAction generation 超限，危险操作已拒绝")

    bot_id = _validate_identity(payload["bot_id"], label="Redis bot_id", maximum=_MAX_IDENTITY_CHARS)
    adapter_id = _validate_identity(
        payload["adapter_id"],
        label="Redis adapter_id",
        maximum=_MAX_IDENTITY_CHARS,
    )
    user_id = _validate_identity(payload["user_id"], label="Redis user_id", maximum=_MAX_IDENTITY_CHARS)
    group_id = _validate_group_id(payload["group_id"])
    tool_name = _validate_identity(
        payload["tool_name"],
        label="Redis tool_name",
        maximum=_MAX_TOOL_NAME_CHARS,
    )
    arguments_json = payload["arguments_json"]
    if not isinstance(arguments_json, str) or len(arguments_json.encode("utf-8")) > max_argument_bytes:
        raise PendingActionError("Redis PendingAction arguments 超过安全边界")
    bundle_digest = _validate_bundle_digest(payload["bundle_digest"])
    created_at = payload["created_at"]
    expires_at = payload["expires_at"]
    if (
        not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not math.isfinite(created_at)
        or created_at < 0
        or not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or not math.isfinite(expires_at)
        or expires_at <= created_at
        or not math.isclose(
            float(expires_at) - float(created_at),
            expected_ttl_seconds,
            rel_tol=0.0,
            abs_tol=0.001,
        )
    ):
        raise PendingActionError("Redis PendingAction 时间边界无效，危险操作已拒绝")

    expected_caller = _fingerprint((bot_id, adapter_id, user_id, group_id))
    expected_slot = _fingerprint((bot_id, adapter_id, user_id, group_id, tool_name))
    if caller_fingerprint != expected_caller or slot_fingerprint != expected_slot:
        raise PendingActionError("Redis PendingAction identity fingerprint 校验失败，危险操作已拒绝")
    action = PendingAction(
        action_id=action_id,
        bot_id=bot_id,
        adapter_id=adapter_id,
        user_id=user_id,
        group_id=group_id,
        tool_name=tool_name,
        arguments_json=arguments_json,
        arguments_hash=arguments_hash,
        generation=generation,
        bundle_digest=bundle_digest,
        created_at=float(created_at),
        expires_at=float(expires_at),
        nonce=nonce,
    )
    return _RedisPendingActionRecord(
        action=action,
        caller_fingerprint=caller_fingerprint,
        slot_fingerprint=slot_fingerprint,
    )


class RedisPendingActionStore:
    """Explicit Redis backend with bounded WATCH/MULTI one-shot semantics."""

    def __init__(
        self,
        client: Redis,
        *,
        settings: RedisPendingActionSettings | None = None,
        nonce_factory: NonceFactory | None = None,
        wall_clock: WallClock = time.time,
    ) -> None:
        if not isinstance(client, Redis):
            raise TypeError("client 必须是 redis-py asyncio Redis client")
        if settings is not None and not isinstance(settings, RedisPendingActionSettings):
            raise TypeError("settings 必须是 RedisPendingActionSettings")
        if not callable(nonce_factory) and nonce_factory is not None:
            raise TypeError("nonce_factory 必须可调用")
        if not callable(wall_clock):
            raise TypeError("wall_clock 必须可调用")
        self._client = client
        self._settings = RedisPendingActionSettings() if settings is None else settings
        self._keyspace = _RedisPendingActionKeyspace.from_prefix(self._settings.key_prefix)
        self._nonce_factory = (lambda: secrets.token_hex(3).upper()) if nonce_factory is None else nonce_factory
        self._wall_clock = wall_clock

    @property
    def settings(self) -> RedisPendingActionSettings:
        return self._settings

    def __repr__(self) -> str:
        return (
            "RedisPendingActionStore("
            f"key_prefix={self._settings.key_prefix!r}, "
            f"ttl_seconds={float(self._settings.ttl_seconds)!r}, "
            f"max_entries={self._settings.max_entries!r})"
        )

    def safe_diagnostics(self) -> dict[str, bool | float | int | str]:
        return {
            "backend": "redis",
            "configured": True,
            **self._settings.safe_diagnostics(),
        }

    async def _server_now(self) -> float:
        response = await self._client.time()
        if (
            not isinstance(response, (list, tuple))
            or len(response) != 2
            or not isinstance(response[0], int)
            or isinstance(response[0], bool)
            or not isinstance(response[1], int)
            or isinstance(response[1], bool)
            or response[0] < 0
            or not 0 <= response[1] < 1_000_000
        ):
            raise RedisPendingActionUnavailableError("Redis TIME 响应无效，危险操作已拒绝")
        return response[0] + response[1] / 1_000_000

    def _new_nonce(self) -> str | None:
        nonce = str(self._nonce_factory()).strip().upper()
        return nonce if _NONCE_RE.fullmatch(nonce) else None

    @staticmethod
    async def _unwatch(pipe: Any) -> None:
        await pipe.unwatch()

    def _translate_backend_error(self, operation: str, error: Exception) -> RedisPendingActionUnavailableError:
        return RedisPendingActionUnavailableError(
            f"Redis PendingAction {operation}结果未知，危险操作已拒绝 ({type(error).__name__})"
        )

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
        identities = _caller_identity(bot, event)
        tool_name = _validate_identity(tool_name, label="tool_name", maximum=_MAX_TOOL_NAME_CHARS)
        generation = _validate_generation(generation)
        bundle_digest = _validate_bundle_digest(bundle_digest)
        arguments_json = canonicalize_arguments(arguments)
        if len(arguments_json.encode("utf-8")) > self._settings.max_argument_bytes:
            raise PendingActionError("待确认工具参数超过大小限制")
        arguments_hash = hash_arguments(arguments_json)
        caller_fingerprint = _fingerprint(identities)
        slot_fingerprint = _fingerprint((*identities, tool_name))
        slot_key = self._keyspace.slot(slot_fingerprint)

        try:
            for _attempt in range(self._settings.transaction_retries):
                now = await self._server_now()
                async with self._client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(
                            slot_key,
                            self._keyspace.action_index,
                            self._keyspace.slot_index,
                        )
                        current_nonce = _decode_optional_nonce(await pipe.get(slot_key))
                        old_record: _RedisPendingActionRecord | None = None
                        old_action_key: str | None = None
                        if current_nonce is not None:
                            old_action_key = self._keyspace.action(current_nonce)
                            await pipe.watch(old_action_key)
                            old_raw = await pipe.get(old_action_key)
                            if old_raw is not None:
                                old_record = _decode_record(
                                    old_raw,
                                    expected_nonce=current_nonce,
                                    max_argument_bytes=self._settings.max_argument_bytes,
                                    expected_ttl_seconds=float(self._settings.ttl_seconds),
                                )
                                if old_record.slot_fingerprint != slot_fingerprint:
                                    raise PendingActionError("Redis PendingAction slot identity 已损坏，危险操作已拒绝")
                                old_action = old_record.action
                                if old_action.created_at > now:
                                    raise PendingActionError("Redis PendingAction 时间边界已损坏，危险操作已拒绝")
                                if old_action.expires_at > now:
                                    old_action.arguments()
                                if (
                                    old_action.expires_at > now
                                    and old_action.arguments_hash == arguments_hash
                                    and old_action.generation == generation
                                    and old_action.bundle_digest == bundle_digest
                                ):
                                    await self._unwatch(pipe)
                                    return old_action

                        expired_nonces = _decode_nonce_members(await pipe.zrangebyscore(self._keyspace.action_index, "-inf", now))
                        expired_slots = _decode_fingerprint_members(
                            await pipe.zrangebyscore(self._keyspace.slot_index, "-inf", now),
                            label="slot",
                        )
                        action_count = _decode_nonnegative_integer(
                            await pipe.zcard(self._keyspace.action_index),
                            label="action index count",
                        )
                        active_count = max(0, action_count - len(expired_nonces))
                        if current_nonce is not None:
                            old_score = await pipe.zscore(self._keyspace.action_index, current_nonce)
                            if old_score is not None and current_nonce not in expired_nonces:
                                active_count = max(0, active_count - 1)
                        if active_count >= self._settings.max_entries:
                            await self._unwatch(pipe)
                            raise PendingActionError("待确认操作队列已满，请稍后重试")

                        nonce: str | None = None
                        action_key: str | None = None
                        for _nonce_attempt in range(_NONCE_ATTEMPTS):
                            candidate = self._new_nonce()
                            if candidate is None or candidate == current_nonce:
                                continue
                            candidate_key = self._keyspace.action(candidate)
                            await pipe.watch(candidate_key)
                            if await pipe.get(candidate_key) is None:
                                nonce = candidate
                                action_key = candidate_key
                                break
                        if nonce is None or action_key is None:
                            raise PendingActionError("无法生成安全确认码，危险操作已拒绝")

                        expires_at = now + float(self._settings.ttl_seconds)
                        action = PendingAction(
                            action_id=uuid.uuid4().hex,
                            bot_id=identities[0],
                            adapter_id=identities[1],
                            user_id=identities[2],
                            group_id=identities[3],
                            tool_name=tool_name,
                            arguments_json=arguments_json,
                            arguments_hash=arguments_hash,
                            generation=generation,
                            bundle_digest=bundle_digest,
                            created_at=now,
                            expires_at=expires_at,
                            nonce=nonce,
                        )
                        record = _RedisPendingActionRecord(
                            action=action,
                            caller_fingerprint=caller_fingerprint,
                            slot_fingerprint=slot_fingerprint,
                        )
                        pipe.multi()
                        pipe.zremrangebyscore(self._keyspace.action_index, "-inf", now)
                        pipe.zremrangebyscore(self._keyspace.slot_index, "-inf", now)
                        if old_action_key is not None and current_nonce is not None:
                            pipe.delete(old_action_key)
                            pipe.zrem(self._keyspace.action_index, current_nonce)
                        for expired_slot in expired_slots:
                            pipe.delete(self._keyspace.slot(expired_slot))
                        pipe.set(
                            action_key,
                            _encode_record(record),
                            px=self._settings.ttl_milliseconds,
                        )
                        pipe.set(
                            slot_key,
                            nonce,
                            px=self._settings.ttl_milliseconds,
                        )
                        pipe.zadd(self._keyspace.action_index, {nonce: expires_at})
                        pipe.zadd(self._keyspace.slot_index, {slot_fingerprint: expires_at})
                        await pipe.execute()
                        return action
                    except WatchError:
                        continue
            raise RedisPendingActionConflictError("Redis PendingAction 并发冲突过多，危险操作已拒绝")
        except asyncio.CancelledError:
            raise
        except PendingActionError:
            raise
        except Exception as error:
            raise self._translate_backend_error("创建", error) from None

    async def _read_failure_window(
        self,
        pipe: Any,
        *,
        failure_key: str,
    ) -> tuple[int, int]:
        raw_count = await pipe.get(failure_key)
        ttl_ms = await pipe.pttl(failure_key)
        if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool):
            raise PendingActionError("Redis PendingAction failure TTL 响应无效")
        if raw_count is None:
            if ttl_ms != -2:
                raise PendingActionError("Redis PendingAction failure 状态已损坏，危险操作已拒绝")
            failure_count = 0
            remaining_ms = self._settings.failure_window_milliseconds
        elif ttl_ms == -2:
            # The key may expire between the immediately adjacent GET and PTTL.
            failure_count = 0
            remaining_ms = self._settings.failure_window_milliseconds
        else:
            count_text = _decode_redis_text(raw_count, label="failure count", maximum_bytes=3)
            if not count_text.isascii() or not count_text.isdigit():
                raise PendingActionError("Redis PendingAction failure count 已损坏，危险操作已拒绝")
            failure_count = int(count_text)
            if ttl_ms <= 0:
                raise PendingActionError("Redis PendingAction failure TTL 已损坏，危险操作已拒绝")
            remaining_ms = ttl_ms
        if failure_count >= self._settings.max_failures:
            raise PendingActionError("确认码失败尝试过多，请稍后重试")
        return failure_count, remaining_ms

    async def _prepare_failure_update(
        self,
        pipe: Any,
        *,
        now: float,
        failure_fingerprint: str,
        failure_count: int,
        remaining_ms: int,
    ) -> tuple[int, int, tuple[str, ...], str | None]:
        expired_failures = _decode_fingerprint_members(
            await pipe.zrangebyscore(self._keyspace.failure_index, "-inf", now),
            label="failure",
        )
        failure_count_keys = _decode_nonnegative_integer(
            await pipe.zcard(self._keyspace.failure_index),
            label="failure index count",
        )
        current_score = await pipe.zscore(self._keyspace.failure_index, failure_fingerprint)
        tracked = current_score is not None and float(current_score) > now
        active_keys = max(0, failure_count_keys - len(expired_failures))
        evicted: str | None = None
        if not tracked and active_keys >= self._settings.max_failure_keys:
            oldest = _decode_fingerprint_members(
                await pipe.zrangebyscore(
                    self._keyspace.failure_index,
                    f"({now}",
                    "+inf",
                    start=0,
                    num=1,
                ),
                label="failure",
            )
            if not oldest:
                raise PendingActionError("Redis PendingAction failure index 已损坏，危险操作已拒绝")
            evicted = oldest[0]
        return failure_count + 1, remaining_ms, expired_failures, evicted

    async def _resolve(
        self,
        nonce: object,
        *,
        bot: Any,
        event: Any,
        generation: int | None,
        cancel: bool,
    ) -> PendingAction | None:
        identities = _caller_identity(bot, event)
        caller_fingerprint = _fingerprint(identities)
        failure_key = self._keyspace.failure(caller_fingerprint)
        normalized = nonce.strip().upper() if isinstance(nonce, str) else ""
        valid_nonce = bool(_NONCE_RE.fullmatch(normalized))
        action_member = normalized if valid_nonce else _INVALID_ACTION_MEMBER
        action_key = self._keyspace.action(action_member)
        expected_generation = None if cancel else _validate_generation(generation)

        for _attempt in range(self._settings.transaction_retries):
            now = await self._server_now()
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(
                        action_key,
                        self._keyspace.action_index,
                        self._keyspace.slot_index,
                        failure_key,
                        self._keyspace.failure_index,
                    )
                    failure_count, failure_remaining_ms = await self._read_failure_window(
                        pipe,
                        failure_key=failure_key,
                    )
                    raw_record = await pipe.get(action_key) if valid_nonce else None
                    record: _RedisPendingActionRecord | None = None
                    action: PendingAction | None = None
                    delete_action = False
                    success = False
                    failure_message: str | None = None
                    if not valid_nonce:
                        failure_message = "确认码格式错误"
                    elif raw_record is None:
                        failure_message = "确认码不存在、已过期或与当前会话不匹配" if cancel else "确认码不存在、已过期或已使用"
                    else:
                        try:
                            record = _decode_record(
                                raw_record,
                                expected_nonce=normalized,
                                max_argument_bytes=self._settings.max_argument_bytes,
                                expected_ttl_seconds=float(self._settings.ttl_seconds),
                            )
                        except PendingActionError:
                            delete_action = True
                            failure_message = "Redis PendingAction 记录已损坏，危险操作已拒绝"
                        if record is not None:
                            action = record.action
                            if action.created_at > now or action.expires_at <= now:
                                delete_action = True
                                failure_message = "确认码已过期或时间边界无效"
                            elif record.caller_fingerprint != caller_fingerprint:
                                failure_message = (
                                    "确认码不存在、已过期或与当前会话不匹配" if cancel else "确认码与当前用户或会话不匹配"
                                )
                            elif not cancel and action.generation != expected_generation:
                                delete_action = True
                                failure_message = "工具已重载，原确认码已失效"
                            elif cancel:
                                delete_action = True
                                success = True
                            else:
                                delete_action = True
                                try:
                                    action.arguments()
                                except PendingActionError as error:
                                    failure_message = str(error)
                                else:
                                    success = True

                    slot_key: str | None = None
                    slot_matches = False
                    if record is not None and delete_action:
                        slot_key = self._keyspace.slot(record.slot_fingerprint)
                        await pipe.watch(slot_key)
                        slot_value = await pipe.get(slot_key)
                        if slot_value is not None:
                            try:
                                slot_matches = _decode_optional_nonce(slot_value) == normalized
                            except PendingActionError:
                                slot_matches = False

                    failure_update: tuple[int, int, tuple[str, ...], str | None] | None = None
                    if not success:
                        failure_update = await self._prepare_failure_update(
                            pipe,
                            now=now,
                            failure_fingerprint=caller_fingerprint,
                            failure_count=failure_count,
                            remaining_ms=failure_remaining_ms,
                        )

                    pipe.multi()
                    if delete_action and valid_nonce:
                        pipe.delete(action_key)
                        pipe.zrem(self._keyspace.action_index, normalized)
                        if slot_key is not None and slot_matches and record is not None:
                            pipe.delete(slot_key)
                            pipe.zrem(self._keyspace.slot_index, record.slot_fingerprint)
                    if success:
                        pipe.delete(failure_key)
                        pipe.zrem(self._keyspace.failure_index, caller_fingerprint)
                    else:
                        assert failure_update is not None
                        new_count, remaining_ms, expired_failures, evicted = failure_update
                        pipe.zremrangebyscore(self._keyspace.failure_index, "-inf", now)
                        for expired_failure in expired_failures:
                            pipe.delete(self._keyspace.failure(expired_failure))
                        if evicted is not None:
                            pipe.delete(self._keyspace.failure(evicted))
                            pipe.zrem(self._keyspace.failure_index, evicted)
                        failure_expires_at = now + remaining_ms / 1_000
                        pipe.set(failure_key, str(new_count), px=max(1, remaining_ms))
                        pipe.zadd(
                            self._keyspace.failure_index,
                            {caller_fingerprint: failure_expires_at},
                        )
                    await pipe.execute()
                    if success:
                        return None if cancel else action
                    raise PendingActionError(failure_message or "Redis PendingAction 操作已拒绝")
                except WatchError:
                    continue
        raise RedisPendingActionConflictError("Redis PendingAction 并发冲突过多，危险操作已拒绝")

    async def consume(
        self,
        nonce: str,
        *,
        bot: Any,
        event: Any,
        generation: int,
    ) -> PendingAction:
        try:
            action = await self._resolve(
                nonce,
                bot=bot,
                event=event,
                generation=generation,
                cancel=False,
            )
            if not isinstance(action, PendingAction):
                raise PendingActionError("Redis PendingAction 消费结果无效，危险操作已拒绝")
            return action
        except asyncio.CancelledError:
            raise
        except PendingActionError:
            raise
        except Exception as error:
            raise self._translate_backend_error("消费", error) from None

    async def cancel(self, nonce: str, *, bot: Any, event: Any) -> None:
        try:
            await self._resolve(
                nonce,
                bot=bot,
                event=event,
                generation=None,
                cancel=True,
            )
        except asyncio.CancelledError:
            raise
        except PendingActionError:
            raise
        except Exception as error:
            raise self._translate_backend_error("取消", error) from None

    async def clear(self) -> None:
        try:
            for _attempt in range(self._settings.transaction_retries):
                async with self._client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(
                            self._keyspace.action_index,
                            self._keyspace.slot_index,
                            self._keyspace.failure_index,
                        )
                        action_members = _decode_nonce_members(await pipe.zrange(self._keyspace.action_index, 0, -1))
                        slot_members = _decode_fingerprint_members(
                            await pipe.zrange(self._keyspace.slot_index, 0, -1),
                            label="slot",
                        )
                        failure_members = _decode_fingerprint_members(
                            await pipe.zrange(self._keyspace.failure_index, 0, -1),
                            label="failure",
                        )
                        keys = [self._keyspace.action(member) for member in action_members]
                        keys.extend(self._keyspace.slot(member) for member in slot_members)
                        keys.extend(self._keyspace.failure(member) for member in failure_members)
                        pipe.multi()
                        if keys:
                            pipe.delete(*keys)
                        pipe.delete(
                            self._keyspace.action_index,
                            self._keyspace.slot_index,
                            self._keyspace.failure_index,
                        )
                        await pipe.execute()
                        return
                    except WatchError:
                        continue
            raise RedisPendingActionConflictError("Redis PendingAction 清理并发冲突过多，操作已拒绝")
        except asyncio.CancelledError:
            raise
        except PendingActionError:
            raise
        except Exception as error:
            raise self._translate_backend_error("清理", error) from None

    def remaining_ttl_seconds(self, action: PendingAction) -> int:
        if not isinstance(action, PendingAction):
            raise TypeError("action 必须是 PendingAction")
        now = self._wall_clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise PendingActionError("本地显示时钟无效")
        return max(0, math.ceil(action.expires_at - float(now)))

    async def size(self) -> int:
        try:
            now = await self._server_now()
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.multi()
                pipe.zremrangebyscore(self._keyspace.action_index, "-inf", now)
                pipe.zremrangebyscore(self._keyspace.slot_index, "-inf", now)
                pipe.zremrangebyscore(self._keyspace.failure_index, "-inf", now)
                pipe.zcard(self._keyspace.action_index)
                results = await pipe.execute()
            if not isinstance(results, (list, tuple)) or len(results) != 4:
                raise RedisPendingActionUnavailableError("Redis PendingAction size 响应无效")
            return _decode_nonnegative_integer(results[-1], label="action index count")
        except asyncio.CancelledError:
            raise
        except PendingActionError:
            raise
        except Exception as error:
            raise self._translate_backend_error("计数", error) from None

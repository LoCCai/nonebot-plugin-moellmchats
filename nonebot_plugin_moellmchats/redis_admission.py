from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import re
import secrets
from typing import Any, TypeVar

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .admission import AdmissionRejected
from .admission_store import (
    AdmissionActivation,
    AdmissionActivationStatus,
    AdmissionKey,
    AdmissionLease,
    AdmissionLeaseLostError,
    AdmissionRelease,
    AdmissionRenewal,
    AdmissionReservation,
    AdmissionSnapshot,
    AdmissionStoreError,
    validate_interval,
)
from .runtime_metrics import runtime_metrics

_KEY_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,62}$")
_LOWER_HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 1
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_MAX_KEY_CHARS = 512
_MAX_INTEGER_KEY = 9_223_372_036_854_775_807
_RECORD_BOUND_BYTES = 512
_STATE_OVERHEAD_BYTES = 1_024
_LEASE_ID_ATTEMPTS = 32

LeaseIdFactory = Callable[[], str]
_ResultT = TypeVar("_ResultT")


class RedisAdmissionUnavailableError(AdmissionStoreError):
    """Redis could not establish a known distributed admission result."""


class RedisAdmissionConflictError(AdmissionStoreError):
    """A bounded Redis admission WATCH/MULTI retry budget was exhausted."""


class RedisAdmissionStateError(AdmissionStoreError):
    """The Redis admission state failed strict structural validation."""


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
class RedisAdmissionSettings:
    """Explicit bounded settings for one distributed admission namespace."""

    key_prefix: str = "moellm"
    name: str = "llm"
    max_active: int = 4
    max_pending: int = 32
    max_per_key: int | None = 2
    pending_lease_seconds: float = 30.0
    active_lease_seconds: float = 30.0
    poll_interval_seconds: float = 0.25
    heartbeat_interval_seconds: float = 5.0
    transaction_retries: int = 32
    max_state_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not isinstance(self.key_prefix, str) or not _KEY_PREFIX_RE.fullmatch(self.key_prefix):
            raise ValueError("key_prefix 必须是 1 到 96 位安全 Redis key 前缀")
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise ValueError("name 必须是 1 到 63 位安全 admission 名称")
        _validate_integer(self.max_active, label="max_active", minimum=1, maximum=1_000)
        _validate_integer(self.max_pending, label="max_pending", minimum=1, maximum=10_000)
        if self.max_per_key is not None:
            _validate_integer(
                self.max_per_key,
                label="max_per_key",
                minimum=1,
                maximum=1_000,
            )
        pending_seconds = validate_interval(
            self.pending_lease_seconds,
            label="pending_lease_seconds",
            minimum=1.0,
            maximum=3_600.0,
        )
        active_seconds = validate_interval(
            self.active_lease_seconds,
            label="active_lease_seconds",
            minimum=1.0,
            maximum=86_400.0,
        )
        poll_seconds = validate_interval(
            self.poll_interval_seconds,
            label="poll_interval_seconds",
            minimum=0.01,
            maximum=5.0,
        )
        heartbeat_seconds = validate_interval(
            self.heartbeat_interval_seconds,
            label="heartbeat_interval_seconds",
            minimum=0.1,
            maximum=300.0,
        )
        if poll_seconds > pending_seconds / 3:
            raise ValueError("poll_interval_seconds 不得超过 pending lease 的三分之一")
        if heartbeat_seconds > active_seconds / 3:
            raise ValueError("heartbeat_interval_seconds 不得超过 active lease 的三分之一")
        _validate_integer(
            self.transaction_retries,
            label="transaction_retries",
            minimum=1,
            maximum=64,
        )
        minimum_state_bytes = self.max_records * _RECORD_BOUND_BYTES + _STATE_OVERHEAD_BYTES
        _validate_integer(
            self.max_state_bytes,
            label="max_state_bytes",
            minimum=minimum_state_bytes,
            maximum=8_388_608,
        )

    @property
    def max_records(self) -> int:
        return self.max_active + self.max_pending

    @property
    def pending_lease_milliseconds(self) -> int:
        return round(float(self.pending_lease_seconds) * 1_000)

    @property
    def active_lease_milliseconds(self) -> int:
        return round(float(self.active_lease_seconds) * 1_000)

    @property
    def maximum_key_ttl_milliseconds(self) -> int:
        return max(
            self.pending_lease_milliseconds,
            self.active_lease_milliseconds,
        )

    def safe_diagnostics(self) -> dict[str, bool | float | int | str | None]:
        return {
            "key_prefix": self.key_prefix,
            "name": self.name,
            "max_active": self.max_active,
            "max_pending": self.max_pending,
            "max_per_key": self.max_per_key,
            "pending_lease_seconds": float(self.pending_lease_seconds),
            "active_lease_seconds": float(self.active_lease_seconds),
            "poll_interval_seconds": float(self.poll_interval_seconds),
            "heartbeat_interval_seconds": float(self.heartbeat_interval_seconds),
            "transaction_retries": self.transaction_retries,
            "max_state_bytes": self.max_state_bytes,
        }


@dataclass(frozen=True)
class _AdmissionRecord:
    lease_id: str
    key_fingerprint: str | None
    state: str
    sequence: int
    created_ms: int
    updated_ms: int
    expires_ms: int


@dataclass(frozen=True)
class _AdmissionState:
    next_sequence: int = 1
    leases: tuple[_AdmissionRecord, ...] = ()


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _key_fingerprint(key: AdmissionKey) -> str | None:
    if key is None:
        return None
    if isinstance(key, bool) or not isinstance(key, (int, str)):
        raise AdmissionStoreError("Redis admission key 必须是整数、字符串或 None")
    if isinstance(key, int):
        if not -_MAX_INTEGER_KEY <= key <= _MAX_INTEGER_KEY:
            raise AdmissionStoreError("Redis admission integer key 超出安全范围")
        payload = ("int", str(key))
    else:
        if not key or len(key) > _MAX_KEY_CHARS or _has_control_characters(key):
            raise AdmissionStoreError("Redis admission string key 无效")
        payload = ("str", key)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _namespace_fingerprint(settings: RedisAdmissionSettings) -> str:
    encoded = json.dumps(
        (settings.key_prefix, settings.name),
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(state: _AdmissionState) -> AdmissionSnapshot:
    return AdmissionSnapshot(
        active=sum(record.state == "active" for record in state.leases),
        pending=sum(record.state == "pending" for record in state.leases),
    )


class RedisAdmissionStore:
    """Single-key Redis admission state with bounded WATCH/MULTI transitions."""

    def __init__(
        self,
        client: Redis,
        *,
        settings: RedisAdmissionSettings | None = None,
        lease_id_factory: LeaseIdFactory | None = None,
    ) -> None:
        if not isinstance(client, Redis):
            raise TypeError("client 必须是 redis-py asyncio Redis client")
        if settings is not None and not isinstance(settings, RedisAdmissionSettings):
            raise TypeError("settings 必须是 RedisAdmissionSettings")
        if lease_id_factory is not None and not callable(lease_id_factory):
            raise TypeError("lease_id_factory 必须可调用")
        self._client = client
        self._settings = RedisAdmissionSettings() if settings is None else settings
        self._lease_id_factory = lease_id_factory or (lambda: secrets.token_hex(16))
        self._namespace_fingerprint = _namespace_fingerprint(self._settings)
        self._state_key = f"{self._settings.key_prefix}:{{admission:{self._settings.name}}}:state"

    @property
    def settings(self) -> RedisAdmissionSettings:
        return self._settings

    def __repr__(self) -> str:
        return (
            "RedisAdmissionStore("
            f"name={self._settings.name!r}, "
            f"key_prefix={self._settings.key_prefix!r}, "
            f"max_active={self._settings.max_active!r}, "
            f"max_pending={self._settings.max_pending!r})"
        )

    def safe_diagnostics(self) -> dict[str, bool | float | int | str | None]:
        return {
            "backend": "redis",
            "configured": True,
            **self._settings.safe_diagnostics(),
        }

    def _new_lease_id(self, state: _AdmissionState) -> str:
        existing = {record.lease_id for record in state.leases}
        for _attempt in range(_LEASE_ID_ATTEMPTS):
            try:
                lease_id = str(self._lease_id_factory()).strip().lower()
            except Exception as error:
                raise AdmissionStoreError(f"无法生成安全 admission lease ID ({type(error).__name__})") from None
            if _LOWER_HEX_32_RE.fullmatch(lease_id) and lease_id not in existing:
                return lease_id
        raise AdmissionStoreError("无法生成唯一 admission lease ID")

    async def _server_now_ms(self) -> int:
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
            raise RedisAdmissionUnavailableError("Redis TIME 响应无效，admission 已拒绝")
        return response[0] * 1_000 + response[1] // 1_000

    def _decode_state(self, raw: object) -> _AdmissionState:
        if isinstance(raw, bytes):
            if len(raw) > self._settings.max_state_bytes:
                raise RedisAdmissionStateError("Redis admission state 超过安全大小限制")
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise RedisAdmissionStateError("Redis admission state 不是合法 UTF-8") from None
        elif isinstance(raw, str):
            if len(raw.encode("utf-8")) > self._settings.max_state_bytes:
                raise RedisAdmissionStateError("Redis admission state 超过安全大小限制")
            text = raw
        else:
            raise RedisAdmissionStateError("Redis admission state 类型无效")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise RedisAdmissionStateError("Redis admission state 不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "next_sequence", "leases"}:
            raise RedisAdmissionStateError("Redis admission state schema 无效")
        schema_version = payload["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != _SCHEMA_VERSION:
            raise RedisAdmissionStateError("Redis admission state schema version 无效")
        next_sequence = payload["next_sequence"]
        if not isinstance(next_sequence, int) or isinstance(next_sequence, bool) or not 1 <= next_sequence <= _MAX_SEQUENCE:
            raise RedisAdmissionStateError("Redis admission next_sequence 无效")
        raw_leases = payload["leases"]
        if not isinstance(raw_leases, list) or len(raw_leases) > self._settings.max_records:
            raise RedisAdmissionStateError("Redis admission lease 集合无效")
        records: list[_AdmissionRecord] = []
        lease_ids: set[str] = set()
        sequences: set[int] = set()
        for item in raw_leases:
            if not isinstance(item, dict) or set(item) != {
                "lease_id",
                "key_fingerprint",
                "state",
                "sequence",
                "created_ms",
                "updated_ms",
                "expires_ms",
            }:
                raise RedisAdmissionStateError("Redis admission lease schema 无效")
            lease_id = item["lease_id"]
            key_fingerprint = item["key_fingerprint"]
            state = item["state"]
            sequence = item["sequence"]
            created_ms = item["created_ms"]
            updated_ms = item["updated_ms"]
            expires_ms = item["expires_ms"]
            if not isinstance(lease_id, str) or not _LOWER_HEX_32_RE.fullmatch(lease_id):
                raise RedisAdmissionStateError("Redis admission lease_id 无效")
            if key_fingerprint is not None and (
                not isinstance(key_fingerprint, str) or not _LOWER_HEX_64_RE.fullmatch(key_fingerprint)
            ):
                raise RedisAdmissionStateError("Redis admission key fingerprint 无效")
            if state not in {"pending", "active"}:
                raise RedisAdmissionStateError("Redis admission lease state 无效")
            integer_values = (sequence, created_ms, updated_ms, expires_ms)
            if any(not isinstance(value, int) or isinstance(value, bool) for value in integer_values):
                raise RedisAdmissionStateError("Redis admission lease 时间或序号类型无效")
            if not 1 <= sequence < _MAX_SEQUENCE or not 0 <= created_ms <= updated_ms < expires_ms:
                raise RedisAdmissionStateError("Redis admission lease 时间或序号边界无效")
            expected_ttl = (
                self._settings.pending_lease_milliseconds if state == "pending" else self._settings.active_lease_milliseconds
            )
            if expires_ms - updated_ms != expected_ttl:
                raise RedisAdmissionStateError("Redis admission lease TTL 边界无效")
            if lease_id in lease_ids or sequence in sequences:
                raise RedisAdmissionStateError("Redis admission lease identity 重复")
            lease_ids.add(lease_id)
            sequences.add(sequence)
            records.append(
                _AdmissionRecord(
                    lease_id=lease_id,
                    key_fingerprint=key_fingerprint,
                    state=state,
                    sequence=sequence,
                    created_ms=created_ms,
                    updated_ms=updated_ms,
                    expires_ms=expires_ms,
                )
            )
        if records and next_sequence <= max(record.sequence for record in records):
            raise RedisAdmissionStateError("Redis admission next_sequence 已回退")
        return _AdmissionState(
            next_sequence=next_sequence,
            leases=tuple(sorted(records, key=lambda record: record.sequence)),
        )

    def _encode_state(self, state: _AdmissionState) -> str:
        encoded = json.dumps(
            {
                "schema_version": _SCHEMA_VERSION,
                "next_sequence": state.next_sequence,
                "leases": [
                    {
                        "lease_id": record.lease_id,
                        "key_fingerprint": record.key_fingerprint,
                        "state": record.state,
                        "sequence": record.sequence,
                        "created_ms": record.created_ms,
                        "updated_ms": record.updated_ms,
                        "expires_ms": record.expires_ms,
                    }
                    for record in state.leases
                ],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > self._settings.max_state_bytes:
            raise RedisAdmissionStateError("Redis admission state 超过安全大小限制")
        return encoded

    def _validate_live_state(self, state: _AdmissionState) -> None:
        snapshot = _snapshot(state)
        if snapshot.active > self._settings.max_active or snapshot.pending > self._settings.max_pending:
            raise RedisAdmissionStateError("Redis admission 全局计数超过配置上限")
        active_keys: set[str] = set()
        per_key: dict[str, int] = {}
        for record in state.leases:
            fingerprint = record.key_fingerprint
            if fingerprint is None:
                continue
            per_key[fingerprint] = per_key.get(fingerprint, 0) + 1
            if record.state == "active":
                if fingerprint in active_keys:
                    raise RedisAdmissionStateError("Redis admission 同一 key 存在多个 active lease")
                active_keys.add(fingerprint)
        if self._settings.max_per_key is not None and any(count > self._settings.max_per_key for count in per_key.values()):
            raise RedisAdmissionStateError("Redis admission per-key 计数超过配置上限")

    @staticmethod
    def _prune(state: _AdmissionState, now_ms: int) -> tuple[_AdmissionState, bool]:
        live = tuple(record for record in state.leases if record.expires_ms > now_ms)
        return replace(state, leases=live), live != state.leases

    @staticmethod
    async def _unwatch(pipe: Any) -> None:
        await pipe.unwatch()

    def _validate_execute_result(self, result: object, *, deleting: bool) -> None:
        if not isinstance(result, (list, tuple)) or len(result) != 1:
            raise RedisAdmissionUnavailableError("Redis admission EXEC 响应无效，结果未知")
        value = result[0]
        if deleting:
            if not isinstance(value, int) or isinstance(value, bool) or value not in {0, 1}:
                raise RedisAdmissionUnavailableError("Redis admission DELETE 响应无效，结果未知")
        elif value is not True:
            raise RedisAdmissionUnavailableError("Redis admission SET 响应无效，结果未知")

    @staticmethod
    def _translate_backend_error(
        operation: str,
        error: Exception,
    ) -> RedisAdmissionUnavailableError:
        return RedisAdmissionUnavailableError(f"Redis admission {operation}结果未知，操作已拒绝 ({type(error).__name__})")

    async def _transact(
        self,
        operation: str,
        mutate: Callable[
            [_AdmissionState, int],
            tuple[_AdmissionState, _ResultT, bool],
        ],
    ) -> _ResultT:
        try:
            for _attempt in range(self._settings.transaction_retries):
                now_ms = await self._server_now_ms()
                async with self._client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(self._state_key)
                        raw_state = await pipe.get(self._state_key)
                        key_ttl = await pipe.pttl(self._state_key)
                        if not isinstance(key_ttl, int) or isinstance(key_ttl, bool):
                            raise RedisAdmissionUnavailableError("Redis admission state PTTL 响应无效")
                        if raw_state is None:
                            if key_ttl != -2:
                                raise RedisAdmissionStateError("Redis admission 空 state 的 TTL 边界无效")
                            state = _AdmissionState()
                        elif key_ttl in {-2, 0}:
                            state = _AdmissionState()
                        else:
                            if key_ttl == -1:
                                raise RedisAdmissionStateError("Redis admission state 缺少 TTL")
                            if not 0 < key_ttl <= self._settings.maximum_key_ttl_milliseconds:
                                raise RedisAdmissionStateError("Redis admission state TTL 超过安全上限")
                            state = self._decode_state(raw_state)
                        state, pruned = self._prune(state, now_ms)
                        self._validate_live_state(state)
                        new_state, result, changed = mutate(state, now_ms)
                        self._validate_live_state(new_state)
                        changed = changed or pruned
                        if not changed:
                            await self._unwatch(pipe)
                            return result
                        pipe.multi()
                        deleting = not new_state.leases
                        if deleting:
                            pipe.delete(self._state_key)
                        else:
                            ttl_ms = max(record.expires_ms for record in new_state.leases) - now_ms
                            if not 0 < ttl_ms <= self._settings.maximum_key_ttl_milliseconds:
                                raise RedisAdmissionStateError("Redis admission 写入 TTL 超过安全上限")
                            pipe.set(
                                self._state_key,
                                self._encode_state(new_state),
                                px=ttl_ms,
                            )
                        execute_result = await pipe.execute()
                        self._validate_execute_result(
                            execute_result,
                            deleting=deleting,
                        )
                        return result
                    except WatchError:
                        continue
            raise RedisAdmissionConflictError(f"Redis admission {operation}并发冲突过多，操作已拒绝")
        except asyncio.CancelledError:
            raise
        except AdmissionRejected:
            raise
        except Exception as error:
            raise self._translate_backend_error(operation, error) from None

    def _require_lease(self, lease: AdmissionLease) -> None:
        if not isinstance(lease, AdmissionLease):
            raise TypeError("lease 必须是 AdmissionLease")
        if lease.namespace_fingerprint != self._namespace_fingerprint:
            raise AdmissionStoreError("admission lease 不属于当前 namespace")

    async def reserve(self, key: AdmissionKey = None) -> AdmissionReservation:
        fingerprint = _key_fingerprint(key)

        def mutate(
            state: _AdmissionState,
            now_ms: int,
        ) -> tuple[_AdmissionState, AdmissionReservation, bool]:
            snapshot = _snapshot(state)
            if snapshot.pending >= self._settings.max_pending:
                raise AdmissionRejected(f"{self._settings.name} queue is full")
            if fingerprint is not None and self._settings.max_per_key is not None:
                key_count = sum(record.key_fingerprint == fingerprint for record in state.leases)
                if key_count >= self._settings.max_per_key:
                    raise AdmissionRejected(f"{self._settings.name} per-user limit reached")
            next_sequence = state.next_sequence
            if next_sequence >= _MAX_SEQUENCE:
                if state.leases:
                    raise AdmissionStoreError("Redis admission sequence 已耗尽")
                next_sequence = 1
            lease_id = self._new_lease_id(state)
            record = _AdmissionRecord(
                lease_id=lease_id,
                key_fingerprint=fingerprint,
                state="pending",
                sequence=next_sequence,
                created_ms=now_ms,
                updated_ms=now_ms,
                expires_ms=now_ms + self._settings.pending_lease_milliseconds,
            )
            new_state = _AdmissionState(
                next_sequence=next_sequence + 1,
                leases=(*state.leases, record),
            )
            reservation = AdmissionReservation(
                lease=AdmissionLease(
                    namespace_fingerprint=self._namespace_fingerprint,
                    lease_id=lease_id,
                    key_fingerprint=fingerprint,
                ),
                snapshot=_snapshot(new_state),
            )
            return new_state, reservation, True

        return await self._transact("reserve", mutate)

    async def try_activate(self, lease: AdmissionLease) -> AdmissionActivation:
        self._require_lease(lease)

        def mutate(
            state: _AdmissionState,
            now_ms: int,
        ) -> tuple[_AdmissionState, AdmissionActivation, bool]:
            record_index = next(
                (
                    index
                    for index, record in enumerate(state.leases)
                    if record.lease_id == lease.lease_id and record.key_fingerprint == lease.key_fingerprint
                ),
                None,
            )
            if record_index is None:
                return (
                    state,
                    AdmissionActivation(
                        status=AdmissionActivationStatus.LOST,
                        snapshot=_snapshot(state),
                    ),
                    False,
                )
            record = state.leases[record_index]
            if record.state == "active":
                return (
                    state,
                    AdmissionActivation(
                        status=AdmissionActivationStatus.ACTIVATED,
                        snapshot=_snapshot(state),
                    ),
                    False,
                )
            active_records = tuple(candidate for candidate in state.leases if candidate.state == "active")
            active_keys = {candidate.key_fingerprint for candidate in active_records if candidate.key_fingerprint is not None}
            eligible_pending = tuple(
                candidate
                for candidate in state.leases
                if candidate.state == "pending"
                and (candidate.key_fingerprint is None or candidate.key_fingerprint not in active_keys)
            )
            chosen = min(eligible_pending, key=lambda candidate: candidate.sequence, default=None)
            can_activate = (
                len(active_records) < self._settings.max_active and chosen is not None and chosen.lease_id == record.lease_id
            )
            if can_activate:
                updated = replace(
                    record,
                    state="active",
                    updated_ms=now_ms,
                    expires_ms=now_ms + self._settings.active_lease_milliseconds,
                )
                leases = list(state.leases)
                leases[record_index] = updated
                new_state = replace(state, leases=tuple(leases))
                return (
                    new_state,
                    AdmissionActivation(
                        status=AdmissionActivationStatus.ACTIVATED,
                        snapshot=_snapshot(new_state),
                    ),
                    True,
                )
            renew_threshold = self._settings.pending_lease_milliseconds // 2
            if record.expires_ms - now_ms <= renew_threshold:
                updated = replace(
                    record,
                    updated_ms=now_ms,
                    expires_ms=now_ms + self._settings.pending_lease_milliseconds,
                )
                leases = list(state.leases)
                leases[record_index] = updated
                new_state = replace(state, leases=tuple(leases))
                return (
                    new_state,
                    AdmissionActivation(
                        status=AdmissionActivationStatus.WAITING,
                        snapshot=_snapshot(new_state),
                    ),
                    True,
                )
            return (
                state,
                AdmissionActivation(
                    status=AdmissionActivationStatus.WAITING,
                    snapshot=_snapshot(state),
                ),
                False,
            )

        return await self._transact("activate", mutate)

    async def renew_active(self, lease: AdmissionLease) -> AdmissionRenewal:
        self._require_lease(lease)

        def mutate(
            state: _AdmissionState,
            now_ms: int,
        ) -> tuple[_AdmissionState, AdmissionRenewal, bool]:
            for index, record in enumerate(state.leases):
                if (
                    record.lease_id == lease.lease_id
                    and record.key_fingerprint == lease.key_fingerprint
                    and record.state == "active"
                ):
                    updated = replace(
                        record,
                        updated_ms=now_ms,
                        expires_ms=now_ms + self._settings.active_lease_milliseconds,
                    )
                    leases = list(state.leases)
                    leases[index] = updated
                    new_state = replace(state, leases=tuple(leases))
                    return (
                        new_state,
                        AdmissionRenewal(
                            renewed=True,
                            snapshot=_snapshot(new_state),
                        ),
                        True,
                    )
            return (
                state,
                AdmissionRenewal(renewed=False, snapshot=_snapshot(state)),
                False,
            )

        return await self._transact("renew", mutate)

    async def release(self, lease: AdmissionLease) -> AdmissionRelease:
        self._require_lease(lease)

        def mutate(
            state: _AdmissionState,
            _now_ms: int,
        ) -> tuple[_AdmissionState, AdmissionRelease, bool]:
            leases = tuple(
                record
                for record in state.leases
                if not (record.lease_id == lease.lease_id and record.key_fingerprint == lease.key_fingerprint)
            )
            released = len(leases) != len(state.leases)
            new_state = replace(state, leases=leases)
            return (
                new_state,
                AdmissionRelease(
                    released=released,
                    snapshot=_snapshot(new_state),
                ),
                released,
            )

        return await self._transact("release", mutate)

    async def snapshot(self) -> AdmissionSnapshot:
        def mutate(
            state: _AdmissionState,
            _now_ms: int,
        ) -> tuple[_AdmissionState, AdmissionSnapshot, bool]:
            return state, _snapshot(state), False

        return await self._transact("snapshot", mutate)


class RedisAdmissionController:
    """Polling distributed admission gate with active-lease heartbeats."""

    def __init__(self, store: RedisAdmissionStore) -> None:
        if not isinstance(store, RedisAdmissionStore):
            raise TypeError("store 必须是 RedisAdmissionStore")
        self._store = store
        self._active = 0
        self._pending = 0

    @property
    def name(self) -> str:
        return self._store.settings.name

    @property
    def max_active(self) -> int:
        return self._store.settings.max_active

    @property
    def max_pending(self) -> int:
        return self._store.settings.max_pending

    @property
    def max_per_key(self) -> int | None:
        return self._store.settings.max_per_key

    @property
    def active(self) -> int:
        return self._active

    @property
    def pending(self) -> int:
        return self._pending

    def __repr__(self) -> str:
        return f"RedisAdmissionController(name={self.name!r}, active={self.active!r}, pending={self.pending!r})"

    def safe_diagnostics(self) -> dict[str, bool | float | int | str | None]:
        return {
            **self._store.safe_diagnostics(),
            "active": self.active,
            "pending": self.pending,
        }

    def _publish(self, snapshot: AdmissionSnapshot) -> None:
        self._active = snapshot.active
        self._pending = snapshot.pending
        if self.name == "llm":
            runtime_metrics.llm_active = snapshot.active
            runtime_metrics.llm_pending = snapshot.pending
        else:
            runtime_metrics.dispatch_active = snapshot.active
            runtime_metrics.dispatch_pending = snapshot.pending

    def _rejected(self) -> None:
        if self.name == "llm":
            runtime_metrics.llm_rejected += 1
        else:
            runtime_metrics.dispatch_rejected += 1

    async def refresh_snapshot(self) -> AdmissionSnapshot:
        snapshot = await self._store.snapshot()
        self._publish(snapshot)
        return snapshot

    @asynccontextmanager
    async def slot(self, key: Hashable | None = None):
        if key is not None and (isinstance(key, bool) or not isinstance(key, (int, str))):
            raise AdmissionStoreError("Redis admission key 必须是整数、字符串或 None")
        try:
            reservation = await self._store.reserve(key)
        except AdmissionRejected:
            self._rejected()
            raise
        lease = reservation.lease
        self._publish(reservation.snapshot)
        heartbeat_task: asyncio.Task[None] | None = None
        heartbeat_error: Exception | None = None
        stop_heartbeat = asyncio.Event()
        activated = False
        try:
            while True:
                activation = await self._store.try_activate(lease)
                self._publish(activation.snapshot)
                if activation.status is AdmissionActivationStatus.ACTIVATED:
                    activated = True
                    break
                if activation.status is AdmissionActivationStatus.LOST:
                    raise AdmissionLeaseLostError(f"Redis admission {self.name} pending lease 已丢失")
                await asyncio.sleep(float(self._store.settings.poll_interval_seconds))

            owner_task = asyncio.current_task()
            if owner_task is None:
                raise AdmissionStoreError("Redis admission 无法确认当前 task")

            async def heartbeat() -> None:
                nonlocal heartbeat_error
                try:
                    while True:
                        try:
                            await asyncio.wait_for(
                                stop_heartbeat.wait(),
                                timeout=float(self._store.settings.heartbeat_interval_seconds),
                            )
                            return
                        except asyncio.TimeoutError:
                            renewal = await self._store.renew_active(lease)
                            self._publish(renewal.snapshot)
                            if not renewal.renewed:
                                raise AdmissionLeaseLostError(f"Redis admission {self.name} active lease 已丢失")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    heartbeat_error = error
                    owner_task.cancel()

            heartbeat_task = asyncio.create_task(
                heartbeat(),
                name=f"redis-admission-heartbeat:{self.name}",
            )
            try:
                yield
                if heartbeat_error is not None:
                    raise heartbeat_error
            except asyncio.CancelledError:
                if heartbeat_error is not None:
                    raise heartbeat_error
                raise
        finally:
            stop_heartbeat.set()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            release = await self._store.release(lease)
            self._publish(release.snapshot)
            if activated and not release.released and heartbeat_error is None:
                raise AdmissionLeaseLostError(f"Redis admission {self.name} active lease 在释放前已丢失")

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import errno
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
from threading import RLock
import time
from typing import Any, BinaryIO, TypeAlias

try:
    import fcntl
except ImportError:  # pragma: no cover - private spool intentionally requires POSIX flock
    fcntl = None  # type: ignore[assignment]

from .audit_event import AuditEventRecord, mutable_audit_json
from .model_usage import MAX_MODEL_USAGE_BATCH_SIZE, ModelUsageRecord
from .private_files import (
    PrivateStorageError,
    atomic_write_private_text,
    ensure_private_directory,
    ensure_private_file,
)

LOCAL_SPOOL_VERSION = 1
LOCAL_SPOOL_MAX_RECORDS_PER_FILE = 100
LOCAL_SPOOL_MAX_FILE_BYTES = 1_048_576
LOCAL_SPOOL_DEFAULT_MAX_READY_FILES = 10_000
LOCAL_SPOOL_DEFAULT_MAX_READY_BYTES = 64 * 1_048_576

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_TOKEN_RE = re.compile(r"^[a-f0-9]{32}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_FILE_RE = re.compile(r"^(ready|leased|unknown)\.(usage|audit)\.([0-9]{20})\.([a-f0-9]{32})\.json$")


class LocalSpoolError(RuntimeError):
    """Base error for the private durable Usage/Audit spool."""


class LocalSpoolConfigurationError(LocalSpoolError):
    """The spool settings or on-disk schema are unsafe."""


class LocalSpoolLifecycleError(LocalSpoolError):
    """A spool operation does not match its explicit lifecycle."""


class LocalSpoolOwnershipError(LocalSpoolError):
    """The spool was reused across a process or event loop."""


class LocalSpoolDrainRequiredError(LocalSpoolError):
    """Durable records remain and must not be silently discarded."""


class LocalSpoolResultUnknownError(LocalSpoolDrainRequiredError):
    """A leased database result is unknown and automatic replay is forbidden."""


class LocalSpoolKind(str, Enum):
    USAGE = "usage"
    AUDIT = "audit"


class LocalSpoolState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True, repr=False)
class LocalSpoolSettings:
    root: Path
    max_ready_files: int = LOCAL_SPOOL_DEFAULT_MAX_READY_FILES
    max_ready_bytes: int = LOCAL_SPOOL_DEFAULT_MAX_READY_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("LocalSpoolSettings.root 必须是 Path")
        root = Path(os.path.abspath(os.fspath(self.root)))
        if root == Path(root.anchor) or len(os.fspath(root).encode("utf-8")) > 4_096:
            raise ValueError("LocalSpoolSettings.root 不得是文件系统根且必须有界")
        if (
            not isinstance(self.max_ready_files, int)
            or isinstance(self.max_ready_files, bool)
            or not 1 <= self.max_ready_files <= 100_000
        ):
            raise ValueError("max_ready_files 必须是 1 到 100000 的整数")
        if (
            not isinstance(self.max_ready_bytes, int)
            or isinstance(self.max_ready_bytes, bool)
            or not 128 * 1_024 <= self.max_ready_bytes <= 4 * 1_024 * 1_024 * 1_024
        ):
            raise ValueError("max_ready_bytes 必须是 128 KiB 到 4 GiB 的整数")
        object.__setattr__(self, "root", root)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(root=<private>, "
            f"max_ready_files={self.max_ready_files!r}, "
            f"max_ready_bytes={self.max_ready_bytes!r})"
        )

    def safe_diagnostics(self) -> dict[str, int]:
        return {
            "max_ready_files": self.max_ready_files,
            "max_ready_bytes": self.max_ready_bytes,
        }


SpoolRecord: TypeAlias = ModelUsageRecord | AuditEventRecord


@dataclass(frozen=True, repr=False)
class LocalSpoolLease:
    lease_id: str
    kind: LocalSpoolKind
    generation: int
    records: tuple[SpoolRecord, ...] = field(repr=False)
    _path: Path = field(repr=False, compare=False)
    _owner_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, str) or not re.fullmatch(r"[1-9][0-9]{0,18}:[a-f0-9]{32}", self.lease_id):
            raise ValueError("LocalSpoolLease.lease_id 非法")
        if not isinstance(self.kind, LocalSpoolKind):
            raise TypeError("LocalSpoolLease.kind 必须是 LocalSpoolKind")
        _require_generation(self.generation)
        if not isinstance(self.records, tuple) or not self.records or len(self.records) > LOCAL_SPOOL_MAX_RECORDS_PER_FILE:
            raise ValueError("LocalSpoolLease.records 必须是有界非空元组")
        expected = ModelUsageRecord if self.kind is LocalSpoolKind.USAGE else AuditEventRecord
        if any(not isinstance(record, expected) for record in self.records):
            raise ValueError("LocalSpoolLease.records 与 kind 不一致")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(lease_id={self.lease_id!r}, "
            f"kind={self.kind.value!r}, generation={self.generation!r}, "
            f"record_count={len(self.records)!r})"
        )

    def safe_diagnostics(self) -> dict[str, int | str]:
        return {
            "lease_id": self.lease_id,
            "kind": self.kind.value,
            "generation": self.generation,
            "record_count": len(self.records),
        }


def _require_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError("generation 必须是正 PostgreSQL BIGINT")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
        raise LocalSpoolConfigurationError("local spool record 无法编码为 canonical JSON") from None


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("datetime must be canonical UTC text")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    if _datetime_text(parsed) != value:
        raise ValueError("datetime must be canonical UTC text")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _usage_payload(record: ModelUsageRecord) -> dict[str, object]:
    if record.persisted:
        raise ValueError("local spool 只接受未持久化 usage")
    return {
        "cached_tokens": record.cached_tokens,
        "cost": None if record.cost is None else _decimal_text(record.cost),
        "created_at": _datetime_text(record.created_at),
        "input_tokens": record.input_tokens,
        "model": record.model,
        "output_tokens": record.output_tokens,
        "provider": record.provider,
        "reasoning_tokens": record.reasoning_tokens,
        "run_id": record.run_id,
    }


def _usage_from_payload(value: object) -> ModelUsageRecord:
    expected = {
        "cached_tokens",
        "cost",
        "created_at",
        "input_tokens",
        "model",
        "output_tokens",
        "provider",
        "reasoning_tokens",
        "run_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("usage spool schema mismatch")
    raw_cost = value["cost"]
    if raw_cost is not None and not isinstance(raw_cost, str):
        raise ValueError("usage cost must be canonical text or null")
    cost = None if raw_cost is None else Decimal(raw_cost)
    record = ModelUsageRecord(
        usage_id=None,
        run_id=value["run_id"],  # type: ignore[arg-type]
        provider=value["provider"],  # type: ignore[arg-type]
        model=value["model"],  # type: ignore[arg-type]
        input_tokens=value["input_tokens"],  # type: ignore[arg-type]
        output_tokens=value["output_tokens"],  # type: ignore[arg-type]
        reasoning_tokens=value["reasoning_tokens"],  # type: ignore[arg-type]
        cached_tokens=value["cached_tokens"],  # type: ignore[arg-type]
        cost=cost,
        created_at=_parse_datetime(value["created_at"]),
    )
    if _usage_payload(record) != dict(value):
        raise ValueError("usage spool payload is not canonical")
    return record


def _audit_payload(record: AuditEventRecord) -> dict[str, object]:
    if record.persisted:
        raise ValueError("local spool 只接受未持久化 audit")
    return {
        "actor_type": record.actor_type,
        "actor_user_id": record.actor_user_id,
        "created_at": _datetime_text(record.created_at),
        "event_type": record.event_type,
        "metadata_json": mutable_audit_json(record.metadata_json),
        "run_id": record.run_id,
        "target_id": record.target_id,
        "target_type": record.target_type,
        "tool_call_id": record.tool_call_id,
    }


def _audit_from_payload(value: object) -> AuditEventRecord:
    expected = {
        "actor_type",
        "actor_user_id",
        "created_at",
        "event_type",
        "metadata_json",
        "run_id",
        "target_id",
        "target_type",
        "tool_call_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("audit spool schema mismatch")
    metadata = value["metadata_json"]
    if not isinstance(metadata, Mapping):
        raise ValueError("audit metadata must be an object")
    record = AuditEventRecord(
        event_id=None,
        event_type=value["event_type"],  # type: ignore[arg-type]
        actor_user_id=value["actor_user_id"],  # type: ignore[arg-type]
        actor_type=value["actor_type"],  # type: ignore[arg-type]
        target_type=value["target_type"],  # type: ignore[arg-type]
        target_id=value["target_id"],  # type: ignore[arg-type]
        run_id=value["run_id"],  # type: ignore[arg-type]
        tool_call_id=value["tool_call_id"],  # type: ignore[arg-type]
        metadata_json=metadata,  # type: ignore[arg-type]
        created_at=_parse_datetime(value["created_at"]),
    )
    if _audit_payload(record) != dict(value):
        raise ValueError("audit spool payload is not canonical")
    return record


def _encode_file(
    *,
    generation: int,
    kind: LocalSpoolKind,
    records: tuple[SpoolRecord, ...],
) -> str:
    if not records or len(records) > LOCAL_SPOOL_MAX_RECORDS_PER_FILE:
        raise ValueError("local spool records 必须是 1 到 100 条")
    if kind is LocalSpoolKind.USAGE:
        usage_records = tuple(record for record in records if isinstance(record, ModelUsageRecord))
        if len(records) > MAX_MODEL_USAGE_BATCH_SIZE or len(usage_records) != len(records):
            raise ValueError("usage spool records 类型或数量非法")
        encoded_records = [_usage_payload(record) for record in usage_records]
    else:
        audit_records = tuple(record for record in records if isinstance(record, AuditEventRecord))
        if len(audit_records) != len(records):
            raise ValueError("audit spool records 类型非法")
        encoded_records = [_audit_payload(record) for record in audit_records]
    identity = {
        "generation": generation,
        "kind": kind.value,
        "records": encoded_records,
        "version": LOCAL_SPOOL_VERSION,
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    payload = {
        **identity,
        "payload_digest": digest,
        "record_count": len(records),
    }
    rendered = _canonical_json(payload) + "\n"
    if len(rendered.encode("utf-8")) > LOCAL_SPOOL_MAX_FILE_BYTES:
        raise LocalSpoolDrainRequiredError("local spool 单文件超过安全上限")
    return rendered


def _decode_file(
    raw: str,
    *,
    generation: int,
    kind: LocalSpoolKind,
) -> tuple[SpoolRecord, ...]:
    if not raw.endswith("\n") or len(raw.encode("utf-8")) > LOCAL_SPOOL_MAX_FILE_BYTES:
        raise ValueError("spool file framing invalid")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ValueError("spool file is not strict JSON") from None
    expected = {
        "generation",
        "kind",
        "payload_digest",
        "record_count",
        "records",
        "version",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("spool file schema mismatch")
    if _canonical_json(payload) + "\n" != raw:
        raise ValueError("spool file is not canonical")
    if (
        payload["version"] != LOCAL_SPOOL_VERSION
        or payload["generation"] != generation
        or payload["kind"] != kind.value
        or not isinstance(payload["records"], list)
        or not 1 <= len(payload["records"]) <= LOCAL_SPOOL_MAX_RECORDS_PER_FILE
        or payload["record_count"] != len(payload["records"])
        or not isinstance(payload["payload_digest"], str)
        or not _DIGEST_RE.fullmatch(payload["payload_digest"])
    ):
        raise ValueError("spool file identity mismatch")
    identity = {
        "generation": payload["generation"],
        "kind": payload["kind"],
        "records": payload["records"],
        "version": payload["version"],
    }
    expected_digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    if not secrets.compare_digest(payload["payload_digest"], expected_digest):
        raise ValueError("spool file digest mismatch")
    decoder = _usage_from_payload if kind is LocalSpoolKind.USAGE else _audit_from_payload
    return tuple(decoder(item) for item in payload["records"])


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PrivateStorageError(f"私有 spool 目录同步打开失败: {type(error).__name__}") from None
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise PrivateStorageError(f"私有 spool 目录同步失败: {type(error).__name__}") from None
    finally:
        os.close(descriptor)


class LocalUsageAuditSpool:
    """Private file-backed lease spool with durable unknown-result quarantine.

    Each append is one canonical, owner-only file. Leasing durably renames that
    file before any database write. A definitive rollback may rename it back to
    ``ready``; an unknown result is renamed to ``unknown`` and permanently stops
    automatic replay for this generation. On restart, any leftover ``leased``
    file is conservatively converted to ``unknown`` before work can continue.
    """

    def __init__(
        self,
        *,
        generation: int,
        settings: LocalSpoolSettings,
        token_factory: Callable[[], str] = (lambda: secrets.token_hex(16)),
        time_ns: Callable[[], int] = time.time_ns,
        pid_getter: Callable[[], int] = os.getpid,
    ) -> None:
        self._generation = _require_generation(generation)
        if not isinstance(settings, LocalSpoolSettings):
            raise TypeError("settings 必须是 LocalSpoolSettings")
        for value, label in (
            (token_factory, "token_factory"),
            (time_ns, "time_ns"),
            (pid_getter, "pid_getter"),
        ):
            if not callable(value) or inspect.iscoroutinefunction(value):
                raise TypeError(f"{label} 必须是同步 callable")
        self._settings = settings
        self._root = settings.root / f"generation-{generation}"
        self._generation_lock_path = settings.root / f".generation-{generation}.lock"
        self._token_factory = token_factory
        self._time_ns = time_ns
        self._pid_getter = pid_getter
        self._state = LocalSpoolState.CREATED
        self._owner_pid: int | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._async_lock: asyncio.Lock | None = None
        self._wake_event: asyncio.Event | None = None
        self._generation_lock: BinaryIO | None = None
        self._owner_token = object()
        self._next_lease_sequence = 1
        self._occupied_bytes = 0
        self._diagnostics_lock = RLock()
        self._diagnostics: dict[str, int] = {
            "leased_files": 0,
            "ready_bytes": 0,
            "ready_files": 0,
            "ready_records": 0,
            "result_unknown_files": 0,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(generation={self.generation!r}, state={self.state.value!r}, root=<private>)"

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def state(self) -> LocalSpoolState:
        return self._state

    @property
    def settings(self) -> LocalSpoolSettings:
        return self._settings

    def safe_diagnostics(self) -> dict[str, int | str]:
        with self._diagnostics_lock:
            values = dict(self._diagnostics)
        return {
            "generation": self.generation,
            **values,
            "state": self.state.value,
        }

    def _read_pid(self) -> int:
        failed = False
        value: object | None = None
        try:
            value = self._pid_getter()
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError
        except Exception:
            failed = True
        if failed or not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LocalSpoolOwnershipError("local spool 无法确认当前进程")
        return value

    def _bind_owner(self) -> tuple[asyncio.Lock, asyncio.Event]:
        pid = self._read_pid()
        loop = asyncio.get_running_loop()
        if self._owner_pid is None:
            self._owner_pid = pid
            self._owner_loop = loop
            self._async_lock = asyncio.Lock()
            self._wake_event = asyncio.Event()
        elif self._owner_pid != pid or self._owner_loop is not loop:
            raise LocalSpoolOwnershipError("LocalUsageAuditSpool 不得跨进程或 event loop 复用")
        if self._async_lock is None or self._wake_event is None:
            raise LocalSpoolOwnershipError("local spool owner 状态损坏")
        return self._async_lock, self._wake_event

    def _new_token(self) -> str:
        try:
            value = self._token_factory()
        except Exception as error:
            raise LocalSpoolConfigurationError(f"local spool token 生成失败 ({type(error).__name__})") from None
        if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
            raise LocalSpoolConfigurationError("local spool token_factory 必须返回 32 位小写十六进制")
        return value

    def _timestamp(self) -> int:
        try:
            value = self._time_ns()
        except Exception as error:
            raise LocalSpoolConfigurationError(f"local spool clock 失败 ({type(error).__name__})") from None
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 99_999_999_999_999_999_999:
            raise LocalSpoolConfigurationError("local spool time_ns 返回非法值")
        return value

    @staticmethod
    def _parsed_path(path: Path) -> tuple[str, LocalSpoolKind, int, str]:
        match = _FILE_RE.fullmatch(path.name)
        if match is None:
            raise LocalSpoolConfigurationError("local spool 目录包含未知文件")
        return match.group(1), LocalSpoolKind(match.group(2)), int(match.group(3)), match.group(4)

    def _children_sync(self) -> tuple[Path, ...]:
        try:
            entries = tuple(sorted(self._root.iterdir(), key=lambda path: path.name))
        except OSError as error:
            raise LocalSpoolConfigurationError(f"local spool 目录读取失败 ({type(error).__name__})") from None
        for path in entries:
            try:
                info = path.lstat()
            except OSError as error:
                raise LocalSpoolConfigurationError(f"local spool 文件检查失败 ({type(error).__name__})") from None
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise LocalSpoolConfigurationError("local spool 只允许普通文件")
            ensure_private_file(path)
            self._parsed_path(path)
        return entries

    def _acquire_generation_lock_sync(self) -> None:
        if self._generation_lock is not None:
            raise LocalSpoolOwnershipError("local spool generation lock 已被当前实例持有")
        required_flags = ("O_CLOEXEC", "O_NOFOLLOW")
        missing = [name for name in required_flags if not hasattr(os, name)]
        if os.name != "posix" or fcntl is None or not callable(getattr(os, "geteuid", None)) or missing:
            raise LocalSpoolConfigurationError(f"local spool 需要 POSIX flock/UID/O_CLOEXEC/O_NOFOLLOW；missing={missing}")

        ensure_private_directory(self._settings.root)
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        descriptor = -1
        try:
            try:
                descriptor = os.open(
                    self._generation_lock_path,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                created = True
            except FileExistsError:
                descriptor = os.open(self._generation_lock_path, flags)
            except OSError as error:
                raise LocalSpoolConfigurationError(f"local spool generation lock 打开失败 ({type(error).__name__})") from None

            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                _fsync_directory(self._settings.root)
            ensure_private_file(self._generation_lock_path)
            descriptor_info = os.fstat(descriptor)
            path_info = self._generation_lock_path.lstat()
            if (
                not stat.S_ISREG(descriptor_info.st_mode)
                or descriptor_info.st_uid != os.geteuid()
                or stat.S_IMODE(descriptor_info.st_mode) != 0o600
                or stat.S_ISLNK(path_info.st_mode)
                or (path_info.st_dev, path_info.st_ino) != (descriptor_info.st_dev, descriptor_info.st_ino)
            ):
                raise LocalSpoolConfigurationError("local spool generation lock identity 或权限非法")

            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno == errno.EINTR:
                        continue
                    if error.errno in {errno.EACCES, errno.EAGAIN}:
                        raise LocalSpoolOwnershipError("同一 generation 已存在活跃 owner") from None
                    raise LocalSpoolConfigurationError(f"local spool generation lock 获取失败 ({type(error).__name__})") from None

            locked_info = os.fstat(descriptor)
            current_info = self._generation_lock_path.lstat()
            if stat.S_ISLNK(current_info.st_mode) or (
                current_info.st_dev,
                current_info.st_ino,
            ) != (locked_info.st_dev, locked_info.st_ino):
                raise LocalSpoolConfigurationError("local spool generation lock 在获取期间被替换")
            self._generation_lock = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = -1
        except BaseException:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def _release_generation_lock_sync(self) -> None:
        handle = self._generation_lock
        if handle is None:
            return
        self._generation_lock = None
        failure: OSError | None = None
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError as error:
                    failure = error
        finally:
            try:
                handle.close()
            except OSError as error:
                failure = failure or error
        if failure is not None:
            raise LocalSpoolConfigurationError(f"local spool generation lock 释放失败 ({type(failure).__name__})") from None

    def _read_records_sync(self, path: Path, *, expected_kind: LocalSpoolKind | None = None) -> tuple[SpoolRecord, ...]:
        _state, kind, _timestamp, _token = self._parsed_path(path)
        if expected_kind is not None and kind is not expected_kind:
            raise LocalSpoolConfigurationError("local spool 文件 kind 漂移")
        try:
            raw = path.read_text(encoding="utf-8")
            return _decode_file(raw, generation=self.generation, kind=kind)
        except (OSError, UnicodeError, ValueError, LocalSpoolError) as error:
            raise LocalSpoolDrainRequiredError(f"local spool 文件损坏，已拒绝处理 ({type(error).__name__})") from None

    def _refresh_diagnostics_sync(self) -> None:
        ready_files = 0
        ready_bytes = 0
        ready_records = 0
        leased_files = 0
        unknown_files = 0
        occupied_bytes = 0
        for path in self._children_sync():
            state, _kind, _timestamp, _token = self._parsed_path(path)
            if state == "ready":
                ready_files += 1
                size = path.stat().st_size
                ready_bytes += size
                occupied_bytes += size
                ready_records += len(self._read_records_sync(path))
            elif state == "leased":
                leased_files += 1
                occupied_bytes += path.stat().st_size
            else:
                unknown_files += 1
        self._occupied_bytes = occupied_bytes
        with self._diagnostics_lock:
            self._diagnostics = {
                "leased_files": leased_files,
                "ready_bytes": ready_bytes,
                "ready_files": ready_files,
                "ready_records": ready_records,
                "result_unknown_files": unknown_files,
            }

    def _start_sync(self) -> None:
        ensure_private_directory(self._settings.root)
        self._acquire_generation_lock_sync()
        try:
            ensure_private_directory(self._root)
            for path in self._children_sync():
                state, kind, timestamp, token = self._parsed_path(path)
                self._read_records_sync(path)
                if state == "leased":
                    target = self._root / f"unknown.{kind.value}.{timestamp:020d}.{token}.json"
                    try:
                        os.replace(path, target)
                    except OSError as error:
                        raise LocalSpoolConfigurationError(f"local spool 中断租约隔离失败 ({type(error).__name__})") from None
                    ensure_private_file(target)
                    _fsync_directory(self._root)
            self._refresh_diagnostics_sync()
        except BaseException:
            self._release_generation_lock_sync()
            raise

    async def start(self) -> LocalUsageAuditSpool:
        lock, wake = self._bind_owner()
        async with lock:
            if self._state is LocalSpoolState.RUNNING:
                return self
            if self._state is not LocalSpoolState.CREATED:
                raise LocalSpoolLifecycleError("local spool 当前不可启动")
            try:
                await asyncio.to_thread(self._start_sync)
            except asyncio.CancelledError:
                raise
            except LocalSpoolError:
                raise
            except Exception as error:
                raise LocalSpoolConfigurationError(f"local spool 启动失败 ({type(error).__name__})") from None
            if self._diagnostics["result_unknown_files"]:
                self._state = LocalSpoolState.RESULT_UNKNOWN
            else:
                self._state = LocalSpoolState.RUNNING
            if self._diagnostics["ready_files"]:
                wake.set()
            return self

    def _require_running(self) -> None:
        if self._state is LocalSpoolState.RESULT_UNKNOWN:
            raise LocalSpoolResultUnknownError("local spool durable result unknown；禁止自动重放")
        if self._state is not LocalSpoolState.RUNNING:
            raise LocalSpoolLifecycleError("local spool 未运行或已关闭")

    def _append_sync(self, kind: LocalSpoolKind, records: tuple[SpoolRecord, ...]) -> None:
        rendered = _encode_file(generation=self.generation, kind=kind, records=records)
        encoded_bytes = len(rendered.encode("utf-8"))
        self._refresh_diagnostics_sync()
        total_files = self._diagnostics["ready_files"] + self._diagnostics["leased_files"]
        if total_files >= self._settings.max_ready_files or self._occupied_bytes + encoded_bytes > self._settings.max_ready_bytes:
            raise LocalSpoolDrainRequiredError("local spool 已达到有界容量上限")
        timestamp = self._timestamp()
        token = self._new_token()
        path = self._root / f"ready.{kind.value}.{timestamp:020d}.{token}.json"
        if path.exists():
            raise LocalSpoolConfigurationError("local spool 文件 identity 冲突")
        atomic_write_private_text(path, rendered)
        _fsync_directory(self._root)
        self._refresh_diagnostics_sync()

    async def _append(self, kind: LocalSpoolKind, records: tuple[SpoolRecord, ...]) -> None:
        lock, wake = self._bind_owner()
        async with lock:
            self._require_running()
            await asyncio.to_thread(self._append_sync, kind, records)
            wake.set()

    async def append_usage(self, records: tuple[ModelUsageRecord, ...]) -> None:
        if not isinstance(records, tuple):
            raise TypeError("usage records 必须是 tuple")
        await self._append(LocalSpoolKind.USAGE, records)

    async def append_audit(self, records: tuple[AuditEventRecord, ...]) -> None:
        if not isinstance(records, tuple):
            raise TypeError("audit records 必须是 tuple")
        await self._append(LocalSpoolKind.AUDIT, records)

    def _lease_sync(self, kind: LocalSpoolKind) -> LocalSpoolLease | None:
        selected: Path | None = None
        selected_parts: tuple[str, LocalSpoolKind, int, str] | None = None
        for path in self._children_sync():
            parts = self._parsed_path(path)
            if parts[0] == "ready" and parts[1] is kind:
                selected = path
                selected_parts = parts
                break
        if selected is None or selected_parts is None:
            self._refresh_diagnostics_sync()
            return None
        records = self._read_records_sync(selected, expected_kind=kind)
        _state, _kind, timestamp, token = selected_parts
        sequence = self._next_lease_sequence
        lease_token = self._new_token()
        lease_id = f"{sequence}:{lease_token}"
        leased_path = self._root / f"leased.{kind.value}.{timestamp:020d}.{token}.json"
        try:
            os.replace(selected, leased_path)
        except OSError as error:
            raise LocalSpoolConfigurationError(f"local spool lease 持久化失败 ({type(error).__name__})") from None
        ensure_private_file(leased_path)
        _fsync_directory(self._root)
        self._next_lease_sequence += 1
        lease = LocalSpoolLease(
            lease_id=lease_id,
            kind=kind,
            generation=self.generation,
            records=records,
            _path=leased_path,
            _owner_token=self._owner_token,
        )
        self._refresh_diagnostics_sync()
        return lease

    async def lease_next(self, kind: LocalSpoolKind) -> LocalSpoolLease | None:
        if not isinstance(kind, LocalSpoolKind):
            raise TypeError("kind 必须是 LocalSpoolKind")
        lock, wake = self._bind_owner()
        async with lock:
            self._require_running()
            lease = await asyncio.to_thread(self._lease_sync, kind)
            if self._diagnostics["ready_files"] == 0:
                wake.clear()
            return lease

    def _require_lease(self, lease: LocalSpoolLease) -> None:
        if not isinstance(lease, LocalSpoolLease) or lease._owner_token is not self._owner_token:
            raise ValueError("lease 不属于当前 local spool")
        if lease.generation != self.generation:
            raise ValueError("lease generation 与 local spool 不一致")
        try:
            state, kind, _timestamp, _token = self._parsed_path(lease._path)
        except LocalSpoolError:
            raise ValueError("lease path 非法") from None
        if state != "leased" or kind is not lease.kind or not lease._path.exists():
            raise ValueError("lease 已结束或 identity 漂移")

    def _rename_lease_sync(self, lease: LocalSpoolLease, state: str) -> None:
        self._require_lease(lease)
        _old_state, kind, timestamp, token = self._parsed_path(lease._path)
        target = self._root / f"{state}.{kind.value}.{timestamp:020d}.{token}.json"
        try:
            os.replace(lease._path, target)
        except OSError as error:
            raise LocalSpoolConfigurationError(f"local spool lease 状态写入失败 ({type(error).__name__})") from None
        ensure_private_file(target)
        _fsync_directory(self._root)
        self._refresh_diagnostics_sync()

    def _ack_sync(self, lease: LocalSpoolLease) -> None:
        self._require_lease(lease)
        try:
            lease._path.unlink()
        except OSError as error:
            raise LocalSpoolConfigurationError(f"local spool committed ack 失败 ({type(error).__name__})") from None
        _fsync_directory(self._root)
        self._refresh_diagnostics_sync()

    async def acknowledge_committed(self, lease: LocalSpoolLease) -> None:
        lock, wake = self._bind_owner()
        async with lock:
            self._require_running()
            await asyncio.to_thread(self._ack_sync, lease)
            if self._diagnostics["ready_files"]:
                wake.set()

    async def release_unwritten(self, lease: LocalSpoolLease) -> None:
        lock, wake = self._bind_owner()
        async with lock:
            self._require_running()
            await asyncio.to_thread(self._rename_lease_sync, lease, "ready")
            wake.set()

    async def mark_result_unknown(self, lease: LocalSpoolLease) -> None:
        lock, wake = self._bind_owner()
        async with lock:
            self._require_running()
            await asyncio.to_thread(self._rename_lease_sync, lease, "unknown")
            self._state = LocalSpoolState.RESULT_UNKNOWN
            wake.set()

    async def wait_for_ready(self, timeout_seconds: float | None = None) -> bool:
        _lock, wake = self._bind_owner()
        self._require_running()
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) < 0
        ):
            raise ValueError("timeout_seconds 必须是有限非负秒数或 None")
        if self._diagnostics["ready_files"]:
            return True
        try:
            if timeout_seconds is None:
                await wake.wait()
            else:
                await asyncio.wait_for(wake.wait(), timeout=float(timeout_seconds))
        except asyncio.TimeoutError:
            return False
        self._require_running()
        return self._diagnostics["ready_files"] > 0

    async def close(self) -> None:
        lock, _wake = self._bind_owner()
        async with lock:
            if self._state is LocalSpoolState.CLOSED:
                return
            if self._state is LocalSpoolState.CREATED:
                self._state = LocalSpoolState.CLOSED
                return
            await asyncio.to_thread(self._refresh_diagnostics_sync)
            if self._state is LocalSpoolState.RESULT_UNKNOWN or self._diagnostics["result_unknown_files"]:
                self._state = LocalSpoolState.RESULT_UNKNOWN
                raise LocalSpoolDrainRequiredError("local spool 存在 result_unknown 记录，禁止自动清理")
            if self._diagnostics["ready_files"] or self._diagnostics["leased_files"]:
                self._state = LocalSpoolState.RUNNING
                raise LocalSpoolDrainRequiredError("local spool 仍含未确认 durable 记录")
            self._state = LocalSpoolState.CLOSING
            try:
                self._release_generation_lock_sync()
            finally:
                if self._generation_lock is None:
                    self._state = LocalSpoolState.CLOSED


__all__ = [
    "LOCAL_SPOOL_DEFAULT_MAX_READY_BYTES",
    "LOCAL_SPOOL_DEFAULT_MAX_READY_FILES",
    "LOCAL_SPOOL_MAX_FILE_BYTES",
    "LOCAL_SPOOL_MAX_RECORDS_PER_FILE",
    "LOCAL_SPOOL_VERSION",
    "LocalSpoolConfigurationError",
    "LocalSpoolDrainRequiredError",
    "LocalSpoolError",
    "LocalSpoolKind",
    "LocalSpoolLease",
    "LocalSpoolLifecycleError",
    "LocalSpoolOwnershipError",
    "LocalSpoolResultUnknownError",
    "LocalSpoolSettings",
    "LocalSpoolState",
    "LocalUsageAuditSpool",
]

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import math
import re
from types import MappingProxyType
from typing import Any, TypeAlias

from .database_schema import (
    AUDIT_ACTOR_TYPE_MAX_CHARS,
    AUDIT_EVENT_TYPE_MAX_CHARS,
    AUDIT_METADATA_MAX_BYTES,
    AUDIT_TARGET_TYPE_MAX_CHARS,
    ENTITY_ID_MAX_CHARS,
)

MAX_AUDIT_BATCH_SIZE = 100

_AUDIT_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_AUDIT_ACTOR_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,31}$")
_AUDIT_TARGET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_AUDIT_JSON_MAX_DEPTH = 32
_AUDIT_JSON_MAX_NODES = 100_000
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

BATCHABLE_AUDIT_EVENT_TYPES = frozenset(
    {
        "runtime_reload",
        "runtime_reload_failed",
        "tool_draft_created",
    }
)

AuditJsonValue: TypeAlias = (
    bool | int | float | str | Mapping[str, "AuditJsonValue"] | list["AuditJsonValue"] | tuple["AuditJsonValue", ...] | None
)


class AuditWriteMode(str, Enum):
    """Whether an event may wait in the non-critical batch queue."""

    IMMEDIATE = "immediate"
    BATCH = "batch"


def _require_token(
    value: object,
    *,
    label: str,
    maximum: int,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(value, str) or len(value) > maximum or not pattern.fullmatch(value):
        raise ValueError(f"{label} 必须是 canonical audit token")
    return value


def _require_entity_id(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > ENTITY_ID_MAX_CHARS
        or _CONTROL_CHARACTER_RE.search(value)
    ):
        raise ValueError(f"{label} 必须是安全的有界非空标识")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} 必须是有效 UTF-8 标识") from None
    return value


def _require_optional_entity_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_entity_id(value, label=label)


def validate_audit_run_id(value: object) -> str:
    """Validate the AgentRun identity shared by audit records and cursors."""

    return _require_entity_id(value, label="run_id")


def _require_optional_positive_bigint(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT 或 None")
    return value


def _normalize_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} 必须是带时区的 datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise ValueError(f"{label} 必须是有效的带时区 datetime") from None


def _validate_json_string(value: str, *, label: str) -> str:
    if "\x00" in value:
        raise ValueError(f"{label} JSON 字符串不得包含 NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{label} JSON 字符串必须是有效 UTF-8 文本") from None
    return value


def _freeze_audit_json(
    value: AuditJsonValue,
    *,
    label: str,
    depth: int = 0,
    active_containers: set[int] | None = None,
    node_budget: list[int] | None = None,
) -> AuditJsonValue:
    if depth > _AUDIT_JSON_MAX_DEPTH:
        raise ValueError(f"{label} JSON 嵌套超过安全上限")
    budget = node_budget if node_budget is not None else [0]
    budget[0] += 1
    if budget[0] > _AUDIT_JSON_MAX_NODES:
        raise ValueError(f"{label} JSON 节点数超过安全上限")

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} JSON 浮点数必须有限")
        return value
    if isinstance(value, str):
        return _validate_json_string(value, label=label)

    if isinstance(value, Mapping):
        active = active_containers if active_containers is not None else set()
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} JSON 不得包含循环引用")
        active.add(identity)
        try:
            frozen: dict[str, AuditJsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{label} JSON 对象键必须是字符串")
                _validate_json_string(key, label=label)
                if key in frozen:
                    raise ValueError(f"{label} JSON 对象不得包含重复字段")
                frozen[key] = _freeze_audit_json(
                    item,
                    label=label,
                    depth=depth + 1,
                    active_containers=active,
                    node_budget=budget,
                )
        finally:
            active.remove(identity)
        return MappingProxyType(frozen)

    if isinstance(value, (list, tuple)):
        active = active_containers if active_containers is not None else set()
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} JSON 不得包含循环引用")
        active.add(identity)
        try:
            return tuple(
                _freeze_audit_json(
                    item,
                    label=label,
                    depth=depth + 1,
                    active_containers=active,
                    node_budget=budget,
                )
                for item in value
            )
        finally:
            active.remove(identity)

    raise ValueError(f"{label} 必须是 JSON 兼容值")


def mutable_audit_json(value: AuditJsonValue) -> Any:
    """Return a detached JSON value suitable for a database driver."""

    if isinstance(value, Mapping):
        return {key: mutable_audit_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mutable_audit_json(item) for item in value]
    return value


def _metadata_size_bytes(value: Mapping[str, AuditJsonValue]) -> int:
    try:
        rendered = json.dumps(
            mutable_audit_json(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        compact_size = len(rendered.encode("utf-8"))
        postgres_text_size = _postgres_json_text_size(value)
        return max(compact_size, postgres_text_size)
    except (OverflowError, TypeError, UnicodeEncodeError, ValueError):
        raise ValueError("AuditEventRecord.metadata_json 必须是规范 JSON object") from None


def _postgres_json_text_size(value: AuditJsonValue) -> int:
    """Conservatively size the normalized ``jsonb::text`` representation."""

    if value is None:
        return 4
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, int):
        return len(str(value).encode("ascii"))
    if isinstance(value, float):
        # PostgreSQL JSONB normalizes exponent notation to its numeric text form.
        return len(format(Decimal(repr(value)), "f").encode("ascii"))
    if isinstance(value, str):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if isinstance(value, Mapping):
        size = 2
        for index, (key, item) in enumerate(value.items()):
            if index:
                size += 2  # `, `
            size += len(json.dumps(key, ensure_ascii=False).encode("utf-8"))
            size += 2  # `: `
            size += _postgres_json_text_size(item)
        return size
    if isinstance(value, (list, tuple)):
        size = 2
        for index, item in enumerate(value):
            if index:
                size += 2  # `, `
            size += _postgres_json_text_size(item)
        return size
    raise TypeError("unsupported audit JSON value")


@dataclass(frozen=True)
class AuditEventRecord:
    """Detached immutable event aligned with the existing ``audit_events`` table."""

    event_id: int | None
    event_type: str
    actor_user_id: str | None
    actor_type: str
    target_type: str
    target_id: str
    run_id: str | None
    tool_call_id: str | None
    metadata_json: Mapping[str, AuditJsonValue] = field(repr=False)
    created_at: datetime

    def __post_init__(self) -> None:
        _require_optional_positive_bigint(
            self.event_id,
            label="AuditEventRecord.event_id",
        )
        _require_token(
            self.event_type,
            label="AuditEventRecord.event_type",
            maximum=AUDIT_EVENT_TYPE_MAX_CHARS,
            pattern=_AUDIT_EVENT_TYPE_RE,
        )
        _require_optional_entity_id(
            self.actor_user_id,
            label="AuditEventRecord.actor_user_id",
        )
        _require_token(
            self.actor_type,
            label="AuditEventRecord.actor_type",
            maximum=AUDIT_ACTOR_TYPE_MAX_CHARS,
            pattern=_AUDIT_ACTOR_TYPE_RE,
        )
        _require_token(
            self.target_type,
            label="AuditEventRecord.target_type",
            maximum=AUDIT_TARGET_TYPE_MAX_CHARS,
            pattern=_AUDIT_TARGET_TYPE_RE,
        )
        _require_entity_id(self.target_id, label="AuditEventRecord.target_id")
        run_id = _require_optional_entity_id(
            self.run_id,
            label="AuditEventRecord.run_id",
        )
        tool_call_id = _require_optional_entity_id(
            self.tool_call_id,
            label="AuditEventRecord.tool_call_id",
        )
        if tool_call_id is not None and run_id is None:
            raise ValueError("AuditEventRecord.tool_call_id 必须同时绑定 run_id")
        if not isinstance(self.metadata_json, Mapping):
            raise ValueError("AuditEventRecord.metadata_json 必须是 JSON object")
        frozen_metadata = _freeze_audit_json(
            self.metadata_json,
            label="AuditEventRecord.metadata_json",
        )
        if not isinstance(frozen_metadata, Mapping):
            raise ValueError("AuditEventRecord.metadata_json 必须是 JSON object")
        if _metadata_size_bytes(frozen_metadata) > AUDIT_METADATA_MAX_BYTES:
            raise ValueError("AuditEventRecord.metadata_json 超过 64 KiB 上限")
        created_at = _normalize_datetime(
            self.created_at,
            label="AuditEventRecord.created_at",
        )
        object.__setattr__(self, "metadata_json", frozen_metadata)
        object.__setattr__(self, "created_at", created_at)

    @property
    def persisted(self) -> bool:
        return self.event_id is not None

    @property
    def write_mode(self) -> AuditWriteMode:
        if self.event_type in BATCHABLE_AUDIT_EVENT_TYPES:
            return AuditWriteMode.BATCH
        return AuditWriteMode.IMMEDIATE

    @property
    def batchable(self) -> bool:
        return self.write_mode is AuditWriteMode.BATCH

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor_user_id": self.actor_user_id,
            "actor_type": self.actor_type,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "metadata_json": self.metadata_json,
            "created_at": self.created_at,
        }

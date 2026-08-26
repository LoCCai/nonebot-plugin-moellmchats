from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from types import MappingProxyType
from typing import Any, TypeAlias

from .database_schema import (
    CONVERSATION_TYPE_MAX_CHARS,
    DISPLAY_NAME_MAX_CHARS,
    ENTITY_ID_MAX_CHARS,
    MESSAGE_ROLE_MAX_CHARS,
    PLATFORM_MAX_CHARS,
)

_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_HISTORY_JSON_MAX_DEPTH = 32
_HISTORY_JSON_MAX_NODES = 100_000
_POSTGRES_BIGINT_MAX = (1 << 63) - 1

HistoryJsonValue: TypeAlias = (
    bool | int | float | str | Mapping[str, "HistoryJsonValue"] | list["HistoryJsonValue"] | tuple["HistoryJsonValue", ...] | None
)


def _require_bounded_text(
    value: object,
    *,
    label: str,
    maximum: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _CONTROL_CHARACTER_RE.search(value)
    ):
        raise ValueError(f"{label} 必须是无首尾空白和控制字符的有界非空字符串")
    return value


def _require_optional_bounded_text(
    value: object,
    *,
    label: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _require_bounded_text(value, label=label, maximum=maximum)


def _normalize_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} 必须是带时区的 datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise ValueError(f"{label} 必须是有效的带时区 datetime") from None


def _freeze_history_json(
    value: HistoryJsonValue,
    *,
    label: str,
    depth: int = 0,
    active_containers: set[int] | None = None,
    node_budget: list[int] | None = None,
) -> HistoryJsonValue:
    if depth > _HISTORY_JSON_MAX_DEPTH:
        raise ValueError(f"{label} JSON 嵌套超过安全上限")
    budget = node_budget if node_budget is not None else [0]
    budget[0] += 1
    if budget[0] > _HISTORY_JSON_MAX_NODES:
        raise ValueError(f"{label} JSON 节点数超过安全上限")

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} JSON 浮点数必须有限")
        return value
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError(f"{label} JSON 字符串不得包含 NUL")
        return value

    if isinstance(value, Mapping):
        active = active_containers if active_containers is not None else set()
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} JSON 不得包含循环引用")
        active.add(identity)
        try:
            frozen: dict[str, HistoryJsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str) or "\x00" in key:
                    raise ValueError(f"{label} JSON 对象键必须是不含 NUL 的字符串")
                frozen[key] = _freeze_history_json(
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
                _freeze_history_json(
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


def mutable_history_json(value: HistoryJsonValue) -> Any:
    """Return a detached mutable JSON value suitable for a database driver."""

    if isinstance(value, Mapping):
        return {key: mutable_history_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mutable_history_json(item) for item in value]
    return value


def validate_conversation_id(value: object) -> str:
    """Validate the durable conversation identity shared by records and cursors."""

    return _require_bounded_text(
        value,
        label="conversation_id",
        maximum=ENTITY_ID_MAX_CHARS,
    )


def validate_user_id(value: object) -> str:
    """Validate the durable user identity shared by runtime records."""

    return _require_bounded_text(
        value,
        label="user_id",
        maximum=ENTITY_ID_MAX_CHARS,
    )


@dataclass(frozen=True)
class UserRecord:
    """Detached immutable representation of one platform user."""

    user_id: str
    platform: str
    platform_user_id: str
    display_name: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        validate_user_id(self.user_id)
        _require_bounded_text(
            self.platform,
            label="UserRecord.platform",
            maximum=PLATFORM_MAX_CHARS,
        )
        _require_bounded_text(
            self.platform_user_id,
            label="UserRecord.platform_user_id",
            maximum=ENTITY_ID_MAX_CHARS,
        )
        _require_optional_bounded_text(
            self.display_name,
            label="UserRecord.display_name",
            maximum=DISPLAY_NAME_MAX_CHARS,
        )
        created_at = _normalize_datetime(
            self.created_at,
            label="UserRecord.created_at",
        )
        updated_at = _normalize_datetime(
            self.updated_at,
            label="UserRecord.updated_at",
        )
        if updated_at < created_at:
            raise ValueError("UserRecord.updated_at 不能早于 created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    def as_dict(self) -> dict[str, str | datetime | None]:
        return {
            "user_id": self.user_id,
            "platform": self.platform,
            "platform_user_id": self.platform_user_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ConversationRecord:
    """Detached immutable representation of one durable chat conversation."""

    conversation_id: str
    conversation_type: str
    platform: str
    group_id: str | None
    user_id: str | None
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_conversation_id(self.conversation_id)
        _require_bounded_text(
            self.conversation_type,
            label="ConversationRecord.conversation_type",
            maximum=CONVERSATION_TYPE_MAX_CHARS,
        )
        _require_bounded_text(
            self.platform,
            label="ConversationRecord.platform",
            maximum=PLATFORM_MAX_CHARS,
        )
        _require_optional_bounded_text(
            self.group_id,
            label="ConversationRecord.group_id",
            maximum=ENTITY_ID_MAX_CHARS,
        )
        _require_optional_bounded_text(
            self.user_id,
            label="ConversationRecord.user_id",
            maximum=ENTITY_ID_MAX_CHARS,
        )
        if self.group_id is None and self.user_id is None:
            raise ValueError("ConversationRecord 必须绑定 group_id 或 user_id")

        created_at = _normalize_datetime(
            self.created_at,
            label="ConversationRecord.created_at",
        )
        updated_at = _normalize_datetime(
            self.updated_at,
            label="ConversationRecord.updated_at",
        )
        last_message_at = (
            None
            if self.last_message_at is None
            else _normalize_datetime(
                self.last_message_at,
                label="ConversationRecord.last_message_at",
            )
        )
        if updated_at < created_at:
            raise ValueError("ConversationRecord.updated_at 不能早于 created_at")

        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "last_message_at", last_message_at)

    def as_dict(self) -> dict[str, str | datetime | None]:
        return {
            "conversation_id": self.conversation_id,
            "conversation_type": self.conversation_type,
            "platform": self.platform,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_message_at": self.last_message_at,
        }


@dataclass(frozen=True)
class MessageRecord:
    """Detached immutable chat message; ``message_id=None`` denotes a draft."""

    message_id: int | None
    conversation_id: str
    role: str
    created_at: datetime
    platform_message_id: str | None = None
    sender_id: str | None = None
    content: str | None = None
    structured_content: HistoryJsonValue = None

    def __post_init__(self) -> None:
        if self.message_id is not None and (
            not isinstance(self.message_id, int)
            or isinstance(self.message_id, bool)
            or not 1 <= self.message_id <= _POSTGRES_BIGINT_MAX
        ):
            raise ValueError("MessageRecord.message_id 必须是正 PostgreSQL BIGINT 或 None")
        validate_conversation_id(self.conversation_id)
        _require_bounded_text(
            self.role,
            label="MessageRecord.role",
            maximum=MESSAGE_ROLE_MAX_CHARS,
        )
        _require_optional_bounded_text(
            self.platform_message_id,
            label="MessageRecord.platform_message_id",
            maximum=ENTITY_ID_MAX_CHARS,
        )
        _require_optional_bounded_text(
            self.sender_id,
            label="MessageRecord.sender_id",
            maximum=ENTITY_ID_MAX_CHARS,
        )
        if self.content is not None:
            if not isinstance(self.content, str) or "\x00" in self.content:
                raise ValueError("MessageRecord.content 必须是不含 NUL 的字符串或 None")

        structured_content = _freeze_history_json(
            self.structured_content,
            label="MessageRecord.structured_content",
        )
        if self.content is None and structured_content is None:
            raise ValueError("MessageRecord 必须包含 content 或 structured_content")
        created_at = _normalize_datetime(
            self.created_at,
            label="MessageRecord.created_at",
        )

        object.__setattr__(self, "structured_content", structured_content)
        object.__setattr__(self, "created_at", created_at)

    @property
    def persisted(self) -> bool:
        return self.message_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "platform_message_id": self.platform_message_id,
            "role": self.role,
            "sender_id": self.sender_id,
            "content": self.content,
            "structured_content": mutable_history_json(self.structured_content),
            "created_at": self.created_at,
        }

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from itertools import pairwise
import json
import re

from .chat_history import MessageRecord, mutable_history_json, validate_conversation_id
from .database_schema import (
    ENTITY_ID_MAX_CHARS,
    MODEL_NAME_MAX_CHARS,
    MODEL_PROVIDER_MAX_CHARS,
    SESSION_SUMMARY_MAX_CHARS,
    SESSION_SUMMARY_POLICY_MAX_CHARS,
)

SESSION_SUMMARY_POLICY_VERSION = "session-summary-v1"
DEFAULT_SUMMARY_TRIGGER_MESSAGES = 50
DEFAULT_SUMMARY_KEEP_RECENT_MESSAGES = 10
DEFAULT_SUMMARY_MAX_SOURCE_CHARS = 64_000
MAX_SUMMARY_CANDIDATE_MESSAGES = 200

_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_POLICY_VERSION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1


class SessionSummaryError(RuntimeError):
    """Base error for deterministic session-summary preparation."""


class SessionSummarySourceTooLargeError(SessionSummaryError):
    """The next complete source message cannot fit within the input boundary."""


def _require_bounded_identity(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > ENTITY_ID_MAX_CHARS
        or _CONTROL_CHARACTER_RE.search(value)
    ):
        raise ValueError(f"{label} 必须是无首尾空白和控制字符的有界非空字符串")
    return value


def _require_bounded_label(
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


def _require_positive_bigint(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT")
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


def _require_summary_content(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > SESSION_SUMMARY_MAX_CHARS
        or "\x00" in value
    ):
        raise ValueError(
            f"SessionSummaryRecord.content 必须是 1 到 {SESSION_SUMMARY_MAX_CHARS} 个字符、无首尾空白且不含 NUL 的字符串"
        )
    return value


def _message_payload(message: MessageRecord) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "platform_message_id": message.platform_message_id,
        "role": message.role,
        "sender_id": message.sender_id,
        "content": message.content,
        "structured_content": mutable_history_json(message.structured_content),
        "created_at": message.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }


def _previous_summary_payload(summary: SessionSummaryRecord | None) -> dict[str, object] | None:
    if summary is None:
        return None
    return {
        "summary_id": summary.summary_id,
        "generation": summary.generation,
        "covered_through_message_id": summary.covered_through_message_id,
        "covered_message_count": summary.covered_message_count,
        "source_digest": summary.source_digest,
        "content": summary.content,
    }


def _render_model_input(
    *,
    conversation_id: str,
    previous_summary: SessionSummaryRecord | None,
    source_messages: tuple[MessageRecord, ...],
    trigger_message_count: int,
    keep_recent_message_count: int,
) -> str:
    payload = {
        "schema": SESSION_SUMMARY_POLICY_VERSION,
        "conversation_sha256": hashlib.sha256(conversation_id.encode("utf-8")).hexdigest(),
        "policy": {
            "trigger_message_count": trigger_message_count,
            "keep_recent_message_count": keep_recent_message_count,
        },
        "previous_summary": _previous_summary_payload(previous_summary),
        "messages": [_message_payload(message) for message in source_messages],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class SessionSummaryRecord:
    """One immutable node in a conversation's append-only summary chain."""

    summary_id: str
    conversation_id: str
    generation: int
    previous_summary_id: str | None
    covered_from_message_id: int
    covered_through_message_id: int
    covered_message_count: int
    source_message_count: int
    source_digest: str
    policy_version: str
    trigger_message_count: int
    keep_recent_message_count: int
    max_source_chars: int
    source_char_count: int
    model_provider: str
    model: str
    content: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_bounded_identity(self.summary_id, label="SessionSummaryRecord.summary_id")
        validate_conversation_id(self.conversation_id)
        _require_positive_bigint(self.generation, label="SessionSummaryRecord.generation")
        if self.previous_summary_id is not None:
            _require_bounded_identity(
                self.previous_summary_id,
                label="SessionSummaryRecord.previous_summary_id",
            )
        if (self.generation == 1) != (self.previous_summary_id is None):
            raise ValueError("SessionSummaryRecord generation=1 必须且只能没有 previous_summary_id")

        covered_from = _require_positive_bigint(
            self.covered_from_message_id,
            label="SessionSummaryRecord.covered_from_message_id",
        )
        covered_through = _require_positive_bigint(
            self.covered_through_message_id,
            label="SessionSummaryRecord.covered_through_message_id",
        )
        if covered_from > covered_through:
            raise ValueError("SessionSummaryRecord 覆盖消息水位顺序无效")
        covered_count = _require_positive_bigint(
            self.covered_message_count,
            label="SessionSummaryRecord.covered_message_count",
        )
        source_count = _require_positive_bigint(
            self.source_message_count,
            label="SessionSummaryRecord.source_message_count",
        )
        if source_count > covered_count:
            raise ValueError("SessionSummaryRecord.source_message_count 不得超过累计覆盖数")
        if self.generation == 1 and source_count != covered_count:
            raise ValueError("首个 SessionSummaryRecord 的源消息数必须等于累计覆盖数")

        if not isinstance(self.source_digest, str) or not _SHA256_RE.fullmatch(self.source_digest):
            raise ValueError("SessionSummaryRecord.source_digest 必须是小写 SHA-256")
        if (
            not isinstance(self.policy_version, str)
            or len(self.policy_version) > SESSION_SUMMARY_POLICY_MAX_CHARS
            or not _POLICY_VERSION_RE.fullmatch(self.policy_version)
        ):
            raise ValueError("SessionSummaryRecord.policy_version 必须是安全有界标识")
        if self.policy_version != SESSION_SUMMARY_POLICY_VERSION:
            raise ValueError("SessionSummaryRecord.policy_version 当前不受支持")
        _validate_policy_bounds(
            trigger_message_count=self.trigger_message_count,
            keep_recent_message_count=self.keep_recent_message_count,
            max_source_chars=self.max_source_chars,
        )
        if (
            not isinstance(self.source_char_count, int)
            or isinstance(self.source_char_count, bool)
            or not 1 <= self.source_char_count <= self.max_source_chars
        ):
            raise ValueError("SessionSummaryRecord.source_char_count 超出输入边界")

        _require_bounded_label(
            self.model_provider,
            label="SessionSummaryRecord.model_provider",
            maximum=MODEL_PROVIDER_MAX_CHARS,
        )
        _require_bounded_label(
            self.model,
            label="SessionSummaryRecord.model",
            maximum=MODEL_NAME_MAX_CHARS,
        )
        _require_summary_content(self.content)
        created_at = _normalize_datetime(
            self.created_at,
            label="SessionSummaryRecord.created_at",
        )
        object.__setattr__(self, "created_at", created_at)

    def as_dict(self) -> dict[str, object]:
        return {
            "summary_id": self.summary_id,
            "conversation_id": self.conversation_id,
            "generation": self.generation,
            "previous_summary_id": self.previous_summary_id,
            "covered_from_message_id": self.covered_from_message_id,
            "covered_through_message_id": self.covered_through_message_id,
            "covered_message_count": self.covered_message_count,
            "source_message_count": self.source_message_count,
            "source_digest": self.source_digest,
            "policy_version": self.policy_version,
            "trigger_message_count": self.trigger_message_count,
            "keep_recent_message_count": self.keep_recent_message_count,
            "max_source_chars": self.max_source_chars,
            "source_char_count": self.source_char_count,
            "model_provider": self.model_provider,
            "model": self.model,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SessionSummaryPlan:
    """A bounded, digest-bound model input plus the messages it leaves recent."""

    conversation_id: str
    previous_summary: SessionSummaryRecord | None = field(repr=False)
    source_messages: tuple[MessageRecord, ...] = field(repr=False)
    retained_messages: tuple[MessageRecord, ...] = field(repr=False)
    trigger_message_count: int
    keep_recent_message_count: int
    max_source_chars: int
    model_input: str = field(repr=False)
    source_digest: str

    def __post_init__(self) -> None:
        validate_conversation_id(self.conversation_id)
        if self.previous_summary is not None:
            if not isinstance(self.previous_summary, SessionSummaryRecord):
                raise TypeError("SessionSummaryPlan.previous_summary 必须是 SessionSummaryRecord 或 None")
            if self.previous_summary.conversation_id != self.conversation_id:
                raise ValueError("SessionSummaryPlan.previous_summary 不属于当前会话")
        if not isinstance(self.source_messages, tuple) or not self.source_messages:
            raise ValueError("SessionSummaryPlan.source_messages 必须是非空 tuple")
        if not isinstance(self.retained_messages, tuple):
            raise ValueError("SessionSummaryPlan.retained_messages 必须是 tuple")
        _validate_policy_bounds(
            trigger_message_count=self.trigger_message_count,
            keep_recent_message_count=self.keep_recent_message_count,
            max_source_chars=self.max_source_chars,
        )
        candidates = self.source_messages + self.retained_messages
        if len(candidates) != self.trigger_message_count:
            raise ValueError("SessionSummaryPlan 必须绑定完整触发窗口")
        _validate_candidate_messages(
            self.conversation_id,
            previous_summary=self.previous_summary,
            messages=candidates,
        )
        if len(self.retained_messages) < self.keep_recent_message_count:
            raise ValueError("SessionSummaryPlan 不得少保留最近消息")
        if not isinstance(self.model_input, str) or not self.model_input:
            raise ValueError("SessionSummaryPlan.model_input 必须是非空字符串")
        if len(self.model_input) > self.max_source_chars:
            raise ValueError("SessionSummaryPlan.model_input 超过字符上限")
        expected_input = _render_model_input(
            conversation_id=self.conversation_id,
            previous_summary=self.previous_summary,
            source_messages=self.source_messages,
            trigger_message_count=self.trigger_message_count,
            keep_recent_message_count=self.keep_recent_message_count,
        )
        if self.model_input != expected_input:
            raise ValueError("SessionSummaryPlan.model_input 与源消息不匹配")
        expected_digest = hashlib.sha256(self.model_input.encode("utf-8")).hexdigest()
        if self.source_digest != expected_digest:
            raise ValueError("SessionSummaryPlan.source_digest 与 canonical 输入不匹配")

    @property
    def generation(self) -> int:
        return 1 if self.previous_summary is None else self.previous_summary.generation + 1

    @property
    def covered_from_message_id(self) -> int:
        message_id = self.source_messages[0].message_id
        return _require_positive_bigint(message_id, label="covered_from_message_id")

    @property
    def covered_through_message_id(self) -> int:
        message_id = self.source_messages[-1].message_id
        return _require_positive_bigint(message_id, label="covered_through_message_id")

    @property
    def covered_message_count(self) -> int:
        previous_count = 0 if self.previous_summary is None else self.previous_summary.covered_message_count
        return previous_count + len(self.source_messages)

    def complete(
        self,
        *,
        summary_id: str,
        model_provider: str,
        model: str,
        content: str,
        created_at: datetime,
    ) -> SessionSummaryRecord:
        completed_at = _normalize_datetime(created_at, label="created_at")
        earliest_completion = max(message.created_at for message in self.source_messages)
        if self.previous_summary is not None:
            earliest_completion = max(earliest_completion, self.previous_summary.created_at)
        if completed_at < earliest_completion:
            raise ValueError("摘要完成时间不得早于其源消息或前一摘要")
        return SessionSummaryRecord(
            summary_id=summary_id,
            conversation_id=self.conversation_id,
            generation=self.generation,
            previous_summary_id=(None if self.previous_summary is None else self.previous_summary.summary_id),
            covered_from_message_id=self.covered_from_message_id,
            covered_through_message_id=self.covered_through_message_id,
            covered_message_count=self.covered_message_count,
            source_message_count=len(self.source_messages),
            source_digest=self.source_digest,
            policy_version=SESSION_SUMMARY_POLICY_VERSION,
            trigger_message_count=self.trigger_message_count,
            keep_recent_message_count=self.keep_recent_message_count,
            max_source_chars=self.max_source_chars,
            source_char_count=len(self.model_input),
            model_provider=model_provider,
            model=model,
            content=content,
            created_at=completed_at,
        )


def _validate_candidate_messages(
    conversation_id: str,
    *,
    previous_summary: SessionSummaryRecord | None,
    messages: tuple[MessageRecord, ...],
) -> None:
    if any(not isinstance(message, MessageRecord) for message in messages):
        raise TypeError("候选消息必须全部是 MessageRecord")
    if any(not message.persisted for message in messages):
        raise ValueError("Session Summary 只接受已持久化消息")
    if any(message.conversation_id != conversation_id for message in messages):
        raise ValueError("Session Summary 候选消息不得跨会话")
    message_ids = tuple(_require_positive_bigint(message.message_id, label="MessageRecord.message_id") for message in messages)
    if any(older >= newer for older, newer in pairwise(message_ids)):
        raise ValueError("Session Summary 候选消息必须按 identity 严格递增")
    if previous_summary is not None and message_ids:
        if message_ids[0] <= previous_summary.covered_through_message_id:
            raise ValueError("Session Summary 候选消息必须位于前一覆盖水位之后")


def _validate_policy_bounds(
    *,
    trigger_message_count: object,
    keep_recent_message_count: object,
    max_source_chars: object,
) -> None:
    if (
        not isinstance(trigger_message_count, int)
        or isinstance(trigger_message_count, bool)
        or not 2 <= trigger_message_count <= MAX_SUMMARY_CANDIDATE_MESSAGES
    ):
        raise ValueError("trigger_message_count 必须是 2 到 200 的整数")
    if (
        not isinstance(keep_recent_message_count, int)
        or isinstance(keep_recent_message_count, bool)
        or not 1 <= keep_recent_message_count < trigger_message_count
    ):
        raise ValueError("keep_recent_message_count 必须小于触发消息数且至少为 1")
    if not isinstance(max_source_chars, int) or isinstance(max_source_chars, bool) or not 1_024 <= max_source_chars <= 1_000_000:
        raise ValueError("max_source_chars 必须是 1024 到 1000000 的整数")


@dataclass(frozen=True)
class SessionSummaryPolicy:
    """Deterministically compact an oldest-first committed message window."""

    trigger_message_count: int = DEFAULT_SUMMARY_TRIGGER_MESSAGES
    keep_recent_message_count: int = DEFAULT_SUMMARY_KEEP_RECENT_MESSAGES
    max_source_chars: int = DEFAULT_SUMMARY_MAX_SOURCE_CHARS

    def __post_init__(self) -> None:
        _validate_policy_bounds(
            trigger_message_count=self.trigger_message_count,
            keep_recent_message_count=self.keep_recent_message_count,
            max_source_chars=self.max_source_chars,
        )

    def plan(
        self,
        conversation_id: str,
        *,
        previous_summary: SessionSummaryRecord | None,
        messages: tuple[MessageRecord, ...],
    ) -> SessionSummaryPlan | None:
        conversation_id = validate_conversation_id(conversation_id)
        if previous_summary is not None:
            if not isinstance(previous_summary, SessionSummaryRecord):
                raise TypeError("previous_summary 必须是 SessionSummaryRecord 或 None")
            if previous_summary.conversation_id != conversation_id:
                raise ValueError("previous_summary 不属于当前会话")
        if not isinstance(messages, tuple):
            raise TypeError("messages 必须是 oldest-first tuple")
        if len(messages) > self.trigger_message_count:
            raise ValueError("messages 超过确定性触发窗口；Repository 查询必须使用 policy limit")
        _validate_candidate_messages(
            conversation_id,
            previous_summary=previous_summary,
            messages=messages,
        )
        if len(messages) < self.trigger_message_count:
            return None

        maximum_source_count = self.trigger_message_count - self.keep_recent_message_count
        selected_input: str | None = None
        selected_count = 0
        lower = 1
        upper = maximum_source_count
        while lower <= upper:
            source_count = (lower + upper) // 2
            source_messages = messages[:source_count]
            model_input = _render_model_input(
                conversation_id=conversation_id,
                previous_summary=previous_summary,
                source_messages=source_messages,
                trigger_message_count=self.trigger_message_count,
                keep_recent_message_count=self.keep_recent_message_count,
            )
            if len(model_input) <= self.max_source_chars:
                selected_input = model_input
                selected_count = source_count
                lower = source_count + 1
            else:
                upper = source_count - 1
        if selected_input is None:
            raise SessionSummarySourceTooLargeError("下一条完整消息无法放入 Session Summary 输入上限；覆盖水位未推进")

        source_messages = messages[:selected_count]
        retained_messages = messages[selected_count:]
        return SessionSummaryPlan(
            conversation_id=conversation_id,
            previous_summary=previous_summary,
            source_messages=source_messages,
            retained_messages=retained_messages,
            trigger_message_count=self.trigger_message_count,
            keep_recent_message_count=self.keep_recent_message_count,
            max_source_chars=self.max_source_chars,
            model_input=selected_input,
            source_digest=hashlib.sha256(selected_input.encode("utf-8")).hexdigest(),
        )

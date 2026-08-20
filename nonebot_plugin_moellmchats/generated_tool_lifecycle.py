from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from types import MappingProxyType
from typing import Any

try:  # pragma: no cover - exercised through the explicit platform guard
    import fcntl
except ImportError:  # pragma: no cover - fcntl is intentionally POSIX-only
    fcntl = None  # type: ignore[assignment]

from .private_files import ensure_private_directory

LIFECYCLE_SCHEMA_VERSION = 3
_LEGACY_LIFECYCLE_SCHEMA_VERSION = 2
LIFECYCLE_STATE_FILE = "lifecycle_state.json"
LIFECYCLE_LOCK_FILE = ".lifecycle.lock"

_MAX_STATE_BYTES = 16 * 1024 * 1024
_MAX_LEGACY_JSON_BYTES = 4 * 1024 * 1024
_MAX_MIGRATED_DRAFTS = 4096
_MAX_MIGRATED_VERSIONS = 8192
_COPY_BUFFER_BYTES = 128 * 1024
_BUNDLE_FILE_LIMIT = 65_536
_STATE_DIRECTORY_FSYNC_ATTEMPTS = 3
_STATE_DIRECTORY_FSYNC_RETRY_SECONDS = 0.01

_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LifecycleError(ValueError):
    """Base error for fail-closed generated-tool lifecycle storage."""


class LifecyclePlatformError(LifecycleError):
    """The host cannot provide the required POSIX locking guarantees."""


class LifecycleCorruptionError(LifecycleError):
    """Persisted lifecycle or legacy state is malformed or inconsistent."""


class LifecycleConflictError(LifecycleError):
    """A compare-and-swap precondition no longer matches persisted state."""


class LifecycleCommitUncertainError(LifecycleError):
    """The state file was replaced, but post-replace durability is uncertain."""

    def __init__(
        self,
        message: str,
        *,
        state_visible: bool,
        durability_confirmed: bool = False,
    ) -> None:
        super().__init__(message)
        if type(state_visible) is not bool or type(durability_confirmed) is not bool:
            raise TypeError(
                "state_visible 和 durability_confirmed 必须是 bool"
            )
        self.state_visible = state_visible
        self.durability_confirmed = durability_confirmed


class LifecycleTransitionError(LifecycleError):
    """A requested lifecycle transition is not allowed."""


class LifecycleLockTimeout(TimeoutError, LifecycleError):
    """The bounded lifecycle file-lock wait expired."""


class ImmutableVersionError(LifecycleError):
    """An immutable version cannot be verified or durably published."""


class DraftState(str, Enum):
    DRAFT = "draft"
    STATIC_VALIDATED = "static_validated"
    SANDBOX_TESTED = "sandbox_tested"
    MODEL_REVIEWED = "model_reviewed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVIEW_FAILED = "review_failed"
    VALIDATION_FAILED = "validation_failed"
    TEST_FAILED = "test_failed"


class VersionState(str, Enum):
    APPROVED = "approved"
    ACTIVATED = "activated"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


_EVIDENCE_SUCCESS_STATES = frozenset(
    {
        DraftState.STATIC_VALIDATED,
        DraftState.SANDBOX_TESTED,
        DraftState.MODEL_REVIEWED,
    }
)
_EVIDENCE_FAILURE_STATES = frozenset(
    {
        DraftState.VALIDATION_FAILED,
        DraftState.TEST_FAILED,
        DraftState.REVIEW_FAILED,
    }
)
_EVIDENCE_STATES = _EVIDENCE_SUCCESS_STATES | _EVIDENCE_FAILURE_STATES | {
    DraftState.REJECTED
}
_EVIDENCE_PRODUCER_LIMIT = 160
_EVIDENCE_SUMMARY_LIMIT = 4000
_EVIDENCE_RISK_LIMIT = 64
_EVIDENCE_RISK_TEXT_LIMIT = 500


def _require_identifier(value: str, pattern: re.Pattern[str], label: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LifecycleCorruptionError(f"{label} 非法: {value!r}")


def _require_timestamp(value: float | None, label: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0:
        raise LifecycleCorruptionError(f"{label} 必须是非负有限时间戳")


def _timestamp(value: float, label: str = "now") -> float:
    _require_timestamp(value, label)
    return float(value)


def _transition_timestamp(value: float, *previous: float | None) -> float:
    current = _timestamp(value)
    latest = max((item for item in previous if item is not None), default=0.0)
    if current < latest:
        raise LifecycleTransitionError(f"生命周期时间不能倒退: now={current}, latest={latest}")
    return current


@dataclass(frozen=True)
class DraftEvidence:
    """Canonical, bounded evidence for one draft lifecycle transition."""

    state: DraftState
    draft_digest: str
    producer: str
    outcome: str
    summary: str
    recorded_at: float
    risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.state not in _EVIDENCE_STATES:
            raise LifecycleCorruptionError(
                f"DraftEvidence 不支持状态: {self.state!r}"
            )
        _require_identifier(self.draft_digest, _DIGEST_RE, "evidence draft digest")
        if (
            not isinstance(self.producer, str)
            or not self.producer.strip()
            or self.producer != self.producer.strip()
            or len(self.producer) > _EVIDENCE_PRODUCER_LIMIT
        ):
            raise LifecycleCorruptionError(
                "evidence producer 必须为 1 到 160 个字符"
            )
        expected_outcome = (
            "passed"
            if self.state in _EVIDENCE_SUCCESS_STATES
            else "rejected"
            if self.state is DraftState.REJECTED
            else "failed"
        )
        if self.outcome != expected_outcome:
            raise LifecycleCorruptionError(
                f"{self.state.value} evidence outcome 必须为 {expected_outcome}"
            )
        if (
            not isinstance(self.summary, str)
            or not self.summary.strip()
            or self.summary != self.summary.strip()
            or len(self.summary) > _EVIDENCE_SUMMARY_LIMIT
        ):
            raise LifecycleCorruptionError(
                "evidence summary 必须为 1 到 4000 个字符"
            )
        _require_timestamp(self.recorded_at, "evidence recorded_at")
        if (
            not isinstance(self.risks, tuple)
            or len(self.risks) > _EVIDENCE_RISK_LIMIT
        ):
            raise LifecycleCorruptionError("evidence risks 必须是有界字符串元组")
        for risk in self.risks:
            if (
                not isinstance(risk, str)
                or not risk.strip()
                or risk != risk.strip()
                or len(risk) > _EVIDENCE_RISK_TEXT_LIMIT
            ):
                raise LifecycleCorruptionError(
                    "evidence risk 必须为 1 到 500 个字符"
                )
        object.__setattr__(self, "recorded_at", float(self.recorded_at))

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "draft_digest": self.draft_digest,
            "producer": self.producer,
            "outcome": self.outcome,
            "summary": self.summary,
            "recorded_at": self.recorded_at,
            "risks": list(self.risks),
        }


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    bundle_id: str
    digest: str
    state: DraftState
    created_at: float
    updated_at: float
    evidence: tuple[DraftEvidence, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.draft_id, _DRAFT_ID_RE, "draft_id")
        _require_identifier(self.bundle_id, _BUNDLE_ID_RE, "bundle_id")
        _require_identifier(self.digest, _DIGEST_RE, "draft digest")
        if not isinstance(self.state, DraftState):
            raise LifecycleCorruptionError("DraftRecord state 必须是 DraftState")
        _require_timestamp(self.created_at, "draft created_at")
        _require_timestamp(self.updated_at, "draft updated_at")
        if self.updated_at < self.created_at:
            raise LifecycleCorruptionError("draft updated_at 不能早于 created_at")
        if not isinstance(self.evidence, tuple):
            raise LifecycleCorruptionError("draft evidence 必须是不可变元组")
        evidence_by_state: dict[DraftState, DraftEvidence] = {}
        previous_evidence_at = self.created_at
        for item in self.evidence:
            if not isinstance(item, DraftEvidence):
                raise LifecycleCorruptionError(
                    "draft evidence 项必须是 DraftEvidence"
                )
            if item.draft_digest != self.digest:
                raise LifecycleCorruptionError("draft evidence 未绑定当前 digest")
            if item.state in evidence_by_state:
                raise LifecycleCorruptionError(
                    f"draft evidence 状态重复: {item.state.value}"
                )
            if not self.created_at <= item.recorded_at <= self.updated_at:
                raise LifecycleCorruptionError(
                    "draft evidence 时间必须位于草稿生命周期区间"
                )
            if item.recorded_at < previous_evidence_at:
                raise LifecycleCorruptionError(
                    "draft evidence 时间顺序不能倒退"
                )
            evidence_by_state[item.state] = item
            previous_evidence_at = item.recorded_at

        required: frozenset[DraftState]
        allowed: frozenset[DraftState]
        if self.state is DraftState.STATIC_VALIDATED:
            required = frozenset({DraftState.STATIC_VALIDATED})
            allowed = required
        elif self.state is DraftState.SANDBOX_TESTED:
            required = frozenset(
                {DraftState.STATIC_VALIDATED, DraftState.SANDBOX_TESTED}
            )
            allowed = required
        elif self.state in {
            DraftState.MODEL_REVIEWED,
            DraftState.AWAITING_APPROVAL,
            DraftState.APPROVED,
        }:
            required = _EVIDENCE_SUCCESS_STATES
            allowed = required
        elif self.state is DraftState.VALIDATION_FAILED:
            required = frozenset({DraftState.VALIDATION_FAILED})
            allowed = required
        elif self.state is DraftState.TEST_FAILED:
            required = frozenset(
                {DraftState.STATIC_VALIDATED, DraftState.TEST_FAILED}
            )
            allowed = required
        elif self.state is DraftState.REVIEW_FAILED:
            required = frozenset(
                {
                    DraftState.STATIC_VALIDATED,
                    DraftState.SANDBOX_TESTED,
                    DraftState.REVIEW_FAILED,
                }
            )
            allowed = required | {DraftState.MODEL_REVIEWED}
        elif self.state is DraftState.REJECTED:
            required = frozenset({DraftState.REJECTED})
            allowed = _EVIDENCE_STATES
        else:
            required = frozenset()
            allowed = frozenset()
        missing = required - evidence_by_state.keys()
        if missing:
            raise LifecycleCorruptionError(
                "draft state 缺少 canonical evidence: "
                + ", ".join(sorted(item.value for item in missing))
            )
        unexpected = evidence_by_state.keys() - allowed
        if unexpected:
            raise LifecycleCorruptionError(
                "draft state 包含不可能的 canonical evidence: "
                + ", ".join(sorted(item.value for item in unexpected))
            )
        if self.evidence:
            expected_last = (
                DraftState.MODEL_REVIEWED
                if self.state
                in {
                    DraftState.MODEL_REVIEWED,
                    DraftState.AWAITING_APPROVAL,
                    DraftState.APPROVED,
                }
                else self.state
            )
            if self.evidence[-1].state is not expected_last:
                raise LifecycleCorruptionError(
                    "draft evidence 最后一项与当前状态不一致"
                )
        object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "updated_at", float(self.updated_at))

    def as_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "bundle_id": self.bundle_id,
            "digest": self.digest,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "evidence": [item.as_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class VersionRecord:
    bundle_id: str
    digest: str
    state: VersionState
    source_draft_id: str | None
    created_at: float
    approved_at: float
    activated_at: float | None = None
    deprecated_at: float | None = None
    archived_at: float | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.bundle_id, _BUNDLE_ID_RE, "bundle_id")
        _require_identifier(self.digest, _DIGEST_RE, "version digest")
        if not isinstance(self.state, VersionState):
            raise LifecycleCorruptionError("VersionRecord state 必须是 VersionState")
        if self.source_draft_id is not None:
            _require_identifier(self.source_draft_id, _DRAFT_ID_RE, "source_draft_id")
        _require_timestamp(self.created_at, "version created_at")
        _require_timestamp(self.approved_at, "version approved_at")
        _require_timestamp(self.activated_at, "version activated_at", optional=True)
        _require_timestamp(self.deprecated_at, "version deprecated_at", optional=True)
        _require_timestamp(self.archived_at, "version archived_at", optional=True)
        if self.approved_at < self.created_at:
            raise LifecycleCorruptionError("version approved_at 不能早于 created_at")
        if self.activated_at is not None and self.activated_at < self.approved_at:
            raise LifecycleCorruptionError("version activated_at 不能早于 approved_at")
        if self.deprecated_at is not None and (self.activated_at is None or self.deprecated_at < self.activated_at):
            raise LifecycleCorruptionError("version deprecated_at 不能早于 activated_at")
        latest_before_archive = max(
            value
            for value in (
                self.approved_at,
                self.activated_at,
                self.deprecated_at,
            )
            if value is not None
        )
        if self.archived_at is not None and self.archived_at < latest_before_archive:
            raise LifecycleCorruptionError("version archived_at 不能早于先前生命周期时间")
        if self.state is VersionState.APPROVED:
            if any(value is not None for value in (self.activated_at, self.deprecated_at, self.archived_at)):
                raise LifecycleCorruptionError("Approved version 不得带激活、弃用或归档时间")
        elif self.state is VersionState.ACTIVATED:
            if self.activated_at is None or self.deprecated_at is not None or self.archived_at is not None:
                raise LifecycleCorruptionError("Activated version 时间字段不一致")
        elif self.state is VersionState.DEPRECATED:
            if self.activated_at is None or self.deprecated_at is None or self.archived_at is not None:
                raise LifecycleCorruptionError("Deprecated version 时间字段不一致")
        elif self.activated_at is None or self.deprecated_at is None or self.archived_at is None:
            raise LifecycleCorruptionError("Archived version 必须来自已激活且已弃用版本")
        object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "approved_at", float(self.approved_at))
        for field_name in ("activated_at", "deprecated_at", "archived_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, float(value))

    def as_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "digest": self.digest,
            "state": self.state.value,
            "source_draft_id": self.source_draft_id,
            "created_at": self.created_at,
            "approved_at": self.approved_at,
            "activated_at": self.activated_at,
            "deprecated_at": self.deprecated_at,
            "archived_at": self.archived_at,
        }


@dataclass(frozen=True)
class PermissionGrant:
    approved_by: str
    approved_at: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.approved_by, str)
            or not self.approved_by.strip()
            or self.approved_by != self.approved_by.strip()
            or len(self.approved_by) > 160
        ):
            raise LifecycleCorruptionError("permission approved_by 必须为 1 到 160 个字符")
        _require_timestamp(self.approved_at, "permission approved_at")
        object.__setattr__(self, "approved_at", float(self.approved_at))

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": True,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


def permission_key(bundle_id: str, digest: str, tool_name: str) -> str:
    _require_identifier(bundle_id, _BUNDLE_ID_RE, "permission bundle_id")
    _require_identifier(digest, _DIGEST_RE, "permission digest")
    _require_identifier(tool_name, _TOOL_NAME_RE, "permission tool_name")
    return f"{bundle_id}:{digest}:{tool_name}"


def _split_permission_key(key: str) -> tuple[str, str, str]:
    if not isinstance(key, str):
        raise LifecycleCorruptionError("permission grant key 必须是字符串")
    parts = key.split(":")
    if len(parts) != 3:
        raise LifecycleCorruptionError(f"permission grant key 非法: {key!r}")
    if permission_key(*parts) != key:
        raise LifecycleCorruptionError(f"permission grant key 非规范: {key!r}")
    return parts[0], parts[1], parts[2]


@dataclass(frozen=True)
class LifecycleState:
    revision: int
    drafts: Mapping[str, DraftRecord]
    versions: Mapping[str, Mapping[str, VersionRecord]]
    active: Mapping[str, str]
    permission_grants: Mapping[str, PermissionGrant]
    schema_version: int = LIFECYCLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIFECYCLE_SCHEMA_VERSION:
            raise LifecycleCorruptionError(f"不支持 lifecycle schema version: {self.schema_version!r}")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 0:
            raise LifecycleCorruptionError("lifecycle revision 必须是非负整数")

        if not all(
            isinstance(value, Mapping)
            for value in (
                self.drafts,
                self.versions,
                self.active,
                self.permission_grants,
            )
        ):
            raise LifecycleCorruptionError("LifecycleState collection 必须是映射")

        drafts: dict[str, DraftRecord] = {}
        for draft_id, record in self.drafts.items():
            if not isinstance(record, DraftRecord) or draft_id != record.draft_id:
                raise LifecycleCorruptionError("draft map key 与 DraftRecord 不一致")
            drafts[draft_id] = record

        versions: dict[str, Mapping[str, VersionRecord]] = {}
        for bundle_id, bundle_versions in self.versions.items():
            _require_identifier(bundle_id, _BUNDLE_ID_RE, "versions bundle_id")
            if not isinstance(bundle_versions, Mapping):
                raise LifecycleCorruptionError("versions bundle entry 必须是映射")
            inner: dict[str, VersionRecord] = {}
            for digest, record in bundle_versions.items():
                if not isinstance(record, VersionRecord) or record.bundle_id != bundle_id or record.digest != digest:
                    raise LifecycleCorruptionError("version map key 与 VersionRecord 不一致")
                inner[digest] = record
            versions[bundle_id] = MappingProxyType(inner)

        active: dict[str, str] = {}
        for bundle_id, digest in self.active.items():
            _require_identifier(bundle_id, _BUNDLE_ID_RE, "active bundle_id")
            _require_identifier(digest, _DIGEST_RE, "active digest")
            record = versions.get(bundle_id, {}).get(digest)
            if record is None or record.state is not VersionState.ACTIVATED:
                raise LifecycleCorruptionError(f"active {bundle_id}@{digest} 必须指向 Activated VersionRecord")
            active[bundle_id] = digest

        for bundle_id, bundle_versions in versions.items():
            activated = [digest for digest, record in bundle_versions.items() if record.state is VersionState.ACTIVATED]
            expected = active.get(bundle_id)
            if (not activated and expected is not None) or (activated and (len(activated) != 1 or activated[0] != expected)):
                raise LifecycleCorruptionError(f"bundle {bundle_id} 的 active 与唯一 Activated 状态不一致")

        grants: dict[str, PermissionGrant] = {}
        for key, grant in self.permission_grants.items():
            bundle_id, digest, _ = _split_permission_key(key)
            if not isinstance(grant, PermissionGrant):
                raise LifecycleCorruptionError("permission grant 必须是 PermissionGrant")
            version = versions.get(bundle_id, {}).get(digest)
            if version is None or version.state is VersionState.ARCHIVED:
                raise LifecycleCorruptionError(f"permission grant 指向不存在或已归档版本: {key}")
            grants[key] = grant

        for record in drafts.values():
            if record.state is DraftState.APPROVED and record.digest not in versions.get(record.bundle_id, {}):
                raise LifecycleCorruptionError(f"Approved draft 未关联持久版本: {record.draft_id}")

        object.__setattr__(self, "drafts", MappingProxyType(drafts))
        object.__setattr__(self, "versions", MappingProxyType(versions))
        object.__setattr__(self, "active", MappingProxyType(active))
        object.__setattr__(self, "permission_grants", MappingProxyType(grants))

    @classmethod
    def empty(cls) -> LifecycleState:
        return cls(revision=0, drafts={}, versions={}, active={}, permission_grants={})

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "drafts": {draft_id: record.as_dict() for draft_id, record in self.drafts.items()},
            "versions": {
                bundle_id: {digest: record.as_dict() for digest, record in bundle_versions.items()}
                for bundle_id, bundle_versions in self.versions.items()
            },
            "active": dict(self.active),
            "permission_grants": {key: grant.as_dict() for key, grant in self.permission_grants.items()},
        }

    @property
    def state_digest(self) -> str:
        return hashlib.sha256(_canonical_json_bytes(self.as_dict())).hexdigest()


@dataclass(frozen=True)
class ImmutableVersionPublish:
    source_directory: Path
    bundle_id: str
    digest: str

    def __post_init__(self) -> None:
        _require_identifier(self.bundle_id, _BUNDLE_ID_RE, "publish bundle_id")
        _require_identifier(self.digest, _DIGEST_RE, "publish digest")
        object.__setattr__(self, "source_directory", Path(self.source_directory))


@dataclass(frozen=True)
class LifecyclePlan:
    operation_id: str
    operation: str
    expected_revision: int
    before_digest: str
    after_digest: str
    after_state: LifecycleState
    no_op: bool

    def __post_init__(self) -> None:
        _require_identifier(self.operation_id, _DIGEST_RE, "operation_id")
        _require_identifier(self.operation, _OPERATION_RE, "operation")
        _require_identifier(self.before_digest, _DIGEST_RE, "before_digest")
        _require_identifier(self.after_digest, _DIGEST_RE, "after_digest")
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 0:
            raise LifecycleError("expected_revision 必须是非负整数")
        if not isinstance(self.after_state, LifecycleState):
            raise LifecycleError("after_state 必须是 LifecycleState")
        if self.after_digest != self.after_state.state_digest:
            raise LifecycleError("LifecyclePlan after_digest 与 after_state 不一致")
        if self.no_op:
            if self.after_state.revision != self.expected_revision or self.before_digest != self.after_digest:
                raise LifecycleError("no-op plan 不得改变 revision 或 state digest")
        elif self.after_state.revision != self.expected_revision + 1 or self.before_digest == self.after_digest:
            raise LifecycleError("变更 plan 必须将 revision 精确增加 1")


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LifecycleCorruptionError("lifecycle state 必须是规范 JSON") from error


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleCorruptionError(f"JSON 包含重复字段: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise LifecycleCorruptionError(f"JSON 包含非有限数字: {value}")


def _decode_json_bytes(content: bytes, *, label: str) -> Any:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LifecycleCorruptionError(f"{label} 必须是 UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except LifecycleCorruptionError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise LifecycleCorruptionError(f"{label} JSON 损坏: {error}") from error


def _require_exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LifecycleCorruptionError(f"{label} 必须是对象")
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise LifecycleCorruptionError(f"{label} 字段不匹配 (unknown={unknown}, missing={missing})")
    return value


def _evidence_from_dict(value: Any) -> DraftEvidence:
    item = _require_exact_keys(
        value,
        frozenset(
            {
                "state",
                "draft_digest",
                "producer",
                "outcome",
                "summary",
                "recorded_at",
                "risks",
            }
        ),
        "DraftEvidence",
    )
    try:
        state = DraftState(item["state"])
    except (TypeError, ValueError) as error:
        raise LifecycleCorruptionError(
            f"未知 DraftEvidence state: {item.get('state')!r}"
        ) from error
    risks = item["risks"]
    if not isinstance(risks, list):
        raise LifecycleCorruptionError("DraftEvidence risks 必须是数组")
    return DraftEvidence(
        state=state,
        draft_digest=item["draft_digest"],
        producer=item["producer"],
        outcome=item["outcome"],
        summary=item["summary"],
        recorded_at=item["recorded_at"],
        risks=tuple(risks),
    )


def _legacy_evidence(
    *,
    state: DraftState,
    digest: str,
    recorded_at: float,
) -> tuple[DraftEvidence, ...]:
    """Make schema-v2 provenance explicit instead of pretending it was verified."""

    legacy_summary = "schema v2 迁移记录；原状态没有 canonical evidence"

    def evidence(target: DraftState) -> DraftEvidence:
        return DraftEvidence(
            state=target,
            draft_digest=digest,
            producer="schema-v2-migration",
            outcome=(
                "passed"
                if target in _EVIDENCE_SUCCESS_STATES
                else "rejected"
                if target is DraftState.REJECTED
                else "failed"
            ),
            summary=legacy_summary,
            recorded_at=recorded_at,
            risks=("legacy_unverified",)
            if target in {DraftState.MODEL_REVIEWED, DraftState.REVIEW_FAILED}
            else (),
        )

    if state is DraftState.DRAFT:
        targets: tuple[DraftState, ...] = ()
    elif state is DraftState.STATIC_VALIDATED:
        targets = (DraftState.STATIC_VALIDATED,)
    elif state is DraftState.SANDBOX_TESTED:
        targets = (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
        )
    elif state in {
        DraftState.MODEL_REVIEWED,
        DraftState.AWAITING_APPROVAL,
        DraftState.APPROVED,
    }:
        targets = (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
            DraftState.MODEL_REVIEWED,
        )
    elif state is DraftState.VALIDATION_FAILED:
        targets = (DraftState.VALIDATION_FAILED,)
    elif state is DraftState.TEST_FAILED:
        targets = (DraftState.STATIC_VALIDATED, DraftState.TEST_FAILED)
    elif state is DraftState.REVIEW_FAILED:
        targets = (
            DraftState.STATIC_VALIDATED,
            DraftState.SANDBOX_TESTED,
            DraftState.REVIEW_FAILED,
        )
    else:
        targets = (DraftState.REJECTED,)
    return tuple(evidence(target) for target in targets)


def _draft_from_dict(value: Any, *, schema_version: int) -> DraftRecord:
    expected = {
        "draft_id",
        "bundle_id",
        "digest",
        "state",
        "created_at",
        "updated_at",
    }
    if schema_version == LIFECYCLE_SCHEMA_VERSION:
        expected.add("evidence")
    item = _require_exact_keys(value, frozenset(expected), "DraftRecord")
    try:
        state = DraftState(item["state"])
    except (TypeError, ValueError) as error:
        raise LifecycleCorruptionError(f"未知 DraftState: {item.get('state')!r}") from error
    evidence = (
        tuple(_evidence_from_dict(entry) for entry in item["evidence"])
        if schema_version == LIFECYCLE_SCHEMA_VERSION
        and isinstance(item["evidence"], list)
        else None
    )
    if schema_version == LIFECYCLE_SCHEMA_VERSION and evidence is None:
        raise LifecycleCorruptionError("DraftRecord evidence 必须是数组")
    if evidence is None:
        evidence = _legacy_evidence(
            state=state,
            digest=item["digest"],
            recorded_at=float(item["updated_at"]),
        )
    return DraftRecord(
        draft_id=item["draft_id"],
        bundle_id=item["bundle_id"],
        digest=item["digest"],
        state=state,
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        evidence=evidence,
    )


def _version_from_dict(value: Any) -> VersionRecord:
    item = _require_exact_keys(
        value,
        frozenset(
            {
                "bundle_id",
                "digest",
                "state",
                "source_draft_id",
                "created_at",
                "approved_at",
                "activated_at",
                "deprecated_at",
                "archived_at",
            }
        ),
        "VersionRecord",
    )
    try:
        state = VersionState(item["state"])
    except (TypeError, ValueError) as error:
        raise LifecycleCorruptionError(f"未知 VersionState: {item.get('state')!r}") from error
    return VersionRecord(
        bundle_id=item["bundle_id"],
        digest=item["digest"],
        state=state,
        source_draft_id=item["source_draft_id"],
        created_at=item["created_at"],
        approved_at=item["approved_at"],
        activated_at=item["activated_at"],
        deprecated_at=item["deprecated_at"],
        archived_at=item["archived_at"],
    )


def _grant_from_dict(value: Any) -> PermissionGrant:
    item = _require_exact_keys(
        value,
        frozenset({"approved", "approved_by", "approved_at"}),
        "PermissionGrant",
    )
    if item["approved"] is not True:
        raise LifecycleCorruptionError("PermissionGrant approved 必须为 true")
    return PermissionGrant(
        approved_by=item["approved_by"],
        approved_at=item["approved_at"],
    )


def decode_lifecycle_state(content: bytes) -> LifecycleState:
    raw = _require_exact_keys(
        _decode_json_bytes(content, label=LIFECYCLE_STATE_FILE),
        frozenset(
            {
                "schema_version",
                "revision",
                "drafts",
                "versions",
                "active",
                "permission_grants",
            }
        ),
        "LifecycleState",
    )
    if raw["schema_version"] not in {
        _LEGACY_LIFECYCLE_SCHEMA_VERSION,
        LIFECYCLE_SCHEMA_VERSION,
    }:
        raise LifecycleCorruptionError(f"未知 lifecycle schema version: {raw['schema_version']!r}")
    if not isinstance(raw["drafts"], dict):
        raise LifecycleCorruptionError("LifecycleState drafts 必须是对象")
    if not isinstance(raw["versions"], dict):
        raise LifecycleCorruptionError("LifecycleState versions 必须是对象")
    if not isinstance(raw["active"], dict):
        raise LifecycleCorruptionError("LifecycleState active 必须是对象")
    if not isinstance(raw["permission_grants"], dict):
        raise LifecycleCorruptionError("LifecycleState permission_grants 必须是对象")

    drafts = {
        draft_id: _draft_from_dict(
            value,
            schema_version=raw["schema_version"],
        )
        for draft_id, value in raw["drafts"].items()
    }
    versions: dict[str, dict[str, VersionRecord]] = {}
    for bundle_id, bundle_versions in raw["versions"].items():
        if not isinstance(bundle_versions, dict):
            raise LifecycleCorruptionError("LifecycleState bundle versions 必须是对象")
        versions[bundle_id] = {digest: _version_from_dict(value) for digest, value in bundle_versions.items()}
    grants = {key: _grant_from_dict(value) for key, value in raw["permission_grants"].items()}
    return LifecycleState(
        schema_version=LIFECYCLE_SCHEMA_VERSION,
        revision=raw["revision"],
        drafts=drafts,
        versions=versions,
        active=raw["active"],
        permission_grants=grants,
    )


def encode_lifecycle_state(state: LifecycleState) -> bytes:
    if not isinstance(state, LifecycleState):
        raise TypeError("state 必须是 LifecycleState")
    return _canonical_json_bytes(state.as_dict()) + b"\n"


def _replace_state(
    state: LifecycleState,
    *,
    revision: int | None = None,
    drafts: Mapping[str, DraftRecord] | None = None,
    versions: Mapping[str, Mapping[str, VersionRecord]] | None = None,
    active: Mapping[str, str] | None = None,
    permission_grants: Mapping[str, PermissionGrant] | None = None,
) -> LifecycleState:
    return LifecycleState(
        revision=state.revision if revision is None else revision,
        drafts=state.drafts if drafts is None else drafts,
        versions=state.versions if versions is None else versions,
        active=state.active if active is None else active,
        permission_grants=(state.permission_grants if permission_grants is None else permission_grants),
    )


def _make_plan(
    before: LifecycleState,
    operation: str,
    payload: Mapping[str, Any],
    candidate: LifecycleState,
) -> LifecyclePlan:
    if candidate.revision != before.revision:
        raise LifecycleError("plan candidate 必须保留原 revision")
    before_digest = before.state_digest
    no_op = candidate.state_digest == before_digest
    after = candidate if no_op else _replace_state(candidate, revision=before.revision + 1)
    after_digest = after.state_digest
    operation_id = hashlib.sha256(
        _canonical_json_bytes(
            {
                "operation": operation,
                "expected_revision": before.revision,
                "before_digest": before_digest,
                "after_digest": after_digest,
                "payload": dict(payload),
            }
        )
    ).hexdigest()
    return LifecyclePlan(
        operation_id=operation_id,
        operation=operation,
        expected_revision=before.revision,
        before_digest=before_digest,
        after_digest=after_digest,
        after_state=after,
        no_op=no_op,
    )


def plan_record_draft(state: LifecycleState, record: DraftRecord) -> LifecyclePlan:
    if record.state is not DraftState.DRAFT:
        raise LifecycleTransitionError("运行期只能登记 Draft；后续状态必须通过显式 transition plan 推进")
    drafts = dict(state.drafts)
    existing = drafts.get(record.draft_id)
    if existing is not None and existing != record:
        raise LifecycleConflictError(f"draft_id 已存在且内容不同: {record.draft_id}")
    drafts[record.draft_id] = record
    candidate = _replace_state(state, drafts=drafts)
    return _make_plan(state, "record_draft", {"draft_id": record.draft_id}, candidate)


_DRAFT_TRANSITIONS: Mapping[DraftState, frozenset[DraftState]] = MappingProxyType(
    {
        DraftState.DRAFT: frozenset({DraftState.STATIC_VALIDATED, DraftState.VALIDATION_FAILED, DraftState.REJECTED}),
        DraftState.STATIC_VALIDATED: frozenset(
            {
                DraftState.SANDBOX_TESTED,
                DraftState.TEST_FAILED,
                DraftState.REJECTED,
            }
        ),
        DraftState.SANDBOX_TESTED: frozenset(
            {
                DraftState.MODEL_REVIEWED,
                DraftState.REVIEW_FAILED,
                DraftState.REJECTED,
            }
        ),
        DraftState.MODEL_REVIEWED: frozenset({DraftState.AWAITING_APPROVAL, DraftState.REVIEW_FAILED, DraftState.REJECTED}),
        DraftState.AWAITING_APPROVAL: frozenset({DraftState.REJECTED}),
    }
)


def plan_transition_draft(
    state: LifecycleState,
    draft_id: str,
    target: DraftState,
    *,
    now: float,
    evidence: DraftEvidence | None = None,
) -> LifecyclePlan:
    _require_identifier(draft_id, _DRAFT_ID_RE, "draft_id")
    if not isinstance(target, DraftState):
        raise LifecycleTransitionError("target 必须是 DraftState")
    record = state.drafts.get(draft_id)
    if record is None:
        raise LifecycleTransitionError(f"草稿不存在: {draft_id}")
    if record.state is target:
        if target in _EVIDENCE_STATES and not any(
            item.state is target for item in record.evidence
        ):
            raise LifecycleTransitionError(
                f"{target.value} 缺少 canonical evidence"
            )
        return _make_plan(state, "transition_draft", {"draft_id": draft_id, "target": target.value}, state)
    if target is DraftState.APPROVED or target not in _DRAFT_TRANSITIONS.get(record.state, frozenset()):
        raise LifecycleTransitionError(f"草稿状态不能从 {record.state.value} 转为 {target.value}")
    if target in _EVIDENCE_STATES:
        if not isinstance(evidence, DraftEvidence):
            raise LifecycleTransitionError(
                f"推进到 {target.value} 必须提供结构化 canonical evidence"
            )
        if evidence.state is not target or evidence.draft_digest != record.digest:
            raise LifecycleTransitionError("transition evidence 与目标状态或草稿 digest 不匹配")
    elif evidence is not None:
        raise LifecycleTransitionError(
            f"推进到 {target.value} 不接受额外 evidence"
        )
    timestamp = _transition_timestamp(now, record.updated_at)
    if evidence is not None and evidence.recorded_at != timestamp:
        raise LifecycleTransitionError(
            "transition evidence recorded_at 必须与状态推进时间一致"
        )
    drafts = dict(state.drafts)
    drafts[draft_id] = replace(
        record,
        state=target,
        updated_at=timestamp,
        evidence=(
            (*record.evidence, evidence)
            if evidence is not None
            else record.evidence
        ),
    )
    candidate = _replace_state(state, drafts=drafts)
    return _make_plan(
        state,
        "transition_draft",
        {"draft_id": draft_id, "target": target.value},
        candidate,
    )


def plan_reject(
    state: LifecycleState,
    draft_id: str,
    *,
    now: float,
    evidence: DraftEvidence,
) -> LifecyclePlan:
    _require_identifier(draft_id, _DRAFT_ID_RE, "draft_id")
    record = state.drafts.get(draft_id)
    if record is None:
        raise LifecycleTransitionError(f"草稿不存在: {draft_id}")
    if record.state is DraftState.APPROVED:
        raise LifecycleTransitionError("已批准草稿不能再拒绝")
    if record.state is DraftState.REJECTED:
        if not any(
            item.state is DraftState.REJECTED for item in record.evidence
        ):
            raise LifecycleTransitionError("Rejected 草稿缺少 canonical evidence")
        return _make_plan(state, "reject_draft", {"draft_id": draft_id}, state)
    if (
        not isinstance(evidence, DraftEvidence)
        or evidence.state is not DraftState.REJECTED
        or evidence.draft_digest != record.digest
    ):
        raise LifecycleTransitionError(
            "拒绝草稿必须提供匹配 digest 的 Rejected evidence"
        )
    timestamp = _transition_timestamp(now, record.updated_at)
    if evidence.recorded_at != timestamp:
        raise LifecycleTransitionError(
            "rejection evidence recorded_at 必须与状态推进时间一致"
        )
    drafts = dict(state.drafts)
    drafts[draft_id] = replace(
        record,
        state=DraftState.REJECTED,
        updated_at=timestamp,
        evidence=(*record.evidence, evidence),
    )
    return _make_plan(
        state,
        "reject_draft",
        {"draft_id": draft_id},
        _replace_state(state, drafts=drafts),
    )


def _mutable_versions(state: LifecycleState) -> dict[str, dict[str, VersionRecord]]:
    return {bundle_id: dict(bundle_versions) for bundle_id, bundle_versions in state.versions.items()}


def plan_approve_draft(
    state: LifecycleState,
    draft_id: str,
    *,
    now: float,
    expected_digest: str | None = None,
) -> LifecyclePlan:
    """Approve a reviewed draft without making the version active."""

    _require_identifier(draft_id, _DRAFT_ID_RE, "draft_id")
    record = state.drafts.get(draft_id)
    if record is None:
        raise LifecycleTransitionError(f"草稿不存在: {draft_id}")
    if expected_digest is not None and expected_digest != record.digest:
        raise LifecycleConflictError("草稿 digest 与批准前置条件不一致")
    existing = state.versions.get(record.bundle_id, {}).get(record.digest)
    if record.state is DraftState.APPROVED and existing is not None:
        return _make_plan(
            state,
            "approve_draft",
            {"draft_id": draft_id, "digest": record.digest},
            state,
        )
    if record.state is not DraftState.AWAITING_APPROVAL:
        raise LifecycleTransitionError(f"草稿当前状态不可批准: {record.state.value}")
    if existing is not None and existing.state is VersionState.ARCHIVED:
        raise LifecycleTransitionError("已归档版本不能由草稿重新批准")

    timestamp = _transition_timestamp(now, record.updated_at)
    versions = _mutable_versions(state)
    bundle_versions = versions.setdefault(record.bundle_id, {})
    if existing is None:
        bundle_versions[record.digest] = VersionRecord(
            bundle_id=record.bundle_id,
            digest=record.digest,
            state=VersionState.APPROVED,
            source_draft_id=record.draft_id,
            created_at=timestamp,
            approved_at=timestamp,
        )
    drafts = dict(state.drafts)
    drafts[draft_id] = replace(
        record,
        state=DraftState.APPROVED,
        updated_at=timestamp,
    )
    return _make_plan(
        state,
        "approve_draft",
        {"draft_id": draft_id, "digest": record.digest},
        _replace_state(state, drafts=drafts, versions=versions),
    )


def plan_activate_version(
    state: LifecycleState,
    bundle_id: str,
    digest: str,
    *,
    now: float,
) -> LifecyclePlan:
    """Activate one Approved/Deprecated version and deprecate its predecessor."""

    _require_identifier(bundle_id, _BUNDLE_ID_RE, "bundle_id")
    _require_identifier(digest, _DIGEST_RE, "activation digest")
    target = state.versions.get(bundle_id, {}).get(digest)
    if target is None:
        raise LifecycleTransitionError("激活目标版本不存在")
    if target.state is VersionState.ARCHIVED:
        raise LifecycleTransitionError("已归档版本不能激活")
    if state.active.get(bundle_id) == digest:
        return _make_plan(
            state,
            "activate_version",
            {"bundle_id": bundle_id, "digest": digest},
            state,
        )
    if target.state not in {VersionState.APPROVED, VersionState.DEPRECATED}:
        raise LifecycleTransitionError(f"版本当前状态不可激活: {target.state.value}")

    timestamp = _transition_timestamp(
        now,
        target.approved_at,
        target.activated_at,
        target.deprecated_at,
    )
    versions = _mutable_versions(state)
    active = dict(state.active)
    previous_digest = active.get(bundle_id)
    if previous_digest is not None:
        previous = versions[bundle_id][previous_digest]
        previous_time = _transition_timestamp(
            timestamp,
            previous.activated_at,
            previous.deprecated_at,
        )
        versions[bundle_id][previous_digest] = replace(
            previous,
            state=VersionState.DEPRECATED,
            deprecated_at=previous_time,
            archived_at=None,
        )
    versions[bundle_id][digest] = replace(
        target,
        state=VersionState.ACTIVATED,
        activated_at=timestamp,
        deprecated_at=None,
        archived_at=None,
    )
    active[bundle_id] = digest
    return _make_plan(
        state,
        "activate_version",
        {"bundle_id": bundle_id, "digest": digest},
        _replace_state(state, versions=versions, active=active),
    )


def plan_activate_from_draft(
    state: LifecycleState,
    draft_id: str,
    *,
    now: float,
    expected_digest: str | None = None,
) -> LifecyclePlan:
    """Atomically approve and activate one AwaitingApproval draft.

    This command-oriented compound transition intentionally uses one revision.
    Callers needing the intermediate Approved version use plan_approve_draft
    followed by plan_activate_version.
    """

    _require_identifier(draft_id, _DRAFT_ID_RE, "draft_id")
    record = state.drafts.get(draft_id)
    if record is None:
        raise LifecycleTransitionError(f"草稿不存在: {draft_id}")
    if expected_digest is not None and expected_digest != record.digest:
        raise LifecycleConflictError("草稿 digest 与批准前置条件不一致")
    existing = state.versions.get(record.bundle_id, {}).get(record.digest)
    if record.state is DraftState.APPROVED:
        if (
            state.active.get(record.bundle_id) == record.digest
            and existing is not None
            and existing.state is VersionState.ACTIVATED
        ):
            return _make_plan(
                state,
                "activate_from_draft",
                {"draft_id": draft_id, "digest": record.digest},
                state,
            )
        raise LifecycleTransitionError("Approved draft 仅允许对当前活动版本做幂等重试；请使用 activate/rollback plan")
    if record.state is not DraftState.AWAITING_APPROVAL:
        raise LifecycleTransitionError(f"草稿当前状态不可批准: {record.state.value}")

    timestamp = _transition_timestamp(now, record.updated_at)
    versions = _mutable_versions(state)
    bundle_versions = versions.setdefault(record.bundle_id, {})
    target = bundle_versions.get(record.digest)
    if target is not None and target.state is VersionState.ARCHIVED:
        raise LifecycleTransitionError("已归档版本不能重新激活")

    active = dict(state.active)
    previous_digest = active.get(record.bundle_id)
    if previous_digest is not None and previous_digest != record.digest:
        previous = bundle_versions[previous_digest]
        _transition_timestamp(timestamp, previous.activated_at, previous.deprecated_at)
        bundle_versions[previous_digest] = replace(
            previous,
            state=VersionState.DEPRECATED,
            deprecated_at=timestamp,
            archived_at=None,
        )

    if target is None:
        target = VersionRecord(
            bundle_id=record.bundle_id,
            digest=record.digest,
            state=VersionState.ACTIVATED,
            source_draft_id=record.draft_id,
            created_at=timestamp,
            approved_at=timestamp,
            activated_at=timestamp,
        )
    elif target.state is not VersionState.ACTIVATED:
        _transition_timestamp(
            timestamp,
            target.approved_at,
            target.activated_at,
            target.deprecated_at,
        )
        target = replace(
            target,
            state=VersionState.ACTIVATED,
            activated_at=timestamp,
            deprecated_at=None,
            archived_at=None,
            source_draft_id=target.source_draft_id or record.draft_id,
        )
    bundle_versions[record.digest] = target
    active[record.bundle_id] = record.digest

    drafts = dict(state.drafts)
    drafts[draft_id] = replace(record, state=DraftState.APPROVED, updated_at=timestamp)
    candidate = _replace_state(state, drafts=drafts, versions=versions, active=active)
    return _make_plan(
        state,
        "activate_from_draft",
        {"draft_id": draft_id, "digest": record.digest},
        candidate,
    )


def plan_deactivate(state: LifecycleState, bundle_id: str, *, now: float) -> LifecyclePlan:
    _require_identifier(bundle_id, _BUNDLE_ID_RE, "bundle_id")
    active = dict(state.active)
    digest = active.get(bundle_id)
    if digest is None:
        return _make_plan(state, "deactivate", {"bundle_id": bundle_id}, state)
    versions = _mutable_versions(state)
    current = versions[bundle_id][digest]
    timestamp = _transition_timestamp(
        now,
        current.activated_at,
        current.deprecated_at,
    )
    versions[bundle_id][digest] = replace(
        current,
        state=VersionState.DEPRECATED,
        deprecated_at=timestamp,
        archived_at=None,
    )
    del active[bundle_id]
    return _make_plan(
        state,
        "deactivate",
        {"bundle_id": bundle_id, "digest": digest},
        _replace_state(state, versions=versions, active=active),
    )


def plan_rollback(
    state: LifecycleState,
    bundle_id: str,
    digest: str,
    *,
    now: float,
) -> LifecyclePlan:
    _require_identifier(bundle_id, _BUNDLE_ID_RE, "bundle_id")
    _require_identifier(digest, _DIGEST_RE, "rollback digest")
    target = state.versions.get(bundle_id, {}).get(digest)
    if target is None:
        raise LifecycleTransitionError("回滚目标版本不存在")
    if target.state is VersionState.ARCHIVED:
        raise LifecycleTransitionError("已归档版本不能回滚")
    if state.active.get(bundle_id) == digest:
        return _make_plan(state, "rollback", {"bundle_id": bundle_id, "digest": digest}, state)

    timestamp = _transition_timestamp(
        now,
        target.approved_at,
        target.activated_at,
        target.deprecated_at,
    )
    versions = _mutable_versions(state)
    active = dict(state.active)
    previous_digest = active.get(bundle_id)
    if previous_digest is not None:
        previous = versions[bundle_id][previous_digest]
        _transition_timestamp(timestamp, previous.activated_at, previous.deprecated_at)
        versions[bundle_id][previous_digest] = replace(
            previous,
            state=VersionState.DEPRECATED,
            deprecated_at=timestamp,
            archived_at=None,
        )
    versions[bundle_id][digest] = replace(
        target,
        state=VersionState.ACTIVATED,
        activated_at=timestamp,
        deprecated_at=None,
        archived_at=None,
    )
    active[bundle_id] = digest
    return _make_plan(
        state,
        "rollback",
        {"bundle_id": bundle_id, "digest": digest},
        _replace_state(state, versions=versions, active=active),
    )


def plan_permission(
    state: LifecycleState,
    bundle_id: str,
    digest: str,
    tool_name: str,
    *,
    allow_user: bool,
    approved_by: str | None,
    now: float,
) -> LifecyclePlan:
    if type(allow_user) is not bool:
        raise LifecycleTransitionError("allow_user 必须是 bool")
    key = permission_key(bundle_id, digest, tool_name)
    version = state.versions.get(bundle_id, {}).get(digest)
    if version is None or version.state is VersionState.ARCHIVED:
        raise LifecycleTransitionError("权限策略目标版本不存在或已归档")
    grants = dict(state.permission_grants)
    if allow_user:
        if approved_by is None:
            raise LifecycleTransitionError("放宽 user 权限必须记录批准人")
        proposed = PermissionGrant(approved_by=approved_by, approved_at=_timestamp(now))
        existing = grants.get(key)
        if existing is not None and existing.approved_by == proposed.approved_by:
            return _make_plan(state, "permission", {"key": key, "allow_user": True}, state)
        grants[key] = proposed
    else:
        if key not in grants:
            return _make_plan(state, "permission", {"key": key, "allow_user": False}, state)
        del grants[key]
    return _make_plan(
        state,
        "permission",
        {"key": key, "allow_user": allow_user},
        _replace_state(state, permission_grants=grants),
    )


def plan_archive(
    state: LifecycleState,
    bundle_id: str,
    digest: str,
    *,
    now: float,
) -> LifecyclePlan:
    _require_identifier(bundle_id, _BUNDLE_ID_RE, "bundle_id")
    _require_identifier(digest, _DIGEST_RE, "archive digest")
    version = state.versions.get(bundle_id, {}).get(digest)
    if version is None:
        raise LifecycleTransitionError("归档目标版本不存在")
    if state.active.get(bundle_id) == digest:
        raise LifecycleTransitionError("必须先停用活动版本再归档")
    if version.state is VersionState.ARCHIVED:
        return _make_plan(state, "archive", {"bundle_id": bundle_id, "digest": digest}, state)
    if version.state is not VersionState.DEPRECATED:
        raise LifecycleTransitionError(f"只有 Deprecated version 可以归档: {version.state.value}")
    timestamp = _transition_timestamp(
        now,
        version.approved_at,
        version.activated_at,
        version.deprecated_at,
    )
    versions = _mutable_versions(state)
    versions[bundle_id][digest] = replace(
        version,
        state=VersionState.ARCHIVED,
        archived_at=timestamp,
    )
    prefix = f"{bundle_id}:{digest}:"
    grants = {key: grant for key, grant in state.permission_grants.items() if not key.startswith(prefix)}
    return _make_plan(
        state,
        "archive",
        {"bundle_id": bundle_id, "digest": digest},
        _replace_state(state, versions=versions, permission_grants=grants),
    )


def plan_restore_snapshot(
    state: LifecycleState,
    snapshot: LifecycleState,
    *,
    failed_plan: LifecyclePlan,
) -> LifecyclePlan:
    """Compensate a failed post-commit activation without rewinding revision."""

    if not isinstance(failed_plan, LifecyclePlan) or failed_plan.no_op:
        raise LifecycleTransitionError("补偿必须绑定一个已变更 LifecyclePlan")
    if state.revision != failed_plan.after_state.revision or state.state_digest != failed_plan.after_digest:
        raise LifecycleConflictError("失败 plan 提交后 lifecycle 已有其他变更，拒绝覆盖式补偿")
    if snapshot.revision > state.revision:
        raise LifecycleTransitionError("不能用未来 revision 作为补偿快照")
    if snapshot.revision != failed_plan.expected_revision or snapshot.state_digest != failed_plan.before_digest:
        raise LifecycleConflictError("补偿快照不是失败 plan 的精确 before state")
    candidate = LifecycleState(
        revision=state.revision,
        drafts=snapshot.drafts,
        versions=snapshot.versions,
        active=snapshot.active,
        permission_grants=snapshot.permission_grants,
    )
    return _make_plan(
        state,
        "restore_snapshot",
        {
            "failed_operation_id": failed_plan.operation_id,
            "snapshot_digest": snapshot.state_digest,
            "snapshot_revision": snapshot.revision,
        },
        candidate,
    )


def _require_platform() -> None:
    required_flags = ("O_CLOEXEC", "O_NOFOLLOW", "O_DIRECTORY")
    missing = [name for name in required_flags if not hasattr(os, name)]
    if os.name != "posix" or fcntl is None or not callable(getattr(os, "geteuid", None)) or missing:
        raise LifecyclePlatformError(
            f"generated-tool lifecycle 需要 POSIX flock/UID/O_CLOEXEC/O_NOFOLLOW/O_DIRECTORY；missing={missing}"
        )


def _validate_owned_stat(
    info: os.stat_result,
    path: Path,
    *,
    regular: bool = False,
    directory: bool = False,
    exact_mode: int | None = None,
) -> None:
    if info.st_uid != os.geteuid():
        raise LifecycleCorruptionError(f"受保护 lifecycle 路径所有者不匹配: {path}")
    if regular and not stat.S_ISREG(info.st_mode):
        raise LifecycleCorruptionError(f"lifecycle 路径必须是普通文件: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise LifecycleCorruptionError(f"lifecycle 路径必须是真实目录: {path}")
    if exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode:
        raise LifecycleCorruptionError(
            f"lifecycle 路径权限必须为 0o{exact_mode:o}: {path} (actual=0o{stat.S_IMODE(info.st_mode):o})"
        )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise LifecycleCorruptionError(f"无法安全打开目录 {path}: {error}") from error
    try:
        _validate_owned_stat(os.fstat(fd), path, directory=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _fsync_directory(path: Path) -> None:
    fd = _open_directory(path)
    try:
        os.fsync(fd)
    except OSError as error:
        raise LifecycleError(f"无法 fsync 目录 {path}: {error}") from error
    finally:
        os.close(fd)


def _fsync_state_directory_with_retry(path: Path) -> None:
    """Bound retries for the durability barrier while the lifecycle lock is held."""

    for attempt in range(_STATE_DIRECTORY_FSYNC_ATTEMPTS):
        try:
            _fsync_directory(path)
            return
        except (LifecycleError, OSError):
            if attempt + 1 >= _STATE_DIRECTORY_FSYNC_ATTEMPTS:
                raise
            time.sleep(_STATE_DIRECTORY_FSYNC_RETRY_SECONDS)


def _read_secure_file(path: Path, *, limit: int, exact_mode: int | None) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise LifecycleCorruptionError(f"无法安全读取 {path}: {error}") from error
    try:
        info = os.fstat(fd)
        _validate_owned_stat(info, path, regular=True, exact_mode=exact_mode)
        if info.st_size > limit:
            raise LifecycleCorruptionError(f"文件超过大小限制 {limit}: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(_COPY_BUFFER_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            raise LifecycleCorruptionError(f"文件超过大小限制 {limit}: {path}")
        try:
            path_info = path.lstat()
        except OSError as error:
            raise LifecycleCorruptionError(f"受保护文件读取期间消失或不可检查: {path}: {error}") from error
        if stat.S_ISLNK(path_info.st_mode) or (
            path_info.st_dev,
            path_info.st_ino,
        ) != (info.st_dev, info.st_ino):
            raise LifecycleCorruptionError(f"受保护文件读取期间被替换: {path}")
        return content
    finally:
        os.close(fd)


def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LifecycleCorruptionError(f"无法检查 lifecycle 路径 {path}: {error}") from error
    return True


class LifecycleStore:
    """Canonical lifecycle state guarded by one fixed OS lock."""

    def __init__(self, root: Path, *, lock_timeout_seconds: float = 5.0) -> None:
        self.root = Path(root)
        self.state_file = self.root / LIFECYCLE_STATE_FILE
        self.lock_file = self.root / LIFECYCLE_LOCK_FILE
        self.drafts_dir = self.root / "drafts"
        self.versions_dir = self.root / "versions"
        self.legacy_active_file = self.root / "active.json"
        self.legacy_permission_file = self.root / "permission_policy.json"
        self.lock_timeout_seconds = self._validate_timeout(lock_timeout_seconds)

    @staticmethod
    def _validate_timeout(value: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
            raise ValueError("lock timeout 必须是非负有限秒数")
        return float(value)

    def _ensure_root(self) -> None:
        _require_platform()
        ensure_private_directory(self.root)

    def _open_lock_file(self) -> int:
        self._ensure_root()
        common = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        created = False
        try:
            fd = os.open(self.lock_file, common | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                fd = os.open(self.lock_file, common)
            except OSError as error:
                raise LifecycleCorruptionError(f"无法安全打开固定 lifecycle lock: {self.lock_file}: {error}") from error
        except OSError as error:
            raise LifecycleCorruptionError(f"无法创建固定 lifecycle lock: {self.lock_file}: {error}") from error

        try:
            if created:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                _fsync_directory(self.root)
            info = os.fstat(fd)
            _validate_owned_stat(info, self.lock_file, regular=True, exact_mode=0o600)
            path_info = self.lock_file.lstat()
            if stat.S_ISLNK(path_info.st_mode) or (
                path_info.st_dev,
                path_info.st_ino,
            ) != (info.st_dev, info.st_ino):
                raise LifecycleCorruptionError("固定 lifecycle lock 在打开期间被替换")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def lock(
        self,
        *,
        exclusive: bool,
        timeout_seconds: float | None = None,
    ) -> Iterator[None]:
        timeout = self.lock_timeout_seconds if timeout_seconds is None else self._validate_timeout(timeout_seconds)
        fd = self._open_lock_file()
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, operation)
                    break
                except OSError as error:
                    if error.errno == errno.EINTR:
                        continue
                    if error.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise LifecycleError(f"lifecycle flock 失败: {error}") from error
                    if time.monotonic() >= deadline:
                        mode = "exclusive" if exclusive else "shared"
                        raise LifecycleLockTimeout(f"等待 {mode} lifecycle lock 超时 ({timeout:.3f}s)") from error
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

            fd_info = os.fstat(fd)
            path_info = self.lock_file.lstat()
            if stat.S_ISLNK(path_info.st_mode) or (
                path_info.st_dev,
                path_info.st_ino,
            ) != (fd_info.st_dev, fd_info.st_ino):
                raise LifecycleCorruptionError("固定 lifecycle lock 在等待期间被替换")
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def shared_lock(self, *, timeout_seconds: float | None = None):
        return self.lock(exclusive=False, timeout_seconds=timeout_seconds)

    def exclusive_lock(self, *, timeout_seconds: float | None = None):
        return self.lock(exclusive=True, timeout_seconds=timeout_seconds)

    def _read_state_locked(self) -> LifecycleState:
        if not _path_exists_no_follow(self.state_file):
            raise FileNotFoundError(self.state_file)
        content = _read_secure_file(
            self.state_file,
            limit=_MAX_STATE_BYTES,
            exact_mode=0o600,
        )
        return decode_lifecycle_state(content)

    def _atomic_write_state_locked(self, state: LifecycleState) -> None:
        content = encode_lifecycle_state(state)
        if len(content) > _MAX_STATE_BYTES:
            raise LifecycleError("lifecycle state 超过持久化大小限制")
        if _path_exists_no_follow(self.state_file):
            _read_secure_file(self.state_file, limit=_MAX_STATE_BYTES, exact_mode=0o600)

        temporary = self.root / f".{LIFECYCLE_STATE_FILE}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = -1
        replaced_state = False
        durability_confirmed = False
        try:
            fd = os.open(temporary, flags, 0o600)
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary, self.state_file)
            replaced_state = True
            _fsync_state_directory_with_retry(self.root)
            durability_confirmed = True
            persisted = _read_secure_file(
                self.state_file,
                limit=_MAX_STATE_BYTES,
                exact_mode=0o600,
            )
            if decode_lifecycle_state(persisted).state_digest != state.state_digest:
                raise LifecycleCorruptionError("原子写入后的 lifecycle state 校验失败")
        except Exception as error:
            if replaced_state:
                state_visible = False
                try:
                    observed = self._read_state_locked()
                    state_visible = observed.state_digest == state.state_digest
                except Exception:
                    pass
                visibility = "目标 state 当前可见" if state_visible else "当前可见 state 无法确认"
                durability = (
                    "目录 fsync 已确认"
                    if durability_confirmed
                    else "目录 fsync 未确认"
                )
                raise LifecycleCommitUncertainError(
                    "lifecycle state 已 replace，但持久化/回读失败；"
                    f"{durability}；{visibility}: {error}",
                    state_visible=state_visible,
                    durability_confirmed=durability_confirmed,
                ) from error
            if isinstance(error, LifecycleError):
                raise
            raise LifecycleError(f"durable lifecycle state 写入失败: {error}") from error
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self) -> LifecycleState:
        self._ensure_root()
        with self.shared_lock():
            try:
                return self._read_state_locked()
            except FileNotFoundError:
                pass
        with self.exclusive_lock():
            try:
                return self._read_state_locked()
            except FileNotFoundError:
                migrated = self._migrate_legacy_locked()
                self._atomic_write_state_locked(migrated)
                return migrated

    read = load

    @contextmanager
    def read_snapshot(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator[LifecycleState]:
        """Yield canonical state while retaining a shared lifecycle lock.

        Callers may validate/read immutable bundle files inside this context so
        a concurrent activation cannot publish a different state mid-snapshot.
        """

        self.load()
        with self.shared_lock(timeout_seconds=timeout_seconds):
            yield self._read_state_locked()

    @contextmanager
    def latest_exclusive_snapshot(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator[LifecycleState]:
        """Yield latest canonical state while retaining the exclusive lock.

        Compatibility projections (legacy active/permission/draft metadata)
        must be written only inside this context.  That prevents an older
        process from projecting stale state after a newer canonical commit.
        Callers acquire their in-process mutation RLock before entering.
        """

        self.load()
        with self.exclusive_lock(timeout_seconds=timeout_seconds):
            yield self._read_state_locked()

    def _commit_plan_internal(
        self,
        plan: LifecyclePlan,
        *,
        publish: ImmutableVersionPublish | None = None,
    ) -> LifecycleState:
        if not isinstance(plan, LifecyclePlan):
            raise TypeError("plan 必须是 LifecyclePlan")
        with self.exclusive_lock():
            current = self._load_or_migrate_locked()
            self._verify_plan_precondition(current, plan)
            if publish is not None:
                target = plan.after_state.versions.get(publish.bundle_id, {}).get(publish.digest)
                if target is None:
                    raise LifecycleConflictError("publish 版本不在 plan.after_state 中")
            if plan.no_op:
                if publish is not None:
                    self._publish_immutable_version_locked(publish)
                return current
            if publish is not None:
                self._publish_immutable_version_locked(publish)
            self._atomic_write_state_locked(plan.after_state)
            return plan.after_state

    def _compare_and_swap_internal(
        self,
        *,
        expected_revision: int,
        expected_state_digest: str,
        new_state: LifecycleState,
    ) -> LifecycleState:
        _require_identifier(expected_state_digest, _DIGEST_RE, "expected_state_digest")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise LifecycleConflictError("CAS expected_revision 必须是非负整数")
        if not isinstance(new_state, LifecycleState):
            raise TypeError("new_state 必须是 LifecycleState")
        if new_state.revision != expected_revision + 1:
            raise LifecycleConflictError("CAS new_state revision 必须精确增加 1")
        with self.exclusive_lock():
            current = self._load_or_migrate_locked()
            if current.revision != expected_revision or current.state_digest != expected_state_digest:
                raise LifecycleConflictError("lifecycle CAS 冲突: persisted revision/state digest 已变化")
            self._atomic_write_state_locked(new_state)
            return new_state

    def _verify_plan_precondition(self, current: LifecycleState, plan: LifecyclePlan) -> None:
        if current.revision != plan.expected_revision or current.state_digest != plan.before_digest:
            raise LifecycleConflictError("lifecycle plan CAS 冲突: persisted revision/state digest 已变化")

    def _load_or_migrate_locked(self) -> LifecycleState:
        try:
            return self._read_state_locked()
        except FileNotFoundError:
            migrated = self._migrate_legacy_locked()
            self._atomic_write_state_locked(migrated)
            return migrated

    def _publish_immutable_version_internal(
        self,
        source_directory: Path,
        bundle_id: str,
        digest: str,
    ) -> Path:
        publish = ImmutableVersionPublish(Path(source_directory), bundle_id, digest)
        with self.exclusive_lock():
            return self._publish_immutable_version_locked(publish)

    def _publish_immutable_version_locked(self, publish: ImmutableVersionPublish) -> Path:
        source = publish.source_directory
        self._validate_directory(source, label="version source")
        actual_digest = self._bundle_digest(source, expected_bundle_id=publish.bundle_id)
        if actual_digest != publish.digest:
            raise ImmutableVersionError(f"publish bundle digest 不匹配: expected={publish.digest}, actual={actual_digest}")

        ensure_private_directory(self.versions_dir)
        _fsync_directory(self.root)
        bundle_directory = self.versions_dir / publish.bundle_id
        ensure_private_directory(bundle_directory)
        _fsync_directory(self.versions_dir)
        destination = bundle_directory / publish.digest
        if _path_exists_no_follow(destination):
            self._validate_immutable_tree(destination)
            if (
                self._bundle_digest(
                    destination,
                    expected_bundle_id=publish.bundle_id,
                )
                != publish.digest
            ):
                raise ImmutableVersionError("已存在的 immutable version 内容哈希冲突")
            return destination

        temporary = bundle_directory / f".{publish.digest}.{secrets.token_hex(16)}.tmp"
        try:
            os.mkdir(temporary, 0o700)
            for name in ("manifest.json", "tool.py", "tests.py"):
                self._copy_bundle_file(source / name, temporary / name)
            if (
                self._bundle_digest(
                    temporary,
                    expected_bundle_id=publish.bundle_id,
                )
                != publish.digest
            ):
                raise ImmutableVersionError("临时 immutable version 内容哈希校验失败")
            _fsync_directory(temporary)
            for child in temporary.iterdir():
                os.chmod(child, 0o400, follow_symlinks=False)
            os.chmod(temporary, 0o500, follow_symlinks=False)
            _fsync_directory(temporary)
            if _path_exists_no_follow(destination):
                raise ImmutableVersionError("immutable version 发布目标并发出现")
            os.replace(temporary, destination)
            _fsync_directory(bundle_directory)
            self._validate_immutable_tree(destination)
            return destination
        except OSError as error:
            raise ImmutableVersionError(f"durable immutable version 发布失败: {error}") from error
        finally:
            if _path_exists_no_follow(temporary):
                self._remove_private_temporary_tree(temporary)

    def _copy_bundle_file(self, source: Path, destination: Path) -> None:
        content = _read_secure_file(source, limit=_BUNDLE_FILE_LIMIT, exact_mode=None)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(destination, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _remove_private_temporary_tree(self, root: Path) -> None:
        try:
            os.chmod(root, 0o700, follow_symlinks=False)
            for child in root.iterdir():
                info = child.lstat()
                if stat.S_ISREG(info.st_mode):
                    os.chmod(child, 0o600, follow_symlinks=False)
                    child.unlink()
            root.rmdir()
        except OSError:
            pass

    def _validate_directory(self, path: Path, *, label: str) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as error:
            raise ImmutableVersionError(f"{label} 不可访问: {path}: {error}") from error
        if stat.S_ISLNK(info.st_mode):
            raise ImmutableVersionError(f"{label} 禁止符号链接: {path}")
        try:
            _validate_owned_stat(info, path, directory=True)
        except LifecycleCorruptionError as error:
            raise ImmutableVersionError(str(error)) from error
        return info

    def _validate_immutable_tree(self, root: Path) -> None:
        info = self._validate_directory(root, label="immutable version")
        if stat.S_IMODE(info.st_mode) != 0o500:
            raise ImmutableVersionError("immutable version 目录权限必须为 0500")
        required = {"manifest.json", "tool.py", "tests.py"}
        seen: set[str] = set()
        try:
            children = list(root.iterdir())
        except OSError as error:
            raise ImmutableVersionError(f"无法遍历 immutable version: {error}") from error
        for child in children:
            child_info = child.lstat()
            if not stat.S_ISREG(child_info.st_mode):
                raise ImmutableVersionError(f"immutable version 只允许普通文件: {child}")
            _validate_owned_stat(child_info, child, regular=True, exact_mode=0o400)
            seen.add(child.name)
        if seen != required:
            raise ImmutableVersionError(
                "immutable version 必须且只能包含 manifest.json/tool.py/tests.py"
            )

    def _verify_immutable_version_locked(
        self,
        bundle_id: str,
        digest: str,
    ) -> Path:
        """Verify one immutable version while the caller retains lifecycle lock."""

        _require_identifier(bundle_id, _BUNDLE_ID_RE, "immutable bundle_id")
        _require_identifier(digest, _DIGEST_RE, "immutable digest")
        self._validate_directory(self.versions_dir, label="versions root")
        bundle_directory = self.versions_dir / bundle_id
        self._validate_directory(
            bundle_directory,
            label="immutable version bundle",
        )
        path = bundle_directory / digest
        before = self._validate_directory(path, label="immutable version")
        self._validate_immutable_tree(path)
        actual_digest = self._bundle_digest(
            path,
            expected_bundle_id=bundle_id,
            exact_file_mode=0o400,
        )
        after = self._validate_directory(path, label="immutable version")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ImmutableVersionError("immutable version 校验期间被替换")
        self._validate_immutable_tree(path)
        if actual_digest != digest:
            raise ImmutableVersionError(
                "immutable version 目录名与完整内容 digest 不一致"
            )
        return path

    def verify_immutable_version(self, bundle_id: str, digest: str) -> Path:
        """Read-only integrity gate for an already published version."""

        self.load()
        with self.shared_lock():
            return self._verify_immutable_version_locked(bundle_id, digest)

    @staticmethod
    def _normalize_source(content: bytes) -> bytes:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ImmutableVersionError("bundle source 必须是 UTF-8") from error
        return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

    def _bundle_digest(
        self,
        directory: Path,
        *,
        expected_bundle_id: str | None = None,
        exact_file_mode: int | None = None,
    ) -> str:
        manifest_bytes = _read_secure_file(
            directory / "manifest.json",
            limit=_BUNDLE_FILE_LIMIT,
            exact_mode=exact_file_mode,
        )
        source = _read_secure_file(
            directory / "tool.py",
            limit=_BUNDLE_FILE_LIMIT,
            exact_mode=exact_file_mode,
        )
        tests = _read_secure_file(
            directory / "tests.py",
            limit=_BUNDLE_FILE_LIMIT,
            exact_mode=exact_file_mode,
        )
        manifest = _decode_json_bytes(manifest_bytes, label="manifest.json")
        if not isinstance(manifest, dict):
            raise ImmutableVersionError("manifest.json 顶层必须是对象")
        if expected_bundle_id is not None and manifest.get("bundle_id") != expected_bundle_id:
            raise ImmutableVersionError(
                f"manifest bundle_id 与版本目录不一致: expected={expected_bundle_id!r}, actual={manifest.get('bundle_id')!r}"
            )
        return hashlib.sha256(
            _canonical_json_bytes(manifest) + b"\0" + self._normalize_source(source) + b"\0" + self._normalize_source(tests)
        ).hexdigest()

    def _migrate_legacy_locked(self) -> LifecycleState:
        active = self._read_legacy_active()
        drafts = self._read_legacy_drafts()
        versions = self._read_legacy_versions(active, drafts)
        grants = self._read_legacy_permissions(versions)
        return LifecycleState(
            revision=0,
            drafts=drafts,
            versions=versions,
            active=active,
            permission_grants=grants,
        )

    def _legacy_json(self, path: Path, *, label: str) -> Any | None:
        if not _path_exists_no_follow(path):
            return None
        content = _read_secure_file(
            path,
            limit=_MAX_LEGACY_JSON_BYTES,
            exact_mode=None,
        )
        return _decode_json_bytes(content, label=label)

    def _read_legacy_active(self) -> dict[str, str]:
        value = self._legacy_json(self.legacy_active_file, label="legacy active.json")
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise LifecycleCorruptionError("legacy active.json 必须是字符串映射")
        active: dict[str, str] = {}
        for bundle_id, digest in value.items():
            _require_identifier(bundle_id, _BUNDLE_ID_RE, "legacy active bundle_id")
            _require_identifier(digest, _DIGEST_RE, "legacy active digest")
            active[bundle_id] = digest
        return active

    def _read_legacy_drafts(self) -> dict[str, DraftRecord]:
        if not _path_exists_no_follow(self.drafts_dir):
            return {}
        self._validate_directory(self.drafts_dir, label="legacy drafts")
        status_map = {
            "draft": DraftState.DRAFT,
            "static_validated": DraftState.STATIC_VALIDATED,
            "validated": DraftState.STATIC_VALIDATED,
            "sandbox_tested": DraftState.SANDBOX_TESTED,
            "tested": DraftState.SANDBOX_TESTED,
            "model_reviewed": DraftState.MODEL_REVIEWED,
            "reviewed": DraftState.AWAITING_APPROVAL,
            "awaiting_approval": DraftState.AWAITING_APPROVAL,
            "approved": DraftState.APPROVED,
            "rejected": DraftState.REJECTED,
            "review_failed": DraftState.REVIEW_FAILED,
            "validation_failed": DraftState.VALIDATION_FAILED,
            "test_failed": DraftState.TEST_FAILED,
        }
        drafts: dict[str, DraftRecord] = {}
        paths = sorted(self.drafts_dir.iterdir(), key=lambda item: item.name)
        for path in paths:
            if path.name.startswith("."):
                continue
            if len(drafts) >= _MAX_MIGRATED_DRAFTS:
                raise LifecycleCorruptionError("legacy drafts 数量超过迁移上限")
            self._validate_directory(path, label="legacy draft")
            _require_identifier(path.name, _DRAFT_ID_RE, "legacy draft_id")
            metadata = self._legacy_json(path / "metadata.json", label="legacy draft metadata")
            manifest = self._legacy_json(path / "manifest.json", label="legacy draft manifest")
            if not isinstance(metadata, dict) or not isinstance(manifest, dict):
                raise LifecycleCorruptionError(f"legacy draft 元数据缺失: {path.name}")
            if metadata.get("draft_id") != path.name:
                raise LifecycleCorruptionError("legacy draft metadata draft_id 不一致")
            bundle_id = manifest.get("bundle_id")
            _require_identifier(bundle_id, _BUNDLE_ID_RE, "legacy draft bundle_id")
            digest = metadata.get("digest")
            _require_identifier(digest, _DIGEST_RE, "legacy draft digest")
            actual_digest = self._bundle_digest(path)
            if actual_digest != digest:
                raise LifecycleCorruptionError("legacy draft digest 校验失败")
            raw_status = metadata.get("status")
            if raw_status not in status_map:
                raise LifecycleCorruptionError(f"未知 legacy draft status: {raw_status!r}")
            info = (path / "metadata.json").lstat()
            created_at = metadata.get("created_at", info.st_mtime)
            _require_timestamp(created_at, "legacy draft created_at")
            draft_state = status_map[raw_status]
            updated_at = max(float(created_at), float(info.st_mtime))
            drafts[path.name] = DraftRecord(
                draft_id=path.name,
                bundle_id=bundle_id,
                digest=digest,
                state=draft_state,
                created_at=float(created_at),
                updated_at=updated_at,
                evidence=_legacy_evidence(
                    state=draft_state,
                    digest=digest,
                    recorded_at=updated_at,
                ),
            )
        return drafts

    def _read_legacy_versions(
        self,
        active: Mapping[str, str],
        drafts: Mapping[str, DraftRecord],
    ) -> dict[str, dict[str, VersionRecord]]:
        if not _path_exists_no_follow(self.versions_dir):
            if active:
                raise LifecycleCorruptionError("legacy active 指向版本，但 versions 目录不存在")
            return {}
        self._validate_directory(self.versions_dir, label="legacy versions")
        source_drafts: dict[tuple[str, str], list[str]] = {}
        for draft in drafts.values():
            if draft.state is DraftState.APPROVED:
                source_drafts.setdefault((draft.bundle_id, draft.digest), []).append(draft.draft_id)

        versions: dict[str, dict[str, VersionRecord]] = {}
        count = 0
        for bundle_path in sorted(self.versions_dir.iterdir(), key=lambda item: item.name):
            if bundle_path.name.startswith("."):
                continue
            self._validate_directory(bundle_path, label="legacy version bundle")
            _require_identifier(bundle_path.name, _BUNDLE_ID_RE, "legacy version bundle_id")
            bundle_versions: dict[str, VersionRecord] = {}
            for version_path in sorted(bundle_path.iterdir(), key=lambda item: item.name):
                if version_path.name.startswith("."):
                    continue
                count += 1
                if count > _MAX_MIGRATED_VERSIONS:
                    raise LifecycleCorruptionError("legacy versions 数量超过迁移上限")
                self._validate_directory(version_path, label="legacy version")
                _require_identifier(version_path.name, _DIGEST_RE, "legacy version digest")
                actual_digest = self._bundle_digest(
                    version_path,
                    expected_bundle_id=bundle_path.name,
                )
                if actual_digest != version_path.name:
                    raise LifecycleCorruptionError("legacy version 目录名与内容 digest 不一致")
                timestamp = float(version_path.lstat().st_mtime)
                is_active = active.get(bundle_path.name) == version_path.name
                candidates = source_drafts.get((bundle_path.name, version_path.name), [])
                source_draft_id = candidates[0] if len(candidates) == 1 else None
                bundle_versions[version_path.name] = VersionRecord(
                    bundle_id=bundle_path.name,
                    digest=version_path.name,
                    state=(VersionState.ACTIVATED if is_active else VersionState.DEPRECATED),
                    source_draft_id=source_draft_id,
                    created_at=timestamp,
                    approved_at=timestamp,
                    activated_at=timestamp,
                    deprecated_at=None if is_active else timestamp,
                )
            versions[bundle_path.name] = bundle_versions

        for bundle_id, digest in active.items():
            if digest not in versions.get(bundle_id, {}):
                raise LifecycleCorruptionError(f"legacy active 指向不存在版本: {bundle_id}@{digest}")
        for draft in drafts.values():
            if draft.state is DraftState.APPROVED and draft.digest not in versions.get(draft.bundle_id, {}):
                raise LifecycleCorruptionError(f"legacy approved draft 缺少版本: {draft.draft_id}")
        return versions

    def _read_legacy_permissions(
        self,
        versions: Mapping[str, Mapping[str, VersionRecord]],
    ) -> dict[str, PermissionGrant]:
        value = self._legacy_json(
            self.legacy_permission_file,
            label="legacy permission_policy.json",
        )
        if value is None:
            return {}
        root = _require_exact_keys(value, frozenset({"version", "grants"}), "legacy permission policy")
        if root["version"] != 1 or not isinstance(root["grants"], dict):
            raise LifecycleCorruptionError("legacy permission policy 版本或 grants 非法")
        grants: dict[str, PermissionGrant] = {}
        for key, raw_grant in root["grants"].items():
            bundle_id, digest, _ = _split_permission_key(key)
            if digest not in versions.get(bundle_id, {}):
                raise LifecycleCorruptionError(f"legacy permission grant 指向未知版本: {key}")
            grants[key] = _grant_from_dict(raw_grant)
        return grants


__all__ = [
    "LIFECYCLE_LOCK_FILE",
    "LIFECYCLE_SCHEMA_VERSION",
    "LIFECYCLE_STATE_FILE",
    "DraftEvidence",
    "DraftRecord",
    "DraftState",
    "ImmutableVersionError",
    "ImmutableVersionPublish",
    "LifecycleCommitUncertainError",
    "LifecycleConflictError",
    "LifecycleCorruptionError",
    "LifecycleError",
    "LifecycleLockTimeout",
    "LifecyclePlan",
    "LifecyclePlatformError",
    "LifecycleState",
    "LifecycleStore",
    "LifecycleTransitionError",
    "PermissionGrant",
    "VersionRecord",
    "VersionState",
    "decode_lifecycle_state",
    "encode_lifecycle_state",
    "permission_key",
    "plan_activate_from_draft",
    "plan_activate_version",
    "plan_approve_draft",
    "plan_archive",
    "plan_deactivate",
    "plan_permission",
    "plan_record_draft",
    "plan_reject",
    "plan_restore_snapshot",
    "plan_rollback",
    "plan_transition_draft",
]

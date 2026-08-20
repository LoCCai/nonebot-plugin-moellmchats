from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import threading
import time
from types import MappingProxyType
from typing import Any
import uuid

from nonebot.log import logger

from .ast_policy import (
    AstPolicyReport,
    PolicyDecision,
    PolicyFinding,
    analyze_ast_policy,
)
from .generated_tool_lifecycle import (
    DraftEvidence,
    DraftRecord,
    DraftState,
    ImmutableVersionPublish,
    LifecycleCommitUncertainError,
    LifecycleConflictError,
    LifecyclePlan,
    LifecycleState,
    LifecycleStore,
    LifecycleTransitionError,
    VersionState,
    plan_activate_from_draft,
    plan_deactivate,
    plan_permission,
    plan_record_draft,
    plan_reject,
    plan_rollback,
    plan_transition_draft,
)
from .generated_tool_runner import generated_tool_runner
from .model_selector import config_path
from .private_files import (
    ensure_private_directory,
    ensure_private_file,
    harden_private_tree,
    harden_readonly_tree,
)
from .tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from .tool_contracts import (
    ToolContext,
    ToolEffect,
    ToolPolicy,
    ToolSpec,
    validate_parameters_schema,
)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?:postgres(?:ql)?|redis)://[^\s'\"]+", re.IGNORECASE),
)
_PERMISSION_POLICY_VERSION = 1
_DRAFT_REVIEW_PAGE_LIMIT = 1800
_DRAFT_REVIEW_SECTIONS = (
    "summary",
    "manifest",
    "source",
    "tests",
    "risks",
    "capabilities",
    "diff",
)
_CANONICAL_TO_LEGACY_DRAFT_STATUS = {
    DraftState.DRAFT: "draft",
    DraftState.STATIC_VALIDATED: "static_validated",
    DraftState.SANDBOX_TESTED: "sandbox_tested",
    DraftState.MODEL_REVIEWED: "model_reviewed",
    DraftState.AWAITING_APPROVAL: "reviewed",
    DraftState.APPROVED: "approved",
    DraftState.REJECTED: "rejected",
    DraftState.REVIEW_FAILED: "review_failed",
    DraftState.VALIDATION_FAILED: "validation_failed",
    DraftState.TEST_FAILED: "test_failed",
}
@dataclass(frozen=True)
class BundleValidation:
    manifest: dict[str, Any]
    digest: str
    risks: tuple[str, ...]
    policy: ToolPolicy
    tool_policies: Mapping[str, ToolPolicy]
    tool_ast_report: AstPolicyReport
    tests_ast_report: AstPolicyReport
    source: bytes
    tests_source: bytes


def _freeze_lifecycle_result(value: Any) -> Any:
    """Detach and recursively freeze values carried across reload phases."""

    if isinstance(value, Mapping):
        return MappingProxyType({deepcopy(key): _freeze_lifecycle_result(item) for key, item in value.items()})
    if isinstance(value, tuple | list):
        return tuple(_freeze_lifecycle_result(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_lifecycle_result(item) for item in value)
    return deepcopy(value)


@dataclass(frozen=True)
class PreparedLifecycleChange:
    """One immutable lifecycle CAS prepared before runtime candidate loading."""

    plan: LifecyclePlan
    result: Any
    publish: ImmutableVersionPublish | None = None
    generated_source_overrides: Mapping[tuple[str, str], Path] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.plan, LifecyclePlan):
            raise TypeError("plan 必须是 LifecyclePlan")
        if self.publish is not None and not isinstance(self.publish, ImmutableVersionPublish):
            raise TypeError("publish 必须是 ImmutableVersionPublish 或 None")
        if not isinstance(self.generated_source_overrides, Mapping):
            raise TypeError("generated_source_overrides 必须是映射")
        overrides: dict[tuple[str, str], Path] = {}
        for key, path in self.generated_source_overrides.items():
            if not isinstance(key, tuple) or len(key) != 2 or not all(isinstance(item, str) for item in key):
                raise TypeError("生成工具源覆盖 key 必须是 (bundle_id, digest)")
            overrides[key] = Path(path)
        object.__setattr__(
            self,
            "generated_source_overrides",
            MappingProxyType(overrides),
        )
        object.__setattr__(self, "result", _freeze_lifecycle_result(self.result))


def _canonical_review_json(value: Any) -> str:
    """Serialize review data deterministically without escaping human text."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("草稿审阅数据必须是规范 JSON") from error


def _draft_review_stamp(
    *,
    draft_id: str,
    digest: str,
    lifecycle_revision: int,
    lifecycle_state_digest: str,
    active_digest: str | None,
) -> str:
    payload = _canonical_review_json(
        {
            "version": 1,
            "draft_id": draft_id,
            "draft_digest": digest,
            "lifecycle_revision": lifecycle_revision,
            "lifecycle_state_digest": lifecycle_state_digest,
            "active_digest": active_digest,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _draft_review_page_header(
    *,
    draft_id: str,
    digest: str,
    lifecycle_revision: int,
    lifecycle_state_digest: str,
    active_digest: str | None,
    review_stamp: str,
    section: str,
    page: int,
    total_pages: int,
    content_sha256: str,
) -> str:
    return (
        f"draft_id: {draft_id}\n"
        f"digest: {digest}\n"
        f"lifecycle_revision: {lifecycle_revision}\n"
        f"lifecycle_state_digest: {lifecycle_state_digest}\n"
        f"active_digest: {active_digest or 'none'}\n"
        f"review_stamp: {review_stamp}\n"
        "approve_command: "
        f"批准LLM功能 {draft_id} {digest[:12]} {review_stamp}\n"
        f"section: {section}\n"
        f"page: {page}/{total_pages}\n"
        f"content_sha256: {content_sha256}\n\n"
    )


@dataclass(frozen=True)
class DraftReviewPage:
    """One immutable, plain-text page from a validated draft snapshot."""

    draft_id: str
    digest: str
    lifecycle_revision: int
    lifecycle_state_digest: str
    active_digest: str | None
    review_stamp: str
    section: str
    page: int
    total_pages: int
    content_sha256: str
    content: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{12}", self.draft_id):
            raise ValueError("草稿 ID 非法")
        if not re.fullmatch(r"[a-f0-9]{64}", self.digest):
            raise ValueError("草稿哈希非法")
        if (
            not isinstance(self.lifecycle_revision, int)
            or isinstance(self.lifecycle_revision, bool)
            or self.lifecycle_revision < 0
        ):
            raise ValueError("lifecycle revision 非法")
        if not re.fullmatch(r"[a-f0-9]{64}", self.lifecycle_state_digest):
            raise ValueError("lifecycle state digest 非法")
        if self.active_digest is not None and not re.fullmatch(
            r"[a-f0-9]{64}",
            self.active_digest,
        ):
            raise ValueError("active digest 非法")
        expected_stamp = _draft_review_stamp(
            draft_id=self.draft_id,
            digest=self.digest,
            lifecycle_revision=self.lifecycle_revision,
            lifecycle_state_digest=self.lifecycle_state_digest,
            active_digest=self.active_digest,
        )
        if not secrets.compare_digest(self.review_stamp, expected_stamp):
            raise ValueError("review stamp 与审阅快照不匹配")
        if self.section not in _DRAFT_REVIEW_SECTIONS:
            raise ValueError(f"审阅区段非法：{self.section}")
        if not 1 <= self.page <= self.total_pages:
            raise ValueError("草稿审阅页码非法")
        if not re.fullmatch(r"[a-f0-9]{64}", self.content_sha256):
            raise ValueError("草稿审阅内容哈希非法")
        if not isinstance(self.content, str):
            raise ValueError("草稿审阅分页内容必须是文本")
        if len(self.text) > _DRAFT_REVIEW_PAGE_LIMIT:
            raise ValueError("草稿审阅分页超过单页上限")

    @property
    def header(self) -> str:
        return _draft_review_page_header(
            draft_id=self.draft_id,
            digest=self.digest,
            lifecycle_revision=self.lifecycle_revision,
            lifecycle_state_digest=self.lifecycle_state_digest,
            active_digest=self.active_digest,
            review_stamp=self.review_stamp,
            section=self.section,
            page=self.page,
            total_pages=self.total_pages,
            content_sha256=self.content_sha256,
        )

    @property
    def text(self) -> str:
        return self.header + self.content

    def render(self) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class DraftReviewSnapshot:
    """A complete point-in-time review assembled from one validated read."""

    draft_id: str
    digest: str
    lifecycle_revision: int
    lifecycle_state_digest: str
    active_digest: str | None
    review_stamp: str
    sections: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sections, tuple) or not all(isinstance(item, tuple) and len(item) == 2 for item in self.sections):
            raise ValueError("草稿审阅区段必须是不可变二元组")
        names = tuple(name for name, _ in self.sections)
        if names != _DRAFT_REVIEW_SECTIONS:
            raise ValueError("草稿审阅区段不完整或顺序非法")
        if not re.fullmatch(r"[a-f0-9]{12}", self.draft_id):
            raise ValueError("草稿 ID 非法")
        if not re.fullmatch(r"[a-f0-9]{64}", self.digest):
            raise ValueError("草稿哈希非法")
        if (
            not isinstance(self.lifecycle_revision, int)
            or isinstance(self.lifecycle_revision, bool)
            or self.lifecycle_revision < 0
        ):
            raise ValueError("lifecycle revision 非法")
        if not re.fullmatch(r"[a-f0-9]{64}", self.lifecycle_state_digest):
            raise ValueError("lifecycle state digest 非法")
        if self.active_digest is not None and not re.fullmatch(
            r"[a-f0-9]{64}",
            self.active_digest,
        ):
            raise ValueError("active digest 非法")
        expected_stamp = _draft_review_stamp(
            draft_id=self.draft_id,
            digest=self.digest,
            lifecycle_revision=self.lifecycle_revision,
            lifecycle_state_digest=self.lifecycle_state_digest,
            active_digest=self.active_digest,
        )
        if not secrets.compare_digest(self.review_stamp, expected_stamp):
            raise ValueError("review stamp 与审阅快照不匹配")
        if not all(isinstance(content, str) for _, content in self.sections):
            raise ValueError("草稿审阅内容必须是文本")

    @property
    def available_sections(self) -> tuple[str, ...]:
        return _DRAFT_REVIEW_SECTIONS

    @property
    def approval_command(self) -> str:
        return (
            f"批准LLM功能 {self.draft_id} {self.digest[:12]} "
            f"{self.review_stamp}"
        )

    def section_content(self, section: str) -> str:
        if section not in _DRAFT_REVIEW_SECTIONS:
            choices = ", ".join(_DRAFT_REVIEW_SECTIONS)
            raise ValueError(f"审阅区段非法：{section}；可选：{choices}")
        return next(content for name, content in self.sections if name == section)

    def pages(self, section: str) -> tuple[DraftReviewPage, ...]:
        content = self.section_content(section)
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # The header is part of the QQ message budget and contains total_pages.
        # Starting with one page and recomputing is monotonic; at most the
        # decimal-width boundaries of total_pages can add another iteration.
        total_hint = 1
        chunks: list[str] = []
        for _ in range(64):
            chunks = []
            offset = 0
            page_number = 1
            while offset < len(content) or not chunks:
                header = _draft_review_page_header(
                    draft_id=self.draft_id,
                    digest=self.digest,
                    lifecycle_revision=self.lifecycle_revision,
                    lifecycle_state_digest=self.lifecycle_state_digest,
                    active_digest=self.active_digest,
                    review_stamp=self.review_stamp,
                    section=section,
                    page=page_number,
                    total_pages=total_hint,
                    content_sha256=content_sha256,
                )
                capacity = _DRAFT_REVIEW_PAGE_LIMIT - len(header)
                if capacity <= 0:
                    raise ValueError("草稿审阅页头超过单页上限")
                chunk = content[offset : offset + capacity]
                chunks.append(chunk)
                offset += len(chunk)
                page_number += 1
            if len(chunks) == total_hint:
                break
            total_hint = len(chunks)
        else:  # pragma: no cover - bounded 64 KiB bundles converge immediately
            raise ValueError("草稿审阅分页无法收敛")

        total_pages = len(chunks)
        pages = tuple(
            DraftReviewPage(
                draft_id=self.draft_id,
                digest=self.digest,
                lifecycle_revision=self.lifecycle_revision,
                lifecycle_state_digest=self.lifecycle_state_digest,
                active_digest=self.active_digest,
                review_stamp=self.review_stamp,
                section=section,
                page=index,
                total_pages=total_pages,
                content_sha256=content_sha256,
                content=chunk,
            )
            for index, chunk in enumerate(chunks, start=1)
        )
        if any(len(page.text) > _DRAFT_REVIEW_PAGE_LIMIT for page in pages):
            raise ValueError("草稿审阅分页超过单页上限")
        if "".join(page.content for page in pages) != content:
            raise ValueError("草稿审阅分页内容不完整")
        return pages

    def get_page(self, section: str, page: int = 1) -> DraftReviewPage:
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError("页码必须是从 1 开始的正整数")
        pages = self.pages(section)
        if page > len(pages):
            raise ValueError(f"页码越界：{section} 共 {len(pages)} 页，请输入 1-{len(pages)}")
        return pages[page - 1]


def _review_file_diff(
    *,
    filename: str,
    old_content: str,
    new_content: str,
    old_label: str,
    new_label: str,
) -> str:
    body = "\n".join(
        difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile=old_label,
            tofile=new_label,
            lineterm="",
        )
    )
    return f"===== {filename} =====\n{body or '无变化'}"


def _atomic_json(path: Path, value: Any, mode: int = 0o600) -> None:
    ensure_private_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        ensure_private_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _literal_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    value = node.value
    if value is None:
        return True
    try:
        ast.literal_eval(value)
        return True
    except (ValueError, TypeError):
        return False


def _validate_top_level(
    tree: ast.Module,
    *,
    allowed_dynamic_assignments: frozenset[str] = frozenset(),
) -> list[str]:
    errors: list[str] = []
    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Pass,
    )
    for node in tree.body:
        if isinstance(node, allowed):
            continue
        if isinstance(node, ast.Assign | ast.AnnAssign) and _literal_assignment(node):
            continue
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in allowed_dynamic_assignments for target in node.targets
        ):
            continue
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in allowed_dynamic_assignments
        ):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        errors.append(f"line {getattr(node, 'lineno', '?')}: 禁止模块顶层执行 {type(node).__name__}")
    return errors


def _policy_finding_message(finding: PolicyFinding) -> str:
    location = finding.scope or ""
    if finding.line is not None:
        location = f"{location}:line {finding.line}" if location else f"line {finding.line}"
    return f"{location}: {finding.message}" if location else finding.message


def _reject_blocking_policy(filename: str, report: AstPolicyReport) -> None:
    blockers = report.blocking_findings
    if blockers:
        messages = "; ".join(_policy_finding_message(item) for item in blockers[:5])
        raise ValueError(f"{filename} AST policy 拒绝: {messages}")


def _risk_messages(filename: str, report: AstPolicyReport) -> tuple[str, ...]:
    return tuple(
        f"{filename}: {_policy_finding_message(item)}" for item in report.findings if item.decision is PolicyDecision.RISK
    )


class GeneratedToolStore:
    def __init__(self) -> None:
        self._mutation_lock = threading.RLock()
        self._hardened_root: Path | None = None
        self._initialized_root: Path | None = None
        self._lifecycle_root: Path | None = None
        self._lifecycle_store: LifecycleStore | None = None
        self._projection_stale = False
        self._projection_error: str | None = None
        self.root = Path(config_path / "generated_tools")
        self.drafts_dir = self.root / "drafts"
        self.versions_dir = self.root / "versions"
        self.active_file = self.root / "active.json"

    @property
    def permission_policy_file(self) -> Path:
        return self.root / "permission_policy.json"

    def _lifecycle(self) -> LifecycleStore:
        root_identity = self.root.absolute()
        if self._lifecycle_store is None or self._lifecycle_root != root_identity:
            self._lifecycle_store = LifecycleStore(self.root)
            self._lifecycle_root = root_identity
        return self._lifecycle_store

    @staticmethod
    def _require_generated_state(state: LifecycleState) -> LifecycleState:
        if not isinstance(state, LifecycleState):
            raise TypeError("generated_state 必须是 LifecycleState")
        return state

    @staticmethod
    def _same_lifecycle_state(
        left: LifecycleState,
        right: LifecycleState,
    ) -> bool:
        return left.revision == right.revision and left.state_digest == right.state_digest

    @staticmethod
    def _write_projection(path: Path, value: Any) -> None:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            current = object()
        if current == value:
            ensure_private_file(path)
            return
        _atomic_json(path, value)

    @staticmethod
    def _projected_metadata(
        metadata: dict[str, Any],
        record: DraftRecord,
        state: LifecycleState,
    ) -> dict[str, Any]:
        projected = deepcopy(metadata)
        projected.update(
            {
                "draft_id": record.draft_id,
                "digest": record.digest,
                "status": _CANONICAL_TO_LEGACY_DRAFT_STATUS[record.state],
                "canonical_state": record.state.value,
                "lifecycle_evidence": [
                    item.as_dict() for item in record.evidence
                ],
                "lifecycle_revision": state.revision,
                "lifecycle_state_digest": state.state_digest,
            }
        )
        return projected

    def _project_lifecycle_state(self, state: LifecycleState) -> None:
        """Repair compatibility files while an exclusive lifecycle lock is held."""

        self._write_projection(self.active_file, dict(state.active))
        self._write_projection(
            self.permission_policy_file,
            {
                "version": _PERMISSION_POLICY_VERSION,
                "grants": {key: grant.as_dict() for key, grant in state.permission_grants.items()},
            },
        )
        for draft_id, record in state.drafts.items():
            path = self.drafts_dir / draft_id / "metadata.json"
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"canonical draft {draft_id} metadata.json 无法投影: {error}") from error
            if not isinstance(metadata, dict):
                raise ValueError(f"canonical draft {draft_id} metadata.json 顶层必须是对象")
            if metadata.get("draft_id") != draft_id:
                raise ValueError(f"canonical draft {draft_id} metadata draft_id 不匹配")
            if metadata.get("digest") != record.digest:
                raise ValueError(f"canonical draft {draft_id} metadata digest 不匹配")
            self._write_projection(
                path,
                self._projected_metadata(metadata, record, state),
            )

    def _mark_projection_failure(
        self,
        error: BaseException,
        state: LifecycleState,
    ) -> None:
        self._projection_stale = True
        self._projection_error = str(error)[:500]
        logger.error(
            "Generated Tool canonical state 已生效，但 legacy 投影"
            f"修复失败 revision={state.revision}: {self._projection_error}"
        )

    def _repair_legacy_projections(
        self,
        *,
        strict: bool = False,
    ) -> LifecycleState:
        lifecycle = self._lifecycle()
        with lifecycle.latest_exclusive_snapshot() as state:
            try:
                self._project_lifecycle_state(state)
            except Exception as error:
                self._mark_projection_failure(error, state)
                if strict:
                    raise
            else:
                self._projection_stale = False
                self._projection_error = None
            return state

    def _init_files(self) -> None:
        # Lock order is always the in-process mutation RLock followed by the
        # lifecycle OS lock.  Callers of this internal method hold the RLock.
        root_identity = self.root.absolute()
        if self._initialized_root == root_identity:
            return
        ensure_private_directory(self.root)
        ensure_private_directory(self.drafts_dir)
        ensure_private_directory(self.versions_dir)
        if self._hardened_root != root_identity:
            harden_private_tree(self.drafts_dir)
            # Bundle parent directories remain writable so a new
            # content-addressed digest can be published. Each digest directory
            # stays owner-readable and immutable.
            for bundle_dir in self.versions_dir.iterdir():
                if bundle_dir.is_symlink() or not bundle_dir.is_dir():
                    continue
                ensure_private_directory(bundle_dir)
                for version_dir in bundle_dir.iterdir():
                    if not version_dir.is_symlink() and version_dir.is_dir() and re.fullmatch(r"[a-f0-9]{64}", version_dir.name):
                        harden_readonly_tree(version_dir)
            self._hardened_root = root_identity
        # The first load performs the one-way legacy migration when needed.
        # A fresh exclusive read then repairs projections from the newest
        # canonical state, never from the possibly stale state returned above.
        self._lifecycle().load()
        self._repair_legacy_projections()
        self._initialized_root = root_identity

    def ensure_initialized(self) -> None:
        with self._mutation_lock:
            self._init_files()

    def repair_legacy_projections(
        self,
        *,
        strict: bool = True,
    ) -> LifecycleState:
        """Explicitly rebuild compatibility files from canonical state."""

        if type(strict) is not bool:
            raise TypeError("strict 必须是 bool")
        with self._mutation_lock:
            self._init_files()
            return self._repair_legacy_projections(strict=strict)

    def read_lifecycle_state(self) -> LifecycleState:
        with self._mutation_lock:
            self._init_files()
            return self._lifecycle().load()

    def watched_paths(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> list[Path]:
        state = self.read_lifecycle_state() if generated_state is None else self._require_generated_state(generated_state)
        paths = [self._lifecycle().state_file]
        for bundle_id, digest in state.active.items():
            bundle = self.version_path(bundle_id, digest)
            paths.extend(bundle / name for name in ("manifest.json", "tool.py", "tests.py"))
        return paths

    def read_active(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> dict[str, str]:
        state = self.read_lifecycle_state() if generated_state is None else self._require_generated_state(generated_state)
        return dict(state.active)

    @staticmethod
    def _permission_grants_from_state(
        state: LifecycleState,
    ) -> dict[str, dict[str, Any]]:
        return {key: grant.as_dict() for key, grant in state.permission_grants.items()}

    def _read_permission_grants(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> dict[str, dict[str, Any]]:
        state = self.read_lifecycle_state() if generated_state is None else self._require_generated_state(generated_state)
        return self._permission_grants_from_state(state)

    def read_permission_policy(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> dict[str, Any]:
        return {
            "version": _PERMISSION_POLICY_VERSION,
            "grants": deepcopy(self._read_permission_grants(generated_state=generated_state)),
        }

    @staticmethod
    def _permission_key(bundle_id: str, digest: str, tool_name: str) -> str:
        if (
            not _IDENTIFIER.fullmatch(bundle_id)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or not _IDENTIFIER.fullmatch(tool_name)
        ):
            raise ValueError("权限策略目标标识非法")
        return f"{bundle_id}:{digest}:{tool_name}"

    @classmethod
    def _effective_permission(
        cls,
        *,
        bundle_id: str,
        digest: str,
        tool: dict[str, Any],
        grants: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any] | None]:
        requested = str(tool.get("permission") or "superuser")
        key = cls._permission_key(bundle_id, digest, str(tool.get("name") or ""))
        grant = grants.get(key)
        if requested == "user" and grant is not None and grant.get("approved") is True:
            return "user", grant
        return "superuser", None

    def describe_permissions(
        self,
        validation: BundleValidation,
        *,
        grants: dict[str, dict[str, Any]] | None = None,
        generated_state: LifecycleState | None = None,
    ) -> list[dict[str, Any]]:
        if grants is not None and generated_state is not None:
            raise ValueError("grants 与 generated_state 不能同时指定")
        current_grants = self._read_permission_grants(generated_state=generated_state) if grants is None else grants
        bundle_id = str(validation.manifest["bundle_id"])
        result = []
        for tool in validation.manifest["tools"]:
            tool_policy = validation.tool_policies[tool["handler"]]
            effective, grant = self._effective_permission(
                bundle_id=bundle_id,
                digest=validation.digest,
                tool=tool,
                grants=current_grants,
            )
            result.append(
                {
                    "name": tool["name"],
                    "requested_permission": tool["permission"],
                    "effective_permission": effective,
                    "user_policy_approved": grant is not None,
                    "approved_by": grant.get("approved_by") if grant else None,
                    "approved_at": grant.get("approved_at") if grant else None,
                    "requested_capabilities": tool_policy.requested.as_dict(),
                    "detected_capabilities": tool_policy.detected.as_dict(),
                    "admin_capabilities": tool_policy.admin.as_dict(),
                    "effective_capabilities": tool_policy.effective.as_dict(),
                    "capability_policy": tool_policy.capability_contract(),
                }
            )
        return result

    def prepare_permission(
        self,
        bundle_id: str,
        digest: str,
        tool_name: str,
        *,
        allow_user: bool,
        approved_by: str,
        require_active: bool = False,
    ) -> PreparedLifecycleChange:
        if type(require_active) is not bool:
            raise TypeError("require_active 必须是 bool")
        actor = str(approved_by).strip()
        if not actor or len(actor) > 160:
            raise ValueError("权限批准人标识必须为 1 到 160 个字符")
        with self._mutation_lock:
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                if require_active and state.active.get(bundle_id) != digest:
                    raise ValueError("权限策略目标不是当前激活版本")
                version = self.version_path(bundle_id, digest)
                validation = self.validate_bundle(version)
                if validation.digest != digest or validation.manifest.get("bundle_id") != bundle_id:
                    raise ValueError("权限策略目标版本哈希不匹配")
                tool = next(
                    (item for item in validation.manifest["tools"] if item.get("name") == tool_name),
                    None,
                )
                if tool is None:
                    raise ValueError(f"权限策略目标工具不存在: {tool_name}")
                if allow_user and tool.get("permission") != "user":
                    raise ValueError("manifest 未请求 user 权限，不能放宽有效权限")
                plan = plan_permission(
                    state,
                    bundle_id,
                    digest,
                    tool_name,
                    allow_user=allow_user,
                    approved_by=actor if allow_user else None,
                    now=time.time(),
                )
                grants = self._permission_grants_from_state(plan.after_state)
                result = next(
                    item
                    for item in self.describe_permissions(
                        validation,
                        grants=grants,
                    )
                    if item["name"] == tool_name
                )
                return PreparedLifecycleChange(plan=plan, result=result)

    def replace_active(self, active: dict[str, str]) -> None:
        del active
        raise LifecycleTransitionError("replace_active 已停用；请使用 prepare/commit lifecycle API")

    def version_path(self, bundle_id: str, digest: str) -> Path:
        if not _IDENTIFIER.fullmatch(bundle_id) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("工具包标识或版本哈希非法")
        # Both components are strict identifiers, so a lexical child path is
        # sufficient.  Resolving here would hide a malicious symlink and make
        # later no-follow checks observe the symlink target instead.
        return self.versions_dir / bundle_id / digest

    def validate_bundle(self, path: Path) -> BundleValidation:
        path = Path(path)
        if path.is_symlink() or path.parent.is_symlink() or not path.is_dir():
            raise ValueError("工具包必须是非符号链接目录")
        manifest_path = path / "manifest.json"
        source_path = path / "tool.py"
        tests_path = path / "tests.py"
        for item in (manifest_path, source_path, tests_path):
            if item.is_symlink() or not item.is_file():
                raise ValueError(f"{item.name} 缺失或超过 64 KiB")
        manifest_bytes = manifest_path.read_bytes()
        source_bytes = source_path.read_bytes()
        tests_source_bytes = tests_path.read_bytes()
        for filename, content in (
            ("manifest.json", manifest_bytes),
            ("tool.py", source_bytes),
            ("tests.py", tests_source_bytes),
        ):
            if len(content) > 65_536:
                raise ValueError(f"{filename} 缺失或超过 64 KiB")
        try:
            manifest_source = manifest_bytes.decode("utf-8")
            source = source_bytes.decode("utf-8")
            tests_source = tests_source_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("工具包源码和 manifest 必须是 UTF-8") from error
        for pattern in _SECRET_PATTERNS:
            if pattern.search(source) or pattern.search(tests_source):
                raise ValueError("源码疑似包含凭据或私钥字面量")
        try:
            manifest = json.loads(manifest_source)
        except json.JSONDecodeError as error:
            raise ValueError(f"manifest.json 无效: {error}") from error
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json 顶层必须是对象")
        try:
            policy = ToolPolicy.generated(manifest.get("capabilities"))
        except ValueError as error:
            raise ValueError(f"manifest capabilities 非法: {error}") from error
        bundle_id = str(manifest.get("bundle_id") or "")
        if not _IDENTIFIER.fullmatch(bundle_id):
            raise ValueError("bundle_id 必须是安全标识符")
        description = manifest.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("工具包 description 不能为空")
        tools = manifest.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError("工具包 tools 必须是非空数组")
        try:
            tree = ast.parse(source, filename="tool.py")
            tests_tree = ast.parse(tests_source, filename="tests.py")
        except SyntaxError as error:
            raise ValueError(f"Python 语法错误: {error}") from error
        top_errors = _validate_top_level(tree) + _validate_top_level(tests_tree)
        if top_errors:
            raise ValueError("; ".join(top_errors[:5]))
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
        test_functions = {node.name for node in tests_tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
        if "run_tests" not in test_functions:
            raise ValueError("tests.py 必须定义 run_tests(tool_module)")
        names: set[str] = set()
        handlers: set[str] = set()
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("tools 数组元素必须是对象")
            name = str(tool.get("name") or "")
            if not _IDENTIFIER.fullmatch(name) or name in names:
                raise ValueError(f"工具名非法或重复: {name}")
            names.add(name)
            if not isinstance(tool.get("description"), str) or not tool["description"].strip():
                raise ValueError(f"工具 {name} description 不能为空")
            validate_parameters_schema(tool.get("parameters"))
            handler = str(tool.get("handler") or "")
            if handler not in functions:
                raise ValueError(f"工具 {name} handler 不存在: {handler}")
            handlers.add(handler)
            if tool.get("permission") not in {"user", "superuser"}:
                raise ValueError(f"工具 {name} permission 非法")
            if tool.get("effect") not in {"read_only", "mutating"}:
                raise ValueError(f"工具 {name} effect 非法")
            timeout = tool.get("timeout_seconds", 30)
            result_limit = tool.get("result_limit", 6000)
            if not isinstance(timeout, int | float) or not 0 < timeout <= 30:
                raise ValueError(f"工具 {name} timeout_seconds 必须在 0 到 30 秒")
            if not isinstance(result_limit, int) or not 0 < result_limit <= 6000:
                raise ValueError(f"工具 {name} result_limit 必须在 1 到 6000")
            dependencies = tool.get("dependencies", [])
            if not isinstance(dependencies, list) or not all(
                isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in dependencies
            ):
                raise ValueError(f"工具 {name} dependencies 必须是安全工具名数组")
            if len(set(dependencies)) != len(dependencies):
                raise ValueError(f"工具 {name} dependencies 不得重复")

        tool_ast_report = analyze_ast_policy(
            tree,
            source_type="generated",
            policy=policy,
            handler_names=handlers,
        )
        tests_ast_report = analyze_ast_policy(
            tests_tree,
            source_type="generated",
            policy=policy,
            handler_names=test_functions,
        )
        _reject_blocking_policy("tool.py", tool_ast_report)
        _reject_blocking_policy("tests.py", tests_ast_report)

        tests_detected = tests_ast_report.detected_capabilities
        tool_policies = MappingProxyType(
            {
                handler: policy.with_detected(
                    tool_ast_report.for_handler(handler)
                    .detected_capabilities.union(tests_detected)
                )
                for handler in sorted(handlers)
            }
        )
        policy = policy.with_detected(
            tool_ast_report.detected_capabilities.union(tests_detected)
        )

        digest = canonical_bundle_digest(
            manifest,
            source_bytes,
            tests_source_bytes,
        )
        risks = tuple(sorted(set(_risk_messages("tool.py", tool_ast_report) + _risk_messages("tests.py", tests_ast_report))))
        return BundleValidation(
            manifest=manifest,
            digest=digest,
            risks=risks,
            policy=policy,
            tool_policies=tool_policies,
            tool_ast_report=tool_ast_report,
            tests_ast_report=tests_ast_report,
            source=source_bytes,
            tests_source=tests_source_bytes,
        )

    def _commit_lifecycle_plan(
        self,
        plan: LifecyclePlan,
        *,
        publish: ImmutableVersionPublish | None = None,
    ) -> LifecycleState:
        lifecycle = self._lifecycle()
        try:
            return lifecycle._commit_plan_internal(plan, publish=publish)
        except LifecycleCommitUncertainError as error:
            # A visible after-image is not proof that a failed directory fsync
            # reached stable storage.  Only a post-fsync read uncertainty may be
            # resolved by comparing the complete canonical identity.
            if not error.durability_confirmed:
                raise
            # Never infer success from the exception's advisory flag.  Re-read
            # the canonical state and compare the complete revision/digest
            # identity with the plan boundaries.
            observed = lifecycle.load()
            if self._same_lifecycle_state(observed, plan.after_state):
                return observed
            if observed.revision == plan.expected_revision and observed.state_digest == plan.before_digest:
                raise
            raise LifecycleConflictError(
                "lifecycle 提交可见性不确定，且 canonical state " "既非精确 before 也非精确 after，已拒绝推断"
            ) from error

    def _commit_prepared_internal(
        self,
        change: PreparedLifecycleChange,
    ) -> LifecycleState:
        """Durable phase used exclusively by ``RuntimeReloader``.

        Callers must prebuild the exact after-state candidate before invoking
        this private method and publish that candidate immediately afterward.
        """

        if not isinstance(change, PreparedLifecycleChange):
            raise TypeError("change 必须是 PreparedLifecycleChange")
        with self._mutation_lock:
            self._init_files()
            committed = self._commit_lifecycle_plan(
                change.plan,
                publish=change.publish,
            )
            # Project only the newest state while holding the lifecycle's
            # exclusive lock.  An older process can therefore never overwrite
            # a newer process' compatibility projection.
            self._repair_legacy_projections()
            return committed

    def create_draft(
        self,
        manifest: dict[str, Any],
        source: str,
        tests_source: str,
        *,
        request: str,
        review: dict[str, Any],
    ) -> tuple[str, BundleValidation]:
        with self._mutation_lock:
            self._init_files()
            draft_id = uuid.uuid4().hex[:12]
            destination = self.drafts_dir / draft_id
            temporary = Path(tempfile.mkdtemp(prefix=".draft-", dir=self.drafts_dir))
            created_at = time.time()
            validation: BundleValidation | None = None
            try:
                _atomic_json(temporary / "manifest.json", manifest)
                (temporary / "tool.py").write_text(source, encoding="utf-8")
                (temporary / "tests.py").write_text(
                    tests_source,
                    encoding="utf-8",
                )
                ensure_private_file(temporary / "tool.py")
                ensure_private_file(temporary / "tests.py")
                validation = self.validate_bundle(temporary)
                _atomic_json(
                    temporary / "metadata.json",
                    {
                        "draft_id": draft_id,
                        "request": request,
                        "digest": validation.digest,
                        "risks": list(validation.risks),
                        "review": review,
                        # Initial persistence is always Draft.  Canonical
                        # transitions below are the only way to advance it.
                        "status": "draft",
                        "created_at": created_at,
                    },
                )
                harden_private_tree(temporary)
                os.replace(temporary, destination)
                state = self._lifecycle().load()
                record = DraftRecord(
                    draft_id=draft_id,
                    bundle_id=str(validation.manifest["bundle_id"]),
                    digest=validation.digest,
                    state=DraftState.DRAFT,
                    created_at=created_at,
                    updated_at=created_at,
                )
                plan = plan_record_draft(state, record)
                self._commit_lifecycle_plan(plan)
                self._repair_legacy_projections()
            except BaseException:
                # Remove only an orphan that canonical state definitely did
                # not adopt.  Ambiguous/corrupt reads fail closed and retain
                # evidence for later operator inspection.
                try:
                    observed = self._lifecycle().load()
                except Exception:
                    observed = None
                if observed is not None and draft_id not in observed.drafts:
                    shutil.rmtree(destination, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(temporary, ignore_errors=True)

            if validation is None:  # pragma: no cover - assignment invariant
                raise RuntimeError("草稿验证结果缺失")
            return draft_id, validation

    def _transition_draft_internal(
        self,
        draft_id: str,
        target: DraftState,
        *,
        producer: str | None = None,
        summary: str | None = None,
        risks: tuple[str, ...] = (),
    ) -> LifecycleState:
        """Commit one evidence-bound draft-only transition.

        Draft progression does not alter the loaded tool set. Active/runtime
        mutations use ``RuntimeReloader.apply_generated_change`` instead.
        """

        with self._mutation_lock:
            self._init_files()
            state = self._lifecycle().load()
            record = state.drafts.get(draft_id)
            if record is None:
                raise ValueError(f"草稿不存在: {draft_id}")
            if target is DraftState.STATIC_VALIDATED:
                _, _, validation = self._get_draft_from_state(
                    draft_id,
                    state,
                )
                producer = "generated-tool-bundle-validator"
                risks = tuple(validation.risks)
                summary = f"bundle validation passed; risks={len(risks)}"
            now = time.time()
            evidence = None
            if target not in {DraftState.AWAITING_APPROVAL}:
                if producer is None or summary is None:
                    raise LifecycleTransitionError(
                        f"推进到 {target.value} 缺少结构化 evidence"
                    )
                evidence = DraftEvidence(
                    state=target,
                    draft_digest=record.digest,
                    producer=producer,
                    outcome=(
                        "passed"
                        if target
                        in {
                            DraftState.STATIC_VALIDATED,
                            DraftState.SANDBOX_TESTED,
                            DraftState.MODEL_REVIEWED,
                        }
                        else "failed"
                    ),
                    summary=summary,
                    recorded_at=now,
                    risks=risks,
                )
            plan = plan_transition_draft(
                state,
                draft_id,
                target,
                now=now,
                evidence=evidence,
            )
            committed = self._commit_lifecycle_plan(plan)
            self._repair_legacy_projections()
            return committed

    def mark_static_validated(self, draft_id: str) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.STATIC_VALIDATED,
        )

    def mark_sandbox_tested(
        self,
        draft_id: str,
        test_summary: str,
    ) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.SANDBOX_TESTED,
            producer="generated-tool-sandbox",
            summary=test_summary,
        )

    def mark_model_reviewed(
        self,
        draft_id: str,
        *,
        summary: str,
        risks: tuple[str, ...] = (),
    ) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.MODEL_REVIEWED,
            producer="summary-model",
            summary=summary,
            risks=risks,
        )

    def mark_awaiting_approval(self, draft_id: str) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.AWAITING_APPROVAL,
        )

    def mark_validation_failed(
        self,
        draft_id: str,
        error: str,
    ) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.VALIDATION_FAILED,
            producer="generated-tool-bundle-validator",
            summary=error,
        )

    def mark_test_failed(
        self,
        draft_id: str,
        error: str,
    ) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.TEST_FAILED,
            producer="generated-tool-sandbox",
            summary=error,
        )

    def mark_review_failed(
        self,
        draft_id: str,
        *,
        summary: str,
        risks: tuple[str, ...] = (),
    ) -> LifecycleState:
        return self._transition_draft_internal(
            draft_id,
            DraftState.REVIEW_FAILED,
            producer="summary-model",
            summary=summary,
            risks=risks,
        )

    def _get_draft_from_state(
        self,
        draft_id: str,
        state: LifecycleState,
    ) -> tuple[Path, dict[str, Any], BundleValidation]:
        if not re.fullmatch(r"[a-f0-9]{12}", draft_id):
            raise ValueError("草稿 ID 非法")
        record = state.drafts.get(draft_id)
        if record is None:
            raise ValueError(f"草稿不存在: {draft_id}")
        path = self._draft_review_path(draft_id)
        try:
            metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"草稿 metadata.json 无效: {error}") from error
        if not isinstance(metadata, dict):
            raise ValueError("草稿 metadata.json 顶层必须是对象")
        validation = self.validate_bundle(path)
        if (
            metadata.get("draft_id") != draft_id
            or metadata.get("digest") != validation.digest
            or record.digest != validation.digest
            or record.bundle_id != validation.manifest.get("bundle_id")
        ):
            raise ValueError("草稿内容已变化，哈希校验失败")
        return (
            path,
            self._projected_metadata(metadata, record, state),
            validation,
        )

    def get_draft(
        self,
        draft_id: str,
        *,
        generated_state: LifecycleState | None = None,
    ) -> tuple[Path, dict[str, Any], BundleValidation]:
        with self._mutation_lock:
            if generated_state is not None:
                state = self._require_generated_state(generated_state)
                return self._get_draft_from_state(draft_id, state)
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                return self._get_draft_from_state(draft_id, state)

    @staticmethod
    def _require_review_files(path: Path, filenames: tuple[str, ...]) -> None:
        if path.is_symlink() or not path.is_dir():
            raise ValueError("草稿审阅拒绝符号链接或非目录工具包")
        for filename in filenames:
            item = path / filename
            if item.is_symlink() or not item.is_file():
                raise ValueError(f"草稿审阅拒绝符号链接或非普通文件：{filename}")

    def _draft_review_path(self, draft_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", draft_id):
            raise ValueError("草稿 ID 非法")
        drafts_root = self.drafts_dir.resolve()
        path = self.drafts_dir / draft_id
        self._require_review_files(
            path,
            ("metadata.json", "manifest.json", "tool.py", "tests.py"),
        )
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"草稿路径不可访问：{error}") from error
        if resolved.parent != drafts_root:
            raise ValueError("草稿路径越界")
        return path

    def _draft_review_snapshot_from_state(
        self,
        draft_id: str,
        state: LifecycleState,
    ) -> DraftReviewSnapshot:
        _, metadata, validation = self._get_draft_from_state(draft_id, state)
        record = state.drafts[draft_id]
        bundle_id = str(validation.manifest["bundle_id"])
        active_digest = state.active.get(bundle_id)
        review_stamp = _draft_review_stamp(
            draft_id=draft_id,
            digest=validation.digest,
            lifecycle_revision=state.revision,
            lifecycle_state_digest=state.state_digest,
            active_digest=active_digest,
        )
        try:
            source = validation.source.decode("utf-8")
            tests_source = validation.tests_source.decode("utf-8")
        except UnicodeDecodeError as error:  # validate_bundle already checks
            raise ValueError("草稿源码必须是 UTF-8") from error
        manifest = _canonical_review_json(validation.manifest)

        grants = self._permission_grants_from_state(state)
        permission_rows = self.describe_permissions(validation, grants=grants)
        tools = []
        for item in validation.manifest["tools"]:
            permission = next(row for row in permission_rows if row["name"] == item["name"])
            declared_effect = ToolEffect(item["effect"])
            tools.append(
                {
                    "name": item["name"],
                    "handler": item["handler"],
                    "requested_permission": permission["requested_permission"],
                    "effective_permission": permission["effective_permission"],
                    "declared_effect": declared_effect.value,
                    "effective_effect": validation.tool_ast_report.for_handler(item["handler"])
                    .effective_effect(declared_effect)
                    .value,
                }
            )
        risk_rows = list(validation.risks)
        capability_rows = {
            **validation.policy.capability_contract(),
            "tools": {
                item["name"]: validation.tool_policies[
                    item["handler"]
                ].detected.as_dict()
                for item in validation.manifest["tools"]
            },
        }
        # Canonical lifecycle fields are deliberately assigned last so
        # tampered legacy metadata can never override the review decision.
        summary = _canonical_review_json(
            {
                **metadata,
                "bundle_id": validation.manifest["bundle_id"],
                "capabilities": capability_rows,
                "digest": validation.digest,
                "draft_id": draft_id,
                "risks": risk_rows,
                "sections": list(_DRAFT_REVIEW_SECTIONS),
                "tools": tools,
                "status": _CANONICAL_TO_LEGACY_DRAFT_STATUS[record.state],
                "canonical_state": record.state.value,
                "lifecycle_evidence": [
                    item.as_dict() for item in record.evidence
                ],
                "lifecycle_revision": state.revision,
                "lifecycle_state_digest": state.state_digest,
                "active_digest": active_digest,
                "review_stamp": review_stamp,
            }
        )
        risks = _canonical_review_json(risk_rows)
        capabilities = _canonical_review_json(capability_rows)

        old_manifest = ""
        old_source = ""
        old_tests = ""
        if active_digest is None:
            old_labels = dict.fromkeys(("manifest.json", "tool.py", "tests.py"), "/dev/null")
        else:
            logical_active_path = self.versions_dir / bundle_id / active_digest
            self._require_review_files(
                logical_active_path,
                ("manifest.json", "tool.py", "tests.py"),
            )
            active_path = self.version_path(bundle_id, active_digest)
            active_validation = self.validate_bundle(active_path)
            if active_validation.digest != active_digest or active_validation.manifest.get("bundle_id") != bundle_id:
                raise ValueError("当前激活工具版本哈希校验失败")
            old_manifest = _canonical_review_json(active_validation.manifest)
            old_source = active_validation.source.decode("utf-8")
            old_tests = active_validation.tests_source.decode("utf-8")
            old_labels = {name: f"{bundle_id}@{active_digest}/{name}" for name in ("manifest.json", "tool.py", "tests.py")}

        new_prefix = f"draft:{draft_id}@{validation.digest}"
        diff = "\n\n".join(
            (
                _review_file_diff(
                    filename="manifest.json",
                    old_content=old_manifest,
                    new_content=manifest,
                    old_label=old_labels["manifest.json"],
                    new_label=f"{new_prefix}/manifest.json",
                ),
                _review_file_diff(
                    filename="tool.py",
                    old_content=old_source,
                    new_content=source,
                    old_label=old_labels["tool.py"],
                    new_label=f"{new_prefix}/tool.py",
                ),
                _review_file_diff(
                    filename="tests.py",
                    old_content=old_tests,
                    new_content=tests_source,
                    old_label=old_labels["tests.py"],
                    new_label=f"{new_prefix}/tests.py",
                ),
            )
        )
        return DraftReviewSnapshot(
            draft_id=draft_id,
            digest=validation.digest,
            lifecycle_revision=state.revision,
            lifecycle_state_digest=state.state_digest,
            active_digest=active_digest,
            review_stamp=review_stamp,
            sections=(
                ("summary", summary),
                ("manifest", manifest),
                ("source", source),
                ("tests", tests_source),
                ("risks", risks),
                ("capabilities", capabilities),
                ("diff", diff),
            ),
        )

    def get_draft_review_snapshot(
        self,
        draft_id: str,
        *,
        generated_state: LifecycleState | None = None,
    ) -> DraftReviewSnapshot:
        """Read every section from one immutable canonical state snapshot."""

        with self._mutation_lock:
            if generated_state is not None:
                state = self._require_generated_state(generated_state)
                return self._draft_review_snapshot_from_state(draft_id, state)
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                return self._draft_review_snapshot_from_state(draft_id, state)

    def get_draft_review_page(
        self,
        draft_id: str,
        section: str = "summary",
        page: int = 1,
        *,
        generated_state: LifecycleState | None = None,
    ) -> DraftReviewPage:
        return self.get_draft_review_snapshot(
            draft_id,
            generated_state=generated_state,
        ).get_page(section, page)

    def prepare_approval(
        self,
        draft_id: str,
        short_hash: str,
        review_stamp: str,
    ) -> PreparedLifecycleChange:
        with self._mutation_lock:
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                path, _, validation = self._get_draft_from_state(
                    draft_id,
                    state,
                )
                record = state.drafts[draft_id]
                if record.state is not DraftState.AWAITING_APPROVAL:
                    raise ValueError("草稿当前状态不可批准")
                if not isinstance(short_hash, str) or len(short_hash) < 8 or not validation.digest.startswith(short_hash.lower()):
                    raise ValueError("确认哈希不匹配")
                bundle_id = str(validation.manifest["bundle_id"])
                if not isinstance(review_stamp, str) or not re.fullmatch(
                    r"[a-f0-9]{64}",
                    review_stamp,
                ):
                    raise ValueError("review stamp 必须是完整 64 位十六进制值")
                expected_stamp = _draft_review_stamp(
                    draft_id=draft_id,
                    digest=validation.digest,
                    lifecycle_revision=state.revision,
                    lifecycle_state_digest=state.state_digest,
                    active_digest=state.active.get(bundle_id),
                )
                if not secrets.compare_digest(review_stamp, expected_stamp):
                    raise LifecycleConflictError(
                        "review stamp 已过期；草稿或 lifecycle/active 状态在审阅后发生变化，请重新查看完整草稿"
                    )
                plan = plan_activate_from_draft(
                    state,
                    draft_id,
                    now=time.time(),
                    expected_digest=validation.digest,
                )
                publish = ImmutableVersionPublish(
                    source_directory=path,
                    bundle_id=bundle_id,
                    digest=validation.digest,
                )
                return PreparedLifecycleChange(
                    plan=plan,
                    result=(bundle_id, validation.digest),
                    publish=publish,
                    generated_source_overrides={
                        (bundle_id, validation.digest): path,
                    },
                )

    def restore_draft_metadata(self, draft_id: str, metadata: dict[str, Any]) -> None:
        """Restore non-authoritative metadata without changing lifecycle state."""

        if not isinstance(metadata, dict):
            raise TypeError("metadata 必须是字典")
        with self._mutation_lock:
            self._init_files()
            with self._lifecycle().latest_exclusive_snapshot() as state:
                path, _, validation = self._get_draft_from_state(
                    draft_id,
                    state,
                )
                restored = deepcopy(metadata)
                if restored.get("draft_id") != draft_id or restored.get("digest") != validation.digest:
                    raise ValueError("不能恢复不匹配的草稿元数据")
                restored = self._projected_metadata(
                    restored,
                    state.drafts[draft_id],
                    state,
                )
                self._write_projection(path / "metadata.json", restored)

    def draft_diff(
        self,
        draft_id: str,
        *,
        limit: int = 4000,
        generated_state: LifecycleState | None = None,
    ) -> str:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("diff limit 必须是正整数")
        diff = self.get_draft_review_snapshot(
            draft_id,
            generated_state=generated_state,
        ).section_content("diff")
        if len(diff) > limit:
            return diff[:limit] + "\n...[diff 已截断]"
        return diff

    def prepare_rejection(
        self,
        draft_id: str,
        *,
        actor: str,
        reason: str,
    ) -> PreparedLifecycleChange:
        with self._mutation_lock:
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                # Validate both canonical membership and immutable draft bytes
                # before preparing the transition.
                self._get_draft_from_state(draft_id, state)
                record = state.drafts[draft_id]
                now = time.time()
                evidence = DraftEvidence(
                    state=DraftState.REJECTED,
                    draft_digest=record.digest,
                    producer=actor,
                    outcome="rejected",
                    summary=reason,
                    recorded_at=now,
                )
                plan = plan_reject(
                    state,
                    draft_id,
                    now=now,
                    evidence=evidence,
                )
                return PreparedLifecycleChange(plan=plan, result=None)

    def prepare_deactivation(
        self,
        bundle_id: str,
    ) -> PreparedLifecycleChange:
        with self._mutation_lock:
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                plan = plan_deactivate(state, bundle_id, now=time.time())
                return PreparedLifecycleChange(
                    plan=plan,
                    result=not plan.no_op,
                )

    def prepare_rollback(
        self,
        bundle_id: str,
        digest_or_prefix: str,
    ) -> PreparedLifecycleChange:
        if not isinstance(digest_or_prefix, str) or len(digest_or_prefix) < 8:
            raise ValueError("回滚版本前缀至少需要 8 位")
        with self._mutation_lock:
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                prefix = digest_or_prefix.lower()
                matches = [
                    digest
                    for digest, record in state.versions.get(
                        bundle_id,
                        {},
                    ).items()
                    if record.state is not VersionState.ARCHIVED and digest.startswith(prefix)
                ]
                if len(matches) != 1:
                    raise ValueError("回滚版本不存在或前缀不唯一")
                digest = matches[0]
                version_path = self._lifecycle()._verify_immutable_version_locked(
                    bundle_id,
                    digest,
                )
                validation = self.validate_bundle(version_path)
                if validation.digest != digest or validation.manifest.get("bundle_id") != bundle_id:
                    raise ValueError("回滚版本内容哈希不匹配")
                plan = plan_rollback(
                    state,
                    bundle_id,
                    digest,
                    now=time.time(),
                )
                return PreparedLifecycleChange(plan=plan, result=digest)

    def _list_status_from_state(self, state: LifecycleState) -> dict[str, Any]:
        grants = self._permission_grants_from_state(state)
        active = dict(state.active)
        active_tools = []
        for bundle_id, digest in active.items():
            try:
                validation = self.validate_bundle(self.version_path(bundle_id, digest))
                if validation.digest != digest or validation.manifest.get("bundle_id") != bundle_id:
                    raise ValueError("当前激活工具版本哈希不匹配")
                active_tools.append(
                    {
                        "bundle_id": bundle_id,
                        "digest": digest,
                        "tools": self.describe_permissions(
                            validation,
                            grants=grants,
                        ),
                    }
                )
            except (OSError, ValueError, TypeError, KeyError) as error:
                active_tools.append(
                    {
                        "bundle_id": bundle_id,
                        "digest": digest,
                        "tools": [],
                        "error": str(error)[:300],
                    }
                )
        drafts = []
        for draft_id, record in sorted(state.drafts.items()):
            try:
                _, metadata, validation = self._get_draft_from_state(
                    draft_id,
                    state,
                )
                item = {key: metadata.get(key) for key in ("draft_id", "digest", "status", "request")}
                item["bundle_id"] = validation.manifest["bundle_id"]
                item["canonical_state"] = record.state.value
                item["tools"] = self.describe_permissions(
                    validation,
                    grants=grants,
                )
                drafts.append(item)
            except (OSError, ValueError, TypeError, KeyError) as error:
                drafts.append(
                    {
                        "draft_id": draft_id,
                        "digest": record.digest,
                        "status": _CANONICAL_TO_LEGACY_DRAFT_STATUS[record.state],
                        "canonical_state": record.state.value,
                        "bundle_id": record.bundle_id,
                        "tools": [],
                        "error": str(error)[:300],
                    }
                )
        return {
            "active": active,
            "active_tools": active_tools,
            "drafts": drafts,
            "lifecycle_revision": state.revision,
            "lifecycle_state_digest": state.state_digest,
            "legacy_projection_stale": self._projection_stale,
            "legacy_projection_error": self._projection_error,
        }

    def list_status(
        self,
        *,
        generated_state: LifecycleState | None = None,
    ) -> dict[str, Any]:
        with self._mutation_lock:
            if generated_state is not None:
                state = self._require_generated_state(generated_state)
                return self._list_status_from_state(state)
            self._init_files()
            with self._lifecycle().read_snapshot() as state:
                return self._list_status_from_state(state)

    @staticmethod
    def _normalize_source_overrides(
        source_overrides: Mapping[tuple[str, str], Path] | None,
    ) -> Mapping[tuple[str, str], Path]:
        if source_overrides is None:
            return MappingProxyType({})
        if not isinstance(source_overrides, Mapping):
            raise TypeError("source_overrides 必须是映射或 None")
        normalized: dict[tuple[str, str], Path] = {}
        for key, value in source_overrides.items():
            if not isinstance(key, tuple) or len(key) != 2 or not all(isinstance(item, str) for item in key):
                raise TypeError("source override key 必须是 (bundle_id, digest)")
            normalized[key] = Path(value)
        return MappingProxyType(normalized)

    def load_active_tools(
        self,
        *,
        generation: int = 0,
        generated_state: LifecycleState | None = None,
        generated_source_overrides: Mapping[tuple[str, str], Path] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("generation 必须是非负整数")
        if generated_state is None:
            with self._mutation_lock:
                self._init_files()
                with self._lifecycle().read_snapshot() as state:
                    return self.load_active_tools(
                        generation=generation,
                        generated_state=state,
                        generated_source_overrides=generated_source_overrides,
                    )
        state = self._require_generated_state(generated_state)
        overrides = self._normalize_source_overrides(generated_source_overrides)
        schemas: dict[str, dict[str, Any]] = {}
        names: set[str] = set()
        dependencies: dict[str, set[str]] = {}
        grants = self._permission_grants_from_state(state)
        for bundle_id, digest in state.active.items():
            path = overrides.get(
                (bundle_id, digest),
                self.version_path(bundle_id, digest),
            )
            self._require_review_files(
                path,
                ("manifest.json", "tool.py", "tests.py"),
            )
            validation = self.validate_bundle(path)
            if validation.digest != digest or validation.manifest.get("bundle_id") != bundle_id:
                raise ValueError(f"工具包 {bundle_id} 内容哈希不匹配")
            for item in validation.manifest["tools"]:
                name = item["name"]
                if name in names:
                    raise ValueError(f"生成工具重名: {name}")
                names.add(name)
                handler_name = item["handler"]
                declared_effect = ToolEffect(item["effect"])
                effective_effect = validation.tool_ast_report.for_handler(handler_name).effective_effect(declared_effect)
                artifact_holder: dict[str, ToolArtifact] = {}

                async def handler(
                    _tool_context: ToolContext | None = None,
                    _artifact_holder=artifact_holder,
                    _tool_name=name,
                    _bundle_digest=digest,
                    _generation=generation,
                    **kwargs,
                ):
                    context = {}
                    caller_permission = "user"
                    if _tool_context is not None:
                        event = _tool_context.event
                        superusers = {
                            str(user_id)
                            for user_id in getattr(
                                getattr(_tool_context.bot, "config", None),
                                "superusers",
                                set(),
                            )
                        }
                        if str(getattr(event, "user_id", "")) in superusers:
                            caller_permission = "superuser"
                        context = {
                            "request_id": _tool_context.request_id,
                            "confirmed": _tool_context.confirmed,
                            "user_id": str(getattr(event, "user_id", "")),
                            "group_id": str(getattr(event, "group_id", "")),
                            "message_type": getattr(event, "message_type", ""),
                        }
                    started = time.monotonic()
                    result_kind = "failure"
                    try:
                        artifact = _artifact_holder["artifact"]
                        result = await generated_tool_runner.execute_artifact(
                            artifact,
                            kwargs,
                            context,
                            expected_artifact_digest=artifact.artifact_digest,
                            expected_bundle_digest=_bundle_digest,
                            generation=_generation,
                        )
                        result_kind = "success"
                        return result
                    except asyncio.CancelledError:
                        result_kind = "cancelled"
                        raise
                    finally:
                        logger.info(
                            f"generated_tool_audit tool={_tool_name} "
                            f"version={_bundle_digest[:12]} "
                            f"caller_permission={caller_permission} "
                            f"elapsed={time.monotonic() - started:.3f}s "
                            f"result={result_kind}"
                        )

                policy = validation.tool_policies[handler_name]
                spec = ToolSpec(
                    name=name,
                    description=item["description"],
                    parameters=deepcopy(item["parameters"]),
                    handler=handler,
                    effect=effective_effect,
                    permission=self._effective_permission(
                        bundle_id=bundle_id,
                        digest=digest,
                        tool=item,
                        grants=grants,
                    )[0],
                    timeout_seconds=float(item.get("timeout_seconds", 30)),
                    result_limit=int(item.get("result_limit", 6000)),
                    dependencies=tuple(item.get("dependencies") or ()),
                    policy=policy,
                )
                contract = ToolContractSnapshot.from_spec(
                    spec,
                    requested_permission=item["permission"],
                    declared_effect=declared_effect,
                )
                artifact = ToolArtifact(
                    tool_name=name,
                    handler_name=handler_name,
                    source=validation.source,
                    source_hash=source_sha256(validation.source),
                    schema={
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                    spec=spec,
                    contract=contract,
                    source_type="generated",
                    generation=generation,
                    filename="tool.py",
                    tests_source=validation.tests_source,
                    bundle_manifest=validation.manifest,
                    bundle_id=bundle_id,
                    bundle_digest=digest,
                )
                artifact_holder["artifact"] = artifact
                schema = spec.as_legacy_schema()
                schema["source"] = "generated"
                schema["bundle_id"] = bundle_id
                schema["bundle_digest"] = digest
                schema["requested_permission"] = item["permission"]
                schema["effective_permission"] = spec.permission
                schema["declared_effect"] = declared_effect.value
                schema["effective_effect"] = spec.effect.value
                schema["user_policy_approved"] = spec.permission == "user"
                schema["tool_contract_version"] = contract.contract_version
                schema["artifact_digest_version"] = artifact.artifact_version
                schema["requested_capabilities"] = policy.requested.as_dict()
                schema["detected_capabilities"] = policy.detected.as_dict()
                schema["admin_capabilities"] = policy.admin.as_dict()
                schema["effective_capabilities"] = policy.effective.as_dict()
                schema["capability_policy"] = policy.capability_contract()
                schema["tool_artifact"] = artifact
                schema["artifact_digest"] = artifact.artifact_digest
                schema["generation"] = generation
                schemas[name] = schema
                if spec.dependencies:
                    dependencies[name] = set(spec.dependencies)
        return schemas, dependencies


generated_tool_store = GeneratedToolStore()

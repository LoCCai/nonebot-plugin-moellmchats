from __future__ import annotations

import asyncio
import base64
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import secrets
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .generated_tool_lifecycle import (
    DraftState,
    LifecycleState,
    VersionState,
    draft_review_stamp,
)
from .runtime_api import (
    _MAX_JSON_INTEGER,
    RUNTIME_API_VERSION,
    RuntimeApiAuthenticator,
    RuntimeApiConfigurationError,
    RuntimeApiPrincipal,
    RuntimeApiRequest,
    RuntimeApiResponse,
    RuntimeSnapshotReader,
    _error_response,
    _validated_snapshot,
)
from .tool_providers import DiscoveredTool, ProviderCatalogSnapshot

if TYPE_CHECKING:
    from .runtime_snapshot import RuntimeSnapshot

TOOL_BUNDLE_API_READ_SCOPE = "tools:read"
TOOL_BUNDLE_API_WRITE_SCOPE = "tools:write"

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_OPERATION_ID_RE = _DIGEST_RE
_CURSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
_TOOLS_DETAIL_PATH_RE = re.compile(r"^/tools/([A-Za-z0-9_-]{1,64})$")
_DRAFT_APPROVE_PATH_RE = re.compile(r"^/tool-drafts/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/approve$")
_BUNDLE_ACTIVATE_PATH_RE = re.compile(r"^/tool-bundles/([A-Za-z][A-Za-z0-9_-]{0,63})/activate$")
_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 20
_MUTATION_BODY_MAX_BYTES = 4_096
_CURSOR_VERSION = 1


class ToolBundleApiError(RuntimeError):
    """Base error for the detached H-02 Tool Bundle API."""


class ToolBundleMutationError(ToolBundleApiError):
    """Base error returned by an explicitly injected mutation port."""


class ToolBundleMutationNotFoundError(ToolBundleMutationError):
    """The mutation target did not exist at the required CAS identity."""


class ToolBundleMutationConflictError(ToolBundleMutationError):
    """The mutation precondition or lifecycle transition no longer matches."""


class ToolBundleMutationUnavailableError(ToolBundleMutationError):
    """The mutation was not started because its dependency was unavailable."""


class ToolBundleMutationResultUnknownError(ToolBundleMutationError):
    """The caller must inspect state and must not automatically replay."""


def _require_generation(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > _MAX_JSON_INTEGER:
        raise RuntimeApiConfigurationError(f"{label} 必须是正整数")
    return value


def _require_revision(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_JSON_INTEGER:
        raise RuntimeApiConfigurationError(f"{label} 必须是非负整数")
    return value


def _require_identifier(
    value: object,
    *,
    pattern: re.Pattern[str],
    label: str,
) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RuntimeApiConfigurationError(f"{label} 非法")
    return value


@dataclass(frozen=True)
class ApproveToolDraftCommand:
    actor_subject: str
    draft_id: str
    bundle_id: str
    digest: str
    expected_generation: int
    expected_lifecycle_revision: int
    expected_lifecycle_state_digest: str
    review_stamp: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_subject, str) or not self.actor_subject or len(self.actor_subject) > 128:
            raise RuntimeApiConfigurationError("approve actor subject 非法")
        _require_identifier(self.draft_id, pattern=_DRAFT_ID_RE, label="approve draft_id")
        _require_identifier(self.bundle_id, pattern=_BUNDLE_ID_RE, label="approve bundle_id")
        _require_identifier(self.digest, pattern=_DIGEST_RE, label="approve digest")
        _require_generation(self.expected_generation, label="approve expected_generation")
        _require_revision(
            self.expected_lifecycle_revision,
            label="approve expected_lifecycle_revision",
        )
        _require_identifier(
            self.expected_lifecycle_state_digest,
            pattern=_DIGEST_RE,
            label="approve expected_lifecycle_state_digest",
        )
        _require_identifier(
            self.review_stamp,
            pattern=_DIGEST_RE,
            label="approve review_stamp",
        )


@dataclass(frozen=True)
class ActivateToolBundleCommand:
    actor_subject: str
    bundle_id: str
    digest: str
    expected_generation: int
    expected_lifecycle_revision: int
    expected_lifecycle_state_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_subject, str) or not self.actor_subject or len(self.actor_subject) > 128:
            raise RuntimeApiConfigurationError("activate actor subject 非法")
        _require_identifier(self.bundle_id, pattern=_BUNDLE_ID_RE, label="activate bundle_id")
        _require_identifier(self.digest, pattern=_DIGEST_RE, label="activate digest")
        _require_generation(self.expected_generation, label="activate expected_generation")
        _require_revision(
            self.expected_lifecycle_revision,
            label="activate expected_lifecycle_revision",
        )
        _require_identifier(
            self.expected_lifecycle_state_digest,
            pattern=_DIGEST_RE,
            label="activate expected_lifecycle_state_digest",
        )


@dataclass(frozen=True)
class ToolBundleMutationResult:
    operation: str
    operation_id: str
    generation: int
    lifecycle_revision: int
    lifecycle_state_digest: str
    bundle_id: str
    digest: str
    draft_id: str | None
    active_digest: str | None
    audit_recorded: bool

    def __post_init__(self) -> None:
        if self.operation not in {"approve_draft", "activate_bundle"}:
            raise RuntimeApiConfigurationError("mutation result operation 非法")
        _require_identifier(
            self.operation_id,
            pattern=_OPERATION_ID_RE,
            label="mutation result operation_id",
        )
        _require_generation(self.generation, label="mutation result generation")
        _require_revision(
            self.lifecycle_revision,
            label="mutation result lifecycle_revision",
        )
        _require_identifier(
            self.lifecycle_state_digest,
            pattern=_DIGEST_RE,
            label="mutation result lifecycle_state_digest",
        )
        _require_identifier(self.bundle_id, pattern=_BUNDLE_ID_RE, label="mutation result bundle_id")
        _require_identifier(self.digest, pattern=_DIGEST_RE, label="mutation result digest")
        if self.draft_id is not None:
            _require_identifier(self.draft_id, pattern=_DRAFT_ID_RE, label="mutation result draft_id")
        if self.active_digest is not None:
            _require_identifier(
                self.active_digest,
                pattern=_DIGEST_RE,
                label="mutation result active_digest",
            )
        if self.audit_recorded is not True:
            raise RuntimeApiConfigurationError("危险 Tool Bundle mutation 必须同步确认即时审计已记录")


@runtime_checkable
class ToolLifecycleStateReader(Protocol):
    async def read_current(self) -> LifecycleState: ...


@runtime_checkable
class ToolBundleMutationPort(Protocol):
    """CAS-bound mutation boundary.

    Implementations must authenticate no authority of their own from request
    data, enforce every expected runtime/lifecycle identity, settle durable
    finalization under cancellation, synchronously record the critical audit
    event, and never replay an unknown result.
    """

    async def approve_draft(
        self,
        command: ApproveToolDraftCommand,
    ) -> ToolBundleMutationResult: ...

    async def activate_bundle(
        self,
        command: ActivateToolBundleCommand,
    ) -> ToolBundleMutationResult: ...


@dataclass(frozen=True)
class ToolBundleApiEndpoint:
    method: str
    path_template: str
    required_scope: str

    def __post_init__(self) -> None:
        allowed = {
            ("GET", "/tools"),
            ("GET", "/tools/{name}"),
            ("GET", "/tool-bundles"),
            ("GET", "/tool-drafts"),
            ("POST", "/tool-drafts/{id}/approve"),
            ("POST", "/tool-bundles/{id}/activate"),
        }
        if (self.method, self.path_template) not in allowed:
            raise RuntimeApiConfigurationError("H-02 endpoint contract 非法")
        expected_scope = TOOL_BUNDLE_API_READ_SCOPE if self.method == "GET" else TOOL_BUNDLE_API_WRITE_SCOPE
        if self.required_scope != expected_scope:
            raise RuntimeApiConfigurationError("H-02 endpoint scope 非法")


_ENDPOINTS = (
    ToolBundleApiEndpoint("GET", "/tools", TOOL_BUNDLE_API_READ_SCOPE),
    ToolBundleApiEndpoint("GET", "/tools/{name}", TOOL_BUNDLE_API_READ_SCOPE),
    ToolBundleApiEndpoint("GET", "/tool-bundles", TOOL_BUNDLE_API_READ_SCOPE),
    ToolBundleApiEndpoint("GET", "/tool-drafts", TOOL_BUNDLE_API_READ_SCOPE),
    ToolBundleApiEndpoint(
        "POST",
        "/tool-drafts/{id}/approve",
        TOOL_BUNDLE_API_WRITE_SCOPE,
    ),
    ToolBundleApiEndpoint(
        "POST",
        "/tool-bundles/{id}/activate",
        TOOL_BUNDLE_API_WRITE_SCOPE,
    ),
)


@dataclass(frozen=True)
class _ResolvedEndpoint:
    endpoint: ToolBundleApiEndpoint
    target: str | None = None


def _resolve_endpoint(path: str) -> _ResolvedEndpoint | None:
    if path == "/tools":
        return _ResolvedEndpoint(_ENDPOINTS[0])
    match = _TOOLS_DETAIL_PATH_RE.fullmatch(path)
    if match is not None:
        return _ResolvedEndpoint(_ENDPOINTS[1], match.group(1))
    if path == "/tool-bundles":
        return _ResolvedEndpoint(_ENDPOINTS[2])
    if path == "/tool-drafts":
        return _ResolvedEndpoint(_ENDPOINTS[3])
    match = _DRAFT_APPROVE_PATH_RE.fullmatch(path)
    if match is not None:
        return _ResolvedEndpoint(_ENDPOINTS[4], match.group(1))
    match = _BUNDLE_ACTIVATE_PATH_RE.fullmatch(path)
    if match is not None:
        return _ResolvedEndpoint(_ENDPOINTS[5], match.group(1))
    return None


@dataclass(frozen=True)
class _PageCursor:
    kind: str
    generation: int
    anchor: tuple[str, ...]
    lifecycle_revision: int | None = None
    lifecycle_state_digest: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"tools", "tool_bundles", "tool_drafts"}:
            raise ValueError("cursor kind 非法")
        _require_generation(self.generation, label="cursor generation")
        expected_anchor_size = 2 if self.kind == "tool_bundles" else 1
        if not isinstance(self.anchor, tuple) or len(self.anchor) != expected_anchor_size:
            raise ValueError("cursor anchor 非法")
        if self.kind == "tools":
            _require_identifier(self.anchor[0], pattern=_TOOL_NAME_RE, label="cursor tool anchor")
        elif self.kind == "tool_drafts":
            _require_identifier(self.anchor[0], pattern=_DRAFT_ID_RE, label="cursor draft anchor")
        else:
            _require_identifier(self.anchor[0], pattern=_BUNDLE_ID_RE, label="cursor bundle anchor")
            _require_identifier(self.anchor[1], pattern=_DIGEST_RE, label="cursor digest anchor")
        if self.kind == "tools":
            if self.lifecycle_revision is not None or self.lifecycle_state_digest is not None:
                raise ValueError("tools cursor 不得携带 lifecycle identity")
        else:
            _require_revision(self.lifecycle_revision, label="cursor lifecycle_revision")
            _require_identifier(
                self.lifecycle_state_digest,
                pattern=_DIGEST_RE,
                label="cursor lifecycle_state_digest",
            )

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "anchor": list(self.anchor),
            "generation": self.generation,
            "kind": self.kind,
            "version": _CURSOR_VERSION,
        }
        if self.lifecycle_revision is not None:
            value["lifecycle_revision"] = self.lifecycle_revision
            value["lifecycle_state_digest"] = self.lifecycle_state_digest
        return value


def _encode_cursor(cursor: _PageCursor) -> str:
    content = json.dumps(
        cursor.as_dict(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object 字段重复")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("JSON 常量非法")


def _decode_cursor(token: str) -> _PageCursor:
    if not _CURSOR_TOKEN_RE.fullmatch(token):
        raise ValueError("cursor token 非法")
    padding = "=" * (-len(token) % 4)
    try:
        content = base64.b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(
            content,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("cursor payload 非法") from None
    if not isinstance(value, dict):
        raise ValueError("cursor payload 必须是 object")
    kind = value.get("kind")
    generation = value.get("generation")
    if not isinstance(kind, str) or not isinstance(generation, int):
        raise ValueError("cursor payload identity 非法")
    expected_keys = (
        {"anchor", "generation", "kind", "version"}
        if kind == "tools"
        else {
            "anchor",
            "generation",
            "kind",
            "lifecycle_revision",
            "lifecycle_state_digest",
            "version",
        }
    )
    if set(value) != expected_keys or value.get("version") != _CURSOR_VERSION:
        raise ValueError("cursor payload contract 非法")
    anchor = value.get("anchor")
    if not isinstance(anchor, list) or not all(isinstance(item, str) for item in anchor):
        raise ValueError("cursor anchor 非法")
    try:
        cursor = _PageCursor(
            kind=kind,
            generation=generation,
            anchor=tuple(anchor),
            lifecycle_revision=value.get("lifecycle_revision"),
            lifecycle_state_digest=value.get("lifecycle_state_digest"),
        )
    except (RuntimeApiConfigurationError, ValueError, TypeError):
        raise ValueError("cursor payload identity 非法") from None
    if _encode_cursor(cursor) != token:
        raise ValueError("cursor token 必须是 canonical base64url")
    return cursor


@dataclass(frozen=True)
class _PageQuery:
    limit: int = _PAGE_SIZE_DEFAULT
    cursor: _PageCursor | None = None


def _parse_page_query(query_string: bytes) -> _PageQuery:
    if not query_string:
        return _PageQuery()
    try:
        query = query_string.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("query 必须是 ASCII") from None
    parts = query.split("&")
    if not 1 <= len(parts) <= 2:
        raise ValueError("query 字段数非法")
    values: dict[str, str] = {}
    for part in parts:
        if part.count("=") != 1:
            raise ValueError("query 字段格式非法")
        key, value = part.split("=", 1)
        if key not in {"cursor", "limit"} or key in values or not value:
            raise ValueError("query 字段非法或重复")
        values[key] = value
    limit_text = values.get("limit")
    limit = _PAGE_SIZE_DEFAULT
    if limit_text is not None:
        if not re.fullmatch(r"[1-9][0-9]?", limit_text):
            raise ValueError("limit 非法")
        limit = int(limit_text)
        if limit > _PAGE_SIZE_MAX:
            raise ValueError("limit 超过安全上限")
    cursor_text = values.get("cursor")
    return _PageQuery(
        limit=limit,
        cursor=None if cursor_text is None else _decode_cursor(cursor_text),
    )


def _validate_cursor_identity(
    cursor: _PageCursor | None,
    *,
    kind: str,
    snapshot: RuntimeSnapshot,
    lifecycle: LifecycleState | None = None,
) -> tuple[str, ...] | None:
    if cursor is None:
        return None
    if cursor.kind != kind or cursor.generation != snapshot.generation:
        raise ToolBundleMutationConflictError("stale cursor")
    if lifecycle is not None and (
        cursor.lifecycle_revision != lifecycle.revision or cursor.lifecycle_state_digest != lifecycle.state_digest
    ):
        raise ToolBundleMutationConflictError("stale cursor")
    return cursor.anchor


def _page_items(
    items: tuple[Any, ...],
    *,
    keys: tuple[tuple[str, ...], ...],
    anchor: tuple[str, ...] | None,
    limit: int,
) -> tuple[tuple[Any, ...], tuple[str, ...] | None]:
    start = 0 if anchor is None else bisect_right(keys, anchor)
    selected = items[start : start + limit]
    next_anchor = keys[start + limit - 1] if start + limit < len(items) else None
    return selected, next_anchor


def _tool_summary(item: DiscoveredTool) -> dict[str, Any]:
    return {
        "effect": item.spec.effect.value,
        "name": item.spec.name,
        "permission": item.spec.permission,
        "provider_id": item.provider_id,
        "source": item.source.value,
        "trust": item.trust.value,
    }


def _tool_detail(item: DiscoveredTool) -> dict[str, Any]:
    properties = item.spec.parameters.get("properties", {})
    required = item.spec.parameters.get("required", ())
    return {
        **_tool_summary(item),
        "dependency_count": len(item.spec.dependencies),
        "dependencies": list(item.spec.dependencies),
        "description": item.spec.description,
        "has_capability_policy": item.spec.policy is not None,
        "parameter_count": len(properties) if isinstance(properties, Mapping) else 0,
        "required_parameter_count": len(required) if isinstance(required, (list, tuple)) else 0,
        "result_limit": item.spec.result_limit,
        "timeout_seconds": (None if item.spec.timeout_seconds is None else float(item.spec.timeout_seconds)),
    }


def _bundle_version_item(
    bundle_id: str,
    digest: str,
    state: LifecycleState,
) -> dict[str, Any]:
    record = state.versions[bundle_id][digest]
    return {
        "activated_at": record.activated_at,
        "active": state.active.get(bundle_id) == digest,
        "approved_at": record.approved_at,
        "archived_at": record.archived_at,
        "bundle_id": bundle_id,
        "created_at": record.created_at,
        "deprecated_at": record.deprecated_at,
        "digest": digest,
        "source_draft_id": record.source_draft_id,
        "state": record.state.value,
    }


def _draft_item(draft_id: str, state: LifecycleState) -> dict[str, Any]:
    record = state.drafts[draft_id]
    risks = {risk for evidence in record.evidence for risk in evidence.risks}
    return {
        "approvable": record.state is DraftState.AWAITING_APPROVAL,
        "bundle_id": record.bundle_id,
        "created_at": record.created_at,
        "digest": record.digest,
        "draft_id": draft_id,
        "evidence_states": [evidence.state.value for evidence in record.evidence],
        "has_risks": bool(risks),
        "risk_count": len(risks),
        "state": record.state.value,
        "updated_at": record.updated_at,
    }


def _decode_mutation_body(
    body: bytes,
    *,
    require_review_stamp: bool,
) -> dict[str, Any]:
    if not body or len(body) > _MUTATION_BODY_MAX_BYTES:
        raise ValueError("mutation body 为空或超限")
    try:
        value = json.loads(
            body,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("mutation body 不是严格 JSON") from None
    if not isinstance(value, dict):
        raise ValueError("mutation body 必须是 object")
    expected = {
        "digest",
        "expected_generation",
        "expected_lifecycle_revision",
        "expected_lifecycle_state_digest",
    }
    if require_review_stamp:
        expected.add("review_stamp")
    if set(value) != expected:
        raise ValueError("mutation body 字段不精确")
    if (
        not isinstance(value["digest"], str)
        or not _DIGEST_RE.fullmatch(value["digest"])
        or not isinstance(value["expected_generation"], int)
        or isinstance(value["expected_generation"], bool)
        or value["expected_generation"] <= 0
        or value["expected_generation"] > _MAX_JSON_INTEGER
        or not isinstance(value["expected_lifecycle_revision"], int)
        or isinstance(value["expected_lifecycle_revision"], bool)
        or value["expected_lifecycle_revision"] < 0
        or value["expected_lifecycle_revision"] > _MAX_JSON_INTEGER
        or not isinstance(value["expected_lifecycle_state_digest"], str)
        or not _DIGEST_RE.fullmatch(value["expected_lifecycle_state_digest"])
        or (
            require_review_stamp
            and (not isinstance(value["review_stamp"], str) or not _DIGEST_RE.fullmatch(value["review_stamp"]))
        )
    ):
        raise ValueError("mutation body identity 非法")
    return value


def _lifecycle_matches_runtime(
    lifecycle: LifecycleState,
    snapshot: RuntimeSnapshot,
) -> bool:
    return (
        lifecycle.revision == snapshot.generated_state_revision
        and lifecycle.state_digest == snapshot.generated_state_digest
        and dict(lifecycle.active) == dict(snapshot.generated_active)
    )


class ToolBundleApiService:
    """Authenticated H-02 catalog and lifecycle API over explicit ports."""

    def __init__(
        self,
        *,
        snapshots: RuntimeSnapshotReader,
        lifecycle: ToolLifecycleStateReader,
        authenticator: RuntimeApiAuthenticator,
        mutations: ToolBundleMutationPort,
    ) -> None:
        if not isinstance(snapshots, RuntimeSnapshotReader):
            raise RuntimeApiConfigurationError("Tool Bundle API snapshots 必须实现 current()")
        if not isinstance(lifecycle, ToolLifecycleStateReader):
            raise RuntimeApiConfigurationError("Tool Bundle API lifecycle 必须实现 async read_current()")
        if not isinstance(authenticator, RuntimeApiAuthenticator):
            raise RuntimeApiConfigurationError("Tool Bundle API authenticator 必须实现 async authenticate()")
        if not isinstance(mutations, ToolBundleMutationPort):
            raise RuntimeApiConfigurationError("Tool Bundle API mutations 必须实现显式 mutation port")
        self._snapshots = snapshots
        self._lifecycle = lifecycle
        self._authenticator = authenticator
        self._mutations = mutations

    @property
    def endpoints(self) -> tuple[ToolBundleApiEndpoint, ...]:
        return _ENDPOINTS

    async def _authenticate(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiPrincipal | RuntimeApiResponse:
        try:
            principal = await self._authenticator.authenticate(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "authentication_unavailable")
        if principal is None:
            return _error_response(
                401,
                "unauthorized",
                extra_headers=((b"www-authenticate", b'Bearer realm="moellm-runtime"'),),
            )
        if not isinstance(principal, RuntimeApiPrincipal):
            return _error_response(503, "authentication_unavailable")
        return principal

    def _current_snapshot(self) -> RuntimeSnapshot | RuntimeApiResponse:
        try:
            snapshot = _validated_snapshot(self._snapshots)
        except Exception:
            return _error_response(503, "runtime_snapshot_unavailable")
        if snapshot is None or snapshot.tool_snapshot is None:
            return _error_response(503, "runtime_snapshot_unavailable")
        catalog = snapshot.tool_snapshot.provider_catalog
        if not isinstance(catalog, ProviderCatalogSnapshot):
            return _error_response(503, "tool_catalog_unavailable")
        return snapshot

    async def _current_lifecycle(
        self,
        snapshot: RuntimeSnapshot,
    ) -> LifecycleState | RuntimeApiResponse:
        try:
            lifecycle = await self._lifecycle.read_current()
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "tool_lifecycle_unavailable")
        if not isinstance(lifecycle, LifecycleState):
            return _error_response(503, "tool_lifecycle_unavailable")
        try:
            matches = _lifecycle_matches_runtime(lifecycle, snapshot)
        except Exception:
            matches = False
        if not matches:
            return _error_response(503, "tool_lifecycle_unavailable")
        return lifecycle

    @staticmethod
    def _page_response(
        *,
        snapshot: RuntimeSnapshot,
        items: tuple[dict[str, Any], ...],
        next_cursor: str | None,
        lifecycle: LifecycleState | None = None,
    ) -> RuntimeApiResponse:
        payload: dict[str, Any] = {
            "api_version": RUNTIME_API_VERSION,
            "generation": snapshot.generation,
            "items": list(items),
            "next_cursor": next_cursor,
        }
        if lifecycle is not None:
            payload.update(
                {
                    "lifecycle_revision": lifecycle.revision,
                    "lifecycle_state_digest": lifecycle.state_digest,
                }
            )
        return RuntimeApiResponse(status_code=200, payload=payload)

    async def _list_tools(
        self,
        snapshot: RuntimeSnapshot,
        query: _PageQuery,
    ) -> RuntimeApiResponse:
        catalog = snapshot.tool_snapshot.provider_catalog
        assert isinstance(catalog, ProviderCatalogSnapshot)
        anchor = _validate_cursor_identity(
            query.cursor,
            kind="tools",
            snapshot=snapshot,
        )
        records = tuple(catalog.tools[name] for name in sorted(catalog.tools))
        keys = tuple((item.spec.name,) for item in records)
        selected, next_anchor = _page_items(
            records,
            keys=keys,
            anchor=anchor,
            limit=query.limit,
        )
        next_cursor = (
            None
            if next_anchor is None
            else _encode_cursor(
                _PageCursor(
                    kind="tools",
                    generation=snapshot.generation,
                    anchor=next_anchor,
                )
            )
        )
        return self._page_response(
            snapshot=snapshot,
            items=tuple(_tool_summary(item) for item in selected),
            next_cursor=next_cursor,
        )

    async def _get_tool(
        self,
        snapshot: RuntimeSnapshot,
        name: str,
    ) -> RuntimeApiResponse:
        catalog = snapshot.tool_snapshot.provider_catalog
        assert isinstance(catalog, ProviderCatalogSnapshot)
        item = catalog.tools.get(name)
        if item is None:
            return _error_response(404, "not_found")
        return RuntimeApiResponse(
            status_code=200,
            payload={
                "api_version": RUNTIME_API_VERSION,
                "generation": snapshot.generation,
                "tool": _tool_detail(item),
            },
        )

    async def _list_bundles(
        self,
        snapshot: RuntimeSnapshot,
        lifecycle: LifecycleState,
        query: _PageQuery,
    ) -> RuntimeApiResponse:
        anchor = _validate_cursor_identity(
            query.cursor,
            kind="tool_bundles",
            snapshot=snapshot,
            lifecycle=lifecycle,
        )
        keys = tuple(
            (bundle_id, digest) for bundle_id in sorted(lifecycle.versions) for digest in sorted(lifecycle.versions[bundle_id])
        )
        records = tuple(_bundle_version_item(bundle_id, digest, lifecycle) for bundle_id, digest in keys)
        selected, next_anchor = _page_items(
            records,
            keys=keys,
            anchor=anchor,
            limit=query.limit,
        )
        next_cursor = (
            None
            if next_anchor is None
            else _encode_cursor(
                _PageCursor(
                    kind="tool_bundles",
                    generation=snapshot.generation,
                    lifecycle_revision=lifecycle.revision,
                    lifecycle_state_digest=lifecycle.state_digest,
                    anchor=next_anchor,
                )
            )
        )
        return self._page_response(
            snapshot=snapshot,
            lifecycle=lifecycle,
            items=selected,
            next_cursor=next_cursor,
        )

    async def _list_drafts(
        self,
        snapshot: RuntimeSnapshot,
        lifecycle: LifecycleState,
        query: _PageQuery,
    ) -> RuntimeApiResponse:
        anchor = _validate_cursor_identity(
            query.cursor,
            kind="tool_drafts",
            snapshot=snapshot,
            lifecycle=lifecycle,
        )
        draft_ids = tuple(sorted(lifecycle.drafts))
        keys = tuple((draft_id,) for draft_id in draft_ids)
        records = tuple(_draft_item(draft_id, lifecycle) for draft_id in draft_ids)
        selected, next_anchor = _page_items(
            records,
            keys=keys,
            anchor=anchor,
            limit=query.limit,
        )
        next_cursor = (
            None
            if next_anchor is None
            else _encode_cursor(
                _PageCursor(
                    kind="tool_drafts",
                    generation=snapshot.generation,
                    lifecycle_revision=lifecycle.revision,
                    lifecycle_state_digest=lifecycle.state_digest,
                    anchor=next_anchor,
                )
            )
        )
        return self._page_response(
            snapshot=snapshot,
            lifecycle=lifecycle,
            items=selected,
            next_cursor=next_cursor,
        )

    @staticmethod
    def _check_mutation_preconditions(
        body: Mapping[str, Any],
        *,
        snapshot: RuntimeSnapshot,
        lifecycle: LifecycleState,
    ) -> RuntimeApiResponse | None:
        if snapshot.generation >= _MAX_JSON_INTEGER or lifecycle.revision >= _MAX_JSON_INTEGER:
            return _error_response(503, "mutation_unavailable")
        if (
            body["expected_generation"] != snapshot.generation
            or body["expected_lifecycle_revision"] != lifecycle.revision
            or body["expected_lifecycle_state_digest"] != lifecycle.state_digest
        ):
            return _error_response(409, "mutation_precondition_failed")
        return None

    @staticmethod
    def _validate_mutation_result(
        result: ToolBundleMutationResult,
        *,
        operation: str,
        bundle_id: str,
        digest: str,
        draft_id: str | None,
        expected_generation: int,
        expected_lifecycle_revision: int,
        expected_lifecycle_state_digest: str,
    ) -> bool:
        return (
            isinstance(result, ToolBundleMutationResult)
            and result.operation == operation
            and result.bundle_id == bundle_id
            and result.digest == digest
            and result.draft_id == draft_id
            and result.generation == expected_generation + 1
            and result.lifecycle_revision == expected_lifecycle_revision + 1
            and result.lifecycle_state_digest != expected_lifecycle_state_digest
            and result.audit_recorded is True
            and (operation != "activate_bundle" or result.active_digest == digest)
        )

    @staticmethod
    def _mutation_response(result: ToolBundleMutationResult) -> RuntimeApiResponse:
        return RuntimeApiResponse(
            status_code=200,
            payload={
                "active_digest": result.active_digest,
                "api_version": RUNTIME_API_VERSION,
                "audit_recorded": result.audit_recorded,
                "bundle_id": result.bundle_id,
                "digest": result.digest,
                "draft_id": result.draft_id,
                "generation": result.generation,
                "lifecycle_revision": result.lifecycle_revision,
                "lifecycle_state_digest": result.lifecycle_state_digest,
                "operation": result.operation,
                "operation_id": result.operation_id,
            },
        )

    @staticmethod
    def _mutation_error(error: BaseException) -> RuntimeApiResponse:
        if isinstance(error, ToolBundleMutationResultUnknownError):
            return RuntimeApiResponse(
                status_code=409,
                payload={
                    "api_version": RUNTIME_API_VERSION,
                    "error": "mutation_result_unknown",
                    "retryable": False,
                },
            )
        if isinstance(error, ToolBundleMutationNotFoundError):
            return _error_response(404, "not_found")
        if isinstance(error, ToolBundleMutationConflictError):
            return _error_response(409, "mutation_precondition_failed")
        return _error_response(503, "mutation_unavailable")

    async def _approve_draft(
        self,
        principal: RuntimeApiPrincipal,
        draft_id: str,
        body: Mapping[str, Any],
        snapshot: RuntimeSnapshot,
        lifecycle: LifecycleState,
    ) -> RuntimeApiResponse:
        conflict = self._check_mutation_preconditions(
            body,
            snapshot=snapshot,
            lifecycle=lifecycle,
        )
        if conflict is not None:
            return conflict
        record = lifecycle.drafts.get(draft_id)
        if record is None:
            return _error_response(404, "not_found")
        if record.digest != body["digest"] or record.state is not DraftState.AWAITING_APPROVAL:
            return _error_response(409, "mutation_precondition_failed")
        expected_review_stamp = draft_review_stamp(
            draft_id=draft_id,
            digest=record.digest,
            lifecycle_revision=lifecycle.revision,
            lifecycle_state_digest=lifecycle.state_digest,
            active_digest=lifecycle.active.get(record.bundle_id),
        )
        if not secrets.compare_digest(
            body["review_stamp"],
            expected_review_stamp,
        ):
            return _error_response(409, "mutation_precondition_failed")
        command = ApproveToolDraftCommand(
            actor_subject=principal.subject,
            draft_id=draft_id,
            bundle_id=record.bundle_id,
            digest=record.digest,
            expected_generation=snapshot.generation,
            expected_lifecycle_revision=lifecycle.revision,
            expected_lifecycle_state_digest=lifecycle.state_digest,
            review_stamp=expected_review_stamp,
        )
        try:
            result = await self._mutations.approve_draft(command)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._mutation_error(error)
        if not self._validate_mutation_result(
            result,
            operation="approve_draft",
            bundle_id=record.bundle_id,
            digest=record.digest,
            draft_id=draft_id,
            expected_generation=snapshot.generation,
            expected_lifecycle_revision=lifecycle.revision,
            expected_lifecycle_state_digest=lifecycle.state_digest,
        ):
            return _error_response(503, "mutation_unavailable")
        return self._mutation_response(result)

    async def _activate_bundle(
        self,
        principal: RuntimeApiPrincipal,
        bundle_id: str,
        body: Mapping[str, Any],
        snapshot: RuntimeSnapshot,
        lifecycle: LifecycleState,
    ) -> RuntimeApiResponse:
        conflict = self._check_mutation_preconditions(
            body,
            snapshot=snapshot,
            lifecycle=lifecycle,
        )
        if conflict is not None:
            return conflict
        digest = body["digest"]
        record = lifecycle.versions.get(bundle_id, {}).get(digest)
        if record is None:
            return _error_response(404, "not_found")
        if record.state not in {VersionState.APPROVED, VersionState.DEPRECATED} or lifecycle.active.get(bundle_id) == digest:
            return _error_response(409, "mutation_precondition_failed")
        command = ActivateToolBundleCommand(
            actor_subject=principal.subject,
            bundle_id=bundle_id,
            digest=digest,
            expected_generation=snapshot.generation,
            expected_lifecycle_revision=lifecycle.revision,
            expected_lifecycle_state_digest=lifecycle.state_digest,
        )
        try:
            result = await self._mutations.activate_bundle(command)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._mutation_error(error)
        if not self._validate_mutation_result(
            result,
            operation="activate_bundle",
            bundle_id=bundle_id,
            digest=digest,
            draft_id=None,
            expected_generation=snapshot.generation,
            expected_lifecycle_revision=lifecycle.revision,
            expected_lifecycle_state_digest=lifecycle.state_digest,
        ):
            return _error_response(503, "mutation_unavailable")
        return self._mutation_response(result)

    async def handle(self, request: RuntimeApiRequest) -> RuntimeApiResponse:
        if not isinstance(request, RuntimeApiRequest):
            return _error_response(400, "invalid_request")
        authenticated = await self._authenticate(request)
        if isinstance(authenticated, RuntimeApiResponse):
            return authenticated
        principal = authenticated

        resolved = _resolve_endpoint(request.path)
        if resolved is None:
            return _error_response(404, "not_found")
        endpoint = resolved.endpoint
        if not principal.permits(endpoint.required_scope):
            return _error_response(403, "forbidden")
        if request.method != endpoint.method:
            return _error_response(
                405,
                "method_not_allowed",
                extra_headers=((b"allow", endpoint.method.encode("ascii")),),
            )

        page_query: _PageQuery | None = None
        mutation_body: dict[str, Any] | None = None
        if request.method == "GET":
            if request.content_type is not None or request.body:
                return _error_response(400, "body_not_supported")
            if endpoint.path_template in {"/tools", "/tool-bundles", "/tool-drafts"}:
                try:
                    page_query = _parse_page_query(request.query_string)
                except ValueError:
                    return _error_response(400, "invalid_query")
            elif request.query_string:
                return _error_response(400, "query_not_supported")
        else:
            if request.query_string:
                return _error_response(400, "query_not_supported")
            if request.content_type != "application/json":
                return _error_response(415, "unsupported_media_type")
            try:
                mutation_body = _decode_mutation_body(
                    request.body,
                    require_review_stamp=(endpoint.path_template == "/tool-drafts/{id}/approve"),
                )
            except ValueError:
                return _error_response(400, "invalid_request")

        snapshot = self._current_snapshot()
        if isinstance(snapshot, RuntimeApiResponse):
            return snapshot

        try:
            if endpoint.path_template == "/tools":
                assert page_query is not None
                return await self._list_tools(snapshot, page_query)
            if endpoint.path_template == "/tools/{name}":
                assert resolved.target is not None
                return await self._get_tool(snapshot, resolved.target)

            lifecycle = await self._current_lifecycle(snapshot)
            if isinstance(lifecycle, RuntimeApiResponse):
                return lifecycle
            if endpoint.path_template == "/tool-bundles":
                assert page_query is not None
                return await self._list_bundles(snapshot, lifecycle, page_query)
            if endpoint.path_template == "/tool-drafts":
                assert page_query is not None
                return await self._list_drafts(snapshot, lifecycle, page_query)
            if endpoint.path_template == "/tool-drafts/{id}/approve":
                assert resolved.target is not None
                assert mutation_body is not None
                return await self._approve_draft(
                    principal,
                    resolved.target,
                    mutation_body,
                    snapshot,
                    lifecycle,
                )
            assert endpoint.path_template == "/tool-bundles/{id}/activate"
            assert resolved.target is not None
            assert mutation_body is not None
            return await self._activate_bundle(
                principal,
                resolved.target,
                mutation_body,
                snapshot,
                lifecycle,
            )
        except asyncio.CancelledError:
            raise
        except ToolBundleMutationConflictError:
            return _error_response(409, "stale_cursor")
        except (RuntimeApiConfigurationError, TypeError, ValueError):
            return _error_response(503, "tool_catalog_unavailable")


__all__ = [
    "TOOL_BUNDLE_API_READ_SCOPE",
    "TOOL_BUNDLE_API_WRITE_SCOPE",
    "ActivateToolBundleCommand",
    "ApproveToolDraftCommand",
    "ToolBundleApiEndpoint",
    "ToolBundleApiError",
    "ToolBundleApiService",
    "ToolBundleMutationConflictError",
    "ToolBundleMutationError",
    "ToolBundleMutationNotFoundError",
    "ToolBundleMutationPort",
    "ToolBundleMutationResult",
    "ToolBundleMutationResultUnknownError",
    "ToolBundleMutationUnavailableError",
    "ToolLifecycleStateReader",
]

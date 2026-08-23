from __future__ import annotations

import asyncio
import base64
import binascii
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Protocol, runtime_checkable
import unicodedata

from .model_selector import ModelRuntimeState
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

METRICS_API_READ_SCOPE = "metrics:read"
MODEL_API_READ_SCOPE = "models:read"

_METRICS_PATH = "/metrics"
_MODELS_PATH = "/models"
_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 20
_MAX_MODEL_RECORDS = 4_096
_MODEL_IDENTITY_BYTES_MAX = 512
_MODEL_NAME_BYTES_MAX = 255
_PROVIDER_NAME_BYTES_MAX = 128
_MODEL_ITEM_BYTES_MAX = 512
_MODEL_ITEM_JSON_BYTES_MAX = 640
_CURSOR_VERSION = 1
_CURSOR_RAW_BYTES_MAX = 1_024
_CURSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,2048}$")
_DISPATCH_MODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_DISPATCH_MODES = 16

_INTEGER_METRIC_FIELDS = (
    "llm_active",
    "llm_pending",
    "llm_rejected",
    "dispatch_active",
    "dispatch_pending",
    "dispatch_rejected",
    "dispatch_timeouts",
    "member_cache_hits",
    "member_cache_misses",
    "member_lookup_timeouts",
    "tool_steps",
    "tool_timeouts",
    "generated_runner_active",
    "generated_runner_pending",
    "generated_runner_rejected",
    "generated_runner_timeouts",
    "generated_runner_killed",
    "generated_runner_orphan_cleanups",
    "generated_runner_failures",
    "generated_authoring_active",
    "classification_count",
    "reload_generation",
    "reload_successes",
    "reload_failures",
)


class MetricsApiError(RuntimeError):
    """Base error for the detached H-04 model and metrics API."""


class MetricsApiCursorConflictError(MetricsApiError):
    """A model cursor no longer identifies the current runtime generation."""


@runtime_checkable
class RuntimeMetricsReader(Protocol):
    """Explicit snapshot-only boundary for low-cardinality runtime metrics."""

    def snapshot(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class MetricsApiEndpoint:
    method: str
    path: str
    required_scope: str

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise RuntimeApiConfigurationError("H-04 endpoint 只允许 GET")
        expected_scope = {
            _METRICS_PATH: METRICS_API_READ_SCOPE,
            _MODELS_PATH: MODEL_API_READ_SCOPE,
        }.get(self.path)
        if expected_scope is None or self.required_scope != expected_scope:
            raise RuntimeApiConfigurationError("H-04 endpoint contract 非法")


_ENDPOINTS = (
    MetricsApiEndpoint("GET", _METRICS_PATH, METRICS_API_READ_SCOPE),
    MetricsApiEndpoint("GET", _MODELS_PATH, MODEL_API_READ_SCOPE),
)


def _safe_text(value: object, *, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise RuntimeApiConfigurationError(f"{label} 必须是非空 canonical 字符串")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise RuntimeApiConfigurationError(f"{label} 必须是有效 UTF-8") from None
    if len(encoded) > maximum_bytes or any(unicodedata.category(character).startswith("C") for character in value):
        raise RuntimeApiConfigurationError(f"{label} 包含控制字符或超过安全上限")
    return value


def _safe_integer(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_JSON_INTEGER:
        raise RuntimeApiConfigurationError(f"{label} 必须是非负 BIGINT")
    return value


def _safe_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeApiConfigurationError(f"{label} 必须是有限非负数")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise RuntimeApiConfigurationError(f"{label} 必须是有限非负数")
    return normalized


@dataclass(frozen=True)
class _ModelItem:
    identity: str
    model: str
    provider: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "identity",
            _safe_text(
                self.identity,
                label="model identity",
                maximum_bytes=_MODEL_IDENTITY_BYTES_MAX,
            ),
        )
        object.__setattr__(
            self,
            "model",
            _safe_text(
                self.model,
                label="model name",
                maximum_bytes=_MODEL_NAME_BYTES_MAX,
            ),
        )
        object.__setattr__(
            self,
            "provider",
            _safe_text(
                self.provider,
                label="provider name",
                maximum_bytes=_PROVIDER_NAME_BYTES_MAX,
            ),
        )
        if sum(len(value.encode("utf-8")) for value in (self.identity, self.model, self.provider)) > _MODEL_ITEM_BYTES_MAX:
            raise RuntimeApiConfigurationError("model catalog item 超过响应安全上限")
        encoded_item = json.dumps(
            {
                "id": self.identity,
                "model": self.model,
                "provider": self.provider,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded_item) > _MODEL_ITEM_JSON_BYTES_MAX:
            raise RuntimeApiConfigurationError("model catalog item JSON 超过响应安全上限")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.identity)

    def as_payload(self) -> dict[str, str]:
        return {
            "id": self.identity,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class _ModelCursor:
    generation: int
    provider: str
    model: str
    identity: str

    def __post_init__(self) -> None:
        generation = _safe_integer(self.generation, label="cursor generation")
        if generation <= 0:
            raise RuntimeApiConfigurationError("cursor generation 必须是正整数")
        object.__setattr__(self, "generation", generation)
        item = _ModelItem(
            identity=self.identity,
            model=self.model,
            provider=self.provider,
        )
        object.__setattr__(self, "identity", item.identity)
        object.__setattr__(self, "model", item.model)
        object.__setattr__(self, "provider", item.provider)

    @property
    def anchor(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.identity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "anchor": [self.provider, self.model, self.identity],
            "generation": self.generation,
            "kind": "models",
            "version": _CURSOR_VERSION,
        }


def _encode_cursor(cursor: _ModelCursor) -> str:
    content = json.dumps(
        cursor.as_dict(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not content or len(content) > _CURSOR_RAW_BYTES_MAX:
        raise RuntimeApiConfigurationError("cursor payload 超过安全上限")
    token = base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")
    if not _CURSOR_TOKEN_RE.fullmatch(token):
        raise RuntimeApiConfigurationError("cursor token 超过安全上限")
    return token


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("cursor JSON 字段重复")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"cursor JSON 常量非法: {value}")


def _decode_cursor(token: str) -> _ModelCursor:
    if not isinstance(token, str) or not _CURSOR_TOKEN_RE.fullmatch(token):
        raise ValueError("cursor token 非法")
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(token + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("cursor token 不是 base64url") from None
    if not raw or len(raw) > _CURSOR_RAW_BYTES_MAX:
        raise ValueError("cursor payload 为空或超限")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("cursor payload 不是严格 JSON") from None
    if not isinstance(value, dict) or set(value) != {"anchor", "generation", "kind", "version"}:
        raise ValueError("cursor payload 字段不精确")
    anchor = value["anchor"]
    if value["kind"] != "models" or value["version"] != _CURSOR_VERSION or not isinstance(anchor, list) or len(anchor) != 3:
        raise ValueError("cursor payload identity 非法")
    try:
        cursor = _ModelCursor(
            generation=value["generation"],
            provider=anchor[0],
            model=anchor[1],
            identity=anchor[2],
        )
        canonical = _encode_cursor(cursor)
    except (RuntimeApiConfigurationError, TypeError, ValueError):
        raise ValueError("cursor payload identity 非法") from None
    if canonical != token:
        raise ValueError("cursor token 必须是 canonical base64url")
    return cursor


@dataclass(frozen=True)
class _PageQuery:
    limit: int = _PAGE_SIZE_DEFAULT
    cursor: _ModelCursor | None = None


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
    limit = _PAGE_SIZE_DEFAULT
    limit_text = values.get("limit")
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


def _model_items(snapshot: object) -> tuple[_ModelItem, ...]:
    state = getattr(snapshot, "model_state", None)
    if not isinstance(state, ModelRuntimeState):
        raise RuntimeApiConfigurationError("model catalog 不可用")
    models = state.models
    if not isinstance(models, Mapping) or len(models) > _MAX_MODEL_RECORDS:
        raise RuntimeApiConfigurationError("model catalog 类型或长度非法")
    items: list[_ModelItem] = []
    for identity, details in models.items():
        if not isinstance(details, Mapping):
            raise RuntimeApiConfigurationError("model catalog item 必须是映射")
        model = details.get("model")
        provider = details.get("provider")
        if not isinstance(model, str) or not isinstance(provider, str):
            raise RuntimeApiConfigurationError("model catalog identity 字段非法")
        items.append(
            _ModelItem(
                identity=identity,
                model=model,
                provider=provider,
            )
        )
    items.sort(key=lambda item: item.key)
    keys = tuple(item.key for item in items)
    if len(keys) != len(set(keys)):
        raise RuntimeApiConfigurationError("model catalog identity 重复")
    return tuple(items)


def _model_page(
    items: tuple[_ModelItem, ...],
    *,
    generation: int,
    query: _PageQuery,
) -> tuple[tuple[_ModelItem, ...], str | None]:
    keys = tuple(item.key for item in items)
    start = 0
    if query.cursor is not None:
        cursor = query.cursor
        if cursor.generation != generation or cursor.anchor not in keys:
            raise MetricsApiCursorConflictError("model cursor 已过期")
        start = bisect_right(keys, cursor.anchor)
    selected = items[start : start + query.limit]
    next_cursor = None
    if start + query.limit < len(items):
        anchor = selected[-1]
        next_cursor = _encode_cursor(
            _ModelCursor(
                generation=generation,
                provider=anchor.provider,
                model=anchor.model,
                identity=anchor.identity,
            )
        )
    return selected, next_cursor


def _dispatch_modes(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or len(value) > _MAX_DISPATCH_MODES:
        raise RuntimeApiConfigurationError("dispatch_modes 类型或长度非法")
    result: dict[str, int] = {}
    for mode, count in value.items():
        if not isinstance(mode, str) or not _DISPATCH_MODE_RE.fullmatch(mode):
            raise RuntimeApiConfigurationError("dispatch mode 非法")
        result[mode] = _safe_integer(count, label=f"dispatch mode {mode}")
    return dict(sorted(result.items()))


def _metrics_payload(value: object, *, generation: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeApiConfigurationError("metrics snapshot 必须是映射")
    metrics = {name: _safe_integer(value.get(name), label=name) for name in _INTEGER_METRIC_FIELDS}
    if metrics["reload_generation"] != generation:
        raise RuntimeApiConfigurationError("metrics/runtime generation 不一致")
    started_at = _safe_float(value.get("started_at"), label="started_at")
    classification_seconds = _safe_float(
        value.get("classification_seconds"),
        label="classification_seconds",
    )
    last_reload_value = value.get("last_reload_at")
    last_reload_at = None if last_reload_value is None else _safe_float(last_reload_value, label="last_reload_at")
    classification_count = metrics["classification_count"]
    average_seconds = 0.0 if classification_count == 0 else classification_seconds / classification_count
    return {
        "api_version": RUNTIME_API_VERSION,
        "classification": {
            "average_seconds": average_seconds,
            "count": classification_count,
            "total_seconds": classification_seconds,
        },
        "dispatch": {
            "active": metrics["dispatch_active"],
            "modes": _dispatch_modes(value.get("dispatch_modes")),
            "pending": metrics["dispatch_pending"],
            "rejected": metrics["dispatch_rejected"],
            "timeouts": metrics["dispatch_timeouts"],
        },
        "generated": {
            "authoring_active": metrics["generated_authoring_active"],
            "runner": {
                "active": metrics["generated_runner_active"],
                "failures": metrics["generated_runner_failures"],
                "killed": metrics["generated_runner_killed"],
                "orphan_cleanups": metrics["generated_runner_orphan_cleanups"],
                "pending": metrics["generated_runner_pending"],
                "rejected": metrics["generated_runner_rejected"],
                "timeouts": metrics["generated_runner_timeouts"],
            },
        },
        "generation": generation,
        "llm": {
            "active": metrics["llm_active"],
            "pending": metrics["llm_pending"],
            "rejected": metrics["llm_rejected"],
        },
        "member_cache": {
            "hits": metrics["member_cache_hits"],
            "lookup_timeouts": metrics["member_lookup_timeouts"],
            "misses": metrics["member_cache_misses"],
        },
        "reload": {
            "failures": metrics["reload_failures"],
            "last_at": last_reload_at,
            "successes": metrics["reload_successes"],
        },
        "started_at": started_at,
        "tools": {
            "steps": metrics["tool_steps"],
            "timeouts": metrics["tool_timeouts"],
        },
    }


class MetricsApiService:
    """Authenticated H-04 model catalog and aggregate metrics boundary."""

    def __init__(
        self,
        *,
        snapshots: RuntimeSnapshotReader,
        metrics: RuntimeMetricsReader,
        authenticator: RuntimeApiAuthenticator,
    ) -> None:
        if not isinstance(snapshots, RuntimeSnapshotReader):
            raise RuntimeApiConfigurationError("Metrics API snapshots 必须实现 current()")
        if not isinstance(metrics, RuntimeMetricsReader):
            raise RuntimeApiConfigurationError("Metrics API metrics 必须实现 snapshot()")
        if not isinstance(authenticator, RuntimeApiAuthenticator):
            raise RuntimeApiConfigurationError("Metrics API authenticator 必须实现 async authenticate()")
        self._snapshots = snapshots
        self._metrics = metrics
        self._authenticator = authenticator

    @property
    def endpoints(self) -> tuple[MetricsApiEndpoint, ...]:
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

    async def handle(self, request: RuntimeApiRequest) -> RuntimeApiResponse:
        if not isinstance(request, RuntimeApiRequest):
            return _error_response(400, "invalid_request")
        authenticated = await self._authenticate(request)
        if isinstance(authenticated, RuntimeApiResponse):
            return authenticated
        principal = authenticated

        endpoint = next((item for item in _ENDPOINTS if item.path == request.path), None)
        if endpoint is None:
            return _error_response(404, "not_found")
        if not principal.permits(endpoint.required_scope):
            return _error_response(403, "forbidden")
        if request.method != endpoint.method:
            return _error_response(
                405,
                "method_not_allowed",
                extra_headers=((b"allow", endpoint.method.encode("ascii")),),
            )
        if request.content_type is not None or request.body:
            return _error_response(400, "body_not_supported")

        query = _PageQuery()
        if request.path == _MODELS_PATH:
            try:
                query = _parse_page_query(request.query_string)
            except ValueError:
                return _error_response(400, "invalid_query")
        elif request.query_string:
            return _error_response(400, "query_not_supported")

        try:
            snapshot = _validated_snapshot(self._snapshots)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "runtime_snapshot_unavailable")
        if snapshot is None:
            return _error_response(503, "runtime_snapshot_unavailable")

        if request.path == _MODELS_PATH:
            try:
                items = _model_items(snapshot)
                selected, next_cursor = _model_page(
                    items,
                    generation=snapshot.generation,
                    query=query,
                )
                return RuntimeApiResponse(
                    status_code=200,
                    payload={
                        "api_version": RUNTIME_API_VERSION,
                        "generation": snapshot.generation,
                        "items": [item.as_payload() for item in selected],
                        "next_cursor": next_cursor,
                        "total_count": len(items),
                    },
                )
            except asyncio.CancelledError:
                raise
            except MetricsApiCursorConflictError:
                return _error_response(409, "cursor_precondition_failed")
            except Exception:
                return _error_response(503, "model_catalog_unavailable")

        try:
            metrics = self._metrics.snapshot()
            payload = _metrics_payload(metrics, generation=snapshot.generation)
            return RuntimeApiResponse(status_code=200, payload=payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "metrics_unavailable")


__all__ = [
    "METRICS_API_READ_SCOPE",
    "MODEL_API_READ_SCOPE",
    "MetricsApiCursorConflictError",
    "MetricsApiEndpoint",
    "MetricsApiError",
    "MetricsApiService",
    "RuntimeMetricsReader",
]

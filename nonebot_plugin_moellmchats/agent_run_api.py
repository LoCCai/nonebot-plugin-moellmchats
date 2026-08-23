from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Protocol, runtime_checkable

from .agent_runtime import AgentRun, AgentRunState
from .runtime_api import (
    _MAX_JSON_INTEGER,
    RUNTIME_API_VERSION,
    RuntimeApiAuthenticator,
    RuntimeApiConfigurationError,
    RuntimeApiPrincipal,
    RuntimeApiRequest,
    RuntimeApiResponse,
    _error_response,
)

AGENT_RUN_API_READ_SCOPE = "agent-runs:read"
AGENT_RUN_API_WRITE_SCOPE = "agent-runs:write"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ACTOR_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_OPERATION_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_RUN_DETAIL_PATH_RE = re.compile(r"^/agent-runs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})$")
_RUN_CANCEL_PATH_RE = re.compile(r"^/agent-runs/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/cancel$")
_CURSOR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 20
_PAGE_FETCH_MAX = _PAGE_SIZE_MAX + 1
_CURSOR_VERSION = 1
_CURSOR_RAW_BYTES_MAX = 512
_CANCEL_BODY_MAX_BYTES = 2_048
_CANCELLABLE_STATES = frozenset(
    {
        AgentRunState.CREATED,
        AgentRunState.ADMITTED,
        AgentRunState.CLASSIFYING,
        AgentRunState.PLANNING,
        AgentRunState.EXECUTING,
        AgentRunState.WAITING_CONFIRMATION,
        AgentRunState.SUMMARIZING,
    }
)


class AgentRunApiError(RuntimeError):
    """Base error for the detached H-03 Agent Run API."""


class AgentRunCancellationError(AgentRunApiError):
    """Base error returned by an explicitly injected cancellation port."""


class AgentRunCancellationNotFoundError(AgentRunCancellationError):
    """The requested run no longer exists at the cancellation boundary."""


class AgentRunCancellationConflictError(AgentRunCancellationError):
    """The run state or generation no longer matches the supplied CAS."""


class AgentRunCancellationUnavailableError(AgentRunCancellationError):
    """Cancellation was not started because its dependency was unavailable."""


class AgentRunCancellationResultUnknownError(AgentRunCancellationError):
    """The caller must inspect state and must not automatically replay."""


def _require_run_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        raise RuntimeApiConfigurationError(f"{label} 非法")
    return value


def _require_generation(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > _MAX_JSON_INTEGER:
        raise RuntimeApiConfigurationError(f"{label} 必须是非负 BIGINT")
    return value


def _require_api_run(run: object, *, label: str) -> AgentRun:
    if not isinstance(run, AgentRun):
        raise RuntimeApiConfigurationError(f"{label} 必须是 AgentRun")
    if run.request_id > _MAX_JSON_INTEGER or run.generation > _MAX_JSON_INTEGER:
        raise RuntimeApiConfigurationError(f"{label} identity 超过 BIGINT 上限")
    return run


@dataclass(frozen=True)
class AgentRunReadRequest:
    """A bounded newest-first keyset request for an explicit read port."""

    limit: int
    before_started_at: float | None = None
    before_run_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or not 1 <= self.limit <= _PAGE_FETCH_MAX:
            raise RuntimeApiConfigurationError("Agent Run read limit 必须是 1 到 21 的整数")
        if (self.before_started_at is None) != (self.before_run_id is None):
            raise RuntimeApiConfigurationError("Agent Run read keyset anchor 必须完整")
        if self.before_started_at is not None:
            if isinstance(self.before_started_at, bool) or not isinstance(self.before_started_at, (int, float)):
                raise RuntimeApiConfigurationError("Agent Run read started_at anchor 非法")
            normalized = float(self.before_started_at)
            if not math.isfinite(normalized) or normalized < 0:
                raise RuntimeApiConfigurationError("Agent Run read started_at anchor 非法")
            object.__setattr__(self, "before_started_at", normalized)
            _require_run_id(self.before_run_id, label="Agent Run read run_id anchor")


@dataclass(frozen=True)
class CancelAgentRunCommand:
    actor_subject: str
    run_id: str
    expected_state: AgentRunState
    expected_generation: int

    def __post_init__(self) -> None:
        if not isinstance(self.actor_subject, str) or not _ACTOR_SUBJECT_RE.fullmatch(self.actor_subject):
            raise RuntimeApiConfigurationError("cancel actor subject 非法")
        _require_run_id(self.run_id, label="cancel run_id")
        if self.expected_state not in _CANCELLABLE_STATES:
            raise RuntimeApiConfigurationError("cancel expected_state 不可取消")
        _require_generation(
            self.expected_generation,
            label="cancel expected_generation",
        )


@dataclass(frozen=True)
class CancelAgentRunResult:
    operation_id: str
    previous_state: AgentRunState
    run: AgentRun
    cancellation_settled: bool
    audit_recorded: bool

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise RuntimeApiConfigurationError("cancel result operation_id 非法")
        if self.previous_state not in _CANCELLABLE_STATES:
            raise RuntimeApiConfigurationError("cancel result previous_state 不可取消")
        run = _require_api_run(self.run, label="cancel result run")
        if run.state is not AgentRunState.CANCELLED:
            raise RuntimeApiConfigurationError("cancel result run 必须是 cancelled")
        if self.cancellation_settled is not True:
            raise RuntimeApiConfigurationError("Agent Run cancellation 必须确认执行已停止")
        if self.audit_recorded is not True:
            raise RuntimeApiConfigurationError("危险 Agent Run cancellation 必须确认即时审计已记录")


@runtime_checkable
class AgentRunStateReader(Protocol):
    """Read-only run boundary using a stable newest-first keyset.

    Implementations must return at most ``request.limit`` immutable records,
    ordered by ``(started_at DESC, run_id DESC)`` and strictly older than the
    supplied anchor. They must not use an unbounded scan or OFFSET pagination.
    """

    async def list_runs(
        self,
        request: AgentRunReadRequest,
    ) -> tuple[AgentRun, ...]: ...

    async def get_run(self, run_id: str) -> AgentRun | None: ...


@runtime_checkable
class AgentRunCancellationPort(Protocol):
    """CAS-bound cancellation boundary.

    Implementations must coordinate the live task and durable state under the
    exact expected state/generation, settle cancellation before returning,
    synchronously record the critical audit event, preserve caller
    cancellation, and never replay an unknown result.
    """

    async def cancel_run(
        self,
        command: CancelAgentRunCommand,
    ) -> CancelAgentRunResult: ...


@dataclass(frozen=True)
class AgentRunApiEndpoint:
    method: str
    path_template: str
    required_scope: str

    def __post_init__(self) -> None:
        allowed = {
            ("GET", "/agent-runs"),
            ("GET", "/agent-runs/{id}"),
            ("POST", "/agent-runs/{id}/cancel"),
        }
        if (self.method, self.path_template) not in allowed:
            raise RuntimeApiConfigurationError("H-03 endpoint contract 非法")
        expected_scope = AGENT_RUN_API_READ_SCOPE if self.method == "GET" else AGENT_RUN_API_WRITE_SCOPE
        if self.required_scope != expected_scope:
            raise RuntimeApiConfigurationError("H-03 endpoint scope 非法")


_ENDPOINTS = (
    AgentRunApiEndpoint("GET", "/agent-runs", AGENT_RUN_API_READ_SCOPE),
    AgentRunApiEndpoint(
        "GET",
        "/agent-runs/{id}",
        AGENT_RUN_API_READ_SCOPE,
    ),
    AgentRunApiEndpoint(
        "POST",
        "/agent-runs/{id}/cancel",
        AGENT_RUN_API_WRITE_SCOPE,
    ),
)


@dataclass(frozen=True)
class _ResolvedEndpoint:
    endpoint: AgentRunApiEndpoint
    run_id: str | None = None


def _resolve_endpoint(path: str) -> _ResolvedEndpoint | None:
    if path == "/agent-runs":
        return _ResolvedEndpoint(_ENDPOINTS[0])
    match = _RUN_DETAIL_PATH_RE.fullmatch(path)
    if match is not None:
        return _ResolvedEndpoint(_ENDPOINTS[1], match.group(1))
    match = _RUN_CANCEL_PATH_RE.fullmatch(path)
    if match is not None:
        return _ResolvedEndpoint(_ENDPOINTS[2], match.group(1))
    return None


@dataclass(frozen=True)
class _AgentRunCursor:
    started_at: float
    run_id: str

    def __post_init__(self) -> None:
        if isinstance(self.started_at, bool) or not isinstance(self.started_at, (int, float)):
            raise ValueError("cursor started_at 非法")
        normalized = float(self.started_at)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("cursor started_at 非法")
        object.__setattr__(self, "started_at", normalized)
        if not isinstance(self.run_id, str) or not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("cursor run_id 非法")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "agent_runs",
            "run_id": self.run_id,
            "started_at_hex": self.started_at.hex(),
            "version": _CURSOR_VERSION,
        }


def _encode_cursor(cursor: _AgentRunCursor) -> str:
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
    raise ValueError("JSON constant 非法")


def _decode_cursor(token: str) -> _AgentRunCursor:
    if not isinstance(token, str) or not _CURSOR_TOKEN_RE.fullmatch(token):
        raise ValueError("cursor token 非法")
    try:
        encoded = token.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise ValueError("cursor token 非法") from None
    if not raw or len(raw) > _CURSOR_RAW_BYTES_MAX:
        raise ValueError("cursor payload 非法或超限")
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("cursor payload 非法") from None
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "run_id",
        "started_at_hex",
        "version",
    }:
        raise ValueError("cursor payload 字段非法")
    if value["kind"] != "agent_runs" or value["version"] != _CURSOR_VERSION:
        raise ValueError("cursor identity 非法")
    started_at_hex = value["started_at_hex"]
    if not isinstance(started_at_hex, str) or len(started_at_hex) > 64:
        raise ValueError("cursor started_at 非法")
    try:
        started_at = float.fromhex(started_at_hex)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("cursor started_at 非法") from None
    if started_at.hex() != started_at_hex:
        raise ValueError("cursor started_at 非 canonical")
    cursor = _AgentRunCursor(started_at=started_at, run_id=value["run_id"])
    if _encode_cursor(cursor) != token:
        raise ValueError("cursor token 非 canonical")
    return cursor


@dataclass(frozen=True)
class _PageQuery:
    limit: int = _PAGE_SIZE_DEFAULT
    cursor: _AgentRunCursor | None = None


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


@dataclass(frozen=True)
class _CancelBody:
    expected_state: AgentRunState
    expected_generation: int

    def __post_init__(self) -> None:
        if self.expected_state not in _CANCELLABLE_STATES:
            raise ValueError("expected_state 不可取消")
        _require_generation(
            self.expected_generation,
            label="expected_generation",
        )


def _decode_cancel_body(body: bytes) -> _CancelBody:
    if not body or len(body) > _CANCEL_BODY_MAX_BYTES:
        raise ValueError("cancel body 为空或超限")
    try:
        value = json.loads(
            body,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise ValueError("cancel body 不是严格 JSON") from None
    if not isinstance(value, dict) or set(value) != {
        "expected_generation",
        "expected_state",
    }:
        raise ValueError("cancel body 字段不精确")
    state_value = value["expected_state"]
    if not isinstance(state_value, str):
        raise ValueError("cancel expected_state 非法")
    try:
        state = AgentRunState(state_value)
    except ValueError:
        raise ValueError("cancel expected_state 非法") from None
    generation = value["expected_generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or generation > _MAX_JSON_INTEGER:
        raise ValueError("cancel expected_generation 非法")
    return _CancelBody(
        expected_state=state,
        expected_generation=generation,
    )


def _run_summary(run: AgentRun) -> dict[str, Any]:
    return {
        "cancellable": run.state in _CANCELLABLE_STATES,
        "elapsed": run.elapsed,
        "finished_at": run.finished_at,
        "generation": run.generation,
        "request_id": run.request_id,
        "run_id": run.run_id,
        "started_at": run.started_at,
        "state": run.state.value,
    }


def _run_detail(run: AgentRun) -> dict[str, Any]:
    return {
        **_run_summary(run),
        "group_id": run.group_id,
        "user_id": run.user_id,
    }


def _validate_reader_page(
    records: object,
    *,
    request: AgentRunReadRequest,
) -> tuple[AgentRun, ...]:
    if not isinstance(records, tuple) or len(records) > request.limit:
        raise RuntimeApiConfigurationError("Agent Run reader page 类型或长度非法")
    previous_key: tuple[float, str] | None = None
    if request.before_started_at is not None:
        assert request.before_run_id is not None
        previous_key = (request.before_started_at, request.before_run_id)
    validated: list[AgentRun] = []
    for item in records:
        run = _require_api_run(item, label="Agent Run reader item")
        key = (run.started_at, run.run_id)
        if previous_key is not None and key >= previous_key:
            raise RuntimeApiConfigurationError("Agent Run reader page 未遵守稳定降序 keyset")
        validated.append(run)
        previous_key = key
    return tuple(validated)


class AgentRunApiService:
    """Authenticated H-03 run inspection and cancellation over explicit ports."""

    def __init__(
        self,
        *,
        runs: AgentRunStateReader,
        authenticator: RuntimeApiAuthenticator,
        cancellations: AgentRunCancellationPort,
    ) -> None:
        if not isinstance(runs, AgentRunStateReader):
            raise RuntimeApiConfigurationError("Agent Run API runs 必须实现显式 read port")
        if not isinstance(authenticator, RuntimeApiAuthenticator):
            raise RuntimeApiConfigurationError("Agent Run API authenticator 必须实现 async authenticate()")
        if not isinstance(cancellations, AgentRunCancellationPort):
            raise RuntimeApiConfigurationError("Agent Run API cancellations 必须实现显式 cancellation port")
        self._runs = runs
        self._authenticator = authenticator
        self._cancellations = cancellations

    @property
    def endpoints(self) -> tuple[AgentRunApiEndpoint, ...]:
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

    async def _read_page(
        self,
        query: _PageQuery,
    ) -> tuple[tuple[AgentRun, ...], str | None] | RuntimeApiResponse:
        cursor = query.cursor
        request = AgentRunReadRequest(
            limit=query.limit + 1,
            before_started_at=(None if cursor is None else cursor.started_at),
            before_run_id=(None if cursor is None else cursor.run_id),
        )
        try:
            records = await self._runs.list_runs(request)
            validated = _validate_reader_page(records, request=request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "agent_runs_unavailable")
        has_more = len(validated) > query.limit
        selected = validated[: query.limit]
        next_cursor = None
        if has_more:
            anchor = selected[-1]
            next_cursor = _encode_cursor(
                _AgentRunCursor(
                    started_at=anchor.started_at,
                    run_id=anchor.run_id,
                )
            )
        return selected, next_cursor

    async def _read_run(
        self,
        run_id: str,
    ) -> AgentRun | RuntimeApiResponse | None:
        try:
            run = await self._runs.get_run(run_id)
            if run is None:
                return None
            validated = _require_api_run(run, label="Agent Run reader result")
            if validated.run_id != run_id:
                raise RuntimeApiConfigurationError("Agent Run reader 返回了错误 identity")
            return validated
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "agent_runs_unavailable")

    async def _list_runs(self, query: _PageQuery) -> RuntimeApiResponse:
        page = await self._read_page(query)
        if isinstance(page, RuntimeApiResponse):
            return page
        records, next_cursor = page
        return RuntimeApiResponse(
            status_code=200,
            payload={
                "api_version": RUNTIME_API_VERSION,
                "items": [_run_summary(run) for run in records],
                "next_cursor": next_cursor,
            },
        )

    async def _get_run(self, run_id: str) -> RuntimeApiResponse:
        run = await self._read_run(run_id)
        if isinstance(run, RuntimeApiResponse):
            return run
        if run is None:
            return _error_response(404, "not_found")
        return RuntimeApiResponse(
            status_code=200,
            payload={
                "api_version": RUNTIME_API_VERSION,
                "run": _run_detail(run),
            },
        )

    @staticmethod
    def _cancellation_error(error: BaseException) -> RuntimeApiResponse:
        if isinstance(error, AgentRunCancellationResultUnknownError):
            return RuntimeApiResponse(
                status_code=409,
                payload={
                    "api_version": RUNTIME_API_VERSION,
                    "error": "mutation_result_unknown",
                    "retryable": False,
                },
            )
        if isinstance(error, AgentRunCancellationNotFoundError):
            return _error_response(404, "not_found")
        if isinstance(error, AgentRunCancellationConflictError):
            return _error_response(409, "mutation_precondition_failed")
        return _error_response(503, "mutation_unavailable")

    @staticmethod
    def _valid_cancellation_result(
        result: object,
        *,
        current: AgentRun,
    ) -> bool:
        if not isinstance(result, CancelAgentRunResult):
            return False
        run = result.run
        return (
            result.previous_state is current.state
            and run.run_id == current.run_id
            and run.request_id == current.request_id
            and run.user_id == current.user_id
            and run.group_id == current.group_id
            and run.generation == current.generation
            and run.started_at == current.started_at
            and run.state is AgentRunState.CANCELLED
            and result.cancellation_settled is True
            and result.audit_recorded is True
        )

    async def _cancel_run(
        self,
        principal: RuntimeApiPrincipal,
        run_id: str,
        body: _CancelBody,
    ) -> RuntimeApiResponse:
        current = await self._read_run(run_id)
        if isinstance(current, RuntimeApiResponse):
            return current
        if current is None:
            return _error_response(404, "not_found")
        if (
            current.state not in _CANCELLABLE_STATES
            or current.state is not body.expected_state
            or current.generation != body.expected_generation
        ):
            return _error_response(409, "mutation_precondition_failed")
        command = CancelAgentRunCommand(
            actor_subject=principal.subject,
            run_id=current.run_id,
            expected_state=current.state,
            expected_generation=current.generation,
        )
        try:
            result = await self._cancellations.cancel_run(command)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return self._cancellation_error(error)
        if not self._valid_cancellation_result(result, current=current):
            return _error_response(503, "mutation_unavailable")
        assert isinstance(result, CancelAgentRunResult)
        return RuntimeApiResponse(
            status_code=200,
            payload={
                "api_version": RUNTIME_API_VERSION,
                "audit_recorded": result.audit_recorded,
                "cancellation_settled": result.cancellation_settled,
                "operation": "cancel_agent_run",
                "operation_id": result.operation_id,
                "run": _run_detail(result.run),
            },
        )

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
        cancel_body: _CancelBody | None = None
        if request.method == "GET":
            if request.content_type is not None or request.body:
                return _error_response(400, "body_not_supported")
            if endpoint.path_template == "/agent-runs":
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
                cancel_body = _decode_cancel_body(request.body)
            except (RuntimeApiConfigurationError, ValueError):
                return _error_response(400, "invalid_request")

        try:
            if endpoint.path_template == "/agent-runs":
                assert page_query is not None
                return await self._list_runs(page_query)
            assert resolved.run_id is not None
            if endpoint.path_template == "/agent-runs/{id}":
                return await self._get_run(resolved.run_id)
            assert cancel_body is not None
            return await self._cancel_run(
                principal,
                resolved.run_id,
                cancel_body,
            )
        except asyncio.CancelledError:
            raise
        except (RuntimeApiConfigurationError, TypeError, ValueError):
            return _error_response(503, "agent_runs_unavailable")


__all__ = [
    "AGENT_RUN_API_READ_SCOPE",
    "AGENT_RUN_API_WRITE_SCOPE",
    "AgentRunApiEndpoint",
    "AgentRunApiError",
    "AgentRunApiService",
    "AgentRunCancellationConflictError",
    "AgentRunCancellationError",
    "AgentRunCancellationNotFoundError",
    "AgentRunCancellationPort",
    "AgentRunCancellationResultUnknownError",
    "AgentRunCancellationUnavailableError",
    "AgentRunReadRequest",
    "AgentRunStateReader",
    "CancelAgentRunCommand",
    "CancelAgentRunResult",
]

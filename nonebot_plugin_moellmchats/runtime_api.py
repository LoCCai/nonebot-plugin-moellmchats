from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import math
import re
import secrets
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .runtime_snapshot import RuntimeSnapshot

RUNTIME_API_VERSION = 1
RUNTIME_API_READ_SCOPE = "runtime:read"

_BEARER_TOKEN_RE = re.compile(rb"^[A-Za-z0-9._~-]{32,512}$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}:[a-z][a-z0-9_.-]{0,63}$")
_METHOD_RE = re.compile(r"^[A-Z]{1,16}$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,255}$")
_PAYLOAD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_QUERY_BYTES = 8_192
_MAX_HEADER_COUNT = 128
_MAX_HEADER_BYTES = 65_536
_MAX_REQUEST_BODY_BYTES = 16_384
_MAX_REQUEST_BODY_MESSAGES = 32
_MAX_RESPONSE_BYTES = 16_384
_MAX_RESPONSE_DEPTH = 16
_MAX_RESPONSE_NODES = 8_192
_MAX_RESPONSE_COLLECTION_ITEMS = 512
_MAX_RESPONSE_STRING_BYTES = 8_192
_MAX_JSON_INTEGER = (1 << 63) - 1
_RUNTIME_PATHS = frozenset({"/runtime/generation", "/runtime/status"})

RuntimeApiJsonScalar: TypeAlias = bool | int | float | str | None
RuntimeApiMessage: TypeAlias = dict[str, Any]
RuntimeApiReceive: TypeAlias = Callable[[], Awaitable[RuntimeApiMessage]]
RuntimeApiSend: TypeAlias = Callable[[RuntimeApiMessage], Awaitable[None]]


class RuntimeApiError(RuntimeError):
    """Base error for the detached internal Runtime API boundary."""


class RuntimeApiConfigurationError(RuntimeApiError):
    """A Runtime API component was configured with an unsafe contract."""


class RuntimeApiProtocolError(RuntimeApiError):
    """The ASGI adapter received a scope it cannot safely serve."""


def _validate_scope(value: object) -> str:
    if not isinstance(value, str) or not _SCOPE_RE.fullmatch(value):
        raise RuntimeApiConfigurationError("Runtime API scope 必须是 canonical scope token")
    return value


@dataclass(frozen=True)
class RuntimeApiPrincipal:
    subject: str
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not _PRINCIPAL_RE.fullmatch(self.subject):
            raise RuntimeApiConfigurationError("Runtime API principal subject 非法")
        if not isinstance(self.scopes, frozenset):
            raise RuntimeApiConfigurationError("Runtime API principal scopes 必须是 frozenset")
        normalized = frozenset(_validate_scope(scope) for scope in self.scopes)
        object.__setattr__(self, "scopes", normalized)

    def permits(self, required_scope: str) -> bool:
        return _validate_scope(required_scope) in self.scopes


class RuntimeApiCredential:
    """A parsed bearer credential whose secret never appears in diagnostics."""

    __slots__ = ("_token",)

    def __init__(self, token: bytes) -> None:
        if not isinstance(token, bytes) or not _BEARER_TOKEN_RE.fullmatch(token):
            raise RuntimeApiConfigurationError("Runtime API bearer token 必须是安全的 32～512 字节 token")
        self._token = token

    @classmethod
    def from_authorization_header(cls, header: bytes | None) -> RuntimeApiCredential | None:
        if header is None or not isinstance(header, bytes) or len(header) > 519:
            return None
        scheme, separator, token = header.partition(b" ")
        if separator != b" " or scheme.lower() != b"bearer" or not _BEARER_TOKEN_RE.fullmatch(token):
            return None
        return cls(token)

    def matches(self, expected_token: bytes) -> bool:
        if not isinstance(expected_token, bytes):
            return False
        return secrets.compare_digest(self._token, expected_token)

    def __repr__(self) -> str:
        return "<runtime-api-credential:redacted>"

    def __deepcopy__(self, _memo: dict[int, object]) -> RuntimeApiCredential:
        return self


@dataclass(frozen=True, repr=False)
class RuntimeApiRequest:
    method: str
    path: str
    query_string: bytes = b""
    credential: RuntimeApiCredential | None = None
    content_type: str | None = None
    body: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.method, str) or not _METHOD_RE.fullmatch(self.method):
            raise RuntimeApiProtocolError("Runtime API method 非法")
        if not isinstance(self.path, str) or not _PATH_RE.fullmatch(self.path) or "//" in self.path or self.path.endswith("/"):
            raise RuntimeApiProtocolError("Runtime API path 必须是 canonical ASCII path")
        if not isinstance(self.query_string, bytes) or len(self.query_string) > _MAX_QUERY_BYTES:
            raise RuntimeApiProtocolError("Runtime API query string 非法或超限")
        if self.credential is not None and not isinstance(self.credential, RuntimeApiCredential):
            raise RuntimeApiProtocolError("Runtime API credential 类型非法")
        if self.content_type is not None and (
            not isinstance(self.content_type, str)
            or self.content_type != self.content_type.lower()
            or not re.fullmatch(r"[a-z0-9!#$&^_.+/-]{1,127}", self.content_type)
        ):
            raise RuntimeApiProtocolError("Runtime API content type 非法")
        if not isinstance(self.body, bytes) or len(self.body) > _MAX_REQUEST_BODY_BYTES:
            raise RuntimeApiProtocolError("Runtime API body 非法或超限")

    def __repr__(self) -> str:
        credential = "present" if self.credential is not None else "missing"
        return (
            "RuntimeApiRequest("
            f"method={self.method!r}, path={self.path!r}, "
            f"query_bytes={len(self.query_string)}, body_bytes={len(self.body)}, "
            f"content_type={self.content_type!r}, credential=<{credential}>"
            ")"
        )


def _freeze_json_value(
    value: Any,
    *,
    depth: int,
    node_budget: list[int],
    active_containers: set[int],
) -> Any:
    if depth > _MAX_RESPONSE_DEPTH:
        raise RuntimeApiConfigurationError("Runtime API response JSON 嵌套超过安全上限")
    node_budget[0] += 1
    if node_budget[0] > _MAX_RESPONSE_NODES:
        raise RuntimeApiConfigurationError("Runtime API response JSON 节点数超过安全上限")
    if value is None or type(value) is bool:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not -_MAX_JSON_INTEGER <= value <= _MAX_JSON_INTEGER:
            raise RuntimeApiConfigurationError("Runtime API response integer 超过安全上限")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeApiConfigurationError("Runtime API response 浮点值必须有限")
        return value
    if isinstance(value, str):
        if "\x00" in value:
            raise RuntimeApiConfigurationError("Runtime API response 字符串不得包含 NUL")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise RuntimeApiConfigurationError("Runtime API response 字符串必须是 UTF-8") from None
        if len(encoded) > _MAX_RESPONSE_STRING_BYTES:
            raise RuntimeApiConfigurationError("Runtime API response 字符串超过安全上限")
        return value

    if isinstance(value, Mapping):
        if len(value) > _MAX_RESPONSE_COLLECTION_ITEMS:
            raise RuntimeApiConfigurationError("Runtime API response object 字段数超过安全上限")
        identity = id(value)
        if identity in active_containers:
            raise RuntimeApiConfigurationError("Runtime API response JSON 不得包含循环引用")
        active_containers.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not _PAYLOAD_KEY_RE.fullmatch(key):
                    raise RuntimeApiConfigurationError("Runtime API response 字段名非法")
                if key in frozen:
                    raise RuntimeApiConfigurationError("Runtime API response 字段重复")
                frozen[key] = _freeze_json_value(
                    item,
                    depth=depth + 1,
                    node_budget=node_budget,
                    active_containers=active_containers,
                )
        finally:
            active_containers.remove(identity)
        return MappingProxyType(dict(sorted(frozen.items())))

    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_RESPONSE_COLLECTION_ITEMS:
            raise RuntimeApiConfigurationError("Runtime API response array 项数超过安全上限")
        identity = id(value)
        if identity in active_containers:
            raise RuntimeApiConfigurationError("Runtime API response JSON 不得包含循环引用")
        active_containers.add(identity)
        try:
            return tuple(
                _freeze_json_value(
                    item,
                    depth=depth + 1,
                    node_budget=node_budget,
                    active_containers=active_containers,
                )
                for item in value
            )
        finally:
            active_containers.remove(identity)

    raise RuntimeApiConfigurationError("Runtime API response 只允许有界 JSON value")


def _freeze_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or not payload:
        raise RuntimeApiConfigurationError("Runtime API response payload 必须是非空映射")
    frozen = _freeze_json_value(
        payload,
        depth=0,
        node_budget=[0],
        active_containers=set(),
    )
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise RuntimeApiConfigurationError("Runtime API response payload 必须是 object")
    return frozen


def _mutable_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _mutable_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json_value(item) for item in value]
    return value


@dataclass(frozen=True)
class RuntimeApiResponse:
    status_code: int
    payload: Mapping[str, Any]
    extra_headers: tuple[tuple[bytes, bytes], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status_code, int)
            or isinstance(self.status_code, bool)
            or self.status_code not in {200, 400, 401, 403, 404, 405, 409, 413, 415, 503}
        ):
            raise RuntimeApiConfigurationError("Runtime API response status 非法")
        object.__setattr__(self, "payload", _freeze_payload(self.payload))
        if not isinstance(self.extra_headers, tuple):
            raise RuntimeApiConfigurationError("Runtime API response headers 必须是 tuple")
        normalized: list[tuple[bytes, bytes]] = []
        seen: set[bytes] = set()
        for header in self.extra_headers:
            if (
                not isinstance(header, tuple)
                or len(header) != 2
                or not isinstance(header[0], bytes)
                or not isinstance(header[1], bytes)
            ):
                raise RuntimeApiConfigurationError("Runtime API response header 非法")
            name, value = header
            if (
                not name
                or name != name.lower()
                or any(byte < 33 or byte > 126 for byte in name)
                or any(byte < 32 or byte == 127 for byte in value)
                or name in seen
                or name in {b"cache-control", b"content-length", b"content-type", b"x-content-type-options"}
            ):
                raise RuntimeApiConfigurationError("Runtime API response header 不安全或重复")
            seen.add(name)
            normalized.append((name, value))
        object.__setattr__(self, "extra_headers", tuple(normalized))
        if len(self.body) > _MAX_RESPONSE_BYTES:
            raise RuntimeApiConfigurationError("Runtime API response 超过安全上限")

    @property
    def body(self) -> bytes:
        return json.dumps(
            _mutable_json_value(self.payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def headers(self) -> tuple[tuple[bytes, bytes], ...]:
        body = self.body
        return (
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"content-type", b"application/json; charset=utf-8"),
            (b"x-content-type-options", b"nosniff"),
            *self.extra_headers,
        )


@runtime_checkable
class RuntimeApiAuthenticator(Protocol):
    async def authenticate(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiPrincipal | None: ...


@runtime_checkable
class RuntimeSnapshotReader(Protocol):
    def current(self) -> RuntimeSnapshot | None: ...


@runtime_checkable
class RuntimeApiHandler(Protocol):
    async def handle(self, request: RuntimeApiRequest) -> RuntimeApiResponse: ...


class StaticBearerRuntimeApiAuthenticator:
    """An explicitly provisioned constant-time bearer authenticator."""

    __slots__ = ("_principal", "_token")

    def __init__(
        self,
        *,
        token: str | bytes,
        principal: RuntimeApiPrincipal,
    ) -> None:
        if isinstance(token, str):
            try:
                token_bytes = token.encode("ascii")
            except UnicodeEncodeError:
                raise RuntimeApiConfigurationError("Runtime API bearer token 必须是 ASCII") from None
        elif isinstance(token, bytes):
            token_bytes = bytes(token)
        else:
            raise RuntimeApiConfigurationError("Runtime API bearer token 类型非法")
        if not _BEARER_TOKEN_RE.fullmatch(token_bytes):
            raise RuntimeApiConfigurationError("Runtime API bearer token 必须是安全的 32～512 字节 token")
        if not isinstance(principal, RuntimeApiPrincipal):
            raise RuntimeApiConfigurationError("Runtime API authenticator principal 非法")
        self._token = token_bytes
        self._principal = principal

    async def authenticate(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiPrincipal | None:
        if not isinstance(request, RuntimeApiRequest):
            return None
        credential = request.credential
        if credential is None or not credential.matches(self._token):
            return None
        return self._principal

    def __repr__(self) -> str:
        return f"StaticBearerRuntimeApiAuthenticator(principal={self._principal.subject!r}, token=<redacted>)"


@dataclass(frozen=True)
class RuntimeApiEndpoint:
    method: str
    path: str
    required_scope: str

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise RuntimeApiConfigurationError("H-01 Runtime API endpoint 只允许 GET")
        if self.path not in _RUNTIME_PATHS:
            raise RuntimeApiConfigurationError("H-01 Runtime API endpoint path 非法")
        object.__setattr__(self, "required_scope", _validate_scope(self.required_scope))


_ENDPOINTS = (
    RuntimeApiEndpoint(
        method="GET",
        path="/runtime/generation",
        required_scope=RUNTIME_API_READ_SCOPE,
    ),
    RuntimeApiEndpoint(
        method="GET",
        path="/runtime/status",
        required_scope=RUNTIME_API_READ_SCOPE,
    ),
)


def _error_response(
    status_code: int,
    error: str,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> RuntimeApiResponse:
    return RuntimeApiResponse(
        status_code=status_code,
        payload={"api_version": RUNTIME_API_VERSION, "error": error},
        extra_headers=extra_headers,
    )


def _validated_snapshot(reader: RuntimeSnapshotReader) -> RuntimeSnapshot | None:
    snapshot = reader.current()
    if snapshot is None:
        return None
    if not isinstance(snapshot, RuntimeSnapshot):
        raise RuntimeApiError("runtime snapshot reader 返回了非法对象")
    if (
        not isinstance(snapshot.generation, int)
        or isinstance(snapshot.generation, bool)
        or snapshot.generation <= 0
        or snapshot.generation > _MAX_JSON_INTEGER
        or snapshot.generated_state_revision > _MAX_JSON_INTEGER
        or not isinstance(snapshot.reloaded_at, (int, float))
        or isinstance(snapshot.reloaded_at, bool)
        or not math.isfinite(snapshot.reloaded_at)
        or snapshot.reloaded_at < 0
    ):
        raise RuntimeApiError("runtime snapshot identity 非法")
    tool_snapshot = snapshot.tool_snapshot
    if tool_snapshot is not None and (
        tool_snapshot.generation != snapshot.generation
        or tool_snapshot.generated_state_revision != snapshot.generated_state_revision
        or tool_snapshot.generated_state_digest != snapshot.generated_state_digest
        or tool_snapshot.generated_active != snapshot.generated_active
    ):
        raise RuntimeApiError("runtime/tool snapshot identity 不一致")
    return snapshot


class RuntimeApiService:
    """Authenticated, read-only H-01 service over the current runtime snapshot."""

    def __init__(
        self,
        *,
        snapshots: RuntimeSnapshotReader,
        authenticator: RuntimeApiAuthenticator,
    ) -> None:
        if not isinstance(snapshots, RuntimeSnapshotReader):
            raise RuntimeApiConfigurationError("Runtime API snapshots 必须实现 current()")
        if not isinstance(authenticator, RuntimeApiAuthenticator):
            raise RuntimeApiConfigurationError("Runtime API authenticator 必须实现 async authenticate()")
        self._snapshots = snapshots
        self._authenticator = authenticator

    @property
    def endpoints(self) -> tuple[RuntimeApiEndpoint, ...]:
        return _ENDPOINTS

    async def handle(self, request: RuntimeApiRequest) -> RuntimeApiResponse:
        if not isinstance(request, RuntimeApiRequest):
            return _error_response(400, "invalid_request")
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
        if request.query_string:
            return _error_response(400, "query_not_supported")
        if request.content_type is not None or request.body:
            return _error_response(400, "body_not_supported")

        try:
            snapshot = _validated_snapshot(self._snapshots)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _error_response(503, "runtime_snapshot_unavailable")
        if snapshot is None:
            return _error_response(503, "runtime_snapshot_unavailable")
        if request.path == "/runtime/generation":
            return RuntimeApiResponse(
                status_code=200,
                payload={
                    "api_version": RUNTIME_API_VERSION,
                    "generated_state_revision": snapshot.generated_state_revision,
                    "generation": snapshot.generation,
                    "reloaded_at": float(snapshot.reloaded_at),
                },
            )
        return RuntimeApiResponse(
            status_code=200,
            payload={
                "api_version": RUNTIME_API_VERSION,
                "generated_active_count": len(snapshot.generated_active),
                "generated_state_revision": snapshot.generated_state_revision,
                "generation": snapshot.generation,
                "model_catalog_loaded": snapshot.model_state is not None,
                "reloaded_at": float(snapshot.reloaded_at),
                "status": "ready",
                "tool_catalog_loaded": snapshot.tool_snapshot is not None,
            },
        )


@dataclass(frozen=True)
class _RuntimeApiRequestHeaders:
    credential: RuntimeApiCredential | None
    content_type: str | None
    content_length: int | None
    malformed: bool = False


def _request_headers(headers: object) -> _RuntimeApiRequestHeaders:
    if not isinstance(headers, Sequence) or isinstance(headers, (bytes, bytearray, str)):
        return _RuntimeApiRequestHeaders(None, None, None, True)
    if len(headers) > _MAX_HEADER_COUNT:
        return _RuntimeApiRequestHeaders(None, None, None, True)
    total_bytes = 0
    authorization_values: list[bytes] = []
    content_type_values: list[bytes] = []
    content_length_values: list[bytes] = []
    for header in headers:
        if not isinstance(header, Sequence) or isinstance(header, (bytes, bytearray, str)) or len(header) != 2:
            return _RuntimeApiRequestHeaders(None, None, None, True)
        name, value = header
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            return _RuntimeApiRequestHeaders(None, None, None, True)
        total_bytes += len(name) + len(value)
        if total_bytes > _MAX_HEADER_BYTES:
            return _RuntimeApiRequestHeaders(None, None, None, True)
        normalized_name = name.lower()
        if normalized_name == b"authorization":
            authorization_values.append(value)
        elif normalized_name == b"content-type":
            content_type_values.append(value)
        elif normalized_name == b"content-length":
            content_length_values.append(value)
        elif normalized_name == b"content-encoding" and value.lower() != b"identity":
            return _RuntimeApiRequestHeaders(None, None, None, True)
    if len(authorization_values) > 1 or len(content_type_values) > 1 or len(content_length_values) > 1:
        return _RuntimeApiRequestHeaders(None, None, None, True)
    header = authorization_values[0] if authorization_values else None
    content_type: str | None = None
    if content_type_values:
        try:
            content_type = content_type_values[0].decode("ascii").lower()
        except UnicodeDecodeError:
            return _RuntimeApiRequestHeaders(None, None, None, True)
        if not re.fullmatch(r"[a-z0-9!#$&^_.+/-]{1,127}", content_type):
            return _RuntimeApiRequestHeaders(None, None, None, True)
    content_length: int | None = None
    if content_length_values:
        raw_length = content_length_values[0]
        if not re.fullmatch(rb"(?:0|[1-9][0-9]{0,5})", raw_length):
            return _RuntimeApiRequestHeaders(None, None, None, True)
        content_length = int(raw_length)
    return _RuntimeApiRequestHeaders(
        RuntimeApiCredential.from_authorization_header(header),
        content_type,
        content_length,
    )


async def _receive_request_body(
    receive: RuntimeApiReceive,
) -> tuple[bytes, int | None]:
    chunks: list[bytes] = []
    size = 0
    for _ in range(_MAX_REQUEST_BODY_MESSAGES):
        try:
            message = await receive()
        except asyncio.CancelledError:
            raise
        except Exception:
            return b"", 400
        if not isinstance(message, Mapping) or message.get("type") != "http.request":
            return b"", 400
        chunk = message.get("body", b"")
        more_body = message.get("more_body", False)
        if not isinstance(chunk, bytes) or type(more_body) is not bool:
            return b"", 400
        size += len(chunk)
        if size > _MAX_REQUEST_BODY_BYTES:
            return b"", 413
        chunks.append(chunk)
        if not more_body:
            return b"".join(chunks), None
    return b"", 413


class RuntimeApiASGIApp:
    """A detached ASGI adapter; callers must explicitly mount this object."""

    def __init__(self, *, service: RuntimeApiHandler) -> None:
        if not isinstance(service, RuntimeApiHandler):
            raise RuntimeApiConfigurationError("Runtime API ASGI service 非法")
        self._service = service

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: RuntimeApiReceive,
        send: RuntimeApiSend,
    ) -> None:
        if not isinstance(scope, Mapping) or scope.get("type") != "http":
            raise RuntimeApiProtocolError("Runtime API ASGI adapter 只接受 HTTP scope")
        request_headers = _request_headers(scope.get("headers", ()))
        if request_headers.malformed:
            response = _error_response(400, "invalid_request")
        elif request_headers.content_length is not None and request_headers.content_length > _MAX_REQUEST_BODY_BYTES:
            response = _error_response(413, "request_too_large")
        else:
            method = scope.get("method")
            path = scope.get("path")
            if not isinstance(method, str) or not isinstance(path, str):
                response = _error_response(400, "invalid_request")
            else:
                body, body_error = await _receive_request_body(receive)
                if body_error is not None:
                    response = _error_response(
                        body_error,
                        "request_too_large" if body_error == 413 else "invalid_request",
                    )
                elif request_headers.content_length is not None and request_headers.content_length != len(body):
                    response = _error_response(400, "invalid_request")
                else:
                    try:
                        request = RuntimeApiRequest(
                            method=method,
                            path=path,
                            query_string=scope.get("query_string", b""),
                            credential=request_headers.credential,
                            content_type=request_headers.content_type,
                            body=body,
                        )
                    except RuntimeApiProtocolError:
                        response = _error_response(400, "invalid_request")
                    else:
                        response = await self._service.handle(request)
        await send(
            {
                "type": "http.response.start",
                "status": response.status_code,
                "headers": list(response.headers),
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": response.body,
                "more_body": False,
            }
        )

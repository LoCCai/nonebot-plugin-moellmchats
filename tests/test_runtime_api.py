from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import importlib
import json
from typing import Any

import pytest

from nonebot_plugin_moellmchats.runtime_api import (
    RUNTIME_API_READ_SCOPE,
    RUNTIME_API_VERSION,
    RuntimeApiASGIApp,
    RuntimeApiConfigurationError,
    RuntimeApiCredential,
    RuntimeApiEndpoint,
    RuntimeApiPrincipal,
    RuntimeApiProtocolError,
    RuntimeApiRequest,
    RuntimeApiResponse,
    RuntimeApiService,
    StaticBearerRuntimeApiAuthenticator,
)
from nonebot_plugin_moellmchats.runtime_snapshot import (
    RuntimeSnapshot,
    RuntimeSnapshotStore,
)
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot

_TOKEN = "a" * 32
_WRONG_TOKEN = "b" * 32


def _snapshot(
    generation: int = 1,
    *,
    reloaded_at: float = 10.5,
    generated_state_revision: int = 0,
    generated_state_digest: str = "",
    generated_active: dict[str, str] | None = None,
    tool_snapshot: ToolSnapshot | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config={"secret": "must-not-leak"},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=tool_snapshot,
        emotions=(),
        reloaded_at=reloaded_at,
        generated_state_revision=generated_state_revision,
        generated_state_digest=generated_state_digest,
        generated_active={} if generated_active is None else generated_active,
    )


def _credential(token: str = _TOKEN) -> RuntimeApiCredential:
    credential = RuntimeApiCredential.from_authorization_header(f"Bearer {token}".encode("ascii"))
    assert credential is not None
    return credential


def _principal(*scopes: str) -> RuntimeApiPrincipal:
    return RuntimeApiPrincipal(
        subject="runtime-admin",
        scopes=frozenset(scopes),
    )


class _SnapshotReader:
    def __init__(self, snapshot: RuntimeSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def current(self) -> RuntimeSnapshot | None:
        self.calls += 1
        return self.snapshot


class _FixedAuthenticator:
    def __init__(self, principal: RuntimeApiPrincipal | None) -> None:
        self.principal = principal
        self.calls = 0

    async def authenticate(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiPrincipal | None:
        assert isinstance(request, RuntimeApiRequest)
        self.calls += 1
        return self.principal


class _InvalidAuthenticator:
    async def authenticate(self, request: RuntimeApiRequest) -> object:
        assert isinstance(request, RuntimeApiRequest)
        return object()


def _service(
    snapshot: RuntimeSnapshot | None = None,
    *,
    principal: RuntimeApiPrincipal | None = None,
) -> RuntimeApiService:
    reader = _SnapshotReader(_snapshot() if snapshot is None else snapshot)
    authenticator = StaticBearerRuntimeApiAuthenticator(
        token=_TOKEN,
        principal=principal or _principal(RUNTIME_API_READ_SCOPE),
    )
    return RuntimeApiService(
        snapshots=reader,
        authenticator=authenticator,
    )


def _request(
    path: str = "/runtime/status",
    *,
    method: str = "GET",
    token: str | None = _TOKEN,
    query_string: bytes = b"",
) -> RuntimeApiRequest:
    return RuntimeApiRequest(
        method=method,
        path=path,
        query_string=query_string,
        credential=None if token is None else _credential(token),
    )


def _payload(response: RuntimeApiResponse) -> dict[str, Any]:
    return json.loads(response.body)


async def _call_asgi(
    app: RuntimeApiASGIApp,
    *,
    method: str = "GET",
    path: str = "/runtime/status",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[bytes, bytes], dict[str, Any], list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string,
            "headers": ([(b"authorization", f"Bearer {_TOKEN}".encode("ascii"))] if headers is None else headers),
        },
        receive,
        send,
    )
    assert len(sent) == 2
    start, body = sent
    assert start["type"] == "http.response.start"
    assert body == {
        "type": "http.response.body",
        "body": body["body"],
        "more_body": False,
    }
    return (
        start["status"],
        dict(start["headers"]),
        json.loads(body["body"]),
        sent,
    )


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "a" * 513,
        "contains space" + "a" * 32,
        "包含非 ASCII" + "a" * 32,
        b"a" * 31,
        bytearray(b"a" * 32),
    ],
)
def test_static_bearer_authenticator_rejects_unsafe_tokens(token: object) -> None:
    with pytest.raises(RuntimeApiConfigurationError, match="token"):
        StaticBearerRuntimeApiAuthenticator(
            token=token,  # type: ignore[arg-type]
            principal=_principal(RUNTIME_API_READ_SCOPE),
        )


def test_credential_parsing_and_diagnostics_never_expose_token() -> None:
    credential = _credential()
    request = _request()
    authenticator = StaticBearerRuntimeApiAuthenticator(
        token=_TOKEN,
        principal=_principal(RUNTIME_API_READ_SCOPE),
    )

    assert credential.matches(_TOKEN.encode("ascii"))
    assert not credential.matches(_WRONG_TOKEN.encode("ascii"))
    assert _TOKEN not in repr(credential)
    assert _TOKEN not in repr(request)
    assert _TOKEN not in repr(authenticator)


@pytest.mark.parametrize(
    "header",
    [
        None,
        b"",
        b"Basic " + b"a" * 32,
        b"Bearer",
        b"Bearer  " + b"a" * 32,
        b"Bearer " + b"a" * 31,
        b"Bearer " + b"a" * 513,
        b"Bearer " + b"a" * 32 + b" ",
    ],
)
def test_credential_parser_rejects_missing_or_noncanonical_header(
    header: bytes | None,
) -> None:
    assert RuntimeApiCredential.from_authorization_header(header) is None


@pytest.mark.parametrize(
    ("subject", "scopes"),
    [
        ("", frozenset()),
        ("bad subject", frozenset()),
        ("ok", {RUNTIME_API_READ_SCOPE}),
        ("ok", frozenset({"BAD"})),
    ],
)
def test_principal_rejects_unsafe_identity_or_scopes(
    subject: str,
    scopes: object,
) -> None:
    with pytest.raises(RuntimeApiConfigurationError):
        RuntimeApiPrincipal(
            subject=subject,
            scopes=scopes,  # type: ignore[arg-type]
        )


def test_endpoint_contract_is_frozen_and_exact() -> None:
    service = _service()
    assert service.endpoints == (
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
    with pytest.raises(FrozenInstanceError):
        service.endpoints[0].path = "/changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_status_returns_only_bounded_safe_runtime_metadata() -> None:
    digest = "d" * 64
    response = await _service(
        _snapshot(
            generation=7,
            reloaded_at=12.25,
            generated_state_revision=3,
            generated_state_digest="e" * 64,
            generated_active={"weather": digest},
        )
    ).handle(_request())

    assert response.status_code == 200
    assert _payload(response) == {
        "api_version": RUNTIME_API_VERSION,
        "generated_active_count": 1,
        "generated_state_revision": 3,
        "generation": 7,
        "model_catalog_loaded": False,
        "reloaded_at": 12.25,
        "status": "ready",
        "tool_catalog_loaded": False,
    }
    body_text = response.body.decode("utf-8")
    assert "secret" not in body_text
    assert digest not in body_text
    assert "weather" not in body_text


@pytest.mark.asyncio
async def test_generation_endpoint_has_a_smaller_stable_contract() -> None:
    response = await _service(
        _snapshot(
            generation=9,
            reloaded_at=20,
            generated_state_revision=4,
        )
    ).handle(_request("/runtime/generation"))

    assert response.status_code == 200
    assert _payload(response) == {
        "api_version": RUNTIME_API_VERSION,
        "generated_state_revision": 4,
        "generation": 9,
        "reloaded_at": 20.0,
    }


@pytest.mark.asyncio
async def test_service_reads_current_snapshot_once_per_successful_request() -> None:
    reader = _SnapshotReader(_snapshot(generation=1))
    service = RuntimeApiService(
        snapshots=reader,
        authenticator=StaticBearerRuntimeApiAuthenticator(
            token=_TOKEN,
            principal=_principal(RUNTIME_API_READ_SCOPE),
        ),
    )

    first = await service.handle(_request("/runtime/generation"))
    reader.snapshot = _snapshot(generation=2)
    second = await service.handle(_request("/runtime/generation"))

    assert _payload(first)["generation"] == 1
    assert _payload(second)["generation"] == 2
    assert reader.calls == 2


@pytest.mark.asyncio
async def test_missing_or_wrong_bearer_is_indistinguishable_and_precedes_snapshot_read() -> None:
    reader = _SnapshotReader(_snapshot())
    service = RuntimeApiService(
        snapshots=reader,
        authenticator=StaticBearerRuntimeApiAuthenticator(
            token=_TOKEN,
            principal=_principal(RUNTIME_API_READ_SCOPE),
        ),
    )

    missing = await service.handle(_request(token=None))
    wrong = await service.handle(_request(token=_WRONG_TOKEN))

    assert missing.status_code == wrong.status_code == 401
    assert missing.body == wrong.body
    assert _payload(missing) == {
        "api_version": RUNTIME_API_VERSION,
        "error": "unauthorized",
    }
    assert dict(missing.extra_headers) == {b"www-authenticate": b'Bearer realm="moellm-runtime"'}
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_authenticated_principal_requires_runtime_read_scope() -> None:
    reader = _SnapshotReader(_snapshot())
    service = RuntimeApiService(
        snapshots=reader,
        authenticator=_FixedAuthenticator(_principal("runtime:write")),
    )

    response = await service.handle(_request())

    assert response.status_code == 403
    assert _payload(response)["error"] == "forbidden"
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_unknown_path_method_and_query_fail_before_snapshot_read() -> None:
    reader = _SnapshotReader(_snapshot())
    service = RuntimeApiService(
        snapshots=reader,
        authenticator=_FixedAuthenticator(_principal(RUNTIME_API_READ_SCOPE)),
    )

    unknown = await service.handle(_request("/runtime/unknown"))
    method = await service.handle(_request(method="POST"))
    query = await service.handle(_request(query_string=b"verbose=true"))

    assert unknown.status_code == 404
    assert method.status_code == 405
    assert dict(method.extra_headers) == {b"allow": b"GET"}
    assert query.status_code == 400
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_missing_or_invalid_snapshot_returns_fixed_unavailable_response() -> None:
    missing_reader = _SnapshotReader(None)
    missing = RuntimeApiService(
        snapshots=missing_reader,
        authenticator=_FixedAuthenticator(_principal(RUNTIME_API_READ_SCOPE)),
    )
    invalid_reader = _SnapshotReader(_snapshot(generation=0))
    invalid = RuntimeApiService(
        snapshots=invalid_reader,
        authenticator=_FixedAuthenticator(_principal(RUNTIME_API_READ_SCOPE)),
    )

    missing_response = await missing.handle(_request())
    invalid_response = await invalid.handle(_request())

    assert missing_response.status_code == invalid_response.status_code == 503
    assert missing_response.body == invalid_response.body
    assert _payload(missing_response)["error"] == "runtime_snapshot_unavailable"


@pytest.mark.asyncio
async def test_runtime_and_tool_generation_mismatch_fails_closed() -> None:
    tool_snapshot = ToolSnapshot(
        generation=2,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=frozenset(),
    )
    response = await _service(_snapshot(generation=1, tool_snapshot=tool_snapshot)).handle(_request())

    assert response.status_code == 503
    assert _payload(response)["error"] == "runtime_snapshot_unavailable"


@pytest.mark.asyncio
async def test_authenticator_failure_is_redacted_and_invalid_result_fails_closed() -> None:
    secret = "credential-secret-must-not-leak"

    class FailingAuthenticator:
        async def authenticate(
            self,
            request: RuntimeApiRequest,
        ) -> RuntimeApiPrincipal | None:
            assert isinstance(request, RuntimeApiRequest)
            raise RuntimeError(secret)

    reader = _SnapshotReader(_snapshot())
    failed = RuntimeApiService(
        snapshots=reader,
        authenticator=FailingAuthenticator(),
    )
    invalid = RuntimeApiService(
        snapshots=reader,
        authenticator=_InvalidAuthenticator(),  # type: ignore[arg-type]
    )

    failed_response = await failed.handle(_request())
    invalid_response = await invalid.handle(_request())

    assert failed_response.status_code == invalid_response.status_code == 503
    assert failed_response.body == invalid_response.body
    assert secret.encode() not in failed_response.body
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_authenticator_cancellation_propagates_without_snapshot_read() -> None:
    class CancelledAuthenticator:
        async def authenticate(
            self,
            request: RuntimeApiRequest,
        ) -> RuntimeApiPrincipal | None:
            assert isinstance(request, RuntimeApiRequest)
            raise asyncio.CancelledError

    reader = _SnapshotReader(_snapshot())
    service = RuntimeApiService(
        snapshots=reader,
        authenticator=CancelledAuthenticator(),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.handle(_request())
    assert reader.calls == 0


def test_response_detaches_payload_and_emits_security_headers() -> None:
    payload = {"api_version": 1, "status": "ready"}
    response = RuntimeApiResponse(status_code=200, payload=payload)
    payload["status"] = "tampered"

    assert _payload(response)["status"] == "ready"
    with pytest.raises(TypeError):
        response.payload["status"] = "changed"  # type: ignore[index]
    headers = dict(response.headers)
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"content-type"] == b"application/json; charset=utf-8"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert int(headers[b"content-length"]) == len(response.body)
    assert b"access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"method": "get", "path": "/runtime/status"},
        {"method": "GET", "path": "/runtime/status/"},
        {"method": "GET", "path": "/runtime//status"},
        {"method": "GET", "path": "/运行/status"},
        {
            "method": "GET",
            "path": "/runtime/status",
            "query_string": b"a" * 8_193,
        },
    ],
)
def test_request_rejects_noncanonical_transport_fields(
    request_kwargs: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeApiProtocolError):
        RuntimeApiRequest(**request_kwargs)


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"status_code": 201, "payload": {"api_version": 1}},
        {"status_code": 200, "payload": {}},
        {"status_code": 200, "payload": {"Bad-Key": 1}},
        {"status_code": 200, "payload": {"value": float("nan")}},
        {
            "status_code": 200,
            "payload": {"api_version": 1},
            "extra_headers": ((b"content-type", b"text/plain"),),
        },
        {
            "status_code": 200,
            "payload": {"api_version": 1},
            "extra_headers": ((b"x-test", b"bad\nvalue"),),
        },
    ],
)
def test_response_rejects_unsafe_contracts(
    response_kwargs: dict[str, Any],
) -> None:
    with pytest.raises(RuntimeApiConfigurationError):
        RuntimeApiResponse(**response_kwargs)


@pytest.mark.asyncio
async def test_asgi_adapter_serves_authenticated_status_with_canonical_json() -> None:
    app = RuntimeApiASGIApp(service=_service(_snapshot(generation=5)))

    status, headers, payload, sent = await _call_asgi(app)

    assert status == 200
    assert payload["generation"] == 5
    assert payload["status"] == "ready"
    assert headers[b"cache-control"] == b"no-store"
    assert int(headers[b"content-length"]) == len(sent[1]["body"])


@pytest.mark.asyncio
async def test_asgi_adapter_treats_authorization_name_case_insensitively() -> None:
    app = RuntimeApiASGIApp(service=_service())

    status, _headers, payload, _sent = await _call_asgi(
        app,
        headers=[
            (b"x-test", b"ok"),
            (b"Authorization", f"Bearer {_TOKEN}".encode("ascii")),
        ],
    )

    assert status == 200
    assert payload["status"] == "ready"


@pytest.mark.asyncio
async def test_asgi_adapter_rejects_duplicate_or_malformed_headers() -> None:
    app = RuntimeApiASGIApp(service=_service())
    bearer = f"Bearer {_TOKEN}".encode("ascii")

    duplicate = await _call_asgi(
        app,
        headers=[(b"authorization", bearer), (b"Authorization", bearer)],
    )
    malformed = await _call_asgi(
        app,
        headers=[(b"authorization", bearer, b"extra")],  # type: ignore[list-item]
    )

    assert duplicate[0] == malformed[0] == 400
    assert (
        duplicate[2]
        == malformed[2]
        == {
            "api_version": RUNTIME_API_VERSION,
            "error": "invalid_request",
        }
    )


@pytest.mark.asyncio
async def test_asgi_adapter_rejects_wrong_bearer_without_echoing_it() -> None:
    app = RuntimeApiASGIApp(service=_service())
    wrong_header = f"Bearer {_WRONG_TOKEN}".encode("ascii")

    status, headers, payload, sent = await _call_asgi(
        app,
        headers=[(b"authorization", wrong_header)],
    )

    assert status == 401
    assert payload["error"] == "unauthorized"
    assert headers[b"www-authenticate"] == b'Bearer realm="moellm-runtime"'
    assert _WRONG_TOKEN.encode() not in sent[1]["body"]


@pytest.mark.asyncio
async def test_asgi_adapter_rejects_non_http_scope() -> None:
    app = RuntimeApiASGIApp(service=_service())

    async def receive() -> dict[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(_message: dict[str, Any]) -> None:
        raise AssertionError("non-http scope must not send a response")

    with pytest.raises(RuntimeApiProtocolError, match="HTTP"):
        await app({"type": "lifespan"}, receive, send)


def test_runtime_api_module_has_no_global_service_authenticator_or_asgi_app() -> None:
    module = importlib.import_module("nonebot_plugin_moellmchats.runtime_api")

    assert not any(
        isinstance(
            value,
            (
                RuntimeApiASGIApp,
                RuntimeApiService,
                StaticBearerRuntimeApiAuthenticator,
            ),
        )
        for value in vars(module).values()
    )


def test_service_accepts_existing_snapshot_store_without_mutating_it() -> None:
    store = RuntimeSnapshotStore()
    snapshot = _snapshot()
    store.publish(snapshot)

    service = RuntimeApiService(
        snapshots=store,
        authenticator=StaticBearerRuntimeApiAuthenticator(
            token=_TOKEN,
            principal=_principal(RUNTIME_API_READ_SCOPE),
        ),
    )

    assert service.endpoints
    assert store.current() is snapshot

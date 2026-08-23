from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import importlib
from typing import Any

import pytest

from nonebot_plugin_moellmchats import web_admin as web_admin_module
from nonebot_plugin_moellmchats.web_admin import (
    WEB_ADMIN_DEFAULT_BASE_PATH,
    WEB_ADMIN_VERSION,
    WebAdminASGIApp,
    WebAdminAsset,
    WebAdminConfig,
    WebAdminConfigurationError,
    WebAdminProtocolError,
    WebAdminRequest,
    WebAdminResponse,
    WebAdminService,
)


async def _call_asgi(
    app: WebAdminASGIApp,
    *,
    method: str = "GET",
    path: str = "/admin",
    query_string: bytes = b"",
    headers: object = (),
    body_messages: list[dict[str, Any]] | None = None,
    raw_path: object = ...,
) -> tuple[int, dict[bytes, bytes], bytes, list[dict[str, Any]], int]:
    sent: list[dict[str, Any]] = []
    messages = list(body_messages or [{"type": "http.request", "body": b"", "more_body": False}])
    receive_calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        receive_calls += 1
        if not messages:
            raise AssertionError("ASGI app requested an unexpected body message")
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": headers,
    }
    scope["raw_path"] = path.encode("ascii") if raw_path is ... else raw_path
    await app(scope, receive, send)
    assert len(sent) == 2
    start, body = sent
    assert start["type"] == "http.response.start"
    assert body["type"] == "http.response.body"
    assert body["more_body"] is False
    return start["status"], dict(start["headers"]), body["body"], sent, receive_calls


def _asset(service: WebAdminService, suffix: str) -> WebAdminAsset:
    path = f"{service.config.base_path}{suffix}"
    return next(asset for asset in service.assets if asset.path == path)


def test_default_config_and_assets_are_frozen_bounded_and_secret_free() -> None:
    config = WebAdminConfig()
    service = WebAdminService(config=config)

    assert WEB_ADMIN_VERSION == 1
    assert WEB_ADMIN_DEFAULT_BASE_PATH == "/admin"
    assert config.base_path == "/admin"
    assert config.api_prefix == ""
    assert service.config is config
    assert isinstance(service.assets, tuple)
    assert [asset.path for asset in service.assets] == ["/admin", "/admin/app.js", "/admin/styles.css"]
    assert all(0 < len(asset.body) <= 32_768 for asset in service.assets)
    assert all(b"\x00" not in asset.body for asset in service.assets)
    assert all("body_bytes=" in repr(asset) and "Bearer token" not in repr(asset) for asset in service.assets)

    with pytest.raises(FrozenInstanceError):
        config.base_path = "/changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        service.assets[0].path = "/changed"  # type: ignore[misc]


def test_custom_canonical_paths_are_rendered_without_placeholders_or_external_origins() -> None:
    service = WebAdminService(config=WebAdminConfig(base_path="/ops/runtime-admin", api_prefix="/internal/v1"))
    html = _asset(service, "").body.decode("utf-8")
    javascript = _asset(service, "/app.js").body.decode("utf-8")

    assert 'href="/ops/runtime-admin/styles.css"' in html
    assert 'src="/ops/runtime-admin/app.js"' in html
    assert 'const API_PREFIX = "/internal/v1";' in javascript
    assert "__BASE_PATH__" not in html
    assert "__API_PREFIX_JSON__" not in javascript
    assert "https://" not in html + javascript
    assert "http://" not in html + javascript


@pytest.mark.parametrize(
    "base_path",
    [
        "",
        "/",
        "admin",
        "/admin/",
        "/admin//panel",
        "/admin/.",
        "/admin/..",
        "/admin/%2fpanel",
        "/admin?token=secret",
        "/管理",
        "/" + "a" * 257,
        1,
        b"/admin",
    ],
)
def test_config_rejects_unsafe_base_paths(base_path: object) -> None:
    with pytest.raises(WebAdminConfigurationError, match="base_path"):
        WebAdminConfig(base_path=base_path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "api_prefix",
    [
        "/",
        "api",
        "/api/",
        "/api//v1",
        "/api/.",
        "/api/..",
        "//api.example.invalid",
        "https://api.example.invalid",
        "/api?token=secret",
        "/内部",
        "/" + "a" * 257,
        1,
        b"/api",
    ],
)
def test_config_rejects_unsafe_api_prefixes(api_prefix: object) -> None:
    with pytest.raises(WebAdminConfigurationError, match="api_prefix"):
        WebAdminConfig(api_prefix=api_prefix)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"path": "/asset/..", "content_type": "text/html; charset=utf-8", "body": b"ok"}, "path"),
        ({"path": "/asset", "content_type": "application/json", "body": b"ok"}, "content type"),
        ({"path": "/asset", "content_type": "text/html; charset=utf-8", "body": b""}, "body"),
        ({"path": "/asset", "content_type": "text/html; charset=utf-8", "body": b"a" * 32_769}, "body"),
        ({"path": "/asset", "content_type": "text/html; charset=utf-8", "body": b"a\x00b"}, "NUL"),
        ({"path": "/asset", "content_type": "text/html; charset=utf-8", "body": b"\xff"}, "UTF-8"),
    ],
)
def test_asset_contract_rejects_unsafe_values(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(WebAdminConfigurationError, match=message):
        WebAdminAsset(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "get", "path": "/admin"},
        {"method": "G ET", "path": "/admin"},
        {"method": "GET", "path": ""},
        {"method": "GET", "path": "/"},
        {"method": "GET", "path": "admin"},
        {"method": "GET", "path": "/admin/"},
        {"method": "GET", "path": "/admin//app.js"},
        {"method": "GET", "path": "/admin/.."},
        {"method": "GET", "path": "/admin%2fapp.js"},
        {"method": "GET", "path": "/管理"},
        {"method": "GET", "path": "/admin", "query_string": b"a" * 1_025},
        {"method": "GET", "path": "/admin", "query_string": "token=secret"},
        {"method": "GET", "path": "/admin", "body": b"a" * 1_025},
        {"method": "GET", "path": "/admin", "body": "body"},
    ],
)
def test_request_contract_rejects_noncanonical_or_oversized_input(kwargs: dict[str, object]) -> None:
    with pytest.raises(WebAdminProtocolError):
        WebAdminRequest(**kwargs)  # type: ignore[arg-type]


def test_request_repr_never_contains_query_or_body_content() -> None:
    request = WebAdminRequest(
        method="GET",
        path="/admin",
        query_string=b"token=must-not-leak",
        body=b"must-not-leak",
    )

    assert "must-not-leak" not in repr(request)
    assert "query_bytes=19" in repr(request)
    assert "body_bytes=13" in repr(request)


def test_assets_use_external_scripts_strict_dom_sinks_and_memory_only_credentials() -> None:
    service = WebAdminService()
    html = _asset(service, "").body.decode("utf-8")
    javascript = _asset(service, "/app.js").body.decode("utf-8")
    css = _asset(service, "/styles.css").body.decode("utf-8")

    assert '<script src="/admin/app.js" defer></script>' in html
    assert "<script>" not in html
    assert "<style" not in html
    assert 'autocomplete="off"' in html
    assert 'type="password"' in html
    assert "审批、激活或取消" in html
    assert "MCP 详情和 Token 明细尚未" in html

    for path in (
        "/runtime/status",
        "/agent-runs?limit=20",
        "/tool-bundles?limit=20",
        "/tool-drafts?limit=20",
        "/tools?limit=20",
        "/models?limit=20",
        "/metrics",
    ):
        assert path in javascript
    for forbidden_path in ("/approve", "/activate", "/cancel"):
        assert forbidden_path not in javascript
    for forbidden_source in (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "eval(",
        "new Function",
        "console.",
        "location.search",
        "location.hash",
        "sendBeacon",
        "WebSocket",
        "EventSource",
    ):
        assert forbidden_source not in javascript
    assert 'method: "GET"' in javascript
    assert 'credentials: "omit"' in javascript
    assert 'mode: "same-origin"' in javascript
    assert 'redirect: "error"' in javascript
    assert 'referrerPolicy: "no-referrer"' in javascript
    assert 'tokenInput.value = "";' in javascript
    assert 'window.addEventListener("pagehide", disconnect)' in javascript
    assert "Number.isSafeInteger" in javascript
    assert "runtime generation mismatch" in javascript
    assert "validateJsonValue(payload" in javascript
    assert "url(" not in css
    assert "@import" not in css


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/admin", "text/html; charset=utf-8"),
        ("/admin/app.js", "application/javascript; charset=utf-8"),
        ("/admin/styles.css", "text/css; charset=utf-8"),
    ],
)
async def test_service_serves_only_the_three_bounded_assets(path: str, content_type: str) -> None:
    service = WebAdminService()
    response = await service.handle(WebAdminRequest(method="GET", path=path))

    assert response.status_code == 200
    headers = dict(response.headers)
    assert headers[b"content-type"] == content_type.encode("ascii")
    assert headers[b"content-length"] == str(len(response.body)).encode("ascii")
    assert response.body == next(asset.body for asset in service.assets if asset.path == path)


@pytest.mark.asyncio
async def test_service_head_preserves_asset_length_without_returning_content() -> None:
    service = WebAdminService()
    asset = _asset(service, "/app.js")
    response = await service.handle(WebAdminRequest(method="HEAD", path=asset.path))

    assert response.status_code == 200
    assert response.body == b""
    assert dict(response.headers)[b"content-length"] == str(len(asset.body)).encode("ascii")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admin_request", "status", "message"),
    [
        (WebAdminRequest(method="POST", path="/admin"), 405, b"method_not_allowed"),
        (WebAdminRequest(method="GET", path="/admin", query_string=b"token=secret"), 400, b"query_not_supported"),
        (WebAdminRequest(method="GET", path="/admin", body=b"body"), 400, b"body_not_supported"),
        (WebAdminRequest(method="GET", path="/admin/missing"), 404, b"not_found"),
    ],
)
async def test_service_rejects_mutation_query_body_and_unknown_assets(
    admin_request: WebAdminRequest,
    status: int,
    message: bytes,
) -> None:
    response = await WebAdminService().handle(admin_request)

    assert response.status_code == status
    assert message in response.body
    assert b"secret" not in response.body
    assert b"access-control-allow-origin" not in dict(response.headers)
    assert b"set-cookie" not in dict(response.headers)
    if status == 405:
        assert dict(response.headers)[b"allow"] == b"GET, HEAD"


@pytest.mark.asyncio
async def test_service_invalid_request_object_returns_fixed_error() -> None:
    response = await WebAdminService().handle(object())  # type: ignore[arg-type]

    assert response.status_code == 400
    assert response.body == b"invalid_request\n"


@pytest.mark.asyncio
async def test_asgi_get_returns_html_with_complete_security_headers() -> None:
    status, headers, body, _sent, receive_calls = await _call_asgi(WebAdminASGIApp(service=WebAdminService()))

    assert status == 200
    assert receive_calls == 1
    assert body.startswith(b"<!doctype html>")
    assert headers[b"cache-control"] == b"no-store, max-age=0"
    csp = headers[b"content-security-policy"].decode("ascii")
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert headers[b"cross-origin-opener-policy"] == b"same-origin"
    assert headers[b"cross-origin-resource-policy"] == b"same-origin"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"content-length"] == str(len(body)).encode("ascii")
    assert not any(name.startswith(b"access-control-") for name in headers)
    assert b"set-cookie" not in headers


@pytest.mark.asyncio
async def test_asgi_head_and_not_found_never_send_response_content() -> None:
    app = WebAdminASGIApp(service=WebAdminService())
    status, headers, body, _sent, _calls = await _call_asgi(app, method="HEAD", path="/admin/app.js")
    missing_status, missing_headers, missing_body, _missing_sent, _missing_calls = await _call_asgi(
        app,
        method="HEAD",
        path="/admin/missing",
    )

    assert status == 200
    assert body == b""
    assert int(headers[b"content-length"]) > 0
    assert missing_status == 404
    assert missing_body == b""
    assert missing_headers[b"content-length"] == str(len(b"not_found\n")).encode("ascii")


@pytest.mark.asyncio
async def test_asgi_rejects_query_tokens_without_echo_or_asset_delivery() -> None:
    token = b"a" * 32
    status, _headers, body, _sent, _calls = await _call_asgi(
        WebAdminASGIApp(service=WebAdminService()),
        query_string=b"token=" + token,
    )

    assert status == 400
    assert body == b"query_not_supported\n"
    assert token not in body
    assert b"<!doctype html>" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        "not-headers",
        [("content-length", b"0")],
        [(b"Content-Length", b"0")],
        [(b"content-length", b"0"), (b"content-length", b"0")],
        [(b"content-length", b"00")],
        [(b"content-length", b"-1")],
        [(b"x-test", b"bad\nvalue")],
        [(b"x-test", b"bad\x00value")],
        [(b"x" * 129, b"value")],
        [(b"x-test", b"a" * 65_537)],
        [(b"x-test",)],
    ],
)
async def test_asgi_rejects_malformed_headers(headers: object) -> None:
    status, _response_headers, body, _sent, receive_calls = await _call_asgi(
        WebAdminASGIApp(service=WebAdminService()),
        headers=headers,
    )

    assert status == 400
    assert body == b"invalid_request\n"
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_asgi_rejects_oversized_declared_body_before_receive() -> None:
    status, _headers, body, _sent, receive_calls = await _call_asgi(
        WebAdminASGIApp(service=WebAdminService()),
        headers=[(b"content-length", b"1025")],
    )

    assert status == 413
    assert body == b"request_too_large\n"
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_asgi_rejects_body_length_mismatch_and_nonempty_get_body() -> None:
    app = WebAdminASGIApp(service=WebAdminService())
    mismatch_status, _headers, mismatch_body, _sent, _calls = await _call_asgi(
        app,
        headers=[(b"content-length", b"1")],
    )
    body_status, _body_headers, body, _body_sent, _body_calls = await _call_asgi(
        app,
        headers=[(b"content-length", b"4")],
        body_messages=[{"type": "http.request", "body": b"body", "more_body": False}],
    )

    assert mismatch_status == 400
    assert mismatch_body == b"invalid_request\n"
    assert body_status == 400
    assert body == b"body_not_supported\n"


@pytest.mark.asyncio
async def test_asgi_rejects_chunked_body_over_limit_and_message_flood() -> None:
    app = WebAdminASGIApp(service=WebAdminService())
    oversized_status, _headers, oversized_body, _sent, _calls = await _call_asgi(
        app,
        body_messages=[
            {"type": "http.request", "body": b"a" * 800, "more_body": True},
            {"type": "http.request", "body": b"b" * 300, "more_body": False},
        ],
    )
    flooded_status, _flood_headers, flooded_body, _flood_sent, flooded_calls = await _call_asgi(
        app,
        body_messages=[{"type": "http.request", "body": b"", "more_body": True} for _ in range(8)],
    )

    assert oversized_status == 413
    assert oversized_body == b"request_too_large\n"
    assert flooded_status == 413
    assert flooded_body == b"request_too_large\n"
    assert flooded_calls == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_path",
    [
        b"/admin%2fapp.js",
        b"/admin/../admin",
        b"\xff/admin",
        "/admin",
        b"a" * 257,
    ],
)
async def test_asgi_rejects_ambiguous_raw_paths(raw_path: object) -> None:
    status, _headers, body, _sent, receive_calls = await _call_asgi(
        WebAdminASGIApp(service=WebAdminService()),
        raw_path=raw_path,
    )

    assert status == 400
    assert body == b"invalid_request\n"
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_asgi_rejects_non_http_scope() -> None:
    app = WebAdminASGIApp(service=WebAdminService())

    async def receive() -> dict[str, Any]:
        raise AssertionError("receive must not be called")

    async def send(_message: dict[str, Any]) -> None:
        raise AssertionError("send must not be called")

    with pytest.raises(WebAdminProtocolError, match="HTTP"):
        await app({"type": "websocket"}, receive, send)


@pytest.mark.asyncio
async def test_asgi_propagates_receive_cancellation() -> None:
    app = WebAdminASGIApp(service=WebAdminService())

    async def receive() -> dict[str, Any]:
        raise asyncio.CancelledError

    async def send(_message: dict[str, Any]) -> None:
        raise AssertionError("send must not be called after cancellation")

    with pytest.raises(asyncio.CancelledError):
        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin",
                "raw_path": b"/admin",
                "query_string": b"",
                "headers": (),
            },
            receive,
            send,
        )


@pytest.mark.asyncio
async def test_asgi_propagates_send_cancellation() -> None:
    app = WebAdminASGIApp(service=WebAdminService())

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await app(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin",
                "raw_path": b"/admin",
                "query_string": b"",
                "headers": (),
            },
            receive,
            send,
        )


def test_response_contract_rejects_missing_security_headers_and_cors_or_cookie() -> None:
    service = WebAdminService()
    response = asyncio.run(service.handle(WebAdminRequest(method="GET", path="/admin")))
    valid_headers = response.headers

    with pytest.raises(WebAdminConfigurationError, match="status"):
        WebAdminResponse(status_code=500, headers=valid_headers, body=response.body)
    with pytest.raises(WebAdminConfigurationError, match="headers"):
        WebAdminResponse(status_code=200, headers=(), body=b"")
    with pytest.raises(WebAdminConfigurationError, match=r"Cookie|CORS"):
        WebAdminResponse(
            status_code=200,
            headers=(*valid_headers, (b"set-cookie", b"token=secret")),
            body=response.body,
        )
    with pytest.raises(WebAdminConfigurationError, match=r"Cookie|CORS"):
        WebAdminResponse(
            status_code=200,
            headers=(*valid_headers, (b"access-control-allow-origin", b"*")),
            body=response.body,
        )
    with pytest.raises(WebAdminConfigurationError, match="content length"):
        WebAdminResponse(
            status_code=200,
            headers=tuple((name, b"1") if name == b"content-length" else (name, value) for name, value in valid_headers),
            body=response.body,
        )
    with pytest.raises(WebAdminConfigurationError, match="安全 header"):
        WebAdminResponse(
            status_code=200,
            headers=tuple(
                (name, b"default-src *") if name == b"content-security-policy" else (name, value) for name, value in valid_headers
            ),
            body=response.body,
        )
    with pytest.raises(WebAdminConfigurationError, match="未允许 header"):
        WebAdminResponse(
            status_code=200,
            headers=(*valid_headers, (b"x-extra", b"value")),
            body=response.body,
        )


def test_app_requires_explicit_valid_handler_and_module_has_no_live_app_or_service() -> None:
    with pytest.raises(WebAdminConfigurationError, match="service"):
        WebAdminASGIApp(service=object())  # type: ignore[arg-type]

    module = importlib.reload(web_admin_module)
    assert not any(isinstance(value, (module.WebAdminService, module.WebAdminASGIApp)) for value in vars(module).values())
    assert "nonebot" not in module.__dict__
    assert "runtime_snapshots" not in module.__dict__
    assert "runtime_metrics" not in module.__dict__

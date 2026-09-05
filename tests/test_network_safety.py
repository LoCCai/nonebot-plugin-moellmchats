from __future__ import annotations

import asyncio

import pytest

from nonebot_plugin_moellmchats import network_safety as module
from nonebot_plugin_moellmchats.network_safety import (
    SAFE_HTTP_MAX_RESPONSE_BYTES,
    SafeHttpError,
    safe_public_get,
    safe_request,
    validate_public_url,
)


@pytest.mark.asyncio
async def test_safe_public_get_fixes_method_headers_and_public_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def request(url: str, **kwargs):
        calls.append((url, kwargs))
        return module.SafeHttpResponse(
            status=200,
            headers={"content-type": "text/html"},
            body=b"<html></html>",
            url=url,
            redirect_count=0,
        )

    monkeypatch.setattr(module, "safe_request", request)

    response = await safe_public_get("https://example.com/article")

    assert response.status == 200
    assert calls == [
        (
            "https://example.com/article",
            {
                "method": "GET",
                "headers": module._PUBLIC_DOCUMENT_HEADERS,
                "_network_allow": ("*",),
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
        "http://metadata.google.internal/",
    ],
)
async def test_network_tools_reject_non_public_targets(url: str) -> None:
    with pytest.raises(ValueError, match="网络工具"):
        await validate_public_url(url)


class _Writer:
    def __init__(self) -> None:
        self.request = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.request.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _BlockingCloseWriter(_Writer):
    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


def _dns(address: str):
    family = 10 if ":" in address else 2
    return [(family, 1, 6, "", (address, 443))]


@pytest.mark.asyncio
async def test_safe_request_pins_the_validated_address_to_connection() -> None:
    connected: list[tuple[str, int, str | None]] = []
    writer = _Writer()

    async def resolver(_hostname: str, _port: int):
        return _dns("93.184.216.34")

    async def connector(address, port, _ssl_context, server_hostname, _timeout):
        connected.append((address, port, server_hostname))
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        reader.feed_eof()
        return reader, writer

    response = await safe_request(
        "https://api.example/data",
        _network_allow=("api.example",),
        _resolver=resolver,
        _connector=connector,
    )

    assert response.status == 200
    assert response.text == "ok"
    assert connected == [("93.184.216.34", 443, "api.example")]
    assert writer.closed is True
    assert writer.request.startswith(b"GET /data HTTP/1.1\r\n")
    assert b"host: api.example\r\n" in writer.request.lower()


@pytest.mark.asyncio
async def test_safe_request_rejects_mixed_public_private_dns_before_connect() -> None:
    async def resolver(_hostname: str, _port: int):
        return [*_dns("93.184.216.34"), *_dns("127.0.0.1")]

    async def connector(*_args):
        raise AssertionError("private DNS answer must prevent every connection")

    with pytest.raises(SafeHttpError, match="私网"):
        await safe_request(
            "https://api.example/data",
            _network_allow=("api.example",),
            _resolver=resolver,
            _connector=connector,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "method", "expected_method"),
    [
        (301, "POST", "GET"),
        (302, "POST", "GET"),
        (303, "PUT", "GET"),
        (307, "POST", "POST"),
        (308, "POST", "POST"),
    ],
)
async def test_safe_request_handles_redirect_methods_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    method: str,
    expected_method: str,
) -> None:
    calls: list[tuple[str, str, bytes]] = []

    async def request_once(target, *, method, headers, body, **_kwargs):
        calls.append((target.url, method, body))
        if len(calls) == 1:
            return status, {"location": "/next"}, b""
        return 200, {"content-length": "2"}, b"ok"

    monkeypatch.setattr(module, "_request_once", request_once)
    response = await safe_request(
        "https://api.example/start",
        method=method,
        body="value",
        _network_allow=("api.example",),
    )

    assert response.redirect_count == 1
    assert calls[1][1] == expected_method
    assert calls[1][2] == (b"" if expected_method == "GET" else b"value")


@pytest.mark.asyncio
async def test_safe_request_revalidates_redirect_and_rejects_private_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request_once(*_args, **_kwargs):
        return 302, {"location": "http://169.254.169.254/latest"}, b""

    monkeypatch.setattr(module, "_request_once", request_once)
    with pytest.raises(SafeHttpError, match="私网"):
        await safe_request(
            "http://api.example/start",
            _network_allow=("*",),
        )


@pytest.mark.asyncio
async def test_cross_origin_redirect_drops_sensitive_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, str]] = []

    async def request_once(target, *, headers, **_kwargs):
        observed.append(dict(headers))
        if len(observed) == 1:
            return 302, {"location": "https://other.example/next"}, b""
        return 200, {}, b"ok"

    monkeypatch.setattr(module, "_request_once", request_once)
    await safe_request(
        "https://api.example/start",
        headers={
            "Authorization": "Bearer private",
            "Cookie": "session=private",
            "X-Api-Key": "private",
            "X-Safe": "kept",
        },
        _network_allow=("api.example", "other.example"),
    )

    assert set(observed[1]) == {"x-safe"}


@pytest.mark.asyncio
async def test_safe_request_rejects_https_downgrade_and_redirect_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def downgrade(*_args, **_kwargs):
        return 302, {"location": "http://api.example/next"}, b""

    monkeypatch.setattr(module, "_request_once", downgrade)
    with pytest.raises(SafeHttpError, match="HTTPS 降级"):
        await safe_request(
            "https://api.example/start",
            _network_allow=("api.example",),
        )

    async def loop(*_args, **_kwargs):
        return 302, {"location": "https://api.example/start"}, b""

    monkeypatch.setattr(module, "_request_once", loop)
    with pytest.raises(SafeHttpError, match="重定向循环"):
        await safe_request(
            "https://api.example/start",
            _network_allow=("api.example",),
        )


@pytest.mark.asyncio
async def test_safe_request_rejects_credentials_and_oversized_response() -> None:
    with pytest.raises(SafeHttpError, match="凭据"):
        await safe_request(
            "https://user:password@api.example/",
            _network_allow=("api.example",),
        )

    writer = _Writer()

    async def resolver(_hostname: str, _port: int):
        return _dns("93.184.216.34")

    async def connector(*_args):
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(SAFE_HTTP_MAX_RESPONSE_BYTES + 1).encode("ascii")
            + b"\r\n\r\n"
        )
        reader.feed_eof()
        return reader, writer

    with pytest.raises(SafeHttpError, match="大小上限"):
        await safe_request(
            "https://api.example/large",
            _network_allow=("api.example",),
            _resolver=resolver,
            _connector=connector,
        )


@pytest.mark.asyncio
async def test_safe_request_close_wait_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _BlockingCloseWriter()

    async def resolver(_hostname: str, _port: int):
        return _dns("93.184.216.34")

    async def connector(*_args):
        reader = asyncio.StreamReader()
        reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        reader.feed_eof()
        return reader, writer

    monkeypatch.setattr(module, "_CLOSE_TIMEOUT_SECONDS", 0.01)
    response = await asyncio.wait_for(
        safe_request(
            "https://api.example/data",
            _network_allow=("api.example",),
            _resolver=resolver,
            _connector=connector,
        ),
        timeout=0.2,
    )

    assert response.text == "ok"
    assert writer.closed is True


@pytest.mark.asyncio
async def test_safe_request_rejects_invalid_status_and_header_controls() -> None:
    async def resolver(_hostname: str, _port: int):
        return _dns("93.184.216.34")

    for payload, expected in (
        (b"HTTP/1.1 999 Invalid\r\nContent-Length: 0\r\n\r\n", "状态码"),
        (b"HTTP/1.1 200 OK\r\nX-Test: bad\x00value\r\nContent-Length: 0\r\n\r\n", "控制字符"),
    ):
        writer = _Writer()

        async def connector(*_args, payload=payload, writer=writer):
            reader = asyncio.StreamReader()
            reader.feed_data(payload)
            reader.feed_eof()
            return reader, writer

        with pytest.raises(SafeHttpError, match=expected):
            await safe_request(
                "https://api.example/data",
                _network_allow=("api.example",),
                _resolver=resolver,
                _connector=connector,
            )

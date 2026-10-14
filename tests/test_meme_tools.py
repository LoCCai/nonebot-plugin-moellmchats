from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from nonebot_plugin_moellmchats import meme_tools as module
from nonebot_plugin_moellmchats.meme_tools import (
    GENERATE_MEME_TOOL,
    RenderToolError,
    _download_public_image,
    _validate_meme_arguments,
    generate_meme_image,
    meme_capability_reason,
    register_meme_tools,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolContext, tool_registry


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, event: Any, message: Any) -> None:
        self.sent.append(message)


def _tiny_png() -> bytes:
    # Verified 1x1 RGB PNG; Pillow stays optional in these tests.
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _context(bot: _FakeBot | None = None) -> ToolContext:
    return ToolContext(bot=bot or _FakeBot(), event=object())


def _configure_base(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    monkeypatch.setattr(module.render_tools_config, "moellm_meme_api_base", base, raising=False)


# ---------------------------------------------------------------------------
# Capability / registration
# ---------------------------------------------------------------------------


def test_meme_capability_reason_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_base(monkeypatch, "  ")
    reason = meme_capability_reason()
    assert reason is not None
    assert "MOELLM_MEME_API_BASE" in reason
    _configure_base(monkeypatch, "ftp://192.168.31.193:10008")
    reason = meme_capability_reason()
    assert reason is not None
    assert "http/https" in reason
    _configure_base(monkeypatch, "http://192.168.31.193:10008/")
    assert meme_capability_reason() is None
    assert module.meme_api_base() == "http://192.168.31.193:10008"


def test_register_meme_tools_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive cleanup: a host that pre-set MOELLM_MEME_API_BASE may have
    # registered the tool at import time.
    tool_registry.unregister("generate_meme")
    _configure_base(monkeypatch, "")
    assert register_meme_tools() == ()
    assert tool_registry.get("generate_meme") is None
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    assert register_meme_tools() == ("generate_meme",)
    # duplicate registration is tolerated
    assert register_meme_tools() == ()
    assert tool_registry.get("generate_meme") is GENERATE_MEME_TOOL
    tool_registry.unregister("generate_meme")


# ---------------------------------------------------------------------------
# Handler validation branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_meme_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_base(monkeypatch, "")
    result = await generate_meme_image(_tool_context=_context(), meme_key="petpet")
    assert "未启用" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["Petpet", "../etc", "a b", ""])
async def test_generate_meme_rejects_bad_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    result = await generate_meme_image(_tool_context=_context(), meme_key=key)
    assert "模板 key" in result.text


@pytest.mark.asyncio
async def test_generate_meme_rejects_bad_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    context = _context()
    bad_texts = await generate_meme_image(
        _tool_context=context,
        meme_key="petpet",
        texts="不是数组",  # type: ignore[arg-type]
    )
    assert "texts" in bad_texts.text
    bad_urls = await generate_meme_image(
        _tool_context=context,
        meme_key="petpet",
        image_urls=[123],  # type: ignore[list-item]
    )
    assert "image_urls" in bad_urls.text
    bad_options = await generate_meme_image(_tool_context=context, meme_key="petpet", options={"x": ["y"]})
    assert "options" in bad_options.text
    too_many_texts = await generate_meme_image(_tool_context=context, meme_key="petpet", texts=["哈" * 201])
    assert "文字" in too_many_texts.text


# ---------------------------------------------------------------------------
# Backend conversation (mocked transport)
# ---------------------------------------------------------------------------


class _ScriptedBackend:
    """Replays canned (status, payload) replies in order, recording paths."""

    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, Any]] = []

    async def __call__(self, method: str, path: str, *, json_payload: Any = None, **kwargs: Any) -> tuple[int, Any]:
        self.calls.append((method, path, json_payload))
        if not self.replies:
            raise AssertionError(f"unexpected meme request: {method} {path}")
        return self.replies.pop(0)


_PETPET_INFO = (
    200,
    {
        "key": "petpet",
        "params": {
            "min_images": 1,
            "max_images": 1,
            "min_texts": 0,
            "max_texts": 0,
            "default_texts": [],
            "options": [],
        },
    },
)

# A text-only template (e.g. 5000choyen) so the no-image paths stay valid.
_TEXT_INFO = (
    200,
    {
        "key": "5000choyen",
        "params": {
            "min_images": 0,
            "max_images": 0,
            "min_texts": 2,
            "max_texts": 2,
            "default_texts": [],
            "options": [],
        },
    },
)


@pytest.mark.asyncio
async def test_generate_meme_happy_path_without_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    backend = _ScriptedBackend(
        [
            _TEXT_INFO,
            (200, {"image_id": "result-1"}),
            (200, _tiny_png()),
        ]
    )
    monkeypatch.setattr(module, "_meme_request", backend)
    bot = _FakeBot()
    result = await generate_meme_image(
        _tool_context=_context(bot),
        meme_key="5000choyen",
        texts=["你好", "世界"],
        image_urls=[],
    )
    assert "已生成" in result.text
    assert result.metadata["meme_key"] == "5000choyen"
    assert len(bot.sent) == 1
    assert [call[1] for call in backend.calls] == [
        "/memes/5000choyen/info",
        "/memes/5000choyen",
        "/image/result-1",
    ]
    generate_call = backend.calls[1]
    assert generate_call[2] == {
        "images": [],
        "texts": ["你好", "世界"],
        "options": {},
    }


@pytest.mark.asyncio
async def test_generate_meme_uploads_input_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    backend = _ScriptedBackend(
        [
            _PETPET_INFO,
            (200, {"image_id": "up-1"}),
            (200, {"image_id": "result-2"}),
            (200, _tiny_png()),
        ]
    )
    monkeypatch.setattr(module, "_meme_request", backend)

    downloads: list[str] = []

    async def fake_download(url: str) -> bytes:
        downloads.append(url)
        return _tiny_png()

    monkeypatch.setattr(module, "_download_public_image", fake_download)
    result = await generate_meme_image(_tool_context=_context(), meme_key="petpet", image_urls=["http://93.184.216.34/a.png"])
    assert "已生成" in result.text
    assert downloads == ["http://93.184.216.34/a.png"]
    upload_call = backend.calls[1]
    assert upload_call[1] == "/image/upload"
    assert upload_call[2]["type"] == "data"
    generate_call = backend.calls[2]
    assert generate_call[2]["images"] == [{"name": "image_0", "id": "up-1"}]


@pytest.mark.asyncio
async def test_generate_meme_unknown_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    backend = _ScriptedBackend([(404, {"detail": "not found"})])
    monkeypatch.setattr(module, "_meme_request", backend)
    result = await generate_meme_image(_tool_context=_context(), meme_key="nosuch")
    assert "未找到模板" in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "fragment"),
    [
        (551, "文字数量"),
        (550, "图片数量"),
        (560, "长度"),
        (570, "不可用"),
        (999, "999"),
    ],
)
async def test_generate_meme_maps_backend_error_codes(monkeypatch: pytest.MonkeyPatch, code: int, fragment: str) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    backend = _ScriptedBackend(
        [
            _TEXT_INFO,
            (500, {"code": code, "message": "boom", "data": {}}),
        ]
    )
    monkeypatch.setattr(module, "_meme_request", backend)
    result = await generate_meme_image(_tool_context=_context(), meme_key="5000choyen", texts=["你好", "世界"])
    assert fragment in result.text


@pytest.mark.asyncio
async def test_generate_meme_rejects_invalid_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    backend = _ScriptedBackend(
        [
            _TEXT_INFO,
            (200, {"image_id": "bad id with spaces!"}),
        ]
    )
    monkeypatch.setattr(module, "_meme_request", backend)
    result = await generate_meme_image(_tool_context=_context(), meme_key="5000choyen", texts=["你好", "世界"])
    assert "非法的图片标识" in result.text


@pytest.mark.asyncio
async def test_generate_meme_validates_counts_against_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    backend = _ScriptedBackend([_PETPET_INFO])
    monkeypatch.setattr(module, "_meme_request", backend)
    result = await generate_meme_image(_tool_context=_context(), meme_key="petpet", image_urls=[])
    assert "至少需要 1 张图片" in result.text
    assert len(backend.calls) == 1  # generation never started


def test_validate_meme_arguments_text_bounds() -> None:
    params = {"min_images": 0, "max_images": 1, "min_texts": 1, "max_texts": 2}
    with pytest.raises(RenderToolError, match="至少需要 1 条文字"):
        _validate_meme_arguments(params, images=[], texts=[])
    with pytest.raises(RenderToolError, match="最多接受 2 条文字"):
        _validate_meme_arguments(params, images=[], texts=["a", "b", "c"])
    with pytest.raises(RenderToolError, match="最多接受 1 张图片"):
        _validate_meme_arguments(params, images=["a", "b"], texts=["a"])


def test_validate_meme_arguments_zero_is_an_explicit_bound() -> None:
    # Text-only templates declare max_images=0; that must reject images
    # outright instead of being treated as "no limit declared".
    params = {"min_images": 0, "max_images": 0, "min_texts": 0, "max_texts": 0}
    with pytest.raises(RenderToolError, match="最多接受 0 张图片"):
        _validate_meme_arguments(params, images=["a"], texts=[])
    _validate_meme_arguments(params, images=[], texts=[])
    missing = {"min_texts": 1}
    _validate_meme_arguments(missing, images=[], texts=["ok"])


# ---------------------------------------------------------------------------
# Strict image downloader
# ---------------------------------------------------------------------------


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int) -> Any:
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(chunks or [])


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.requested: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_FakeSession":
        assert "allow_redirects" not in kwargs
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> Any:
        assert kwargs["allow_redirects"] is False
        self.requested.append(url)

        class _Ctx:
            def __init__(self, response: _FakeResponse) -> None:
                self.response = response

            async def __aenter__(self) -> _FakeResponse:
                return self.response

            async def __aexit__(self, *info: object) -> None:
                return None

        return _Ctx(self.responses.pop(0))


@pytest.mark.asyncio
async def test_download_public_image_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[str] = []

    async def fake_validate(url: str) -> None:
        validated.append(url)

    monkeypatch.setattr(module, "ensure_public_tool_url", fake_validate)
    png = _tiny_png()
    session = _FakeSession(
        [
            _FakeResponse(
                headers={"content-type": "image/png"},
                chunks=[png],
            )
        ]
    )
    monkeypatch.setattr(module.aiohttp, "ClientSession", session)
    data = await _download_public_image("http://93.184.216.34/a.png")
    assert data == png
    assert validated == ["http://93.184.216.34/a.png"]


@pytest.mark.asyncio
async def test_download_public_image_follows_and_revalidates_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[str] = []

    async def fake_validate(url: str) -> None:
        validated.append(url)
        if url == "http://192.168.0.9/evil.png":
            raise RenderToolError("图片地址未通过公网安全检查")

    monkeypatch.setattr(module, "ensure_public_tool_url", fake_validate)
    session = _FakeSession(
        [
            _FakeResponse(status=302, headers={"location": "http://192.168.0.9/evil.png"}),
            _FakeResponse(headers={"content-type": "image/png"}, chunks=[b""]),
        ]
    )
    monkeypatch.setattr(module.aiohttp, "ClientSession", session)
    with pytest.raises(RenderToolError, match="公网安全检查"):
        await _download_public_image("http://93.184.216.34/a.png")
    assert session.requested == ["http://93.184.216.34/a.png"]


@pytest.mark.asyncio
async def test_download_public_image_rejects_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_validate(url: str) -> None:
        return None

    monkeypatch.setattr(module, "ensure_public_tool_url", fake_validate)
    session = _FakeSession(
        [
            _FakeResponse(
                headers={"content-type": "image/png"},
                chunks=[b"\x89PNG\r\n\x1a\n", b"0" * (8 * 1_024 * 1_024)],
            )
        ]
    )
    monkeypatch.setattr(module.aiohttp, "ClientSession", session)
    with pytest.raises(RenderToolError, match="8MiB"):
        await _download_public_image("http://93.184.216.34/a.png")


@pytest.mark.asyncio
async def test_download_public_image_rejects_non_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_validate(url: str) -> None:
        return None

    monkeypatch.setattr(module, "ensure_public_tool_url", fake_validate)
    session = _FakeSession([_FakeResponse(headers={"content-type": "text/html"}, chunks=[b"<html>"])])
    monkeypatch.setattr(module.aiohttp, "ClientSession", session)
    with pytest.raises(RenderToolError, match="不是受支持的图片"):
        await _download_public_image("http://93.184.216.34/a.png")


@pytest.mark.asyncio
async def test_download_public_image_rejects_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_validate(url: str) -> None:
        return None

    monkeypatch.setattr(module, "ensure_public_tool_url", fake_validate)
    session = _FakeSession([_FakeResponse(status=404)])
    monkeypatch.setattr(module.aiohttp, "ClientSession", session)
    with pytest.raises(RenderToolError, match="HTTP 404"):
        await _download_public_image("http://93.184.216.34/a.png")


class _FakeStream:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        if size and size > 0:
            return self._data[:size]
        return self._data

    async def iter_chunked(self, size: int) -> Any:
        for offset in range(0, len(self._data), size):
            yield self._data[offset:offset + size]


class _AsyncCM:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _MemeRequestSession:
    def __init__(self, response: Any) -> None:
        self._response = response

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return _AsyncCM(self._response)


class _MemeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.content = _FakeStream(body)


@pytest.mark.asyncio
async def test_meme_request_bounds_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    oversized = _MemeResponse(200, {"content-type": "application/octet-stream"}, b"x" * (33 * 1_024 * 1_024))
    monkeypatch.setattr(module.aiohttp, "ClientSession", _MemeRequestSession(oversized))
    with pytest.raises(RenderToolError, match="大小上限"):
        await module._meme_request("GET", "/image/too-big")


@pytest.mark.asyncio
async def test_meme_request_parses_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_base(monkeypatch, "http://192.168.31.193:10008")
    import ujson as json

    body = json.dumps({"params": {"min_images": 1}}).encode()
    response = _MemeResponse(200, {"content-type": "application/json"}, body)
    monkeypatch.setattr(module.aiohttp, "ClientSession", _MemeRequestSession(response))
    status, payload = await module._meme_request("GET", "/memes/petpet/info")
    assert status == 200
    assert payload == {"params": {"min_images": 1}}


@asynccontextmanager
async def _http_test_server(handler: Any) -> Any:
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/image/test", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    try:
        await site.start()
        port = runner.addresses[0][1]
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_meme_request_reads_complete_chunked_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import web

    body = bytes(range(256)) * 1024

    async def stream(request: Any) -> Any:
        response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
        await response.prepare(request)
        for offset in range(0, len(body), 32_768):
            await response.write(body[offset:offset + 32_768])
            await asyncio.sleep(0.01)
        await response.write_eof()
        return response

    async with _http_test_server(stream) as base:
        _configure_base(monkeypatch, base)
        status, payload = await module._meme_request("GET", "/image/test")
    assert status == 200
    assert payload == body


@pytest.mark.asyncio
async def test_image_downloader_real_session_checks_redirect_before_following(monkeypatch: pytest.MonkeyPatch) -> None:
    from aiohttp import web

    blocked = "http://192.168.0.9/evil.png"
    validated: list[str] = []

    async def validate(url: str) -> None:
        validated.append(url)
        if url == blocked:
            raise RenderToolError("图片地址未通过公网安全检查")

    async def redirect(request: Any) -> Any:
        return web.Response(status=302, headers={"Location": blocked})

    monkeypatch.setattr(module, "ensure_public_tool_url", validate)
    async with _http_test_server(redirect) as base:
        url = base + "/image/test"
        with pytest.raises(RenderToolError, match="公网安全检查"):
            await _download_public_image(url)
    assert validated == [url, blocked]


def test_meme_tool_spec_shape() -> None:
    assert GENERATE_MEME_TOOL.name == "generate_meme"
    assert GENERATE_MEME_TOOL.permission == "user"
    assert GENERATE_MEME_TOOL.effect.value == "read_only"
    assert GENERATE_MEME_TOOL.timeout_seconds is not None
    assert GENERATE_MEME_TOOL.policy is not None
    required = GENERATE_MEME_TOOL.parameters["required"]
    assert list(required) == ["meme_key"]

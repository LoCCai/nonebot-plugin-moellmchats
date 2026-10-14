from __future__ import annotations

import asyncio
import base64
from importlib.util import find_spec
import io
import os
import sys
from typing import Any

from pydantic import ValidationError
import pytest

from nonebot_plugin_moellmchats import render_tools as module
from nonebot_plugin_moellmchats.config import RenderToolsConfig
from nonebot_plugin_moellmchats.render_tools import (
    RENDER_HTML_TOOL,
    RENDER_MARKDOWN_TOOL,
    RENDER_TOOL_SPECS,
    SCREENSHOT_URL_TOOL,
    RenderCapability,
    RenderToolError,
    check_markdown_links_public,
    deliver_image,
    ensure_public_tool_url,
    evaluate_render_capability,
    guarded_image_bytes,
    register_render_tools,
    run_browser_task,
    sniff_image_mime,
    vision_data_uri,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolContext,
    ToolResult,
    tool_registry,
)

# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------


def test_render_tools_config_defaults_and_env_coercion() -> None:
    config = RenderToolsConfig()
    assert config.moellm_render_enabled is True
    assert config.moellm_render_browser_executable == ""
    assert config.moellm_render_default_width == 800
    assert config.moellm_meme_api_base == ""
    disabled = RenderToolsConfig(moellm_render_enabled="false")
    assert disabled.moellm_render_enabled is False
    with pytest.raises(ValidationError):
        RenderToolsConfig(moellm_render_default_width=200)
    with pytest.raises(ValidationError):
        RenderToolsConfig(moellm_render_default_width=9_999)


# ---------------------------------------------------------------------------
# Capability probing
# ---------------------------------------------------------------------------


def _capability(**overrides: Any) -> RenderCapability:
    arguments: dict[str, Any] = {
        "enabled": True,
        "executable": "",
        "executable_exists": False,
        "endpoint": "",
        "htmlrender_found": True,
        "playwright_found": True,
        "bundled_browser": None,
        "chromium_in_path": None,
    }
    arguments.update(overrides)
    return evaluate_render_capability(**arguments)


def test_capability_disabled_by_config() -> None:
    capability = _capability(enabled=False)
    assert capability.available is False
    assert "MOELLM_RENDER_ENABLED" in capability.reason


def test_capability_missing_htmlrender_hint() -> None:
    capability = _capability(htmlrender_found=False)
    assert capability.available is False
    assert "nonebot-plugin-htmlrender" in capability.reason


def test_capability_missing_playwright_hint() -> None:
    capability = _capability(playwright_found=False)
    assert capability.available is False
    assert "playwright install chromium" in capability.reason


def test_capability_explicit_executable_branches() -> None:
    assert _capability(executable="/usr/bin/chromium", executable_exists=True).available
    missing = _capability(executable="/no/such/chrome", executable_exists=False)
    assert missing.available is False
    assert "/no/such/chrome" in missing.reason


def test_capability_endpoint_branches() -> None:
    assert _capability(endpoint="http://127.0.0.1:9222").available
    assert _capability(endpoint="ws://127.0.0.1:9222/ws").available
    bad = _capability(endpoint="ftp://127.0.0.1:9222")
    assert bad.available is False
    assert "http/https" in bad.reason


def test_capability_system_default_tiers() -> None:
    assert _capability(bundled_browser=True).reason == "playwright-bundled-browser"
    assert _capability(chromium_in_path="/usr/bin/chromium").available
    none_found = _capability()
    assert none_found.available is False
    assert "MOELLM_RENDER_BROWSER_EXECUTABLE" in none_found.reason


def test_probe_render_capability_reads_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_enabled",
        False,
        raising=False,
    )
    capability = module.probe_render_capability()
    assert capability.available is False
    assert "MOELLM_RENDER_ENABLED" in capability.reason


def _deterministic_probe_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every environment-dependent probe input for reproducibility."""

    def fake_find_spec(name: str) -> Any:
        return object() if name.startswith("nonebot_plugin_htmlrender") or name == "playwright" else None

    monkeypatch.setattr(module, "find_spec", fake_find_spec, raising=False)
    monkeypatch.setattr(module, "_bundled_playwright_browser_present", lambda: False, raising=False)
    monkeypatch.setattr(module, "_chromium_in_path", lambda: None, raising=False)


def test_probe_render_capability_executable_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deterministic_probe_environment(monkeypatch)
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_browser_executable",
        "/no/such/chrome",
        raising=False,
    )
    capability = module.probe_render_capability()
    assert capability.available is False
    assert "/no/such/chrome" in capability.reason


def test_probe_render_capability_prefers_explicit_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _deterministic_probe_environment(monkeypatch)
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_browser_executable",
        "/bin/sh",
        raising=False,
    )
    capability = module.probe_render_capability()
    assert capability.available is True
    assert capability.reason == "explicit-executable"


def test_session_overrides_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_browser_executable",
        "/opt/chrome",
        raising=False,
    )
    assert module._session_overrides() == {"executable_path": "/opt/chrome"}
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_browser_executable",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_browser_endpoint",
        "http://127.0.0.1:9222",
        raising=False,
    )
    assert module._session_overrides() == {"endpoint_url": "http://127.0.0.1:9222"}
    monkeypatch.setattr(
        module.render_tools_config,
        "moellm_render_browser_endpoint",
        "ws://127.0.0.1:9222/ws",
        raising=False,
    )
    assert module._session_overrides() == {"endpoint": "ws://127.0.0.1:9222/ws"}


def test_registration_respects_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive cleanup: an environment that pre-set MOELLM_RENDER_* may have
    # registered the tools at import time.
    for name in ("render_html", "render_markdown", "screenshot_url", "get_render_theme"):
        tool_registry.unregister(name)
    available = RenderCapability(True, "test")
    registered = register_render_tools(available)
    assert registered == tuple(spec.name for spec in RENDER_TOOL_SPECS)
    for name in registered:
        assert tool_registry.get(name) is not None
        tool_registry.unregister(name)
    unavailable = RenderCapability(False, "测试不可用")
    assert register_render_tools(unavailable) == ()
    assert tool_registry.get("render_html") is None
    # duplicate registration is tolerated
    monkeypatch.setattr(module, "RENDER_CAPABILITY", available, raising=False)
    register_render_tools(available)
    assert register_render_tools(available) == ()
    for name in ("render_html", "render_markdown", "screenshot_url", "get_render_theme"):
        tool_registry.unregister(name)


# ---------------------------------------------------------------------------
# Standalone posture (no Qiqi host modules on this import path)
def test_render_theme_reads_published_host_palette(monkeypatch):
    from types import ModuleType, SimpleNamespace
    host = ModuleType('src')
    utils = ModuleType('src.utils')
    utils.render_theme = SimpleNamespace(stylesheet_context=lambda _: {
        'theme_family': 'qiqi', 'theme': 'night',
        'theme_css': ':root[data-theme="light"]{--accent-color:#123456;} :root[data-theme="dark"]{--accent-color:#cbb0ff;--font-color-main:#f2f2fb;--background-color:#141b32;}'})
    utils.report_theme = SimpleNamespace(post_theme_context=lambda: {'post_theme':'qiqi','post_mode':'night'}, graph_palette=lambda _: {'accent':'#cbb0ff'})
    monkeypatch.setitem(sys.modules, 'src', host)
    monkeypatch.setitem(sys.modules, 'src.utils', utils)
    result = module.render_theme_reference('zmws')
    assert result['source'] == 'host' and result['mode'] == 'night'
    assert result['palette']['accent'] == '#cbb0ff'
    assert result['palette']['background'] == '#141b32'
    assert '#cbb0ff' in result['css'] and '文字' in result['guidance']


def test_render_theme_standalone_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, 'src', None)
    result = module.render_theme_reference()
    assert result['source'] == 'standalone' and result['palette']['surface']


# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    find_spec("src") is not None,
    reason="the Qiqi src package is importable in this environment",
)
def test_standalone_mode_has_no_host_admission() -> None:
    # The repository test environment intentionally lacks the Qiqi src
    # package, so the soft imports must have degraded to None.
    assert module.admitted_browser_call is None
    assert module._host_compress_image is None


@pytest.mark.asyncio
async def test_run_browser_task_local_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "admitted_browser_call", None, raising=False)

    async def slow() -> bytes:
        await asyncio.sleep(5)
        return b""

    with pytest.raises(RenderToolError, match="超时"):
        await run_browser_task(slow, budget_seconds=0.05)


@pytest.mark.asyncio
async def test_run_browser_task_local_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "admitted_browser_call", None, raising=False)

    async def quick() -> bytes:
        return b"payload"

    assert await run_browser_task(quick, budget_seconds=5) == b"payload"


@pytest.mark.asyncio
async def test_run_browser_task_translates_host_gate_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _QueueFull(RuntimeError):
        pass

    class _TaskTimeout(TimeoutError):
        pass

    async def rejected() -> bytes:
        raise _QueueFull("queue")

    async def timed_out() -> bytes:
        raise _TaskTimeout("timeout")

    async def admitted(call: Any, *args: Any, **kwargs: Any) -> bytes:
        return await call(*args, **kwargs)

    monkeypatch.setattr(module, "admitted_browser_call", admitted, raising=False)
    monkeypatch.setattr(module, "BrowserQueueFull", _QueueFull, raising=False)
    monkeypatch.setattr(module, "BrowserTaskTimeout", _TaskTimeout, raising=False)
    with pytest.raises(RenderToolError, match="队列已满"):
        await run_browser_task(rejected, budget_seconds=5)
    with pytest.raises(RenderToolError, match="超时"):
        await run_browser_task(timed_out, budget_seconds=5)


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://192.168.31.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost:8080/",
        "x" * 2_049,
    ],
)
async def test_ensure_public_tool_url_rejects_unsafe(url: str) -> None:
    with pytest.raises(RenderToolError):
        await ensure_public_tool_url(url)


@pytest.mark.asyncio
async def test_ensure_public_tool_url_accepts_public_literal() -> None:
    await ensure_public_tool_url("http://93.184.216.34/index.html")


@pytest.mark.asyncio
async def test_markdown_precheck_rejects_private_link() -> None:
    markdown = "# 标题\n![图](http://192.168.31.7/pic.png)\n正文"
    with pytest.raises(RenderToolError, match="公网安全检查"):
        await check_markdown_links_public(markdown)


@pytest.mark.asyncio
async def test_markdown_precheck_rejects_too_many_links() -> None:
    markdown = "\n".join(f"[{i}](https://example.com/{i})" for i in range(14))
    with pytest.raises(RenderToolError, match="链接过多"):
        await check_markdown_links_public(markdown)


@pytest.mark.asyncio
async def test_markdown_precheck_accepts_plain_text() -> None:
    await check_markdown_links_public("# 标题\n正文没有链接")


@pytest.mark.asyncio
async def test_hostname_blocked_local_names() -> None:
    cache: dict[str, bool] = {}
    assert await module._hostname_blocked("localhost", cache) is True
    assert await module._hostname_blocked("api.localhost", cache) is True


@pytest.mark.asyncio
async def test_hostname_blocked_literal_ips() -> None:
    cache: dict[str, bool] = {}
    assert await module._hostname_blocked("127.0.0.1", cache) is True
    assert await module._hostname_blocked("10.1.2.3", cache) is True
    assert await module._hostname_blocked("93.184.216.34", cache) is False


@pytest.mark.asyncio
async def test_hostname_blocked_fails_closed_on_resolver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        raise OSError("resolver down")

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", broken_getaddrinfo)
    cache: dict[str, bool] = {}
    assert await module._hostname_blocked("example.com", cache) is True


@pytest.mark.asyncio
async def test_hostname_blocked_resolved_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    cache: dict[str, bool] = {}
    assert await module._hostname_blocked("example.com", cache) is False
    # second call must hit the cache, not the resolver
    assert await module._hostname_blocked("example.com", cache) is False


@pytest.mark.asyncio
async def test_hostname_blocked_resolved_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_getaddrinfo(*args: Any, **kwargs: Any) -> Any:
        return [(2, 1, 6, "", ("10.0.0.5", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    assert await module._hostname_blocked("intranet.example", {}) is True


# ---------------------------------------------------------------------------
# Image guard and helpers
# ---------------------------------------------------------------------------


def _tiny_png() -> bytes:
    # A verified 1x1 RGB PNG so these tests never require Pillow to run;
    # Pillow is an optional extra and absent in the standalone dev venv.
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def test_sniff_image_mime() -> None:
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert sniff_image_mime(b"\xff\xd8\xffabc") == "image/jpeg"
    assert sniff_image_mime(b"not-an-image") == "application/octet-stream"


def test_guard_passes_small_payload() -> None:
    payload = _tiny_png()
    assert guarded_image_bytes(payload) is payload


def test_guard_rejects_empty_payload() -> None:
    with pytest.raises(RenderToolError, match="为空"):
        guarded_image_bytes(b"")


def test_guard_rejects_oversize_garbage() -> None:
    with pytest.raises(RenderToolError, match="大小限制"):
        guarded_image_bytes(b"\x00garbage" + b"0" * (8 * 1_024 * 1_024))


def test_guard_degrades_oversize_png_to_jpeg() -> None:
    pytest.importorskip("PIL")
    from PIL import Image

    # Noise barely compresses in PNG form, guaranteeing the >8MiB branch.
    noise = Image.effect_noise((3600, 3600), 128).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    raw = buffer.getvalue()
    if len(raw) <= 8 * 1_024 * 1_024:  # pragma: no cover - size guard for variance
        pytest.skip("generated PNG below degrade threshold")
    degraded = guarded_image_bytes(raw)
    assert degraded[:3] == b"\xff\xd8\xff"
    assert len(degraded) <= 8 * 1_024 * 1_024


def test_guard_uses_host_compressor_when_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default dev venv has no Pillow, so exercise the >8MiB degrade and
    # >20MiB refuse branches through the host-compressor seam instead.
    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (8 * 1_024 * 1_024)

    def degrading_compressor(payload: bytes) -> bytes:
        assert payload is oversized
        return b"\xff\xd8\xff-degraded"

    monkeypatch.setattr(module, "_host_compress_image", degrading_compressor, raising=False)
    assert guarded_image_bytes(oversized) == b"\xff\xd8\xff-degraded"

    def hopeless_compressor(payload: bytes) -> bytes:
        return b"x" * (20 * 1_024 * 1_024 + 1)

    monkeypatch.setattr(module, "_host_compress_image", hopeless_compressor, raising=False)
    with pytest.raises(RenderToolError, match="20MiB"):
        guarded_image_bytes(oversized)


def test_vision_data_uri_builds_bounded_jpeg() -> None:
    pytest.importorskip("PIL")
    uri = vision_data_uri(_tiny_png())
    assert uri is not None
    assert uri.startswith("data:image/jpeg;base64,")
    decoded = base64.b64decode(uri.split(",", 1)[1])
    assert decoded[:3] == b"\xff\xd8\xff"
    assert len(decoded) <= 2 * 1_024 * 1_024


def test_vision_data_uri_rejects_garbage() -> None:
    assert vision_data_uri(b"not-an-image") is None


# ---------------------------------------------------------------------------
# Tool handlers with faked renderer / bot
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, event: Any, message: Any) -> None:
        self.sent.append(message)


class _FakeRenderer:
    def __init__(self, payload: bytes | Exception = b"") -> None:
        self.payload = payload
        self.html_calls: list[dict[str, Any]] = []
        self.markdown_calls: list[dict[str, Any]] = []
        self.context_calls: list[dict[str, Any]] = []

    async def render_html(self, request: Any, **kwargs: Any) -> bytes:
        self.html_calls.append({"request": request, "kwargs": kwargs})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    async def render_markdown(self, *args: Any, **kwargs: Any) -> bytes:
        self.markdown_calls.append({"args": args, "kwargs": kwargs})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def get_render_context(self, **kwargs: Any) -> "_FakePageContext":
        self.context_calls.append(kwargs)
        return _FakePageContext(self)


class _FakePageContext:
    def __init__(self, renderer: _FakeRenderer) -> None:
        self.renderer = renderer

    async def __aenter__(self) -> "_FakePage":
        self.page = _FakePage(self.renderer.payload)
        self.renderer._page_for_last_context = self.page
        return self.page

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakePage:
    def __init__(self, payload: bytes | Exception = b"") -> None:
        self.payload = payload
        self.routes: list[Any] = []
        self.gone_to: list[str] = []
        self.screenshot_calls: list[dict[str, Any]] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.gone_to.append(url)

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, script: str) -> dict[str, int]:
        return {"h": 2_500, "w": 900}

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.screenshot_calls.append(kwargs)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _FakeRoute:
    def __init__(self) -> None:
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


@pytest.fixture
def available_capability(monkeypatch: pytest.MonkeyPatch) -> RenderCapability:
    capability = RenderCapability(True, "test-bundle")
    monkeypatch.setattr(module, "RENDER_CAPABILITY", capability, raising=False)
    return capability


def _context(bot: _FakeBot | None = None) -> ToolContext:
    return ToolContext(bot=bot or _FakeBot(), event=object())


PIL_AVAILABLE = find_spec("PIL") is not None


class _StubContent:
    def __init__(self, html: str, wait_until: str = "", additional_wait: int = 0) -> None:
        self.html = html
        self.wait_until = wait_until
        self.additional_wait = additional_wait


class _StubRequest:
    def __init__(self, content: Any, render: Any = None) -> None:
        self.content = content
        self.render = render


def _install_htmlrender_models_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the htmlrender request models so handler tests need no browser."""

    import types

    stub = types.ModuleType("nonebot_plugin_htmlrender.backend.playwright.models")

    def _passthrough(*args: Any, **kwargs: Any):  # config dataclasses
        return types.SimpleNamespace(**kwargs)

    stub.ContentConfig = _StubContent
    stub.HtmlRenderRequest = _StubRequest
    stub.PageConfig = _passthrough
    stub.RenderConfig = _passthrough
    stub.PngScreenshotOptions = _passthrough
    stub.ViewportConfig = _passthrough
    monkeypatch.setitem(sys.modules, "nonebot_plugin_htmlrender.backend.playwright.models", stub)


def _assert_vision_feedback(result: ToolResult) -> None:
    """Vision feedback needs Pillow; without it delivery still succeeds."""

    if PIL_AVAILABLE:
        assert result.images
        assert result.images[0].startswith("data:image/jpeg;base64,")
    else:
        assert result.images == ()
        assert "未附加视觉回传" in result.text


@pytest.mark.asyncio
async def test_render_html_tool_happy_path(monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability) -> None:
    _install_htmlrender_models_stub(monkeypatch)
    payload = _tiny_png()
    renderer = _FakeRenderer(payload)
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    bot = _FakeBot()
    result = await module.render_html_image(_tool_context=_context(bot), html="<p>你好</p>", width=800)
    assert isinstance(result, ToolResult)
    assert "已完成渲染" in result.text
    _assert_vision_feedback(result)
    assert len(bot.sent) == 1
    # SSTI posture: content rides in ContentConfig.html and renders offline.
    call = renderer.html_calls[0]
    assert call["request"].content.html == "<p>你好</p>"
    assert call["request"].content.wait_until == "domcontentloaded"
    assert call["kwargs"]["offline"] is True


@pytest.mark.asyncio
async def test_render_html_tool_validates_input(
    available_capability: RenderCapability,
) -> None:
    too_long = "x" * (module._HTML_MAX_CHARS + 1)
    empty = await module.render_html_image(_tool_context=_context(), html="   ")
    assert "HTML" in empty.text
    oversize = await module.render_html_image(_tool_context=_context(), html=too_long)
    assert "HTML" in oversize.text
    bad_width = await module.render_html_image(
        _tool_context=_context(),
        html="<p>x</p>",
        width=True,  # type: ignore[arg-type]
    )
    assert "宽度" in bad_width.text


@pytest.mark.asyncio
async def test_render_tool_reports_unavailable_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "RENDER_CAPABILITY",
        RenderCapability(False, "没有浏览器"),
        raising=False,
    )
    called = False

    async def must_not_render() -> Any:
        nonlocal called
        called = True
        raise AssertionError("renderer must not be touched")

    monkeypatch.setattr(module, "get_shared_renderer", must_not_render)
    result = await module.render_html_image(_tool_context=_context(), html="<p>x</p>")
    assert "渲染工具未启用" in result.text
    assert called is False


@pytest.mark.asyncio
async def test_render_html_tool_wraps_renderer_failure(
    monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability
) -> None:
    _install_htmlrender_models_stub(monkeypatch)
    renderer = _FakeRenderer(RuntimeError("boom"))
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    result = await module.render_html_image(_tool_context=_context(), html="<p>x</p>")
    assert "渲染失败" in result.text
    assert not result.images


@pytest.mark.asyncio
async def test_render_markdown_tool_happy_path(monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability) -> None:
    renderer = _FakeRenderer(_tiny_png())
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    bot = _FakeBot()
    result = await module.render_markdown_image(_tool_context=_context(bot), markdown="# 标题\n正文", width=800)
    assert "已完成渲染" in result.text
    assert len(bot.sent) == 1
    call = renderer.markdown_calls[0]
    # Real htmlrender 0.7.2 contract: markdown rides the positional
    # markdown_text parameter, never an ``md`` keyword (see the live smoke
    # test below, which exercises the same shape against the real library).
    assert call["args"] == ("# 标题\n正文",)
    assert "md" not in call["kwargs"]
    assert call["kwargs"]["width"] == 800


@pytest.mark.asyncio
async def test_render_markdown_tool_blocks_private_link(
    monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability
) -> None:
    renderer = _FakeRenderer(_tiny_png())
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    result = await module.render_markdown_image(_tool_context=_context(), markdown="![x](http://10.0.0.8/a.png)")
    assert "公网安全检查" in result.text
    assert renderer.markdown_calls == []


@pytest.mark.asyncio
async def test_screenshot_tool_rejects_private_url_before_browser(
    monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability
) -> None:
    async def must_not_render() -> Any:
        raise AssertionError("renderer must not be touched")

    monkeypatch.setattr(module, "get_shared_renderer", must_not_render)
    result = await module.screenshot_public_url(_tool_context=_context(), url="http://192.168.0.9/private")
    assert "公网安全检查" in result.text


@pytest.mark.asyncio
async def test_screenshot_tool_happy_path(monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability) -> None:
    renderer = _FakeRenderer(_tiny_png())
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    bot = _FakeBot()
    result = await module.screenshot_public_url(_tool_context=_context(bot), url="http://93.184.216.34/", width=800)
    assert "已完成渲染" in result.text
    assert result.metadata["url"] == "http://93.184.216.34"
    assert len(bot.sent) == 1
    assert renderer.context_calls[0]["viewport"]["width"] == 800


@pytest.mark.asyncio
async def test_screenshot_tool_full_page_clips_height(
    monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability
) -> None:
    renderer = _FakeRenderer(_tiny_png())
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    result = await module.screenshot_public_url(_tool_context=_context(), url="http://93.184.216.34/", full_page=True)
    assert "已完成渲染" in result.text
    page = renderer._page_for_last_context
    clip = page.screenshot_calls[0]["clip"]
    assert clip["height"] <= module._MAX_SCREENSHOT_HEIGHT
    assert page.gone_to == ["http://93.184.216.34/"]


@pytest.mark.asyncio
async def test_screenshot_route_guard_blocks_private_subrequest(
    monkeypatch: pytest.MonkeyPatch, available_capability: RenderCapability
) -> None:
    renderer = _FakeRenderer(_tiny_png())
    monkeypatch.setattr(module, "get_shared_renderer", _as_async(renderer))
    context = _context()
    await module.screenshot_public_url(_tool_context=context, url="http://93.184.216.34/")
    page = renderer._page_for_last_context
    assert page.routes, "route guard must be installed"
    _, handler = page.routes[0]
    route = _FakeRoute()
    await handler(route, _FakeRequest("http://10.0.0.1/ads.js"))
    assert route.aborted is True
    public_route = _FakeRoute()
    await handler(public_route, _FakeRequest("http://93.184.216.34/style.css"))
    assert public_route.continued is True
    bad_scheme = _FakeRoute()
    await handler(bad_scheme, _FakeRequest("file:///etc/passwd"))
    assert bad_scheme.aborted is True


@pytest.mark.asyncio
async def test_deliver_image_requires_bot_and_event() -> None:
    with pytest.raises(RenderToolError, match="不支持图片发送"):
        await deliver_image(ToolContext(bot=None, event=None), b"x")


def _as_async(value: Any):
    async def factory() -> Any:
        return value

    return factory


def test_tool_specs_shape() -> None:
    for spec in (RENDER_HTML_TOOL, RENDER_MARKDOWN_TOOL, SCREENSHOT_URL_TOOL):
        assert spec.permission == "user"
        assert spec.effect.value == "read_only"
        assert spec.parameters["additionalProperties"] is False
    assert SCREENSHOT_URL_TOOL.timeout_seconds is not None
    assert SCREENSHOT_URL_TOOL.policy is not None


# ---------------------------------------------------------------------------
# Live-library smoke tests (they run only where the real deps exist)
# ---------------------------------------------------------------------------

_HOST_BROWSERS_DIR = "/app/playwright-browsers"
_HOST_BROWSERS_AVAILABLE = os.path.isdir(_HOST_BROWSERS_DIR)
_HOST_ADMISSION_MODULE = "/app/qi-dev/src/utils/browser_admission.py"
_HOST_ADMISSION_AVAILABLE = os.path.isfile(_HOST_ADMISSION_MODULE)


@pytest.mark.asyncio
async def test_live_htmlrender_contract_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render through the installed htmlrender exactly like the tools do.

    Guards the two integration points mocks cannot prove: the top-level
    ``get_default_render`` import (0.7.2 does not export ``create_render``)
    and the positional ``markdown_text`` forwarding into the backend's
    ``md`` parameter.
    """

    pytest.importorskip("nonebot_plugin_htmlrender")
    if not _HOST_BROWSERS_AVAILABLE:  # pragma: no cover - env gate
        pytest.skip("no playwright browsers available in this environment")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _HOST_BROWSERS_DIR)
    # The suite inits NoneBot without htmlrender env keys, so point the
    # already-cached plugin config at the playwright backend for this test.
    from nonebot_plugin_htmlrender.config import plugin_config
    from nonebot_plugin_htmlrender.consts import RenderBackend

    monkeypatch.setattr(plugin_config, "render_backend", RenderBackend.PLAYWRIGHT)
    from nonebot_plugin_htmlrender import get_default_render, shutdown_render

    renderer = get_default_render()
    try:
        jpeg = await renderer.render_markdown(
            "# 冒烟\n正文一行",
            width=480,
            image_type="jpeg",
            screenshot_timeout=15_000,
        )
        assert jpeg[:3] == b"\xff\xd8\xff"
        from nonebot_plugin_htmlrender.backend.playwright.models import (
            ContentConfig,
            HtmlRenderRequest,
        )

        request = HtmlRenderRequest(
            content=ContentConfig(
                html="<p>ok</p>",
                wait_until="domcontentloaded",
                additional_wait=100,
            )
        )
        png = await renderer.render_html(request, offline=True, screenshot_timeout=15_000)
        assert png[:4] == b"\x89PNG"
    finally:
        await shutdown_render()


@pytest.mark.asyncio
async def test_live_host_browser_admission_import_smoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import and drive the real Qiqi host admission gate when reachable."""

    if not _HOST_ADMISSION_AVAILABLE:  # pragma: no cover - env gate
        pytest.skip("Qiqi host admission module not available in this environment")
    monkeypatch.syspath_prepend("/app/qi-dev")
    # Re-run the soft import against the real module, bypassing the module
    # cache decision made at import time in this environment.  The qi_web
    # service tree drags interpreter-level deps (argon2, ...), so a missing
    # dependency simply means this interpreter cannot run the host gate.
    try:
        from src.utils.browser_admission import admitted_browser_call
    except ModuleNotFoundError as error:  # pragma: no cover - venv dependent
        pytest.skip(f"host admission deps unavailable: {error.name}")

    async def probe() -> bytes:
        return b"admitted"

    assert await admitted_browser_call(probe) == b"admitted"

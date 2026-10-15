"""Trusted LLM tools that turn HTML / Markdown / public URLs into group images.

Security posture (aligned with docs/llm-ai-capability-expansion-survey.md):

- SSTI: LLM-provided HTML / Markdown is handed to htmlrender as a plain
  string (``ContentConfig.html`` / the positional ``markdown_text`` of
  ``render_markdown``).  Content never enters a Jinja template source, so
  it cannot be re-parsed as template code; only ``render_template`` is a
  template path and it is not used here.
- ``render_html`` renders offline (``offline=True``) with an explicit
  ``domcontentloaded`` wait, so semi-trusted content makes no outbound
  requests and cannot stall on slow external links.  ``render_markdown``
  has no offline flag, so referenced links get a text-level public-address
  pre-check plus a whole-coroutine timeout.
- ``screenshot_url`` validates the URL through ``network_safety`` (http or
  https only, DNS-level loopback/private/reserved rejection), then a route
  guard inspects every browser subrequest and navigation redirect with the
  same address policy before the request leaves the process.
- Browser concurrency: inside the Qiqi host this module reuses
  ``src.utils.browser_admission`` (2 active / 8 waiting / dual 120s
  budget); standalone installs fall back to a module-local semaphore with
  the same shape.  Nothing downloads or launches a browser on its own.
- Image guard: payloads over 8 MiB degrade to JPEG (the host reuses
  ``qi_zmws.image_encode.compress_image`` when importable; standalone uses
  its own Pillow loop with identical constants), payloads still over
  20 MiB after degrading are refused, and the 60000px edge cap applies.
  Images travel in memory only; no temporary files are written.
- Capability probing: explicit configuration first, then htmlrender's
  Playwright-managed browsers, then a Chromium found on PATH.  When
  nothing is usable the tools are not registered, a log line explains the
  remedy, and the rest of the plugin keeps working.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib.util import find_spec
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
from typing import Any
from urllib.parse import urlsplit

from nonebot import get_driver
from nonebot.log import logger

from .config import render_tools_config
from .network_safety import validate_public_url
from .tool_contracts import (
    ToolCapabilityV2,
    ToolContext,
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    register_tool,
)

try:  # Qiqi host enhancement; standalone installs keep working without it.
    from src.utils.browser_admission import (
        BrowserQueueFull,
        BrowserTaskTimeout,
        admitted_browser_call,
    )
except ModuleNotFoundError as error:  # pragma: no cover - host-only module
    if error.name not in {"src", "src.utils", "src.utils.browser_admission"}:
        raise
    BrowserQueueFull = BrowserTaskTimeout = None  # type: ignore[assignment]
    admitted_browser_call = None

# Importing a host plugin's child module also initializes its package, which
# can require this plugin before its public ToolSpec exports are available.
# Reuse an already-loaded compressor only when an image actually needs it.
_host_compress_image = None

try:
    from nonebot.adapters.onebot.v11 import MessageSegment
except Exception:  # pragma: no cover - adapter is a hard plugin dependency
    MessageSegment = None  # type: ignore[assignment]


class RenderToolError(RuntimeError):
    """Friendly, non-retryable failure that maps to a plain ToolResult."""


_HTML_MAX_CHARS = 80_000
_MARKDOWN_MAX_CHARS = 100_000
_URL_MAX_CHARS = 2_048
_WIDTH_MIN = 320
_WIDTH_MAX = 1_600
_SCREENSHOT_TIMEOUT_MS = 20_000
_RENDER_BUDGET_SECONDS = 45.0
_SCREENSHOT_BUDGET_SECONDS = 60.0
_LOCAL_RENDER_CONCURRENCY = 2
_MAX_SCREENSHOT_HEIGHT = 16_000
_MAX_SCREENSHOT_WIDTH = 2_400
_MARKDOWN_URL_PRECHECK_LIMIT = 12
# Same delivery red lines as the host message_fx guard.
_IMAGE_DEGRADE_BYTES = 8 * 1_024 * 1_024
_IMAGE_REFUSE_BYTES = 20 * 1_024 * 1_024
_IMAGE_MAX_EDGE = 60_000
# Vision feedback stays well under ToolResult's 4 MiB per-image budget.
_VISION_MAX_EDGE = 1_280
_VISION_JPEG_QUALITY = 72
_VISION_MAX_BYTES = 2 * 1_024 * 1_024

_LOCAL_RENDER_SEMAPHORE = asyncio.Semaphore(_LOCAL_RENDER_CONCURRENCY)
_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)
_PATH_CHROMIUM_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "headless_shell",
)
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"BM", "image/bmp"),
)


# ---------------------------------------------------------------------------
# Capability probing (explicit config -> bundled Playwright -> PATH)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderCapability:
    available: bool
    reason: str

    @property
    def hint(self) -> str:
        return self.reason


def evaluate_render_capability(
    *,
    enabled: bool,
    executable: str,
    executable_exists: bool,
    endpoint: str,
    htmlrender_found: bool,
    playwright_found: bool,
    bundled_browser: bool | None,
    chromium_in_path: str | None,
) -> RenderCapability:
    """Pure probe decision so deployments and tests share one policy."""

    if not enabled:
        return RenderCapability(False, "MOELLM_RENDER_ENABLED=false 已关闭渲染工具")
    if not htmlrender_found:
        return RenderCapability(
            False,
            "缺少 nonebot-plugin-htmlrender；安装该可选依赖后重启即可启用渲染工具",
        )
    if not playwright_found:
        return RenderCapability(
            False,
            "缺少 playwright；安装 playwright 并执行 playwright install chromium 后重启",
        )
    if executable:
        if not executable_exists:
            return RenderCapability(
                False,
                f"MOELLM_RENDER_BROWSER_EXECUTABLE 指向的浏览器不存在: {executable}",
            )
        return RenderCapability(True, "explicit-executable")
    if endpoint:
        scheme = urlsplit(endpoint).scheme.casefold()
        if scheme not in {"http", "https", "ws", "wss"}:
            return RenderCapability(
                False,
                "MOELLM_RENDER_BROWSER_ENDPOINT 仅支持 http/https（CDP）或 ws/wss 端点",
            )
        return RenderCapability(True, "explicit-endpoint")
    if bundled_browser:
        return RenderCapability(True, "playwright-bundled-browser")
    if chromium_in_path:
        return RenderCapability(True, f"path-chromium:{chromium_in_path}")
    return RenderCapability(
        False,
        "未找到可用浏览器：执行 playwright install chromium，或配置 "
        "MOELLM_RENDER_BROWSER_EXECUTABLE / MOELLM_RENDER_BROWSER_ENDPOINT，"
        "或在 PATH 中提供 chromium",
    )


def _bundled_playwright_browser_present() -> bool | None:
    """Ask htmlrender whether its Playwright browsers are installed."""

    try:
        from nonebot_plugin_htmlrender.backend.playwright.runtime import (
            has_installed_browser,
        )
        from nonebot_plugin_htmlrender.consts import BrowserEngine
    except Exception:
        return None
    try:
        return bool(has_installed_browser(BrowserEngine.CHROMIUM))
    except Exception:
        return None


def _chromium_in_path() -> str | None:
    for candidate in _PATH_CHROMIUM_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def probe_render_capability() -> RenderCapability:
    executable = render_tools_config.moellm_render_browser_executable.strip()
    endpoint = render_tools_config.moellm_render_browser_endpoint.strip()
    return evaluate_render_capability(
        enabled=render_tools_config.moellm_render_enabled,
        executable=executable,
        executable_exists=bool(executable) and os.path.isfile(executable),
        endpoint=endpoint,
        htmlrender_found=find_spec("nonebot_plugin_htmlrender") is not None,
        playwright_found=find_spec("playwright") is not None,
        bundled_browser=_bundled_playwright_browser_present(),
        chromium_in_path=_chromium_in_path(),
    )


RENDER_CAPABILITY = probe_render_capability()


# ---------------------------------------------------------------------------
# Shared browser session (htmlrender owns the lifecycle)
# ---------------------------------------------------------------------------


def _session_overrides() -> dict[str, Any]:
    """Map MOELLM_RENDER_BROWSER_* onto htmlrender BrowserSessionKwargs."""

    overrides: dict[str, Any] = {}
    executable = render_tools_config.moellm_render_browser_executable.strip()
    endpoint = render_tools_config.moellm_render_browser_endpoint.strip()
    if executable:
        overrides["executable_path"] = executable
    if endpoint:
        scheme = urlsplit(endpoint).scheme.casefold()
        if scheme in {"http", "https"}:
            overrides["endpoint_url"] = endpoint
        elif scheme in {"ws", "wss"}:
            overrides["endpoint"] = endpoint
    return overrides


async def get_shared_renderer() -> Any:
    """Return htmlrender's default Render, honoring explicit browser config.

    htmlrender 0.7.2 only re-exports ``get_default_render`` (not
    ``create_render``) at the top level, and the default instance is the one
    its own driver hooks shut down, so sharing it avoids leaking a browser.
    Passing session kwargs on every call is intentional: htmlrender returns
    the cached live session on a hit and only applies the kwargs (our
    executable / endpoint overrides) when it has to start a fresh one.
    """

    from nonebot_plugin_htmlrender import get_default_render

    renderer = get_default_render()
    overrides = _session_overrides()
    if overrides:
        await renderer.get_render(**overrides)
    return renderer


async def run_browser_task(
    operation: Callable[[], Awaitable[bytes]],
    *,
    budget_seconds: float,
) -> bytes:
    """Run one browser job inside the host admission gate or local semaphore."""

    if admitted_browser_call is not None:
        try:
            return await admitted_browser_call(operation)
        except RenderToolError:
            raise
        except BrowserQueueFull as error:  # type: ignore[misc]
            raise RenderToolError("浏览器渲染队列已满，请稍后再试。") from error
        except BrowserTaskTimeout as error:  # type: ignore[misc]
            raise RenderToolError("浏览器渲染排队或执行超时，请稍后再试。") from error
    async with _LOCAL_RENDER_SEMAPHORE:
        try:
            return await asyncio.wait_for(operation(), timeout=budget_seconds)
        except asyncio.TimeoutError as error:
            raise RenderToolError("浏览器渲染超时，请稍后再试。") from error


# ---------------------------------------------------------------------------
# URL / address policy (strict for every LLM-supplied URL)
# ---------------------------------------------------------------------------


def _is_blocked_address(value: str) -> bool:
    """Mirror network_safety: everything not globally routable is blocked."""

    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    return not address.is_global


def _hostname_is_local(hostname: str) -> bool:
    name = hostname.casefold().rstrip(".")
    return name in {"localhost"} or name.endswith(".localhost")


async def _hostname_blocked(hostname: str, cache: dict[str, bool]) -> bool:
    """Fail-closed address check for one browser subrequest hostname."""

    if _hostname_is_local(hostname):
        return True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return _is_blocked_address(hostname)
    cached = cache.get(hostname)
    if cached is not None:
        return cached
    try:
        loop = asyncio.get_running_loop()
        records = await asyncio.wait_for(
            loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM),
            timeout=2.0,
        )
    except Exception:
        cache[hostname] = True
        return True
    addresses: set[str] = set()
    for record in records:
        try:
            addresses.add(str(record[4][0]).split("%", 1)[0])
        except (IndexError, TypeError):  # pragma: no cover - odd resolver rows
            cache[hostname] = True
            return True
    blocked = not addresses or any(_is_blocked_address(item) for item in addresses)
    cache[hostname] = blocked
    return blocked


async def ensure_public_tool_url(url: str) -> None:
    """Validate one LLM-supplied URL with the package's DNS-level policy."""

    if not isinstance(url, str) or not 1 <= len(url) <= _URL_MAX_CHARS:
        raise RenderToolError("网址必须是 1～2048 字的 http/https URL。")
    try:
        await validate_public_url(url)
    except Exception as error:
        raise RenderToolError("网址未通过公网安全检查（仅允许 http/https 公网地址）。") from error


async def check_markdown_links_public(markdown_text: str) -> None:
    """Reject Markdown whose referenced URLs touch non-public addresses."""

    candidates: list[str] = []
    for match in _URL_RE.finditer(markdown_text):
        value = match.group(0).rstrip(".,;:!?")
        if value not in candidates:
            candidates.append(value)
        if len(candidates) > _MARKDOWN_URL_PRECHECK_LIMIT:
            raise RenderToolError("Markdown 引用的链接过多，请减少外链后重试。")
    for value in candidates:
        await ensure_public_tool_url(value)


# ---------------------------------------------------------------------------
# Image guard + delivery
# ---------------------------------------------------------------------------


def _standalone_compress_image(payload: bytes) -> bytes:
    """Pillow fallback mirroring qi_zmws.image_encode quality/resize loop."""

    from PIL import Image

    image = Image.open(io.BytesIO(payload))
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    quality = 85
    for _ in range(8):
        if max(image.size) > _IMAGE_MAX_EDGE:
            ratio = _IMAGE_MAX_EDGE / max(image.size)
            image = image.resize(
                (
                    max(1, round(image.width * ratio)),
                    max(1, round(image.height * ratio)),
                ),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
        )
        encoded = buffer.getvalue()
        if len(encoded) <= _IMAGE_DEGRADE_BYTES:
            return encoded
        ratio = max(0.5, (_IMAGE_DEGRADE_BYTES / len(encoded)) ** 0.5)
        image = image.resize(
            (
                max(1, round(image.width * ratio)),
                max(1, round(image.height * ratio)),
            ),
            Image.Resampling.LANCZOS,
        )
        quality = max(60, quality - 5)
    raise ValueError("图片压缩后仍超出大小限制")


def guarded_image_bytes(payload: bytes) -> bytes:
    """Apply the 8 MiB degrade / 20 MiB refuse / 60000px delivery guard."""

    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise RenderToolError("渲染结果为空，未生成图片。")
    data = bytes(payload)
    if len(data) <= _IMAGE_DEGRADE_BYTES:
        return data
    host_guard = sys.modules.get("src.plugins.qi_zmws.image_encode")
    compressor = (
        _host_compress_image
        or getattr(host_guard, "compress_image", None)
        or _standalone_compress_image
    )
    try:
        compressed = compressor(data)
    except Exception as error:
        raise RenderToolError("渲染结果超出图片大小限制，已拒发。") from error
    if len(compressed) > _IMAGE_REFUSE_BYTES:
        raise RenderToolError("渲染结果超出图片大小上限（20MiB），已拒发。")
    return compressed


def sniff_image_mime(payload: bytes) -> str:
    for signature, mime in _IMAGE_SIGNATURES:
        if payload.startswith(signature):
            return mime
    return "application/octet-stream"


def vision_data_uri(payload: bytes) -> str | None:
    """Build a bounded JPEG data URI for the model's vision feedback."""

    try:
        from PIL import Image
    except Exception:
        return None
    try:
        image = Image.open(io.BytesIO(payload))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((_VISION_MAX_EDGE, _VISION_MAX_EDGE))
        buffer = io.BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=_VISION_JPEG_QUALITY,
            optimize=True,
        )
        encoded = buffer.getvalue()
    except Exception:
        return None
    if not encoded or len(encoded) > _VISION_MAX_BYTES:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")


async def deliver_image(context: ToolContext, payload: bytes) -> None:
    """Send the finished picture into the triggering conversation."""

    bot = getattr(context, "bot", None)
    event = getattr(context, "event", None)
    if bot is None or event is None or MessageSegment is None:
        raise RenderToolError("当前会话不支持图片发送，渲染结果已丢弃。")
    try:
        from .request_recovery import durable_send

        await durable_send(bot, event, MessageSegment.image(payload))
    except Exception as error:
        raise RenderToolError("图片已生成但发送失败，请稍后再试。") from error


def _finish_image_result(
    payload: bytes,
    *,
    capability_note: str,
    extra_metadata: Mapping[str, Any] | None = None,
) -> ToolResult:
    vision = vision_data_uri(payload)
    metadata: dict[str, Any] = {
        "bytes": len(payload),
        "mime": sniff_image_mime(payload),
        "backend": capability_note,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    text = "✅ 已完成渲染并把图片发送到当前会话。"
    if vision is None:
        text += "（图片体积较大，未附加视觉回传）"
    return ToolResult(
        text=text,
        images=(vision,) if vision else (),
        metadata=metadata,
    )


def _normalize_width(value: Any) -> int:
    default = render_tools_config.moellm_render_default_width
    if value is None:
        width = default
    elif isinstance(value, bool) or not isinstance(value, int):
        raise RenderToolError("宽度参数必须是 320～1600 的整数像素值。")
    else:
        width = value
    return max(_WIDTH_MIN, min(_WIDTH_MAX, width))


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def render_html_image(
    *,
    _tool_context: ToolContext,
    html: str,
    width: int | None = None,
    full_page: bool = True,
) -> ToolResult:
    """Render one LLM-authored HTML document offline and send it as a picture."""

    if not RENDER_CAPABILITY.available:
        return ToolResult(text=f"渲染工具未启用：{RENDER_CAPABILITY.hint}")
    if not isinstance(html, str) or not html.strip() or len(html) > _HTML_MAX_CHARS:
        return ToolResult(text=f"HTML 内容必须是 1～{_HTML_MAX_CHARS} 字的字符串。")
    try:
        render_width = _normalize_width(width)
    except RenderToolError as error:
        return ToolResult(text=str(error))

    async def operation() -> bytes:
        from nonebot_plugin_htmlrender.backend.playwright.models import (
            ContentConfig,
            HtmlRenderRequest,
            PageConfig,
            PngScreenshotOptions,
            RenderConfig,
            ViewportConfig,
        )

        renderer = await get_shared_renderer()
        # The LLM string goes in as ContentConfig.html only: htmlrender calls
        # page.set_content with it, it is never Jinja-compiled.
        request = HtmlRenderRequest(
            content=ContentConfig(
                html=html,
                wait_until="domcontentloaded",
                additional_wait=300,
            ),
            render=RenderConfig(
                page=PageConfig(
                    viewport=ViewportConfig(
                        width=render_width,
                        height=max(_WIDTH_MIN, round(render_width * 0.6)),
                    ),
                ),
                screenshot=PngScreenshotOptions(
                    device_scale_factor=2.0,
                    timeout=_SCREENSHOT_TIMEOUT_MS,
                    full_page=full_page is not False,
                    wait_before_screenshot=100,
                ),
            ),
        )
        return await renderer.render_html(
            request,
            offline=True,
            screenshot_timeout=_SCREENSHOT_TIMEOUT_MS,
            full_page=full_page is not False,
        )

    try:
        payload = await run_browser_task(operation, budget_seconds=_RENDER_BUDGET_SECONDS)
        payload = guarded_image_bytes(payload)
        await deliver_image(_tool_context, payload)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("render_html 工具失败：error_type={}", type(error).__name__)
        return ToolResult(text="❌ HTML 渲染失败，请简化内容后重试。")
    return _finish_image_result(payload, capability_note=RENDER_CAPABILITY.reason)


async def render_markdown_image(
    *,
    _tool_context: ToolContext,
    markdown: str,
    width: int | None = None,
) -> ToolResult:
    """Render one LLM-authored Markdown document and send it as a picture."""

    if not RENDER_CAPABILITY.available:
        return ToolResult(text=f"渲染工具未启用：{RENDER_CAPABILITY.hint}")
    if not isinstance(markdown, str) or not markdown.strip() or len(markdown) > _MARKDOWN_MAX_CHARS:
        return ToolResult(text=f"Markdown 内容必须是 1～{_MARKDOWN_MAX_CHARS} 字的字符串。")
    try:
        render_width = _normalize_width(width)
        await check_markdown_links_public(markdown)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")

    async def operation() -> bytes:
        renderer = await get_shared_renderer()
        # Positional markdown_text: the facade forwards it to the backend as
        # its own ``md`` parameter, so a kwarg named ``md`` here would ride a
        # less obvious path through the forwarding chain.
        return await renderer.render_markdown(
            markdown,
            width=render_width,
            image_type="jpeg",
            quality=85,
            device_scale_factor=2.0,
            screenshot_timeout=_SCREENSHOT_TIMEOUT_MS,
        )

    try:
        payload = await run_browser_task(operation, budget_seconds=_RENDER_BUDGET_SECONDS)
        payload = guarded_image_bytes(payload)
        await deliver_image(_tool_context, payload)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("render_markdown 工具失败：error_type={}", type(error).__name__)
        return ToolResult(text="❌ Markdown 渲染失败，请精简内容后重试。")
    return _finish_image_result(payload, capability_note=RENDER_CAPABILITY.reason)


async def screenshot_public_url(
    *,
    _tool_context: ToolContext,
    url: str,
    width: int | None = None,
    full_page: bool = False,
) -> ToolResult:
    """Screenshot one user-supplied public web page and send the picture."""

    if not RENDER_CAPABILITY.available:
        return ToolResult(text=f"渲染工具未启用：{RENDER_CAPABILITY.hint}")
    try:
        await ensure_public_tool_url(url)
        render_width = _normalize_width(width)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")

    async def operation() -> bytes:
        renderer = await get_shared_renderer()
        dns_cache: dict[str, bool] = {}

        async def route_guard(route: Any, request: Any) -> None:
            target = request.url
            try:
                parsed = urlsplit(target)
                hostname = (parsed.hostname or "").casefold()
            except ValueError:
                await route.abort()
                return
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or not hostname
                or await _hostname_blocked(hostname, dns_cache)
            ):
                await route.abort()
                return
            await route.continue_()

        async with renderer.get_render_context(
            viewport={"width": render_width, "height": round(render_width * 0.75)},
        ) as page:
            await page.route("**/*", route_guard)
            await page.goto(url, wait_until="domcontentloaded", timeout=_SCREENSHOT_TIMEOUT_MS)
            await page.wait_for_timeout(600)
            clip: dict[str, float] | None = None
            if full_page:
                metrics = await page.evaluate(
                    "() => ({h: document.documentElement.scrollHeight,w: document.documentElement.scrollWidth})"
                )
                height = max(
                    float(render_width * 0.75),
                    min(float(metrics.get("h", 0)) or 0.0, _MAX_SCREENSHOT_HEIGHT),
                )
                page_width = max(
                    float(render_width),
                    min(float(metrics.get("w", 0)) or 0.0, _MAX_SCREENSHOT_WIDTH),
                )
                clip = {"x": 0.0, "y": 0.0, "width": page_width, "height": height}
            return await page.screenshot(
                full_page=False,
                clip=clip,
                timeout=_SCREENSHOT_TIMEOUT_MS,
                type="jpeg",
                quality=88,
            )

    try:
        payload = await run_browser_task(operation, budget_seconds=_SCREENSHOT_BUDGET_SECONDS)
        payload = guarded_image_bytes(payload)
        await deliver_image(_tool_context, payload)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("screenshot_url 工具失败：error_type={}", type(error).__name__)
        return ToolResult(text="❌ 网页截图失败，请稍后再试或更换网址。")
    return _finish_image_result(
        payload,
        capability_note=RENDER_CAPABILITY.reason,
        extra_metadata={"url": urlsplit(url).scheme + "://" + (urlsplit(url).netloc)},
    )


# ---------------------------------------------------------------------------
# Tool specs + registration
# ---------------------------------------------------------------------------

_NETWORK_POLICY = ToolPolicy.configured(ToolCapabilityV2(network_allow=("*",)))

def render_theme_reference(card: str = "general") -> dict[str, Any]:
    """Read the host's published theme without importing any game plugin."""
    if card not in {"general", "zmws"}:
        raise RenderToolError("主题类型应为 general 或 zmws。")
    palette = {"background": "#f5f6fa", "surface": "#ffffff", "text": "#242841",
               "muted": "#58627c", "accent": "#65429a", "border": "#d7d6e8"}
    family, mode, source = "default", "day", "standalone"
    try:
        from src.utils import render_theme, report_theme
    except ModuleNotFoundError as error:
        if error.name not in {"src", "src.utils", "src.utils.render_theme", "src.utils.report_theme"}:
            raise
    else:
        if card == "zmws":
            context = render_theme.stylesheet_context("zmws/zmws.css")
            family, mode = context["theme_family"], context.get("palette_mode", context["theme"])
            selector = "light" if mode == "day" else "dark"
            match = re.search(r':root\[data-theme=[\"\']' + selector + r'[\"\']\]\s*\{([^}]+)', context["theme_css"])
            variables = dict(re.findall(r"--([\w-]+)\s*:\s*([^;{}]+)\s*;", match[1] if match else ""))
            names = {"background": "background-color", "surface": "card-background-color", "text": "font-color-main",
                     "muted": "font-color-sub", "accent": "accent-color", "border": "border-color"}
            palette.update({key: variables[name].strip() for key, name in names.items() if name in variables})
        else:
            context = report_theme.post_theme_context()
            family, mode = context["post_theme"], context["post_mode"]
            palette = report_theme.graph_palette(context)
        source = "host"
    css = ":root{" + ";".join(f"--{key}:{value}" for key, value in palette.items()) + "}"
    css += ("body{margin:0;padding:24px;background:var(--background);color:var(--text);"
            "font-family:Arial,'Noto Sans CJK SC',sans-serif} "
            ".panel{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px}")
    return {"theme": family, "mode": mode, "source": source, "palette": palette, "css": css,
            "guidance": ("这是当前主题参考，可自行调整 HTML、CSS 和布局。根据用户需要选择文字、图片或其他已授权工具；"
                         "简短回复可直接用文字，表格和图表可选图片。")}


async def get_render_theme(*, _tool_context: ToolContext, card: str = "general") -> ToolResult:
    try:
        reference = await asyncio.to_thread(render_theme_reference, card)
    except Exception as error:
        logger.warning("读取渲染主题失败：error_type={}", type(error).__name__)
        return ToolResult(text="当前主题暂不可读，可以自行选择样式与回复形式。")
    return ToolResult(text=json.dumps(reference, ensure_ascii=False))


RENDER_THEME_TOOL = ToolSpec(
    name="get_render_theme",
    description=("读取当前会话宿主的已发布主题、昼夜模式、配色与基础 CSS，供你自主设计 HTML。"
                 "文字或图片由你按用户需求选择；主题是参考，可自由调整布局和样式。"),
    parameters={"type": "object", "properties": {"card": {"type": "string", "enum": ["general", "zmws"],
                "description": "通用卡或造梦游戏卡，默认 general。"}}, "additionalProperties": False},
    handler=get_render_theme, effect=ToolEffect.READ_ONLY, permission="user", timeout_seconds=10, result_limit=4000,
)

RENDER_HTML_TOOL = ToolSpec(
    name="render_html",
    description=(
        "把完整的 HTML 字符串离线渲染成图片并发到当前会话。"
        "当你需要给用户展示排版内容（卡片、表格、样式化文本）时使用；"
        "HTML 必须自包含（内联 CSS，不引用外部资源），不会发起任何网络请求。"
        "可先调用 get_render_theme 读取当前主题，自由编写与调整 CSS；简短内容也可直接文字回复。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": "完整 HTML 文档字符串，建议内联 CSS、自包含样式。",
                "minLength": 1,
                "maxLength": _HTML_MAX_CHARS,
            },
            "width": {
                "type": "integer",
                "description": "渲染视口宽度（像素），默认 800。",
                "minimum": _WIDTH_MIN,
                "maximum": _WIDTH_MAX,
            },
            "full_page": {
                "type": "boolean",
                "description": "是否截取整个页面，默认 true。",
            },
        },
        "required": ["html"],
        "additionalProperties": False,
    },
    handler=render_html_image,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=150,
    result_limit=4_000,
)

RENDER_MARKDOWN_TOOL = ToolSpec(
    name="render_markdown",
    description=("把 Markdown 文本渲染成图片并发到当前会话。当用户要求展示较长文章、笔记、清单且纯文本不便阅读时使用。"),
    parameters={
        "type": "object",
        "properties": {
            "markdown": {
                "type": "string",
                "description": "Markdown 文本，支持常见语法与表格。",
                "minLength": 1,
                "maxLength": _MARKDOWN_MAX_CHARS,
            },
            "width": {
                "type": "integer",
                "description": "渲染视口宽度（像素），默认 800。",
                "minimum": _WIDTH_MIN,
                "maximum": _WIDTH_MAX,
            },
        },
        "required": ["markdown"],
        "additionalProperties": False,
    },
    handler=render_markdown_image,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=150,
    result_limit=4_000,
)

SCREENSHOT_URL_TOOL = ToolSpec(
    name="screenshot_url",
    description=(
        "对用户提供的公网 http/https 网页截图并发到当前会话。"
        "当用户想看某个网页的样子、页面效果或需要视觉确认时使用；"
        "仅允许公网地址，内网与本机地址会被拒绝。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "用户消息中的完整 http/https 公网网页地址。",
                "minLength": 1,
                "maxLength": _URL_MAX_CHARS,
            },
            "width": {
                "type": "integer",
                "description": "截图视口宽度（像素），默认 800。",
                "minimum": _WIDTH_MIN,
                "maximum": _WIDTH_MAX,
            },
            "full_page": {
                "type": "boolean",
                "description": "是否截取整页（超长页会被截断），默认 false。",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    handler=screenshot_public_url,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=120,
    result_limit=4_000,
    policy=_NETWORK_POLICY,
)

RENDER_TOOL_SPECS: tuple[ToolSpec, ...] = (
    RENDER_THEME_TOOL,
    RENDER_HTML_TOOL,
    RENDER_MARKDOWN_TOOL,
    SCREENSHOT_URL_TOOL,
)


def register_render_tools(
    capability: RenderCapability | None = None,
) -> tuple[str, ...]:
    """Register the render tools when the probe says the runtime is usable."""

    resolved = capability if capability is not None else RENDER_CAPABILITY
    if not resolved.available:
        logger.info("LLM 渲染工具未注册：{}", resolved.hint)
        return ()
    registered: list[str] = []
    for spec in RENDER_TOOL_SPECS:
        try:
            register_tool(spec)
        except ValueError:
            logger.debug("LLM 渲染工具已注册，跳过：{}", spec.name)
            continue
        registered.append(spec.name)
    if registered:
        logger.info("LLM 渲染工具已注册：{}", ", ".join(registered))
    return tuple(registered)


def _schedule_startup_registration() -> None:
    """Register at driver startup instead of at import time.

    The runtime reload transaction builds its registered-tool view from the
    live ``tool_registry`` and validates exact parity, so import-time
    registration would leak package-owned tools into third-party tests that
    pin their own registered universes.  Production still registers before
    the first tool load: this hook is registered earlier in the plugin's
    ``__init__`` than ``_startup_tasks``, so it runs first on startup, and
    the Qiqi host's llm_scripts hook (registered even later) snapshots the
    registry afterwards.
    """

    @get_driver().on_startup
    async def _register_render_tools_on_startup() -> None:
        register_render_tools()


_schedule_startup_registration()

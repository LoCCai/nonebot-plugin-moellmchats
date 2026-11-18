"""Trusted LLM tool that renders memes through a meme-generator backend.

The backend contract mirrors nonebot-plugin-memes-api 0.5.1 (memes
generator 0.2.x): ``POST /image/upload`` returns an ``image_id``,
``POST /memes/{key}`` with ``images``/``texts``/``options`` renders a new
meme and returns another ``image_id``, and ``GET /image/{id}`` streams the
bytes.  ``GET /memes/{key}/info`` exposes each template's image/text count
limits, which are validated before generation so the model gets an
actionable message instead of a backend stack trace.

Template discovery uses the backend's read-only search endpoints:
``GET /meme/search`` maps Chinese keywords (e.g. 打拳 -> punch) to template
keys, and powers both the ``meme_search`` listing tool and the automatic
resolution of non-ASCII ``meme_key`` values inside ``generate_meme``; the
``meme_info`` tool renders ``/memes/{key}/info`` option descriptions for the
model.

Security posture:

- ``MOELLM_MEME_API_BASE`` is administrator configuration: it is only
  scheme-checked (http/https) because the meme backend legitimately lives
  on an intranet host.  Leaving it empty disables the tool entirely.
- Every URL that arrives through tool arguments (meme input images) goes
  through ``network_safety.validate_public_url`` (http/https only,
  DNS-level loopback/private/reserved rejection) and a redirect-by-redirect
  manual follow that re-validates each hop; downloads are size-capped.
- Template keys and backend-returned image ids are strictly pattern-checked
  before they are placed on a request path; search queries are URL-encoded
  and length-capped, and keys returned by ``/meme/search`` are re-checked
  against the same pattern before reuse.
- The finished picture flows through the shared render-tool image guard
  (8 MiB degrade / 20 MiB refuse) and is delivered in memory; no temporary
  files are written.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
from nonebot import get_driver
from nonebot.log import logger
import ujson as json

from .config import render_tools_config
from .render_tools import (
    RenderToolError,
    deliver_image,
    ensure_public_tool_url,
    guarded_image_bytes,
    sniff_image_mime,
    vision_data_uri,
)
from .tool_contracts import (
    ToolCapabilityV2,
    ToolContext,
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    register_tool,
)

_MEME_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MEME_IMAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Chinese meme names such as 打拳 are resolved to template keys through
# /meme/search instead of being rejected outright.
_MEME_QUERY_MAX_CHARS = 32
_MEME_SEARCH_TIMEOUT_SECONDS = 15.0
_MEME_SEARCH_RESULT_LIMIT = 8
_MEME_AMBIGUOUS_CANDIDATES = 6
_MAX_INPUT_IMAGES = 4
_MAX_TEXTS = 10
_TEXT_MAX_CHARS = 200
_MEME_REQUEST_TIMEOUT_SECONDS = 60.0
_IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 20.0
_MAX_DOWNLOAD_BYTES = 8 * 1_024 * 1_024
# Meme results are refused above 20MiB by the delivery guard, so a 32MiB
# read bound only exists to keep a hostile/huge backend reply in check.
_MEME_MAX_RESPONSE_BYTES = 32 * 1_024 * 1_024
_MAX_REDIRECTS = 4
_meme_authorizer: Callable[[ToolContext, str], Awaitable[bool]] | None = None
_MEME_ERROR_MESSAGES: dict[int, str] = {
    410: "meme 后端请求参数错误",
    420: "meme 后端读写失败",
    510: "输入图片解码失败，请换一张图片",
    520: "表情包生成失败",
    530: "模板缺少图片资源",
    540: "生成结果解析失败",
    550: "图片数量不符合模板要求",
    551: "文字数量不符合模板要求",
    560: "文字超出长度限制",
    570: "该模板当前不可用",
}


def register_meme_authorizer(authorizer: Callable[[ToolContext, str], Awaitable[bool]]) -> None:
    global _meme_authorizer
    if not callable(authorizer):
        raise TypeError("表情包权限检查必须可调用")
    _meme_authorizer = authorizer


async def _meme_allowed(context: ToolContext, key: str) -> bool:
    if _meme_authorizer is None:
        return not render_tools_config.moellm_meme_policy_required
    try:
        return await asyncio.wait_for(_meme_authorizer(context, key), timeout=5) is True
    except Exception as error:
        logger.warning("表情包权限检查未通过：error_type={}", type(error).__name__)
        return False


def meme_api_base() -> str:
    return render_tools_config.moellm_meme_api_base.strip().rstrip("/")


def meme_capability_reason() -> str | None:
    """Return None when the meme tool is usable, else the blocking reason."""

    base = meme_api_base()
    if not base:
        return "未配置 MOELLM_MEME_API_BASE，generate_meme 工具未启用"
    scheme = urlsplit(base).scheme.casefold()
    if scheme not in {"http", "https"}:
        return "MOELLM_MEME_API_BASE 仅支持 http/https 地址"
    return None


async def _meme_request(
    method: str,
    path: str,
    *,
    json_payload: Any = None,
    timeout_seconds: float = _MEME_REQUEST_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """Perform one bounded request against the admin-configured backend."""

    base = meme_api_base()
    url = base + path
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=True) as session:
            async with session.request(
                method,
                url,
                json=json_payload,
                headers={"accept": "application/json, image/*"},
            ) as response:
                # Bound the read itself: the 20MiB delivery guard only runs
                # after the bytes are already in memory.
                body = bytearray()
                async for chunk in response.content.iter_chunked(65_536):
                    body.extend(chunk)
                    if len(body) > _MEME_MAX_RESPONSE_BYTES:
                        raise RenderToolError("meme 后端响应超出大小上限，已放弃本次生成。")
    except asyncio.CancelledError:
        raise
    except RenderToolError:
        raise
    except Exception as error:
        raise RenderToolError("meme 后端暂时无法访问，请稍后再试。") from error
    body = bytes(body)
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if "json" in content_type:
        try:
            parsed: Any = json.loads(body)
        except Exception as error:
            raise RenderToolError("meme 后端返回了无法解析的响应。") from error
    else:
        parsed = body
    return response.status, parsed


async def _download_public_image(url: str) -> bytes:
    """Download one LLM-supplied image with strict per-hop validation."""

    await ensure_public_tool_url(url)
    timeout = aiohttp.ClientTimeout(total=_IMAGE_DOWNLOAD_TIMEOUT_SECONDS)
    current = url
    visited = {current}
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            auto_decompress=False,
        ) as session:
            for _ in range(_MAX_REDIRECTS + 1):
                async with session.get(
                    current,
                    allow_redirects=False,
                    headers={
                        "accept": "image/*",
                        "user-agent": "Mozilla/5.0 (compatible; MoEllmChats-MemeTool/1.0)",
                    },
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location or location in visited:
                            raise RenderToolError("图片地址重定向异常，已停止下载。")
                        # Resolve against the current URL, then re-validate the
                        # hop exactly like the original URL.
                        current = urljoin(current, location)
                        if len(current) > 2_048:
                            raise RenderToolError("图片重定向地址过长，已停止下载。")
                        await ensure_public_tool_url(current)
                        visited.add(current)
                        continue
                    if not 200 <= response.status < 300:
                        raise RenderToolError(f"图片下载失败（HTTP {response.status}）。")
                    payload = bytearray()
                    async for chunk in response.content.iter_chunked(65_536):
                        payload.extend(chunk)
                        if len(payload) > _MAX_DOWNLOAD_BYTES:
                            raise RenderToolError("图片超过 8MiB 下载上限。")
                    data = bytes(payload)
                    break
    except asyncio.CancelledError:
        raise
    except RenderToolError:
        raise
    except Exception as error:
        raise RenderToolError("图片下载失败，请稍后再试。") from error
    mime = sniff_image_mime(data)
    if mime == "application/octet-stream":
        raise RenderToolError("该地址不是受支持的图片文件。")
    return data


def _parse_meme_info(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise RenderToolError("meme 模板信息返回异常。")
    params = payload.get("params")
    if not isinstance(params, dict):
        raise RenderToolError("meme 模板信息缺少参数说明。")
    return params


def _optional_int(value: Any) -> int | None:
    """Return the backend-declared limit, treating 0 as an explicit bound."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _validate_meme_arguments(
    params: Mapping[str, Any],
    *,
    images: list[str],
    texts: list[str],
) -> None:
    # A missing/invalid field means "no declared limit"; an explicit 0 from
    # the backend (text-only or image-only templates) does bound the count.
    min_images = _optional_int(params.get("min_images"))
    max_images = _optional_int(params.get("max_images"))
    min_texts = _optional_int(params.get("min_texts"))
    max_texts = _optional_int(params.get("max_texts"))
    if min_images is not None and len(images) < min_images:
        raise RenderToolError(f"该模板至少需要 {min_images} 张图片。")
    if max_images is not None and len(images) > max_images:
        raise RenderToolError(f"该模板最多接受 {max_images} 张图片。")
    if min_texts is not None and len(texts) < min_texts:
        raise RenderToolError(f"该模板至少需要 {min_texts} 条文字。")
    if max_texts is not None and len(texts) > max_texts:
        raise RenderToolError(f"该模板最多接受 {max_texts} 条文字。")


# ---------------------------------------------------------------------------
# Template discovery (read-only /meme/search + /memes/{key}/info)
# ---------------------------------------------------------------------------


def _clean_meme_query(raw: Any) -> str | None:
    """Return a trimmed, length-bounded keyword, or None if unusable."""

    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > _MEME_QUERY_MAX_CHARS:
        return None
    return cleaned


async def _search_meme_keys(query: str) -> list[str]:
    """Map a Chinese/English keyword to template keys via /meme/search."""

    status, payload = await _meme_request(
        "GET",
        f"/meme/search?query={quote(query, safe='')}&include_tags=true",
        timeout_seconds=_MEME_SEARCH_TIMEOUT_SECONDS,
    )
    if status != 200 or not isinstance(payload, list):
        raise RenderToolError("meme 模板搜索失败，请稍后再试。")
    keys: list[str] = []
    for item in payload:
        # Keys come back from the backend; keep the request-path invariant
        # by re-checking the same strict pattern used for tool arguments.
        if isinstance(item, str) and _MEME_KEY_RE.fullmatch(item) and item not in keys:
            keys.append(item)
    return keys


def _describe_count(minimum: Any, maximum: Any) -> str:
    low = _optional_int(minimum)
    high = _optional_int(maximum)
    if low is None and high is None:
        return "不限"
    if low is not None and high is not None:
        return str(low) if low == high else f"{low}-{high}"
    return f"≤{high}" if high is not None else f"≥{low}"


def _format_default(default: Any) -> str:
    if isinstance(default, bool):
        return "true" if default else "false"
    if default is None:
        return "未设置"
    return str(default)


def _summarize_meme_info(payload: Any) -> str:
    """One compact summary line for template listings; missing fields tolerated."""

    if not isinstance(payload, dict):
        return "信息暂不可用"
    parts: list[str] = []
    keywords = payload.get("keywords")
    if isinstance(keywords, list):
        words = [item for item in keywords if isinstance(item, str)]
        if words:
            parts.append("关键词：" + "/".join(words[:6]))
    params = payload.get("params")
    if isinstance(params, dict):
        parts.append("需图片 " + _describe_count(params.get("min_images"), params.get("max_images")) + " 张")
        parts.append("文字 " + _describe_count(params.get("min_texts"), params.get("max_texts")) + " 条")
        options = params.get("options")
        if isinstance(options, list) and options:
            rendered: list[str] = []
            for option in options:
                if isinstance(option, dict) and isinstance(option.get("name"), str):
                    default = option.get("default")
                    rendered.append(option["name"] if default is None else f"{option['name']}={_format_default(default)}")
            if rendered:
                parts.append("选项：" + "、".join(rendered[:6]) + ("…" if len(rendered) > 6 else ""))
    if not parts:
        return "信息暂不可用"
    return "｜".join(parts)


async def _fetch_meme_summary(key: str) -> str:
    """Best-effort one-line summary; never fails the surrounding flow."""

    try:
        status, payload = await _meme_request("GET", f"/memes/{key}/info", timeout_seconds=_MEME_SEARCH_TIMEOUT_SECONDS)
    except RenderToolError:
        return "信息暂不可用"
    if status != 200:
        return "信息暂不可用"
    return _summarize_meme_info(payload)


async def _resolve_meme_keyword(
    keyword: str,
) -> tuple[str | None, list[str]]:
    """Resolve a Chinese keyword to a template key.

    Returns ``(key, matches)``: when exactly one template matches, ``key``
    is that template key and the caller may continue generation; otherwise
    ``key`` is None and ``matches`` holds the (possibly empty) candidate
    list to show back to the model.
    """

    matches = await _search_meme_keys(keyword)
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def _format_option_detail(option: Mapping[str, Any]) -> str:
    name = str(option.get("name", "?"))
    otype = str(option.get("type", "?"))
    constraints: list[str] = []
    if option.get("minimum") is not None:
        constraints.append(f"最小 {option.get('minimum')}")
    if option.get("maximum") is not None:
        constraints.append(f"最大 {option.get('maximum')}")
    choices = option.get("choices")
    if isinstance(choices, list) and choices:
        constraints.append("可选 " + "/".join(str(choice) for choice in choices[:8]))
    line = f"- {name}（{otype}，默认 {_format_default(option.get('default'))}）"
    if constraints:
        line += "［" + "，".join(constraints) + "］"
    description = option.get("description")
    if isinstance(description, str) and description.strip():
        line += "：" + description.strip()
    return line


def _format_meme_details(payload: Any) -> str:
    """Render one /memes/{key}/info payload as readable text for the model."""

    if not isinstance(payload, dict):
        return "模板信息返回异常。"
    lines: list[str] = [f"模板 {payload.get('key', '?')}"]
    keywords = payload.get("keywords")
    words = [item for item in keywords if isinstance(item, str)] if isinstance(keywords, list) else []
    lines.append("关键词：" + ("、".join(words) if words else "（无）"))
    tags = payload.get("tags")
    tag_names = sorted(str(tag) for tag in tags) if isinstance(tags, list) else []
    lines.append("标签：" + ("、".join(tag_names) if tag_names else "（无）"))
    params = payload.get("params")
    if isinstance(params, dict):
        lines.append("图片：需 " + _describe_count(params.get("min_images"), params.get("max_images")) + " 张")
        lines.append("文字：" + _describe_count(params.get("min_texts"), params.get("max_texts")) + " 条")
        defaults = params.get("default_texts")
        default_texts = [item for item in defaults if isinstance(item, str)] if isinstance(defaults, list) else []
        if default_texts:
            lines.append("默认文字：" + "／".join(default_texts[:5]))
        options = params.get("options")
        option_items = [item for item in options if isinstance(item, dict)] if isinstance(options, list) else []
        if option_items:
            lines.append("选项：")
            lines.extend(_format_option_detail(option) for option in option_items)
        else:
            lines.append("选项：无")
    else:
        lines.append("参数说明缺失")
    shortcuts = payload.get("shortcuts")
    if isinstance(shortcuts, list) and shortcuts:
        rendered: list[str] = []
        for shortcut in shortcuts:
            if isinstance(shortcut, dict) and isinstance(shortcut.get("pattern"), str):
                humanized = shortcut.get("humanized")
                rendered.append(shortcut["pattern"] + (f"（{humanized}）" if isinstance(humanized, str) and humanized else ""))
        if rendered:
            lines.append("快捷指令：" + "；".join(rendered[:6]))
    return "\n".join(lines)


async def generate_meme_image(
    *,
    _tool_context: ToolContext,
    meme_key: str,
    texts: list[str] | None = None,
    image_urls: list[str] | None = None,
    options: dict[str, Any] | None = None,
) -> ToolResult:
    """Render one meme from a template key, texts and optional input images."""

    blocked_by = meme_capability_reason()
    if blocked_by is not None:
        return ToolResult(text=f"表情包工具未启用：{blocked_by}")
    raw_key = meme_key.strip() if isinstance(meme_key, str) else ""
    resolved_from: str | None = None
    if raw_key and _MEME_KEY_RE.fullmatch(raw_key):
        key = raw_key
    elif raw_key and not raw_key.isascii():
        # Chinese meme names (e.g. 打拳) resolve to template keys through
        # /meme/search instead of being rejected as invalid keys.
        keyword = _clean_meme_query(raw_key)
        if keyword is None:
            return ToolResult(text=f"表情名称过长（最多 {_MEME_QUERY_MAX_CHARS} 字），请用更短的关键词。")
        try:
            key, matches = await _resolve_meme_keyword(keyword)
        except RenderToolError as error:
            return ToolResult(text=f"❌ {error}")
        if key is None:
            if not matches:
                return ToolResult(text=f"未找到与『{keyword}』相关的表情模板；可换个说法，或先用 meme_search 查询模板 key。")
            candidate_lines = [
                f"{candidate}｜{await _fetch_meme_summary(candidate)}" for candidate in matches[:_MEME_AMBIGUOUS_CANDIDATES]
            ]
            suffix = (
                f"\n（另有 {len(matches) - len(candidate_lines)} 个匹配，可用 meme_search 查询完整列表。）"
                if len(matches) > len(candidate_lines)
                else ""
            )
            return ToolResult(
                text=(
                    f"『{keyword}』匹配到 {len(matches)} 个模板，请选一个 key 重新调用 generate_meme：\n"
                    + "\n".join(candidate_lines)
                    + suffix
                )
            )
        resolved_from = keyword
    else:
        return ToolResult(
            text=("模板 key 必须是字母、数字、下划线或连字符（如 petpet）；也可直接传中文表情名（如 打拳），会自动匹配模板。")
        )
    if not await _meme_allowed(_tool_context, key):
        return ToolResult(text="当前群已关闭该表情包，无法制作。", metadata={"status": "denied"})
    if texts is None:
        texts = []
    if image_urls is None:
        image_urls = []
    if options is None:
        options = {}
    if not isinstance(texts, list) or not all(isinstance(item, str) for item in texts):
        return ToolResult(text="texts 必须是字符串数组。")
    if not isinstance(image_urls, list) or not all(isinstance(item, str) for item in image_urls):
        return ToolResult(text="image_urls 必须是字符串数组。")
    if len(texts) > _MAX_TEXTS or any(len(item) > _TEXT_MAX_CHARS for item in texts):
        return ToolResult(text=f"文字最多 {_MAX_TEXTS} 条，每条不超过 {_TEXT_MAX_CHARS} 字。")
    if len(image_urls) > _MAX_INPUT_IMAGES:
        return ToolResult(text=f"图片最多 {_MAX_INPUT_IMAGES} 张。")
    if not isinstance(options, dict) or not all(
        isinstance(name, str)
        and (
            isinstance(value, bool) or (isinstance(value, (int, float)) and not isinstance(value, bool)) or isinstance(value, str)
        )
        for name, value in options.items()
    ):
        return ToolResult(text="options 必须是布尔/数字/字符串值的对象。")

    try:
        status, info_payload = await _meme_request("GET", f"/memes/{key}/info")
        if status == 404:
            return ToolResult(text=f"未找到模板 {key}；请确认 key 是否正确，或先用 meme_search 按中文关键词查询模板。")
        if status != 200:
            return ToolResult(text=f"模板信息查询失败（HTTP {status}）。")
        params = _parse_meme_info(info_payload)
        _validate_meme_arguments(params, images=image_urls, texts=texts)

        uploaded: list[dict[str, str]] = []
        for index, image_url in enumerate(image_urls):
            data = await _download_public_image(image_url)
            status, upload_payload = await _meme_request(
                "POST",
                "/image/upload",
                json_payload={
                    "type": "data",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
            image_id = _extract_image_id(status, upload_payload)
            uploaded.append({"name": f"image_{index}", "id": image_id})

        status, result_payload = await _meme_request(
            "POST",
            f"/memes/{key}",
            json_payload={"images": uploaded, "texts": texts, "options": options},
        )
        result_id = _extract_image_id(status, result_payload)
        status, image_data = await _meme_request("GET", f"/image/{result_id}")
        if status != 200 or not isinstance(image_data, (bytes, bytearray)) or not image_data:
            raise RenderToolError("meme 生成结果获取失败。")
        payload = guarded_image_bytes(bytes(image_data))
        await deliver_image(_tool_context, payload)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("generate_meme 工具失败：error_type={}", type(error).__name__)
        return ToolResult(text="❌ 表情包生成失败，请稍后再试。")
    vision = vision_data_uri(payload)
    metadata: dict[str, Any] = {
        "bytes": len(payload),
        "mime": sniff_image_mime(payload),
        "meme_key": key,
        "images_used": len(image_urls),
        "texts_used": len(texts),
    }
    if resolved_from is not None:
        metadata["meme_key_requested"] = resolved_from
    return ToolResult(
        text=(
            f"✅ 表情包已生成并发送到当前会话（『{resolved_from}』→ {key}）。"
            if resolved_from is not None
            else "✅ 表情包已生成并发送到当前会话。"
        ),
        images=(vision,) if vision else (),
        metadata=metadata,
    )


def _extract_image_id(status: int, payload: Any) -> str:
    if status == 404:
        raise RenderToolError("meme 后端未找到对应资源。")
    if status == 500 and isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int):
            message = _MEME_ERROR_MESSAGES.get(code)
            if message:
                raise RenderToolError(f"{message}。")
            raise RenderToolError(f"meme 生成失败（错误码 {code}）。")
    if status != 200 or not isinstance(payload, dict):
        raise RenderToolError("meme 后端返回异常。")
    image_id = payload.get("image_id")
    if not isinstance(image_id, str) or not _MEME_IMAGE_ID_RE.fullmatch(image_id):
        raise RenderToolError("meme 后端返回了非法的图片标识。")
    return image_id


async def search_meme_templates(*, _tool_context: ToolContext, query: str) -> ToolResult:
    """List meme templates matching a Chinese or English keyword."""

    blocked_by = meme_capability_reason()
    if blocked_by is not None:
        return ToolResult(text=f"表情包工具未启用：{blocked_by}")
    keyword = _clean_meme_query(query)
    if keyword is None:
        return ToolResult(text=f"query 需为 1-{_MEME_QUERY_MAX_CHARS} 字的关键词（如：打拳、摸头、吵架、petpet）。")
    try:
        matches = await _search_meme_keys(keyword)
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")
    if not matches:
        return ToolResult(text=f"未找到与『{keyword}』相关的表情模板；可换个更短或更常见的说法再试。")
    listed = matches[:_MEME_SEARCH_RESULT_LIMIT]
    lines = [f"{key}｜{await _fetch_meme_summary(key)}" for key in listed]
    header = f"『{keyword}』匹配 {len(matches)} 个模板" + (
        f"（仅显示前 {len(listed)} 个）：" if len(matches) > len(listed) else "："
    )
    return ToolResult(
        text=header + "\n" + "\n".join(lines),
        metadata={"query": keyword, "matched": len(matches), "listed": len(lines)},
    )


async def describe_meme_template(*, _tool_context: ToolContext, meme_key: str) -> ToolResult:
    """Show one template's keywords, input limits and option descriptions."""

    blocked_by = meme_capability_reason()
    if blocked_by is not None:
        return ToolResult(text=f"表情包工具未启用：{blocked_by}")
    raw_key = meme_key.strip() if isinstance(meme_key, str) else ""
    if raw_key and _MEME_KEY_RE.fullmatch(raw_key):
        key = raw_key
    elif raw_key and not raw_key.isascii():
        keyword = _clean_meme_query(raw_key)
        if keyword is None:
            return ToolResult(text=f"表情名称过长（最多 {_MEME_QUERY_MAX_CHARS} 字），请用更短的关键词。")
        try:
            key, matches = await _resolve_meme_keyword(keyword)
        except RenderToolError as error:
            return ToolResult(text=f"❌ {error}")
        if key is None:
            if not matches:
                return ToolResult(text=f"未找到与『{keyword}』相关的表情模板；可先用 meme_search 查询。")
            return ToolResult(
                text=(
                    f"『{keyword}』匹配 {len(matches)} 个模板，请用其中某个 key 重新调用 meme_info：\n"
                    + "\n".join(matches[:_MEME_AMBIGUOUS_CANDIDATES])
                )
            )
    else:
        return ToolResult(text="meme_key 必须是英文模板 key（如 petpet）或中文表情名（如 打拳）。")
    try:
        status, payload = await _meme_request("GET", f"/memes/{key}/info")
    except RenderToolError as error:
        return ToolResult(text=f"❌ {error}")
    if status == 404:
        return ToolResult(text=f"未找到模板 {key}；可先用 meme_search 按关键词查询。")
    if status != 200:
        return ToolResult(text=f"模板信息查询失败（HTTP {status}）。")
    return ToolResult(text=_format_meme_details(payload), metadata={"meme_key": key})


GENERATE_MEME_TOOL = ToolSpec(
    name="generate_meme",
    description=(
        "按模板 key 生成表情包图片并发到当前会话。"
        "meme_key 传英文模板 key（如 petpet 摸头需 1 张图片、5000choyen 上下两行文字）；"
        "用户说中文表情名时可直接把中文词传入 meme_key（如 打拳、摸头），会自动匹配模板；"
        "匹配到多个模板时会返回候选清单。不确定模板 key 或模板要几张图几条文字时，"
        "先用 meme_search 按关键词查询，用 meme_info 看参数说明。"
        "先按模板要求准备文字或图片，再调用本工具。"
        "当前/引用图片使用消息素材中的url；头像先通过可用的用户资料/头像查询工具取得avatar_url。"
        "已有消息图片或头像查询能力时，应先读取再生成，不要要求用户重发图片链接。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "meme_key": {
                "type": "string",
                "description": "表情包模板 key（如 petpet）；也可直接传中文表情名（如 打拳），会自动匹配模板。",
                "minLength": 1,
                "maxLength": 64,
            },
            "texts": {
                "type": "array",
                "description": "模板文字列表，条数与顺序需符合模板要求。",
                "items": {"type": "string", "maxLength": _TEXT_MAX_CHARS},
                "maxItems": _MAX_TEXTS,
            },
            "image_urls": {
                "type": "array",
                "description": ("模板输入图片的公网 http/https 地址列表（最多 4 张）；需要用户头像或图片素材时使用。"),
                "items": {"type": "string", "maxLength": 2_048},
                "maxItems": _MAX_INPUT_IMAGES,
            },
            "options": {
                "description": ("模板可选参数对象（值为布尔/数字/字符串），一般可省略。"),
                "additionalProperties": {
                    "anyOf": [
                        {"type": "boolean"},
                        {"type": "number"},
                        {"type": "string"},
                    ]
                },
            },
        },
        "required": ["meme_key"],
        "additionalProperties": False,
    },
    handler=generate_meme_image,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=180,
    result_limit=4_000,
    policy=ToolPolicy.configured(ToolCapabilityV2(network_allow=("*",))),
)

MEME_SEARCH_TOOL = ToolSpec(
    name="meme_search",
    description=(
        "按关键词查找具体表情包模板。用户问有哪些表情、能做什么表情、要表情列表时不要用本工具"
        "逐个搜关键词，改用 framework_query 投递原生表情列表图（runtime=bison，"
        "capability=nonebot_plugin_memes_api:表情包列表，deliver=true）。"
        "本工具返回模板 key、关键词、所需图片张数、文字条数和可选项概要；"
        "生成表情包（generate_meme）前不确定模板 key，或不知道某个表情要几张图几条文字时用本工具。"
        "query 用简短关键词，如：打拳、摸头、亲、吵架、petpet。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "表情关键词（中英文均可），如 打拳、摸头。",
                "minLength": 1,
                "maxLength": _MEME_QUERY_MAX_CHARS,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=search_meme_templates,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=90,
    result_limit=4_000,
    policy=ToolPolicy.configured(ToolCapabilityV2(network_allow=("*",))),
)

MEME_INFO_TOOL = ToolSpec(
    name="meme_info",
    description=(
        "查询单个表情包模板的详细说明：关键词、标签、所需图片张数、文字条数、"
        "默认文字、每个可选项的类型/默认值/取值范围/用途说明及快捷指令。"
        "meme_key 传英文模板 key；也可传中文表情名（如 打拳），会自动匹配。"
        "不确定 key 时先用 meme_search。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "meme_key": {
                "type": "string",
                "description": "表情包模板 key（如 petpet）或中文表情名（如 打拳）。",
                "minLength": 1,
                "maxLength": 64,
            },
        },
        "required": ["meme_key"],
        "additionalProperties": False,
    },
    handler=describe_meme_template,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=60,
    result_limit=4_000,
    policy=ToolPolicy.configured(ToolCapabilityV2(network_allow=("*",))),
)


def register_meme_tools() -> tuple[str, ...]:
    """Register the meme tools only when the admin configured a backend."""

    blocked_by = meme_capability_reason()
    if blocked_by is not None:
        logger.info("LLM 表情包工具未注册：{}", blocked_by)
        return ()
    registered: list[str] = []
    for spec in (GENERATE_MEME_TOOL, MEME_SEARCH_TOOL, MEME_INFO_TOOL):
        try:
            register_tool(spec)
        except ValueError:
            logger.debug("LLM 表情包工具已注册，跳过：{}", spec.name)
            continue
        logger.info("LLM 表情包工具已注册：{}", spec.name)
        registered.append(spec.name)
    return tuple(registered)


def _schedule_startup_registration() -> None:
    """Register at driver startup instead of at import time.

    Mirrors render_tools: import-time registration would leak the meme tool
    into the global ``tool_registry`` before reload transactions and
    third-party tests pin their own registered-tool universes.
    """

    @get_driver().on_startup
    async def _register_meme_tools_on_startup() -> None:
        register_meme_tools()


_schedule_startup_registration()

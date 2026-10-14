"""Trusted LLM tool that renders memes through a meme-generator backend.

The backend contract mirrors nonebot-plugin-memes-api 0.5.1 (memes
generator 0.2.x): ``POST /image/upload`` returns an ``image_id``,
``POST /memes/{key}`` with ``images``/``texts``/``options`` renders a new
meme and returns another ``image_id``, and ``GET /image/{id}`` streams the
bytes.  ``GET /memes/{key}/info`` exposes each template's image/text count
limits, which are validated before generation so the model gets an
actionable message instead of a backend stack trace.

Security posture:

- ``MOELLM_MEME_API_BASE`` is administrator configuration: it is only
  scheme-checked (http/https) because the meme backend legitimately lives
  on an intranet host.  Leaving it empty disables the tool entirely.
- Every URL that arrives through tool arguments (meme input images) goes
  through ``network_safety.validate_public_url`` (http/https only,
  DNS-level loopback/private/reserved rejection) and a redirect-by-redirect
  manual follow that re-validates each hop; downloads are size-capped.
- Template keys and backend-returned image ids are strictly pattern-checked
  before they are placed on a request path.
- The finished picture flows through the shared render-tool image guard
  (8 MiB degrade / 20 MiB refuse) and is delivered in memory; no temporary
  files are written.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

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
    if not isinstance(meme_key, str) or not _MEME_KEY_RE.fullmatch(meme_key.strip()):
        return ToolResult(text="模板 key 必须是字母、数字、下划线或连字符（如 petpet）。")
    key = meme_key.strip()
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
            return ToolResult(text=f"未找到模板 {key}，请确认模板 key 是否正确。")
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
    return ToolResult(
        text="✅ 表情包已生成并发送到当前会话。",
        images=(vision,) if vision else (),
        metadata={
            "bytes": len(payload),
            "mime": sniff_image_mime(payload),
            "meme_key": key,
            "images_used": len(image_urls),
            "texts_used": len(texts),
        },
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


GENERATE_MEME_TOOL = ToolSpec(
    name="generate_meme",
    description=(
        "按模板 key 生成表情包图片并发到当前会话。"
        "常用 key 如 petpet（摸头，需 1 张图片）、5000choyen（上下两行文字）等；"
        "先按模板要求准备文字或图片，再调用本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "meme_key": {
                "type": "string",
                "description": "表情包模板 key（如 petpet）。",
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


def register_meme_tools() -> tuple[str, ...]:
    """Register generate_meme only when the admin configured a backend."""

    blocked_by = meme_capability_reason()
    if blocked_by is not None:
        logger.info("LLM 表情包工具未注册：{}", blocked_by)
        return ()
    try:
        register_tool(GENERATE_MEME_TOOL)
    except ValueError:
        logger.debug("LLM 表情包工具已注册，跳过：generate_meme")
        return ()
    return ("generate_meme",)


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

"""Inline QQ attachments for providers that cannot fetch signed QQ URLs.

Long screenshots are tiled rather than shrunk into unreadable thumbnails.
Public URL validation, DNS pinning, redirect and byte limits stay in the shared
media downloader. The original event and recovery journal retain their URLs.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
from urllib.parse import urlsplit

from nonebot.log import logger

from .compat import timeout as timeout_scope

_QQ_IMAGE_HOSTS = {"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn", "q.qlogo.cn"}
_EDGE = 2048
_TILES = 8


def qq_image_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and parsed.hostname in _QQ_IMAGE_HOSTS
    except (TypeError, ValueError):
        return False


def image_tiles(payload: bytes) -> tuple[str, ...]:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(payload)) as source:
        if source.width * source.height > 40_000_000:
            raise ValueError("image pixel limit")
        image = ImageOps.exif_transpose(source).convert("RGB")
    w, h = image.size
    if min(w, h) < 1:
        raise ValueError("empty image")
    if max(w, h) <= _EDGE:
        count = 1
    else:
        # Keep the short edge legible and bound the number of long-image tiles.
        scale = min(1.0, 1536 / min(w, h), (_EDGE - 128) * _TILES / max(w, h))
        image = image.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        w, h = image.size
        count = min(_TILES, max(1, math.ceil((max(w, h) - 128) / (_EDGE - 128))))
    results = []
    for index in range(count):
        extent = max(w, h)
        start = round(max(0, extent - _EDGE) * index / max(1, count - 1))
        box = ((0, start, w, min(h, start + _EDGE)) if h >= w
               else (start, 0, min(w, start + _EDGE), h))
        tile = image.crop(box)
        output = io.BytesIO()
        tile.save(output, format="JPEG", quality=88, optimize=True)
        if output.tell() > 3 * 1024 * 1024:
            raise ValueError("image tile byte limit")
        results.append("data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii"))
    return tuple(results)


async def inline_qq_images(messages: list, cache: dict, *, timeout: float) -> list:
    from .media_context import download_media

    normalized = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            normalized.append(message)
            continue
        parts = []
        for part in content:
            image = part.get("image_url") if isinstance(part, dict) else None
            url = image.get("url") if isinstance(image, dict) else None
            if not isinstance(url, str) or not qq_image_url(url):
                parts.append(part)
                continue
            if url not in cache:
                try:
                    async with timeout_scope(timeout):
                        payload, _ = await download_media(url)
                        cache[url] = await asyncio.to_thread(image_tiles, payload)
                    logger.info("QQ 视觉附件已内嵌：bytes={} tiles={}", len(payload), len(cache[url]))
                except (OSError, ValueError, TimeoutError) as error:
                    # Keep the original image and allow another provider to fetch
                    # it. Never silently turn an image question into text-only.
                    cache[url] = ()
                    logger.warning("QQ 视觉附件内嵌失败，保留原图：error_type={}", type(error).__name__)
            tiles = cache[url]
            if not tiles:
                parts.append(part)
                continue
            for index, tile in enumerate(tiles):
                if len(tiles) > 1:
                    parts.append({"type":"text", "text":f"同一张长图第 {index + 1}/{len(tiles)} 段，按顺序连续阅读："})
                parts.append({**part, "image_url":{**image, "url":tile}})
        normalized.append({**message, "content":parts})
    return normalized

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

_QQ_IMAGE_HOSTS = {"multimedia.nt.qq.com.cn", "multimedia.nt.qq.com", "gchat.qpic.cn", "c2cpicdw.qpic.cn", "q.qlogo.cn"}
_EDGE = 2048
_TILES = 8
_QQ_PHOTO_MAX_BYTES = 64 * 1024 * 1024
_photo_slots = asyncio.Semaphore(2)


def _reduce_photo(payload: bytes) -> tuple[bytes, str]:
    from PIL import Image, ImageOps
    from .media_context import MAX_MEDIA_BYTES, MediaError

    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > 40_000_000:
                raise MediaError("图片超过 4000 万像素，请缩小原图后重发。")
            # Keep original drawing coordinates and fine detail whenever re-encoding suffices.
            oriented = ImageOps.exif_transpose(image)
            if oriented.has_transparency_data:
                preview = oriented.convert("RGBA")
                codec, mime = "WEBP", "image/webp"
            else:
                preview = oriented.convert("RGB")
                codec, mime = "JPEG", "image/jpeg"
            for quality in (92, 85, 78):
                output = io.BytesIO()
                preview.save(output, format=codec, quality=quality)
                if output.tell() <= MAX_MEDIA_BYTES:
                    return output.getvalue(), mime
            preview.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            preview.save(output, format=codec, quality=85)
            if output.tell() > MAX_MEDIA_BYTES:
                raise MediaError("参考图压缩后仍超过 20 MiB，请缩小原图。")
            return output.getvalue(), mime
    except (OSError, Image.DecompressionBombError) as exc:
        raise MediaError("无法解码这张图片，请重新发送 JPG 或 PNG 原图。") from exc


async def download_reference_image(url: str) -> tuple[bytes, str]:
    from .media_context import MAX_MEDIA_BYTES, download_media

    if not qq_image_url(url):
        return await download_media(url)
    async with _photo_slots:
        payload, mime = await download_media(url, max_bytes=_QQ_PHOTO_MAX_BYTES)
        if len(payload) <= MAX_MEDIA_BYTES:
            return payload, mime
        return await asyncio.to_thread(_reduce_photo, payload)



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
    if max(w, h) <= _EDGE or max(w, h) < 3 * min(w, h):
        image.thumbnail((_EDGE, _EDGE), Image.Resampling.LANCZOS)
        w, h = image.size
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


async def vision_parts(payload: bytes) -> list[dict]:
    from PIL import Image
    from .media_context import MediaError

    try:
        async with _photo_slots:
            tiles = await asyncio.to_thread(image_tiles, payload)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        raise MediaError("无法处理图片，请检查格式和像素尺寸。") from error
    return [{"type": "image_url", "image_url": {"url": tile}} for tile in tiles]


async def inline_qq_images(messages: list, cache: dict, *, timeout: float) -> list:
    from .media_context import MediaError, download_media

    from PIL import Image

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
                        async with _photo_slots:
                            payload, _ = await download_media(url, max_bytes=_QQ_PHOTO_MAX_BYTES)
                            cache[url] = await asyncio.to_thread(image_tiles, payload)
                    logger.info("QQ 视觉附件已内嵌：bytes={} tiles={}", len(payload), len(cache[url]))
                except (OSError, ValueError, TimeoutError, Image.DecompressionBombError) as error:
                    logger.warning("QQ 视觉附件读取失败：error_type={}", type(error).__name__)
                    if isinstance(error, MediaError):
                        raise
                    raise MediaError("QQ 图片解码失败或读取超时，请重新发送原图。") from error
            tiles = cache[url]
            for index, tile in enumerate(tiles):
                if len(tiles) > 1:
                    parts.append({"type":"text", "text":f"同一张长图第 {index + 1}/{len(tiles)} 段，按顺序连续阅读："})
                parts.append({**part, "image_url":{**image, "url":tile}})
        normalized.append({**message, "content":parts})
    return normalized

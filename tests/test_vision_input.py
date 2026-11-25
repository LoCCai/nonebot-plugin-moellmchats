import asyncio
import base64
from copy import deepcopy
import io
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw
import pytest

from nonebot_plugin_moellmchats import media_context
from nonebot_plugin_moellmchats.vision_input import image_tiles, inline_qq_images, qq_image_url


def picture(size=(300, 200)):
    image = Image.new("RGB",size,"white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0,0,size[0],100),fill="blue")
    draw.rectangle((0,size[1]-100,size[0],size[1]),fill="red")
    out = io.BytesIO()
    image.save(out,format="PNG")
    return out.getvalue()


def test_tall_screenshot_keeps_top_bottom_and_legible_width():
    tiles = [Image.open(io.BytesIO(base64.b64decode(value.split(",",1)[1])))
             for value in image_tiles(picture((2000,10576)))]
    assert 1 < len(tiles) <= 8
    assert all(max(tile.size) <= 2048 and min(tile.size) >= 1000 for tile in tiles)
    assert tiles[0].getpixel((20,20))[2] > 200
    assert tiles[-1].getpixel((20,tiles[-1].height-20))[0] > 200


def test_tile_batch_compresses_below_individual_and_total_limits(monkeypatch):
    from nonebot_plugin_moellmchats import vision_input

    tile = Image.effect_noise((1400, 1400), 100).convert("RGB")
    raw = io.BytesIO()
    tile.save(raw, format="JPEG", quality=88, optimize=True)
    limit = len(raw.getvalue())
    monkeypatch.setattr(vision_input, "_TILE_MAX_BYTES", limit - 1)
    monkeypatch.setattr(vision_input, "_TILES_MAX_BYTES", limit)
    result = vision_input._encode_tiles([tile, tile])
    assert len(result) == 2 and sum(map(len, result)) <= limit
    assert all(len(value) < limit for value in result)
    assert all(min(Image.open(io.BytesIO(value)).size) >= 1024 for value in result)


@pytest.mark.parametrize("url", ["https://multimedia.nt.qq.com.cn.evil.invalid/a", "file:///tmp/a", "data:image/png;base64,AA=="])
def test_only_exact_qq_hosts_are_downloaded(url):
    assert not qq_image_url(url)


@pytest.mark.asyncio
async def test_remote_image_cached_without_mutating_original_or_dropping_text(monkeypatch):
    download = AsyncMock(return_value=(picture(),"image/png"))
    monkeypatch.setattr(media_context,"download_media",download)
    messages = [{"role":"user", "content":[{"type":"text","text":"解释图片"},
        {"type":"image_url","image_url":{"url":"https://multimedia.nt.qq.com.cn/test","detail":"high"}}]}]
    original, cache = deepcopy(messages), {}
    first = await inline_qq_images(messages,cache,timeout=1)
    second = await inline_qq_images(messages,cache,timeout=1)
    assert first == second
    assert messages == original
    assert first[0]["content"][0]["text"] == "解释图片"
    assert first[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert first[0]["content"][1]["image_url"]["detail"] == "high"
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_fetch_reports_error_and_cancellation_propagates(monkeypatch):
    messages = [{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://gchat.qpic.cn/test"}}]}]
    monkeypatch.setattr(media_context,"download_media",AsyncMock(side_effect=ValueError("expired")))
    with pytest.raises(media_context.MediaError, match="QQ 图片"):
        await inline_qq_images(messages,{},timeout=1)
    monkeypatch.setattr(media_context,"download_media",AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await inline_qq_images(messages,{},timeout=1)


@pytest.mark.asyncio
async def test_chat_preview_and_drawing_reference_are_separate(monkeypatch):
    from nonebot_plugin_moellmchats.vision_input import download_reference_image, vision_parts
    # Compressible large PNG, representative of a source above the byte threshold.
    source = picture((3600,1800))
    monkeypatch.setattr(media_context, "MAX_MEDIA_BYTES", 256 * 1024)
    image = Image.open(io.BytesIO(source))
    raw = io.BytesIO()
    image.save(raw,format="PNG",compress_level=0)
    monkeypatch.setattr(media_context,"download_media",AsyncMock(return_value=(raw.getvalue(),"image/png")))
    ref, mime = await download_reference_image("https://gchat.qpic.cn/example")
    assert mime == "image/jpeg" and len(ref) <= media_context.MAX_MEDIA_BYTES
    assert Image.open(io.BytesIO(ref)).size == image.size
    parts = await vision_parts(ref)
    assert len(parts) == 1
    thumb = base64.b64decode(parts[0]["image_url"]["url"].split(",",1)[1])
    assert Image.open(io.BytesIO(thumb)).size == (2048,1024)


def test_drawing_transparency_survives_reencoding():
    from nonebot_plugin_moellmchats.vision_input import _reduce_photo
    canvas = Image.new("RGBA",(360,180),(12,34,56,0))
    ImageDraw.Draw(canvas).rectangle((40,40,150,120),fill="blue")
    raw = io.BytesIO()
    canvas.save(raw,format="PNG")
    data,mime = _reduce_photo(raw.getvalue())
    with Image.open(io.BytesIO(data)) as out:
        assert mime == "image/webp" and out.size == canvas.size
        assert out.getpixel((1,1))[3] == 0 and out.getpixel((60,60))[3] == 255

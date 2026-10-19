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
async def test_failed_fetch_preserves_image_and_cancellation_propagates(monkeypatch):
    messages = [{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://gchat.qpic.cn/test"}}]}]
    monkeypatch.setattr(media_context,"download_media",AsyncMock(side_effect=ValueError("expired")))
    assert await inline_qq_images(messages,{},timeout=1) == messages
    monkeypatch.setattr(media_context,"download_media",AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await inline_qq_images(messages,{},timeout=1)

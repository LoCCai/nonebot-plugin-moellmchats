import base64
from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image
import pytest
from test_media_tools import _context, _png, _selector

from nonebot_plugin_moellmchats import media_models as models
from nonebot_plugin_moellmchats import media_tools as tools


@pytest.mark.parametrize("size", ["3840x2160", "2160x3840", "2048x1152", "1024x1024", "1280x720"])
def test_documented_custom_sizes(size):
    assert models.image_dimensions(size) == tuple(map(int, size.split("x")))


@pytest.mark.parametrize("size", ["4096x2160", "3840x3840", "3840x2161", "128x128", "3840x640", "auto", "3840x2160\n"])
def test_invalid_dimensions_rejected_before_spending(size):
    with pytest.raises(models.MediaError):
        models.image_dimensions(size)


@pytest.mark.asyncio
async def test_4k_native_request_uses_size_parameter_and_still_one_image(tmp_path, monkeypatch):
    selector = _selector(tmp_path, monkeypatch)
    selector.models["model-a (good)"].update(image_api_format="openai", supports_custom_image_sizes=True)
    post = AsyncMock(return_value=(200, {"data": [{"b64_json": base64.b64encode(_png()).decode()}]}))
    monkeypatch.setattr(models, "_post", post)
    await models.generate_picture("A selfie", [], size="3840x2160")
    assert post.await_count == 1
    assert post.call_args.kwargs["payload"]["size"] == "3840x2160"
    assert post.call_args.kwargs["payload"]["n"] == 1


@pytest.mark.asyncio
async def test_legacy_provider_not_sent_unsupported_4k(tmp_path, monkeypatch):
    _selector(tmp_path, monkeypatch)
    post = AsyncMock()
    monkeypatch.setattr(models, "_post", post)
    with pytest.raises(models.MediaError):
        await models.generate_picture("A selfie", [], size="3840x2160")
    post.assert_not_called()


@pytest.mark.asyncio
async def test_4k_word_without_4k_size_stops_before_generation(monkeypatch):
    monkeypatch.setattr(tools, "_image_generation_authorizer", AsyncMock(return_value=True))
    generate = AsyncMock()
    monkeypatch.setattr(tools, "generate_picture", generate)
    result = await tools.generate_image(_tool_context=_context(), prompt="自拍合照，4K的", size="1536x1024")
    assert result.metadata["status"] == "failed"
    assert "size" in result.text
    generate.assert_not_called()


@pytest.mark.asyncio
async def test_returned_pixels_are_reported_truthfully_without_regeneration(monkeypatch):
    monkeypatch.setattr(tools, "_image_generation_authorizer", AsyncMock(return_value=True))
    generate = AsyncMock(return_value=(_png(), "flare"))
    monkeypatch.setattr(tools, "generate_picture", generate)
    monkeypatch.setattr(tools, "deliver_image", AsyncMock())
    result = await tools.generate_image(_tool_context=_context(), prompt="A selfie", size="3840x2160")
    assert result.metadata["width"] == 32
    assert result.metadata["height"] == 32
    assert result.metadata["size_matched"] is False
    assert "不可宣称" in result.text
    assert generate.await_count == 1


def test_large_drawing_compression_preserves_native_pixels(monkeypatch):
    image = Image.effect_noise((3840, 2160), 100).convert("RGB")
    raw = BytesIO()
    image.save(raw, format="PNG", compress_level=0)
    assert raw.tell() > 8 * 1024 * 1024
    calls = []

    def guard(data):
        calls.append(len(data))
        assert len(data) <= 8 * 1024 * 1024
        return data

    monkeypatch.setattr(tools, "guarded_image_bytes", guard)
    data, width, height = tools._drawing_output(raw.getvalue())
    assert (width, height) == (3840, 2160)
    assert len(calls) == 1
    assert data.startswith(b"RIFF")


@pytest.mark.parametrize("source,custom,expected", [
    ((8256,4030), False, "1536x1024"),
    ((8256,4030), True, "3840x1872"),
    ((1024,1536), True, "1024x1536"),
    ((1152,2048), True, "1152x2048"),
    ((700,7000), True, "1280x3840"),
])
def test_edit_canvas_follows_reference_and_model(source, custom, expected):
    payload = BytesIO()
    Image.new("1", source).save(payload, format="PNG")
    assert models.reference_output_size(payload.getvalue(), custom) == expected


@pytest.mark.asyncio
async def test_automatic_edit_canvas_reaches_provider(tmp_path, monkeypatch):
    selector = _selector(tmp_path, monkeypatch)
    selector.models["model-a (good)"].update(image_api_format="openai", supports_custom_image_sizes=False)
    post = AsyncMock(return_value=(200, {"data": [{"b64_json": base64.b64encode(_png()).decode()}]}))
    monkeypatch.setattr(models, "_post", post)
    photo = BytesIO()
    Image.new("RGB",(1800,900)).save(photo, format="JPEG")
    receipt = {}
    await models.generate_picture("保留构图", [photo.getvalue()], size_receipt=receipt)
    assert receipt == {"size": "1536x1024"}
    fields = {head["name"]: value for head, _, value in post.call_args.kwargs["form"]._fields}
    assert fields["size"] == "1536x1024"
    assert fields["image[]"] == photo.getvalue()

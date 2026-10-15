from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

from PIL import Image
import pytest
from test_model_selector import _initialized_selector

from nonebot_plugin_moellmchats import media_context as context_module
from nonebot_plugin_moellmchats import media_models as models
from nonebot_plugin_moellmchats import media_tools as tools
from nonebot_plugin_moellmchats.media_context import (
    MediaAsset,
    MediaError,
    asset_bytes,
    collect_message_context,
    media_store,
)
from nonebot_plugin_moellmchats.media_documents import extract_document
from nonebot_plugin_moellmchats.tool_contracts import ToolContext


def _segment(kind, **data):
    return {"type": kind, "data": data}


def _context(message=None, reply=None, user="10001", group=10002, request_id=None):
    bot = SimpleNamespace(self_id="42", call_api=AsyncMock(), send=AsyncMock())
    event = SimpleNamespace(message=message or [], reply=reply, group_id=group, get_user_id=lambda: user)
    return ToolContext(bot=bot, event=event, request_id=request_id)


def _png(color="blue"):
    stream = BytesIO()
    Image.new("RGB", (32, 32), color).save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture(autouse=True)
def _clear_media_state():
    media_store.entries.clear()
    tools._previous_images.clear()
    tools._drawing_locks.clear()
    tools._drawing_requests.clear()
    yield
    media_store.entries.clear()


@pytest.mark.asyncio
async def test_nested_reply_forward_json_keeps_sources_deduplicates_cycles_and_hides_urls():
    context = _context([_segment("text", text="总结这个记录")],
        reply=SimpleNamespace(message=[_segment("forward", id="one")]))

    async def api(name, **params):
        assert name == "get_forward_msg"
        identity = params["id"]
        if identity == "one":
            return {"messages": [{"sender": {"nickname": "甲"}, "time": 100,
                "message": [_segment("text", text="第一层"), _segment("json", data=json.dumps({
                    "meta": {"detail": {"resid": "two"}}}))]}]}
        return {"messages": [{"sender": {"nickname": "乙"}, "time": 200,
            "message": [_segment("text", text="第二层"),
                        _segment("file", name="说明.txt", file_id="opaque", url="https://example.com/private?token=secret"),
                        _segment("forward", id="one")]}]}

    context.bot.call_api.side_effect = api
    result = await tools.read_message_content(_tool_context=context)
    parsed = json.loads(result.text)
    assert "第一层" in parsed["text"]
    assert "第二层" in parsed["text"]
    assert "100 甲" in parsed["text"]
    assert "200 乙" in parsed["text"]
    assert context.bot.call_api.await_count == 2
    assert len(parsed["assets"]) == 1
    assert parsed["assets"][0]["name"] == "说明.txt"
    assert "secret" not in result.text
    assert "https://" not in result.text
    assert not parsed["partial"]


@pytest.mark.asyncio
async def test_message_pagination_retains_complete_original_and_scope_is_enforced():
    context = _context([_segment("text", text="a" * 5000 + "终点")])
    first = json.loads((await tools.read_message_content(_tool_context=context)).text)
    assert first["has_more"]
    second = json.loads((await tools.read_message_content(_tool_context=context,
        context_ref=first["context_ref"], page=2)).text)
    assert "终点" in second["text"]
    assert not second["has_more"]
    for other in (_context(user="10003"), _context(group=10004)):
        result = await tools.read_message_content(_tool_context=other, context_ref=first["context_ref"])
        assert result.metadata["status"] == "failed"
        assert "终点" not in result.text


@pytest.mark.asyncio
async def test_recursive_depth_and_character_budgets_report_partial():
    raw = [_segment("text", text="inside")]
    for _ in range(15):
        raw = [_segment("node", content=raw)]
    document = await collect_message_context(_context(raw))
    assert document.partial
    assert "inside" not in document.text
    document = await collect_message_context(_context([_segment("text", text="x" * 130_000)]))
    assert document.partial
    assert len(document.text) < 121_000


@pytest.mark.asyncio
async def test_history_stays_in_current_group_paginates_and_orders_by_time():
    context = _context()
    pages = [
        {"messages": [{"message_id": 3, "message_seq": 30, "group_id": 10002, "time": 300,
                       "sender": {"nickname": "丙"}, "message": "第三条"},
                      {"message_id": 2, "message_seq": 20, "group_id": 10002, "time": 200, "message": "第二条"}]},
        {"messages": [{"message_id": 1, "message_seq": 10, "group_id": 10002, "time": 100, "message": "第一条"},
                      {"message_id": 99, "message_seq": 9, "group_id": 90000, "time": 90, "message": "其他群隐私"}]},
    ]
    context.bot.call_api.side_effect = pages
    document = await collect_message_context(context, source="group_history", max_messages=3)
    assert document.text.index("第一条") < document.text.index("第二条") < document.text.index("第三条")
    assert "其他群" not in document.text
    calls = context.bot.call_api.call_args_list
    assert calls[0].kwargs["group_id"] == calls[1].kwargs["group_id"] == 10002
    assert calls[1].kwargs["message_seq"] == 19
    with pytest.raises(MediaError):
        await collect_message_context(_context(group=None), source="group_history")


@pytest.mark.asyncio
async def test_file_reference_resolves_gateway_id_and_cannot_cross_users(monkeypatch):
    context = _context([_segment("file", name="说明.txt", file_id="actual-file")])
    document = await collect_message_context(context)
    context.bot.call_api.side_effect = [RuntimeError("not supported"), {"url": "https://example.com/qq-file"}]
    download = AsyncMock(return_value=(b"content", "text/plain"))
    monkeypatch.setattr(context_module, "download_media", download)
    asset = await asset_bytes(context, document.assets[0])
    assert asset.payload == b"content"
    assert context.bot.call_api.call_args.kwargs["file_id"] == "actual-file"
    assert context.bot.call_api.call_args.kwargs["group_id"] == 10002
    with pytest.raises(MediaError):
        await asset_bytes(_context(user="10003"), document.assets[0])


@pytest.mark.asyncio
async def test_expired_media_reference_cannot_be_read(monkeypatch):
    context = _context()
    reference = media_store.put(context, MediaAsset("image", "a", payload=_png()))
    monkeypatch.setattr(context_module.time, "monotonic", lambda: 1e20)
    with pytest.raises(MediaError):
        media_store.get(context, reference, MediaAsset)


@pytest.mark.asyncio
async def test_private_file_uses_actual_sender_and_received_hash(monkeypatch):
    context = _context([_segment("file", name="私聊.txt", file_id="received-id", file_hash="received-hash")], group=None)
    document = await collect_message_context(context)
    context.bot.call_api.side_effect = [RuntimeError("not cached"), {"url": "https://example.com/file"}]
    monkeypatch.setattr(context_module, "download_media", AsyncMock(return_value=(b"hello", "text/plain")))
    asset = await asset_bytes(context, document.assets[0])
    assert asset.payload == b"hello"
    assert context.bot.call_api.call_args.args == ("get_private_file_url",)
    assert context.bot.call_api.call_args.kwargs["user_id"] == 10001
    assert context.bot.call_api.call_args.kwargs["file_hash"] == "received-hash"


@pytest.mark.asyncio
async def test_qq_silk_uses_framework_transcode_instead_of_mislabeled_mp3(monkeypatch):
    context = _context([_segment("record", file="voice.silk", url="https://example.com/voice")])
    document = await collect_message_context(context)
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
    context.bot.call_api.return_value = {"base64": base64.b64encode(wav).decode()}
    download = AsyncMock()
    monkeypatch.setattr(context_module, "download_media", download)
    asset = await asset_bytes(context, document.assets[0])
    assert asset.mime == "audio/wav"
    assert asset.payload == wav
    download.assert_not_called()
    assert context.bot.call_api.call_args.kwargs["out_format"] == "wav"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/file", "http://169.254.169.254/", "file:///etc/passwd"])
async def test_model_cannot_download_private_or_local_inputs(url):
    with pytest.raises(MediaError):
        await context_module.download_media(url)


@pytest.mark.asyncio
async def test_analysis_reports_failed_attachments_without_claiming_model_read(monkeypatch):
    context = _context([_segment("file", name="a.txt", file_id="missing")])
    context.bot.call_api.side_effect = RuntimeError("unsupported")
    complete = AsyncMock()
    monkeypatch.setattr(tools, "complete_media", complete)
    result = await tools.analyze_media(_tool_context=context, question="里面有什么")
    assert result.metadata["status"] == "failed"
    assert "未能成功读取" in result.text
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_text_document_is_read_before_model_and_no_attachment_is_sent_as_fake_image(monkeypatch):
    context = _context()
    reference = media_store.put(context, MediaAsset("file", "数值.txt", payload="伤害系数=17".encode(), mime="text/plain"))
    complete = AsyncMock(return_value=("系数为17", "media-model"))
    monkeypatch.setattr(tools, "complete_media", complete)
    result = await tools.analyze_media(_tool_context=context, question="系数是多少", asset_refs=[reference])
    assert "17" in result.text
    assert "伤害系数=17" in complete.call_args.kwargs["prompt"]
    assert complete.call_args.kwargs["parts"] == []
    context.bot.send.assert_not_called()


@pytest.mark.asyncio
async def test_chat_summary_uses_separate_configured_model_and_returns_text_to_agent(monkeypatch):
    context = _context([_segment("forward", content=[{"sender": {"nickname": "甲"}, "time": 100,
        "message": [_segment("text", text="明天八点集合")]}])])
    complete = AsyncMock(return_value=("甲约定明天八点集合。", "summary-model"))
    monkeypatch.setattr(tools, "complete_media", complete)
    result = await tools.summarize_chat(_tool_context=context)
    assert complete.call_args.args == ("chat_summary_model",)
    assert "100 甲" in complete.call_args.kwargs["prompt"]
    assert result.metadata["model"] == "summary-model"
    context.bot.send.assert_not_called()


def _selector(tmp_path, monkeypatch, role="image_model"):
    selector = _initialized_selector(tmp_path, monkeypatch)
    primary = "model-a (good)"
    for name in ("backup", "last"):
        selector.models[f"{name} (good)"] = {"model": name, "provider": "good", "key": ["second-key"],
            "url": "https://second.invalid/compatible-mode/v1/chat/completions", "is_vision": True,
            "image_api_format": "dashscope"}
    selector.models[primary].update(image_api_format="dashscope", is_vision=True)
    selector.model_config[role] = primary
    selector.model_config["model_fallbacks"] = {primary: ["backup (good)", "last (good)"]}
    monkeypatch.setattr(models, "model_selector", selector)
    return selector


@pytest.mark.asyncio
async def test_native_image_generation_switches_only_explicit_quota_models_and_downloads_once(tmp_path, monkeypatch):
    _selector(tmp_path, monkeypatch)
    post = AsyncMock(side_effect=[(429, {}), (403, {"code": "AllocationQuota.FreeTierOnly"}),
        (200, {"output": {"choices": [{"message": {"content": [{"image": "https://example.com/finished"}]}}]}})])
    monkeypatch.setattr(models, "_post", post)
    download = AsyncMock(return_value=(_png(), "image/png"))
    monkeypatch.setattr(models, "download_media", download)
    picture, model = await models.generate_picture("一朵花", [_png()])
    assert picture == _png()
    assert model == "last"
    assert [call.args[0]["model"] for call in post.call_args_list] == ["model-a", "backup", "last"]
    assert post.call_args_list[1].args[0]["key"] == "Bearer second-key"
    assert post.call_args_list[1].args[1] == "https://second.invalid/api/v1/services/aigc/multimodal-generation/generation"
    assert all(call.kwargs["payload"]["parameters"]["n"] == 1 for call in post.call_args_list)
    assert post.call_args.kwargs["payload"]["input"]["messages"][0]["content"][0]["image"].startswith("data:image/")
    assert download.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [MediaError("超时，不重提"), (401, {"error": {"message": "private-secret"}}),
    (400, {"code": "DataInspectionFailed", "message": "private-secret"})])
async def test_generation_does_not_switch_on_timeout_auth_or_content_failure(tmp_path, monkeypatch, failure):
    _selector(tmp_path, monkeypatch)
    post = AsyncMock(side_effect=[failure])
    monkeypatch.setattr(models, "_post", post)
    with pytest.raises(MediaError) as error:
        await models.generate_picture("test", [])
    assert "private-secret" not in str(error.value)
    assert post.await_count == 1


@pytest.mark.asyncio
async def test_text_and_media_model_failover_keeps_payload_and_model_options_separate(tmp_path, monkeypatch):
    selector = _selector(tmp_path, monkeypatch, role="multimodal_model")
    selector.models["model-a (good)"]["extra_payload"] = {"enable_thinking": False}
    post = AsyncMock(side_effect=[(429, {}), (200, {"choices": [{"message": {"content": "蓝色"}}]})])
    monkeypatch.setattr(models, "_post", post)
    text, model = await models.complete_media("multimodal_model", prompt="什么颜色", parts=[
        {"type": "image_url", "image_url": {"url": models.image_uri(_png())}}])
    assert text == "蓝色"
    assert model == "backup"
    assert post.call_args_list[0].kwargs["payload"]["messages"] == post.call_args_list[1].kwargs["payload"]["messages"]
    assert "enable_thinking" not in post.call_args_list[1].kwargs["payload"]


@pytest.mark.asyncio
async def test_audio_input_uses_qwen_data_uri_and_correct_format():
    part = await models.media_part(MediaAsset("audio", "a.wav", payload=b"wav-input", mime="audio/wav"))
    assert part["input_audio"]["data"].startswith("data:audio/wav;base64,")
    assert part["input_audio"]["format"] == "wav"


@pytest.mark.asyncio
async def test_drawing_continuation_is_scoped_and_does_not_send_vision_echo(monkeypatch):
    context = _context()
    generate = AsyncMock(return_value=(_png(), "image-model"))
    monkeypatch.setattr(tools, "generate_picture", generate)
    monkeypatch.setattr(tools, "guarded_image_bytes", lambda data: data)
    monkeypatch.setattr(tools, "deliver_image", AsyncMock())
    first = await tools.generate_image(_tool_context=context, prompt="蓝色圆形")
    second = await tools.generate_image(_tool_context=context, prompt="改成红色", continue_previous=True)
    assert first.metadata["status"] == second.metadata["status"] == "succeeded"
    assert generate.call_args.args[1] == [_png()]
    assert not first.images
    assert not second.images
    other = await tools.generate_image(_tool_context=_context(group=10004), prompt="改成红色", continue_previous=True)
    assert other.metadata["status"] == "failed"
    assert tools.clear_drawing_history(context)


@pytest.mark.asyncio
async def test_failed_delivery_retry_cannot_generate_another_paid_image(monkeypatch):
    context = _context(request_id=17)
    generate = AsyncMock(return_value=(_png(), "image-model"))
    monkeypatch.setattr(tools, "generate_picture", generate)
    monkeypatch.setattr(tools, "guarded_image_bytes", lambda data: data)
    monkeypatch.setattr(tools, "deliver_image", AsyncMock(side_effect=RuntimeError("ack unknown")))
    first = await tools.generate_image(_tool_context=context, prompt="一次生成")
    second = await tools.generate_image(_tool_context=context, prompt="一次生成")
    assert first.metadata["status"] == "failed"
    assert second.metadata["status"] == "already_attempted"
    assert generate.await_count == 1


@pytest.mark.asyncio
async def test_drawing_lock_refuses_same_user_parallel_generation(monkeypatch):
    context = _context()
    started, finish = asyncio.Event(), asyncio.Event()

    async def generate(*args, **kwargs):
        started.set()
        await finish.wait()
        return _png(), "image-model"

    monkeypatch.setattr(tools, "generate_picture", generate)
    monkeypatch.setattr(tools, "guarded_image_bytes", lambda data: data)
    monkeypatch.setattr(tools, "deliver_image", AsyncMock())
    active = asyncio.create_task(tools.generate_image(_tool_context=context, prompt="one"))
    await started.wait()
    try:
        refused = await tools.generate_image(_tool_context=context, prompt="two")
        assert "上一张图" in refused.text
    finally:
        finish.set()
        await active


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "gb18030"])
async def test_document_text_decoding(encoding):
    text, pictures, warnings = await extract_document(MediaAsset("file", "技能.txt",
        payload="水魔爆19级".encode(encoding), mime="text/plain"))
    assert text == "水魔爆19级"
    assert not pictures
    assert not warnings


@pytest.mark.asyncio
async def test_docx_and_pdf_are_actually_extracted_instead_of_reporting_upload_success():
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("word/document.xml", '<w:document xmlns:w="urn:w"><w:p><w:r><w:t>技能19级</w:t></w:r></w:p></w:document>')
    text, _, _ = await extract_document(MediaAsset("file", "技能.docx", payload=archive.getvalue()))
    assert text == "技能19级"
    pytest.importorskip("pypdf")
    # Pillow creates a real scanned PDF. The worker must return its page
    # image, so the configured vision model can read it.
    pdf = BytesIO()
    Image.new("RGB", (32, 32), "blue").save(pdf, format="PDF")
    text, pictures, warnings = await extract_document(MediaAsset("file", "扫描.pdf", payload=pdf.getvalue()))
    assert "第1页" in text
    assert len(pictures) == 1
    assert not warnings
    assert models.image_uri(pictures[0]).startswith("data:image/")


def test_media_model_configuration_can_be_changed_without_provider_specific_keys(tmp_path, monkeypatch):
    selector = _selector(tmp_path, monkeypatch)
    assert "已切换" in selector.set_media_model("multimodal_model", "backup (good)")
    assert selector.model_config["multimodal_model"] == "backup (good)"
    assert "已关闭" in selector.set_media_model("multimodal_model", "关闭")
    assert selector.get_model("multimodal_model") is None

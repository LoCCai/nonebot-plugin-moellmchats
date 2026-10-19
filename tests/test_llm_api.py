from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nonebot_plugin_moellmchats import llm_api as module
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin


class _Api(LlmApiMixin):
    pass


class _ErrorResponse:
    status = 400

    def __init__(self, body: str) -> None:
        self.body = body

    async def text(self) -> str:
        return self.body


class _StreamResponse:
    status = 200

    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.content = self.lines()

    async def lines(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}).encode()
        yield b'data: {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 8}}'
        yield b"data: [DONE]"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _stream(monkeypatch, chunks, *, max_segments=5, is_segment=True, temperament="七七", render_buffer=False):
    api = _Api()
    api._buffer_structured_reply = lambda: render_buffer
    api.model_info = {"max_segments": max_segments}
    api.temperament = temperament
    api.send_emotion_message = AsyncMock(side_effect=lambda text: text)
    usage = []
    api._record_token_usage = usage.append
    pacing = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", pacing)
    response = _StreamResponse(chunks)
    result = await api.stream_llm_chat(
        SimpleNamespace(post=lambda *args, **kwargs: response), "https://model.invalid",
        {}, {}, None, is_segment=is_segment,
    )
    sent = [call.args[0] for call in api.send_emotion_message.await_args_list]
    return result, sent, usage, pacing, response


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "方向对照：\n|方程|方向|\n|---|---|\n|$r=a(1+\\sin\\theta)$|朝下|\n面积 $A=3\\pi/2$。",
    "公式：\\[A=3\\pi/2\\]。\n\n" + "说明。\n" * 400,
])
async def test_stream_keeps_complete_formulas_and_tables_for_rendering(monkeypatch, text):
    result, sent, _, _, response = await _stream(monkeypatch, list(text), render_buffer=True)
    assert result[1] == text
    assert sent == [text]
    assert response.consumed == len(text)


@pytest.mark.asyncio
async def test_buffered_plain_stream_still_uses_bounded_text_messages(monkeypatch):
    text = "普通文字。\n" * 400
    result, sent, _, _, _ = await _stream(monkeypatch, [text], render_buffer=True)
    assert result[1] == text
    assert "".join(sent) == text
    assert all(len(part) <= 1200 for part in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_segments", [1, 2, 5, 0, None, "invalid"])
async def test_stream_segment_budget_preserves_full_answer_and_final_usage(monkeypatch, max_segments):
    text = "第一句。第二句！第三句？第四句。第五句。第六句。第七句。结尾仍然在。"
    result, sent, usage, pacing, response = await _stream(monkeypatch, list(text), max_segments=max_segments)
    assert result[:3] == (True, text, None)
    assert "".join(sent) == text
    budget = max(1, max_segments) if isinstance(max_segments, int) else 5
    assert len(sent) <= budget
    assert response.consumed == len(text)
    assert usage == [{"prompt_tokens": 2, "completion_tokens": 8}]
    assert all(call.args[0] <= 2 for call in pacing.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_segment", "temperament"), [(True, "七七"), (False, "七七"), (True, "ai助手")])
async def test_stream_long_answer_uses_safe_message_sizes_without_loss(monkeypatch, is_segment, temperament):
    text = "说明。\n" * 1100 + "最终结论"
    result, sent, _, pacing, _ = await _stream(monkeypatch, [text], is_segment=is_segment, temperament=temperament)
    assert result[1] == text
    assert "".join(sent) == text
    assert all(len(part) <= 1200 for part in sent)
    assert all(call.args[0] <= 2 for call in pacing.await_args_list)


@pytest.mark.asyncio
async def test_stream_thinking_tags_across_chunks_do_not_leak_into_sentences(monkeypatch):
    chunks = ["开头。<th", "ink>不要公开。\n", "</thi", "nk>正文。<thought>", "不要公开2", "</thought>末尾。比较 a < b。"]
    result, sent, _, _, _ = await _stream(monkeypatch, chunks)
    expected = "开头。正文。末尾。比较 a < b。"
    assert result[1] == expected
    assert "".join(sent) == expected


@pytest.mark.asyncio
async def test_stream_delivery_error_is_propagated_without_retry(monkeypatch):
    api = _Api()
    api.model_info = {"max_segments": 1}
    api.temperament = "七七"
    api.send_emotion_message = AsyncMock(side_effect=TimeoutError("delivery unknown"))
    response = _StreamResponse(["完整回复"])
    with pytest.raises(TimeoutError):
        await api.stream_llm_chat(
            SimpleNamespace(post=lambda *args, **kwargs: response), "https://model.invalid", {}, {}, None, True
        )
    api.send_emotion_message.assert_awaited_once()


def test_sse_done_marker_variants() -> None:
    is_done = _Api._is_sse_done
    assert is_done(b"data: [DONE]")
    assert is_done(b"data:[DONE]")
    assert is_done(b"[DONE]")
    assert is_done(b"data: [DONE]\r")
    assert not is_done(b'data: {"choices": []}')
    assert not is_done(b"event: message")


def test_sse_payload_skips_control_lines_arrays_and_scalars() -> None:
    decode = _Api._decode_sse_payload
    for line in (
        b"event: message",
        b"retry: 1000",
        b"id: 42",
        b": keep-alive",
        b"",
        b"\r",
        b"[1, 2]",
        b'data: ["not", "an", "object"]',
        b'data: "scalar"',
    ):
        assert decode(line) is None


def test_sse_payload_extracts_only_json_objects() -> None:
    decode = _Api._decode_sse_payload
    assert decode(b'data: {"choices": []}') == {"choices": []}
    assert decode(b'data:{"choices": []}') == {"choices": []}
    assert decode(b'  data: {"a": 1}  ') == {"a": 1}
    assert decode(b'{"usage": {"prompt_tokens": 1}}') == {
        "usage": {"prompt_tokens": 1}
    }


def test_bad_sse_line_log_never_contains_payload(
    monkeypatch,
) -> None:
    records: list[str] = []

    class _Recorder:
        def debug(self, message, *args) -> None:
            records.append(str(message).format(*args))

    monkeypatch.setattr(module, "logger", _Recorder())
    raw = b'{"token":"SECRET_PAYLOAD"'

    assert _Api._decode_sse_payload(raw) is None
    assert records
    assert all("SECRET_PAYLOAD" not in record for record in records)
    assert any("payload_sha256" in record for record in records)


@pytest.mark.asyncio
async def test_http_400_policy_detection_uses_only_structured_code_or_type() -> None:
    api = _Api()
    ordinary = await api._check_400_error(
        _ErrorResponse(
            json.dumps(
                {
                    "error": {
                        "code": "invalid_parameter",
                        "type": "invalid_request_error",
                        "message": "audit and safety options are unsupported",
                    }
                }
            )
        )
    )
    blocked = await api._check_400_error(
        _ErrorResponse(
            json.dumps(
                {
                    "error": {
                        "code": "Content-Filter",
                        "message": "rejected",
                    }
                }
            )
        )
    )
    invalid = await api._check_400_error(
        _ErrorResponse("not-json audit safety content_filter")
    )

    assert ordinary is not None
    assert "API请求被拒绝" in ordinary
    assert ordinary != "图片或内容可能包含敏感信息"
    assert blocked == "图片或内容可能包含敏感信息"
    assert invalid is not None
    assert "API请求被拒绝" in invalid

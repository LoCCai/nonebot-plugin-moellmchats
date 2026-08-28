from __future__ import annotations

from nonebot_plugin_moellmchats.llm_api import LlmApiMixin


class _Api(LlmApiMixin):
    pass


def test_sse_done_marker_variants() -> None:
    is_done = _Api._is_sse_done
    assert is_done(b"data: [DONE]")
    assert is_done(b"data:[DONE]")
    assert is_done(b"[DONE]")
    assert is_done(b"data: [DONE]\r")
    assert not is_done(b'data: {"choices": []}')
    assert not is_done(b"event: message")


def test_sse_payload_skips_non_data_lines() -> None:
    decode = _Api._decode_sse_payload
    # 回归：event:/retry:/:注释 行此前会被喂给 json.loads 并抛 ValueError，
    # 导致整轮请求重试、分段内容重复发送
    assert decode(b"event: message") is None
    assert decode(b"retry: 1000") is None
    assert decode(b"id: 42") is None
    assert decode(b": keep-alive") is None
    assert decode(b"") is None
    assert decode(b"\r") is None


def test_sse_payload_extracts_data_lines() -> None:
    decode = _Api._decode_sse_payload
    assert decode(b'data: {"choices": []}') == '{"choices": []}'
    assert decode(b'data:{"choices": []}') == '{"choices": []}'
    assert decode(b'  data: {"a": 1}  ') == '{"a": 1}'


def test_sse_payload_preserves_bare_ndjson_lines() -> None:
    # 个别网关省略 data: 前缀直接输出 JSON 行，必须保持兼容
    decode = _Api._decode_sse_payload
    assert decode(b'{"choices": []}') == '{"choices": []}'
    assert decode(b"[1, 2]") == "[1, 2]"


def test_sse_payload_rejects_plain_text_lines() -> None:
    decode = _Api._decode_sse_payload
    assert decode(b"hello world") is None
    # [DONE] 由 _is_sse_done 在解码前拦截，这里只保证不会解析成裸文本崩溃
    assert decode(b"data: [DONE]") == "[DONE]"

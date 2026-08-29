from __future__ import annotations

import json

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

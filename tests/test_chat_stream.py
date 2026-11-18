import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

from nonebot_plugin_moellmchats.chat_stream import Completion, StreamIncomplete, read_completion
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin


def chunk(delta=None, finish=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def call(index, *, identifier=None, name=None, arguments=None):
    value = {"index": index, "function": {}}
    if identifier is not None:
        value["id"] = identifier
    if name is not None:
        value["function"]["name"] = name
    if arguments is not None:
        value["function"]["arguments"] = arguments
    return value


class Response:
    status = 200

    def __init__(self, chunks):
        self.headers = {"Content-Type": "text/event-stream"}
        self.chunks = chunks
        self.content = self.lines()

    async def lines(self):
        for item in self.chunks:
            yield b"data: " + (item if isinstance(item, bytes) else json.dumps(item).encode()) + b"\n"
            yield b"\n"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def tool_chunks():
    return [
        chunk(
            {
                "reasoning_content": "private reasoning",
                "content": "查询中",
                "tool_calls": [
                    call(1, identifier="second", name="get_", arguments='{"uid":'),
                    call(0, identifier="first", name="read", arguments="{"),
                ],
            }
        ),
        chunk({"tool_calls": [call(0, arguments='"page":1}'), call(1, name="user", arguments='"七七"}')]}),
        chunk(finish="tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 12}},
        b"[DONE]",
    ]


@pytest.mark.asyncio
async def test_interleaved_tool_arguments_and_reasoning_never_deliver_before_execution():
    api = LlmApiMixin()
    api.model_info = {"model": "test", "stream": True}
    api.send_emotion_message = AsyncMock()
    usages = []
    api._record_token_usage = usages.append
    result = await api.stream_llm_chat(
        SimpleNamespace(post=lambda *a, **kw: Response(tool_chunks())), "http://test.invalid", {}, {"tools": [{}]}, None
    )
    assert result[:2] == (True, "查询中")
    assert result[3] == "private reasoning"
    assert [c["id"] for c in result[2]] == ["first", "second"]
    assert result[2][1]["function"] == {"name": "get_user", "arguments": '{"uid":"七七"}'}
    api.send_emotion_message.assert_not_called()
    assert usages == [{"prompt_tokens": 11, "completion_tokens": 12}]


@pytest.mark.parametrize(
    "ending", [[], [b"[DONE]"], [chunk(finish="length"), b"[DONE]"], [chunk(finish="tool_calls"), b"[DONE]"]]
)
@pytest.mark.asyncio
async def test_truncated_tool_never_returns_executable_call(ending):
    response = Response([chunk({"tool_calls": [call(0, identifier="write", name="delete", arguments='{"x":')]}), *ending])
    with pytest.raises(StreamIncomplete):
        await read_completion(response)


@pytest.mark.asyncio
async def test_complete_json_but_length_finish_is_not_executable():
    with pytest.raises(StreamIncomplete):
        await read_completion(
            Response(
                [
                    chunk({"tool_calls": [call(0, identifier="write", name="delete", arguments="{}")]}),
                    chunk(finish="length"),
                    b"[DONE]",
                ]
            )
        )


@pytest.mark.parametrize(
    "bad",
    [
        chunk({"tool_calls": [call(-1, identifier="a", name="write", arguments="{}")]}),
        chunk({"tool_calls": [call(64, identifier="a", name="write", arguments="{}")]}),
        {"error": {"message": "private secret"}},
        b'{"private secret":',
    ],
)
def test_bad_events_fail_without_exposing_payload(bad):
    c = Completion()
    with pytest.raises(StreamIncomplete) as caught:
        c.feed(bad if isinstance(bad, bytes) else json.dumps(bad).encode())
    assert "private secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_finish_plus_usage_without_done_is_valid_and_counts_last_usage_once():
    result = await read_completion(
        Response(
            [
                chunk({"content": "正文"}),
                {"choices": [], "usage": {"completion_tokens": 1}},
                chunk(finish="stop"),
                {"choices": [], "usage": {"completion_tokens": 3}},
            ]
        )
    )
    assert result["choices"][0]["message"]["content"] == "正文"
    assert result["usage"]["completion_tokens"] == 3


@pytest.mark.asyncio
async def test_deferred_summary_returns_text_without_sending():
    api = LlmApiMixin()
    api.model_info = {"model": "fake", "stream": True}
    api._defer_model_output = True
    api.send_emotion_message = AsyncMock()
    result = await api.stream_llm_chat(
        SimpleNamespace(post=lambda *a, **kw: Response([chunk({"content": "最终总结"}), chunk(finish="stop"), b"[DONE]"])),
        "http://test.invalid",
        {},
        {},
        None,
    )
    assert result[:3] == (True, "最终总结", None)
    api.send_emotion_message.assert_not_called()


@pytest.mark.asyncio
async def test_real_http_utf8_fragments_comments_and_multiline_event():
    async def serve(request):
        assert (await request.json())["stream"] is True
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        # Split Chinese UTF-8 and JSON across arbitrary TCP writes; exercise
        # actual aiohttp StreamReader rather than a line-shaped mock.
        wire = (
            b": heartbeat\r\n\r\n"
            + (
                'data: {"choices": [\ndata: {"index":0,"delta":{"content":"七七"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ).encode()
        )
        for i in range(0, len(wire), 2):
            await response.write(wire[i : i + 2])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/chat", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as client:
            async with client.post(f"http://127.0.0.1:{port}/chat", json={"stream": True}) as response:
                assert (await read_completion(response))["choices"][0]["message"]["content"] == "七七"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_two_tool_rounds_then_final_reply_stay_streaming(monkeypatch):
    from test_moe_llm import _empty_reply_chat

    from nonebot_plugin_moellmchats import moe_llm

    chat, entity = _empty_reply_chat(monkeypatch, [])
    chat.model_info["stream"] = True
    chat.temperament = "七七"
    chat._buffer_structured_reply = lambda: False
    chat._buffer_speech_reply = lambda: False
    chat._build_payload = lambda history: (
        {"messages": history, "stream": True, "tools": [{"type": "function", "function": {"name": "read"}}]},
        True,
    )
    responses = [
        Response(tool_chunks()),
        Response(tool_chunks()),
        Response([chunk({"content": "最终结果"}), chunk(finish="stop"), b"[DONE]"]),
    ]
    requests = []

    def post(*args, **kwargs):
        assert kwargs["json"]["stream"] is True
        requests.append(kwargs["json"]["messages"][:])
        return responses.pop(0)

    monkeypatch.setattr(moe_llm, "get_session", lambda: SimpleNamespace(post=post))

    async def execute(calls, text, history, reasoning):
        assert reasoning == "private reasoning"
        updated = [
            *history,
            {"role": "assistant", "content": text, "tool_calls": calls},
            *[{"role": "tool", "tool_call_id": c["id"], "content": "ok"} for c in calls],
        ]
        entity.tool_messages = updated[1:]
        return updated

    chat._execute_tools = AsyncMock(side_effect=execute)
    chat.send_emotion_message = AsyncMock(side_effect=lambda text: text)
    assert await chat.get_llm_chat() is True
    assert len(requests) == 3
    assert chat._execute_tools.await_count == 2
    chat.send_emotion_message.assert_awaited_once_with("最终结果")


@pytest.mark.asyncio
async def test_fallback_keeps_streaming_and_tools(tmp_path, monkeypatch):
    from test_model_failover import _pair

    from nonebot_plugin_moellmchats import moe_llm
    from nonebot_plugin_moellmchats.moe_llm import MoeLlm

    selector = _pair(tmp_path, monkeypatch)
    backup = selector.get_model("selected_model")
    backup["stream"] = True
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", lambda *a, **kw: backup)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    modes = []

    async def model(session, url, headers, data, *args):
        modes.append(data["stream"])
        assert data["tools"]
        if len(modes) == 1:
            chat._last_api_http_status = 503
            return False, "busy", None, ""
        return True, "done", None, ""

    chat.stream_llm_chat = model
    chat.none_stream_llm_chat = AsyncMock(side_effect=AssertionError("non-stream fallback"))
    result = await chat._request_with_rate_limit_fallback(object(), {}, {"stream": True, "tools": [{}]}, True)
    assert result[0]
    assert modes == [True, True]


@pytest.mark.asyncio
async def test_classifier_consumes_streamed_json_and_preserves_schema(monkeypatch):
    from test_cache_runtime_wiring import _configure_runtime, _FakeResponse, _FakeSession

    from nonebot_plugin_moellmchats import categorize as category_module

    _configure_runtime(monkeypatch)
    # The classifier's actual expected schema is taken from its existing response
    # fixture, retaining the classification assertions from that integration test.
    from test_cache_runtime_wiring import _model_response

    response = _FakeResponse(_model_response())
    text = response.payload["choices"][0]["message"]["content"]
    response.headers["Content-Type"] = "text/event-stream"
    response.content = Response(
        [chunk({"content": text[:7]}), chunk({"content": text[7:]}), chunk(finish="stop"), b"[DONE]"]
    ).content
    response.json = AsyncMock(side_effect=AssertionError("SSE is not a JSON response body"))
    session = _FakeSession([response])
    monkeypatch.setattr(category_module, "get_session", lambda: session)
    result = await category_module.Categorize("当前真实输入")._request_category_model(
        {"model": "classifier", "url": "https://test.invalid", "key": "test-only", "stream": True}, "分类提示"
    )
    assert result == ("1", False, ["alpha"])
    assert json.loads(session.requests[0]["data"])["stream"] is True
    response.json.assert_not_called()

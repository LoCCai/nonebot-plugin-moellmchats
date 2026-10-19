from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from test_llm_api import _StreamResponse
from test_model_failover import _pair

from nonebot_plugin_moellmchats import llm_api, moe_llm
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


@pytest.mark.parametrize(("remaining", "expected"), [(480, 180), (180, 90), (20, 10), (0.5, 0.25)])
def test_model_attempt_leaves_time_for_backup_tools_and_delivery(remaining, expected):
    chat = object.__new__(MoeLlm)
    chat._remaining_request_seconds = lambda: remaining
    assert chat._llm_request_timeout().total == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["stall", "connection"])
async def test_stalled_transport_switches_to_configured_backup_without_replaying_tools(
    tmp_path, monkeypatch, stream, failure,
):
    selector = _pair(tmp_path, monkeypatch)
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    monkeypatch.setattr(moe_llm, "_MODEL_RESPONSE_TIMEOUT_SECONDS", 0.03)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    requests, cancelled = [], []

    async def model(session, url, headers, data, *args):
        requests.append((url, dict(headers), deepcopy(data), args[-1].total))
        if len(requests) == 1:
            if failure == "connection":
                raise aiohttp.ServerDisconnectedError("PRIVATE_PROVIDER_DETAIL")
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        return True, "backup answer", None, ""

    chat.stream_llm_chat = chat.none_stream_llm_chat = model
    history = [{"role": "tool", "tool_call_id": "done", "content": "completed result"}]
    tools = [{"type": "function", "function": {"name": "plot_function"}}]
    result = await asyncio.wait_for(chat._request_with_rate_limit_fallback(
        object(), {"Authorization": "Bearer primary"},
        {"model": "model-a", "messages": history, "tools": tools, "stream": stream}, stream,
    ), timeout=1)
    assert result[:2] == (True, "backup answer")
    assert len(requests) == 2
    assert requests[1][1]["Authorization"] == "Bearer test-backup"
    assert requests[1][2]["messages"] == history
    assert requests[1][2]["tools"] == tools
    assert all(request[3] <= 0.03 for request in requests)
    assert bool(cancelled) is (failure == "stall")
    assert not selector.is_model_exhausted("model-a (good)")


@pytest.mark.asyncio
async def test_timeout_without_backup_returns_bounded_failure(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["model_fallbacks"] = {}
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    monkeypatch.setattr(moe_llm, "_MODEL_RESPONSE_TIMEOUT_SECONDS", 0.01)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    chat.none_stream_llm_chat = AsyncMock(side_effect=lambda *args: None)

    async def wait(*args):
        await asyncio.Event().wait()

    chat.none_stream_llm_chat.side_effect = wait
    result = await asyncio.wait_for(chat._request_with_rate_limit_fallback(
        object(), {}, {"messages": []}, False,
    ), timeout=1)
    assert not result[0]
    assert "模型响应超时" in result[1]
    chat.none_stream_llm_chat.assert_awaited_once()
    assert not chat._last_api_error_non_retryable  # Existing outer retry remains bounded.


@pytest.mark.asyncio
async def test_visible_or_uncertain_stream_is_never_replayed_after_timeout(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    fallback = AsyncMock()
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", fallback)
    monkeypatch.setattr(moe_llm, "_MODEL_RESPONSE_TIMEOUT_SECONDS", 0.01)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None

    async def partial(*args):
        chat._last_api_output_started = True
        await asyncio.Event().wait()

    chat.stream_llm_chat = partial
    result = await chat._request_with_rate_limit_fallback(object(), {}, {"messages": []}, True)
    assert not result[0]
    assert chat._last_api_error_non_retryable
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_start_backup(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    fallback = AsyncMock()
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", fallback)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    chat.none_stream_llm_chat = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await chat._request_with_rate_limit_fallback(object(), {}, {"messages": []}, False)
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_real_stream_marks_output_before_uncertain_delivery(monkeypatch):
    api = LlmApiMixin()
    api.model_info, api.temperament = {}, "ai助手"
    api.send_emotion_message = AsyncMock(side_effect=TimeoutError)
    response = _StreamResponse(["public answer"])
    monkeypatch.setattr(llm_api, "record_usage", AsyncMock())
    with pytest.raises(TimeoutError):
        await api.stream_llm_chat(
            SimpleNamespace(post=lambda *args, **kwargs: response),
            "https://example.invalid", {}, {}, None,
        )
    assert api._last_api_output_started

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_model_selector import _initialized_selector

from nonebot_plugin_moellmchats import moe_llm
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


def _pair(tmp_path, monkeypatch):
    selector = _initialized_selector(tmp_path, monkeypatch)
    selector.models["backup (other)"] = {
        "model": "backup", "provider": "other", "url": "https://backup.invalid/v1/chat/completions",
        "key": ["test-backup"], "stream": False, "extra_payload": {"thinking": {"type": "disabled"}},
    }
    selector.model_config["model_fallbacks"] = {"model-a (good)": ["backup (other)"]}
    return selector


def test_fallback_uses_configured_identity_credentials_and_avoids_cycles(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["model_fallbacks"]["backup (other)"] = ["model-a (good)"]
    current = selector.get_model("category_model")
    attempted = set()
    backup = selector.get_rate_limit_fallback(current, attempted)
    assert backup["provider"] == "other"
    assert backup["key"] == "Bearer test-backup"
    assert selector.get_rate_limit_fallback(backup, attempted) is None
    assert attempted == {"model-a (good)", "backup (other)"}


@pytest.mark.parametrize(("options", "requirements"), [
    ({"no_tools": True}, {"has_tools": True}),
    ({"is_vision": False}, {"has_images": True}),
    ({"key": []}, {}),
])
def test_ineligible_backup_is_not_selected(tmp_path, monkeypatch, options, requirements):
    selector = _pair(tmp_path, monkeypatch)
    selector.models["backup (other)"].update(options)
    assert selector.get_rate_limit_fallback(selector.get_model("category_model"), set(), **requirements) is None


@pytest.mark.parametrize("bad", [[], {"missing": ["backup (other)"]},
    {"model-a (good)": []}, {"model-a (good)": ["backup (other)"] * 2},
    {"model-a (good)": ["model-a (good)"]}, {"model-a (good)": ["unknown"]}])
def test_invalid_pairs_are_rejected_on_candidate_validation(tmp_path, monkeypatch, bad):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["model_fallbacks"] = bad
    valid, errors = selector.validate_model_config(persist=False)
    assert not valid
    assert "model_fallbacks" in errors[0]


class _RateLimitResponse:
    status = 429

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        pytest.fail("429 body must not delay failover or leak provider text")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_both_transports_report_429_before_reading_body(stream):
    api = LlmApiMixin()
    session = SimpleNamespace(post=lambda *a, **k: _RateLimitResponse())
    operation = api.stream_llm_chat if stream else api.none_stream_llm_chat
    result = await operation(session, "https://primary.invalid", {}, {}, None)
    assert not result[0]
    assert api._last_api_http_status == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("backup_ok", [False, True])
async def test_chat_failover_preserves_history_tools_and_uses_backup_transport(
    tmp_path, monkeypatch, stream, backup_ok,
):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["model_fallbacks"]["backup (other)"] = ["model-a (good)"]
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    chat = object.__new__(MoeLlm)
    chat.model_info = selector.get_model("category_model")
    chat.model_info["extra_payload"] = {"primary_only_option": True}
    chat.agent_runtime = None
    chat._last_api_error_non_retryable = False
    requests = []

    async def model(session, url, headers, data, *args):
        requests.append((url, dict(headers), deepcopy(data)))
        chat._last_api_http_status = 200 if len(requests) == 2 and backup_ok else 429
        return chat._last_api_http_status == 200, "answer", None, ""

    chat.stream_llm_chat = model
    chat.none_stream_llm_chat = model
    messages = [{"role": "tool", "tool_call_id": "already-executed", "content": "completed-result"}]
    tools = [{"type": "function", "function": {"name": "query"}}]
    data = {"model": "model-a", "messages": messages, "tools": tools,
            "stream": stream, "primary_only_option": True}
    result = await chat._request_with_rate_limit_fallback(object(), {"Authorization": "Bearer one"}, data, stream)
    assert result[0] is backup_ok
    assert result[-1] is False
    assert len(requests) == 2
    assert requests[1][0] == "https://backup.invalid/v1/chat/completions"
    assert requests[1][1]["Authorization"] == "Bearer test-backup"
    assert requests[1][2]["messages"] == messages
    assert requests[1][2]["tools"] == tools
    assert requests[1][2]["model"] == "backup"
    assert "primary_only_option" not in requests[1][2]
    assert requests[1][2]["thinking"] == {"type": "disabled"}
    assert chat._last_api_error_non_retryable is (not backup_ok)
    if backup_ok:
        next_round = await chat._request_with_rate_limit_fallback(object(), {"Authorization": "Bearer test-backup"}, data, False)
        assert not next_round[0]
        assert len(requests) == 3
        assert requests[-1][2]["model"] == "backup"

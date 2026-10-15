from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from test_model_selector import _initialized_selector

from nonebot_plugin_moellmchats import llm_payload, moe_llm
from nonebot_plugin_moellmchats.llm_api import LlmApiMixin
from nonebot_plugin_moellmchats.model_error_policy import is_quota_exhaustion_error, read_bounded_error
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


def _pair(tmp_path, monkeypatch):
    selector = _initialized_selector(tmp_path, monkeypatch)
    monkeypatch.setattr(moe_llm.model_selector, "mark_model_exhausted", selector.mark_model_exhausted)
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


@pytest.mark.parametrize("primary_options", [
    {"is_vision": False}, {"no_tools": True}, {"key": [], "keys": []},
])
def test_vision_pin_uses_eligible_backup_when_primary_cannot_serve_images_and_tools(
    tmp_path, monkeypatch, primary_options,
):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["vision_model"] = "model-a (good)"
    selector.models["model-a (good)"].update(is_vision=True, no_tools=False)
    selector.models["model-a (good)"].update(primary_options)
    selector.models["backup (other)"]["is_vision"] = True
    chosen = selector.get_model("vision_model", has_images=True, has_tools=True)
    assert chosen["model"] == "backup"
    assert chosen["key"] == "Bearer test-backup"


@pytest.mark.asyncio
async def test_exhausted_vision_pair_reaches_configured_cross_provider_backup(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["vision_model"] = "model-a (good)"
    selector.models["empty-vision (good)"] = dict(selector.models["model-a (good)"], model="empty-vision")
    selector.models["backup (other)"]["is_vision"] = True
    selector.model_config["model_fallbacks"]["model-a (good)"] = ["empty-vision (good)", "backup (other)"]
    await selector.mark_model_exhausted(selector.models["model-a (good)"], status=403)
    await selector.mark_model_exhausted(selector.models["empty-vision (good)"], status=403)
    chosen = selector.get_model("vision_model", has_images=True, has_tools=True)
    assert chosen["provider"] == "other"
    assert chosen["is_vision"]
    assert selector.is_model_exhausted("model-a (good)")


@pytest.mark.asyncio
async def test_exhausted_vision_chain_does_not_claim_it_was_never_configured(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config.update(vision_model="model-a (good)", use_tools=False)
    for model in selector.models.values():
        await selector.mark_model_exhausted(model, status=403)
    monkeypatch.setattr(llm_payload, "model_selector", selector)
    chat = object.__new__(MoeLlm)
    chat.model_info = {}
    chat.messages_handler = SimpleNamespace(current_images=["https://image.invalid/input"])
    result = await chat._prepare_model_info("看看图片")
    assert "额度耗尽" in result
    assert "查看LLM链路" in result
    assert "未配置" not in result


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
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_both_transports_report_recoverable_status_before_reading_body(stream, status):
    api = LlmApiMixin()
    response = _RateLimitResponse()
    response.status = status
    session = SimpleNamespace(post=lambda *a, **k: response)
    operation = api.stream_llm_chat if stream else api.none_stream_llm_chat
    result = await operation(session, "https://primary.invalid", {}, {}, None)
    assert not result[0]
    assert api._last_api_http_status == status
    assert not getattr(api, "_last_api_quota_exhausted", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("backup_ok", [False, True])
@pytest.mark.parametrize("primary_status", [429, 402, 403, 500, 502, 503, 504])
async def test_chat_failover_preserves_history_tools_and_uses_backup_transport(
    tmp_path, monkeypatch, stream, backup_ok, primary_status,
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
        chat._last_api_http_status = 200 if len(requests) == 2 and backup_ok else primary_status
        chat._last_api_quota_exhausted = chat._last_api_http_status in {402, 403}
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
    assert selector.is_model_exhausted("model-a (good)") is (primary_status in {402, 403})
    if backup_ok:
        next_round = await chat._request_with_rate_limit_fallback(object(), {"Authorization": "Bearer test-backup"}, data, False)
        assert not next_round[0]
        assert len(requests) == 3
        assert requests[-1][2]["model"] == "backup"


class _JsonResponse(_RateLimitResponse):
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def text(self):
        return json.dumps(self.body)

    async def json(self):
        return self.body


@pytest.mark.parametrize(("status", "error", "expected"), [
    (403, {"code": "AllocationQuota.FreeTierOnly", "message": "Free quota exhausted."}, True),
    (402, {"code": "insufficient_quota"}, True),
    (403, {"code": "Forbidden", "message": "Free quota exhausted. To continue accessing the model..."}, True),
    (403, {"message": "Free quota exhausted."}, True),
    (403, {"code": "AccessDenied", "message": "Free quota exhausted."}, False),
    (403, {"code": "InvalidApiKey"}, False),
    (403, {"code": "content_policy_violation", "message": "Free quota exhausted."}, False),
    (401, {"code": "insufficient_quota"}, False),
    (403, {"message": "Forbidden"}, False),
])
def test_quota_policy_only_matches_explicit_billing_failures(status, error, expected):
    assert is_quota_exhaustion_error(status, json.dumps({"error": error})) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("quota", [False, True])
async def test_transports_classify_403_without_retrying_generic_forbidden(stream, quota):
    api = LlmApiMixin()
    code = "AllocationQuota.FreeTierOnly" if quota else "InvalidApiKey"
    session = SimpleNamespace(post=lambda *args, **kwargs: _JsonResponse(403, {"error": {"code": code}}))
    operation = api.stream_llm_chat if stream else api.none_stream_llm_chat
    result = await operation(session, "https://primary.invalid", {}, {}, None)
    assert not result[0]
    assert api._last_api_http_status == 403
    assert api._last_api_quota_exhausted is quota
    assert api._last_api_error_non_retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("quota", [False, True])
async def test_actual_403_transport_only_routes_quota_to_backup(tmp_path, monkeypatch, quota):
    selector = _pair(tmp_path, monkeypatch)
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    requests = []
    responses = iter([
        _JsonResponse(403, {"error": {"code": "AllocationQuota.FreeTierOnly" if quota else "AccessDenied"}}),
        _JsonResponse(200, {"choices": [{"message": {"content": "backup answer"}}]}),
    ])

    def post(**kwargs):
        requests.append(kwargs)
        return next(responses)

    result = await chat._request_with_rate_limit_fallback(
        SimpleNamespace(post=post), {"Authorization": "Bearer primary"},
        {"model": "model-a", "messages": [{"role": "user", "content": "hello"}]}, False,
    )
    assert result[0] is quota
    assert len(requests) == (2 if quota else 1)
    if quota:
        assert result[1] == "backup answer"
        assert requests[1]["headers"]["Authorization"] == "Bearer test-backup"


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_exhausted", [False, True])
@pytest.mark.parametrize("last_ok", [False, True])
async def test_transient_chain_keeps_remaining_vision_backups_and_completed_tools(
    tmp_path, monkeypatch, primary_exhausted, last_ok,
):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["vision_model"] = "model-a (good)"
    selector.models["last (other)"] = dict(selector.models["backup (other)"], model="last")
    selector.model_config["model_fallbacks"]["model-a (good)"].append("last (other)")
    for model in selector.models.values():
        model["is_vision"] = True
    if primary_exhausted:
        await selector.mark_model_exhausted(selector.models["model-a (good)"], status=403)
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    chat = object.__new__(MoeLlm)
    chat.model_info = selector.get_model("vision_model", has_images=True, has_tools=True)
    chat.agent_runtime = None
    messages = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://image.invalid/input"}}]},
        {"role": "tool", "tool_call_id": "completed", "content": "saved tool result"},
    ]
    tools = [{"type": "function", "function": {"name": "query"}}]
    requests = []

    def post(**kwargs):
        requests.append(deepcopy(kwargs))
        if kwargs["json"]["model"] == "last" and last_ok:
            return _JsonResponse(200, {"choices": [{"message": {"content": "backup answer"}}]})
        response = _RateLimitResponse()
        response.status = 502
        return response

    result = await chat._request_with_rate_limit_fallback(
        SimpleNamespace(post=post), {"Authorization": chat.model_info["key"]},
        {"model": chat.model_info["model"], "messages": messages, "tools": tools}, False,
    )
    assert result[0] is last_ok
    assert [request["json"]["model"] for request in requests] == (
        ["backup", "last"] if primary_exhausted else ["model-a", "backup", "last"]
    )
    assert all(request["json"]["messages"] == messages and request["json"]["tools"] == tools for request in requests)
    assert not selector.is_model_exhausted("backup (other)")
    assert not selector.is_model_exhausted("last (other)")
    assert selector.is_model_exhausted("model-a (good)") is primary_exhausted
    assert chat._last_api_error_non_retryable is (not last_ok)


@pytest.mark.asyncio
async def test_transient_single_model_preserves_bounded_outer_retry(tmp_path, monkeypatch):
    selector = _pair(tmp_path, monkeypatch)
    selector.model_config["model_fallbacks"] = {}
    monkeypatch.setattr(moe_llm.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    response = _RateLimitResponse()
    response.status = 503
    result = await chat._request_with_rate_limit_fallback(
        SimpleNamespace(post=lambda **kwargs: response), {}, {"model": "model-a", "messages": []}, False,
    )
    assert not result[0]
    assert not chat._last_api_error_non_retryable
    assert not selector.is_model_exhausted("model-a (good)")


@pytest.mark.asyncio
async def test_quota_response_reads_partial_chunks_and_enforces_size_bound():
    chunks = iter([b'{"error":', b'{"message":"Free quota exhausted."}}', b""])

    async def read(size):
        return next(chunks)

    response = SimpleNamespace(content=SimpleNamespace(read=read))
    assert is_quota_exhaustion_error(403, await read_bounded_error(response))
    response.content.read = lambda size: _large_chunk(size)
    assert await read_bounded_error(response) == ""


async def _large_chunk(size):
    return b"x" * size

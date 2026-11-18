from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_media_tools import _context, _png, _selector
from test_model_pools import pool

from nonebot_plugin_moellmchats import media_models, media_tools, model_pools
from nonebot_plugin_moellmchats import usage_ledger as usage
from nonebot_plugin_moellmchats.daily_quota import DailyQuotaStore, apply_openai_chat_policy, daily_store
from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin

MODEL = {
    "model": "gpt-5.4-mini",
    "provider": "openai-free",
    "daily_quota": {"group": "small", "tokens": 10000, "timezone": "UTC"},
    "openai_chat_compat": True,
    "supports_reasoning_effort": True,
    "token_limit_field": "max_completion_tokens",
}
PAYLOAD = {"messages": [{"role": "user", "content": "hello"}], "max_completion_tokens": 100}


def test_shared_group_concurrent_reservations_and_restart(tmp_path):
    path = tmp_path / "daily.sqlite3"
    store = DailyQuotaStore(path)
    now = 1794660000
    other = {**MODEL, "model": "gpt-5-mini"}
    with ThreadPoolExecutor(max_workers=3) as workers:
        statuses = list(workers.map(lambda i: store.reserve(other if i else MODEL, PAYLOAD, str(i), now=now), range(3)))
    assert statuses.count(0) == 2
    assert statuses.count(403) == 1
    restarted = DailyQuotaStore(path)
    assert restarted.remaining(MODEL, now=now) == store.remaining(MODEL, now=now)
    assert restarted.remaining(MODEL, now=now + 86400) == 10000
    assert path.stat().st_mode & 0o777 == 0o600


def test_settlement_unknown_usage_and_definite_rejection(tmp_path):
    store = DailyQuotaStore(tmp_path / "daily.sqlite3")
    assert store.reserve(MODEL, PAYLOAD, "a") == 0
    reserved = store.remaining(MODEL)
    store.finish("a")
    assert store.remaining(MODEL) == reserved
    store.finish("a", used=150)
    assert store.remaining(MODEL) == 9850
    assert store.reserve(MODEL, PAYLOAD, "b") == 0
    store.finish("b", definite_rejection=True)
    assert store.remaining(MODEL) == 9850
    with pytest.raises(ValueError, match="cannot be submitted twice"):
        store.reserve(MODEL, PAYLOAD, "b")


@pytest.mark.parametrize("reasoning", [False, True])
def test_openai_parameters_do_not_leak_between_model_families(reasoning):
    model = {
        **MODEL,
        "supports_reasoning_effort": reasoning,
        "token_limit_field": "max_completion_tokens" if reasoning else "max_tokens",
    }
    payload = {
        "reasoning_effort": "max",
        "temperature": 0.7,
        "top_p": 0.8,
        "thinking": {},
        "tool_stream": True,
        "max_tokens": 400,
        "max_completion_tokens": 300,
        "tools": [{"type": "function"}],
    }
    apply_openai_chat_policy(model, payload)
    assert payload[model["token_limit_field"]] == 300
    assert len({"max_tokens", "max_completion_tokens"} & payload.keys()) == 1
    assert "thinking" not in payload
    assert "tool_stream" not in payload
    assert payload["tools"] == [{"type": "function"}]
    if reasoning:
        assert payload["reasoning_effort"] == "high"
        assert "temperature" not in payload
        assert "top_p" not in payload
    else:
        assert "reasoning_effort" not in payload
        assert payload["temperature"] == 0.7


def test_gpt54_tools_use_supported_reasoning_mode_only_when_present():
    model = {**MODEL, "tool_reasoning_effort": "none"}
    payload = {"reasoning_effort": "low", "tools": [{"type": "function"}]}
    apply_openai_chat_policy(model, payload)
    assert payload["reasoning_effort"] == "none"
    plain = {"reasoning_effort": "low"}
    apply_openai_chat_policy(model, plain)
    assert plain["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_transport_stops_before_http_and_settles_actual_usage(tmp_path, monkeypatch):
    ledger = usage.UsageLedger(tmp_path / "usage.sqlite3")
    ledger.initialize()
    monkeypatch.setattr(usage, "_ledger", ledger)
    post = Mock()

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    def send(**kwargs):
        post(**kwargs)
        return Response()

    session = SimpleNamespace(post=send)
    async with usage.budgeted_post(session, MODEL, json=deepcopy(PAYLOAD)) as response:
        assert response.status == 200
        await usage.record_usage(MODEL, {"prompt_tokens": 20, "completion_tokens": 30})
    assert daily_store(ledger).remaining(MODEL) == 9950
    tiny = {**MODEL, "daily_quota": {**MODEL["daily_quota"], "tokens": 50}}
    async with usage.budgeted_post(session, tiny, json=PAYLOAD) as response:
        assert response.status == 403
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_daily_group_exhaustion_expires_and_does_not_disable_other_provider(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    for name in ["model-a (good)", "backup (other)"]:
        selector.models[name]["daily_quota"] = MODEL["daily_quota"]
    selector.models["same (good)"] = {**selector.models["model-a (good)"], "model": "same"}
    now = datetime(2026, 11, 14, 23, 59, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(model_pools.time, "time", lambda: now)
    await selector.mark_model_exhausted(selector.models["model-a (good)"], 403)
    assert selector.is_model_exhausted("same (good)")
    assert not selector.is_model_exhausted("backup (other)")
    now += 120
    assert not selector.is_model_exhausted("model-a (good)")
    assert not selector.is_model_exhausted("same (good)")


@pytest.mark.parametrize(("text", "role"), [("早上好", "0"), ("写一段python代码", "1"), ("请证明这个数学公式", "2")])
def test_ordinary_routing_is_local_opt_in(text, role):
    assert LlmPayloadMixin._ordinary_task_pool(text, True) == role
    assert LlmPayloadMixin._ordinary_task_pool(text, False) is None


@pytest.mark.asyncio
async def test_detailed_drawing_uses_dedicated_model_and_parameters(tmp_path, monkeypatch):
    selector = _selector(tmp_path, monkeypatch)
    selector.models["detail (other)"] = {
        **selector.models["model-a (good)"],
        "model": "sunburst",
        "model_type": "image",
        "image_api_format": "openai",
        "image_parameters": {"quality": "medium", "output_format": "png"},
    }
    selector.model_config["image_detailed_model"] = "detail (other)"
    import base64

    post = AsyncMock(return_value=(200, {"data": [{"b64_json": base64.b64encode(_png()).decode()}]}))
    monkeypatch.setattr(media_models, "_post", post)
    picture, name = await media_models.generate_picture("a cloud", [], mode="detailed")
    assert name == "sunburst"
    assert picture == _png()
    assert post.call_args.kwargs["payload"]["quality"] == "medium"
    assert post.call_args.kwargs["payload"]["output_format"] == "png"


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_both_drawing_modes_use_existing_grant_at_spend_time(monkeypatch, allowed):
    monkeypatch.setattr(media_tools, "_image_generation_authorizer", AsyncMock(side_effect=[True, allowed]))
    generate = AsyncMock(return_value=(_png(), "sunburst"))
    monkeypatch.setattr(media_tools, "generate_picture", generate)
    monkeypatch.setattr(media_tools, "deliver_image", AsyncMock())
    result = await media_tools.generate_image(_tool_context=_context(), prompt="a cloud", mode="detailed")
    assert result.metadata["status"] == ("succeeded" if allowed else "denied")
    assert generate.await_count == int(allowed)
    if allowed:
        assert generate.call_args.kwargs["mode"] == "detailed"

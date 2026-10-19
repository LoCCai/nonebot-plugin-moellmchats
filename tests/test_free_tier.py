from __future__ import annotations

from copy import deepcopy
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_model_pools import pool

from nonebot_plugin_moellmchats import free_tier, usage_ledger, usage_reports
from nonebot_plugin_moellmchats.model_capabilities import ModelCapability
from nonebot_plugin_moellmchats.request_budget import (
    apply_payload_budget,
    classification_budget_scope,
    request_budget_scope,
)
from nonebot_plugin_moellmchats.search import Search


@pytest.fixture
def selector(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector.models["backup (other)"].update(billing_type="free", is_vision=True)
    selector.models["third (other)"].update(billing_type="free", is_vision=True)
    selector.model_config["ordinary_model_pools"] = {
        role: ["backup (other)", "third (other)"] for role in ("category", "0", "1", "2", "summary", "vision")
    }
    monkeypatch.setattr(importlib.import_module("nonebot_plugin_moellmchats.model_selector"), "model_selector", selector)
    return selector


@pytest.mark.parametrize(
    "key", ["category_model", "selected_model", "summary_model", "vision_model", "chat_summary_model", "multimodal_model"]
)
def test_ordinary_roles_use_free_pools_and_authorized_routes_stay_separate(selector, key):
    with request_budget_scope(authorized=False):
        assert selector.get_model(key)["billing_type"] == "free"
        for tier in ("0", "1", "2"):
            assert selector.get_moe_current_model(tier)["billing_type"] == "free"
        assert selector.get_web_search() is False
        assert selector.get_model("image_model") is None
    with request_budget_scope(authorized=True):
        assert selector.get_moe_current_model("0")["model"] == "model-a"


def test_free_exhaustion_and_missing_vision_never_enter_paid_or_fixed_routes(selector):
    selector.model_config["model_fallbacks"]["backup (other)"] = ["model-a (good)"]
    with request_budget_scope(authorized=False):
        primary = selector.get_moe_current_model("0")
        attempted = set()
        backup = selector.get_rate_limit_fallback(primary, attempted)
        assert backup["model"] == "third"
        assert selector.get_rate_limit_fallback(backup, attempted) is None
        selector.model_config["ordinary_model_pools"].pop("vision")
        assert selector.get_model("vision_model") is None
        assert selector.get_moe_current_model("0", has_images=True) is None


def test_free_pool_precedes_capability_routes_and_applies_requirements(selector):
    selector.models["backup (other)"]["no_tools"] = True
    selector.model_config["capability_routing"] = {"enabled": True}
    capabilities = ModelCapability(text=True, tools=True, vision=True, json_schema=False, reasoning=False, streaming=False)
    with request_budget_scope(authorized=False):
        assert selector.get_model_for_capabilities("vision_model", capabilities)["model"] == "third"
        assert selector.get_moe_current_model_for_capabilities("2", capabilities)["model"] == "third"


@pytest.mark.parametrize("change", ["paid", "unknown", "missing", "vision"])
def test_invalid_ordinary_pools_reject_configuration(selector, change):
    if change == "paid":
        selector.models["backup (other)"]["billing_type"] = "paid"
    elif change == "unknown":
        selector.model_config["ordinary_model_pools"]["0"] = ["missing"]
    elif change == "missing":
        selector.model_config["ordinary_model_pools"].pop("summary")
    else:
        selector.models["backup (other)"]["is_vision"] = False
    assert selector.validate_model_config()[0] is False


@pytest.mark.asyncio
async def test_last_http_boundary_rejects_paid_copies_and_wrong_endpoints_without_network(selector, monkeypatch):
    ledger = SimpleNamespace(budgets={}, exhausted=lambda _: False)
    monkeypatch.setattr(usage_ledger, "get_ledger", lambda: ledger)
    recorder = AsyncMock()
    monkeypatch.setattr(usage_ledger, "record_usage", recorder)
    post = AsyncMock(side_effect=AssertionError("Network must not be called"))
    paid = selector.models["model-a (good)"]
    forged = {**paid, "billing_type": "free"}
    free = selector.models["backup (other)"]
    with request_budget_scope(authorized=False):
        for model, url in [(paid, paid["url"]), (forged, forged["url"]), (free, free["url"] + "/paid")]:
            async with usage_ledger.budgeted_post(SimpleNamespace(post=post), model, url, json={"messages": []}) as response:
                assert response.status == 503
                assert (await response.json())["error"]["code"] == "free_route_unavailable"
    post.assert_not_called()
    recorder.assert_not_called()


@pytest.mark.asyncio
async def test_free_payload_cannot_override_the_model_or_add_paid_native_tools(selector):
    model = selector.models["backup (other)"]
    post = AsyncMock(side_effect=AssertionError("Network must not be called"))
    with request_budget_scope(authorized=False):
        for extra in ({"model": "paid-model"}, {"web_search_options": {"enable": True}}, {"tools": [{"type": "web_search"}]}):
            async with usage_ledger.budgeted_post(
                SimpleNamespace(post=post), model, model["url"], json={"messages": [], **extra}
            ) as response:
                assert response.status == 503
    post.assert_not_called()


@pytest.mark.asyncio
async def test_tokenhub_direct_transport_is_blocked_for_free_users(selector):
    from nonebot_plugin_moellmchats import tokenhub_tools
    from nonebot_plugin_moellmchats.media_context import MediaError

    with request_budget_scope(authorized=False), pytest.raises(MediaError, match="免费模型链路"):
        await tokenhub_tools.request({}, "/chat/completions")


@pytest.mark.asyncio
async def test_free_http_calls_keep_usage_and_authorized_requests_keep_existing_behavior(selector, monkeypatch):
    monkeypatch.setattr(usage_ledger, "get_ledger", lambda: SimpleNamespace(budgets={}, exhausted=lambda _: False))
    recorder = AsyncMock()
    monkeypatch.setattr(usage_ledger, "record_usage", recorder)
    seen = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def post(url, **kwargs):
        seen.append((url, kwargs["json"]))
        return Response()

    for authorized in (False, True):
        model = selector.models["model-a (good)" if authorized else "backup (other)"]
        with request_budget_scope(authorized=authorized):
            async with usage_ledger.budgeted_post(SimpleNamespace(post=post), model, model["url"], json={"messages": []}):
                pass
    assert len(seen) == 2
    assert recorder.await_count == 4


@pytest.mark.asyncio
async def test_paid_web_search_is_blocked_even_when_invoked_as_a_tool(selector, monkeypatch):
    with request_budget_scope(authorized=False):
        assert free_tier.ordinary_free_only()
        assert "免费模型链路" in await object.__new__(Search).get_search()


@pytest.mark.parametrize("name", ["glm-4-flash-250414", "glm-4.7-flash", "glm-4.6v-flash"])
def test_free_glm_parameters_do_not_inherit_53_thinking_or_paid_search(name):
    model = {"provider": "bigmodel", "model": name}
    payload = {
        "max_tokens": 128,
        "reasoning_effort": "max",
        "thinking": {"type": "enabled"},
        "tool_stream": True,
        "web_search_options": {"enable": True},
        "tools": [{"type": "web_search"}, {"type": "function", "function": {"name": "query_game"}}],
    }
    usage_ledger.apply_glm_policy(model, payload)
    with request_budget_scope(authorized=False), classification_budget_scope():
        apply_payload_budget(model, payload)
    assert payload["max_tokens"] == 128
    assert "reasoning_effort" not in payload
    assert "tool_stream" not in payload
    assert "web_search_options" not in payload
    assert payload["tools"] == [{"type": "function", "function": {"name": "query_game"}}]
    assert payload.get("thinking") == (None if name == "glm-4-flash-250414" else {"type": "disabled"})


def test_glm_report_distinguishes_paid_model_from_expired_promotional_grant(monkeypatch):
    config = deepcopy(usage_reports.usage_display_config)
    config.moellm_glm_promo_exhausted = True
    monkeypatch.setattr(usage_reports, "usage_display_config", config)
    note = usage_reports.glm_note()
    assert all(word in note for word in ("付费模型", "40,000,000", "已用尽", "已停用", "一个月", "0.33"))
    assert "官方免费额度" not in note

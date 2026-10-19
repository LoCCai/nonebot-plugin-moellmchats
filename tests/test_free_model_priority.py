from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_model_pools import pool

from nonebot_plugin_moellmchats import usage_ledger
from nonebot_plugin_moellmchats.request_budget import (
    apply_payload_budget,
    classification_budget_scope,
    request_budget_scope,
)


@pytest.fixture
def selector(tmp_path, monkeypatch):
    selector = pool(tmp_path, monkeypatch)
    selector.models["backup (other)"].update(billing_type="free", priority_consume=True)
    selector.models["model-a (good)"]["billing_type"] = "paid"
    return selector


@pytest.mark.parametrize("authorized", [False, True])
def test_free_model_is_prioritized_in_existing_pools_with_normal_fallback(selector, authorized):
    with request_budget_scope(authorized=authorized):
        assert selector.get_model("category_model")["model"] == "backup"
        primary = selector.get_moe_current_model("0")
        assert primary["model"] == "backup"
        attempted = set()
        fallback = selector.get_rate_limit_fallback(primary, attempted)
        assert fallback["model"] == "model-a"
        assert fallback["billing_type"] == "paid"
        assert selector.get_rate_limit_fallback(fallback, attempted) is None
        assert not selector.is_model_exhausted("backup (other)")
        assert selector.get_moe_current_model("1")["model"] == "third"


@pytest.mark.asyncio
async def test_exhausted_free_model_is_skipped_in_all_shared_roles(selector):
    with request_budget_scope(authorized=False):
        await selector.mark_model_exhausted(selector.get_model("category_model"), 403)
        assert selector.get_moe_current_model("0")["model"] == "model-a"
        assert selector.get_model("category_model")["model"] == "model-a"


def test_preferred_free_model_still_needs_requested_capabilities(selector):
    selector.models["backup (other)"]["no_tools"] = True
    with request_budget_scope(authorized=False):
        assert selector.get_moe_current_model("0", has_tools=True)["model"] == "model-a"
        selector.models["model-a (good)"]["is_vision"] = True
        assert selector.get_model("category_model", has_images=True)["model"] == "model-a"


def test_ordinary_users_retain_search_and_fixed_media_routes(selector):
    selector.model_config["use_web_search"] = True
    selector.model_config["multimodal_model"] = "model-a (good)"
    with request_budget_scope(authorized=False):
        assert selector.get_web_search() is True
        assert selector.get_model("multimodal_model")["billing_type"] == "paid"


@pytest.mark.asyncio
@pytest.mark.parametrize("billing_type", ["free", "paid"])
async def test_ordinary_http_requests_allow_both_billing_types_and_keep_budget(monkeypatch, billing_type):
    monkeypatch.setattr(usage_ledger, "get_ledger", lambda: SimpleNamespace(budgets={}, exhausted=lambda _: False))
    monkeypatch.setattr(usage_ledger, "record_usage", AsyncMock())
    seen = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def post(**kwargs):
        seen.append(kwargs["json"])
        return Response()

    model = {"provider": "test", "model": "available", "billing_type": billing_type}
    with request_budget_scope(authorized=False):
        async with usage_ledger.budgeted_post(SimpleNamespace(post=post), model, json={"messages": [], "max_tokens": 16384}):
            pass
    assert seen[0]["max_tokens"] == 4096


@pytest.mark.parametrize("name", ["glm-4-flash-250414", "glm-4.7-flash", "glm-4.6v-flash"])
def test_free_glm_keeps_its_own_thinking_parameters(name):
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

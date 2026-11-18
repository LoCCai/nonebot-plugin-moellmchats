import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_model_selector import _tier_selector

from nonebot_plugin_moellmchats import model_routing_runtime, personal_billing
from nonebot_plugin_moellmchats.model_capabilities import ModelCapability
from nonebot_plugin_moellmchats.request_budget import request_budget_scope


@pytest.fixture
def adapter(monkeypatch):
    payer = ContextVar("test_payer", default=None)

    @asynccontextmanager
    async def scope(bot, event):
        token = payer.set(event)
        try:
            yield
        finally:
            payer.reset(token)

    host = SimpleNamespace(
        active=lambda: payer.get() is not None,
        snapshot=lambda: payer.get(),
        event_scope=scope,
        model=Mock(side_effect=lambda key, **kwargs: {"model": key, "payer": payer.get(), **kwargs}),
        config=lambda key, default: {"use_moe": True}.get(key, default),
    )
    monkeypatch.setattr(personal_billing, "_adapter", host)
    return host


@pytest.mark.asyncio
async def test_no_adapter_preserves_public_routing_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(personal_billing, "_adapter", None)
    selector, _, _ = _tier_selector(tmp_path, monkeypatch)
    async with personal_billing.event_scope(None, None), personal_billing.recovery_scope({}):
        assert not personal_billing.active()
        assert personal_billing.snapshot() is None
        assert selector.get_model("selected_model")["provider"] == "good"


@pytest.mark.asyncio
async def test_saved_personal_payer_cannot_recover_without_adapter(monkeypatch):
    monkeypatch.setattr(personal_billing, "_adapter", None)
    reached = False
    with pytest.raises(RuntimeError, match="saved payer cannot be changed"):
        async with personal_billing.recovery_scope({"billing_payer": {"user_id": "test-user"}}):
            reached = True
    assert not reached


@pytest.mark.asyncio
async def test_personal_routing_precedes_public_authorization_and_never_fails_over(adapter, tmp_path, monkeypatch):
    selector, _, _ = _tier_selector(tmp_path, monkeypatch)
    async with personal_billing.event_scope(None, "alice"):
        with request_budget_scope(authorized=True):
            monkeypatch.setattr(
                model_routing_runtime,
                "build_model_routing_runtime",
                Mock(side_effect=AssertionError("personal requests must not enter public capability routing")),
            )
            assert selector.get_moe()
            assert selector.get_model("selected_model")["payer"] == "alice"
            capabilities = ModelCapability(text=True, tools=True, vision=True, json_schema=False, reasoning=False, streaming=True)
            model = selector.get_model_for_capabilities("selected_model", capabilities)
            assert model["has_tools"]
            assert model["has_images"]
            for tier in ("0", "1", "2"):
                assert selector.get_moe_current_model(tier)["model"] == "moe_" + tier
                routed = selector.get_moe_current_model_for_capabilities(tier, capabilities)
                assert routed["model"] == "moe_" + tier
                assert routed["has_tools"]
                assert routed["has_images"]
            assert selector.get_rate_limit_fallback(model, set()) is None
            adapter.model.side_effect = RuntimeError("personal route unavailable")
            with pytest.raises(RuntimeError, match="personal route unavailable"):
                selector.get_model("selected_model")
    assert not personal_billing.active()
    assert selector.get_model("selected_model")["provider"] == "good"


@pytest.mark.asyncio
async def test_concurrent_tool_scopes_keep_their_payers_and_restore_on_error(adapter):
    @personal_billing.billable_tool
    async def tool(*, _tool_context, fail=False):
        await asyncio.sleep(0)
        assert personal_billing.snapshot() == _tool_context.event
        if fail:
            raise ValueError("tool failed")
        return personal_billing.snapshot()

    result = await asyncio.gather(
        tool(_tool_context=SimpleNamespace(bot=None, event="alice")),
        tool(_tool_context=SimpleNamespace(bot=None, event="bob"), fail=True),
        return_exceptions=True,
    )
    assert result[0] == "alice"
    assert isinstance(result[1], ValueError)
    assert not personal_billing.active()

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nonebot_plugin_moellmchats import request_budget as policy, usage_ledger
from nonebot_plugin_moellmchats.moe_llm import MoeLlm
from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
from nonebot_plugin_moellmchats.usage_ledger import budgeted_post


GLM = {"model": "glm-5.3-flash", "provider": "bigmodel"}


@pytest.fixture
def no_accounting_io(monkeypatch):
    ledger = SimpleNamespace(budgets={}, exhausted=lambda _: False)
    monkeypatch.setattr(usage_ledger, "get_ledger", lambda: ledger)
    monkeypatch.setattr(usage_ledger, "record_usage", AsyncMock())


class Response:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
async def test_concurrent_qq_budgets_do_not_leak_to_models_or_other_users(no_accounting_io, encoded):
    original_model = deepcopy(GLM)
    original_payload = {"messages": [{"role": "user", "content": "test"}],
                        "max_tokens": 16384, "stream": True, "reasoning_effort": "max"}
    observed = {}

    async def call(index):
        def post(**kwargs):
            observed[index] = json.loads(kwargs["data"]) if encoded else kwargs["json"]
            return Response()

        with policy.request_budget_scope(authorized=index % 2 == 0):
            await asyncio.sleep(0)
            data = {"data": json.dumps(original_payload)} if encoded else {"json": original_payload}
            async with budgeted_post(SimpleNamespace(post=post), GLM, **data):
                pass

    await asyncio.gather(*(call(index) for index in range(16)))
    assert GLM == original_model
    assert original_payload["max_tokens"] == 16384
    for index, payload in observed.items():
        assert payload["max_tokens"] == (16384 if index % 2 == 0 else 4096)
        assert payload["reasoning_effort"] == ("max" if index % 2 == 0 else "low")
        assert payload["thinking"] == {"type": "enabled", "clear_thinking": False}
        assert payload["tool_stream"] is True


@pytest.mark.asyncio
async def test_classification_is_small_then_chat_restores_its_own_budget(no_accounting_io):
    observed = []

    def post(**kwargs):
        observed.append(kwargs["json"])
        return Response()

    payload = {"messages": [], "max_tokens": 512}
    with policy.request_budget_scope(authorized=True):
        with policy.classification_budget_scope():
            async with budgeted_post(SimpleNamespace(post=post), GLM, json=payload):
                pass
        async with budgeted_post(SimpleNamespace(post=post), GLM, json=payload):
            pass
    assert [p["max_tokens"] for p in observed] == [2048, 8192]
    assert [p["reasoning_effort"] for p in observed] == ["low", "max"]


def test_request_caps_cover_tools_history_retries_and_model_wait():
    values = {"request_timeout_seconds": 480, "max_tool_rounds": 12, "max_agent_steps": 12,
              "max_tool_calls_per_round": 4, "max_retry_times": 3,
              "max_history_tokens": 4000, "max_history_chars": 16000,
              "max_user_history": 8, "classification_timeout_seconds": 20}
    with policy.request_budget_scope(authorized=False):
        result = {k: policy.budget_limit(k, v) for k, v in values.items()}
        assert result == dict(request_timeout_seconds=180, max_tool_rounds=4, max_agent_steps=4,
                              max_tool_calls_per_round=2, max_retry_times=2,
                              max_history_tokens=2000, max_history_chars=6000,
                              max_user_history=4, classification_timeout_seconds=8)
        assert policy.budget_limit("max_tool_rounds", 2) == 2
        chat = object.__new__(MoeLlm)
        chat._remaining_request_seconds = lambda: 180
        assert chat._llm_request_timeout().total == 45
    with policy.request_budget_scope(authorized=True):
        assert {k: policy.budget_limit(k, v) for k, v in values.items()} == {
            **values, 'request_timeout_seconds': 3600, 'max_tool_rounds': 9, 'max_agent_steps': 9}
        chat._remaining_request_seconds = lambda: 3600
        assert chat._llm_request_timeout().total == 360


def test_plain_models_keep_supported_parameters_and_completion_limit_field():
    model = {"token_limit_field": "max_completion_tokens"}
    with policy.request_budget_scope(authorized=False):
        payload = {"messages": []}
        policy.apply_payload_budget(model, payload)
        assert payload == {"messages": [], "max_completion_tokens": 4096}
        payload = {"max_completion_tokens": 1200, "reasoning_effort": "high"}
        policy.apply_payload_budget(model, payload)
        assert payload == {"max_completion_tokens": 1200, "reasoning_effort": "low"}


@pytest.mark.asyncio
async def test_host_manual_grants_are_bound_to_real_caller_and_override_group_roles(monkeypatch):
    async def allowed(_bot, event):
        return event.get_user_id() == "90001"

    monkeypatch.setattr(policy, "_authorizer", allowed)
    bot = SimpleNamespace(config=SimpleNamespace(superusers={"90002"}))
    for qq, expected in [("90001", True), ("90002", False), ("90003", False)]:
        event = SimpleNamespace(get_user_id=lambda: qq, sender={"role": "owner"})
        assert await policy.budget_authorized(bot, event) is expected
    monkeypatch.setattr(policy, "_authorizer", AsyncMock(side_effect=RuntimeError("PRIVATE")))
    assert not await policy.budget_authorized(bot, event)


@pytest.mark.asyncio
async def test_authorizer_cancellation_is_propagated(monkeypatch):
    monkeypatch.setattr(policy, "_authorizer", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await policy.budget_authorized(object(), object())


@pytest.mark.asyncio
async def test_parallel_graph_cannot_bypass_ordinary_tool_budget(monkeypatch):
    harness = LlmToolsMixin()
    prepare = AsyncMock(side_effect=AssertionError("over-budget graph must not start"))
    monkeypatch.setattr(harness, "_prepare_read_only_parallel_batch", prepare)
    with policy.request_budget_scope(authorized=False):
        assert await harness._try_execute_read_only_parallel_tools([{}, {}, {}], "", [], "") is None
    prepare.assert_not_called()

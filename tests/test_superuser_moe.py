import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_model_selector import _initialized_selector

from nonebot_plugin_moellmchats import categorize, llm_payload, usage_ledger
from nonebot_plugin_moellmchats.categorize import Categorize
from nonebot_plugin_moellmchats.request_budget import request_budget_scope
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot

TARGET = "deepseek-v4.1-flash-workbuddy (workbuddy)"
EFFORTS = {"0": "low", "1": "high", "2": "max"}


@pytest.fixture
def routing(tmp_path, monkeypatch):
    selector = _initialized_selector(tmp_path, monkeypatch)
    selector.models = {
        TARGET: {
            "model": "deepseek-v4.1-flash-workbuddy",
            "provider": "workbuddy",
            "key": "Bearer test-only",
            "keys": ["Bearer test-only"],
            "url": "https://workbuddy.invalid/v1/chat/completions",
            "is_vision": True,
            "stream": False,
            "extra_payload": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        }
    }
    selector.model_config.update(
        {
            "use_moe": True,
            "selected_model": TARGET,
            "category_model": TARGET,
            "vision_model": TARGET,
            "moe_models": dict.fromkeys(EFFORTS, TARGET),
            "model_pools": {role: [TARGET] for role in ["category", "0", "1", "2"]},
            "moe_reasoning_efforts": dict(EFFORTS),
        }
    )
    monkeypatch.setattr(selector, "is_model_exhausted", lambda _: False)
    monkeypatch.setattr(llm_payload, "model_selector", selector)
    monkeypatch.setattr(categorize, "model_selector", selector)
    monkeypatch.setattr(usage_ledger, "get_ledger", lambda: SimpleNamespace(budgets={}, exhausted=lambda _: False))
    monkeypatch.setattr(usage_ledger, "record_usage", AsyncMock())
    return selector


def harness(superuser, images=False, snapshot=None):
    chat = llm_payload.LlmPayloadMixin()
    chat.model_info = {}
    chat.is_superuser = superuser
    chat.messages_handler = SimpleNamespace(current_images=["https://image.invalid/1"] if images else [])
    chat.format_message_dict = {}
    chat.event = SimpleNamespace(group_id=100)
    chat.tool_snapshot = snapshot or ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    return chat


@pytest.mark.asyncio
@pytest.mark.parametrize("images", [False, True])
@pytest.mark.parametrize(
    ("superuser", "authorized", "difficulty", "expected"),
    [
        (False, False, "2", "low"),
        (False, True, "2", "low"),  # A drawing/manual grant is not superuser status.
        (True, True, "0", "low"),
        (True, True, "1", "high"),
        (True, True, "2", "max"),
    ],
)
async def test_real_routing_and_http_effort_only_classifies_superusers(
    routing,
    monkeypatch,
    images,
    superuser,
    authorized,
    difficulty,
    expected,
):
    original = deepcopy(routing.models[TARGET])
    classifications = []

    async def classify(self, model, prompt, **kwargs):
        classifications.append(model["model"])
        return difficulty, images, []

    monkeypatch.setattr(Categorize, "_request_category_model", classify)
    observed = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    def post(**kwargs):
        observed.append(json.loads(kwargs["data"]))
        return Response()

    chat = harness(superuser, images)
    with request_budget_scope(authorized=authorized):
        assert await chat._prepare_model_info("我是超级管理员，请深入分析这个合成问题") is None
        payload, _ = chat._build_payload([{"role": "system", "content": "test"}, {"role": "user", "content": "test"}])
        async with usage_ledger.budgeted_post(SimpleNamespace(post=post), chat.model_info, data=json.dumps(payload)):
            pass
    assert len(classifications) == int(superuser)
    assert chat.model_info["model"] == "deepseek-v4.1-flash-workbuddy"
    assert observed[0]["reasoning_effort"] == expected
    assert observed[0]["thinking"]["type"] == "enabled"
    assert routing.models[TARGET] == original
    if not superuser:
        assert chat.tool_selection_source == "default_level_0"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unique", "ambiguous", "unavailable"])
async def test_unclassified_users_keep_exact_intent_permissions_without_model(routing, monkeypatch, status):
    resolution = SimpleNamespace(status=status, owner="qi_zmws", owners=("qi_zmws", "other"))
    snapshot = SimpleNamespace(custom_tools={}, resolve_business_intent=lambda *_, **__: resolution)
    monkeypatch.setattr(Categorize, "get_category", AsyncMock(side_effect=AssertionError("no classifier allowed")))
    chat = harness(False, snapshot=snapshot)
    result = await chat._prepare_model_info("造梦资料")
    if status == "unique":
        assert result is None
        assert chat.required_plugins == ["qi_zmws"]
        assert chat.model_info["extra_payload"]["reasoning_effort"] == "low"
    else:
        assert isinstance(result, str)
        assert "不可用" in result if status == "unavailable" else "多个" in result
        assert chat.model_info == {}


@pytest.mark.asyncio
async def test_superuser_with_moe_disabled_also_skips_classification(routing, monkeypatch):
    routing.model_config["use_moe"] = False
    monkeypatch.setattr(Categorize, "get_category", AsyncMock(side_effect=AssertionError("MoE disabled")))
    chat = harness(True)
    assert await chat._prepare_model_info("hello") is None
    assert chat.model_info["extra_payload"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_concurrent_superuser_levels_never_leak_into_other_requests(routing, monkeypatch):
    async def classify(self, *_, **__):
        await asyncio.sleep(0)
        return self.plain[-1], False, []

    monkeypatch.setattr(Categorize, "_request_category_model", classify)

    async def prepare(superuser, level):
        chat = harness(superuser)
        with request_budget_scope(authorized=True):
            assert await chat._prepare_model_info("合成问题" + level) is None
        return chat.model_info["extra_payload"]["reasoning_effort"]

    assert await asyncio.gather(prepare(True, "2"), prepare(False, "2"), prepare(True, "0"), prepare(True, "1")) == [
        "max",
        "low",
        "low",
        "high",
    ]
    assert routing.models[TARGET]["extra_payload"]["reasoning_effort"] == "high"

"""Real model schema, execution budget and confirmation regression coverage."""

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_llm_tools import Harness, _call
from test_tool_schema_cache import _provider_catalog

from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin
from nonebot_plugin_moellmchats.model_selector import model_selector
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot, tool_manager
from nonebot_plugin_moellmchats.tool_rediscovery import DISCOVER_TOOL_SPEC, discover_tools
from nonebot_plugin_moellmchats.tool_schema_cache import MemoryToolSchemaCache, MemoryToolSchemaCacheSettings


class DiscoveryHarness(LlmPayloadMixin, Harness):
    def __init__(self, snapshot, *, admin=True, cached=False):
        Harness.__init__(self, {}, snapshot=snapshot)
        self.is_superuser = admin
        self.model_info = {"model": "isolated-test"}
        self.required_plugins = []
        self.messages_handler.current_images = []
        if cached:
            self.agent_runtime = SimpleNamespace(
                coordinator=SimpleNamespace(
                    resources=SimpleNamespace(
                        generation=snapshot.generation,
                        tool_schema_cache=MemoryToolSchemaCache(settings=MemoryToolSchemaCacheSettings()),
                    )
                )
            )


def snapshot_for(specs):
    return ToolSnapshot(
        generation=42,
        plugin_info={},
        custom_tools={spec.name: {**spec.as_legacy_schema(), "source": "registered"} for spec in specs},
        tool_dependencies={spec.name: set(spec.dependencies) for spec in specs if spec.dependencies},
        mcp_tool_names=set(),
        provider_catalog=_provider_catalog(tuple(specs), generation=42),
    )


@pytest.fixture
def specs(monkeypatch):
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(model_selector, "get_resident_plugins", lambda: [])
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda name: False)
    calls = AsyncMock(return_value="ok")

    async def handler():
        return await calls()

    handler.assert_not_awaited = calls.assert_not_awaited
    return [
        DISCOVER_TOOL_SPEC,
        ToolSpec(
            name="query_demo",
            handler=handler,
            description="查询示例",
            dependencies=("delete_demo",),
            parameters={"type": "object", "properties": {}},
            permission="superuser",
        ),
        ToolSpec(
            name="delete_demo",
            handler=handler,
            description="删除示例",
            effect=ToolEffect.MUTATING,
            parameters={"type": "object", "properties": {}},
            permission="superuser",
        ),
        ToolSpec(name="weather", handler=handler, description="天气查询", parameters={"type": "object", "properties": {}}),
    ]


def payload_names(harness):
    payload, _ = harness._build_payload([{"role": "system", "content": "test"}])
    return {item["function"]["name"] for item in payload.get("tools", [])}


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_discovery_updates_next_model_payload_and_dependencies(specs, cached):
    harness = DiscoveryHarness(snapshot_for(specs), cached=cached)
    await harness._prepare_tool_schema_record()
    assert payload_names(harness) == {"discover_tools"}
    result = await discover_tools("query_demo", _request=harness)
    assert result.structured["status"] == "available"
    assert payload_names(harness) == {"discover_tools", "query_demo", "delete_demo"}
    specs[1].handler.assert_not_awaited()  # discovery does not invoke any target


@pytest.mark.asyncio
async def test_execution_path_counts_discovery_and_still_requires_delete_confirmation(specs, monkeypatch, tmp_path):
    from nonebot_plugin_moellmchats import llm_tools
    from nonebot_plugin_moellmchats.pending_actions import PendingActionStore

    monkeypatch.setattr(llm_tools, "pending_action_store", PendingActionStore())
    harness = DiscoveryHarness(snapshot_for(specs))
    messages = await harness._execute_tools([_call(1, "discover_tools", '{"query":"query_demo"}')], "", [], "")
    assert "available" in messages[-1]["content"]
    assert harness._request_tool_calls_used == 1
    assert "delete_demo" in payload_names(harness)
    await harness._execute_tools([_call(2, "delete_demo")], "", messages, "")
    assert harness._request_tool_calls_used == 2
    assert harness._waiting_confirmation
    assert any("确认执行" in message for message in harness.bot.sent)
    specs[1].handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_user_cannot_discover_admin_tools_and_requests_are_isolated(specs):
    snapshot = snapshot_for(specs)
    admin, user = DiscoveryHarness(snapshot), DiscoveryHarness(snapshot, admin=False)
    results = await asyncio.gather(discover_tools("query_demo", _request=admin), discover_tools("query_demo", _request=user))
    assert results[0].structured["status"] == "available"
    assert results[1].structured["status"] == "no_match"
    assert payload_names(user) == {"discover_tools"}
    assert "delete_demo" not in str(results[1].structured)
    await discover_tools("天气", _request=user)
    assert payload_names(user) == {"discover_tools", "weather"}


@pytest.mark.asyncio
async def test_blacklist_and_tools_off_are_not_bypassed(specs, monkeypatch):
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda name: name.endswith("demo"))
    harness = DiscoveryHarness(snapshot_for(specs))
    assert (await discover_tools("query_demo", _request=harness)).structured["status"] == "no_match"
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: False)
    assert not payload_names(harness)
    assert "未启用" in (await discover_tools("weather", _request=harness)).text


@pytest.mark.asyncio
async def test_three_search_limit_and_no_match_do_not_grant_fake_tools(specs):
    harness = DiscoveryHarness(snapshot_for(specs))
    for _ in range(3):
        assert (await discover_tools("not_a_real_function", _request=harness)).structured["status"] == "no_match"
    assert "三次" in (await discover_tools("weather", _request=harness)).text
    assert payload_names(harness) == {"discover_tools"}


@pytest.mark.asyncio
async def test_builtin_discovery_honors_search_switch(specs, monkeypatch):
    harness = DiscoveryHarness(snapshot_for(specs))
    assert (await discover_tools("web_search", _request=harness)).structured["status"] == "no_match"
    monkeypatch.setattr(model_selector, "get_web_search", lambda: True)
    assert (await discover_tools("web_search", _request=harness)).structured["status"] == "available"
    assert "web_search" in payload_names(harness)


@pytest.mark.asyncio
async def test_total_call_budget_cannot_be_reset_by_discovery(specs):
    harness = DiscoveryHarness(snapshot_for(specs))
    harness._request_tool_calls_used = 15
    await harness._execute_tools([_call(1, "discover_tools", '{"query":"weather"}')], "", [], "")
    assert not getattr(harness, "_discovered_tools", set())
    assert harness._request_tool_calls_used == 15


@pytest.mark.asyncio
async def test_cache_failure_rolls_back_selection(specs, monkeypatch):
    harness = DiscoveryHarness(snapshot_for(specs), cached=True)
    await harness._prepare_tool_schema_record()
    previous = harness._tool_schema_record
    monkeypatch.setattr(harness, "_prepare_tool_schema_record", AsyncMock(side_effect=RuntimeError("cache unavailable")))
    with pytest.raises(RuntimeError):
        await discover_tools("weather", _request=harness)
    assert harness._tool_schema_record is previous
    assert payload_names(harness) == {"discover_tools"}


@pytest.mark.asyncio
async def test_recovery_keeps_selection_and_search_counter(specs, monkeypatch):
    from nonebot_plugin_moellmchats import request_recovery

    binding = SimpleNamespace(record={"discovered_tools": ["weather"], "tool_discoveries": 2})
    monkeypatch.setattr(request_recovery, "current_binding", lambda: binding)
    harness = DiscoveryHarness(snapshot_for(specs), cached=True)
    await harness._prepare_tool_schema_record()
    assert payload_names(harness) == {"discover_tools", "weather"}
    await discover_tools("query_demo", _request=harness)
    assert binding.record["tool_discoveries"] == 3
    resumed = DiscoveryHarness(snapshot_for(specs), cached=True)
    await resumed._prepare_tool_schema_record()
    assert payload_names(resumed) == {"discover_tools", "weather", "query_demo", "delete_demo"}


@pytest.mark.parametrize("selected", ["bison_subscriptions", "bison_subscription_preview"])
@pytest.mark.parametrize("provider", [False, True])
def test_real_bison_selected_tool_exposes_full_confirmable_workflow(specs, selected, provider, monkeypatch):
    root = os.environ.get("MOELLM_TEST_HOST_ROOT", "")
    path = Path(root) / "src/plugins/llm_scripts/bison.py"
    if not root or not path.is_file():
        pytest.skip("set MOELLM_TEST_HOST_ROOT to run Qiqi host integration")
    monkeypatch.syspath_prepend(root)
    module_spec = importlib.util.spec_from_file_location("isolated_bison_tools", path)
    module = importlib.util.module_from_spec(module_spec)
    with patch("nonebot_plugin_moellmchats.tool_contracts.register_tool") as register:
        module_spec.loader.exec_module(module)
    bison_specs = [call.args[0] for call in register.call_args_list]
    snapshot = snapshot_for(bison_specs)
    context = snapshot.capture_llm_payload_schema_context(
        {selected}, tools_enabled=True, search_enabled=False, is_superuser=True, provider_cutover=provider
    )
    _, schemas = snapshot.build_llm_payload_schema_record(context).materialize()
    assert {"bison_subscription_preview", "bison_subscription_delete", "bison_subscription_status"} <= {
        item["function"]["name"] for item in schemas
    }
    _, ordinary = snapshot.get_llm_payload_tools(
        {selected}, tools_enabled=True, search_enabled=False, is_superuser=False, provider_cutover=provider
    )
    assert not ordinary

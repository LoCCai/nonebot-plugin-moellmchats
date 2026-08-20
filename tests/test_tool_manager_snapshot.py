from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.runtime_metrics import runtime_metrics
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots
from nonebot_plugin_moellmchats.tool_contracts import ToolSpec
from nonebot_plugin_moellmchats.tool_manager import (
    ToolSnapshot,
    model_selector,
    tool_manager,
)


async def _handler(value: str) -> str:
    return value


def test_tool_snapshot_generated_stamp_is_detached_and_immutable() -> None:
    active = {"weather": "a" * 64}
    snapshot = ToolSnapshot(
        generation=3,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        generated_state_revision=8,
        generated_state_digest="b" * 64,
        generated_active=active,
    )
    active["weather"] = "c" * 64

    assert snapshot.generated_state_revision == 8
    assert snapshot.generated_state_digest == "b" * 64
    assert snapshot.generated_active == {"weather": "a" * 64}
    with pytest.raises(TypeError):
        snapshot.generated_active["weather"] = "d" * 64


def test_legacy_tool_snapshot_constructor_gets_empty_generated_stamp() -> None:
    snapshot = ToolSnapshot(
        generation=0,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )

    assert snapshot.generated_state_revision == 0
    assert snapshot.generated_state_digest == ""
    assert snapshot.generated_active == {}


def test_load_custom_tools_forwards_generated_state_and_source_overrides(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    generated_state = object()
    source_overrides = {("weather", "a" * 64): b"source"}
    received: dict = {}

    def load_files(_files, *, generation: int):
        assert generation == 23
        return {}, {}

    def load_generated(
        *,
        generation: int,
        generated_state,
        generated_source_overrides,
    ):
        received.update(
            generation=generation,
            generated_state=generated_state,
            generated_source_overrides=generated_source_overrides,
        )
        return {}, {}

    monkeypatch.setattr(manager_module.tool_registry, "snapshot", lambda: {})
    monkeypatch.setattr(manager_module, "load_file_tools", load_files)
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        load_generated,
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tools, dependencies = tool_manager.load_custom_tools(
        commit=False,
        generation=23,
        generated_state=generated_state,
        generated_source_overrides=source_overrides,
    )

    assert tools == {}
    assert dependencies == {}
    assert received["generation"] == 23
    assert received["generated_state"] is generated_state
    assert received["generated_source_overrides"] is source_overrides


def test_load_custom_tools_defaults_remain_compatible_with_legacy_store(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    calls: list[int] = []

    def load_files(_files, *, generation: int):
        return {}, {}

    def load_generated(*, generation: int):
        calls.append(generation)
        return {}, {}

    monkeypatch.setattr(manager_module.tool_registry, "snapshot", lambda: {})
    monkeypatch.setattr(manager_module, "load_file_tools", load_files)
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        load_generated,
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tool_manager.load_custom_tools(commit=False, generation=11)

    assert calls == [11]


def test_tool_manager_snapshot_returns_active_runtime_snapshot(monkeypatch) -> None:
    authoritative = object()
    monkeypatch.setattr(
        runtime_snapshots,
        "active",
        lambda: SimpleNamespace(tool_snapshot=authoritative),
    )

    assert tool_manager.snapshot() is authoritative


def test_tool_manager_snapshot_bootstrap_falls_back_to_detached_mirrors(
    monkeypatch,
) -> None:
    plugins = {"plugin": {"description": "old"}}
    custom_tools = {"tool": {"description": "old"}}
    dependencies = {"tool": {"plugin"}}
    mcp_names = {"tool"}
    monkeypatch.setattr(runtime_snapshots, "active", lambda: None)
    monkeypatch.setattr(runtime_metrics, "reload_generation", 17)
    monkeypatch.setattr(tool_manager, "plugin_info", plugins)
    monkeypatch.setattr(tool_manager, "custom_tools", custom_tools)
    monkeypatch.setattr(tool_manager, "tool_dependencies", dependencies)
    monkeypatch.setattr(tool_manager, "mcp_tool_names", mcp_names)

    snapshot = tool_manager.snapshot()
    plugins["plugin"]["description"] = "changed"
    custom_tools["other"] = {"description": "changed"}
    dependencies["tool"].add("other")
    mcp_names.add("other")

    assert snapshot.generation == 17
    assert snapshot.plugin_info["plugin"]["description"] == "old"
    assert snapshot.custom_tools == {"tool": {"description": "old"}}
    assert snapshot.tool_dependencies == {"tool": {"plugin"}}
    assert snapshot.mcp_tool_names == {"tool"}
    with pytest.raises(TypeError):
        snapshot.custom_tools["tool"]["description"] = "tampered"


def test_real_frozen_snapshot_filters_permissions_and_thaws_model_schema(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="admin_only",
        description="admin only",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=_handler,
        permission="superuser",
    )
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={"admin_only": spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)

    assert not isinstance(snapshot.custom_tools["admin_only"], dict)
    assert "admin_only" not in snapshot.get_brief_catalog(is_superuser=False)
    assert snapshot.get_tool_schema(["admin_only"], is_superuser=False) == []
    assert "admin_only" in snapshot.get_brief_catalog(is_superuser=True)

    schema = snapshot.get_tool_schema(["admin_only"], is_superuser=True)
    json.dumps(schema)
    parameters = schema[0]["function"]["parameters"]
    assert isinstance(parameters, dict)
    assert isinstance(parameters["properties"], dict)
    parameters["properties"]["value"]["type"] = "integer"
    assert (
        snapshot.custom_tools["admin_only"]["parameters"]["properties"]
        ["value"]["type"]
        == "string"
    )


def test_permission_filter_fails_closed_for_malformed_entries() -> None:
    assert not tool_manager.is_tool_allowed(
        object(), is_superuser=False  # type: ignore[arg-type]
    )
    assert not tool_manager.is_tool_allowed(
        {"tool_spec": object()}, is_superuser=False
    )

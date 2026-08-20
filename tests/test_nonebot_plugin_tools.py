from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nonebot_plugin_moellmchats import nonebot_plugin_tools as module
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolResult
from nonebot_plugin_moellmchats.tool_manager import ToolManager, tool_manager


def test_nonebot_plugin_candidate_is_detached_canonical_and_conservative() -> None:
    dependencies = ["helper_zeta", "helper_alpha", "helper_alpha"]
    raw = {
        "plugin_demo": {
            "name": "Demo",
            "description": "legacy demo plugin",
            "usage": "/demo <value>",
            "dependencies": dependencies,
        }
    }

    legacy, specs = build_nonebot_plugin_candidate(raw)
    spec = specs[0]
    dependencies.append("late_mutation")
    raw["plugin_demo"]["description"] = "drifted"

    assert tuple(legacy) == ("plugin_demo",)
    assert legacy["plugin_demo"]["description"] == "legacy demo plugin"
    assert legacy["plugin_demo"]["dependencies"] == [
        "helper_zeta",
        "helper_alpha",
        "helper_alpha",
    ]
    assert legacy["plugin_demo"]["source"] == "nonebot_plugin"
    assert legacy["plugin_demo"]["tool_spec"] is spec
    assert spec.name == "plugin_demo"
    assert spec.permission == "user"
    assert spec.effect is ToolEffect.MUTATING
    assert spec.dependencies == ("helper_alpha", "helper_zeta")
    assert spec.parameters["required"] == ("command",)
    assert "Demo" in spec.description
    assert "/demo <value>" in spec.description
    with pytest.raises(TypeError):
        spec.parameters["required"] = ()
    with pytest.raises(FrozenInstanceError):
        spec.permission = "superuser"


def test_nonebot_plugin_candidate_rejects_malformed_or_reserved_legacy_info() -> None:
    with pytest.raises(TypeError, match="plugin_info"):
        build_nonebot_plugin_candidate([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="描述"):
        build_nonebot_plugin_candidate({"plugin_demo": []})
    with pytest.raises(ValueError, match="保留 Provider 字段"):
        build_nonebot_plugin_candidate(
            {"plugin_demo": {"source": "spoofed"}}
        )
    with pytest.raises(ValueError, match="工具名"):
        build_nonebot_plugin_candidate(
            {"plugin.name": {"description": "invalid function name"}}
        )


@pytest.mark.asyncio
async def test_nonebot_plugin_adapter_uses_bounded_event_simulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = object()
    event = object()
    calls = []

    async def dispatch(
        received_bot,
        received_event,
        command,
        source,
        *,
        plugin_name,
    ):
        calls.append(
            (received_bot, received_event, command, source, plugin_name)
        )
        return "visible plugin output", ["image:one"]

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)

    _legacy, specs = build_nonebot_plugin_candidate(
        {"plugin_demo": {"description": "demo"}}
    )
    result = await specs[0].handler(
        "/demo",
        _bot=bot,
        _event=event,
        _format_message_dict={"mentions": [{"qq": 1}]},
    )

    assert result == ToolResult(
        text="visible plugin output",
        images=("image:one",),
        metadata={
            "provider_id": "nonebot-plugin",
            "plugin_name": "plugin_demo",
            "compatibility_adapter": True,
        },
    )
    assert calls == [
        (
            bot,
            event,
            "/demo",
            {"mentions": [{"qq": 1}]},
            "plugin_demo",
        )
    ]


def test_nonebot_plugin_legacy_schema_uses_canonical_spec_without_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "name": "Demo",
                "description": "legacy demo plugin",
                "usage": "/demo",
            }
        }
    )
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda _name: False)

    schemas = ToolManager.build_tool_schema(
        ["plugin_demo"],
        plugin_info=legacy,
        custom_tools={},
    )

    assert schemas[0]["function"] == {
        "name": "plugin_demo",
        "description": specs[0].description,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "严格根据该插件的'原始用法说明'，"
                        "生成可以直接触发该插件的机器人指令字符串。"
                    ),
                }
            },
            "required": ["command"],
        },
    }
    schemas[0]["function"]["parameters"]["required"].clear()
    assert specs[0].parameters["required"] == ("command",)

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nonebot_plugin_moellmchats import nonebot_plugin_tools as module
from nonebot_plugin_moellmchats.event_simulator import (
    PluginDispatchResult,
    PluginDispatchStatus,
)
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    PluginDispatchError,
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolResult
from nonebot_plugin_moellmchats.tool_discovery import (
    COMPAT_COMMAND_PREFIXES_KEY,
)
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
    with pytest.raises(ValueError, match="保留 Provider 字段"):
        build_nonebot_plugin_candidate(
            {
                "plugin_demo": {
                    COMPAT_COMMAND_PREFIXES_KEY: ("!",),
                }
            }
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
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text="visible plugin output",
            images=("image:one",),
            matcher_checked=2,
            matcher_matched=1,
            successful_captures=1,
            api_succeeded=1,
            duration_ms=12,
        )

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
            "plugin_dispatch": {
                "status": "matched_with_output",
                "matcher_checked": 2,
                "matcher_matched": 1,
                "matcher_failed": 0,
                "matcher_blocked": 0,
                "successful_captures": 1,
                "api_succeeded": 1,
                    "api_failed": 0,
                    "api_unknown": 0,
                    "api_read_failed": 0,
                    "api_read_recovered": 0,
                    "api_unresolved_failed": 0,
                    "api_unresolved_unknown": 0,
                    "mutating_api_succeeded": 0,
                "duration_ms": 12,
            }
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
                        "严格根据该插件的原始用法说明和菜单功能提示，"
                        "生成可以直接触发该插件的机器人指令字符串。"
                    ),
                    "minLength": 1,
                    "maxLength": 1024,
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    }
    schemas[0]["function"]["parameters"]["required"].clear()
    assert specs[0].parameters["required"] == ("command",)


def test_nonebot_plugin_schema_keeps_generation_frozen_command_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "name": "Demo",
                "usage": "<命令前缀>demo",
            }
        },
        command_prefixes=("##", "!"),
    )
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda _name: False)

    schemas = ToolManager.build_tool_schema(
        ["plugin_demo"],
        plugin_info=legacy,
        custom_tools={},
    )

    description = schemas[0]["function"]["description"]
    assert description == specs[0].description
    assert "!demo" in description
    assert "当前首选命令前缀为 '!'" in description
    assert "其他有效前缀：'##'" in description
    assert "<命令前缀>" not in description


@pytest.mark.asyncio
async def test_nonebot_plugin_adapter_accepts_only_verified_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy, specs = build_nonebot_plugin_candidate(
        {"plugin_demo": {"description": "demo"}}
    )

    async def side_effect(*_args, **_kwargs):
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_SIDE_EFFECT,
            matcher_checked=1,
            matcher_matched=1,
            api_succeeded=1,
            mutating_api_succeeded=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", side_effect)
    result = await specs[0].handler(
        "/demo", _bot=object(), _event=object()
    )
    assert "副作用动作" in result.text
    assert result.metadata["plugin_dispatch"]["status"] == (
        "matched_side_effect"
    )

    async def empty(*_args, **_kwargs):
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_EMPTY,
            matcher_checked=1,
            matcher_matched=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", empty)
    with pytest.raises(PluginDispatchError) as captured:
        await specs[0].handler("/demo", _bot=object(), _event=object())
    assert captured.value.result.status is PluginDispatchStatus.MATCHED_EMPTY


@pytest.mark.parametrize(
    ("prefixes", "preferred", "alternatives"),
    [
        (("!", "/"), "/帮助", "'!'"),
        (("!!", "!"), "!帮助", "'!!'"),
        (("",), "帮助", None),
    ],
)
def test_nonebot_plugin_description_uses_real_command_start(
    prefixes: tuple[str, ...],
    preferred: str,
    alternatives: str | None,
) -> None:
    _legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "description": "demo",
                "usage": "<命令前缀>帮助",
            }
        },
        command_prefixes=prefixes,
    )
    description = specs[0].description
    assert preferred in description
    assert "<命令前缀>" not in description
    if alternatives is not None:
        assert alternatives in description

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .event_simulator import event_simulator
from .tool_contracts import ToolEffect, ToolResult, ToolSpec

_COMMAND_PARAMETERS = {
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
}


def _legacy_dependencies(info: Mapping[str, Any]) -> tuple[str, ...]:
    raw = info.get("dependencies") or info.get("tool_dependencies")
    if not raw:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(
        sorted(
            {
                item.strip()
                for item in raw
                if isinstance(item, str) and item.strip()
            }
        )
    )


async def execute_nonebot_plugin(
    command: str,
    *,
    plugin_name: str,
    bot: object,
    event: object,
    format_message_dict: Mapping[str, Any] | None = None,
) -> ToolResult:
    """Dispatch one transaction-selected legacy plugin through the bounded bus."""

    if not isinstance(command, str) or not command:
        raise ValueError("NoneBot 插件 command 必须是非空字符串")
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ValueError("NoneBot 插件标识不能为空")
    source = None if format_message_dict is None else dict(format_message_dict)
    text, images = await event_simulator.dispatch_event(
        bot,
        event,
        command,
        source,
        plugin_name=plugin_name,
    )
    return ToolResult(
        text=text,
        images=tuple(images),
        metadata={
            "provider_id": "nonebot-plugin",
            "plugin_name": plugin_name,
            "compatibility_adapter": True,
        },
    )


def _build_handler(plugin_name: str):
    async def handler(
        command: str,
        *,
        _bot: object,
        _event: object,
        _format_message_dict: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return await execute_nonebot_plugin(
            command,
            plugin_name=plugin_name,
            bot=_bot,
            event=_event,
            format_message_dict=_format_message_dict,
        )

    return handler


def build_nonebot_plugin_candidate(
    legacy_info: Mapping[str, object],
) -> tuple[dict[str, dict[str, Any]], tuple[ToolSpec, ...]]:
    """Bind one legacy plugin directory to exact compatibility ToolSpecs."""

    if not isinstance(legacy_info, Mapping):
        raise TypeError("NoneBot plugin_info 必须是映射")
    candidate: dict[str, dict[str, Any]] = {}
    specs: list[ToolSpec] = []
    for plugin_name in sorted(legacy_info):
        if not isinstance(plugin_name, str):
            raise TypeError("NoneBot 插件标识必须是字符串")
        raw = legacy_info[plugin_name]
        if not isinstance(raw, Mapping):
            raise TypeError(f"NoneBot 插件 {plugin_name} 描述必须是映射")
        info = deepcopy(dict(raw))
        if "tool_spec" in info or "source" in info:
            raise ValueError(
                f"NoneBot 插件 {plugin_name} 不得伪造保留 Provider 字段"
            )
        display_name = str(info.get("name") or plugin_name)
        description = str(info.get("description") or "无描述")
        usage = str(info.get("usage") or "无用法说明")
        spec = ToolSpec(
            name=plugin_name,
            description=(
                f"插件名称：{display_name}。"
                f"功能描述：{description}。"
                f"原始用法说明：{usage}"
            ),
            parameters=_COMMAND_PARAMETERS,
            handler=_build_handler(plugin_name),
            # A legacy command can reach arbitrary plugin behavior.  Keep the
            # existing user permission, but do not mislabel its effect as read-only.
            effect=ToolEffect.MUTATING,
            permission="user",
            dependencies=_legacy_dependencies(info),
        )
        info["tool_spec"] = spec
        info["source"] = "nonebot_plugin"
        candidate[plugin_name] = info
        specs.append(spec)
    return candidate, tuple(specs)

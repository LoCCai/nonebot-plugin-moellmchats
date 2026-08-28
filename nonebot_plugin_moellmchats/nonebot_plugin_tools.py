from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import nonebot

if TYPE_CHECKING:
    from nonebot.adapters import Bot

from .event_simulator import (
    PluginDispatchResult,
    PluginDispatchStatus,
    event_simulator,
)
from .tool_contracts import ToolEffect, ToolResult, ToolSpec
from .tool_discovery import (
    COMPAT_COMMAND_PREFIXES_KEY,
    build_compatibility_description,
)

_COMMAND_PARAMETERS = {
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
}


_DISPATCH_FEEDBACK = {
    PluginDispatchStatus.PARTIAL_SUCCESS: (
        "插件已产生部分可验证结果，但随后失败；不要再次调用同一工具。"
    ),
    PluginDispatchStatus.MATCHED_EMPTY: (
        "目标插件 Matcher 已命中，但没有产生可验证输出或副作用；"
        "不要重复调用相同工具和参数。"
    ),
    PluginDispatchStatus.NOT_MATCHED: (
        "目标插件没有 Matcher 命中该 command；不要重复调用相同工具和参数。"
    ),
    PluginDispatchStatus.FAILED: (
        "目标插件处理发生异常；不要重复调用相同工具和参数。"
    ),
    PluginDispatchStatus.TIMED_OUT: (
        "目标插件处理超时；不要重复调用相同工具和参数。"
    ),
    PluginDispatchStatus.ADMISSION_REJECTED: (
        "插件兼容执行队列已满，本次未执行。"
    ),
    PluginDispatchStatus.RESULT_UNKNOWN: (
        "插件调用后的外部结果不确定；绝对不要再次调用同一工具。"
    ),
}


class PluginDispatchError(RuntimeError):
    """Typed, bounded feedback for a non-success compatibility dispatch."""

    def __init__(self, result: PluginDispatchResult) -> None:
        if not isinstance(result, PluginDispatchResult) or result.succeeded:
            raise TypeError("PluginDispatchError 只接受非成功调度结果")
        self.result = result
        super().__init__(_DISPATCH_FEEDBACK[result.status])


def configured_command_prefixes() -> tuple[str, ...]:
    """Capture NoneBot's real command_start without guessing a placeholder."""

    raw: object
    try:
        raw = nonebot.get_driver().config.command_start
    except Exception:
        raw = {"/"}
    if isinstance(raw, str):
        raw = {raw}
    if not isinstance(raw, (set, frozenset, list, tuple)):
        raw = {"/"}
    prefixes = tuple(
        sorted(
            {item for item in raw if isinstance(item, str)},
            key=lambda item: (len(item), item),
        )
    )
    return prefixes or ("",)


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

    if not isinstance(command, str) or not 1 <= len(command) <= 1024:
        raise ValueError("NoneBot 插件 command 必须是 1～1024 字符字符串")
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ValueError("NoneBot 插件标识不能为空")
    source = None if format_message_dict is None else dict(format_message_dict)
    dispatch = await event_simulator.dispatch_event(
        cast("Bot", bot),
        event,
        command,
        source,
        plugin_name=plugin_name,
    )
    if not isinstance(dispatch, PluginDispatchResult):
        raise TypeError("NoneBot 兼容调度必须返回 PluginDispatchResult")
    if dispatch.status is PluginDispatchStatus.MATCHED_WITH_OUTPUT:
        return ToolResult(
            text=dispatch.text,
            images=dispatch.images,
            metadata={"plugin_dispatch": _dispatch_metadata(dispatch)},
        )
    if dispatch.status is PluginDispatchStatus.MATCHED_SIDE_EFFECT:
        return ToolResult(
            text="插件已成功执行一次由 Bot API 确认的副作用动作。",
            metadata={"plugin_dispatch": _dispatch_metadata(dispatch)},
        )
    raise PluginDispatchError(dispatch)


def _dispatch_metadata(result: PluginDispatchResult) -> dict[str, int | str]:
    return {
        "status": result.status.value,
        "matcher_checked": result.matcher_checked,
        "matcher_matched": result.matcher_matched,
        "matcher_failed": result.matcher_failed,
        "matcher_blocked": result.matcher_blocked,
        "successful_captures": result.successful_captures,
        "api_succeeded": result.api_succeeded,
        "api_failed": result.api_failed,
        "api_unknown": result.api_unknown,
        "mutating_api_succeeded": result.mutating_api_succeeded,
        "duration_ms": result.duration_ms,
    }


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
    *,
    command_prefixes: tuple[str, ...] | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[ToolSpec, ...]]:
    """Bind one legacy plugin directory to exact compatibility ToolSpecs."""

    if not isinstance(legacy_info, Mapping):
        raise TypeError("NoneBot plugin_info 必须是映射")
    candidate: dict[str, dict[str, Any]] = {}
    specs: list[ToolSpec] = []
    if command_prefixes is None:
        command_prefixes = configured_command_prefixes()
    if not isinstance(command_prefixes, tuple) or not all(
        isinstance(prefix, str) for prefix in command_prefixes
    ):
        raise TypeError("command_prefixes 必须是字符串元组")
    for plugin_name in sorted(legacy_info):
        if not isinstance(plugin_name, str):
            raise TypeError("NoneBot 插件标识必须是字符串")
        raw = legacy_info[plugin_name]
        if not isinstance(raw, Mapping):
            raise TypeError(f"NoneBot 插件 {plugin_name} 描述必须是映射")
        info = deepcopy(dict(raw))
        if any(
            key in info
            for key in (
                "tool_spec",
                "source",
                COMPAT_COMMAND_PREFIXES_KEY,
            )
        ):
            raise ValueError(
                f"NoneBot 插件 {plugin_name} 不得伪造保留 Provider 字段"
            )
        spec = ToolSpec(
            name=plugin_name,
            description=build_compatibility_description(
                plugin_name,
                info,
                command_prefixes=command_prefixes,
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
        info[COMPAT_COMMAND_PREFIXES_KEY] = command_prefixes
        candidate[plugin_name] = info
        specs.append(spec)
    return candidate, tuple(specs)

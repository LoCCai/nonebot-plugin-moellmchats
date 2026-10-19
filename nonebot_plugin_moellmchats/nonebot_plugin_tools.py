from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
import io
import json
from typing import TYPE_CHECKING, Any, cast

import nonebot

if TYPE_CHECKING:
    from nonebot.adapters import Bot

from .event_simulator import (
    PluginDispatchResult,
    PluginDispatchStatus,
    event_simulator,
)
from .tool_contracts import (
    TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS,
    TOOL_RESULT_MAX_IMAGES,
    TOOL_RESULT_MAX_PAYLOAD_BYTES,
    TOOL_RESULT_MAX_TEXT_CHARS,
    ToolEffect,
    ToolResult,
    ToolSpec,
)
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
                "原消息中的 @ 对象必须保留为 [at:1]、[at:2] 等身份占位符，"
                "不要改成昵称、QQ号，也不要省略；显式输入的玩家 UID 保持原样，按原顺序排列。"
                "[at:N]仅限当前消息；查询历史玩家时使用本轮提供的[history_at:...]映射，"
                "没有身份映射就请用户补充真实@或UID，不能复用旧[at:N]或猜身份。"
            ),
            "minLength": 1,
            "maxLength": 1024,
        }
    },
    "required": ["command"],
    "additionalProperties": False,
}


_DISPATCH_FEEDBACK = {
    PluginDispatchStatus.INPUT_REQUIRED: (
        "查询对象的身份尚未确认，本次尚未执行插件或游戏查询，不能描述为插件、接口或游戏故障。"
        "不要原样重复调用；若本轮提供了正确的[at:N]或[history_at:...]映射，可用该映射重新生成查询指令。"
        "没有可用映射时，请用户补充目标的真实@或完整玩家UID；不能猜测或替换为当前提问者。"
    ),
    PluginDispatchStatus.PARTIAL_SUCCESS: (
        "插件已产生部分可验证结果，但后续步骤被中断，结果可能不完整；不要再次调用同一工具。"
        "必须立即用一两句话向用户说明：任务未完整完成、缺失了哪部分，并建议稍后重试或改用原生命令。"
    ),
    PluginDispatchStatus.MATCHED_EMPTY: (
        "目标插件 Matcher 已命中，但没有产生可验证输出或副作用；"
        "不要重复调用相同工具和参数。请把这一情况如实告知用户。"
    ),
    PluginDispatchStatus.NOT_MATCHED: (
        "目标插件没有 Matcher 命中该 command；不要重复调用相同工具和参数。"
        "请向用户说明该指令未生效，并建议直接使用对应插件的原生命令。"
    ),
    PluginDispatchStatus.FAILED: (
        "目标插件处理发生异常；不要重复调用相同工具和参数。"
        "必须立即告知用户任务失败，并建议改用原生命令重试。"
    ),
    PluginDispatchStatus.TIMED_OUT: (
        "目标插件处理超时，任务已被中断且没有完成；不要重复调用相同工具和参数。"
        "必须立即用一两句话向用户说明：请求的任务因超时被中断、后续结果不会到达，"
        "建议稍后重试或改用原生命令。"
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
        feedback = _DISPATCH_FEEDBACK[result.status]
        if result.status is PluginDispatchStatus.PARTIAL_SUCCESS and (
            result.text.strip() or result.images
        ):
            confirmed: list[str] = []
            if result.text.strip():
                confirmed.append(result.text.strip()[:2_000])
            if result.images:
                confirmed.append(f"已确认发送图片 {len(result.images)} 张")
            feedback = (
                "插件已经确认产生并向用户显示以下结果；这部分执行成功，"
                "不得描述为失败：\n"
                + "\n".join(confirmed)
                + "\n其后仍有未完成或不确定步骤，最终结果可能不完整；不要再次调用同一工具。"
                "必须向用户简短说明：结果可能不完整、缺失部分不会自动补发，"
                "建议稍后用原生命令重新获取完整结果。"
            )
        super().__init__(feedback)


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


def _normalize_dispatch_text(text: str) -> tuple[str, dict[str, int | bool]]:
    """Normalize an adapter receipt while retaining its confirmed execution."""

    nul_removed = text.count("\x00")
    surrogate_replaced = sum(0xD800 <= ord(char) <= 0xDFFF for char in text)
    normalized = text.replace("\x00", "").encode("utf-8", errors="replace").decode("utf-8")
    truncated = len(normalized) > TOOL_RESULT_MAX_TEXT_CHARS
    if truncated:
        marker = "\n...[工具结果已截断]"
        normalized = normalized[: TOOL_RESULT_MAX_TEXT_CHARS - len(marker)] + marker
    changes: dict[str, int | bool] = {}
    if nul_removed or surrogate_replaced or truncated:
        changes = {
            "original_chars": len(text),
            "normalized_chars": len(normalized),
            "nul_removed": nul_removed,
            "surrogate_replaced": surrogate_replaced,
            "truncated": truncated,
        }
    return normalized, changes


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
        wait_for_result=True,
    )
    if not isinstance(dispatch, PluginDispatchResult):
        raise TypeError("NoneBot 兼容调度必须返回 PluginDispatchResult")
    return await plugin_dispatch_tool_result(dispatch, plugin_name=plugin_name)


def _image_preview(reference: str, maximum: int) -> str | None:
    """Make an optional vision preview; the image already sent to QQ is untouched."""
    if not reference.startswith("data:image/") or ";base64," not in reference:
        return None
    try:
        # Pillow is optional for this plugin. A missing decoder must not undo a
        # confirmed send, and an unexpectedly huge receipt need not be decoded.
        if len(reference) > 32 * 1_024 * 1_024:
            return None
        from PIL import Image

        payload = base64.b64decode(reference.split(";base64,", 1)[1], validate=True)
        with Image.open(io.BytesIO(payload)) as original:
            if original.width * original.height > 48_000_000:
                return None
            preview = original.convert("RGB")
        preview.thumbnail((2560, 8192))
        for quality in (85, 65, 45):
            output = io.BytesIO()
            preview.save(output, format="JPEG", quality=quality)
            result = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
            if len(result) <= maximum:
                return result
            preview.thumbnail((max(1, preview.width // 2), max(1, preview.height // 2)))
    except Exception:
        # Only a preview is lost, never the confirmed external action.
        return None
    return None


def _normalize_dispatch_images(references: tuple[str, ...]) -> tuple[tuple[str, ...], dict[str, int]]:
    images: list[str] = []
    resized = omitted = 0
    # Reserve ample room for bounded text, dispatch counters and JSON syntax.
    remaining = TOOL_RESULT_MAX_PAYLOAD_BYTES - 1_024 * 1_024
    for reference in references:
        if len(images) >= TOOL_RESULT_MAX_IMAGES:
            omitted += 1
            continue
        maximum = min(TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS, remaining)
        if len(reference) > maximum:
            reference = _image_preview(reference, maximum)
            if reference is None:
                omitted += 1
                continue
            resized += 1
        try:
            # Keep the strict contract for all ordinary tool results. Adapt
            # legacy plugin receipts here, after the send has been confirmed.
            ToolResult(images=(reference,))
            size = len(json.dumps(reference, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            omitted += 1
            continue
        if size > remaining:
            omitted += 1
            continue
        images.append(reference)
        remaining -= size
    changes = ({"confirmed_images": len(references), "preview_images": len(images),
                "resized": resized, "omitted": omitted} if resized or omitted else {})
    return tuple(images), changes


async def plugin_dispatch_tool_result(
    dispatch: PluginDispatchResult, *, plugin_name: str,
) -> ToolResult:
    """Use one receipt boundary for provider and legacy NoneBot tools."""
    text, normalization = _normalize_dispatch_text(dispatch.text)
    if normalization:
        dispatch = replace(dispatch, text=text)
        nonebot.logger.warning(
            "兼容插件回执文本已规范化 plugin={} details={}", plugin_name, normalization
        )
    metadata: dict[str, Any] = {"plugin_dispatch": _dispatch_metadata(dispatch)}
    from .config import config_parser
    omit_game_images = plugin_name == "qi_zmws" or config_parser.get_config("skip_self_image_vision", True)
    if omit_game_images:
        metadata["skip_image_vision"] = True
    if normalization:
        metadata["output_text_normalization"] = normalization
    structured = None
    if dispatch.rendered_views:
        structured = {"rendered_views": list(dispatch.rendered_views)}
        metadata["skip_image_vision"] = True
    if dispatch.status is PluginDispatchStatus.MATCHED_WITH_OUTPUT:
        # The native card was delivered to QQ. The model reads its render
        # evidence without receiving or decoding the generated image.
        images, image_normalization = ((), {}) if omit_game_images else await asyncio.to_thread(_normalize_dispatch_images, dispatch.images)
        if omit_game_images:
            metadata["delivered_images"] = len(dispatch.images)
            if dispatch.images:
                note = f"\n已确认向用户发送图片 {len(dispatch.images)} 张；已提供文字回执，不要重复调用。"
                text, _ = _normalize_dispatch_text(text[:TOOL_RESULT_MAX_TEXT_CHARS - len(note)] + note)
        if image_normalization:
            metadata["output_image_normalization"] = image_normalization
            note = (f"\n已确认向用户发送图片 {len(dispatch.images)} 张；"
                    "当前仅提供可用预览，图片发送已成功，不要重复调用。")
            text, _ = _normalize_dispatch_text(text[:TOOL_RESULT_MAX_TEXT_CHARS - len(note)] + note)
            nonebot.logger.warning(
                "兼容插件图片回执已规范化 plugin={} details={}", plugin_name, image_normalization
            )
        return ToolResult(
            text=text,
            images=images,
            metadata=metadata,
            structured=structured,
        )
    if dispatch.status is PluginDispatchStatus.MATCHED_SIDE_EFFECT:
        return ToolResult(
            text=dispatch.text or "插件已成功执行一次由 Bot API 确认的副作用动作。",
            metadata=metadata,
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
        "api_read_failed": result.api_read_failed,
        "api_read_recovered": result.api_read_recovered,
        "api_unresolved_failed": result.api_unresolved_failed,
        "api_unresolved_unknown": result.api_unresolved_unknown,
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

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
from typing import Any

from .compat import timeout as timeout_scope
from .config import config_parser
from .network_safety import validate_url_arguments
from .request_manager import get_current_request_id
from .runtime_metrics import runtime_metrics
from .tool_contracts import ToolContext, ToolEffect, ToolResult
from .tool_manager import tool_manager


class ToolExecutionError(RuntimeError):
    pass


class ToolExecutionTimeoutError(ToolExecutionError):
    """A custom tool exceeded its own cancellable execution budget."""


def ensure_cancellable_mutating_handler(
    tool_name: str,
    spec: Any,
    func: Any,
) -> None:
    if (
        spec is not None
        and spec.effect == ToolEffect.MUTATING
        and not inspect.iscoroutinefunction(func)
    ):
        # Cancelling asyncio.to_thread() only abandons the await; the worker
        # thread continues and may commit a side effect after the caller has
        # observed a timeout. Refuse this unsafe contract before it starts.
        raise ToolExecutionError(
            f"工具 {tool_name} 是不受控的同步 mutating handler，"
            "无法在超时后终止，已拒绝执行；请改为可取消的 async handler，"
            "或迁移到受控文件工具子进程"
        )


def is_tool_superuser(bot: Any, event: Any) -> bool:
    superusers = {
        str(user_id)
        for user_id in getattr(getattr(bot, "config", None), "superusers", set())
    }
    return str(getattr(event, "user_id", "")) in superusers


def _structured_result_fields(
    value: ToolResult | dict[str, Any],
) -> tuple[Any, Any, Any, Any, Any, Any]:
    if isinstance(value, ToolResult):
        return (
            value.text,
            value.images,
            value.files,
            value.structured,
            value.citations,
            value.metadata,
        )

    text_values: list[str] = []
    for key in ("text", "content", "message"):
        if key in value:
            item = value[key]
            if not isinstance(item, str):
                raise ToolExecutionError(
                    f"工具结构化结果 {key} 必须是字符串"
                )
            text_values.append(item)
    text = next((item for item in text_values if item), "")

    image_values: list[list[Any] | tuple[Any, ...]] = []
    for key in ("images", "image_urls"):
        if key in value:
            item = value[key]
            if not isinstance(item, (list, tuple)):
                raise ToolExecutionError(
                    f"工具结构化结果 {key} 必须是字符串数组"
                )
            if not all(
                isinstance(image, str) and image.strip() for image in item
            ):
                raise ToolExecutionError(
                    f"工具结构化结果 {key} 只能包含非空字符串"
                )
            image_values.append(item)
    images = next((item for item in image_values if item), ())
    return (
        text,
        images,
        value.get("files", ()),
        value.get("structured"),
        value.get("citations", ()),
        value.get("metadata", {}),
    )


def _normalize_result(value: Any, *, spec: Any) -> ToolResult:
    result_limit = (
        spec.result_limit
        if spec is not None and spec.result_limit is not None
        else config_parser.get_config("max_tool_result_chars", 6_000)
    )
    image_limit = config_parser.get_config("max_tool_images", 4)
    if (
        not isinstance(result_limit, int)
        or isinstance(result_limit, bool)
        or result_limit <= 0
    ):
        raise ToolExecutionError("工具结果字符上限配置无效")
    if (
        not isinstance(image_limit, int)
        or isinstance(image_limit, bool)
        or image_limit <= 0
    ):
        raise ToolExecutionError("工具结果图片上限配置无效")

    if isinstance(value, (ToolResult, dict)):
        text, images, files, structured, citations, metadata = (
            _structured_result_fields(value)
        )
        if not isinstance(text, str):
            raise ToolExecutionError("工具结构化结果 text 必须是字符串")
        if not isinstance(images, (list, tuple)):
            raise ToolExecutionError("工具结构化结果 images 必须是字符串数组")
        if not all(isinstance(item, str) and item.strip() for item in images):
            raise ToolExecutionError("工具结构化结果 images 只能包含非空字符串")
        if not isinstance(metadata, Mapping):
            raise ToolExecutionError("工具结构化结果 metadata 必须是映射")
        if not all(isinstance(key, str) for key in metadata):
            raise ToolExecutionError("工具结构化结果 metadata 的键必须是字符串")
        normalized_images = tuple(images[:image_limit])
    else:
        text = str(value)
        normalized_images = ()
        files = ()
        structured = None
        citations = ()
        metadata = {}

    if len(text) > result_limit:
        text = text[:result_limit] + "\n...[工具结果已截断]"
    try:
        return ToolResult(
            text=text,
            images=normalized_images,
            metadata=metadata,
            files=files,
            structured=structured,
            citations=citations,
        )
    except (TypeError, ValueError) as error:
        raise ToolExecutionError(f"工具结构化结果无效: {error}") from None


async def _prepare_custom_tool_call(
    tool_name: str,
    tool_entry: Mapping[str, Any],
    arguments: dict[str, Any],
    *,
    bot: Any,
    event: Any,
    confirmed: bool,
    allow_pending_mutating: bool,
) -> tuple[Any, Any, dict[str, Any]]:
    func = tool_entry.get("func")
    if not callable(func):
        raise ToolExecutionError(f"工具 {tool_name} handler 不可调用")
    spec = tool_entry.get("tool_spec")
    if spec is not None:
        if spec.permission == "superuser" and not is_tool_superuser(bot, event):
            raise ToolExecutionError(f"工具 {tool_name} 仅允许超级用户执行")
        ensure_cancellable_mutating_handler(tool_name, spec, func)
        if (
            spec.effect == ToolEffect.MUTATING
            and not confirmed
            and not allow_pending_mutating
        ):
            raise ToolExecutionError(f"工具 {tool_name} 尚未完成二阶段确认")

    call_arguments = dict(arguments)
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError) as error:
        raise ToolExecutionError(f"工具 {tool_name} handler 参数签名不可检查") from error
    if not any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in sig.parameters.values()
    ):
        unexpected = [key for key in call_arguments if key not in sig.parameters]
        if unexpected:
            available = [
                key
                for key, parameter in sig.parameters.items()
                if not key.startswith("_")
                and parameter.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            ]
            raise ToolExecutionError(
                f"{tool_name} 不支持参数 {', '.join(unexpected)}；"
                f"可用参数：{', '.join(available) or '无'}"
            )

    # Validate only model-controlled values. Injecting trusted runtime objects before
    # URL validation would unnecessarily traverse Bot/Event internals.
    await validate_url_arguments(call_arguments)
    if "_tool_manager" in sig.parameters:
        call_arguments["_tool_manager"] = tool_manager
    if "_bot" in sig.parameters:
        call_arguments["_bot"] = bot
    if "_event" in sig.parameters:
        call_arguments["_event"] = event
    if "_tool_context" in sig.parameters:
        call_arguments["_tool_context"] = ToolContext(
            bot=bot,
            event=event,
            request_id=get_current_request_id(),
            confirmed=confirmed,
        )
    return func, spec, call_arguments


async def validate_pending_custom_tool(
    tool_name: str,
    tool_entry: Mapping[str, Any],
    arguments: dict[str, Any],
    *,
    bot: Any,
    event: Any,
) -> None:
    """Apply the execution preflight before storing a mutating call."""

    await _prepare_custom_tool_call(
        tool_name,
        tool_entry,
        arguments,
        bot=bot,
        event=event,
        confirmed=False,
        allow_pending_mutating=True,
    )


async def execute_custom_tool(
    tool_name: str,
    tool_entry: Mapping[str, Any],
    arguments: dict[str, Any],
    *,
    bot: Any,
    event: Any,
    confirmed: bool = False,
) -> ToolResult:
    """Execute one already-snapshotted custom tool under its runtime contract."""

    func, spec, call_arguments = await _prepare_custom_tool_call(
        tool_name,
        tool_entry,
        arguments,
        bot=bot,
        event=event,
        confirmed=confirmed,
        allow_pending_mutating=False,
    )

    timeout = (
        spec.timeout_seconds
        if spec is not None and spec.timeout_seconds
        else config_parser.get_config("tool_timeout_seconds", 30)
    )
    try:
        async with timeout_scope(timeout):
            result = (
                await func(**call_arguments)
                if inspect.iscoroutinefunction(func)
                else await asyncio.to_thread(func, **call_arguments)
            )
    except TimeoutError:
        runtime_metrics.tool_timeouts += 1
        raise ToolExecutionTimeoutError(
            f"工具执行超过 {timeout} 秒预算"
        ) from None
    return _normalize_result(result, spec=spec)

from __future__ import annotations

import asyncio
import base64
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import hashlib
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse
import uuid

from nonebot.adapters.onebot.v11 import (
    Bot as V11Bot,
)
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11 import (
    Message as V11Message,
)
from nonebot.adapters.onebot.v11 import (
    MessageSegment as V11MessageSegment,
)
from nonebot.adapters.onebot.v12 import (
    Bot as V12Bot,
)
from nonebot.adapters.onebot.v12 import (
    GroupMessageEvent as V12GroupMessageEvent,
)
from nonebot.adapters.onebot.v12 import (
    Message as V12Message,
)
from nonebot.adapters.onebot.v12 import (
    MessageSegment as V12MessageSegment,
)
from nonebot.adapters.onebot.v12 import (
    PrivateMessageEvent as V12PrivateMessageEvent,
)
from nonebot.compat import model_dump
from nonebot.exception import (
    ActionFailed,
    ApiNotAvailable,
    NetworkError,
    StopPropagation,
)
from nonebot.internal.matcher import matchers
from nonebot.log import logger
from nonebot.message import (
    _apply_event_postprocessors,
    _apply_event_preprocessors,
    _check_matcher,
    _run_matcher,
    run_postprocessor,
)
from nonebot.plugin import get_plugin
from nonebot.rule import TrieRule

from .admission import AdmissionRejected, get_dispatch_controller
from .compat import timeout as timeout_scope
from .config import config_parser
from .onebot_facade import onebot_protocol
from .protocol_registry import protocol_registry
from .runtime_metrics import runtime_metrics

if TYPE_CHECKING:
    from nonebot.adapters import Bot

_capture_key: ContextVar[str | None] = ContextVar("moellm_capture_key", default=None)
_synthetic_plugin: ContextVar[str | None] = ContextVar("moellm_synthetic_plugin", default=None)
_captures: dict[str, dict] = {}
_SEND_ACTIONS = {
    "send_msg",
    "send_group_msg",
    "send_private_msg",
    "send_message",
}
_AT_PATTERN = re.compile(r"\[(at:(\d+)|at_all)\]")


class PluginDispatchStatus(str, Enum):
    MATCHED_WITH_OUTPUT = "matched_with_output"
    MATCHED_SIDE_EFFECT = "matched_side_effect"
    PARTIAL_SUCCESS = "partial_success"
    MATCHED_EMPTY = "matched_empty"
    NOT_MATCHED = "not_matched"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ADMISSION_REJECTED = "admission_rejected"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True)
class PluginDispatchResult:
    status: PluginDispatchStatus
    text: str = ""
    images: tuple[str, ...] = ()
    matcher_checked: int = 0
    matcher_matched: int = 0
    matcher_failed: int = 0
    matcher_blocked: int = 0
    successful_captures: int = 0
    api_succeeded: int = 0
    api_failed: int = 0
    api_unknown: int = 0
    mutating_api_succeeded: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, PluginDispatchStatus):
            raise TypeError("PluginDispatchResult.status 非法")
        if not isinstance(self.text, str) or not isinstance(self.images, tuple):
            raise TypeError("PluginDispatchResult 输出类型非法")
        for field_name in (
            "matcher_checked",
            "matcher_matched",
            "matcher_failed",
            "matcher_blocked",
            "successful_captures",
            "api_succeeded",
            "api_failed",
            "api_unknown",
            "mutating_api_succeeded",
            "duration_ms",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"PluginDispatchResult.{field_name} 必须是非负整数")

    @property
    def succeeded(self) -> bool:
        return self.status in {
            PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            PluginDispatchStatus.MATCHED_SIDE_EFFECT,
        }


def is_synthetic_event() -> bool:
    """Return whether the current task is executing an LLM compatibility event."""
    return _synthetic_plugin.get() is not None


def get_synthetic_target() -> str | None:
    return _synthetic_plugin.get()


def _message_class(protocol: str) -> type[Any]:
    return V12Message if protocol == "onebot_v12" else V11Message


def _rewrite_reply_id(
    message,
    original_id: int | str,
    fake_id: int | str,
    *,
    protocol: str,
):
    result = _message_class(protocol)(message)
    for segment in result:
        if segment.type != "reply":
            continue
        field = "message_id" if protocol == "onebot_v12" else "id"
        if str(segment.data.get(field)) == str(fake_id):
            segment.data[field] = str(original_id)
    return result


def _extract_send_data(message, *, protocol: str) -> dict:
    normalized = _message_class(protocol)(message)
    text: list[str] = []
    images: list[str] = []
    for segment in normalized:
        if segment.type == "image":
            raw = segment.data.get("url") or segment.data.get("file_id") or segment.data.get("file") or ""
            if raw.startswith("base64://"):
                images.append("data:image/jpeg;base64," + raw[9:])
            elif raw.startswith("file://"):
                try:
                    local_path = unquote(urlparse(raw).path)
                    with open(local_path, "rb") as file:
                        payload = base64.b64encode(file.read()).decode()
                    images.append("data:image/jpeg;base64," + payload)
                except Exception:
                    logger.warning("读取插件返回的本地图片失败")
            elif raw:
                images.append(raw)
            text.append("[图片]")
        elif segment.type == "text":
            text.append(segment.data.get("text", ""))
        elif segment.type == "at":
            text.append(f"[提及:{segment.data.get('qq')}]")
        elif segment.type == "mention":
            text.append(f"[提及:{segment.data.get('user_id')}]")
        elif segment.type != "reply":
            text.append(str(segment))
    full_text = "".join(text).strip()
    limit = config_parser.get_config("max_tool_result_chars", 6000)
    if len(full_text) > limit:
        full_text = full_text[:limit] + "\n...[工具结果已截断]"
    image_limit = config_parser.get_config("max_tool_images", 4)
    return {"text": full_text, "images": images[:image_limit]}


def _api_effect(protocol: str, api: str) -> str | None:
    protocol_ids = ("onebot_v12",) if protocol == "onebot_v12" else ("onebot_v11", "napcat_v11")
    for protocol_id in protocol_ids:
        action = protocol_registry.by_id.get(f"{protocol_id}:{api}")
        if action is not None:
            return protocol_registry.policy_for(action).effect
    return None


def _unknown_api_result(exception: Exception) -> bool:
    if isinstance(exception, (ActionFailed, ApiNotAvailable)):
        return False
    return isinstance(
        exception,
        (NetworkError, TimeoutError, asyncio.TimeoutError, ConnectionError),
    )


async def _capture_outgoing_api(bot: Bot, api: str, data: dict) -> None:
    """Stage API evidence; only ``on_called_api`` may commit success."""
    key = _capture_key.get()
    if key is None:
        return
    context = _captures.get(key)
    if context is None:
        return
    protocol = onebot_protocol(bot) or context.get(
        "protocol",
        "onebot_v11",
    )
    output = None
    message = data.get("message")
    if api in _SEND_ACTIONS and message is not None:
        fixed = _rewrite_reply_id(
            message,
            context["original_id"],
            context["fake_id"],
            protocol=protocol,
        )
        data["message"] = fixed
        output = _extract_send_data(fixed, protocol=protocol)
    context["pending_api"][id(data)] = {
        "api": api,
        "effect": _api_effect(protocol, api),
        "output": output,
    }


async def _confirm_outgoing_api(
    bot: Bot,
    exception: Exception | None,
    api: str,
    data: dict,
    result: Any,
) -> None:
    """Commit captured output/effect only after the adapter confirms success."""

    del bot, result
    key = _capture_key.get()
    if key is None:
        return
    context = _captures.get(key)
    if context is None:
        return
    staged = context["pending_api"].pop(id(data), None)
    if staged is None:
        staged = {"api": api, "effect": None, "output": None}
    if exception is not None:
        context["api_failed"] += 1
        if _unknown_api_result(exception):
            context["api_unknown"] += 1
        return
    context["api_succeeded"] += 1
    if staged.get("effect") == "mutating":
        context["mutating_api_succeeded"] += 1
    output = staged.get("output")
    if isinstance(output, dict):
        context["messages"].append(output)


V11Bot.on_calling_api(_capture_outgoing_api)
V12Bot.on_calling_api(_capture_outgoing_api)
V11Bot.on_called_api(_confirm_outgoing_api)
V12Bot.on_called_api(_confirm_outgoing_api)


@run_postprocessor
async def _observe_synthetic_matcher_exception(exception: Exception | None) -> None:
    key = _capture_key.get()
    if key is None or exception is None:
        return
    context = _captures.get(key)
    if context is not None:
        context["matcher_failed"] += 1


def _build_fake_message(
    command: str,
    source: dict | None = None,
    *,
    protocol: str = "onebot_v11",
) -> Any:
    source = source or {}
    mentions = source.get("mentions") or []
    reply_user = source.get("reply_user") or {}
    message_class = _message_class(protocol)
    segment_class: Any = V12MessageSegment if protocol == "onebot_v12" else V11MessageSegment
    result: Any = message_class()
    last = 0
    for match in _AT_PATTERN.finditer(command):
        if match.start() > last:
            result.append(segment_class.text(command[last : match.start()]))
        token = match.group(1)
        if token.startswith("at:"):
            index = int(match.group(2))
            target = reply_user if index == 0 else (mentions[index - 1] if 0 < index <= len(mentions) else {})
            if qq := target.get("qq"):
                result.append(segment_class.mention(str(qq)) if protocol == "onebot_v12" else segment_class.at(int(qq)))
        elif token == "at_all":
            for target in mentions:
                if qq := target.get("qq"):
                    result.append(segment_class.mention(str(qq)) if protocol == "onebot_v12" else segment_class.at(int(qq)))
        last = match.end()
    if last < len(command):
        result.append(segment_class.text(command[last:]))
    return result


def _build_event(original: Any, command: str, source: dict | None) -> tuple[Any, int | str]:
    protocol = (
        "onebot_v12"
        if isinstance(
            original,
            (V12GroupMessageEvent, V12PrivateMessageEvent),
        )
        else "onebot_v11"
    )
    message = _build_fake_message(command, source, protocol=protocol)
    for segment in getattr(original, "message", []):
        if segment.type == "image":
            message.append(segment)
    if protocol == "onebot_v12":
        fake_id: int | str = uuid.uuid4().hex
    else:
        fake_id = int(time.time_ns() % 10**15)
        if fake_id == int(original.message_id):
            fake_id += 1
    if protocol == "onebot_v12":
        kwargs = model_dump(original)
        kwargs.update(
            {
                "id": uuid.uuid4().hex,
                "message_id": str(fake_id),
                "message": message,
                "original_message": message,
                "alt_message": command,
            }
        )
        if isinstance(original, V12GroupMessageEvent):
            return V12GroupMessageEvent(**kwargs), fake_id
        if isinstance(original, V12PrivateMessageEvent):
            return V12PrivateMessageEvent(**kwargs), fake_id
        raise TypeError(f"不支持的 v12 事件类型: {type(original).__name__}")
    kwargs = {
        "time": original.time,
        "self_id": getattr(original, "self_id"),
        "post_type": getattr(original, "post_type"),
        "sub_type": original.sub_type,
        "user_id": original.user_id,
        "message_type": getattr(original, "message_type"),
        "message_id": fake_id,
        "message": message,
        "raw_message": str(message),
        "font": getattr(original, "font", 0),
        "sender": getattr(original, "sender"),
    }
    if (reply := getattr(original, "reply", None)) is not None:
        kwargs["reply"] = reply
    if isinstance(original, GroupMessageEvent):
        kwargs["group_id"] = original.group_id
        return GroupMessageEvent(**kwargs), fake_id
    if isinstance(original, PrivateMessageEvent):
        return PrivateMessageEvent(**kwargs), fake_id
    raise TypeError(f"不支持的事件类型: {type(original).__name__}")


async def _observe_and_run_matcher(
    matcher,
    bot: Bot,
    event,
    state: dict,
    stack: AsyncExitStack | None,
    dependency_cache: dict,
) -> bool:
    key = _capture_key.get()
    context = _captures.get(key) if key is not None else None
    if context is not None:
        context["matcher_checked"] += 1
    try:
        matched = await _check_matcher(
            matcher,
            bot,
            event,
            state,
            stack,
            dependency_cache,
        )
    except Exception:
        if context is not None:
            context["matcher_failed"] += 1
        logger.error("目标 Matcher 规则检查失败，异常详情已安全省略")
        return False
    if not matched:
        return False
    if context is not None:
        context["matcher_matched"] += 1
    try:
        await _run_matcher(
            matcher,
            bot,
            event,
            state,
            stack,
            dependency_cache,
        )
    except StopPropagation:
        if context is not None:
            context["matcher_blocked"] += 1
        raise
    except Exception:
        if context is not None:
            context["matcher_failed"] += 1
        raise
    return True


async def _dispatch_targeted(bot: Bot, event, plugin_name: str) -> None:
    plugin = get_plugin(plugin_name)
    if plugin is None:
        raise LookupError(f"目标插件未加载: {plugin_name}")
    state: dict = {}
    dependency_cache: dict = {}
    try:
        TrieRule.get_value(bot, event, state)
    except Exception:
        logger.exception("解析目标插件指令失败")
    for matcher in sorted(plugin.matcher, key=lambda item: item.priority):
        try:
            await _observe_and_run_matcher(
                matcher,
                bot,
                event,
                state.copy(),
                None,
                dependency_cache,
            )
        except StopPropagation:
            break


async def _dispatch_full_bus(bot: Bot, event) -> None:
    """Cancelable compatibility bus including preprocessors and all matchers."""
    state: dict = {}
    dependency_cache: dict = {}
    async with AsyncExitStack() as stack:
        if not await _apply_event_preprocessors(bot, event, state, stack, dependency_cache, show_log=False):
            return
        try:
            TrieRule.get_value(bot, event, state)
        except Exception:
            logger.exception("解析兼容事件指令失败")
        stopped = False
        for priority in sorted(matchers):
            for matcher in list(matchers[priority]):
                try:
                    await _observe_and_run_matcher(
                        matcher,
                        bot,
                        event,
                        state.copy(),
                        stack,
                        dependency_cache,
                    )
                except StopPropagation:
                    stopped = True
                    break
            if stopped:
                break
        await _apply_event_postprocessors(bot, event, state, stack, dependency_cache, show_log=False)


def _dispatch_result(
    context: dict[str, Any],
    *,
    started_monotonic: float,
    forced_status: PluginDispatchStatus | None = None,
) -> PluginDispatchResult:
    pending = context.get("pending_api", {})
    if pending:
        context["api_unknown"] += len(pending)
        context["api_failed"] += len(pending)
        pending.clear()
    captured = context.get("messages", [])
    texts = [item["text"] for item in captured if item.get("text")]
    images = [url for item in captured for url in item.get("images", [])]
    has_verified_effect = bool(captured or context.get("mutating_api_succeeded", 0))
    if forced_status is not None:
        status = forced_status
        if (
            forced_status
            in {
                PluginDispatchStatus.FAILED,
                PluginDispatchStatus.TIMED_OUT,
                PluginDispatchStatus.RESULT_UNKNOWN,
            }
            and has_verified_effect
        ):
            status = PluginDispatchStatus.PARTIAL_SUCCESS
        elif context.get("api_unknown", 0) and forced_status in {
            PluginDispatchStatus.FAILED,
            PluginDispatchStatus.TIMED_OUT,
        }:
            status = PluginDispatchStatus.RESULT_UNKNOWN
    elif context.get("api_unknown", 0):
        status = PluginDispatchStatus.PARTIAL_SUCCESS if has_verified_effect else PluginDispatchStatus.RESULT_UNKNOWN
    elif context.get("matcher_failed", 0) or context.get("api_failed", 0):
        status = PluginDispatchStatus.PARTIAL_SUCCESS if has_verified_effect else PluginDispatchStatus.FAILED
    elif captured:
        status = PluginDispatchStatus.MATCHED_WITH_OUTPUT
    elif context.get("mutating_api_succeeded", 0):
        status = PluginDispatchStatus.MATCHED_SIDE_EFFECT
    elif context.get("matcher_matched", 0):
        status = PluginDispatchStatus.MATCHED_EMPTY
    else:
        status = PluginDispatchStatus.NOT_MATCHED

    return PluginDispatchResult(
        status=status,
        text="\n".join(texts),
        images=tuple(images),
        matcher_checked=int(context.get("matcher_checked", 0)),
        matcher_matched=int(context.get("matcher_matched", 0)),
        matcher_failed=int(context.get("matcher_failed", 0)),
        matcher_blocked=int(context.get("matcher_blocked", 0)),
        successful_captures=len(captured),
        api_succeeded=int(context.get("api_succeeded", 0)),
        api_failed=int(context.get("api_failed", 0)),
        api_unknown=int(context.get("api_unknown", 0)),
        mutating_api_succeeded=int(context.get("mutating_api_succeeded", 0)),
        duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
    )


def _empty_dispatch_context() -> dict[str, Any]:
    return {
        "messages": [],
        "pending_api": {},
        "matcher_checked": 0,
        "matcher_matched": 0,
        "matcher_failed": 0,
        "matcher_blocked": 0,
        "api_succeeded": 0,
        "api_failed": 0,
        "api_unknown": 0,
        "mutating_api_succeeded": 0,
    }


class EventSimulator:
    async def dispatch_event(
        self,
        bot: Bot,
        original_event,
        command_str: str,
        format_message_dict: dict | None = None,
        plugin_name: str | None = None,
    ) -> PluginDispatchResult:
        started_monotonic = time.monotonic()
        if (
            not isinstance(command_str, str)
            or not 1 <= len(command_str) <= 1024
            or not isinstance(plugin_name, str)
            or not plugin_name
        ):
            return _dispatch_result(
                _empty_dispatch_context(),
                started_monotonic=started_monotonic,
                forced_status=PluginDispatchStatus.FAILED,
            )
        try:
            async with get_dispatch_controller().slot():
                fake_event, fake_id = _build_event(original_event, command_str, format_message_dict)
                capture_id = uuid.uuid4().hex
                context = _empty_dispatch_context()
                context.update(
                    {
                        "original_id": original_event.message_id,
                        "fake_id": fake_id,
                        "protocol": onebot_protocol(bot, original_event) or "onebot_v11",
                    }
                )
                _captures[capture_id] = context
                capture_token = _capture_key.set(capture_id)
                event_token = _synthetic_plugin.set(plugin_name)
                full_plugins = set(config_parser.get_config("legacy_full_event_plugins", []))
                mode = "full" if plugin_name in full_plugins else "targeted"
                runtime_metrics.dispatch_modes[mode] += 1
                forced_status = None
                try:
                    timeout = config_parser.get_config("legacy_dispatch_timeout_seconds", 20)
                    async with timeout_scope(timeout):
                        if mode == "full":
                            await _dispatch_full_bus(bot, fake_event)
                        else:
                            await _dispatch_targeted(bot, fake_event, plugin_name)
                except TimeoutError:
                    runtime_metrics.dispatch_timeouts += 1
                    forced_status = PluginDispatchStatus.TIMED_OUT
                except Exception:
                    forced_status = PluginDispatchStatus.FAILED
                    logger.error("NoneBot 插件兼容执行失败，异常详情已安全省略")
                finally:
                    _synthetic_plugin.reset(event_token)
                    _capture_key.reset(capture_token)
                    context = _captures.pop(
                        capture_id,
                        _empty_dispatch_context(),
                    )
                result = _dispatch_result(
                    context,
                    started_monotonic=started_monotonic,
                    forced_status=forced_status,
                )
                command_digest = hashlib.sha256(command_str.encode("utf-8")).hexdigest()
                logger.info(
                    "NoneBot 插件兼容调度完成: "
                    f"plugin={plugin_name} command_digest={command_digest[:12]} "
                    f"status={result.status.value} "
                    f"matcher_checked={result.matcher_checked} "
                    f"matcher_matched={result.matcher_matched} "
                    f"matcher_failed={result.matcher_failed} "
                    f"capture_success={result.successful_captures} "
                    f"api_success={result.api_succeeded} "
                    f"api_failed={result.api_failed} "
                    f"duration_ms={result.duration_ms}"
                )
                return result
        except AdmissionRejected:
            return _dispatch_result(
                _empty_dispatch_context(),
                started_monotonic=started_monotonic,
                forced_status=PluginDispatchStatus.ADMISSION_REJECTED,
            )
        except Exception:
            logger.error("NoneBot 插件兼容调度构造失败，异常详情已安全省略")
            return _dispatch_result(
                _empty_dispatch_context(),
                started_monotonic=started_monotonic,
                forced_status=PluginDispatchStatus.FAILED,
            )


event_simulator = EventSimulator()

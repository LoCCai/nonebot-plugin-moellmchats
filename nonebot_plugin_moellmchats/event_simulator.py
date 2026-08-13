from __future__ import annotations

import base64
from contextlib import AsyncExitStack
from contextvars import ContextVar
import re
import time
import traceback
from urllib.parse import unquote, urlparse
import uuid

from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.exception import StopPropagation
from nonebot.internal.matcher import matchers
from nonebot.log import logger
from nonebot.message import (
    _apply_event_postprocessors,
    _apply_event_preprocessors,
    check_and_run_matcher,
)
from nonebot.plugin import get_plugin
from nonebot.rule import TrieRule

from .admission import AdmissionRejected, get_dispatch_controller
from .compat import timeout as timeout_scope
from .config import config_parser
from .runtime_metrics import runtime_metrics

_capture_key: ContextVar[str | None] = ContextVar("moellm_capture_key", default=None)
_synthetic_plugin: ContextVar[str | None] = ContextVar(
    "moellm_synthetic_plugin", default=None
)
_captures: dict[str, dict] = {}
_SEND_ACTIONS = {"send_msg", "send_group_msg", "send_private_msg"}
_AT_PATTERN = re.compile(r"\[(at:(\d+)|at_all)\]")


def is_synthetic_event() -> bool:
    """Return whether the current task is executing an LLM compatibility event."""
    return _synthetic_plugin.get() is not None


def get_synthetic_target() -> str | None:
    return _synthetic_plugin.get()


def _rewrite_reply_id(message, original_id: int | str, fake_id: int | str) -> Message:
    result = Message(message)
    for segment in result:
        if segment.type == "reply" and str(segment.data.get("id")) == str(fake_id):
            segment.data["id"] = str(original_id)
    return result


def _extract_send_data(message) -> dict:
    normalized = Message(message)
    text: list[str] = []
    images: list[str] = []
    for segment in normalized:
        if segment.type == "image":
            raw = segment.data.get("url") or segment.data.get("file") or ""
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
        elif segment.type != "reply":
            text.append(str(segment))
    full_text = "".join(text).strip()
    limit = config_parser.get_config("max_tool_result_chars", 6000)
    if len(full_text) > limit:
        full_text = full_text[:limit] + "\n...[工具结果已截断]"
    image_limit = config_parser.get_config("max_tool_images", 4)
    return {"text": full_text, "images": images[:image_limit]}


@Bot.on_calling_api
async def _capture_outgoing_api(bot: Bot, api: str, data: dict) -> None:
    """Capture target-plugin output through NoneBot's supported API hook."""
    key = _capture_key.get()
    if key is None or api not in _SEND_ACTIONS:
        return
    context = _captures.get(key)
    message = data.get("message")
    if context is None or message is None:
        return
    fixed = _rewrite_reply_id(message, context["original_id"], context["fake_id"])
    data["message"] = fixed
    context["messages"].append(_extract_send_data(fixed))


def _build_fake_message(command: str, source: dict | None = None) -> Message:
    source = source or {}
    mentions = source.get("mentions") or []
    reply_user = source.get("reply_user") or {}
    result = Message()
    last = 0
    for match in _AT_PATTERN.finditer(command):
        if match.start() > last:
            result.append(MessageSegment.text(command[last : match.start()]))
        token = match.group(1)
        if token.startswith("at:"):
            index = int(match.group(2))
            target = reply_user if index == 0 else (
                mentions[index - 1] if 0 < index <= len(mentions) else {}
            )
            if qq := target.get("qq"):
                result.append(MessageSegment.at(int(qq)))
        elif token == "at_all":
            for target in mentions:
                if qq := target.get("qq"):
                    result.append(MessageSegment.at(int(qq)))
        last = match.end()
    if last < len(command):
        result.append(MessageSegment.text(command[last:]))
    return result


def _build_event(original, command: str, source: dict | None):
    message = _build_fake_message(command, source)
    for segment in getattr(original, "message", []):
        if segment.type == "image":
            message.append(segment)
    fake_id = int(time.time_ns() % 10**15)
    if fake_id == int(original.message_id):
        fake_id += 1
    kwargs = {
        "time": original.time,
        "self_id": original.self_id,
        "post_type": original.post_type,
        "sub_type": original.sub_type,
        "user_id": original.user_id,
        "message_type": original.message_type,
        "message_id": fake_id,
        "message": message,
        "raw_message": str(message),
        "font": getattr(original, "font", 0),
        "sender": original.sender,
    }
    if (reply := getattr(original, "reply", None)) is not None:
        kwargs["reply"] = reply
    if isinstance(original, GroupMessageEvent):
        kwargs["group_id"] = original.group_id
        return GroupMessageEvent(**kwargs), fake_id
    if isinstance(original, PrivateMessageEvent):
        return PrivateMessageEvent(**kwargs), fake_id
    raise TypeError(f"不支持的事件类型: {type(original).__name__}")


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
            await check_and_run_matcher(
                matcher,
                bot,
                event,
                state.copy(),
                dependency_cache=dependency_cache,
            )
        except StopPropagation:
            break


async def _dispatch_full_bus(bot: Bot, event) -> None:
    """Cancelable compatibility bus including preprocessors and all matchers."""
    state: dict = {}
    dependency_cache: dict = {}
    async with AsyncExitStack() as stack:
        if not await _apply_event_preprocessors(
            bot, event, state, stack, dependency_cache, show_log=False
        ):
            return
        try:
            TrieRule.get_value(bot, event, state)
        except Exception:
            logger.exception("解析兼容事件指令失败")
        stopped = False
        for priority in sorted(matchers):
            for matcher in list(matchers[priority]):
                try:
                    await check_and_run_matcher(
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
        await _apply_event_postprocessors(
            bot, event, state, stack, dependency_cache, show_log=False
        )


class EventSimulator:
    async def dispatch_event(
        self,
        bot: Bot,
        original_event,
        command_str: str,
        format_message_dict: dict | None = None,
        plugin_name: str | None = None,
    ) -> tuple[str, list[str]]:
        if not command_str or not plugin_name:
            return "执行失败：缺少目标插件或指令参数", []
        try:
            async with get_dispatch_controller().slot():
                fake_event, fake_id = _build_event(
                    original_event, command_str, format_message_dict
                )
                capture_id = uuid.uuid4().hex
                _captures[capture_id] = {
                    "messages": [],
                    "original_id": original_event.message_id,
                    "fake_id": fake_id,
                }
                capture_token = _capture_key.set(capture_id)
                event_token = _synthetic_plugin.set(plugin_name)
                full_plugins = set(
                    config_parser.get_config("legacy_full_event_plugins", [])
                )
                mode = "full" if plugin_name in full_plugins else "targeted"
                runtime_metrics.dispatch_modes[mode] += 1
                try:
                    timeout = config_parser.get_config(
                        "legacy_dispatch_timeout_seconds", 20
                    )
                    async with timeout_scope(timeout):
                        if mode == "full":
                            await _dispatch_full_bus(bot, fake_event)
                        else:
                            await _dispatch_targeted(bot, fake_event, plugin_name)
                except TimeoutError:
                    runtime_metrics.dispatch_timeouts += 1
                    return "执行失败：调用的插件处理超时", []
                finally:
                    _synthetic_plugin.reset(event_token)
                    _capture_key.reset(capture_token)
                    captured = _captures.pop(capture_id, {"messages": []})[
                        "messages"
                    ]
                texts = [item["text"] for item in captured if item["text"]]
                images = [url for item in captured for url in item["images"]]
                return "\n".join(texts), images
        except AdmissionRejected:
            return "执行失败：插件兼容队列已满，请稍后重试", []
        except Exception as error:
            logger.error(traceback.format_exc())
            return f"插件执行出错: {type(error).__name__} - {error}", []


event_simulator = EventSimulator()

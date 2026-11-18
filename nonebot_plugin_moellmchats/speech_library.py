"""Local character dialogue lookup and original voice playback, without an API call."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import random
import re
import time

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import MessageSegment

from .config import config_parser, speech_config
from .onebot_facade import bot_self_id, event_message, event_user_id, onebot_protocol
from .request_recovery import durable_send
from .speech_tools import SpeechError, _read_reference, mark_voice_sent, speech_enabled
from .tool_contracts import (
    ToolCapabilityV2,
    ToolConfirmationMode,
    ToolContext,
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    register_tool,
    tool_registry,
)


def original_voice_requested(text: str) -> bool:
    return bool(re.search(r"原声|游戏.{0,4}(?:语音|台词)|七七.{0,4}台词|台词.{0,4}七七", text))


def _library() -> tuple[Path, list[dict]]:
    if not speech_config.moellm_speech_library:
        raise SpeechError("尚未配置本地七七原声库。")
    path = Path(speech_config.moellm_speech_library).expanduser()
    if path.stat().st_size > 1024 * 1024:
        raise SpeechError("本地原声索引大小异常。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("character") != "七七" or data.get("language") != "zh-CN":
        raise SpeechError("本地原声库角色或语言不匹配。")
    clips = data["clips"]
    if not isinstance(clips, list) or not clips or len(clips) > 500:
        raise SpeechError("本地原声索引无效。")
    for clip in clips:
        if not isinstance(clip, dict) or not all(isinstance(clip.get(key), str) for key in ("title", "text", "file", "sha256")):
            raise SpeechError("本地原声索引字段无效。")
    return path.parent, clips


def _normal(text: str) -> str:
    return "".join(char for char in text.casefold() if char.isalnum())


def _matches(clips: list[dict], query: str) -> list[dict]:
    key = _normal(query)
    if not key:
        return clips
    exact = [clip for clip in clips if key in {_normal(clip["title"]), _normal(clip["text"])}]
    return exact or [clip for clip in clips if key in _normal(clip["title"]) or key in _normal(clip["text"])]


def _audio(root: Path, clip: dict) -> bytes:
    name = clip["file"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}\.wav", name):
        raise SpeechError("本地原声文件名无效。")
    path = root / name
    if path.is_symlink():
        raise SpeechError("本地原声文件路径无效。")
    payload = _read_reference(path)
    if payload is None or hashlib.sha256(payload).hexdigest() != clip["sha256"]:
        raise SpeechError("本地原声缺失或校验失败。")
    return payload


async def qiqi_voice(*, _tool_context: ToolContext, query: str = "", action: str = "search") -> ToolResult:
    if action not in {"search", "play"} or not isinstance(query, str) or len(query) > 200:
        return ToolResult(
            text="请按台词标题或关键词搜索，action 只能为 search 或 play。", metadata={"status": "invalid_arguments"}
        )
    if not speech_enabled():
        return ToolResult(text="管理员已关闭 LLM 语音，请使用文字回复。", metadata={"status": "disabled"})
    try:
        root, clips = await asyncio.to_thread(_library)
        matches = _matches(clips, query)
        structured = {
            "total": len(clips),
            "matches": len(matches),
            "titles": [clip["title"] for clip in matches],
            "lines": [{"title": clip["title"], "text": clip["text"]} for clip in matches[:12]],
        }
        if action == "search" or len(matches) != 1:
            return ToolResult(
                text="七七中文原声库；按完整标题可播放唯一匹配的台词。"
                if matches
                else "没有找到匹配台词，请尝试其他关键词或留空查看全部标题。",
                structured=structured,
                metadata={"status": "success" if action == "search" else "selection_required"},
            )
        if onebot_protocol(_tool_context.bot, _tool_context.event) != "onebot_v11":
            return ToolResult(text="当前平台不支持 QQ 语音发送。", metadata={"status": "failed"})
        payload = await asyncio.to_thread(_audio, root, matches[0])
    except (OSError, ValueError, KeyError, TypeError, SpeechError) as error:
        logger.warning("七七原声读取失败：error_type={}", type(error).__name__)
        return ToolResult(text="本地七七原声暂时不可用，请使用文字回复。", metadata={"status": "failed"})
    if not speech_enabled():
        return ToolResult(text="管理员已关闭语音，本次不发送，请使用文字回复。", metadata={"status": "disabled"})
    receipt = await durable_send(_tool_context.bot, _tool_context.event, MessageSegment.record(payload))
    mark_voice_sent(tool_reply=matches[0]["text"] or matches[0]["title"])
    return ToolResult(
        text="游戏原声已投递，请勿重复发送。" if receipt is not None else "原声已保存到投递队列，请勿重复发送。",
        structured=structured,
        metadata={"status": "sent" if receipt is not None else "queued"},
    )


def is_bare_mention(bot, event) -> bool:
    if onebot_protocol(bot, event) != "onebot_v11" or getattr(event, "reply", None):
        return False
    message = getattr(event, "original_message", None)
    if message is None:
        message = event_message(event)
    mentioned = False
    for segment in message:
        if segment.type == "at" and str(segment.data.get("qq", "")) == bot_self_id(bot, event):
            mentioned = True
        elif segment.type != "text" or str(segment.data.get("text", "")).strip():
            return False
    return mentioned


async def try_ping_voice(bot, event, matcher) -> bool:
    """Only empty @ calls may play random local dialogue; no synthesis or LLM."""
    if (
        not is_bare_mention(bot, event)
        or not speech_enabled()
        or not config_parser.config.get("speech_auto_enabled", True)
        or random.random() >= config_parser.config.get("speech_ping_rate", 0.15)
    ):
        return False
    from .chat_policy import enforce_conduct
    from .chat_runtime import acknowledge_mention, complete_mention, default_cooldown_store
    from .conversation_slots import conversation_slots
    from .group_switch import group_llm_enabled
    from .request_budget import budget_authorized
    from .request_recovery import has_delivery_receipt

    if not group_llm_enabled(event):
        return False
    authorized = await budget_authorized(bot, event)
    if not await enforce_conduct(bot, event, matcher, authorized=authorized):
        return True
    try:
        root, clips = await asyncio.to_thread(_library)
        # Short everyday lines suit a greeting; combat shouts and long monologues do not.
        choices = [
            clip
            for clip in clips
            if 8 <= len(clip["text"]) <= 70
            and clip["title"].startswith(("初次见面", "闲聊", "早上好", "中午好", "晚上好", "晚安", "下雪", "起风"))
        ]
        if not choices:
            return False
        clip = random.choice(choices)
        payload = await asyncio.to_thread(_audio, root, clip)
    except (OSError, ValueError, KeyError, TypeError, SpeechError):
        return False
    async with conversation_slots.slot(event_user_id(event)) as acquired:
        if not acquired:
            await acknowledge_mention(bot, event, rejected=True)
            await matcher.finish(conversation_slots.busy_message(event_user_id(event)))
            return True
        seconds = 0 if authorized else int(config_parser.config.get("cd_seconds", 30) or 0)
        claim = await default_cooldown_store.claim(user_id=event_user_id(event), event_time=time.time(), cooldown_seconds=seconds)
        if claim.retry_after_seconds:
            await acknowledge_mention(bot, event, rejected=True)
            await matcher.finish(
                f"每人 {seconds} 秒只能唤醒七七一次，请在 {claim.retry_after_seconds} 秒后再试；这条请求已忽略。"
            )
            return True
        if not speech_enabled() or not config_parser.config.get("speech_auto_enabled", True):
            if claim.lease:
                await default_cooldown_store.release(claim.lease)
            return False
        await acknowledge_mention(bot, event)
        receipt = await durable_send(bot, event, MessageSegment.record(payload))
        if has_delivery_receipt(receipt):
            await complete_mention(bot, event)
        return True


QIQI_VOICE_TOOL = ToolSpec(
    name="qiqi_voice",
    description="查找、播放七七的原神游戏中文原声台词。按标题（如晚安、初次见面）、关键词或台词查找；"
    "query留空列出全部标题。先action=search查台词，再按完整标题action=play播放唯一匹配的原声。"
    "播放本地录音不调用合成模型，不消耗合成Token。说新内容请用speak_text。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 200, "description": "中文台词标题或关键词；留空查看全部标题"},
            "action": {"type": "string", "enum": ["search", "play"], "description": "search查询台词，play播放唯一匹配的游戏原声"},
        },
        "required": [],
        "additionalProperties": False,
    },
    handler=qiqi_voice,
    timeout_seconds=15,
    effect=ToolEffect.MUTATING,
    confirmation_mode=ToolConfirmationMode.TRUSTED_LOW_RISK_DIRECT,
    policy=ToolPolicy.configured(ToolCapabilityV2(host_filesystem_read=True, bot_read=True, bot_send=True)),
)


@get_driver().on_startup
async def register_qiqi_voice_tools() -> None:
    if speech_config.moellm_speech_library and tool_registry.get(QIQI_VOICE_TOOL.name) is None:
        register_tool(QIQI_VOICE_TOOL)

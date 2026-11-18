"""Bounded MiMo speech synthesis and OneBot voice delivery."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import tempfile
import time
import uuid
import wave

import aiohttp
from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import MessageSegment
import nonebot_plugin_localstore as store

from .compat import TimeoutError
from .compat import timeout as timeout_scope
from .config import config_parser, speech_config
from .model_error_policy import TRANSIENT_HTTP_STATUSES
from .onebot_facade import onebot_protocol
from .private_files import ensure_private_directory, ensure_private_file
from .request_recovery import durable_reply, durable_send
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
from .usage_ledger import get_ledger, record_usage

ENDPOINT = "https://api.xiaomimimo.com/v1/chat/completions"
MODELS = {
    kind: "mimo-v2.5-tts" + suffix
    for kind, suffix in (
        ("tts", ""),
        ("sing", ""),
        ("voiceclone", "-voiceclone"),
        ("voicedesign", "-voicedesign"),
    )
}
MAX_AUDIO_BYTES = 8 * 1024 * 1024
_slots = asyncio.Semaphore(2)
_reference_lock = asyncio.Lock()
_speech_state: ContextVar[dict | None] = ContextVar("moellm_speech", default=None)
_VOICE_INTENT = re.compile(
    r"(?:想听|听听|听一下|听一听).{0,12}(?:说话|声音|语音)|"
    r"(?:用|发|来|给|转|合成|生成|读成).{0,8}(?:语音|声音)|"
    r"(?:语音|声音).{0,8}(?:回复|回答|说|读|念)|(?:念|朗读|读)(?:一下|给我|一段|这段)|"
    r"(?:说句话|说两句|说一句|开口说话)",
    re.I,
)
# Singing is official only on the preset-voice mimo-v2.5-tts via a leading
# "(唱歌)" tag; used for tool preselection, never for random auto replies.
_SING_INTENT = re.compile(r"(?:唱|哼)(?:歌|一首|一段|一下|个|首|两句|几句)|(?:清唱|哼唱|点歌|来一首)", re.I)
_SING_PREFIX = re.compile(r"^\s*(?:[（(]\s*(?:唱歌|sing|singing)\s*[)）]|<style>\s*唱歌\s*</style>)\s*", re.I)
_UNSPOKEN = re.compile(r"https?://|www\.|[\n`|$\\<>\[\]=∑∫√]|/\S|^\s*[#>*-]|\*\*|__")
# Fast transient failures (rate limit / server side / connection) are retried
# once inside the same budget; slow timeouts leave no budget and just fail.
_TRANSIENT_HTTP = frozenset(TRANSIENT_HTTP_STATUSES) | {429}
_RETRY_BACKOFF_SECONDS = 2.0


class SpeechError(Exception):
    """A safe, user-facing synthesis failure; never includes provider payloads."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@contextmanager
def speech_scope():
    token = _speech_state.set({"sent": False})
    try:
        yield
    finally:
        _speech_state.reset(token)


def speech_enabled() -> bool:
    # Administrative stop must also affect requests pinned to an older snapshot.
    return bool(config_parser.config.get("speech_enabled", True))


def speech_available() -> bool:
    return bool(speech_enabled() and speech_config.mi_api_key.get_secret_value())


def mark_voice_sent(*, tool_reply: str | None = None) -> None:
    if state := _speech_state.get():
        state["sent"] = True
        if tool_reply:
            state["tool_reply"] = tool_reply


def tool_voice_reply() -> str | None:
    """Actual spoken answer; a successful voice tool needs no model postscript."""
    return (_speech_state.get() or {}).get("tool_reply")


def voice_requested(text: str) -> bool:
    return bool(_VOICE_INTENT.search(text)) and not re.search(r"(?:不要|别|不用|不想).{0,6}(?:语音|说话|声音|朗读)", text)


def sing_requested(text: str) -> bool:
    """Explicit singing intent, for offering the tool only (never auto replies)."""
    return bool(_SING_INTENT.search(text)) and not re.search(r"(?:不要|别|不用|不想|不想听).{0,6}唱", text)


def speech_prompt(names: set[str]) -> str:
    original = (
        (
            "\n七七的游戏中文原声已保存在本地。用户要原声、游戏台词时，用 qiqi_voice 搜索标题、"
            "关键词或完整台词；action=search 只查，action=play 发送唯一匹配的原声。"
            "不要以合成语音冒充游戏录音。查到后可在下一轮按完整标题播放。播放后不再追加文字确认或客套话。\n"
        )
        if "qiqi_voice" in names
        else ""
    )
    if "speak_text" not in names:
        return original
    return original + (
        "\n语音能力：用户想听你说话、要求用语音回复新内容或朗读时，调用 speak_text 发 QQ 语音。"
        "text 是实际要说的自然中文，短句、轻声、稍慢、自然停顿，保留七七安静呆萌的性格，"
        "不念动作描写、Markdown、表情标签或舞台提示。不要只用文字假装说过或发送过语音。"
        "默认 model=auto 固定七七音色；指定普通合成、声音设计、复刻时再选对应模式。"
        "唱歌：用户想听你唱歌、点歌、哼唱时用 model=sing，text 填中文歌词（建议副歌短片段或自创歌词，"
        "不要自带(唱歌)标签或和声、动作标记），style 用自然语言描述演唱风格（如欢快、抒情）。"
        "唱歌使用预置音色而非七七复刻音色；长歌词合成较慢，可能需要等待一分钟。"
        "合成失败时系统已自动重试一次，工具返回失败后不要在同一轮里连续重调，改用文字回复或让用户稍后再说。"
        "不根据称呼认主人，主人身份仍以系统提供的 QQ 为准。成功后结束回复，不追加文字确认、复述或客套话。\n"
    )


def _validate_audio(data: bytes) -> bytes:
    if not 44 < len(data) <= MAX_AUDIO_BYTES:
        raise SpeechError("语音大小异常，本次未发送。")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if not 0.1 <= duration <= 120 or audio.getnchannels() not in {1, 2}:
                raise ValueError
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        raise SpeechError("语音格式异常，本次未发送。") from None
    return data


def build_payload(text: str, kind: str, *, reference: bytes | None = None, style: str = "") -> dict:
    if kind not in MODELS:
        raise SpeechError("不支持的语音模型。")
    audio = {"format": "wav"}
    if kind == "voiceclone":
        if not reference:
            raise SpeechError("尚未配置复刻参考音频。")
        audio["voice"] = "data:audio/wav;base64," + base64.b64encode(reference).decode("ascii")
    elif kind in {"tts", "sing"}:
        # Singing is only supported by the preset-voice mimo-v2.5-tts, never clones.
        audio["voice"] = speech_config.moellm_sing_voice if kind == "sing" else speech_config.moellm_speech_voice
    else:
        audio["optimize_text_preview"] = True
    if kind == "sing":
        # The leading "(唱歌)" tag must be the very first thing read; style
        # directions go to the user message as natural language instead of
        # being stacked inside the tag (stacking garbles the singing).
        lyrics = _SING_PREFIX.sub("", text).strip()
        messages = ([{"role": "user", "content": style}] if style else []) + [{"role": "assistant", "content": "(唱歌)" + lyrics}]
        return {"model": MODELS[kind], "stream": False, "messages": messages, "audio": audio}
    return {
        "model": MODELS[kind],
        "stream": False,
        "messages": [
            {"role": "user", "content": speech_config.moellm_speech_style + style},
            {"role": "assistant", "content": text},
        ],
        "audio": audio,
    }


async def _request_audio(payload: dict, *, timeout_seconds: float | None = None) -> bytes:
    from . import personal_billing

    if personal_billing.active():
        raise SpeechError("语音接口尚未接入个人计费，本次未调用公共密钥。")
    if not speech_available():
        raise SpeechError("语音合成已关闭或未配置 mi_api_key。")
    # The kind-specific budget must reach the HTTP layer: singing easily runs
    # past the speaking budget, and a hardcoded one silently killed long songs.
    budget = timeout_seconds if timeout_seconds is not None else speech_config.moellm_speech_timeout_seconds
    model = {"model": payload["model"], "provider": "MiMo"}
    attempt_id = uuid.uuid4().hex
    await record_usage(model, None, purpose="speech", attempt_id=attempt_id, status="started_usage_unknown")
    status = "interrupted_usage_unknown"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=budget), trust_env=False) as session:
            async with session.post(
                ENDPOINT,
                json=payload,
                allow_redirects=False,
                headers={
                    "Authorization": "Bearer " + speech_config.mi_api_key.get_secret_value(),
                },
            ) as response:
                status = f"http_{response.status}"
                if response.status != 200:
                    raise SpeechError(
                        f"语音服务暂时不可用（HTTP {response.status}），请稍后再试。",
                        transient=response.status in _TRANSIENT_HTTP,
                    )
                # Base64 is larger than decoded WAV. Never buffer an unbounded response.
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES * 4 // 3 + 131072:
                        raise SpeechError("语音响应过大，本次已停止。")
                    chunks.append(chunk)
                body = json.loads(b"".join(chunks))
                await record_usage(model, body.get("usage"), purpose="speech", attempt_id=attempt_id)
                status = "recorded"
                encoded = body["choices"][0]["message"]["audio"]["data"]
                return _validate_audio(base64.b64decode(encoded, validate=True))
    except asyncio.CancelledError:
        raise
    except SpeechError:
        raise
    except (aiohttp.ClientError, TimeoutError):
        raise SpeechError("语音服务连接超时或中断，请稍后再试。", transient=True) from None
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error):
        raise SpeechError("语音服务未返回可播放的音频。") from None
    finally:
        if status != "recorded":
            await asyncio.shield(record_usage(model, None, purpose="speech", attempt_id=attempt_id, status=status))


def reference_path() -> Path:
    if speech_config.moellm_speech_reference:
        return Path(speech_config.moellm_speech_reference).expanduser()
    identity = hashlib.sha256(speech_config.moellm_speech_style.encode()).hexdigest()[:16]
    return store.get_plugin_data_dir() / "speech" / f"voice-{identity}.wav"


def _read_reference(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise SpeechError("参考音频超过大小限制。")
    return _validate_audio(path.read_bytes())


async def _reference() -> bytes:
    async with _reference_lock:
        path = reference_path()
        if existing := await asyncio.to_thread(_read_reference, path):
            return existing
        if speech_config.moellm_speech_reference:
            raise SpeechError("管理员配置的参考音频不存在。")
        data = await _request_audio(
            build_payload(
                "你好……我是七七。事情太多，要记在本子上。别着急，我会慢慢听，认真帮你。今天，也要照顾好自己。", "voicedesign"
            ),
            timeout_seconds=speech_config.moellm_speech_timeout_seconds,
        )

        def save():
            ensure_private_directory(path.parent)
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".voice-")
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                Path(temporary).replace(path)
                ensure_private_file(path)
            finally:
                Path(temporary).unlink(missing_ok=True)

        await asyncio.to_thread(save)
        return data


async def synthesize(text: str, *, kind: str = "auto", style: str = "") -> bytes:
    if not speech_available():
        raise SpeechError("语音合成未启用或未配置 mi_api_key。")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300 or "[CQ:" in text:
        raise SpeechError("每次语音需要 1 至 300 字的纯文本。")
    if not isinstance(style, str) or len(style) > 200:
        raise SpeechError("语气提示最多 200 字。")
    if kind not in {"auto", *MODELS}:
        raise SpeechError("不支持的语音模型。")
    if _slots.locked():
        raise SpeechError("语音合成正在忙，请稍后再试。")
    async with _slots:
        budget = speech_config.moellm_sing_timeout_seconds if kind == "sing" else speech_config.moellm_speech_timeout_seconds
        async with timeout_scope(budget):
            actual = "voiceclone" if kind == "auto" else kind
            reference = await _reference() if actual == "voiceclone" else None
            payload = build_payload(text.strip(), actual, reference=reference, style=style)
            try:
                return await _request_audio(payload, timeout_seconds=budget)
            except SpeechError as error:
                if not error.transient:
                    raise
            # One bounded retry for transient failures only; the outer budget
            # still bounds both attempts together, so slow timeouts never
            # double the wait.
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
            return await _request_audio(payload, timeout_seconds=budget)


async def speak_text(*, _tool_context: ToolContext, text: str, model: str = "auto", style: str = "") -> ToolResult:
    from . import personal_billing

    async with personal_billing.event_scope(_tool_context.bot, _tool_context.event):
        return await _speak_text(_tool_context=_tool_context, text=text, model=model, style=style)


async def _speak_text(*, _tool_context: ToolContext, text: str, model: str = "auto", style: str = "") -> ToolResult:
    if onebot_protocol(_tool_context.bot, _tool_context.event) != "onebot_v11":
        return ToolResult(text="当前平台不支持 QQ 语音发送，请使用文字回复。", metadata={"status": "failed"})
    try:
        payload = await synthesize(text, kind=model, style=style)
    except (SpeechError, TimeoutError) as error:
        message = str(error) or "合成超时"
        if isinstance(error, SpeechError) and error.transient:
            hint = "已自动重试一次。请改用文字回复，或建议用户稍后再说一次；不要在本轮连续重调。"
        elif model == "sing":
            hint = "长歌词合成较慢，可缩短歌词（如只唱副歌）后重试，或改用文字回复；不要在本轮连续重调。"
        else:
            hint = "请改用文字回复。"
        return ToolResult(text=f"语音未生成：{message}。{hint}", metadata={"status": "failed"})
    except Exception as error:
        logger.warning("语音合成失败：error_type={}", type(error).__name__)
        return ToolResult(text="语音暂时无法生成，请改用文字回复。", metadata={"status": "failed"})
    if not speech_available():
        return ToolResult(text="管理员已关闭语音，本次不发送，请使用文字回复。", metadata={"status": "failed"})
    # Sending is outside the synthesis fallback. An uncertain send must not be retried as another voice.
    receipt = await durable_send(_tool_context.bot, _tool_context.event, MessageSegment.record(payload))
    mark_voice_sent(tool_reply=text)
    if receipt is None:
        return ToolResult(text="语音已保存到投递队列，等待恢复发送；不要重复投递。", metadata={"status": "queued"})
    return ToolResult(text="已生成并投递 QQ 语音，不要重复这段内容。", metadata={"status": "sent", "model": model})


async def try_voice_reply(chat, content: str) -> bool:
    """Return False only before a send; preserve the text in conversation history."""
    if (
        not speech_available()
        or onebot_protocol(chat.bot, chat.event) != "onebot_v11"
        or getattr(chat, "_speech_attempted", False)
        or (_speech_state.get() or {}).get("sent", False)
        or getattr(chat, "_current_tool_usage", {})
        or getattr(chat, "_defer_model_output", False)
        or getattr(chat, "_deferred_commands", [])
        or getattr(chat, "is_objective", False)
    ):
        return False
    text = " ".join(getattr(chat, "format_message_dict", {}).get("text", []))
    explicit = voice_requested(text)
    maximum = 300 if explicit else min(80, config_parser.config.get("speech_auto_max_chars", 40))
    if not content.strip() or len(content) > maximum or _UNSPOKEN.search(content):
        return False
    if not explicit and (
        not config_parser.config.get("speech_auto_enabled", True)
        or random.random() >= config_parser.config.get("speech_auto_rate", 0.15)
    ):
        return False
    chat._speech_attempted = True
    try:
        remaining = chat._remaining_request_seconds()
        if remaining < 5:
            return False
        async with timeout_scope(min(remaining - 2, speech_config.moellm_speech_timeout_seconds)):
            payload = await synthesize(content)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.warning("LLM 语音未生成，保留文字回复：error_type={}", type(error).__name__)
        return False
    if not speech_available() or (not explicit and not config_parser.config.get("speech_auto_enabled", True)):
        return False
    await durable_reply(chat.bot, chat.event, [MessageSegment.record(payload)])
    mark_voice_sent()
    return True


SPEAK_TOOL = ToolSpec(
    name="speak_text",
    description="七七开口说话、语音回复、朗读文本或唱歌，用 MiMo 合成并直接发送 QQ 语音。无需绘图授权。已发送后勿重复。",
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
                "description": "实际朗读的自然中文；唱歌时填中文歌词（建议副歌短片段，勿自带(唱歌)标签）",
            },
            "model": {
                "type": "string",
                "enum": ["auto", "tts", "sing", "voiceclone", "voicedesign"],
                "description": "默认auto固定七七音色；sing唱歌（预置音色）；tts普通合成、voiceclone复刻、voicedesign声音设计",
            },
            "style": {
                "type": "string",
                "maxLength": 200,
                "description": "可选语气/演唱风格的自然语言描述，如轻声、欢快地唱、抒情",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    handler=speak_text,
    timeout_seconds=150,
    effect=ToolEffect.MUTATING,
    confirmation_mode=ToolConfirmationMode.TRUSTED_LOW_RISK_DIRECT,
    policy=ToolPolicy.configured(
        ToolCapabilityV2(
            network_allow=("api.xiaomimimo.com",),
            secret_names=("mi_api_key",),
            host_filesystem_read=True,
            host_filesystem_write=True,
            bot_read=True,
            bot_send=True,
        )
    ),
)


@get_driver().on_startup
async def register_speech_tools() -> None:
    if not speech_config.mi_api_key.get_secret_value():
        logger.info("LLM 语音工具未配置 mi_api_key")
        return
    if tool_registry.get(SPEAK_TOOL.name) is None:
        register_tool(SPEAK_TOOL)
    price = {
        "source": "mimo_limited_free",
        "currency": "CNY",
        "per_tokens": 1000000,
        "input": "0",
        "output": "0",
        "cached": "0",
        "at": time.time(),
        "estimate_only": True,
    }
    if not speech_config.moellm_speech_free_pricing:
        price = {"source": "mimo_price_unknown", "at": time.time()}
    await asyncio.to_thread(get_ledger().put_prices, {f"{name} (MiMo)": price for name in MODELS.values()})
    logger.info("LLM 语音工具已注册：speak_text（MiMo 三种合成模式 + 唱歌）")

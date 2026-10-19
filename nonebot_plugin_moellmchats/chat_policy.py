"""Host-independent chat conduct rules. Only the real caller's text is judged."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import time
import unicodedata

from nonebot.log import logger
import nonebot_plugin_localstore as store

from .config import config_parser
from .compat import timeout
from .onebot_facade import bot_self_id, event_group_id, event_message, event_message_id, event_user_id, onebot_protocol
from .private_files import atomic_write_private_text, ensure_private_directory, ensure_private_file

# Single characters such as 口/抠 and discussions of health/game skills are not
# offences. Match explicit requests and direct insults, never quoted context.
_TAUNT = re.compile(r"(?:你|七七).{0,8}(?:有本事|有胆|敢不敢|能耐).{0,8}(?:禁言|封禁|禁我)|(?:有本事|有胆|敢不敢).{0,6}禁言我")
_ABUSE = re.compile(r"傻[逼比屄]|煞笔|沙比|脑残|弱智|废物|贱[人货]|死妈|[操艹草]你[妈吗]|去死|滚[开吧啊]|\b(?:fuck\s+you|stfu)\b", re.I)
_DIRECT_ABUSE = re.compile(
    r"(?:你|七七|机器人|bot)(?:就是|真是|是|个|大|这|真|太|一个|能不能|给我|赶紧|他妈的|的|怎么这么|\s){0,6}(?:"
    + _ABUSE.pattern + r")", re.I)
_SEXUAL = re.compile(r"(?:帮我|给我|我要|想要|快来|求|能不能帮我|可以帮我|请).{0,12}(?:口交|性交|做爱|打炮|手淫|自慰|射精|颜射|内射|乳交|足交|裸照|裸体|色情|黄片|黄图|涩图|色图)|(?:给我|帮我|为我).{0,4}(?:口一下|口一[口下]|口\b|抠[逼屄]|抠穴|打胶)|(?:r[ -]?18|nsfw).{0,8}(?:图|内容|视频)|(?:画|生成|发|来).{0,8}(?:r[ -]?18|nsfw)|^(?:七七[，, ]*)?(?:帮我|给我|快来|来|请)?(?:口|抠|射精)(?:一下|我|嘛|吧)?[!！。，?？ ]*$", re.I)
_GUARD = threading.RLock()


def current_text(event) -> str:
    # Reply messages, forwarded nodes and render data are deliberately absent.
    return "".join(str(segment.data.get("text", "")) for segment in event_message(event)
                   if getattr(segment, "type", "") == "text")[:8000]


def conduct_reason(text: str) -> str | None:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    # Do not punish someone asking about or reporting a phrase/code sample.
    text = re.sub(r'```[\s\S]*?```|「[^」]*」|“[^”]*”|"[^"\n]*"', "", text)
    if _TAUNT.search(text):
        return "taunt"
    sexual = _SEXUAL.search(text)
    educational = re.search(r"科普|医学|健康|原理|解释.{0,8}(?:意思|含义)|不要.{0,6}(?:色情|色图)", text)
    explicit_output = re.search(r"(?:给我|帮我|画|生成|发|来).{0,8}(?:口交|做爱|色图|涩图|黄片|裸照|裸体|口一下|抠[逼屄穴])", text)
    if sexual and (not educational or explicit_output):
        return "sexual"
    reporting = re.search(r"有人(?:说|骂)|举报|被人骂|怎么回应|是什么意思", text)
    if not reporting and (_DIRECT_ABUSE.search(text) or _ABUSE.fullmatch(text.strip(' ，。！!？?'))):
        return "abuse"
    return None


class ConductStore:
    """Small private file, cross-process lock and atomic replacement; no chat bodies."""
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def transaction(self):
        import fcntl
        ensure_private_directory(self.path.parent)
        with _GUARD, self.path.with_suffix(".lock").open("a+") as lock:
            self.path.with_suffix(".lock").chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    ensure_private_file(self.path)
                    data = json.loads(self.path.read_text())
                    if not isinstance(data, dict):
                        raise ValueError("invalid conduct store")
                else:
                    data = {}
                yield data
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def blocked(self, user_id: str) -> bool:
        if not self.path.exists():
            return False
        with self.transaction() as data:
            return data.get(user_id, {}).get("blocked") is True

    def strike(self, user_id: str, identity: str, reason: str) -> tuple[int, bool]:
        with self.transaction() as data:
            row = data.setdefault(user_id, {"strikes": 0, "blocked": False, "events": []})
            if identity in row["events"] or row["blocked"]:
                return row["strikes"], False
            row["events"] = [*row["events"][-19:], identity]
            if reason != "taunt":
                row["strikes"] += 1
            row.update(blocked=row["strikes"] >= 3, reason=reason, updated_at=time.time())
            atomic_write_private_text(self.path, json.dumps(data, ensure_ascii=False))
            return row["strikes"], True

    def clear(self, user_id: str) -> None:
        with self.transaction() as data:
            data.pop(user_id, None)
            atomic_write_private_text(self.path, json.dumps(data, ensure_ascii=False))

    def info(self, user_id: str) -> dict:
        with self.transaction() as data:
            row = data.get(user_id, {})
            return {"strikes": row.get("strikes", 0), "blocked": row.get("blocked", False)}


def conduct_store() -> ConductStore:
    return ConductStore(store.get_plugin_data_dir() / "chat-conduct.json")


async def user_blocked(bot, event) -> bool:
    from .request_budget import budget_authorized
    if await budget_authorized(bot, event):
        return False
    return await asyncio.to_thread(conduct_store().blocked, event_user_id(event))


async def mute_caller(bot, event, seconds: int) -> bool:
    """One attempt only, and only with fresh proof that both roles allow it."""
    group = event_group_id(event)
    if group is None or onebot_protocol(bot, event) != "onebot_v11":
        return False
    try:
        async with timeout(5):
            own, member = await asyncio.gather(
                bot.call_api("get_group_member_info", group_id=int(group), user_id=int(bot_self_id(bot, event)), no_cache=True),
                bot.call_api("get_group_member_info", group_id=int(group), user_id=int(event_user_id(event)), no_cache=True))
            if own.get("role") not in {"admin", "owner"} or member.get("role") != "member":
                return False
            await bot.call_api("set_group_ban", group_id=int(group), user_id=int(event_user_id(event)), duration=seconds)
            return True
    except Exception as error:
        logger.warning("LLM 违规禁言未确认，未重试：error_type={}", type(error).__name__)
        return False


async def enforce_conduct(bot, event, matcher, *, authorized: bool) -> bool:
    if authorized or not config_parser.get_config("abuse_guard_enabled", True):
        return True
    storage, user_id = conduct_store(), event_user_id(event)
    if await asyncio.to_thread(storage.blocked, user_id):
        await matcher.finish("你的 AI 使用权限已被永久停用，请联系超级管理员处理。")
        return False
    reason = conduct_reason(current_text(event))
    if reason is None:
        return True
    identity = hashlib.sha256(f"{bot_self_id(bot, event)}:{event_group_id(event)}:{event_message_id(event)}".encode()).hexdigest()
    count, fresh = await asyncio.to_thread(storage.strike, user_id, identity, reason)
    if not fresh:
        return False
    if reason == "taunt":
        seconds = 60 + secrets.randbelow(121)
        muted = await mute_caller(bot, event, seconds)
        text = f"请友善交流。已禁言 {seconds} 秒。" if muted else "请友善交流，不要挑衅群管理。"
    elif count >= 3:
        muted = await mute_caller(bot, event, 120)
        text = "第三次违规：你的 AI 使用权限已永久停用。" + ("已禁言 2 分钟。" if muted else "")
    elif count == 2:
        text = "第二次违规警告：请停止色情、辱骂等内容，再次违规将永久停用 AI 权限。"
    else:
        text = "第一次提醒：请友善交流，不要向七七发送色情或辱骂内容。"
    await matcher.finish(text)
    return False

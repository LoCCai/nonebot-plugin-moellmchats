"""Keep verified query targets separate from request-local mention indexes."""

from collections.abc import Mapping
import re
import uuid

from .onebot_facade import bot_self_id, event_group_id, event_user_id, onebot_protocol

HISTORY_TARGET_LIMIT = 12
HISTORY_TOKEN = re.compile(r"[a-f0-9]{32}")


def mention_scope(event) -> dict[str, str] | None:
    user = event_user_id(event)
    bot = bot_self_id(None, event)
    protocol = onebot_protocol(None, event)
    if not user or not bot or not protocol:
        return None
    return {
        "user": user,
        "bot": bot,
        "protocol": protocol,
        "group": event_group_id(event) or "",
        "guild": str(getattr(event, "guild_id", "") or ""),
        "channel": str(getattr(event, "channel_id", "") or ""),
    }


def verified_mention_context(source: dict, scope: dict | None) -> dict | None:
    if scope is None:
        return None
    targets = []
    seen = set()
    for target in [*(source.get("mentions") or []), source.get("reply_user") or {}]:
        if not isinstance(target, Mapping):
            continue
        qq = str(target.get("qq") or "")
        if not qq or qq == scope["bot"] or qq in seen:
            continue
        if len(qq) > 128 or any(ord(char) < 32 for char in qq):
            continue
        if scope["protocol"] == "onebot_v11" and (not qq.isascii() or not qq.isdecimal() or int(qq) <= 0):
            continue
        seen.add(qq)
        targets.append({
            "token": uuid.uuid4().hex,
            "qq": qq,
            "name": str(target.get("name") or "群友").replace("\x00", "")[:80],
        })
        if len(targets) >= HISTORY_TARGET_LIMIT:
            break
    return {"version": 1, "scope": dict(scope), "targets": targets}


def historical_targets(context: object, scope: dict | None) -> list[dict]:
    if (
        scope is None
        or not isinstance(context, Mapping)
        or context.get("version") != 1
        or context.get("scope") != scope
    ):
        return []
    targets = context.get("targets")
    if not isinstance(targets, list):
        return []
    result = []
    for target in targets[:HISTORY_TARGET_LIMIT]:
        if not isinstance(target, Mapping):
            continue
        token, qq, name = target.get("token"), target.get("qq"), target.get("name")
        if (
            not isinstance(token, str) or HISTORY_TOKEN.fullmatch(token) is None
            or not isinstance(qq, str) or not qq or len(qq) > 128
            or any(ord(char) < 32 for char in qq)
            or not isinstance(name, str)
        ):
            continue
        if scope["protocol"] == "onebot_v11" and (not qq.isascii() or not qq.isdecimal() or int(qq) <= 0):
            continue
        result.append({"token": token, "qq": qq, "name": name.replace("\x00", "")[:80]})
    return result

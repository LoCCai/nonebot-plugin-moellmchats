"""Persistent group access, checked independently of request snapshots."""
from __future__ import annotations

import re

from .config import config_parser
from .onebot_facade import event_group_id, event_scene

GROUP_ID_PATTERN = r"[1-9][0-9]{0,19}"
COMMAND_PATTERN = r"^[!！/#]?(开启|关闭|查看)(?:群聊|群)?LLM(?:\s+(.*))?\s*$"
GROUP_SWITCH_HELP = "格式：开启LLM / 关闭LLM / 查看LLM [群号或全部]；群内可省略群号，私聊需指定。"


def group_llm_enabled(event) -> bool:
    if event_scene(event) not in {"group", "channel"}:
        return True
    policy = config_parser.config.get("group_chat_policy", {})
    group_id = event_group_id(event)
    return policy.get("groups", {}).get(group_id, policy.get("default_enabled", True))


def manage_group_llm(raw: str, event) -> str:
    """Called only by the fixed SUPERUSER matcher, without a model request."""
    match = re.fullmatch(COMMAND_PATTERN, raw.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError(GROUP_SWITCH_HELP)
    action, target = match.groups()
    target = (target or event_group_id(event) or "").strip()
    if target != "全部" and not re.fullmatch(GROUP_ID_PATTERN, target):
        raise ValueError(GROUP_SWITCH_HELP)
    policy = config_parser.config.get("group_chat_policy", {})
    default = policy.get("default_enabled", True)
    groups = dict(policy.get("groups", {}))
    if action == "查看":
        if target == "全部":
            enabled = sum(value is True for value in groups.values())
            disabled = sum(value is False for value in groups.values())
            return (f"群聊 LLM 默认{'开启' if default else '关闭'}；"
                    f"单独开启 {enabled} 个群，单独关闭 {disabled} 个群。")
        return f"群 {target} 的 LLM 已{'开启' if groups.get(target, default) else '关闭'}。"
    enabled = action == "开启"
    if target == "全部":
        policy = {"default_enabled": enabled, "groups": {}}
    else:
        groups[target] = enabled
        policy = {"default_enabled": default, "groups": groups}
    config_parser.set_config("group_chat_policy", policy)
    scope = "全部群聊（含以后加入的群）" if target == "全部" else f"群 {target}"
    return f"已{'开启' if enabled else '关闭'}{scope}的 LLM 功能。"

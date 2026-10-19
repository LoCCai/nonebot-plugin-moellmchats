"""One native feature delivery per ordinary request, with copyable follow-ups."""
from __future__ import annotations

import json

from .config import config_parser
from .request_budget import ordinary_request
from .tool_manager import LlmToolExecutionRoute

POLICY_PROMPT = (
    "\n普通用户本次只执行并投递请求中的第一个功能。多个榜单、卡片或插件功能，"
    "其余不执行，使用完整的原生指令（含/前缀及所有参数）逐条回复，告诉用户复制粘贴即可触发。"
    "后续指令必须为普通文字，禁止渲染成图片、表格图片或代码截图。"
    "必要的只读数据查询可继续，但不得换用其他工具、合并指令或循环执行来投递其余功能。"
    "同一功能本身的分页、长图切片及合并转发由原插件处理，不要额外重复投递。\n"
)


def single_delivery_enabled() -> bool:
    return ordinary_request() and config_parser.get_config("ordinary_single_delivery", True)


def reserve_deliveries(chat, calls: list) -> dict[str, str]:
    if not single_delivery_enabled():
        return {}
    reserved = getattr(chat, "_ordinary_delivery_reserved", None)
    deferred = {}
    commands = getattr(chat, "_deferred_commands", [])
    for call in calls:
        name = call.get("function", {}).get("name", "")
        view = chat.tool_snapshot.resolve_llm_tool_execution(name, is_superuser=chat._verified_is_superuser())
        if view is None:
            continue
        native = view.route is LlmToolExecutionRoute.NONEBOT_PLUGIN
        output = name in {"render_markdown", "render_html", "screenshot_url", "generate_image", "edit_image", "generate_meme"}
        if not native and not output:
            continue
        try:
            args = json.loads(call["function"]["arguments"])
        except (ValueError, TypeError):
            continue  # normal argument validation owns this failure
        if not isinstance(args, dict) or native and not isinstance(args.get("command"), str):
            continue
        identity = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if reserved is None:
            reserved = identity
            chat._ordinary_delivery_reserved = identity
        elif identity != reserved:
            command = args.get("command") if native else None
            if command and command not in commands and len(commands) < 12:
                commands.append(command)
            deferred[str(call.get("id", ""))] = (
                "本次只投递第一个功能，该功能未执行。"
                + (f"请用纯文字告诉用户复制粘贴即可触发：{command}" if command else "不要继续渲染，直接文字回复。")
            )
    chat._deferred_commands = commands
    return deferred


def pending_commands(chat) -> str:
    commands = getattr(chat, "_deferred_commands", [])
    sent = getattr(chat, "_deferred_commands_sent", 0)
    if len(commands) <= sent:
        return ""
    chat._deferred_commands_sent = len(commands)
    # Escape CQ syntax so a model-supplied string cannot inject message segments.
    values = [" ".join(command.replace("[CQ:", "［CQ:").split()) for command in commands[sent:]]
    return "本次只帮你投递第一个功能，其余可复制粘贴以下指令触发：\n" + "\n".join(values)

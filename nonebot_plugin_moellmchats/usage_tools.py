"""Read the local accounting ledger without dispatching the LLM plugin itself."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import shlex

from nonebot.permission import SUPERUSER

from .onebot_facade import event_group_id, event_user_id
from .tool_contracts import ToolContext, ToolEffect, ToolResult, ToolSpec, register_tool
from .usage_reports import cost_summary, draw_report, format_report, report_data


def is_usage_request(text: str) -> bool:
    """Recognize accounting requests, including the existing Chinese commands."""
    return bool(re.search(
        r"(?:模型|AI|LLM|Token)[\s的]*(?:消耗|用量|费用|开销|花费|账单)"
        r"|(?:消耗|用量)[\s的]*(?:统计|趋势|占比|图表|图)",
        text, re.IGNORECASE,
    ))


async def query_model_usage(
    *, _tool_context: ToolContext, period: str = "本月", chart: bool = True,
    group_id: str = "", user_id: str = "", model: str = "",
) -> ToolResult:
    # The caller's actual event is the authority, never an argument from the LLM.
    context = _tool_context
    if context.bot is None or context.event is None or not await SUPERUSER(context.bot, context.event):
        return ToolResult(text="模型消耗统计仅限超级管理员查看。", metadata={"status": "permission_denied"})
    words = [period]
    for key, value in (("群", group_id), ("用户", user_id), ("模型", model)):
        if not value:
            continue
        if key == "群" and value == "本群":
            value = event_group_id(context.event)
            if value is None:
                return ToolResult(text="当前是私聊，请指定群号。", metadata={"status": "invalid_arguments"})
        if key == "用户" and value in {"我", "自己"}:
            value = event_user_id(context.event)
        words.extend((key, value))
    try:
        data = await asyncio.to_thread(report_data, shlex.join(words))
    except ValueError as error:
        return ToolResult(text=str(error), metadata={"status": "invalid_arguments"})
    # Return data alongside the chart; no image bytes/URLs go back into vision.
    summary = format_report(data)
    names = sorted(data["model_totals"], key=data["model_totals"].get, reverse=True)
    structured = {
        "period": data["period"], "timezone": "UTC",
        "range_utc": {key: datetime.fromtimestamp(data["filters"][key], timezone.utc).isoformat()
                      for key in ("since", "until")},
        "calls": data["calls"], "total_tokens": data["total"],
        "prompt_tokens": data["prompt"], "completion_tokens": data["completion"],
        "cached_tokens": data["cache"], "reasoning_tokens": data["reasoning"],
        "unknown_usage_calls": data["unknown"], "unpriced_calls": data["unpriced"],
        "estimated_cost": cost_summary(data),
        "trend": [{"date": day, "tokens": data["days"][day]}
                  for day in sorted(data["days"])[-93:]],
        "trend_truncated": len(data["days"]) > 93,
        "models": [{"model": name, "tokens": data["model_totals"][name],
                    "share": data["model_totals"][name] / data["total"] if data["total"] else 0,
                    "calls": data["raw_model_calls"][name],
                    "providers": sorted(data["platforms"][name]),
                    "estimated_cost_cny": str(data["model_cost_cny"][name]),
                    "priced_calls": data["model_priced"][name]}
                   for name in names[:60]],
        "model_count": len(names), "models_truncated": len(names) > 60,
    }
    if not chart:
        return ToolResult(text=summary, structured=structured, metadata={"status": "success"})
    try:
        payload = await asyncio.to_thread(draw_report, data)
    except (ImportError, ValueError, RuntimeError):
        return ToolResult(text="统计图生成失败，以下是账本的文字统计：\n" + summary,
                          structured=structured, metadata={"status": "chart_unavailable"})
    from nonebot.adapters.onebot.v11 import MessageSegment
    from .request_recovery import durable_send

    result = ToolResult(
        text="模型消耗图已发送到当前会话，包含各模型占比、趋势和折算费用。"
             "请直接根据以下账本文字解读，无需读图、搜索聊天记录或重复投递。\n" + summary,
        structured=structured, metadata={"status": "success", "delivered": True},
    )
    # Validate the result before delivery, and propagate uncertain sends through
    # the durable tool boundary without generating a second image.
    await durable_send(context.bot, context.event, MessageSegment.image(payload))
    return result


USAGE_TOOL = ToolSpec(
    name="query_model_usage",
    description=("超级管理员查看七七本地账本的模型消耗、Token用量、缓存、费用及占比趋势图。"
                 "‘看一下这个月的模型消耗图’用period=本月、chart=true；"
                 "‘LLM隐藏菜单里的AI消耗统计’也用本工具。默认全群与私聊，支持按群、QQ、模型筛选。"
                 "数据来自本机持久账本，返回文字数据并直接发送原有图表；"
                 "不要改用消息搜索、dbhelp或TokenHub能力目录，不需要用户提供截图。"),
    parameters={"type": "object", "properties": {
        "period": {"type": "string", "enum": ["本月", "周", "月", "年", "全部"],
                   "description": "本月为UTC自然月截至当前；周/月/年为近7/30/365天。默认本月。"},
        "chart": {"type": "boolean", "description": "是否发送统计图，默认true；false只返回文字数据。"},
        "group_id": {"type": "string", "description": "可选群号或本群；省略为全部群与私聊。"},
        "user_id": {"type": "string", "description": "可选QQ号或我；省略为全部用户。"},
        "model": {"type": "string", "description": "可选模型名；省略为全部模型。"},
    }, "required": [], "additionalProperties": False},
    handler=query_model_usage, permission="superuser", effect=ToolEffect.READ_ONLY,
    timeout_seconds=60, result_limit=24000,
)

register_tool(USAGE_TOOL)

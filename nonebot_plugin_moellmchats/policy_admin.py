"""Superuser controls; none are exposed to the model as tools."""

import asyncio
import re

from nonebot import on_command, on_regex
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .chat_policy import conduct_store
from .config import config_parser
from .failed_requests import failed_requests

unblock = on_command("解除LLM拉黑", permission=SUPERUSER, priority=10, block=True)
conduct = on_command("查看LLM警告", permission=SUPERUSER, priority=10, block=True)
failures = on_command("查看LLM失败请求", permission=SUPERUSER, priority=10, block=True)
retry = on_command("重试LLM请求", permission=SUPERUSER, priority=10, block=True)
speech_control = on_regex(
    r"^[!！/#]?(开启|关闭|查看)LLM(随机)?语音\s*$", flags=re.I, permission=SUPERUSER, priority=0, block=True
)


@speech_control.handle()
async def _(event: MessageEvent):
    match = re.fullmatch(r"[!！/#]?(开启|关闭|查看)LLM(随机)?语音", event.get_plaintext().strip(), re.I)
    action, random_only = match.groups()
    if action != "查看":
        key = "speech_auto_enabled" if random_only else "speech_enabled"
        try:
            config_parser.set_config(key, action == "开启")
        except (OSError, ValueError):
            await speech_control.finish("语音开关保存失败，设置未更改，请检查配置文件。")
    settings = config_parser.config
    enabled = settings.get("speech_enabled", True)
    auto = settings.get("speech_auto_enabled", True)
    await speech_control.finish(
        f"LLM 语音总开关：{'开启' if enabled else '关闭'}（全部群聊与私聊，三种语音模型与游戏原声）。\n"
        f"短回复随机语音：{'开启' if auto else '关闭'}；"
        f"概率 {settings.get('speech_auto_rate', 0.15):.0%}，最多 {settings.get('speech_auto_max_chars', 40)} 字。\n"
        f"仅 @ 无正文时播放游戏原声的概率：{settings.get('speech_ping_rate', 0.15):.0%}。\n"
        + ("总开关关闭期间不合成或发送新语音，改用文字回复。" if not enabled else "随机语音关闭时，用户明确要求的语音仍可使用。")
    )


@unblock.handle()
async def _(args: Message = CommandArg()):
    user = args.extract_plain_text().strip()
    if not re.fullmatch(r"[1-9][0-9]{4,11}", user):
        await unblock.finish("格式：解除LLM拉黑 QQ号")
    await asyncio.to_thread(conduct_store().clear, user)
    await unblock.finish(f"已清除 QQ {user} 的 AI 警告和拉黑状态。")


@conduct.handle()
async def _(args: Message = CommandArg()):
    user = args.extract_plain_text().strip()
    if not re.fullmatch(r"[1-9][0-9]{4,11}", user):
        await conduct.finish("格式：查看LLM警告 QQ号")
    row = await asyncio.to_thread(conduct_store().info, user)
    await conduct.finish(f"QQ {user}：违规 {row['strikes']} 次，" + ("AI 已永久停用。" if row["blocked"] else "未被拉黑。"))


@failures.handle()
async def _():
    rows = await failed_requests.list()
    await failures.finish(
        "\n".join(
            f"{row['id']}\nQQ {row['user_id']} · {row['status']} · " + ("可安全重试" if row["retryable"] else row["reason"])
            for row in rows[:10]
        )
        or "暂无失败请求。"
    )


@retry.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    try:
        result = await failed_requests.retry(args.extract_plain_text().strip(), actor=event.get_user_id())
        text = f"已提交后台重试：{result['id']}，沿用原用户权限和预算。"
    except ValueError as error:
        text = str(error)
    await retry.finish(text)

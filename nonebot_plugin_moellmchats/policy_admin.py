"""Superuser controls; none are exposed to the model as tools."""
import asyncio
import re

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from .chat_policy import conduct_store
from .failed_requests import failed_requests

unblock = on_command('解除LLM拉黑', permission=SUPERUSER, priority=10, block=True)
conduct = on_command('查看LLM警告', permission=SUPERUSER, priority=10, block=True)
failures = on_command('查看LLM失败请求', permission=SUPERUSER, priority=10, block=True)
retry = on_command('重试LLM请求', permission=SUPERUSER, priority=10, block=True)


@unblock.handle()
async def _(args: Message = CommandArg()):
    user = args.extract_plain_text().strip()
    if not re.fullmatch(r'[1-9][0-9]{4,11}', user):
        await unblock.finish('格式：解除LLM拉黑 QQ号')
    await asyncio.to_thread(conduct_store().clear, user)
    await unblock.finish(f'已清除 QQ {user} 的 AI 警告和拉黑状态。')


@conduct.handle()
async def _(args: Message = CommandArg()):
    user = args.extract_plain_text().strip()
    if not re.fullmatch(r'[1-9][0-9]{4,11}', user):
        await conduct.finish('格式：查看LLM警告 QQ号')
    row = await asyncio.to_thread(conduct_store().info, user)
    await conduct.finish(f"QQ {user}：违规 {row['strikes']} 次，" + ('AI 已永久停用。' if row['blocked'] else '未被拉黑。'))


@failures.handle()
async def _():
    rows = await failed_requests.list()
    await failures.finish('\n'.join(f"{row['id']}\nQQ {row['user_id']} · {row['status']} · " +
                         ('可安全重试' if row['retryable'] else row['reason']) for row in rows[:10]) or '暂无失败请求。')


@retry.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    try:
        result = await failed_requests.retry(args.extract_plain_text().strip(), actor=event.get_user_id())
        text = f"已提交后台重试：{result['id']}，沿用原用户权限和预算。"
    except ValueError as error:
        text = str(error)
    await retry.finish(text)

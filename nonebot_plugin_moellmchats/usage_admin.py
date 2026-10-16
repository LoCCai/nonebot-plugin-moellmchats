"""Superuser-only accounting controls. No billing or access grants are inferred."""
import asyncio
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import time
from urllib.request import ProxyHandler, Request, build_opener

from nonebot import get_driver, get_plugin_config, on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from pydantic import BaseModel

from .model_selector import model_selector
from .usage_ledger import get_ledger
from .usage_reports import HELP, draw_report, export_history, format_report, newapi_prices, report_data


class UsageConfig(BaseModel):
    moellm_newapi_prices_file: str = ""
    moellm_newapi_prices_url: str = ""


usage_config = get_plugin_config(UsageConfig)


def refresh_prices(*, fetch=True):
    source = usage_config.moellm_newapi_prices_file
    url = usage_config.moellm_newapi_prices_url
    if not source and not url:
        return "未配置NewAPI价格来源。"
    from .private_files import ensure_private_file, atomic_write_private_text
    ledger = get_ledger()
    ledger.initialize()
    options, reference, warning = {}, None, ''
    if source:
        path = Path(source)
        ensure_private_file(path)
        options = json.loads(path.read_text())
    if url:
        cache = ledger.path.parent / 'newapi-reference.json'
        if fetch:
            try:
                if not url.startswith(('https://', 'http://127.0.0.1:', 'http://localhost:')):
                    raise ValueError('价格源需使用HTTPS或本机接口')
                opener = build_opener(ProxyHandler({}))
                with opener.open(Request(url, headers={'User-Agent': 'Qiqi-Usage/1'}), timeout=12) as response:
                    content = response.read(2_000_001)
                if len(content) > 2_000_000:
                    raise ValueError('价格源过大')
                candidate = json.loads(content)
                if not any('input' in row for row in newapi_prices({}, candidate).values()):
                    raise ValueError('价格源没有可用模型价格')
                atomic_write_private_text(cache, json.dumps(candidate, ensure_ascii=False))
                reference = candidate
            except (OSError, ValueError):
                warning = '价格源拉取失败，使用已有快照。'
        if reference is None and cache.exists():
            ensure_private_file(cache)
            reference = json.loads(cache.read_text())
        if reference is None:
            return warning + '尚无参考价格快照，保留原有价格。'
    prices = newapi_prices(options, reference)
    ledger.put_prices(prices, replace_imported=True)
    usable = sum('input' in row for row in prices.values())
    covered = sum(ledger.estimate(model, {'usage_known': 1, 'total': 1, 'prompt': 1,
        'completion': 0, 'cache': 0})[0] is not None for model in model_selector.models.values())
    return (warning + f"已载入 {usable} 个模型的参考价格；当前模型池覆盖 {covered}/{len(model_selector.models)}。"
        "本地站点自定义价格优先，缺价及不支持的计费规则单独标记。")


@get_driver().on_startup
async def initialize_usage():
    ledger = get_ledger()
    await asyncio.to_thread(ledger.initialize)
    for identity, model in model_selector.models.items():
        if model.get("usage_token_budget"):
            await asyncio.to_thread(ledger.configure_budget, identity, model["usage_token_budget"])
    if usage_config.moellm_newapi_prices_file or usage_config.moellm_newapi_prices_url:
        try:
            logger.info(await asyncio.to_thread(refresh_prices, fetch=False))
        except (ValueError, OSError):
            logger.warning('价格快照读取失败，保留已有参考价格；可稍后刷新模型价格。')


stats = on_command("模型用量统计", aliases={"模型消耗统计"}, permission=SUPERUSER, priority=10, block=True)
chart = on_command("模型用量图", aliases={"模型消耗图"}, permission=SUPERUSER, priority=10, block=True)
history = on_command("导出模型用量", aliases={"导出消耗历史"}, permission=SUPERUSER, priority=10, block=True)
refill = on_command("补充模型额度", permission=SUPERUSER, priority=10, block=True)
pricing = on_command("刷新模型价格", permission=SUPERUSER, priority=10, block=True)
set_price = on_command("设置模型估价", permission=SUPERUSER, priority=10, block=True)


@stats.handle()
async def _(args: Message = CommandArg()):
    try:
        result = await asyncio.to_thread(report_data, args.extract_plain_text().strip())
        text = format_report(result)
    except ValueError as error:
        text = str(error)
    await stats.finish(text)


@chart.handle()
async def _(args: Message = CommandArg()):
    try:
        result = await asyncio.to_thread(report_data, args.extract_plain_text().strip())
        image = await asyncio.to_thread(draw_report, result)
    except (ValueError, ImportError) as error:
        await chart.finish(str(error) if isinstance(error, ValueError) else "统计图需要matplotlib；文字统计与导出仍可使用。")
    await chart.finish(MessageSegment.image(image))


@history.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    try:
        path, count = await asyncio.to_thread(export_history, args.extract_plain_text().strip())
    except ValueError as error:
        await history.finish(str(error))
    try:
        if group_id := getattr(event, "group_id", None):
            await bot.upload_group_file(group_id=group_id, file=str(path), name=path.name)
        else:
            await bot.upload_private_file(user_id=event.user_id, file=str(path), name=path.name)
    except Exception as error:
        logger.warning("用量CSV投递失败，已保留私有导出：error_type={}", type(error).__name__)
        await history.finish("历史CSV已生成，文件发送未成功。请私聊七七重试「导出模型用量 全部」，或由管理员读取私有导出目录。")
    await history.finish(f"已提交 {count:,} 条用量记录的CSV；上传接口返回成功，请确认文件可下载。")


@refill.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    try:
        query, amount = args.extract_plain_text().strip().rsplit(maxsplit=1)
        name = model_selector.resolve_model_name(query)
        if not name:
            raise ValueError("模型名不存在或有多个同名模型，请带服务商或使用模型编号。")
        ledger = get_ledger()
        await asyncio.to_thread(ledger.configure_budget, name, int(amount), reset=True, actor=str(event.user_id))
        await asyncio.to_thread(model_selector._quota_status().set, name, None)
    except ValueError as error:
        await refill.finish(f"{error}\n格式：补充模型额度 <模型名或编号> <新增可用Token数>")
    await refill.finish(f"已为 {name} 开始新的 {int(amount):,} Token额度周期，历史用量保留。")


@pricing.handle()
async def _():
    try:
        result = await asyncio.to_thread(refresh_prices)
    except (ValueError, OSError):
        result = "价格快照读取失败，继续保留原有价格。请检查文件权限和格式。"
    await pricing.finish(result)


@set_price.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    try:
        query, incoming, outgoing, cached, currency = args.extract_plain_text().strip().rsplit(maxsplit=4)
        name = model_selector.resolve_model_name(query)
        rates = [Decimal(value) for value in (incoming, outgoing, cached)]
        if not name or currency not in {'USD', 'CNY'} or any(not rate.is_finite() or not 0 <= rate <= 10**9 for rate in rates):
            raise ValueError
        price = {'input': str(rates[0]), 'output': str(rates[1]), 'cached': str(rates[2]),
            'currency': currency, 'per_tokens': 1_000_000, 'source': 'manual_admin',
            'actor': str(event.user_id), 'at': time.time(), 'estimate_only': True}
        await asyncio.to_thread(get_ledger().put_prices, {name: price})
    except (ValueError, InvalidOperation):
        await set_price.finish('格式：设置模型估价 <模型名或编号> <每百万输入价> <每百万输出价> <每百万缓存价> <USD|CNY>')
    await set_price.finish(f'已设置 {name} 的参考估价；仅用于后续请求，历史价格快照保留。')

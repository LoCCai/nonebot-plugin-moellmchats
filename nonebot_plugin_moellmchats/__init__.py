import asyncio
import random
import time

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import (
    GROUP,
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PokeNotifyEvent,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.log import logger
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, require
from nonebot.plugin.on import on_command, on_fullmatch, on_message, on_notice
from nonebot.rule import to_me

require("nonebot_plugin_localstore")

_model_refresh_task: asyncio.Task | None = None
_runner_preflight_task: asyncio.Task | None = None

from . import moe_llm as llm
from .chat_runtime import (
    cd,
    chat_rule,
    handle_llm,
    reset_all_runtime_state,
    reset_user_runtime_state,
)
from .config import config_parser
from .generated_tool_runner import generated_tool_runner
from .generated_tools import generated_tool_store
from .messages_handler import messages_dict
from .model_selector import model_selector
from .moe_llm import token_usage_history
from .pending_actions import (
    PendingAction,
    PendingActionError,
    PendingActionStore,
    execute_pending_action,
    pending_action_store,
)
from .request_manager import cancel_request_by_arg, format_active_requests
from .runtime_metrics import runtime_metrics
from .runtime_reload import runtime_reloader
from .runtime_snapshot import runtime_snapshots
from .temperament_manager import temperament_manager
from .token_usage_formatter import format_token_usage_history
from .tool_authoring import tool_authoring_service
from .tool_contracts import (
    ToolCapability,
    ToolCapabilityV2,
    ToolContext,
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolSpec,
    register_tool,
)
from .tool_manager import tool_manager
from .tool_runtime import reload_tools_for_commands
from .utils import (
    close_session,
    format_context_message,
    format_message,
    get_member_name,
    get_reply_messages,
    init_session,
)


class _GeneratedCommandInputError(ValueError):
    """A lifecycle command was rejected before changing Generated Tool state."""


__all__ = [
    "PendingAction",
    "PendingActionStore",
    "ToolCapability",
    "ToolCapabilityV2",
    "ToolContext",
    "ToolEffect",
    "ToolPolicy",
    "ToolResult",
    "ToolSpec",
    "register_tool",
]

__plugin_meta__ = PluginMetadata(
    name="MoEllm聊天",
    description=(
        "感谢llm，机器人变聪明了\n" "✨ 混合专家模型调度LLM插件 | 混合调度·联网搜索·上下文优化·个性定制·Token节约·更加拟人 ✨"
    ),
    usage="""1.艾特或以bot的名字开头进行对话
2.用"性格切换xx"来切换性格（每个性格设定绑定每个人账号，不共享）
3.用"ai xx"来快速调用纯ai助手
4.超级管理员限定：用"查看配置"、"查看模型"、"刷新模型"、"切换模型"、
  "切换moe"、"设置联网"、"设置视觉模型"、"设置分类模型"、"设置工具调用"进行系统管理
5.超级管理员限定：用"添加/移除插件黑名单"来禁用bot的特定工具调用
6.超级管理员限定：用"刷新工具/重载工具"来热重载新增的函数
7.超级管理员限定：用"查看插件黑名单/插件黑名单"来查看插件的黑名单列表
8.超级管理员限定：用"设置私聊 开/关"来开启/关闭超级管理员私聊对话模式
9.对bot发送"重置我的/重置对话/清空上下文"来清空自己的上下文对话记忆
10.超级管理员限定：对bot发送"重置全部对话"来清空所有用户的上下文及群组环境记忆
11.超级管理员限定：用"添加常驻插件/移除常驻插件"、"查看常驻插件"来管理无视分类模型强制注入的工具
12.超级管理员限定：用"查看请求"、"停止请求 [编号|all]"来查看或终止当前正在处理的 LLM 请求
13.超级管理员限定：用"查看消耗 [数量或范围]"来查询API Token消耗记录（如：查看消耗 5、查看消耗 10-15、查看消耗 -50）
14.超级管理员限定：用"重载LLM"原子重载运行资源，用"查看LLM状态"查看队列、缓存、工具和重载状态
15.超级管理员限定：用"添加LLM功能"生成工具草稿，复核后用"批准LLM功能"热载入
16.危险工具首次调用只生成确认码；原请求不会执行，用户必须另发"确认执行 <确认码>"，也可"取消执行 <确认码>"
17.超级管理员限定：用"设置LLM功能权限 <包> <哈希> <工具> user|superuser"审批生成工具的普通用户权限
""",
    type="application",
    homepage="https://github.com/LoCCai/nonebot-plugin-moellmchats",
    supported_adapters={"~onebot.v11"},
)


message_matcher = on_message(permission=GROUP, priority=1, block=False)


@message_matcher.handle()
async def context_dict_func(bot: Bot, event: MessageEvent):
    from .event_simulator import is_synthetic_event

    if is_synthetic_event():
        return
    if event.message.extract_plain_text().strip():  # 有文字才记录
        if message_dict := format_context_message(event):
            sender_name = event.sender.card or event.sender.nickname
            message_text = "".join(message_dict["text"])
            reply_text = (message_dict.get("reply") or "").strip()
            reply_user = message_dict.get("reply_user") or {}
            if reply_text and reply_user:
                reply_name = reply_user.get("name") or reply_user.get("qq") or "被引用者"
                message_text = f"[引用消息: {reply_name}说「{reply_text}」] {message_text}"
            llm.context_dict[event.group_id].append(f"[{sender_name}] {message_text}")
        # 概率主动发
        # if random.randint(1, 100) == 1:
        #     llm = llm.MoeLlm(
        # bot, event, message_dict,is_objective=True, temperament='默认')
        #     reply = await llm.handle_llm()


# 性格切换
temperament_switch_matcher = on_command("性格切换", aliases={"切换性格", "人格切换", "切换人格"}, priority=10, block=True)


@temperament_switch_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    if temp := args.extract_plain_text().strip():
        if temp in temperament_manager.get_temperaments_keys():
            # 写入文件
            if temperament_manager.set_temperament_dict(event.user_id, temp):
                await temperament_switch_matcher.finish(f"已切换性格为{temp}")
            else:
                await temperament_switch_matcher.finish("出错了，赶快喊机器人主人来修复一下吧~")
    await temperament_switch_matcher.finish(f"只有{temperament_manager.get_temperaments_keys()}中的性格可以切换")


# 查看性格
temperament_check_matcher = on_fullmatch(("查看性格", "查看人格"), priority=10, block=True)


@temperament_check_matcher.handle()
async def _(event: GroupMessageEvent):
    await temperament_check_matcher.finish(temperament_manager.get_all_temperaments())


# 1. 查看看看库里有什么模型可以切
check_model_matcher = on_command("查看可用模型", aliases={"查看模型"}, permission=SUPERUSER, priority=10, block=True)


@check_model_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    # 允许带多参数模糊搜索，例如：查看模型 deepseek coder
    query = args.extract_plain_text().strip()
    result = model_selector.get_formatted_model_list(query if query else None)
    await check_model_matcher.finish(result)


# 2. 查看当前机器人身上挂着哪些配置
check_config_matcher = on_fullmatch(("查看当前配置", "查看配置"), permission=SUPERUSER, priority=10, block=True)


@check_config_matcher.handle()
async def _(event: MessageEvent):
    cfg = model_selector.model_config

    # 构建美观的配置面板
    msg = (
        "✨ 当前大模型运行配置 ✨\n"
        f"▪ 基础聊天模型: {cfg.get('selected_model')}\n"
        f"▪ 视觉专用模型: {cfg.get('vision_model') or '未设置 (图片任务需先设置)'}\n"
        f"▪ 意图分类模型: {cfg.get('category_model')}\n"
        f"▪ 启用MoE调度: {'✅开启' if cfg.get('use_moe') else '❌关闭'}\n"
        f"  - 难度0: {cfg.get('moe_models', {}).get('0')}\n"
        f"  - 难度1: {cfg.get('moe_models', {}).get('1')}\n"
        f"  - 难度2: {cfg.get('moe_models', {}).get('2')}\n"
        f"▪ 启用联网搜索: {'✅开启' if cfg.get('use_web_search') else '❌关闭'}\n"
        f"▪ 启用函数调用: {'✅开启' if cfg.get('use_tools', False) else '❌关闭'}"
    )
    await check_config_matcher.finish(msg)


model_matcher = on_command("切换模型", permission=SUPERUSER, priority=10, block=True)


@model_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    model_name = args.extract_plain_text().strip()
    result = model_selector.set_chat_model(model_name)
    await model_matcher.finish(result)


set_moe_matcher = on_command("设置moe", permission=SUPERUSER, priority=10, block=True)


@set_moe_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    is_moe = args.extract_plain_text().strip()
    if is_moe not in ["开", "关", "0", "1"]:
        await set_moe_matcher.finish("参数错误，格式为：设置moe 开、关、1、0")
    if is_moe == "开" or is_moe == "1":
        is_moe = True
    else:
        is_moe = False
    result = model_selector.set_moe(is_moe)
    await set_moe_matcher.finish(result)


set_web_search_matcher = on_command("设置联网", aliases={"切换联网"}, permission=SUPERUSER, priority=10, block=True)


@set_web_search_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    is_web_search = args.extract_plain_text().strip()
    if is_web_search not in ["开", "关", "0", "1"]:
        await set_web_search_matcher.finish("参数错误，格式为：设置联网 开、关、1、0")
    if is_web_search == "开" or is_web_search == "1":
        is_web_search = True
    else:
        is_web_search = False
    result = model_selector.set_web_search(is_web_search)
    await set_web_search_matcher.finish(result)


moe_matcher = on_command("切换moe", permission=SUPERUSER, priority=10, block=True)


@moe_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    try:
        difficulty, model_name = args.extract_plain_text().split()
        result = model_selector.set_moe_model(model_name, difficulty)
    except Exception:
        await moe_matcher.finish("参数错误，格式为：切换moe 难度 模型名")
    await moe_matcher.finish(result)


vision_model_matcher = on_command(
    "切换视觉模型",
    aliases={"设置视觉模型"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@vision_model_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    model_name = args.extract_plain_text().strip()
    result = model_selector.set_vision_model(model_name)
    await vision_model_matcher.finish(result)


llm_matcher = on_message(
    rule=to_me() & chat_rule,
    priority=99,
    block=True,
)


@llm_matcher.handle()
async def _(bot: Bot, event: MessageEvent):
    if event.message.extract_plain_text().strip():
        format_message_dict = await format_message(event, bot)
    else:
        await llm_matcher.finish(Message(random.choice(get_reply_messages("hello"))))  # 没有就选一个卖萌回复
    await handle_llm(bot, event, llm_matcher, format_message_dict, is_ai=False)


ai_matcher = on_command(
    "ai",
    rule=chat_rule,
    priority=17,
    block=True,
)


@ai_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not config_parser.get_config("fastai_enabled"):
        await ai_matcher.finish("快速 AI 助手当前未启用。")
    if args.extract_plain_text().strip():
        format_message_dict = await format_message(event, bot)
        await handle_llm(bot, event, ai_matcher, format_message_dict, is_ai=True)
    else:
        await ai_matcher.finish(Message(random.choice(get_reply_messages("hello"))))  # 没有就选一个卖萌回复


confirm_action_matcher = on_command(
    "确认执行",
    priority=0,
    block=True,
)


@confirm_action_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    from .event_simulator import is_synthetic_event

    if is_synthetic_event():
        return
    parts = args.extract_plain_text().strip().split()
    if len(parts) != 1:
        await confirm_action_matcher.finish("格式：确认执行 <6位确认码>")
    try:
        action, result = await execute_pending_action(
            parts[0],
            bot=bot,
            event=event,
            runtime_snapshot=runtime_snapshots.current(),
        )
    except PendingActionError as error:
        await confirm_action_matcher.finish(f"确认失败：{error}")

    result_text = result.text or "执行成功"
    images = result.images
    if images:
        result_text += f"\n[工具还返回了 {len(images)} 张图片；危险操作确认通道不会直接转发工具提供的文件或 URL。]"
    await confirm_action_matcher.send(f"已确认并执行工具 {action.tool_name}：\n{result_text}")
    await confirm_action_matcher.finish()


cancel_action_matcher = on_command(
    "取消执行",
    priority=0,
    block=True,
)


@cancel_action_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    from .event_simulator import is_synthetic_event

    if is_synthetic_event():
        return
    parts = args.extract_plain_text().strip().split()
    if len(parts) != 1:
        await cancel_action_matcher.finish("格式：取消执行 <6位确认码>")
    try:
        await pending_action_store.cancel(parts[0], bot=bot, event=event)
    except PendingActionError as error:
        await cancel_action_matcher.finish(f"取消失败：{error}")
    await cancel_action_matcher.finish("已取消该待确认操作。")


set_use_tools_matcher = on_command(
    "设置工具调用",
    aliases={"设置函数调用"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@set_use_tools_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    is_use_tools = args.extract_plain_text().strip()
    if is_use_tools not in ["开", "关", "0", "1"]:
        await set_use_tools_matcher.finish("参数错误，格式为：设置工具调用 开、关、1、0")
    result = model_selector.set_use_tools(is_use_tools in ["开", "1"])
    await set_use_tools_matcher.finish(result)


manage_blacklist_matcher = on_command(
    "添加插件黑名单",
    aliases={"移除插件黑名单"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


def _current_tool_generation() -> int | None:
    snapshot = runtime_snapshots.current()
    return snapshot.generation if snapshot is not None else None


def _retained_tool_generation(generation: int | None) -> str:
    if generation is None:
        return "旧 generation 未变（当前尚无可用工具 generation）"
    return f"旧 generation {generation} 已保留"


@manage_blacklist_matcher.handle()
async def manage_tool_blacklist_command(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    plugin_name = args.extract_plain_text().strip()
    if not plugin_name:
        await manage_blacklist_matcher.finish("请提供插件/函数/MCP工具名，如：添加插件黑名单 mcp__filesystem")

    command_name = event.message.extract_plain_text().split()[0].strip()
    action = "add" if "添加" in command_name else "remove"

    if action == "add":
        if plugin_name in model_selector.get_tool_blacklist():
            await manage_blacklist_matcher.finish("该插件已在黑名单中")

        try:
            await reload_tools_for_commands()
        except Exception:
            await manage_blacklist_matcher.finish(
                "❌ 添加黑名单前的工具校验重载失败，黑名单未修改；"
                f"{_retained_tool_generation(_current_tool_generation())}。"
                "详情请查看后台日志。"
            )
        validate_tool_identifier = getattr(tool_manager, "validate_tool_identifier", None)
        if not callable(validate_tool_identifier):
            await manage_blacklist_matcher.finish("当前工具管理器不支持工具存在性校验，请重启 Bot 或更新插件后重试。")

        exists, validate_msg = validate_tool_identifier(plugin_name)
        if not exists:
            await manage_blacklist_matcher.finish(validate_msg)

    result = model_selector.manage_tool_blacklist(action, plugin_name)

    try:
        await reload_tools_for_commands()
    except Exception:
        config_status = "黑名单配置已写入" if result.startswith("已将 ") else "黑名单配置未变"
        await manage_blacklist_matcher.finish(
            f"⚠️ {config_status}（{result}），但工具运行快照同步失败；"
            f"{_retained_tool_generation(_current_tool_generation())}。"
            "请修复后再执行“刷新工具”。"
        )

    await manage_blacklist_matcher.finish(result)


check_blacklist_matcher = on_command(
    "插件黑名单",
    aliases={"查看插件黑名单"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@check_blacklist_matcher.handle()
async def _(event: MessageEvent):
    blacklist = model_selector.get_tool_blacklist()
    if not blacklist:
        await check_blacklist_matcher.finish("当前插件黑名单为空，大模型可调用所有已加载且未被过滤的工具。")

    lines = ["🚫 当前插件调用黑名单："]
    for plugin in blacklist:
        lines.append(f"  - {plugin}")

    await check_blacklist_matcher.finish("\n".join(lines))


refresh_tools_matcher = on_command(
    "刷新工具",
    aliases={"重载工具", "刷新插件"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@refresh_tools_matcher.handle()
async def refresh_tools_command():
    try:
        result = await reload_tools_for_commands()
    except Exception:
        await refresh_tools_matcher.finish(
            f"❌ 工具重载失败，未发布新工具；{_retained_tool_generation(_current_tool_generation())}。详情请查看后台日志。"
        )

    msg = "✨ 工具重载完成！\n"
    msg += f"✅ 已发布 generation {result.generation}\n"
    msg += f"✅ 已加载 {len(tool_manager.plugin_info)} 个原生插件\n"
    msg += f"✅ 已加载 {result.custom_tools} 个自定义函数\n"
    msg += f"✅ 已加载 {result.mcp_tools} 个 MCP 工具"

    await refresh_tools_matcher.finish(msg)


category_model_matcher = on_command(
    "切换分类模型",
    aliases={"设置分类模型"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@category_model_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    model_name = args.extract_plain_text().strip()
    result = model_selector.set_category_model(model_name)
    await category_model_matcher.finish(result)


@get_driver().on_startup
async def _startup_tasks():
    await init_session()

    # 工具加载可以同步完成，避免刚启动时插件目录为空
    try:
        await reload_tools_for_commands()
    except Exception:
        logger.exception("启动时加载工具失败")

    # 模型拉取保持后台执行，不阻塞 Bot 启动
    async def refresh_models_in_background():
        try:
            await model_selector.fetch_models_from_providers()
            await runtime_reloader.reload("startup-model-refresh")
        except Exception:
            logger.exception("后台刷新模型失败，继续使用当前运行快照")

    async def runner_preflight_in_background():
        try:
            await generated_tool_runner.preflight()
            logger.info("生成工具 runner 隔离探针通过")
        except Exception:
            logger.exception("生成工具 runner 隔离不可用；生成工具将 fail closed")

    global _model_refresh_task, _runner_preflight_task
    _model_refresh_task = asyncio.create_task(refresh_models_in_background())
    _runner_preflight_task = asyncio.create_task(runner_preflight_in_background())
    runtime_reloader.start_watcher()


@get_driver().on_shutdown
async def _close_http_session():
    for task in (_model_refresh_task, _runner_preflight_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await runtime_reloader.stop_watcher()
    await close_session()


# 超级管理员可手动触发模型刷新
refresh_models_matcher = on_command("刷新模型", aliases={"刷新模型列表"}, permission=SUPERUSER, priority=10, block=True)


@refresh_models_matcher.handle()
async def _():
    await refresh_models_matcher.send("正在重新读取本地配置并拉取各服务商模型列表，请稍候...")
    # fetch_models_from_providers 会先在工作线程中重新校验并解析 owner-private
    # providers.toml，再允许任何外部 /models 请求。
    await model_selector.fetch_models_from_providers()  # 重新请求 API 并重载
    await runtime_reloader.reload("model-command")
    await refresh_models_matcher.finish(f"更新完毕！当前系统共加载了 {len(model_selector.models)} 个模型。")


# 供其他插件总结用
summary_model_matcher = on_command(
    "切换总结模型",
    aliases={"设置总结模型"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@summary_model_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    model_query = args.extract_plain_text().strip()
    result = model_selector.set_summary_model(model_query)
    await summary_model_matcher.finish(result)


set_private_chat_matcher = on_command("设置私聊", permission=SUPERUSER, priority=10, block=True)


@set_private_chat_matcher.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    arg = args.extract_plain_text().strip()
    if arg in ["开", "1"]:
        config_parser.set_config("private_chat_enabled", True)
        await set_private_chat_matcher.finish("已开启超级管理员私聊对话模式")
    elif arg in ["关", "0"]:
        config_parser.set_config("private_chat_enabled", False)
        await set_private_chat_matcher.finish("已关闭超级管理员私聊对话模式")
    else:
        await set_private_chat_matcher.finish("参数错误，格式为：设置私聊 开、关、1、0")


# 重置个人对话（需要 @ 机器人触发）
reset_mine_matcher = on_command(
    "重置我的",
    aliases={"重置对话", "清空上下文", "清空对话"},
    rule=to_me(),
    priority=10,
    block=True,
)


@reset_mine_matcher.handle()
async def _(event: MessageEvent):
    user_id = event.user_id
    if user_id in messages_dict:
        messages_dict[user_id].clear()  # 清空个人记忆

    # 清理该用户的调用CD和状态
    reset_user_runtime_state(user_id)

    await reset_mine_matcher.finish("已清空你的专属上下文对话记忆~")


# 重置全部对话（需要超级管理员 + @ 机器人触发）
reset_all_matcher = on_fullmatch(
    {"重置全部对话", "重置所有对话", "清空所有上下文", "清空全部上下文"},
    rule=to_me(),
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@reset_all_matcher.handle()
async def _():
    messages_dict.clear()  # 清空所有人的个人记忆
    llm.context_dict.clear()  # 清空所有群聊的群聊环境记忆
    reset_all_runtime_state()

    await reset_all_matcher.finish("已清空所有用户的上下文及群聊环境记忆！")


manage_resident_matcher = on_command(
    "添加常驻插件",
    aliases={"移除常驻插件", "添加常驻函数", "移除常驻函数"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@manage_resident_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    plugin_name = args.extract_plain_text().strip()
    if not plugin_name:
        await manage_resident_matcher.finish("请提供插件/函数名，如：添加常驻插件 web_search")

    command_name = event.message.extract_plain_text().split()[0].strip()
    action = "add" if "添加" in command_name else "remove"
    result = model_selector.manage_resident_plugins(action, plugin_name)
    await manage_resident_matcher.finish(result)


check_resident_matcher = on_command(
    "常驻插件",
    aliases={"查看常驻插件", "查看常驻函数", "查看常驻工具"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@check_resident_matcher.handle()
async def _(event: MessageEvent):
    resident = model_selector.get_resident_plugins()
    if not resident:
        await check_resident_matcher.finish("当前常驻插件列表为空。大模型将完全依赖分类模型进行插件调度。")

    lines = ["📌 当前常驻插件/函数列表 (无视分类强制注入)："]
    for plugin in resident:
        lines.append(f"  - {plugin}")

    await check_resident_matcher.finish("\n".join(lines))


check_request_matcher = on_command(
    "查看请求",
    aliases={"查看当前请求", "当前请求", "查看正在请求"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@check_request_matcher.handle()
async def _():
    await check_request_matcher.finish(format_active_requests())


stop_request_matcher = on_command(
    "停止请求",
    aliases={"终止请求", "取消请求", "stop请求"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@stop_request_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    arg = args.extract_plain_text().strip()
    await stop_request_matcher.finish(cancel_request_by_arg(arg))


# --- 查询 Token 消耗的指令 ---
check_token_matcher = on_command(
    "查看消耗",
    aliases={"查询token", "token消耗"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@check_token_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    arg = args.extract_plain_text().strip()
    await check_token_matcher.finish(format_token_usage_history(arg, token_usage_history))


reload_llm_matcher = on_command(
    "重载LLM",
    aliases={"刷新LLM", "重载llm", "刷新llm"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@reload_llm_matcher.handle()
async def _():
    try:
        result = await runtime_reloader.reload("command")
    except Exception as error:
        await reload_llm_matcher.finish(f"LLM 资源重载失败，旧配置仍在使用：{str(error)[:300]}")
    await reload_llm_matcher.finish(
        f"LLM 资源重载完成：generation {result.generation}，" f"自定义工具 {result.custom_tools}，MCP 工具 {result.mcp_tools}。"
    )


author_tool_matcher = on_command(
    "添加LLM功能",
    aliases={"创建LLM功能"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@author_tool_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    requirement = args.extract_plain_text().strip()
    if not requirement:
        await author_tool_matcher.finish("请提供功能需求，例如：添加LLM功能 计算两个日期之间的天数")
    await author_tool_matcher.send("正在生成、隔离测试并复核工具草稿，请稍候……")
    try:
        draft_id, validation, review, test_summary = await tool_authoring_service.create(
            requirement,
            actor_key=str(event.user_id),
        )
    except Exception as error:
        logger.exception("AI 工具草稿生成失败")
        await author_tool_matcher.finish(f"功能草稿生成失败：{str(error)[:500]}")
    risks = "；".join(validation.risks) if validation.risks else "未发现静态高风险调用"
    review_snapshot = await asyncio.to_thread(
        generated_tool_store.get_draft_review_snapshot,
        draft_id,
    )
    diff = review_snapshot.section_content("diff")
    status = "复核通过，等待二次批准" if review["approved"] else "复核未通过，禁止批准"
    await author_tool_matcher.finish(
        f"草稿 {draft_id}：{status}\n"
        f"工具包：{validation.manifest['bundle_id']}\n"
        f"SHA-256：{validation.digest}\n"
        f"测试：{test_summary[:300]}\n"
        f"复核：{str(review.get('summary') or '')[:500]}\n"
        f"风险：{risks[:800]}\n"
        f"Diff：\n{diff[:1200]}\n"
        f"查看：查看LLM功能草稿 {draft_id}\n"
        f"批准：{review_snapshot.approval_command}"
    )


view_tool_draft_matcher = on_command(
    "查看LLM功能草稿",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


def _parse_draft_review_args(raw: str) -> tuple[str, str, int]:
    parts = raw.split()
    if not 1 <= len(parts) <= 3:
        raise ValueError("格式：查看LLM功能草稿 <草稿ID> " "[summary|manifest|source|tests|risks|capabilities|diff] [页码]")
    draft_id = parts[0]
    section = parts[1] if len(parts) >= 2 else "summary"
    allowed_sections = (
        "summary",
        "manifest",
        "source",
        "tests",
        "risks",
        "capabilities",
        "diff",
    )
    if section not in allowed_sections:
        raise ValueError(f"审阅区段非法：{section}；可选：{', '.join(allowed_sections)}")
    if len(parts) < 3:
        return draft_id, section, 1
    if not parts[2].isascii() or not parts[2].isdigit() or int(parts[2]) < 1:
        raise ValueError("页码必须是从 1 开始的正整数")
    return draft_id, section, int(parts[2])


@view_tool_draft_matcher.handle()
async def _(args: Message = CommandArg()):
    raw = args.extract_plain_text().strip()
    if not raw:
        status = await asyncio.to_thread(generated_tool_store.list_status)
        drafts = status["drafts"][-10:]
        text = "\n".join(f"{item['draft_id']} {item['status']} {str(item['request'])[:60]}" for item in drafts) or "暂无工具草稿"
        await view_tool_draft_matcher.finish(text)
    try:
        draft_id, section, page_number = _parse_draft_review_args(raw)
    except ValueError as error:
        await view_tool_draft_matcher.finish(str(error))
    try:
        page = await asyncio.to_thread(
            generated_tool_store.get_draft_review_page,
            draft_id,
            section,
            page_number,
        )
    except Exception as error:
        await view_tool_draft_matcher.finish(f"读取草稿失败：{error}")
    await view_tool_draft_matcher.finish(MessageSegment.text(page.text))


approve_tool_matcher = on_command(
    "批准LLM功能",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@approve_tool_matcher.handle()
async def _(args: Message = CommandArg()):
    parts = args.extract_plain_text().split()
    if len(parts) != 3:
        await approve_tool_matcher.finish(
            "格式：批准LLM功能 <草稿ID> <至少8位哈希> <review stamp>；"
            "请从查看LLM功能草稿页头复制完整命令"
        )
    try:
        change = await asyncio.to_thread(
            generated_tool_store.prepare_approval,
            parts[0],
            parts[1],
            parts[2],
        )
        (bundle_id, digest), result = await runtime_reloader.apply_generated_change(
            "generated-tool-approve",
            change,
        )
    except Exception as error:
        await approve_tool_matcher.finish(
            "批准失败："
            f"{str(error)[:800]}。请用查看LLM状态核对 desired/applied 收敛。"
        )
    await approve_tool_matcher.finish(
        f"已载入 {bundle_id}@{digest[:12]}，"
        f"lifecycle revision {result.generated_state_revision}，"
        f"generation {result.generation}，converged={result.converged}"
    )


reject_tool_matcher = on_command(
    "拒绝LLM功能",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@reject_tool_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    parts = args.extract_plain_text().strip().split(maxsplit=1)
    if not parts:
        await reject_tool_matcher.finish(
            "格式：拒绝LLM功能 <草稿ID> [原因]"
        )
    draft_id = parts[0]
    reason = parts[1] if len(parts) == 2 else "QQ 超级管理员拒绝"
    try:
        change = await asyncio.to_thread(
            generated_tool_store.prepare_rejection,
            draft_id,
            actor=f"qq:{event.user_id}",
            reason=reason,
        )
        _, result = await runtime_reloader.apply_generated_change(
            "generated-tool-reject",
            change,
        )
    except Exception as error:
        await reject_tool_matcher.finish(f"拒绝草稿失败：{error}")
    await reject_tool_matcher.finish(
        f"已拒绝草稿 {draft_id}，源码保留用于审计；"
        f"lifecycle revision {result.generated_state_revision}"
    )


list_tools_matcher = on_command(
    "LLM功能列表",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@list_tools_matcher.handle()
async def _():
    status = await asyncio.to_thread(generated_tool_store.list_status)
    active_lines = []
    for bundle in status["active_tools"]:
        tools = ", ".join(
            f"{item['name']}=" f"{item['requested_permission']}→{item['effective_permission']}"
            for item in bundle.get("tools", [])
        )
        suffix = f" [{tools}]" if tools else ""
        active_lines.append(f"{bundle['bundle_id']}@{bundle['digest'][:12]}{suffix}")
    draft_counts = {}
    for draft in status["drafts"]:
        state = draft.get("status") or "unknown"
        draft_counts[state] = draft_counts.get(state, 0) + 1
    await list_tools_matcher.finish(
        "已激活工具包：\n" + ("\n".join(active_lines) if active_lines else "无") + f"\n草稿状态：{draft_counts or {}}"
    )


generated_permission_matcher = on_command(
    "设置LLM功能权限",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@generated_permission_matcher.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    parts = args.extract_plain_text().strip().split()
    if len(parts) != 4 or parts[3] not in {"user", "superuser"}:
        await generated_permission_matcher.finish("格式：设置LLM功能权限 <工具包> <当前版本哈希前缀> <工具名> user|superuser")
    bundle_id, digest_prefix, tool_name, permission = parts

    try:
        state = await asyncio.to_thread(
            generated_tool_store.read_lifecycle_state
        )
        digest = state.active.get(bundle_id)
        if (
            digest is None
            or len(digest_prefix) < 8
            or not digest.startswith(digest_prefix.lower())
        ):
            raise _GeneratedCommandInputError(
                "工具包未激活，或当前版本哈希与给定前缀不匹配。"
            )
        change = await asyncio.to_thread(
            generated_tool_store.prepare_permission,
            bundle_id,
            digest,
            tool_name,
            allow_user=permission == "user",
            approved_by=f"qq:{event.user_id}",
            require_active=True,
        )
        changed, result = await runtime_reloader.apply_generated_change(
            "generated-tool-permission",
            change,
        )
    except _GeneratedCommandInputError as error:
        await generated_permission_matcher.finish(str(error))
    except Exception as error:
        await generated_permission_matcher.finish(
            "权限更新失败："
            f"{str(error)[:800]}。请核对 desired/applied 收敛状态。"
        )
    await generated_permission_matcher.finish(
        f"已设置 {tool_name} effective_permission="
        f"{changed['effective_permission']}，generation {result.generation}，"
        f"lifecycle revision {result.generated_state_revision}。"
    )


deactivate_tool_matcher = on_command(
    "停用LLM功能",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@deactivate_tool_matcher.handle()
async def _(args: Message = CommandArg()):
    bundle_id = args.extract_plain_text().strip()
    try:
        change = await asyncio.to_thread(
            generated_tool_store.prepare_deactivation,
            bundle_id,
        )
        if change.plan.no_op:
            raise _GeneratedCommandInputError("该工具包当前未激活")
        _, result = await runtime_reloader.apply_generated_change(
            "generated-tool-deactivate",
            change,
        )
    except _GeneratedCommandInputError as error:
        await deactivate_tool_matcher.finish(str(error))
    except Exception as error:
        await deactivate_tool_matcher.finish(f"停用失败：{error}")
    await deactivate_tool_matcher.finish(
        f"已停用 {bundle_id}，generation {result.generation}，"
        f"lifecycle revision {result.generated_state_revision}"
    )


rollback_tool_matcher = on_command(
    "回滚LLM功能",
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@rollback_tool_matcher.handle()
async def _(args: Message = CommandArg()):
    parts = args.extract_plain_text().split()
    if len(parts) != 2:
        await rollback_tool_matcher.finish("格式：回滚LLM功能 <工具包> <版本哈希前缀>")
    try:
        change = await asyncio.to_thread(
            generated_tool_store.prepare_rollback,
            parts[0],
            parts[1],
        )
        digest, result = await runtime_reloader.apply_generated_change(
            "generated-tool-rollback",
            change,
        )
    except Exception as error:
        await rollback_tool_matcher.finish(f"回滚失败：{error}")
    await rollback_tool_matcher.finish(
        f"已回滚 {parts[0]}@{digest[:12]}，generation {result.generation}，"
        f"lifecycle revision {result.generated_state_revision}，"
        f"converged={result.converged}"
    )


llm_status_matcher = on_command(
    "查看LLM状态",
    aliases={"LLM状态", "查看llm状态", "llm状态"},
    permission=SUPERUSER,
    priority=10,
    block=True,
)


@llm_status_matcher.handle()
async def _():
    metrics = runtime_metrics.snapshot()
    generated_status, pending_actions = await asyncio.gather(
        asyncio.to_thread(generated_tool_store.list_status),
        pending_action_store.size(),
    )
    avg_category = metrics["classification_seconds"] / metrics["classification_count"] if metrics["classification_count"] else 0
    current_snapshot = runtime_snapshots.current()
    desired_revision = generated_status["lifecycle_revision"]
    desired_digest = generated_status["lifecycle_state_digest"]
    applied_revision = (
        current_snapshot.generated_state_revision
        if current_snapshot is not None
        else None
    )
    applied_digest = (
        current_snapshot.generated_state_digest
        if current_snapshot is not None
        else None
    )
    lifecycle_converged = (
        applied_revision == desired_revision
        and applied_digest == desired_digest
    )
    await llm_status_matcher.finish(
        "LLM 运行状态\n"
        f"generation: {metrics['reload_generation']}\n"
        "Generated lifecycle: "
        f"desired={desired_revision}:{desired_digest[:12]} "
        f"applied={applied_revision if applied_revision is not None else '无'}:"
        f"{applied_digest[:12] if applied_digest else '无'} "
        f"local_converged={lifecycle_converged}\n"
        f"请求: active={metrics['llm_active']} pending={metrics['llm_pending']} "
        f"rejected={metrics['llm_rejected']}\n"
        f"兼容投递: active={metrics['dispatch_active']} pending={metrics['dispatch_pending']} "
        f"rejected={metrics['dispatch_rejected']} timeout={metrics['dispatch_timeouts']}\n"
        f"投递模式: {metrics['dispatch_modes'] or {}}\n"
        f"成员缓存: hit={metrics['member_cache_hits']} miss={metrics['member_cache_misses']} "
        f"timeout={metrics['member_lookup_timeouts']}\n"
        f"分类平均耗时: {avg_category:.2f}s\n"
        f"工具步骤: {metrics['tool_steps']} timeout={metrics['tool_timeouts']}\n"
        f"危险操作待确认: {pending_actions}\n"
        f"生成工具: active_bundles={len(generated_status['active'])} "
        f"drafts={len(generated_status['drafts'])} "
        f"runner_active={metrics['generated_runner_active']} "
        f"runner_pending={metrics['generated_runner_pending']} "
        f"rejected={metrics['generated_runner_rejected']} "
        f"timeout={metrics['generated_runner_timeouts']} "
        f"failure={metrics['generated_runner_failures']} "
        f"killed={metrics['generated_runner_killed']} "
        f"orphan_cleanup={metrics['generated_runner_orphan_cleanups']} "
        f"isolation={generated_tool_runner.isolation_status}\n"
        "Legacy 投影: "
        f"stale={generated_status['legacy_projection_stale']} "
        f"error={generated_status['legacy_projection_error'] or '无'}\n"
        f"造工具任务: active={metrics['generated_authoring_active']}\n"
        f"重载: success={metrics['reload_successes']} failure={metrics['reload_failures']} "
        f"last_at={metrics['last_reload_at'] or '无'}\n"
        f"最近重载错误: {metrics['last_reload_error'] or '无'}"
    )


# 优先级10，不会向下阻断，条件：戳一戳bot触发
poke_ = on_notice(rule=to_me(), priority=11, block=False)


@poke_.handle()
async def _poke_event(bot: Bot, event: PokeNotifyEvent):
    if not event.is_tome:
        return
    # 概率走LLM对话（内容：(xx戳了一下你)），概率外保持默认随机回复
    poke_llm_rate = config_parser.get_config("poke_llm_rate") or 0
    group_id = getattr(event, "group_id", None)
    if (
        poke_llm_rate
        and group_id
        and random.random() < poke_llm_rate
        # cd中直接走默认回复，避免触发handle_llm的排队等待逻辑
        and event.time - cd.get(event.user_id, 0) >= (config_parser.get_config("cd_seconds") or 0)
    ):
        # 获取戳一戳发起者的群名片/昵称
        try:
            sender_name = await get_member_name(group_id, event.user_id, bot)
        except Exception:
            sender_name = str(event.user_id)

        # 构造伪群消息事件以复用LLM对话全流程（cd/队列/工具/上下文/性格）
        fake_id = int(time.time_ns() % 10**15) + random.randint(1000, 9999)
        fake_event = GroupMessageEvent(
            time=event.time,
            self_id=event.self_id,
            post_type="message",
            sub_type="normal",
            user_id=event.user_id,
            message_type="group",
            group_id=group_id,
            message_id=fake_id,
            message=Message(),
            original_message=Message(),
            raw_message="",
            font=0,
            sender=Sender(user_id=event.user_id, nickname=sender_name, card=sender_name),
        )
        format_message_dict = {
            "text": [f"({sender_name}戳了一下你)"],
            "images": [],
            "mentions": [],
            "reply": "",
            "reply_user": None,
            "current_user": {"qq": str(event.user_id), "name": sender_name},
        }
        await handle_llm(bot, fake_event, poke_, format_message_dict, is_ai=False)
    else:
        await poke_.send(Message(random.choice(get_reply_messages("poke"))))
        # try:
        #     await poke_.send(Message(f"[CQ:group_poke,qq={event.user_id}]"))
        # except ActionFailed:
        #     await poke_.send(Message(f"[CQ:touch,id={event.user_id}]"))
        # except Exception:
        #     return

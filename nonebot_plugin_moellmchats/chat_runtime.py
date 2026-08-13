import asyncio

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, PrivateMessageEvent

from . import moe_llm as llm
from .admission import AdmissionRejected, get_llm_controller
from .compat import timeout as timeout_scope
from .config import config_parser
from .request_manager import register_request, unregister_request
from .runtime_snapshot import runtime_snapshots
from .state_store import BoundedValueStore
from .temperament_manager import temperament_manager

cd = BoundedValueStore(lambda: 0)
is_repeat_ask_dict = BoundedValueStore(lambda: False)


async def chat_rule(bot: Bot, event: MessageEvent) -> bool:
    from .event_simulator import is_synthetic_event

    if is_synthetic_event():
        return False
    if isinstance(event, GroupMessageEvent):
        return True
    if isinstance(event, PrivateMessageEvent):
        return bool(
            config_parser.get_config("private_chat_enabled")
            and str(event.user_id) in bot.config.superusers
        )
    return False


def reset_user_runtime_state(user_id: int) -> None:
    cd[user_id] = 0
    is_repeat_ask_dict[user_id] = False


def reset_all_runtime_state() -> None:
    cd.clear()
    is_repeat_ask_dict.clear()


async def handle_llm(
    bot: Bot, event: MessageEvent, matcher, format_message_dict: dict, is_ai=False
):
    user_id = event.sender.user_id
    try:
        async with get_llm_controller().slot(user_id):
            if event.time - cd[user_id] < config_parser.get_config("cd_seconds"):
                sender_name = getattr(event.sender, "card", None) or event.sender.nickname
                wait_seconds = int(config_parser.get_config("cd_seconds") - (
                    event.time - cd[user_id]
                ))
                await matcher.finish(
                    f"{sender_name}的 LLM 对话冷却中，请在 {max(1, wait_seconds)} 秒后重试。"
                )

            cd[user_id] = event.time
            snapshot = runtime_snapshots.current()
            with runtime_snapshots.bind(snapshot):
                temp = (
                    "ai助手" if is_ai else temperament_manager.get_temperament(user_id)
                )
                if not temp:
                    await matcher.finish("出错了，赶快喊机器人主人来修复一下吧~")

                llm_chat = llm.MoeLlm(
                    bot, event, format_message_dict, temperament=temp
                )
                request_id = register_request(event, format_message_dict, is_ai)
                try:
                    async with timeout_scope(
                        config_parser.get_config("request_timeout_seconds", 180)
                    ):
                        is_finished = await llm_chat.get_llm_chat()
                finally:
                    unregister_request(request_id)
    except AdmissionRejected:
        await matcher.finish("当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。")
    except TimeoutError:
        reset_user_runtime_state(user_id)
        await matcher.finish("本次 LLM 任务已超过总时间预算，已安全终止。")
    except asyncio.CancelledError:
        reset_user_runtime_state(user_id)
        await matcher.finish("当前 LLM 请求已被超级管理员终止。")

    is_repeat_ask_dict[user_id] = False
    if isinstance(is_finished, str):
        cd[user_id] = 0
        await matcher.finish(is_finished)
    elif not is_finished:
        cd[user_id] = 0

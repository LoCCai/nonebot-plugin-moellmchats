import asyncio

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, PrivateMessageEvent

from . import moe_llm as llm
from .admission import AdmissionRejected, get_llm_controller
from .compat import timeout as timeout_scope
from .config import config_parser
from .cooldowns import CooldownError, CooldownLease, CooldownStoreProtocol, MemoryCooldownStore
from .request_manager import register_request, unregister_request
from .runtime_snapshot import runtime_snapshots
from .state_store import BoundedValueStore
from .temperament_manager import temperament_manager

cd = BoundedValueStore(lambda: 0)
default_cooldown_store = MemoryCooldownStore(cd)
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
    default_cooldown_store.reset_user(user_id)
    is_repeat_ask_dict[user_id] = False


def reset_all_runtime_state() -> None:
    default_cooldown_store.clear()
    is_repeat_ask_dict.clear()


async def _release_cooldown(
    store: CooldownStoreProtocol,
    lease: CooldownLease | None,
    *,
    user_id: int,
) -> None:
    if lease is not None:
        await store.release(lease)
    is_repeat_ask_dict[user_id] = False


async def handle_llm(
    bot: Bot,
    event: MessageEvent,
    matcher,
    format_message_dict: dict,
    is_ai=False,
    *,
    cooldown_store: CooldownStoreProtocol | None = None,
):
    user_id = event.sender.user_id
    if user_id is None:
        raise CooldownError("LLM cooldown 无法确认当前 user_id")
    cooldown_seconds = int(config_parser.get_config("cd_seconds", 120) or 0)
    action_store = default_cooldown_store if cooldown_store is None else cooldown_store
    cooldown_claim = await action_store.claim(
        user_id=user_id,
        event_time=event.time,
        cooldown_seconds=cooldown_seconds,
    )
    if cooldown_claim.retry_after_seconds:
        sender_name = getattr(event.sender, "card", None) or event.sender.nickname
        await matcher.finish(
            f"{sender_name}的 LLM 对话冷却中，请在 "
            f"{cooldown_claim.retry_after_seconds} 秒后重试。"
        )

    is_finished: str | bool = False
    try:
        async with timeout_scope(
            config_parser.get_config("request_timeout_seconds", 180)
        ):
            async with get_llm_controller().slot(user_id):
                snapshot = runtime_snapshots.current()
                with runtime_snapshots.bind(snapshot):
                    temp = (
                        "ai助手"
                        if is_ai
                        else temperament_manager.get_temperament(user_id)
                    )
                    if not temp:
                        await matcher.finish("出错了，赶快喊机器人主人来修复一下吧~")

                    llm_chat = llm.MoeLlm(
                        bot, event, format_message_dict, temperament=temp
                    )
                    request_id = register_request(event, format_message_dict, is_ai)
                    try:
                        is_finished = await llm_chat.get_llm_chat()
                    finally:
                        unregister_request(request_id)
    except AdmissionRejected:
        await _release_cooldown(
            action_store,
            cooldown_claim.lease,
            user_id=user_id,
        )
        await matcher.finish("当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。")
    except TimeoutError:
        await _release_cooldown(
            action_store,
            cooldown_claim.lease,
            user_id=user_id,
        )
        await matcher.finish("本次 LLM 任务已超过总时间预算，已安全终止。")
    except asyncio.CancelledError:
        await _release_cooldown(
            action_store,
            cooldown_claim.lease,
            user_id=user_id,
        )
        await matcher.finish("当前 LLM 请求已被超级管理员终止。")

    is_repeat_ask_dict[user_id] = False
    if isinstance(is_finished, str):
        await _release_cooldown(
            action_store,
            cooldown_claim.lease,
            user_id=user_id,
        )
        await matcher.finish(is_finished)
    elif not is_finished:
        await _release_cooldown(
            action_store,
            cooldown_claim.lease,
            user_id=user_id,
        )

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
import time
import re

from nonebot.adapters import Bot
from nonebot.adapters import Event as MessageEvent
from nonebot.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.log import logger

from . import full_metrics as _full_metrics
from . import moe_llm as llm
from .admission import AdmissionGateProtocol, AdmissionRejected, AdmissionQueueTimeout, get_llm_controller, queued_slot
from .agent_context_runtime import (
    AgentGenerationCoordinator,
    AgentRequestIdentity,
    AgentRequestRuntime,
    RuntimeResourceHost,
    runtime_resource_host,
)
from .agent_runtime import AgentRunState, DeadlineContext
from .compat import TimeoutError
from .compat import timeout as timeout_scope
from .config import config_parser, mention_ack_config
from .conversation_slots import conversation_slots
from .cooldowns import CooldownError, CooldownLease, CooldownStoreProtocol, MemoryCooldownStore
from .delivery_errors import reply_delivery_failure
from .group_switch import group_llm_enabled
from .onebot_facade import (
    bot_self_id,
    event_message,
    event_message_id,
    event_scene,
    event_sender_name,
    event_time_seconds,
    event_user_id,
    onebot_protocol,
)
from .protocol_context import protocol_request_scope
from .request_manager import register_request, unregister_request
from .request_recovery import recovery_service, restarting
from .request_budget import budget_authorized, budget_config, budget_limit, budget_tier, request_budget_scope
from .request_budget import authorized_request, ordinary_request
from .runtime_snapshot import runtime_snapshots
from .state_store import BoundedValueStore
from .temperament_manager import temperament_manager

cd = BoundedValueStore(lambda: 0)
default_cooldown_store = MemoryCooldownStore(cd)
is_repeat_ask_dict = BoundedValueStore(lambda: False)
_CANCEL_NOTICE_TIMEOUT_SECONDS = 1.0
_MENTION_ACK_TIMEOUT_SECONDS = 1.0


async def acknowledge_mention(bot: Bot, event: MessageEvent) -> None:
    """Acknowledge group @ and adapter-recognized nickname calls equally."""
    if not mention_ack_config.moellm_mention_ack_enabled:
        return
    if event_scene(event) != "group" or onebot_protocol(bot, event) != "onebot_v11":
        return
    message_id = event_message_id(event)
    self_id = bot_self_id(bot, event)
    if message_id is None or not self_id:
        return
    # OneBot removes the leading @Bot before matcher handlers run.
    message = getattr(event, "original_message", None)
    if message is None:
        message = event_message(event)
    mentioned = any(
        getattr(segment, "type", "") == "at"
        and isinstance(data := getattr(segment, "data", None), Mapping)
        and str(data.get("qq", "")) == self_id
        for segment in message
    )
    # Nicknames have also been removed by the adapter. Use its to_me decision
    # and the original leading text, so an embedded name or quoted reply does
    # not acquire a reaction merely because it contains the bot's name.
    nicknames = getattr(getattr(bot, "config", None), "nickname", ())
    names = "|".join(re.escape(name) for name in nicknames if isinstance(name, str) and name)
    first = next(iter(message), None)
    text = getattr(first, "data", {}).get("text", "") if getattr(first, "type", "") == "text" else ""
    nickname_call = bool(getattr(event, "to_me", False) and names
                         and re.match(rf"^({names})([\s,，]*|$)", text, re.IGNORECASE))
    if not mentioned and not nickname_call:
        return
    try:
        async with timeout_scope(_MENTION_ACK_TIMEOUT_SECONDS):
            await bot.call_api(
                "set_msg_emoji_like", message_id=int(message_id),
                emoji_id=str(mention_ack_config.moellm_mention_ack_emoji_id), set=True,
            )
    except Exception as error:
        logger.debug("LLM 唤醒反馈未完成，继续处理对话：error_type={}", type(error).__name__)


async def chat_rule(bot: Bot, event: MessageEvent) -> bool:
    from .event_simulator import is_synthetic_event

    if is_synthetic_event():
        return False
    scene = event_scene(event)
    if scene in {"group", "channel"}:
        return group_llm_enabled(event)
    if scene == "private":
        bot_config = getattr(bot, "config", None)
        return bool(
            config_parser.get_config("private_chat_enabled")
            and event_user_id(event) in {str(value) for value in getattr(bot_config, "superusers", set())}
        )
    return False


def reset_user_runtime_state(user_id: int | str) -> None:
    default_cooldown_store.reset_user(str(user_id))
    is_repeat_ask_dict[user_id] = False


def reset_all_runtime_state() -> None:
    default_cooldown_store.clear()
    is_repeat_ask_dict.clear()


async def _release_cooldown(
    store: CooldownStoreProtocol,
    lease: CooldownLease | None,
    *,
    user_id: int | str,
    not_admitted: bool = False,
) -> None:
    if lease is not None and (not_admitted or not ordinary_request()):
        await store.release(lease)
    is_repeat_ask_dict[user_id] = False


async def _send_cancel_notice(matcher) -> None:
    try:
        async with timeout_scope(_CANCEL_NOTICE_TIMEOUT_SECONDS):
            await matcher.send("当前 LLM 请求已被超级管理员终止。")
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.debug(
            "取消通知发送失败，保持取消传播: error_type={}",
            type(error).__name__,
        )


@asynccontextmanager
async def _generation_redis_lease(
    host: RuntimeResourceHost,
    *,
    enabled: bool,
) -> AsyncIterator[AgentGenerationCoordinator | None]:
    if not enabled:
        yield None
        return

    snapshot = runtime_snapshots.current()
    if snapshot is None:
        raise RuntimeError("LLM runtime snapshot 尚未发布")
    with runtime_snapshots.bind(snapshot):
        async with host.lease(snapshot) as coordinator:
            yield coordinator


async def handle_llm(
    bot: Bot, event: MessageEvent, matcher,
    format_message_dict: dict | Callable[[], Awaitable[dict]], is_ai=False, **kwargs,
):
    from .usage_ledger import bind_usage_scope
    if not group_llm_enabled(event):
        await matcher.finish("本群 LLM 功能已关闭，请联系超级管理员开启。")
        return None
    authorized = await budget_authorized(bot, event)
    from .chat_policy import enforce_conduct
    if not await enforce_conduct(bot, event, matcher, authorized=authorized):
        return None
    async with conversation_slots.slot(event_user_id(event)) as acquired:
        if not acquired:
            from .runtime_metrics import runtime_metrics
            runtime_metrics.llm_rejected += 1
            await matcher.finish("七七正在思考你的上一个问题，请耐心等待。")
            return None
        with request_budget_scope(authorized=authorized), bind_usage_scope(bot, event):
            return await _handle_llm(bot, event, matcher, format_message_dict, is_ai, **kwargs)


async def _handle_llm(
    bot: Bot,
    event: MessageEvent,
    matcher,
    format_message_dict: dict | Callable[[], Awaitable[dict]],
    is_ai=False,
    *,
    cooldown_store: CooldownStoreProtocol | None = None,
    admission_controller: AdmissionGateProtocol | None = None,
    resource_host: RuntimeResourceHost | None = None,
):
    normalized_user_id = event_user_id(event)
    if not normalized_user_id:
        raise CooldownError("LLM cooldown 无法确认当前 user_id")
    sender = getattr(event, "sender", None)
    raw_user_id = getattr(
        event,
        "user_id",
        getattr(sender, "user_id", normalized_user_id),
    )
    user_id: int | str = raw_user_id if isinstance(raw_user_id, (int, str)) else normalized_user_id
    cooldown_seconds = 0 if authorized_request() else int(config_parser.get_config("cd_seconds", 30) or 0)
    deadline = DeadlineContext.from_timeout(budget_limit(
        "request_timeout_seconds", config_parser.get_config("request_timeout_seconds", 180)))
    selected_host = runtime_resource_host if resource_host is None else resource_host
    use_generation_cooldown = (
        resource_host is not None and cooldown_store is None and selected_host.settings.redis_cooldowns is not None
    )
    use_generation_admission = (
        resource_host is not None and admission_controller is None and selected_host.settings.redis_admission is not None
    )
    is_finished: str | bool = False
    agent_request: AgentRequestRuntime | None = None
    admission_wait_seconds = 0.0

    async def execute_bound_agent(
        coordinator: AgentGenerationCoordinator,
    ) -> None:
        nonlocal format_message_dict
        if not group_llm_enabled(event):
            await matcher.finish("本群 LLM 功能已关闭，请联系超级管理员开启。")
            return
        # Waiting requests must not download/parse attachments or write a
        # recovery record until there is capacity to process them.
        if callable(format_message_dict):
            format_message_dict = await format_message_dict()
        await recovery_service.run(
            bot, event, format_message_dict, is_ai,
            lambda: execute_recoverable_agent(coordinator),
        )

    async def execute_recoverable_agent(
        coordinator: AgentGenerationCoordinator,
    ) -> None:
        nonlocal agent_request, is_finished
        temp = "ai助手" if is_ai else temperament_manager.get_temperament(user_id)
        if not temp:
            await matcher.finish("出错了，赶快喊机器人主人来修复一下吧~")

        request_id = register_request(event, format_message_dict, is_ai)
        execution_started = time.monotonic()
        logger.info("LLM 请求开始：request={} tier={} queue_ms={}",
                    request_id, budget_tier(), int(admission_wait_seconds * 1000))
        try:
            try:
                coordinator.resources.metrics.observe_duration(
                    _full_metrics.FullDurationMetric.QUEUE_DURATION,
                    admission_wait_seconds,
                )
            except Exception:
                pass
            agent_request = await AgentRequestRuntime.begin(
                coordinator,
                AgentRequestIdentity.from_event(
                    event,
                    platform=(onebot_protocol(bot, event) or "onebot"),
                ),
                request_id=request_id,
                deadline=deadline,
            )
            llm_chat = llm.MoeLlm(
                bot,
                event,
                format_message_dict,
                temperament=temp,
                agent_runtime=agent_request,
            )
            try:
                is_finished = await llm_chat.get_llm_chat()
                if isinstance(is_finished, str) or not is_finished:
                    from .request_recovery import current_binding
                    if binding := current_binding():
                        binding.record["model_failed"] = True
                if (isinstance(is_finished, str) or not is_finished) and not agent_request.run.is_terminal:
                    await agent_request.finish_exception(AgentRunState.FAILED)
                elif is_finished and not agent_request.run.is_terminal:
                    await agent_request.finish_success()
            except asyncio.CancelledError as error:
                if not agent_request.run.is_terminal:
                    state = AgentRunState.TIMED_OUT if deadline.remaining() <= 0 else AgentRunState.CANCELLED
                    await agent_request.finish_exception(state, error)
                raise
            except (ActionFailed, NetworkError, ApiNotAvailable) as error:
                if not agent_request.run.is_terminal:
                    await agent_request.finish_exception(AgentRunState.FAILED, error)
                # QQ may have accepted the message before its ack timed out.
                # End this request, release its leases, and never send it again
                # or start another model/tool turn merely to obtain an ack.
                logger.warning("OneBot 回复投递结束，未自动重发：{}", reply_delivery_failure(error))
            except Exception as error:
                if not agent_request.run.is_terminal:
                    await agent_request.finish_exception(
                        AgentRunState.FAILED,
                        error,
                    )
                raise
        finally:
            state = agent_request.run.state if agent_request is not None else "not_started"
            logger.info("LLM 请求结束：request={} tier={} execution_ms={} state={}",
                        request_id, budget_tier(), int((time.monotonic() - execution_started) * 1000), state)
            unregister_request(request_id)

    async def execute_agent(coordinator: AgentGenerationCoordinator) -> None:
        bot_config = getattr(bot, "config", None)
        is_superuser = normalized_user_id in {str(value) for value in getattr(bot_config, "superusers", set())}
        async with protocol_request_scope(
            bot,
            event,
            generation=coordinator.generation,
            is_superuser=is_superuser,
        ):
            await execute_bound_agent(coordinator)

    async with _generation_redis_lease(
        selected_host,
        enabled=use_generation_cooldown or use_generation_admission,
    ) as generation_coordinator:
        if use_generation_cooldown:
            if generation_coordinator is None:
                raise RuntimeError("LLM generation cooldown 缺少 resource lease")
            action_store = generation_coordinator.resources.cooldown_store
            if action_store is None:
                raise RuntimeError("LLM generation cooldown 未组合")
        else:
            action_store = default_cooldown_store if cooldown_store is None else cooldown_store
        cooldown_claim = await action_store.claim(
            user_id=str(user_id),
            event_time=time.time(),
            cooldown_seconds=cooldown_seconds,
        )
        if cooldown_claim.retry_after_seconds:
            await matcher.finish(f"每人 {cooldown_seconds} 秒只能唤醒七七一次，请在 {cooldown_claim.retry_after_seconds} 秒后再试；这条请求已忽略。")
            return None

        admission_started_at = time.monotonic()
        try:
            async with timeout_scope(deadline.remaining()):
                if use_generation_admission:
                    if generation_coordinator is None:
                        raise RuntimeError("LLM generation admission 缺少 resource lease")
                    admission_gate = generation_coordinator.resources.admission_gate
                    if admission_gate is None:
                        raise RuntimeError("LLM generation admission 未组合")
                else:
                    admission_gate = get_llm_controller() if admission_controller is None else admission_controller
                async with queued_slot(admission_gate, user_id, timeout_seconds=budget_config.moellm_queue_wait_seconds):
                    admission_wait_seconds = max(0.0, time.monotonic() - admission_started_at)
                    if generation_coordinator is not None:
                        await execute_agent(generation_coordinator)
                    else:
                        snapshot = runtime_snapshots.current()
                        if snapshot is None:
                            raise RuntimeError("LLM runtime snapshot 尚未发布")
                        with runtime_snapshots.bind(snapshot):
                            async with selected_host.lease(snapshot) as coordinator:
                                await execute_agent(coordinator)
        except AdmissionQueueTimeout:
            logger.warning("LLM 排队超时：tier={} queue_ms={}", budget_tier(),
                           int((time.monotonic() - admission_started_at) * 1000))
            await _release_cooldown(action_store, cooldown_claim.lease, user_id=user_id, not_admitted=True)
            await matcher.finish("当前多个群正在使用 AI，七七暂时没有空闲处理名额，请稍后再试。")
        except AdmissionRejected:
            await _release_cooldown(
                action_store,
                cooldown_claim.lease,
                user_id=user_id,
                not_admitted=True,
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
            if not restarting():
                await _send_cancel_notice(matcher)
            raise
        except Exception:
            await _release_cooldown(
                action_store,
                cooldown_claim.lease,
                user_id=user_id,
            )
            raise

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

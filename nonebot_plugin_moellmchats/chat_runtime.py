import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import time

from nonebot.adapters import Bot
from nonebot.adapters import Event as MessageEvent
from nonebot.log import logger

from . import full_metrics as _full_metrics
from . import moe_llm as llm
from .admission import AdmissionGateProtocol, AdmissionRejected, get_llm_controller
from .agent_context_runtime import (
    AgentGenerationCoordinator,
    AgentRequestIdentity,
    AgentRequestRuntime,
    RuntimeResourceHost,
    runtime_resource_host,
)
from .agent_runtime import AgentRunState, DeadlineContext
from .compat import TimeoutError
from .compat import settle_awaitable, timeout as timeout_scope
from .config import config_parser
from .cooldowns import CooldownError, CooldownLease, CooldownStoreProtocol, MemoryCooldownStore
from .onebot_facade import (
    event_scene,
    event_sender_name,
    event_time_seconds,
    event_user_id,
    onebot_protocol,
)
from .protocol_context import protocol_request_scope
from .request_manager import register_request, unregister_request
from .runtime_snapshot import runtime_snapshots
from .state_store import BoundedValueStore
from .temperament_manager import temperament_manager

cd = BoundedValueStore(lambda: 0)
default_cooldown_store = MemoryCooldownStore(cd)
is_repeat_ask_dict = BoundedValueStore(lambda: False)
_CANCEL_NOTICE_TIMEOUT_SECONDS = 1.0


async def chat_rule(bot: Bot, event: MessageEvent) -> bool:
    from .event_simulator import is_synthetic_event

    if is_synthetic_event():
        return False
    scene = event_scene(event)
    if scene in {"group", "channel"}:
        return True
    if scene == "private":
        bot_config = getattr(bot, "config", None)
        return bool(
            config_parser.get_config("private_chat_enabled")
            and event_user_id(event)
            in {str(value) for value in getattr(bot_config, "superusers", set())}
        )
    return False


def reset_user_runtime_state(user_id: int | str) -> None:
    default_cooldown_store.reset_user(user_id)
    is_repeat_ask_dict[user_id] = False


def reset_all_runtime_state() -> None:
    default_cooldown_store.clear()
    is_repeat_ask_dict.clear()


async def _release_cooldown(
    store: CooldownStoreProtocol,
    lease: CooldownLease | None,
    *,
    user_id: int | str,
) -> None:
    if lease is not None:
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
    bot: Bot,
    event: MessageEvent,
    matcher,
    format_message_dict: dict,
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
    user_id: int | str = (
        raw_user_id if isinstance(raw_user_id, (int, str)) else normalized_user_id
    )
    cooldown_seconds = int(config_parser.get_config("cd_seconds", 120) or 0)
    deadline = DeadlineContext.from_timeout(config_parser.get_config("request_timeout_seconds", 180))
    selected_host = runtime_resource_host if resource_host is None else resource_host
    use_generation_cooldown = (
        resource_host is not None and cooldown_store is None and selected_host.settings.redis_cooldowns is not None
    )
    use_generation_admission = (
        resource_host is not None and admission_controller is None and selected_host.settings.redis_admission is not None
    )
    is_finished: str | bool = False
    agent_request: AgentRequestRuntime | None = None

    async def execute_bound_agent(
        coordinator: AgentGenerationCoordinator,
    ) -> None:
        nonlocal agent_request, is_finished
        temp = "ai助手" if is_ai else temperament_manager.get_temperament(user_id)
        if not temp:
            await matcher.finish("出错了，赶快喊机器人主人来修复一下吧~")

        request_id = register_request(event, format_message_dict, is_ai)
        try:
            try:
                coordinator.resources.metrics.observe_duration(
                    _full_metrics.FullDurationMetric.QUEUE_DURATION,
                    max(0.0, time.monotonic() - admission_started_at),
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
                if (isinstance(is_finished, str) or not is_finished) and not agent_request.run.is_terminal:
                    await agent_request.finish_exception(AgentRunState.FAILED)
                elif is_finished and not agent_request.run.is_terminal:
                    await agent_request.finish_success()
            except asyncio.CancelledError as error:
                if not agent_request.run.is_terminal:
                    state = AgentRunState.TIMED_OUT if deadline.remaining() <= 0 else AgentRunState.CANCELLED
                    # settle 抵御二次取消：清理未完成前不放弃收尾，避免 run 悬在非终态
                    settled = await settle_awaitable(
                        agent_request.finish_exception(state, error)
                    )
                    if settled.error is not None:
                        logger.warning(
                            "取消路径收尾 agent run 失败: state={} error_type={}",
                            state.value,
                            type(settled.error).__name__,
                        )
                raise
            except Exception as error:
                if not agent_request.run.is_terminal:
                    await agent_request.finish_exception(
                        AgentRunState.FAILED,
                        error,
                    )
                raise
        finally:
            unregister_request(request_id)

    async def execute_agent(coordinator: AgentGenerationCoordinator) -> None:
        bot_config = getattr(bot, "config", None)
        is_superuser = normalized_user_id in {
            str(value) for value in getattr(bot_config, "superusers", set())
        }
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
            user_id=user_id,
            event_time=event_time_seconds(event),
            cooldown_seconds=cooldown_seconds,
        )
        if cooldown_claim.retry_after_seconds:
            sender_name = event_sender_name(event)
            await matcher.finish(f"{sender_name}的 LLM 对话冷却中，请在 {cooldown_claim.retry_after_seconds} 秒后重试。")

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
                async with admission_gate.slot(user_id):
                    if generation_coordinator is not None:
                        await execute_agent(generation_coordinator)
                    else:
                        snapshot = runtime_snapshots.current()
                        if snapshot is None:
                            raise RuntimeError("LLM runtime snapshot 尚未发布")
                        with runtime_snapshots.bind(snapshot):
                            async with selected_host.lease(snapshot) as coordinator:
                                await execute_agent(coordinator)
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
            # settle 抵御二次取消：管理员终止与超时/级联取消叠加时，
            # 冷却租约仍要释放完毕（否则用户被卡在冷却里直到 TTL 兜底）
            settled_release = await settle_awaitable(
                _release_cooldown(
                    action_store,
                    cooldown_claim.lease,
                    user_id=user_id,
                )
            )
            if settled_release.error is not None:
                logger.warning(
                    "取消路径释放冷却失败（等待 TTL 兜底）: error_type={}",
                    type(settled_release.error).__name__,
                )
            settled_notice = await settle_awaitable(_send_cancel_notice(matcher))
            if settled_notice.error is not None:
                logger.debug(
                    "取消通知发送异常: error_type={}",
                    type(settled_notice.error).__name__,
                )
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

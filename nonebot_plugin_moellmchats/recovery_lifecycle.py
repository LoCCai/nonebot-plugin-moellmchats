"""NoneBot lifecycle integration for durable LLM requests."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
import signal
import sys
import threading
import time

from nonebot.adapters import Bot  # noqa: TC002 - NoneBot resolves this injection at runtime
from nonebot.exception import FinishedException
from nonebot.log import logger

from .compat import timeout
from .request_recovery import durable_send, recovery_service

_shutdown_task: asyncio.Task | None = None
_forward_shutdown = None
_signal_installed = False


async def authorize_recovery(bot, record):
    """Use current group membership, roles, matcher rules and host preprocessors."""
    from . import ai_matcher, llm_matcher
    if record["protocol"] == "onebot_v12":
        from nonebot.adapters.onebot.v12 import GroupMessageEvent, PrivateMessageEvent
    else:
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
    data = deepcopy(record["event"])
    group = data.get("group_id")
    # Permissions from an old event are never trusted after a restart.
    async with timeout(10):
        if group is not None:
            if record["protocol"] == "onebot_v12":
                members = await bot.call_api("get_group_member_list", group_id=str(group))
                if not any(str(row.get("user_id")) == str(data["user_id"]) for row in members):
                    return None
            else:
                member = await bot.get_group_member_info(group_id=int(group), user_id=int(data["user_id"]), no_cache=True)
                if str(member.get("user_id")) != str(data["user_id"]):
                    return None
                data["sender"] = member
            cls = GroupMessageEvent
        elif data.get("channel_id") is not None:
            # No portable live channel membership/permission proof is available.
            return None
        else:
            cls = PrivateMessageEvent
        event = cls.model_validate(data)
        matcher_type = ai_matcher if record["is_ai"] else llm_matcher
        from nonebot.message import _apply_event_preprocessors, _apply_run_preprocessors
        from nonebot.rule import TrieRule
        state, cache = {}, {}
        async with AsyncExitStack() as stack:
            if not await _apply_event_preprocessors(bot=bot, event=event, state=state, stack=stack,
                                                     dependency_cache=cache, show_log=False):
                return None
            TrieRule.get_value(bot, event, state)
            if not await matcher_type.permission(bot, event, stack=stack, dependency_cache=cache):
                return None
            if not await matcher_type.rule(bot, event, state, stack=stack, dependency_cache=cache):
                return None
            if not await _apply_run_preprocessors(bot=bot, event=event, state=state, matcher=matcher_type(),
                                                  stack=stack, dependency_cache=cache):
                return None
        return event


async def resume_request(bot, event, record):
    from .chat_runtime import _handle_llm
    from .conversation_slots import conversation_slots
    from .cooldowns import MemoryCooldownStore
    from .state_store import BoundedValueStore
    from .usage_ledger import bind_usage_scope

    class RecoveryMatcher:
        async def send(self, message):
            return await durable_send(bot, event, message)

        async def finish(self, message):
            await self.send(message)
            raise FinishedException()

    # Wait for an existing owner; a saved request must not be lost to a fresh
    # request from the same QQ in another group or through another bot account.
    async with conversation_slots.slot(event.user_id, wait=True):
        with bind_usage_scope(bot, event):
            # The old request must not fail its own cooldown after startup.
            event.time = time.time()
            return await _handle_llm(bot, event, RecoveryMatcher(), record["formatted"], record["is_ai"],
                                     cooldown_store=MemoryCooldownStore(BoundedValueStore(lambda: 0)))


def defer_shutdown(signum: int, forward) -> bool:
    """Defer the first signal for a bounded checkpoint drain; preserve escalation."""
    global _shutdown_task, _forward_shutdown
    recovery_service.begin_shutdown()
    if _shutdown_task is not None:
        # Forward the deferred first signal, then let the caller forward the
        # second one. Uvicorn keeps its original repeated-signal behavior.
        if _forward_shutdown is not None:
            _forward_shutdown()
        return False
    if not recovery_service.tasks and not any(not task.done() for task in recovery_service.workers.values()):
        return False
    forwarded = False

    def forward_once():
        nonlocal forwarded
        if not forwarded:
            forwarded = True
            forward()

    _forward_shutdown = forward_once

    async def drain_then_stop():
        try:
            logger.info("LLM 正在保存重启任务，最多等待在途工作 {} 秒", recovery_service_config().moellm_restart_grace_seconds)
            await recovery_service.drain()
        finally:
            forward_once()

    _shutdown_task = asyncio.create_task(drain_then_stop(), name="moellm-restart-drain")
    return True


def recovery_service_config():
    from .config import recovery_config
    return recovery_config


def install_signal_guard() -> bool:
    global _signal_installed
    if threading.current_thread() is not threading.main_thread():
        return False
    # Qiqi already owns the process signal wrapper. Register a deferral with
    # that wrapper so its Reader/browser teardown starts after our drain.
    host = sys.modules.get("src.qi_web.reader_runtime")
    setter = getattr(host, "set_shutdown_deferral", None)
    if callable(setter):
        setter(defer_shutdown)
        _signal_installed = True
        return True
    if _signal_installed:
        return False
    installed = False
    for number in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(number)
        if not callable(previous):
            continue

        def guarded(signum, frame, *, _previous=previous):
            if not defer_shutdown(signum, lambda: _previous(signum, frame)):
                return _previous(signum, frame)

        signal.signal(number, guarded)
        installed = True
    _signal_installed = installed
    return installed


async def start_recovery() -> None:
    await recovery_service.start()
    recovery_service.authorize, recovery_service.resume = authorize_recovery, resume_request
    asyncio.get_running_loop().call_soon(install_signal_guard)


async def connect_recovery(bot: Bot) -> None:
    install_signal_guard()
    recovery_service.connect(bot)

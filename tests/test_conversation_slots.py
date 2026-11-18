from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nonebot_plugin_moellmchats import chat_runtime, recovery_lifecycle
from nonebot_plugin_moellmchats.conversation_slots import ConversationSlots
from nonebot_plugin_moellmchats.cooldowns import CooldownClaim, CooldownLease


class Finished(RuntimeError):
    pass


class Matcher:
    def __init__(self):
        self.messages = []

    async def finish(self, text):
        self.messages.append(text)
        raise Finished()

    async def send(self, text):
        self.messages.append(text)


def event(user_id=42, group_id=100):
    return SimpleNamespace(
        user_id=user_id, group_id=group_id, time=1000, sender=SimpleNamespace(user_id=user_id, nickname="用户")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("nickname", [False, True])
@pytest.mark.parametrize("busy", [False, True])
async def test_busy_question_gets_only_error_reaction(monkeypatch, nickname, busy):
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from test_chat_runtime import _mention_event

    slots = ConversationSlots()
    monkeypatch.setattr(chat_runtime, "conversation_slots", slots)
    monkeypatch.setattr(chat_runtime, "budget_authorized", AsyncMock(return_value=False))
    monkeypatch.setattr("nonebot_plugin_moellmchats.chat_policy.enforce_conduct", AsyncMock(return_value=True))
    work = AsyncMock()
    monkeypatch.setattr(chat_runtime, "_handle_llm", work)
    bot = SimpleNamespace(self_id="123", config=SimpleNamespace(nickname={"七七"}), call_api=AsyncMock())
    message = Message("七七 问题") if nickname else Message(MessageSegment.at(123)) + "问题"
    current = _mention_event(message)
    current.to_me = True
    current.message = Message("问题")
    matcher = Matcher()
    if busy:
        async with slots.slot(42):
            with pytest.raises(Finished):
                await chat_runtime.handle_llm(bot, current, matcher, {})
        work.assert_not_awaited()
        assert len(matcher.messages) == 1
        assert matcher.messages[0].startswith("七七正在生成回复，已用时 ")
        assert matcher.messages[0].endswith("无需重复发送。")
    else:
        await chat_runtime.handle_llm(bot, current, matcher, {})
        work.assert_awaited_once()
    bot.call_api.assert_awaited_once_with(
        "set_msg_emoji_like",
        message_id=current.message_id,
        emoji_id="10060" if busy else "282",
        set=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "group_id", "is_ai"), [(42, 200, False), ("42", None, True), ("00042", 100, True)])
async def test_same_qq_is_rejected_before_formatting_and_recovery(monkeypatch, user_id, group_id, is_ai):
    monkeypatch.setattr(chat_runtime, "conversation_slots", ConversationSlots())
    started, release = asyncio.Event(), asyncio.Event()

    async def run(_bot, _event, _formatted, _is_ai, operation):
        return await operation()

    recovery = SimpleNamespace(run=AsyncMock(side_effect=run))
    monkeypatch.setattr(chat_runtime, "recovery_service", recovery)

    async def operation():
        started.set()
        await release.wait()

    async def work(bot, current, _matcher, formatted, is_ai, **_kwargs):
        # Formatting and durable recovery now run after admission inside
        # _handle_llm. Model that deferred work rather than bypassing it.
        data = await formatted() if callable(formatted) else formatted
        await recovery.run(bot, current, data, is_ai, operation)

    inner = AsyncMock(side_effect=work)
    monkeypatch.setattr(chat_runtime, "_handle_llm", inner)
    first_format, duplicate_format = AsyncMock(return_value={}), AsyncMock(return_value={})
    first = asyncio.create_task(chat_runtime.handle_llm(object(), event(), Matcher(), first_format))
    try:
        await asyncio.wait_for(started.wait(), 1)
        for reset in (lambda: None, lambda: chat_runtime.reset_user_runtime_state(42), chat_runtime.reset_all_runtime_state):
            reset()
            matcher = Matcher()
            with pytest.raises(Finished):
                await chat_runtime.handle_llm(object(), event(user_id, group_id), matcher, duplicate_format, is_ai)
            assert len(matcher.messages) == 1
            assert matcher.messages[0].startswith("七七正在生成回复，已用时 ")
        first_format.assert_awaited_once()
        duplicate_format.assert_not_awaited()
        recovery.run.assert_awaited_once()
        inner.assert_awaited_once()
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_different_qqs_can_run_concurrently_in_same_group(monkeypatch):
    monkeypatch.setattr(chat_runtime, "conversation_slots", ConversationSlots())
    entered, release = asyncio.Event(), asyncio.Event()
    active = set()

    async def work(_bot, current, *_args, **_kwargs):
        active.add(current.user_id)
        if len(active) == 2:
            entered.set()
        await release.wait()

    monkeypatch.setattr(chat_runtime, "_handle_llm", work)
    tasks = [asyncio.create_task(chat_runtime.handle_llm(object(), event(qq), Matcher(), {})) for qq in (42, 43)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert active == {42, 43}
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_queued_request_owns_slot_and_duplicate_does_not_release_cooldown(monkeypatch):
    slots = ConversationSlots()
    monkeypatch.setattr(chat_runtime, "conversation_slots", slots)
    monkeypatch.setattr(
        chat_runtime.config_parser,
        "get_config",
        lambda key, default=None: {"cd_seconds": 0, "request_timeout_seconds": 30}.get(key, default),
    )
    monkeypatch.setattr(chat_runtime, "budget_authorized", AsyncMock(return_value=True))
    queued = asyncio.Event()
    lease = CooldownLease("42", "a" * 32, 1000)
    cooldown = SimpleNamespace(claim=AsyncMock(return_value=CooldownClaim(lease, 0)), release=AsyncMock(return_value=True))

    class Queue:
        @asynccontextmanager
        async def slot(self, _key):
            queued.set()
            await asyncio.Event().wait()
            yield

    chat_runtime.reset_all_runtime_state()
    first = asyncio.create_task(
        chat_runtime.handle_llm(object(), event(), Matcher(), {}, admission_controller=Queue(), cooldown_store=cooldown)
    )
    try:
        await asyncio.wait_for(queued.wait(), 1)
        cooldown.claim.assert_awaited_once()
        cooldown.release.assert_not_awaited()
        with pytest.raises(Finished):
            await chat_runtime.handle_llm(object(), event("42", 200), Matcher(), {})
        cooldown.claim.assert_awaited_once()
        cooldown.release.assert_not_awaited()
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    cooldown.release.assert_awaited_once_with(lease)
    async with slots.slot(42) as acquired:
        assert acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("failed"), TimeoutError(), asyncio.CancelledError()])
async def test_failure_or_cancellation_releases_ownership(monkeypatch, failure):
    slots = ConversationSlots()
    monkeypatch.setattr(chat_runtime, "conversation_slots", slots)
    monkeypatch.setattr(chat_runtime, "_handle_llm", AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)):
        await chat_runtime.handle_llm(object(), event(), Matcher(), {})
    async with slots.slot("42") as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_input_format_failure_releases_slot_without_starting_request(monkeypatch):
    slots = ConversationSlots()
    monkeypatch.setattr(chat_runtime, "conversation_slots", slots)

    async def process(_bot, _event, _matcher, formatted, _is_ai, **_kwargs):
        await formatted()

    inner = AsyncMock(side_effect=process)
    monkeypatch.setattr(chat_runtime, "_handle_llm", inner)
    with pytest.raises(ValueError, match="bad input"):
        await chat_runtime.handle_llm(object(), event(), Matcher(), AsyncMock(side_effect=ValueError("bad input")))
    inner.assert_awaited_once()
    async with slots.slot(42) as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_saved_recovery_waits_then_blocks_fresh_requests(monkeypatch):
    slots = ConversationSlots()
    monkeypatch.setattr(chat_runtime, "conversation_slots", slots)
    monkeypatch.setattr("nonebot_plugin_moellmchats.conversation_slots.conversation_slots", slots)
    resumed, release = asyncio.Event(), asyncio.Event()

    async def work(*_args, **_kwargs):
        resumed.set()
        await release.wait()
        return True

    inner = AsyncMock(side_effect=work)
    monkeypatch.setattr(chat_runtime, "_handle_llm", inner)
    async with slots.slot(42):
        task = asyncio.create_task(
            recovery_lifecycle.resume_request(
                object(),
                event("42", 200),
                {"formatted": {}, "is_ai": False},
            )
        )
        await asyncio.sleep(0)
        inner.assert_not_awaited()
    try:
        await asyncio.wait_for(resumed.wait(), 1)
        with pytest.raises(Finished):
            await chat_runtime.handle_llm(object(), event(42, None), Matcher(), {})
        inner.assert_awaited_once()
    finally:
        release.set()
        assert await task
    async with slots.slot(42) as acquired:
        assert acquired


@pytest.mark.asyncio
async def test_cancelling_waiting_recovery_does_not_release_current_owner():
    slots = ConversationSlots()

    async def waiting():
        async with slots.slot("42", wait=True):
            pytest.fail("recovery must wait")

    async with slots.slot(42):
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with slots.slot(42) as acquired:
            assert not acquired


@pytest.mark.asyncio
async def test_ownership_survives_config_generation_change(monkeypatch):
    slots = ConversationSlots()
    monkeypatch.setattr(chat_runtime, "conversation_slots", slots)
    # Config/resource reload may replace admission gates, never account owners.
    async with slots.slot(42):
        monkeypatch.setattr(chat_runtime, "get_llm_controller", lambda: object())
        with pytest.raises(Finished):
            await chat_runtime.handle_llm(object(), event("42", 300), Matcher(), {})


@pytest.mark.asyncio
async def test_busy_message_names_native_command_and_restores_after_cancellation():
    slots = ConversationSlots()
    entered = asyncio.Event()

    async def native_tool():
        async with slots.activity("/造梦资料 我的"):
            entered.set()
            await asyncio.Event().wait()

    async with slots.slot(42):
        task = asyncio.create_task(native_tool())
        await entered.wait()
        assert "正在执行 /造梦资料（查询、渲染或发送）" in slots.busy_message("00042")
        assert "我的" not in slots.busy_message(42)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "正在生成回复" in slots.busy_message(42)
    assert slots._activity == {}
    async with slots.slot(42) as acquired:
        assert acquired

from __future__ import annotations

import asyncio
from dataclasses import replace
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from test_llm_tools import Harness, _call, _source_progress_view

from nonebot_plugin_moellmchats import llm_tools as module
from nonebot_plugin_moellmchats.moe_llm import MoeLlm
from nonebot_plugin_moellmchats.tool_contracts import ToolSpec
from nonebot_plugin_moellmchats.tool_providers import ToolSource
from nonebot_plugin_moellmchats.tool_manager import LlmToolExecutionRoute


@pytest_asyncio.fixture(autouse=True)
async def progress_tasks(monkeypatch):
    original_get_config = module.config_parser.get_config
    monkeypatch.setattr(module.config_parser, "get_config", lambda key, default=None: (
        True if key == "tool_progress_messages_enabled" else original_get_config(key, default)
    ))
    yield
    tasks = tuple(module._pending_progress_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def recall(monkeypatch):
    utility = ModuleType("src.utils.onebot_recall")
    utility.recall_message = AsyncMock()
    monkeypatch.setitem(sys.modules, utility.__name__, utility)
    return utility.recall_message


async def progress(harness, identifier=1):
    await harness._send_tool_progress(
        call=_call(identifier, "registered_demo"),
        view=_source_progress_view(ToolSource.REGISTERED),
        arguments={}, result_text="",
    )


@pytest.mark.asyncio
async def test_each_distinct_plugin_command_gets_notice_and_independent_recall(recall):
    harness = Harness({})
    harness.bot.send = AsyncMock(side_effect=[{'message_id': -501}, {'message_id': -502}])
    view = replace(_source_progress_view(ToolSource.REGISTERED), route=LlmToolExecutionRoute.NONEBOT_PLUGIN, source=ToolSource.NONEBOT_PLUGIN)
    for i, command in enumerate(['/demo 1区', '/demo 2区', '/demo   2区'], 1):
        await harness._send_tool_progress(call=_call(i, 'demo'), view=view,
            arguments={'command': command}, result_text='前置说明')
    assert harness.bot.send.await_count == 2
    assert '七七正在帮你投递：/demo 1区' in harness.bot.send.call_args_list[0].args[1]
    assert harness.bot.send.call_args_list[1].args[1] == '七七正在帮你投递：/demo 2区'
    assert harness._tool_progress_status(_call(3, 'demo')) == 'suppressed'
    await harness._finish_tool_progress()
    assert {call.kwargs['message_id'] for call in recall.await_args_list} == {-501, -502}


@pytest.mark.asyncio
async def test_uncertain_plugin_notice_is_suppressed_only_for_same_command(recall):
    harness = Harness({})
    harness.bot.send = AsyncMock(return_value={})
    view = replace(_source_progress_view(ToolSource.REGISTERED), route=LlmToolExecutionRoute.NONEBOT_PLUGIN, source=ToolSource.NONEBOT_PLUGIN)
    for i, command in enumerate(['/demo 1区', '/demo 1区', '/demo 2区'], 1):
        await harness._send_tool_progress(call=_call(i, 'demo'), view=view,
            arguments={'command': command}, result_text='')
    assert harness.bot.send.await_count == 2
    await harness._finish_tool_progress()
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_tools_and_model_rounds_share_one_notice(recall):
    queried = []

    async def query(page):
        queried.append(page)
        return f"page {page}"

    spec = ToolSpec(name="knowledge_read", description="read", handler=query,
        parameters={"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"]})
    harness = Harness({spec.name: spec.as_legacy_schema()})
    harness.bot.send = AsyncMock(return_value={"message_id": -123})

    first = await harness._execute_tools([
        _call(1, spec.name, '{"page":1}'), _call(2, spec.name, '{"page":2}')
    ], "", [], "")
    second = await harness._execute_tools([_call(3, spec.name, '{"page":3}')], "", [], "")

    assert queried == [1, 2, 3]
    assert "page 1" in first[1]["content"]
    assert "page 2" in first[2]["content"]
    assert "page 3" in second[-1]["content"]
    assert harness.bot.send.await_count == 1
    assert harness._tool_progress_status(_call(2, spec.name)) == "suppressed"
    await harness._finish_tool_progress()
    recall.assert_awaited_once_with(harness.bot, message_id=-123)


@pytest.mark.asyncio
async def test_concurrent_calls_and_late_receipt_after_completion(monkeypatch, recall):
    released = asyncio.Event()
    sends = 0

    async def slow_send(_event, _message):
        nonlocal sends
        sends += 1
        await released.wait()
        return {"message_id": -456}

    harness = Harness({})
    harness.bot.send = slow_send
    monkeypatch.setattr(module, "_PROGRESS_SEND_TIMEOUT_SECONDS", 0.01)
    await asyncio.gather(progress(harness, 1), progress(harness, 2))
    assert sends == 1
    assert harness._tool_progress_status(_call(1, "registered_demo")) == "pending"
    assert harness._tool_progress_status(_call(2, "registered_demo")) == "suppressed"
    await harness._finish_tool_progress()
    recall.assert_not_awaited()
    tasks = tuple(module._pending_progress_tasks)
    released.set()
    await asyncio.gather(*tasks)
    recall.assert_awaited_once_with(harness.bot, message_id=-456)
    await harness._finish_tool_progress()
    assert recall.await_count == 1


@pytest.mark.asyncio
async def test_notice_expires_during_long_request_and_is_not_recalled_twice(monkeypatch, recall):
    harness = Harness({})
    harness.bot.send = AsyncMock(return_value={"message_id": "-789"})
    monkeypatch.setattr(module, "_PROGRESS_RECALL_DELAY_SECONDS", 0.01)
    await progress(harness)
    await harness._tool_progress_recall_timer
    await progress(harness, 2)
    await harness._finish_tool_progress()
    assert harness.bot.send.await_count == 1
    recall.assert_awaited_once_with(harness.bot, message_id=-789)


@pytest.mark.asyncio
async def test_completion_does_not_cancel_already_started_timer_recall(monkeypatch, recall):
    entered, released = asyncio.Event(), asyncio.Event()

    async def delayed(*args, **kwargs):
        entered.set()
        await released.wait()

    recall.side_effect = delayed
    harness = Harness({})
    harness.bot.send = AsyncMock(return_value={"message_id": -888})
    monkeypatch.setattr(module, "_PROGRESS_RECALL_DELAY_SECONDS", 0)
    await progress(harness)
    await entered.wait()
    await harness._finish_tool_progress()
    assert not harness._tool_progress_recall_timer.done()
    released.set()
    await harness._tool_progress_recall_timer
    recall.assert_awaited_once_with(harness.bot, message_id=-888)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, {}, {"message_id": True}, {"message_id": 0},
    {"message_id": "not-an-id"}, {"message_id": 1.5}])
async def test_unconfirmed_receipt_is_never_retried_or_recalled(recall, receipt):
    harness = Harness({})
    harness.bot.send = AsyncMock(return_value=receipt)
    await progress(harness)
    await progress(harness, 2)
    await harness._finish_tool_progress()
    assert harness.bot.send.await_count == 1
    recall.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_failed_or_timed_out_send_is_never_retried(monkeypatch, recall, slow):
    async def send(_event, _message):
        if slow:
            await asyncio.Event().wait()
        raise RuntimeError("send failed")

    harness = Harness({})
    harness.bot.send = AsyncMock(side_effect=send)
    monkeypatch.setattr(module, "_PROGRESS_DELIVERY_TIMEOUT_SECONDS", 0.01)
    await progress(harness)
    await progress(harness, 2)
    await harness._finish_tool_progress()
    assert harness.bot.send.await_count == 1
    assert harness._tool_progress_status(_call(1, "registered_demo")) == ("timed_out" if slow else "failed")
    recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_request_consumes_late_receipt(monkeypatch, recall):
    sending, released = asyncio.Event(), asyncio.Event()

    async def send(_event, _message):
        sending.set()
        await released.wait()
        return {"message_id": -999}

    chat = object.__new__(MoeLlm)
    harness = Harness({})
    chat.bot, chat.event, chat.tool_snapshot = harness.bot, harness.event, harness.tool_snapshot
    chat.bot.send = send
    monkeypatch.setattr(module, "_PROGRESS_SEND_TIMEOUT_SECONDS", 1)

    async def body():
        await progress(chat)

    chat._get_llm_chat = body
    request = asyncio.create_task(chat.get_llm_chat())
    await sending.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    released.set()
    await asyncio.gather(*tuple(module._pending_progress_tasks))
    recall.assert_awaited_once_with(chat.bot, message_id=-999)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [True, "validation failure", RuntimeError("model failed"), asyncio.CancelledError()])
async def test_request_exits_recall_only_notice_and_preserve_result(recall, outcome):
    chat = object.__new__(MoeLlm)
    harness = Harness({})
    chat.bot, chat.event, chat.tool_snapshot = harness.bot, harness.event, harness.tool_snapshot
    chat.bot.send = AsyncMock(side_effect=[{"message_id": -321}, {"message_id": -322}])

    async def body():
        await progress(chat)
        if isinstance(outcome, BaseException):
            raise outcome
        # A final answer has a different receipt; it must never be recalled.
        await chat.bot.send(chat.event, "answer")
        return outcome

    chat._get_llm_chat = body
    if isinstance(outcome, BaseException):
        with pytest.raises(type(outcome)):
            await chat.get_llm_chat()
    else:
        assert await chat.get_llm_chat() == outcome
    await chat._finish_tool_progress()
    recall.assert_awaited_once_with(chat.bot, message_id=-321)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", [False, True])
async def test_failed_and_slow_recall_do_not_replace_answer(monkeypatch, recall, slow):
    chat = object.__new__(MoeLlm)
    chat.bot = object()
    chat._tool_progress_message_id = -777
    chat._get_llm_chat = AsyncMock(return_value="answer")

    async def stalled(*args, **kwargs):
        if slow:
            await asyncio.Event().wait()
        raise RuntimeError("recall failed")

    recall.side_effect = stalled
    monkeypatch.setattr(module, "_PROGRESS_RECALL_TIMEOUT_SECONDS", 0.01)
    assert await asyncio.wait_for(chat.get_llm_chat(), timeout=1) == "answer"
    await chat._finish_tool_progress()
    assert recall.await_count == 1

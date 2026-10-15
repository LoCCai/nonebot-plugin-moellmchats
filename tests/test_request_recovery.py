from __future__ import annotations

import asyncio
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
import pytest
import pytest_asyncio

from nonebot_plugin_moellmchats import recovery_lifecycle
from nonebot_plugin_moellmchats.request_recovery import (
    RecoveryBinding,
    RecoveryService,
    RecoveryStorageError,
    RecoveryStore,
    bind_recovery,
    current_binding,
    durable_reply,
    durable_send,
    resume_checkpoint,
)


def event(message_id=123):
    return GroupMessageEvent.model_validate({
        "time": 1792050000, "self_id": 99999, "post_type": "message", "message_type": "group",
        "sub_type": "normal", "user_id": 42, "group_id": 55555, "message_id": message_id,
        "message": [{"type": "text", "data": {"text": "请查询并画图"}}], "raw_message": "请查询并画图",
        "font": 0, "sender": {"user_id": 42, "nickname": "用户", "role": "member"}, "to_me": True,
    })


def bot():
    return SimpleNamespace(self_id="99999", adapter=SimpleNamespace(get_name=lambda: "OneBot V11"),
                           send=AsyncMock(return_value={"message_id": 101}))


@pytest_asyncio.fixture
async def service(tmp_path):
    service = RecoveryService()
    await service.start(root=tmp_path / "recovery")
    service.authorize = AsyncMock(return_value=event())
    service.resume = AsyncMock(return_value=True)
    yield service
    service.store.close()


async def reboot(service):
    root = service.store.root
    service.store.close()
    restored = RecoveryService()
    await restored.start(root=root)
    restored.authorize, restored.resume = service.authorize, service.resume
    return restored


@pytest.mark.asyncio
async def test_final_text_and_image_survive_restart_and_are_sent_once(service):
    target = bot()
    async def generate():
        service.begin_shutdown()
        await durable_reply(target, event(), ["生成的文字", MessageSegment.image(b"picture")])
    await service.run(target, event(), {"text": ["请画图"]}, False, generate)
    target.send.assert_not_awaited()
    record, = service.store.records()
    assert record["answer_ready"]
    assert all(e["state"] == "pending" for e in record["outbox"])
    restored = await reboot(service)
    try:
        await restored.recover(target)
        messages = [str(call.args[1]) for call in target.send.call_args_list]
        assert messages[1] == "生成的文字"
        assert "base64://" in messages[2]
        assert "CQ:at,qq=42" in messages[0]
        assert "补发" in messages[0]
        assert len(messages) == 3
        restored.resume.assert_not_awaited()
        await restored.recover(target)
        assert target.send.await_count == 3
    finally:
        restored.store.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_model_wait_and_restores_checkpoint(service):
    started = asyncio.Event()
    async def work():
        await current_binding().checkpoint([{"role": "system", "content": "not persisted"},
                                           {"role": "user", "content": "原任务"}], phase="model")
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(service.run(bot(), event(), {"text": ["原任务"]}, False, work))
    await started.wait()
    await service.drain(grace=0)
    with pytest.raises(asyncio.CancelledError):
        await task
    record, = service.store.records()
    assert record["state"] == "pending"
    assert record["checkpoint"] == [{"role": "user", "content": "原任务"}]
    restored = await reboot(service)
    try:
        await restored.recover(bot())
        assert restored.resume.await_count == 1
        assert restored.resume.call_args.args[2]["checkpoint"] == record["checkpoint"]
    finally:
        restored.store.close()


@pytest.mark.asyncio
async def test_admin_cancel_is_not_resumed(service):
    started = asyncio.Event()
    async def work():
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(service.run(bot(), event(), {}, False, work))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.store.records()[0]["state"] == "cancelled"
    await service.recover(bot())
    service.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_adapter_receipt_is_unknown_and_never_replayed(service):
    target = bot()
    target.send.return_value = None
    async def work():
        await durable_reply(target, event(), ["结果"])
    with pytest.raises(RuntimeError, match="消息回执"):
        await service.run(target, event(), {}, False, work)
    assert service.store.records()[0]["outbox"][0]["state"] == "unknown"
    target.send.return_value = {"message_id": 102}
    await service.recover(target)
    assert target.send.await_count == 2
    assert "结果未确认" in str(target.send.call_args.args[1])


@pytest.mark.asyncio
async def test_corrupt_journal_does_not_block_other_saved_results(service):
    target = bot()
    async def work():
        service.begin_shutdown()
        await durable_reply(target, event(), ["正确的结果"])
    await service.run(target, event(), {}, False, work)
    bad = service.store.path("0" * 64)
    bad.write_text("invalid JSON")
    service.stopping = False
    await service.recover(target)
    assert "正确的结果" in str(target.send.call_args.args[1])
    assert bad.read_text() == "invalid JSON"


@pytest.mark.asyncio
async def test_temporary_permission_lookup_failure_keeps_task_for_reconnect(service):
    async def work():
        service.begin_shutdown()
        raise asyncio.CancelledError()
    target = bot()
    with pytest.raises(asyncio.CancelledError):
        await service.run(target, event(), {}, False, work)
    service.stopping = False
    service.authorize.side_effect = ConnectionError("disconnected")
    await service.recover(target)
    assert service.store.records()[0]["state"] == "pending"
    target.send.assert_not_awaited()
    service.authorize.side_effect = None
    await service.recover(target)
    assert service.resume.await_count == 1


@pytest.mark.asyncio
async def test_shutdown_after_primary_failure_does_not_start_backup(service, tmp_path, monkeypatch):
    from test_model_failover import _pair

    from nonebot_plugin_moellmchats import moe_llm as module
    from nonebot_plugin_moellmchats.moe_llm import MoeLlm

    selector = _pair(tmp_path, monkeypatch)
    monkeypatch.setattr(module.model_selector, "get_rate_limit_fallback", selector.get_rate_limit_fallback)
    chat = object.__new__(MoeLlm)
    chat.model_info, chat.agent_runtime = selector.get_model("category_model"), None
    async def primary(*args):
        service.begin_shutdown()
        chat._last_api_http_status = 429
        return False, "rate limited", None, ""
    chat.none_stream_llm_chat = AsyncMock(side_effect=primary)
    async def work():
        await chat._request_with_rate_limit_fallback(object(), {}, {"messages": [], "stream": False}, False)
    with pytest.raises(asyncio.CancelledError):
        await service.run(bot(), event(), {}, False, work)
    assert chat.none_stream_llm_chat.await_count == 1
    assert service.store.records()[0]["state"] == "pending"


@pytest.mark.asyncio
async def test_unknown_send_is_never_replayed_and_user_gets_one_notice(service):
    target = bot()
    target.send.side_effect = ConnectionError("ack lost")
    async def work():
        await durable_reply(target, event(), ["不可重复的结果"])
    with pytest.raises(ConnectionError):
        await service.run(target, event(), {}, False, work)
    assert service.store.records()[0]["outbox"][0]["state"] == "unknown"
    target.send.reset_mock(side_effect=True)
    await service.recover(target)
    assert target.send.await_count == 1
    assert "结果未确认" in str(target.send.call_args.args[1])
    assert "不可重复的结果" not in str(target.send.call_args.args[1])
    service.resume.assert_not_awaited()
    await service.recover(target)
    assert target.send.await_count == 1


@pytest.mark.asyncio
async def test_send_interrupted_while_awaiting_ack_is_unknown(service):
    target, entered = bot(), asyncio.Event()
    async def send(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    target.send.side_effect = send
    async def work():
        await durable_reply(target, event(), ["正文"])
    task = asyncio.create_task(service.run(target, event(), {}, False, work))
    await entered.wait()
    await service.drain(grace=0)
    with pytest.raises(asyncio.CancelledError):
        await task
    record, = service.store.records()
    assert record["outbox"][0]["state"] == "unknown"


@pytest.mark.asyncio
async def test_all_saved_results_are_sent_before_any_model_resume(service):
    target, order = bot(), []
    async def unfinished(number):
        service.begin_shutdown()
        await durable_send(target, event(number), f"结果{number}")
        raise asyncio.CancelledError()
    for number in (1, 2):
        service.stopping = False
        with pytest.raises(asyncio.CancelledError):
            await service.run(target, event(number), {}, False, lambda: unfinished(number))
    async def send(_event, message):
        order.append(str(message))
        return {"message_id": 100}
    async def resume(*args):
        order.append("MODEL")
    target.send.side_effect = send
    service.resume.side_effect = resume
    restored = await reboot(service)
    try:
        await restored.recover(target)
        assert order.index("结果1") < order.index("MODEL")
        assert order.index("结果2") < order.index("MODEL")
    finally:
        restored.store.close()


@pytest.mark.asyncio
async def test_recovery_rechecks_access_and_never_delivers_to_removed_member(service):
    async def work():
        service.begin_shutdown()
        await durable_reply(bot(), event(), ["私有结果"])
    await service.run(bot(), event(), {}, False, work)
    restored = await reboot(service)
    target = bot()
    restored.authorize.return_value = None
    try:
        await restored.recover(target)
        target.send.assert_not_awaited()
        restored.resume.assert_not_awaited()
        assert restored.store.records()[0]["state"] == "denied"
    finally:
        restored.store.close()


@pytest.mark.asyncio
async def test_duplicate_event_does_not_execute_twice(service):
    operation = AsyncMock()
    await asyncio.gather(*(service.run(bot(), event(), {}, False, operation) for _ in range(2)))
    assert operation.await_count == 1


@pytest.mark.asyncio
async def test_storage_failure_prevents_tool_work(service, monkeypatch):
    operation = AsyncMock()
    def fail(record):
        raise OSError("disk full")
    monkeypatch.setattr(service.store, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        await service.run(bot(), event(), {}, False, operation)
    operation.assert_not_awaited()


def test_store_is_private_has_one_owner_and_rejects_symlinks(tmp_path):
    store = RecoveryStore(tmp_path / "recovery")
    store.open()
    second = RecoveryStore(store.root)
    try:
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        with pytest.raises(RecoveryStorageError, match="占用"):
            second.open()
        outside = tmp_path / "outside.json"
        outside.write_text("untouched")
        store.path("a" * 64).symlink_to(outside)
        with pytest.raises(ValueError, match="符号链接"):
            store.read("a" * 64)
        assert outside.read_text() == "untouched"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_checkpoint_does_not_save_system_prompt_or_model_credentials(service):
    async def work():
        await current_binding().checkpoint([{"role": "system", "content": "model-secret"},
                                           {"role": "user", "content": "问题"}], phase="model")
    await service.run(bot(), event(), {}, False, work)
    path, = service.store.root.glob("*.json")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "model-secret" not in path.read_text()


@pytest.mark.asyncio
async def test_first_signal_drains_then_forwards_and_second_signal_escalates(monkeypatch):
    service, forwarded, entered = RecoveryService(), [], asyncio.Event()
    async def drain():
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(service, "drain", drain)
    monkeypatch.setattr(recovery_lifecycle, "recovery_service", service)
    monkeypatch.setattr(recovery_lifecycle, "_shutdown_task", None)
    monkeypatch.setattr(recovery_lifecycle, "_forward_shutdown", None)
    service.tasks.add(asyncio.current_task())
    assert recovery_lifecycle.defer_shutdown(2, lambda: forwarded.append("first")) is True
    await entered.wait()
    assert not forwarded
    assert service.stopping
    assert recovery_lifecycle.defer_shutdown(2, lambda: forwarded.append("second")) is False
    assert forwarded == ["first"]
    recovery_lifecycle._shutdown_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery_lifecycle._shutdown_task
    assert forwarded == ["first"]


def test_repeated_tool_call_ids_do_not_hide_an_interrupted_later_turn():
    call = {"id": "call0", "function": {"name": "write", "arguments": "{}"}}
    record = {"checkpoint": [
        {"role": "assistant", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call0", "content": "第一次完成"},
        {"role": "assistant", "tool_calls": [call]},
    ], "inflight_tools": [], "outbox": []}
    messages, completed, unsafe = resume_checkpoint(record)
    assert unsafe
    assert len(completed) == 1
    assert "不得自动重放" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_resume_never_calls_tools_when_previous_mutation_is_unknown(service, monkeypatch):
    from nonebot_plugin_moellmchats import moe_llm as module
    from nonebot_plugin_moellmchats.moe_llm import MoeLlm
    target = bot()
    call = {"id": "write1", "type": "function", "function": {"name": "write", "arguments": "{}"}}
    async def interrupted():
        await current_binding().checkpoint([
            {"role": "user", "content": "修改绑定后画图"}, {"role": "assistant", "tool_calls": [call]},
        ], phase="tools", calls=[call])
        service.begin_shutdown()
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.run(target, event(), {}, False, interrupted)
    record, = service.store.records()
    record["state"] = "recovering"
    service.stopping = False
    binding = RecoveryBinding(service, record)
    chat = MoeLlm(target, event(), {"text": ["修改绑定后画图"]})
    chat.model_info = {"model": "fake", "key": "not-written-to-journal"}
    chat.is_objective = True
    chat._validate_runtime_model_config = AsyncMock(return_value=None)
    chat._prepare_model_info = AsyncMock(return_value=None)
    chat.prompt_handler = lambda: None
    chat._build_payload = lambda messages: ({"messages": messages, "tools": [{"name": "write"}], "stream": False}, False)
    chat._execute_tools = AsyncMock(side_effect=AssertionError("must not replay"))
    chat._request_with_rate_limit_fallback = AsyncMock(return_value=(True, "请核对上次操作结果", [call], "", False))
    handler = SimpleNamespace(
        pre_process=lambda *args, **kwargs: "问题", get_send_message_list=lambda **kwargs: [],
        messages_entity=SimpleNamespace(tool_messages=[], mention_context=None))
    monkeypatch.setattr(module, "MessagesHandler", lambda _: handler)
    monkeypatch.setattr(module, "get_session", object)
    monkeypatch.setattr(module.config_parser, "get_config", lambda key, default=None:
                        {"max_tool_rounds": 1, "max_agent_steps": 1, "max_retry_times": 1}.get(key, default))
    with bind_recovery(binding):
        assert await chat.get_llm_chat() is True
    chat._execute_tools.assert_not_awaited()
    data = chat._request_with_rate_limit_fallback.call_args.args[2]
    assert "tools" not in data
    assert any("不得自动重放" in m.get("content", "") for m in data["messages"])
    assert "not-written-to-journal" not in service.store.path(record["key"]).read_text()


@pytest.mark.asyncio
async def test_store_reclaims_completed_tombstones_but_preserves_pending_work(service, monkeypatch):
    from nonebot_plugin_moellmchats.config import recovery_config
    monkeypatch.setattr(recovery_config, "moellm_recovery_max_entries", 1)
    await service.run(bot(), event(1), {}, False, AsyncMock())
    await service.run(bot(), event(2), {}, False, AsyncMock())
    assert len(service.store.records()) == 1
    record, = service.store.records()
    record["state"] = "pending"
    service.store.save(record)
    operation = AsyncMock()
    with pytest.raises(RecoveryStorageError, match="已满"):
        await service.run(bot(), event(3), {}, False, operation)
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_uses_current_member_role_and_host_guards(monkeypatch):
    import nonebot.message

    import nonebot_plugin_moellmchats as plugin
    target = bot()
    target.get_group_member_info = AsyncMock(return_value={"user_id": 42, "nickname": "用户", "role": "member"})
    snapshot = event().model_dump(mode="json")
    snapshot["sender"]["role"] = "owner"
    rule = AsyncMock(return_value=True)
    matcher = SimpleNamespace(permission=AsyncMock(return_value=True), rule=rule)
    class MatcherType:
        permission, rule = matcher.permission, matcher.rule
    monkeypatch.setattr(plugin, "llm_matcher", MatcherType)
    before = AsyncMock(return_value=True)
    run_before = AsyncMock(return_value=True)
    monkeypatch.setattr(nonebot.message, "_apply_event_preprocessors", before)
    monkeypatch.setattr(nonebot.message, "_apply_run_preprocessors", run_before)
    restored = await recovery_lifecycle.authorize_recovery(target, {"protocol": "onebot_v11", "is_ai": False, "event": snapshot})
    assert restored.sender.role == "member"
    assert before.await_count == 1
    assert run_before.await_count == 1
    run_before.return_value = False
    assert await recovery_lifecycle.authorize_recovery(target,
        {"protocol": "onebot_v11", "is_ai": False, "event": snapshot}) is None

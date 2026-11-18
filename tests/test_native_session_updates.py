import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.exception import ActionFailed, StopPropagation
from nonebot.internal.matcher import MatcherSource, matchers
from nonebot.params import ArgPlainText, CommandArg
from nonebot.typing import T_State
import pytest
from test_event_simulator import _event

from nonebot_plugin_moellmchats import event_simulator as simulator
from nonebot_plugin_moellmchats.chat_history import MessageRecord
from nonebot_plugin_moellmchats.mention_context import mention_scope
from nonebot_plugin_moellmchats.messages_handler import MessagesHandler, messages_dict
from nonebot_plugin_moellmchats.native_sessions import (
    NativeSessionUpdates,
    native_session_context,
    native_session_updates,
)


@pytest.fixture
def flow(monkeypatch):
    native_session_updates.clear()
    messages_dict.clear()
    matcher = on_command("session_binding", priority=20, block=True)
    matcher._source = MatcherSource(plugin_id="native-session-test")
    saved = []
    delivery = {"result": {"message_id": 12345}, "exception": None, "extra_target": False}

    @matcher.handle()
    async def start(state: T_State, args: Message = CommandArg()):
        state["candidate"] = args.extract_plain_text().strip()
        await matcher.send("请确认绑定玩家，回复确认或取消")

    @matcher.got("confirmation")
    async def confirm(bot: Bot, event: GroupMessageEvent, state: T_State, answer: str = ArgPlainText("confirmation")):
        if answer.strip() == "取消":
            await matcher.finish("已取消，原绑定未修改。")
        if answer.strip() != "确认":
            await matcher.reject("请回复确认保存，或取消退出。")
        saved.append(state["candidate"])
        if delivery["extra_target"]:
            await bot.send_group_msg(group_id=999, message="管理员通知：其他用户尚未绑定")
        await matcher.finish("✅ 绑定成功：" + state["candidate"])

    monkeypatch.setattr(simulator, "get_plugin", lambda _: SimpleNamespace(id_="native-session-test", matcher={matcher}))

    class Adapter:
        config = SimpleNamespace(session_expire_timeout=timedelta(minutes=2))

        @staticmethod
        def get_name():
            return "OneBot V11"

        async def _call_api(self, bot, api, **data):
            if delivery["exception"]:
                raise delivery["exception"]
            return delivery["result"]

    bot = Bot(Adapter(), "10000")

    async def start_session():
        result = await simulator.event_simulator.dispatch_event(
            bot, _event(1), "/session_binding 958555417|4399|25", plugin_name="native-session-test", wait_for_result=True
        )
        assert result.succeeded
        assert not saved
        return result

    async def reply(event):
        for m in list(matchers[0]):
            if getattr(getattr(m, "_source", None), "plugin_id", None) != "native-session-test":
                continue
            try:
                if await simulator._observe_and_run_matcher(m, bot, event, {}, None, {}):
                    return True
            except StopPropagation:
                return True
        return False

    yield SimpleNamespace(start=start_session, reply=reply, saved=saved, delivery=delivery, bot=bot)
    for group in matchers.values():
        group[:] = [m for m in group if getattr(getattr(m, "_source", None), "plugin_id", None) != "native-session-test"]
    native_session_updates.clear()
    messages_dict.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_native_confirmation_updates_next_prompt_after_stale_pending_history(flow, durable):
    await flow.start()
    scope = mention_scope(_event(1))
    assert native_session_updates.snapshot(scope) == []
    assert await flow.reply(_event(2, "确认"))
    assert flow.saved == ["958555417|4399|25"]
    updates = native_session_updates.snapshot(scope)
    assert len(updates) == 1
    assert updates[0]["state"] == "ended"
    assert updates[0]["text"] == "✅ 绑定成功：958555417|4399|25"
    handler = MessagesHandler("123")
    if durable:
        now = datetime.now(timezone.utc)
        handler.bind_committed_history(
            (
                MessageRecord(
                    message_id=1,
                    conversation_id="test",
                    role="user",
                    content="绑定 UID",
                    structured_content={"mention_context": {"version": 1, "scope": scope, "targets": []}},
                    created_at=now,
                ),
                MessageRecord(
                    message_id=2, conversation_id="test", role="assistant", content="绑定校验卡已发，请确认", created_at=now
                ),
            )
        )
    else:
        handler.pre_process({"text": ["绑定 UID"]}, scope=scope)
        handler.post_process("绑定校验卡已发，请确认")
        handler = MessagesHandler("123")
    handler.pre_process({"text": ["造梦资料"]}, scope=scope)
    messages = handler.get_send_message_list()
    assert messages[-1] == {"role": "user", "content": "造梦资料"}
    assert "运行时观察到" in messages[-2]["content"]
    assert "绑定成功：958555417|4399|25" in messages[-2]["content"]
    assert '"state": "ended"' in messages[-2]["content"]
    assert handler.classification_text("造梦资料").endswith("当前请求：\n造梦资料")
    assert "绑定成功" in handler.classification_text("造梦资料")
    # Context is not consumed merely by building a prompt or binding PG history.
    assert "绑定成功" in handler.get_send_message_list()[-2]["content"]


@pytest.mark.asyncio
async def test_reject_then_confirm_replaces_pending_receipt_without_repeating_action(flow):
    await flow.start()
    assert await flow.reply(_event(2, "继续"))
    assert flow.saved == []
    scope = mention_scope(_event(2))
    assert native_session_updates.snapshot(scope)[0]["state"] == "awaiting_input"
    assert await flow.reply(_event(3, "确认"))
    rows = native_session_updates.snapshot(scope)
    assert len(rows) == 1
    assert rows[0]["state"] == "ended"
    assert "请回复确认保存" not in rows[0]["text"]
    assert not await flow.reply(_event(4, "确认"))
    assert len(flow.saved) == 1


@pytest.mark.asyncio
async def test_cancellation_is_a_finished_flow_without_claiming_binding_succeeded(flow):
    await flow.start()
    assert await flow.reply(_event(2, "取消"))
    rows = native_session_updates.snapshot(mention_scope(_event(2)))
    assert rows[0]["state"] == "ended"
    assert "已取消" in rows[0]["text"]
    assert "绑定成功" not in rows[0]["text"]
    assert not flow.saved


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"user_id": 999}, {"group_id": 999}])
async def test_native_session_permissions_and_prompt_scope_do_not_leak(flow, change):
    await flow.start()
    assert not await flow.reply(_event(2, "确认").model_copy(update=change))
    assert not flow.saved
    assert await flow.reply(_event(3, "确认"))
    for other in ({"user_id": 999}, {"group_id": 999}, {"self_id": 999}):
        assert native_session_context(mention_scope(_event(4).model_copy(update=other))) == ""
    assert native_session_context(None) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing_receipt", "rejected"])
async def test_unconfirmed_output_is_not_promoted_to_a_successful_binding(flow, mode):
    await flow.start()
    if mode == "missing_receipt":
        flow.delivery["result"] = {}
    else:
        flow.delivery["exception"] = ActionFailed("OneBot V11")
    assert await flow.reply(_event(2, "确认"))
    rows = native_session_updates.snapshot(mention_scope(_event(2)))
    assert rows[0]["state"] in {"unknown", "failed"}
    assert "绑定成功" not in rows[0]["text"]
    assert len(flow.saved) == 1  # A failed message receipt must never replay a save.


@pytest.mark.asyncio
async def test_other_target_notifications_are_not_added_to_the_user_context(flow):
    await flow.start()
    flow.delivery["extra_target"] = True
    assert await flow.reply(_event(2, "确认"))
    text = native_session_context(mention_scope(_event(2)))
    assert "绑定成功" in text
    assert "其他用户尚未绑定" not in text


def test_updates_expire_and_are_not_renewed_by_reads():
    store = NativeSessionUpdates()
    scope = mention_scope(_event(1))
    store.record(scope, {"session": "test", "text": "receipt"})
    key = store._key(scope)
    created, row = store._values[key][0]
    assert store.snapshot(scope)[0]["text"] == "receipt"
    assert store._values[key][0][0] == created
    store._values[key] = [(created - 601, row)]
    assert store.snapshot(scope) == []


def test_runtime_receipt_survives_a_small_history_budget(monkeypatch):
    from nonebot_plugin_moellmchats import messages_handler as module

    scope = mention_scope(_event(1))
    native_session_updates.record(
        scope,
        {
            "session": "budget",
            "plugin": "qi_zmws",
            "command": "/造梦绑定",
            "reply": "确认",
            "state": "ended",
            "text": "✅ 绑定成功：111111111|4399|1\n" + "后续说明" * 1000,
        },
    )
    monkeypatch.setattr(
        module,
        "request_config",
        lambda key, default=None: {"max_history_chars": 768, "max_history_tokens": 256}.get(key, default),
    )
    handler = MessagesHandler("123")
    handler.pre_process({"text": ["造梦资料"]}, scope=scope)
    messages = handler.get_send_message_list()
    assert len(messages) == 2
    assert "绑定成功" in messages[0]["content"]
    assert sum(len(message["content"]) for message in messages) <= 768
    native_session_updates.clear()
    messages_dict.clear()


@pytest.mark.asyncio
async def test_real_dispatch_exposes_slow_native_command_without_dropping_the_slot(flow, monkeypatch):
    from nonebot_plugin_moellmchats.conversation_slots import ConversationSlots

    slots = ConversationSlots()
    monkeypatch.setattr(simulator, "conversation_slots", slots)
    started, release = asyncio.Event(), asyncio.Event()
    matcher = on_command("slow_profile", priority=20, block=True)
    matcher._source = MatcherSource(plugin_id="native-session-test")

    @matcher.handle()
    async def slow_profile():
        started.set()
        await release.wait()
        await matcher.finish("资料卡发送完成")

    monkeypatch.setattr(simulator, "get_plugin", lambda _: SimpleNamespace(id_="native-session-test", matcher={matcher}))

    async def request():
        async with slots.slot("123"):
            result = await simulator.event_simulator.dispatch_event(
                flow.bot, _event(1), "/slow_profile 我的", plugin_name="native-session-test", wait_for_result=True
            )
            assert "正在生成回复" in slots.busy_message("123")
            return result

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert "正在执行 /slow_profile（查询、渲染或发送）" in slots.busy_message("123")
        async with slots.slot("123") as acquired:
            assert not acquired
    finally:
        release.set()
    assert (await task).succeeded
    assert slots._activity == {}

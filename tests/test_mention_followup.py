from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.internal.matcher import matchers
from nonebot.params import CommandArg
import pytest
from test_event_simulator import _event

from nonebot_plugin_moellmchats import event_simulator as simulator
from nonebot_plugin_moellmchats import llm_payload as payload_module
from nonebot_plugin_moellmchats.chat_history import MessageRecord
from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin
from nonebot_plugin_moellmchats.mention_context import mention_scope
from nonebot_plugin_moellmchats.messages_handler import MessagesHandler, messages_dict
from nonebot_plugin_moellmchats.model_selector import model_selector
from nonebot_plugin_moellmchats.nonebot_plugin_tools import PluginDispatchError


@pytest.fixture(autouse=True)
def _clean_history():
    messages_dict.clear()
    yield
    messages_dict.clear()


def _previous(*, event=None, targets=None):
    event = event or _event(100)
    source = {"text": ["造梦里我怎么才能打过他 对手[at:1]"],
              "mentions": targets if targets is not None else [{"qq": "1911698701", "name": "对手"}]}
    handler = MessagesHandler(str(event.user_id))
    handler.pre_process(source, scope=mention_scope(event))
    handler.post_process("可以查询双方属性再算天道修正")
    return handler


def _followup(event=None):
    event = event or _event(101)
    source = {"text": ["你自己调用工具查不行吗"], "mentions": []}
    handler = MessagesHandler(str(event.user_id))
    handler.pre_process(source, scope=mention_scope(event))
    return handler, source


def test_actual_failed_command_fingerprint_and_trusted_followup():
    encoded = json.dumps({"command": "/六维计算 [at:1]"}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert hashlib.sha256(encoded.encode()).hexdigest().startswith("57a6b476d418")
    _previous()
    handler, source = _followup()
    assert source["mentions"] == []
    assert len(source["history_mentions"]) == 1
    token = next(iter(source["history_mentions"]))
    fake, _ = simulator._build_event(_event(101), f"/六维计算 [history_at:{token}] 天道", source)
    assert fake.user_id == 123
    assert fake.group_id == 456
    assert fake.sender == _event(101).sender
    assert [seg.data["qq"] for seg in fake.message if seg.type == "at"] == ["1911698701"]
    assert "天道" in fake.message.extract_plain_text()
    with pytest.raises(simulator.MentionIdentityRequired, match="停止执行"):
        simulator._build_event(_event(101), "/六维计算 [at:1]", source)
    classification = handler.classification_text("你自己调用工具查不行吗")
    assert "造梦里我怎么才能打过他" in classification
    assert classification.endswith("当前请求：\n你自己调用工具查不行吗")


def test_committed_history_restores_identity_and_new_turn_keeps_the_same_token():
    previous = _previous()
    entity = previous.messages_entity
    first = MessageRecord(message_id=1, conversation_id="conversation-1", role="user",
                          content=entity.user_msg["content"], structured_content={"mention_context": entity.mention_context},
                          created_at=datetime.now(timezone.utc))
    handler = MessagesHandler("123")
    handler.bind_committed_history((first,))
    source = {"text": ["继续算天道"], "mentions": []}
    handler.pre_process(source, scope=mention_scope(_event(101)))
    token = next(iter(source["history_mentions"]))
    expected = entity.mention_context["targets"][0]["token"]
    assert token == expected
    # Metadata without targets is still a scoped conversational exchange.
    second = replace(first, message_id=2, content="继续算天道",
                     structured_content={"mention_context": handler.messages_entity.mention_context})
    third = MessagesHandler("123")
    third.bind_committed_history((first, second))
    later = {"text": ["他换装了吗"], "mentions": []}
    third.pre_process(later, scope=mention_scope(_event(102)))
    assert list(later["history_mentions"]) == [token]


@pytest.mark.parametrize("change", [{"user_id": 999}, {"group_id": 999}, {"self_id": 999}])
def test_history_never_crosses_user_group_or_bot_and_event_rechecks_scope(change):
    _previous()
    _, source = _followup()
    token = next(iter(source["history_mentions"]))
    other = _event(102).model_copy(update=change)
    with pytest.raises(simulator.MentionIdentityRequired, match="当前用户或群"):
        simulator._build_event(other, f"/六维计算 [history_at:{token}]", source)
    handler, other_source = _followup(other)
    assert other_source["history_mentions"] == {}
    assert handler.classification_text("你自己查") == "你自己查"


@pytest.mark.parametrize("same_target", [True, False])
def test_current_mentions_take_priority_without_retargeting_old_tokens(same_target):
    _previous()
    qq = "1911698701" if same_target else "234"
    handler = MessagesHandler("123")
    source = {"text": ["查这位[at:1]"], "mentions": [{"qq": qq, "name": "这位"}]}
    handler.pre_process(source, scope=mention_scope(_event(101)))
    fake, _ = simulator._build_event(_event(101), "/六维计算 [at:1]", source)
    assert [seg.data["qq"] for seg in fake.message if seg.type == "at"] == [qq]
    assert len(source["history_mentions"]) == (0 if same_target else 1)


@pytest.mark.parametrize("command", ["/造梦绑定", "/推送配置", "/禁言", "/通知"])
def test_historical_targets_cannot_be_used_for_management_or_binding(command):
    _previous()
    _, source = _followup()
    token = next(iter(source["history_mentions"]))
    with pytest.raises(simulator.MentionIdentityRequired):
        simulator._build_event(_event(101), f"{command} [history_at:{token}]", source)


def test_missing_legacy_metadata_does_not_guess_from_old_text_or_tool_command():
    first = MessageRecord(message_id=1, conversation_id="conversation-1", role="user",
                          content="造梦对手[at:1]的天道", created_at=datetime.now(timezone.utc))
    handler = MessagesHandler("123")
    handler.bind_committed_history((first,))
    source = {"text": ["你自己查"], "mentions": []}
    handler.pre_process(source, scope=mention_scope(_event(101)))
    assert source["history_mentions"] == {}
    assert handler.classification_text("你自己查") == "你自己查"


def test_expired_or_duplicate_targets_are_not_inherited():
    previous = _previous(targets=[{"qq": "1911698701", "name": "旧名"}, {"qq": "1911698701", "name": "重复"}])
    assert len(previous.messages_entity.mention_context["targets"]) == 1
    previous.messages_entity.timestamp -= config_parser.get_config("user_history_expire_seconds", 600) + 1
    handler, source = _followup()
    assert source["history_mentions"] == {}
    assert handler.classification_text("你自己查") == "你自己查"


def test_hint_and_tool_history_use_verified_mapping_without_exposing_account_id():
    _previous()
    _, source = _followup()
    token = next(iter(source["history_mentions"]))
    harness = LlmPayloadMixin()
    harness.format_message_dict = source
    hint = harness._build_tool_mention_hint()
    assert f"[history_at:{token}]" in hint
    assert "禁止直接复用历史" in hint
    assert "1911698701" not in hint
    assert harness._render_history_placeholders(f"/六维计算 [history_at:{token}]") == "/六维计算 @对手"


@pytest.mark.asyncio
async def test_followup_model_selection_receives_scoped_conversation_context(monkeypatch):
    previous = _previous()
    previous.messages_entity.user_msg["content"] += "[图片]"
    handler, source = _followup()
    captured = []

    class Category:
        selection_source = "classification_model"

        def __init__(self, text, *args, **kwargs):
            captured.append(text)

        async def get_category(self):
            return "2", False, ["qi_zmws", "zmws_combat_calculate"]

    monkeypatch.setattr(payload_module, "Categorize", Category)
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_moe", lambda: False)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(model_selector, "get_resident_plugins", lambda: [])
    monkeypatch.setattr(model_selector, "get_model_for_capabilities", lambda *args: {"model": "capable-model"})
    harness = LlmPayloadMixin()
    harness.event, harness.messages_handler, harness.format_message_dict = _event(101), handler, source
    harness.tool_snapshot, harness.is_superuser, harness.model_info = object(), False, {}
    assert await harness._prepare_model_info("你自己调用工具查不行吗") is None
    assert "造梦里我怎么才能打过他" in captured[0]
    assert "[图片]" not in captured[0]
    assert harness.required_plugins == ["qi_zmws", "zmws_combat_calculate"]


@pytest.mark.asyncio
async def test_stale_placeholder_returns_input_feedback_before_any_matcher_or_api(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("身份缺失时不能进入 Matcher")

    monkeypatch.setattr(simulator, "_dispatch_targeted", forbidden)
    result = await simulator.event_simulator.dispatch_event(object(), _event(100), "/六维计算 [at:1]", {},
                                                          plugin_name="qi_zmws", wait_for_result=True)
    assert result.status is simulator.PluginDispatchStatus.INPUT_REQUIRED
    assert result.matcher_checked == result.api_succeeded == result.api_failed == 0
    feedback = str(PluginDispatchError(result))
    assert "尚未执行插件或游戏查询" in feedback
    assert "请用户补充" in feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [False, True])
async def test_followup_query_uses_native_permission_with_original_invoker(monkeypatch, allow):
    _previous()
    _, source = _followup()
    token = next(iter(source["history_mentions"]))
    permission_events = []
    executed = []

    async def allowed(event: GroupMessageEvent):
        permission_events.append((event.user_id, event.group_id, event.sender.role))
        return allow

    matcher = on_command("六维计算", permission=allowed, priority=7)

    @matcher.handle()
    async def query(args: Message = CommandArg()):
        executed.append(args)
        await matcher.finish("查询完成")

    monkeypatch.setattr(simulator, "get_plugin", lambda name: SimpleNamespace(matcher={matcher}))

    class Adapter:
        @staticmethod
        def get_name():
            return "OneBot V11"

        async def _call_api(self, bot, api, **data):
            assert allow, "权限拒绝时不能发送消息"
            return {"message_id": -100}

    try:
        result = await simulator.event_simulator.dispatch_event(Bot(Adapter(), "10000"), _event(101),
            f"/六维计算 [history_at:{token}] 天道", source, plugin_name="qi_zmws", wait_for_result=True)
    finally:
        for group in matchers.values():
            if matcher in group:
                group.remove(matcher)
    assert permission_events == [(123, 456, _event(101).sender.role)]
    if allow:
        assert [seg.data["qq"] for seg in executed[0] if seg.type == "at"] == ["1911698701"]
        assert "天道" in executed[0].extract_plain_text()
        assert result.status is simulator.PluginDispatchStatus.MATCHED_WITH_OUTPUT
    else:
        assert not executed
        assert result.status is simulator.PluginDispatchStatus.NOT_MATCHED

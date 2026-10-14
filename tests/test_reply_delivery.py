import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nonebot.adapters.onebot.v11.exception import ActionFailed
import pytest

from nonebot_plugin_moellmchats import moe_llm as module
from nonebot_plugin_moellmchats.delivery_errors import reply_delivery_failure
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


def test_send_ack_timeout_is_unknown_and_diagnostics_do_not_contain_reply_or_credentials():
    error = ActionFailed(
        status="failed", retcode=100, data={"secret": "private-text"}, wording="send ack 1473 timed out after 5000 ms"
    )
    assert reply_delivery_failure(error) == {
        "error_type": "ActionFailed",
        "retcode": 100,
        "result_unknown": True,
        "retry": False,
    }


@pytest.mark.asyncio
async def test_final_reply_delivery_failure_preserves_generated_context_without_retry(monkeypatch):
    values = {"max_tool_rounds": 6, "max_retry_times": 1}
    monkeypatch.setattr(module.config_parser, "get_config", lambda key, default=None: values.get(key, default))
    chat = object.__new__(MoeLlm)
    failure = ActionFailed(status="failed", retcode=100, data=None, wording="send ack 1473 timed out after 5000 ms")
    chat.bot = SimpleNamespace(send=AsyncMock(side_effect=failure))
    chat.event = object()
    chat.user_id = "42"
    chat.agent_runtime = None
    chat.is_objective = False
    chat.emotion_flag = False
    chat.format_message_dict = {"text": ["hello"]}
    chat.prompt, chat.dynamic_context = "system", ""
    chat.model_info = {"model": "fake", "url": "https://model.invalid", "key": "test-only", "stream": False}
    messages = SimpleNamespace(
        messages_entity=SimpleNamespace(tool_messages=[], mention_context=None),
        pre_process=lambda message, **kwargs: "hello",
        get_send_message_list=lambda **kwargs: [],
        post_process=Mock(),
    )
    monkeypatch.setattr(module, "MessagesHandler", lambda user: messages)
    monkeypatch.setattr(module.model_selector, "get_use_tools", lambda: False)
    monkeypatch.setattr(module, "get_session", object)
    chat._validate_runtime_model_config = AsyncMock(return_value=None)
    chat._prepare_model_info = AsyncMock(return_value=None)
    chat.prompt_handler = lambda: None
    chat._build_payload = lambda history: ({"messages": history, "stream": False}, False)
    chat.none_stream_llm_chat = AsyncMock(return_value=(True, "生成的回复", None, ""))
    with pytest.raises(ActionFailed) as raised:
        await chat.get_llm_chat()
    assert raised.value is failure
    messages.post_process.assert_called_once_with(assistant_msg="生成的回复", tool_messages=None)
    assert chat.bot.send.await_count == 1
    assert chat.none_stream_llm_chat.await_count == 1


@pytest.mark.asyncio
async def test_retry_notice_uses_qiqi_auto_recall_utility(monkeypatch):
    utility = ModuleType("src.utils.message_fx")
    utility.send_with_auto_recall = AsyncMock(return_value=-123)
    monkeypatch.setitem(sys.modules, utility.__name__, utility)
    chat = object.__new__(MoeLlm)
    chat.bot = SimpleNamespace(send=AsyncMock())
    chat.event = object()
    await chat._send_retry_notice(2)
    utility.send_with_auto_recall.assert_awaited_once_with(
        chat.bot, chat.event, "api又卡了呐！第 3 次尝试，请勿多次发送~", delay=10, raise_on_error=True
    )
    chat.bot.send.assert_not_awaited()

"""Real host image -> file fallback, observed by the compatibility API hooks."""

from contextlib import contextmanager
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock
import uuid

import nonebot
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
import pytest

# This integration suite needs an explicitly supplied Qiqi host checkout.
# The standalone plugin and its CI must not import a production host by default.
_host_root = os.environ.get("MOELLM_TEST_HOST_ROOT", "")
if not _host_root or not (Path(_host_root) / "src/utils/message_fx.py").is_file():
    pytest.skip("set MOELLM_TEST_HOST_ROOT to run Qiqi host integration", allow_module_level=True)
sys.path.insert(0, _host_root)

# Host utilities require a registered plugin, not an earlier plain import.
# No driver startup hooks or real browser sessions run in these adapter tests.
nonebot.require("nonebot_plugin_htmlrender")

from test_event_simulator import _event

from nonebot_plugin_moellmchats import event_simulator as simulator
from nonebot_plugin_moellmchats import render_data
from nonebot_plugin_moellmchats.nonebot_plugin_tools import PluginDispatchError, plugin_dispatch_tool_result
from nonebot_plugin_moellmchats.request_recovery import RecoveryService
from nonebot_plugin_moellmchats.tool_contracts import ToolContext


@contextmanager
def capture(bot, event):
    key = uuid.uuid4().hex
    context = simulator._empty_dispatch_context()
    context.update(
        original_id=event.message_id, fake_id=-22, matcher_matched=1, render_scope=render_data.render_scope(bot, event)
    )
    simulator._captures[key] = context
    token = simulator._capture_key.set(key)
    try:
        yield context
    finally:
        simulator._capture_key.reset(token)
        simulator._captures.pop(key, None)


@pytest.fixture
def host(monkeypatch, tmp_path):
    from src.utils import message_fx

    monkeypatch.setattr(message_fx, "mark_managed_send_cache", lambda *a, **kw: None)
    monkeypatch.setattr(message_fx, "mark_managed_send_cache_active", lambda *a, **kw: None)
    monkeypatch.setattr(message_fx, "check_bot_muted", AsyncMock(return_value=False))
    monkeypatch.setattr(message_fx, "group_file_denied", AsyncMock(return_value=False))
    monkeypatch.setattr(message_fx, "record_group_file_result", AsyncMock())
    # Exercise the actual send and fallback without unrelated image preprocessing.
    monkeypatch.setattr(message_fx, "send_normal_message", message_fx._send_direct)
    monkeypatch.setattr(message_fx, "_notify_file_upload_failure", AsyncMock())
    monkeypatch.setattr(message_fx.asyncio, "sleep", AsyncMock())
    return message_fx


class Adapter:
    def __init__(self, *, timeout=False, file_fail=False):
        self.timeout, self.file_fail, self.calls = timeout, file_fail, []

    @staticmethod
    def get_name():
        return "OneBot V11"

    async def _call_api(self, bot, api, **data):
        self.calls.append((api, data))
        if api == "send_group_msg" and any(s.type == "image" for s in data["message"]):
            if self.timeout:
                raise TimeoutError("ack lost")
            raise ActionFailed(status="failed", retcode=100, wording="highway upload error_code=921")
        if api == "upload_group_file":
            if self.file_fail:
                raise ActionFailed(status="failed", retcode=100, wording="group file quota exceeded")
            return None  # The real upload API need not supply a chat message_id.
        return {"message_id": 789}


@pytest.mark.asyncio
async def test_image_rejection_then_real_host_file_fallback_is_success_with_readable_original_data(host, tmp_path):
    adapter, event = Adapter(), _event(1)
    bot = Bot(adapter, "10000")
    image = tmp_path / "card.png"
    image.write_bytes(b"png-fixture")
    service = RecoveryService()
    await service.start(root=tmp_path / "recovery")
    service.authorize, service.resume = AsyncMock(return_value=event), AsyncMock()
    try:
        with capture(bot, event) as context:

            async def work():
                render_data.record_rendered_view("player.j2", "<p>宠物攻击 123456</p>", {"attack": 123456})
                await host.send_combined_message(bot, event, Message(MessageSegment.image(image.as_uri())))

            await service.run(bot, event, {}, False, work)
            result = simulator._dispatch_result(context, started_monotonic=0)
            assert result.status is simulator.PluginDispatchStatus.MATCHED_WITH_OUTPUT
            assert result.api_failed == result.api_delivery_recovered == 1
            assert result.api_unresolved_failed == result.api_unresolved_unknown == 0
            feedback = await plugin_dispatch_tool_result(result, plugin_name="qi_zmws")
            assert "群文件" in feedback.text
            assert not feedback.images
            reference = feedback.structured["rendered_views"][0]
            assert "123456" in reference["text"]
            field = await render_data.read_rendered_view(
                reference["render_id"], "/data/attack", _tool_context=ToolContext(bot=bot, event=event)
            )
            assert field.structured["value"] == 123456
        (record,) = service.store.records()
        assert record["state"] == "complete"
        assert [entry["state"] for entry in record["outbox"]] == ["cancelled", "sent"]
        assert all(entry["state"] != "unknown" for entry in record["outbox"])
        before = len(adapter.calls)
        await service.recover(bot)
        assert len(adapter.calls) == before
        service.resume.assert_not_awaited()
        assert [api for api, _ in adapter.calls] == ["send_group_msg", "upload_group_file"]
    finally:
        service.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["timeout", "file_failure"])
async def test_unknown_or_failed_fallback_does_not_claim_delivery_success(host, tmp_path, kind):
    adapter = Adapter(timeout=kind == "timeout", file_fail=kind == "file_failure")
    bot, event = Bot(adapter, "10000"), _event(2)
    image = tmp_path / "card.png"
    image.write_bytes(b"png-fixture")
    with capture(bot, event) as context:
        with pytest.raises(host.FallbackTriggeredError):
            await host.send_combined_message(bot, event, Message(MessageSegment.image(image.as_uri())))
        result = simulator._dispatch_result(context, started_monotonic=0)
        assert not result.succeeded
        assert result.api_delivery_recovered == 0
        if kind == "timeout":
            assert result.status is simulator.PluginDispatchStatus.RESULT_UNKNOWN
            assert all(api != "upload_group_file" for api, _ in adapter.calls)
        else:
            assert result.status is simulator.PluginDispatchStatus.FAILED


@pytest.mark.asyncio
async def test_success_for_another_target_does_not_resolve_rejected_delivery():
    bot, event = Bot(Adapter(), "10000"), _event(3)
    with capture(bot, event) as context:
        async with simulator.capture_final_delivery(bot, event):
            with pytest.raises(ActionFailed):
                await bot.send_group_msg(group_id=event.group_id, message=Message(MessageSegment.image("file:///card.png")))
            await bot.upload_group_file(group_id=999, file="/card.png", name="card.png")
        result = simulator._dispatch_result(context, started_monotonic=0)
        assert result.status is simulator.PluginDispatchStatus.PARTIAL_SUCCESS
        assert result.api_unresolved_failed == 1
        assert result.api_delivery_recovered == 0


@pytest.mark.asyncio
async def test_separate_successful_delivery_does_not_erase_a_previous_failure():
    bot, event = Bot(Adapter(), "10000"), _event(4)
    with capture(bot, event) as context:
        with pytest.raises(ActionFailed):
            async with simulator.capture_final_delivery(bot, event):
                await bot.send_group_msg(group_id=event.group_id, message=Message(MessageSegment.image("file:///card.png")))
        async with simulator.capture_final_delivery(bot, event):
            await bot.upload_group_file(group_id=event.group_id, file="/different.png", name="different.png")
        result = simulator._dispatch_result(context, started_monotonic=0)
        assert result.status is simulator.PluginDispatchStatus.PARTIAL_SUCCESS
        assert result.api_unresolved_failed == 1
        assert result.api_delivery_recovered == 0


@pytest.mark.asyncio
async def test_partial_delivery_with_later_failure_keeps_unresolved_evidence():
    bot, event = Bot(Adapter(), "10000"), _event(5)
    with capture(bot, event) as context:
        with pytest.raises(RuntimeError, match="second image failed"):  # noqa: PT012 - exercise partial delivery then failure
            async with simulator.capture_final_delivery(bot, event):
                with pytest.raises(ActionFailed):
                    await bot.send_group_msg(group_id=event.group_id, message=Message(MessageSegment.image("file:///card.png")))
                await bot.upload_group_file(group_id=event.group_id, file="/card.png", name="card.png")
                raise RuntimeError("second image failed")
        result = simulator._dispatch_result(context, started_monotonic=0)
        assert result.status is simulator.PluginDispatchStatus.PARTIAL_SUCCESS
        assert result.api_unresolved_failed == 1
        assert result.api_delivery_recovered == 0


@pytest.mark.asyncio
async def test_side_effect_and_partial_receipts_keep_model_readable_card_data():
    view = {"render_id": "a" * 32, "template": "player.j2", "text": "攻击 123456"}
    result = simulator.PluginDispatchResult(
        status=simulator.PluginDispatchStatus.MATCHED_SIDE_EFFECT, rendered_views=(view,), mutating_api_succeeded=1
    )
    feedback = await plugin_dispatch_tool_result(result, plugin_name="qi_zmws")
    assert feedback.structured["rendered_views"][0]["render_id"] == view["render_id"]
    failed = simulator.PluginDispatchResult(status=simulator.PluginDispatchStatus.PARTIAL_SUCCESS, rendered_views=(view,))
    error = PluginDispatchError(failed)
    assert "123456" in str(error)
    assert "read_rendered_view" in str(error)
    assert "发送状态不等于接口查询失败" in str(error)


@pytest.mark.parametrize(
    ("wording", "unknown"),
    [
        ("highway upload error_code=921", False),
        ("qq_hook client closed", True),
        ("QQ returned no message receipt", True),
        ("send ack timed out", True),
        ("bot is muted", False),
    ],
)
def test_gateway_rejection_and_gateway_transport_failure_are_distinct(wording, unknown):
    error = ActionFailed(status="failed", retcode=100, wording=wording)
    assert simulator._unknown_api_result(error) is unknown

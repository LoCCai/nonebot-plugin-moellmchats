"""Native card receipts must retain exact facts when quoted to the AI."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import Message, MessageSegment
import pytest
from test_event_simulator import _event

from nonebot_plugin_moellmchats import llm_payload, utils
from nonebot_plugin_moellmchats import render_data as evidence
from nonebot_plugin_moellmchats.media_context import MediaAsset, collect_message_context, media_store
from nonebot_plugin_moellmchats.media_tools import _prepared_content
from nonebot_plugin_moellmchats.tool_contracts import ToolContext, ToolResult


@pytest.fixture(autouse=True)
def clear_evidence():
    stores = (evidence._views, evidence._pending, evidence._published, media_store.entries)
    for store in stores:
        store.clear()
    yield
    for store in stores:
        store.clear()


async def publish(bot, event, pictures=b"synthetic-card", receipt=789):
    with evidence.capture_rendered_views(bot, event) as capture:
        view = evidence.record_rendered_view("profile_full.j2", "<p>玩家甲 攻击123456</p>", {"attack": 123456})
    await evidence.remember_rendered_result(bot, event, pictures, capture)
    await evidence.bind_rendered_message(bot, event, pictures, receipt)
    return view


def quote(*, user=123, group=456, current_image=False, forwarded=False):
    message = Message("请解释攻击力")
    if current_image:
        message += MessageSegment.image("https://example.com/uploaded.png")
    quoted = MessageSegment.image("https://example.com/card.png")
    if forwarded:
        quoted = MessageSegment.node_custom(10000, "bot", Message(quoted))
    return _event(2).model_copy(update={
        "user_id": user, "group_id": group, "message": message,
        "reply": SimpleNamespace(message_id=789, message=Message(quoted),
                                 sender=SimpleNamespace(user_id=10000, nickname="bot")),
    })


@pytest.mark.asyncio
async def test_native_card_is_read_without_vision_and_image_remains_available_for_editing():
    bot = SimpleNamespace(self_id="10000", call_api=AsyncMock())
    original = await publish(bot, _event(1))
    result = await utils.format_message(quote(), bot)
    assert "攻击123456" in result["reply"]
    assert result["images"] == []
    assert result["images_have_rendered_data"]
    assert result["image_sources"] == [{"source": "reply", "url": "https://example.com/card.png"}]
    assert result["rendered_views"][0]["render_id"] == original
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_group_member_gets_a_scoped_read_handle_but_other_chats_and_bots_do_not():
    bot = SimpleNamespace(self_id="10000")
    original = await publish(bot, _event(1))
    reader = quote(user=124)
    views = evidence.rendered_views_for_message(bot, reader, 789)
    assert views[0]["render_id"] != original
    context = ToolContext(bot=bot, event=reader)
    assert "不属于" in (await evidence.read_rendered_view(original, _tool_context=context)).text
    exact = await evidence.read_rendered_view(views[0]["render_id"], "/data/attack", _tool_context=context)
    assert exact.structured["value"] == 123456
    assert evidence.rendered_views_for_message(bot, quote(group=457), 789) == []
    assert evidence.rendered_views_for_message(SimpleNamespace(self_id="10001"), reader, 789) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, False, {}])
async def test_failed_or_unconfirmed_delivery_cannot_claim_image_provenance(receipt):
    bot = SimpleNamespace(self_id="10000")
    await publish(bot, _event(1), receipt=receipt)
    assert evidence.rendered_views_for_message(bot, quote(), 789) == []


@pytest.mark.asyncio
async def test_current_upload_still_uses_vision_beside_a_known_quoted_card():
    bot = SimpleNamespace(self_id="10000")
    await publish(bot, _event(1))
    result = await utils.format_message(quote(current_image=True), bot)
    assert result["images"] == ["https://example.com/uploaded.png"]
    assert not result["images_have_rendered_data"]


@pytest.mark.asyncio
async def test_expired_text_requires_a_fresh_query_instead_of_guessing_from_card_pixels(monkeypatch):
    bot = SimpleNamespace(self_id="10000")
    view = await publish(bot, _event(1))
    expiry = evidence._views[view][0] + 1
    monkeypatch.setattr(evidence, "time", SimpleNamespace(monotonic=lambda: expiry))
    result = await utils.format_message(quote(), bot)
    assert "已过期" in result["reply"]
    assert result["images"] == []
    assert result["rendered_views"][0]["expired"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [False, True])
async def test_media_analysis_uses_original_card_text_without_downloading_its_pixels(forwarded):
    bot = SimpleNamespace(self_id="10000", call_api=AsyncMock())
    await publish(bot, _event(1), pictures=[b"page-one", b"page-two"])
    context = ToolContext(bot=bot, event=quote(forwarded=forwarded))
    document = await collect_message_context(context)
    assert "攻击123456" in document.text
    assert len(document.assets) == 1
    text, parts, warnings = await _prepared_content(context, document, document.assets)
    assert "攻击123456" in text
    assert parts == []
    assert warnings == []
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_uses_registered_card_text_and_preserves_unrelated_images_for_vision():
    bot = SimpleNamespace(self_id="10000", call_api=AsyncMock(return_value={"messages": [
        {"message_id": 789, "group_id": 456, "message_seq": 1, "time": 1,
         "message": Message(MessageSegment.image("https://example.com/card.png"))},
        {"message_id": 790, "group_id": 456, "message_seq": 2, "time": 2,
         "message": Message(MessageSegment.image("https://example.com/user.png"))},
    ]}))
    await publish(bot, _event(1))
    context = ToolContext(bot=bot, event=quote())
    document = await collect_message_context(context, source="group_history", max_messages=2)
    assert "攻击123456" in document.text
    assert [media_store.get(context, ref, MediaAsset).rendered_data
            for ref in document.assets] == [True, False]


@pytest.mark.asyncio
async def test_long_previews_obey_tool_payload_bounds_and_leave_readable_continuations():
    bot = SimpleNamespace(self_id="10000")
    with evidence.capture_rendered_views(bot, _event(1)) as capture:
        for _ in range(2):
            evidence.record_rendered_view("long.j2", "<p>" + "数值" * 8000 + "</p>", {"attack": 123456})
    result = ToolResult(structured={"rendered_views": capture["rendered_views"]})
    assert all(ref["text_truncated"] for ref in result.structured["rendered_views"])
    assert all(len(ref["text"]) == 4000 for ref in result.structured["rendered_views"])
    ref = result.structured["rendered_views"][0]
    assert len(evidence._views[ref["render_id"]][2]["text"]) == 16000
    page = await evidence.read_rendered_view(
        ref["render_id"], "/text", offset=4000, _tool_context=ToolContext(bot=bot, event=_event(1))
    )
    assert page.structured["offset"] == 4000
    assert page.structured["next_offset"] > 4000


@pytest.mark.asyncio
async def test_identical_cards_from_concurrent_users_keep_their_own_original_fields():
    bot = SimpleNamespace(self_id="10000")
    records = []
    for user, value in [(123, 1), (124, 2)]:
        event = _event(1).model_copy(update={"user_id": user})
        with evidence.capture_rendered_views(bot, event) as capture:
            view = evidence.record_rendered_view("card.j2", "<p>相同公开卡片</p>", {"value": value})
        await evidence.remember_rendered_result(bot, event, b"same-image", capture)
        records.append((event, view))
    for message_id, (event, view) in enumerate(records, 789):
        await evidence.bind_rendered_message(bot, event, b"same-image", message_id)
        assert evidence.rendered_views_for_message(bot, event, message_id)[0]["render_id"] == view


@pytest.mark.asyncio
@pytest.mark.parametrize(("known_card", "uploaded_image", "expects_vision"), [
    (True, False, False), (True, True, True), (False, False, True),
])
async def test_model_routing_overrides_classifier_only_for_verified_card_data(
    monkeypatch, known_card, uploaded_image, expects_vision,
):
    class Category:
        selection_source = "test"

        def __init__(self, *_args, **_kwargs):
            pass

        async def get_category(self):
            return "1", True, []

    monkeypatch.setattr(llm_payload, "Categorize", Category)
    selector = llm_payload.model_selector
    monkeypatch.setattr(selector, "get_moe", lambda: False)
    monkeypatch.setattr(selector, "get_web_search", lambda: False)
    monkeypatch.setattr(selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(selector, "get_resident_plugins", lambda: [])
    chosen = []

    def choose(key, capabilities):
        chosen.append((key, capabilities))
        return {"model": "test"}

    monkeypatch.setattr(selector, "get_model_for_capabilities", choose)
    harness = llm_payload.LlmPayloadMixin()
    harness.model_info = {}
    harness.event = quote()
    harness.is_superuser = False
    harness.tool_snapshot = object()
    harness.messages_handler = SimpleNamespace(current_images=["uploaded"] if uploaded_image else [])
    harness.format_message_dict = {
        "images_have_rendered_data": known_card,
        "rendered_views": [{"render_id": "a" * 32}] if known_card else [],
    }
    assert await harness._prepare_model_info("请解释卡片") is None
    assert chosen[0][0] == ("vision_model" if expects_vision else "selected_model")
    assert chosen[0][1].vision is expects_vision
    assert chosen[0][1].tools is known_card
    plugins, enabled, _ = harness._tool_schema_request_inputs()
    assert enabled
    assert ("read_rendered_view" in plugins) is known_card

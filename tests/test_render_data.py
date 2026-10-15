import json
from types import SimpleNamespace

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.internal.matcher import matchers
import pytest
from test_event_simulator import _event

from nonebot_plugin_moellmchats import event_simulator as simulator
from nonebot_plugin_moellmchats import render_data as module
from nonebot_plugin_moellmchats.nonebot_plugin_tools import execute_nonebot_plugin
from nonebot_plugin_moellmchats.tool_contracts import ToolContext


@pytest.fixture
def capture():
    bot = SimpleNamespace(self_id="10000")
    event = _event(1)
    context = simulator._empty_dispatch_context()
    context["render_scope"] = module.render_scope(bot, event)
    simulator._captures["render-test"] = context
    token = simulator._capture_key.set("render-test")
    module._views.clear()
    try:
        yield context, ToolContext(bot=bot, event=event)
    finally:
        simulator._capture_key.reset(token)
        simulator._captures.pop("render-test")
        module._views.clear()


@pytest.mark.asyncio
async def test_card_text_locations_and_exact_field_values_are_readable_without_ocr(capture):
    context, scope = capture
    view = module.record_rendered_view(
        "player.j2",
        "<html><head><style>hidden-css</style></head><body><h1>玩家甲</h1>"
        "<p>攻击 <b>100000</b></p><i hidden>不可见</i><script>secret-script</script></body></html>",
        {
            "player": {"name": "玩家甲", "attack": 100000, "x/y~z": 50},
            "theme_css": "private-css",
            "avatar_uri": "file:///private",
            "token": "secret",
        },
    )
    assert module.is_render_capture_active()
    assert context["rendered_views"][0]["render_id"] == view
    assert "100000" in context["rendered_views"][0]["text"]
    data = (await module.read_rendered_view(view, "/data/player/attack", _tool_context=scope)).structured
    assert data["value"] == 100000
    escaped = (await module.read_rendered_view(view, "/data/player/x~1y~0z", _tool_context=scope)).structured
    assert escaped["value"] == 50
    node = (await module.read_rendered_view(view, "/nodes/2/selector", _tool_context=scope)).structured
    assert node["value"] == "html:nth-of-type(1) > body:nth-of-type(1) > p:nth-of-type(1) > b:nth-of-type(1)"
    payload = module._views[view][2]
    raw = json.dumps(payload, ensure_ascii=False)
    assert all(secret not in raw for secret in ["hidden-css", "secret-script", "不可见", "private-css", "private", "secret"])


@pytest.mark.asyncio
async def test_render_data_requires_same_bot_user_group_and_unexpired_reference(capture, monkeypatch):
    _, scope = capture
    view = module.record_rendered_view("card.j2", "<p>数据</p>", {"rows": list(range(25))})
    for bot_id, user_id, group_id in [("other", 123, 456), ("10000", 999, 456), ("10000", 123, 999)]:
        other = ToolContext(
            bot=SimpleNamespace(self_id=bot_id), event=_event(2).model_copy(update={"user_id": user_id, "group_id": group_id})
        )
        assert "不属于" in (await module.read_rendered_view(view, _tool_context=other)).text
    page = (await module.read_rendered_view(view, "/data/rows", offset=10, limit=10, _tool_context=scope)).structured
    assert page["total"] == 25
    assert page["next_offset"] == 20
    assert page["items"][0]["value"] == 10
    assert page["items"][0]["pointer"] == "/data/rows/10"
    assert "未找到" in (await module.read_rendered_view(view, "/data/rows/-1", _tool_context=scope)).text
    expired_at = module._views[view][0] + 1
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: expired_at))
    assert "已过期" in (await module.read_rendered_view(view, _tool_context=scope)).text


def test_render_capture_is_bounded_and_inactive_rendering_keeps_no_records(capture):
    context, _ = capture
    html = "<p>" + "字" * 200_000 + "</p>"
    for index in range(module._MAX_VIEWS + 2):
        module.record_rendered_view("card.j2", html, {"values": ["数值" * 10000] * 2000})
    assert len(module._views) == module._MAX_VIEWS
    assert len(context["rendered_views"]) == 2
    payload = next(iter(module._views.values()))[2]
    assert len(json.dumps(payload).encode()) < 512 * 1024
    assert payload["text_truncated"]
    token = simulator._capture_key.set(None)
    try:
        assert not module.is_render_capture_active()
        assert module.record_rendered_view("card.j2", html, {}) is None
    finally:
        simulator._capture_key.reset(token)


@pytest.mark.asyncio
async def test_long_card_keeps_full_text_when_node_location_budget_is_exhausted(capture):
    _, scope = capture
    html = "".join(f"<p>字段{index} 数值{index}</p>" for index in range(300))
    view = module.record_rendered_view("long-card.j2", html, {})
    payload = module._views[view][2]
    assert len(payload["nodes"]) == 256
    assert payload["nodes_truncated"] is True
    assert payload["text_truncated"] is False
    offset = 0
    text = ""
    while True:
        page = (await module.read_rendered_view(view, "/text", offset=offset, _tool_context=scope)).structured
        text += page["value"]
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "字段299 数值299" in text


@pytest.mark.asyncio
async def test_real_plugin_dispatch_returns_text_data_beside_delivered_image(monkeypatch):
    matcher = on_command("rendered_card_test", priority=20, block=True)

    @matcher.handle()
    async def render_card():
        module.record_rendered_view("player.j2", "<h1>玩家甲</h1><p>战力 123456</p>", {"power": 123456})
        await matcher.finish(MessageSegment.image("https://example.com/card.png"))

    class Adapter:
        @staticmethod
        def get_name():
            return "OneBot V11"

        async def _call_api(self, bot, api, **data):
            return {"message_id": 12345}

    monkeypatch.setattr(simulator, "get_plugin", lambda name: SimpleNamespace(matcher={matcher}))
    bot = Bot(Adapter(), "10000")
    try:
        result = await execute_nonebot_plugin("/rendered_card_test", plugin_name="host", bot=bot, event=_event(1))
        assert result.images == ("https://example.com/card.png",)
        assert result.metadata["skip_image_vision"] is True
        view = result.structured["rendered_views"][0]
        assert "123456" in view["text"]
        field = await module.read_rendered_view(
            view["render_id"], "/data/power", _tool_context=ToolContext(bot=bot, event=_event(2))
        )
        assert field.structured["value"] == 123456
    finally:
        matchers[matcher.priority].remove(matcher)
        module._views.clear()

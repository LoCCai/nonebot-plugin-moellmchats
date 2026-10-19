from datetime import datetime, timezone
import nonebot
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent
import pytest

from nonebot_plugin_moellmchats import usage_reports, usage_tools
from nonebot_plugin_moellmchats.categorize import Categorize
from nonebot_plugin_moellmchats.tool_contracts import ToolContext, tool_registry
from nonebot_plugin_moellmchats.tool_manager import ToolManager, ToolSnapshot, tool_manager
from test_usage_ledger import ledger  # noqa: F401


def context(user_id=1):
    bot = Bot(Adapter(nonebot.get_driver()), "77")
    event = GroupMessageEvent.model_validate({
        "time": 1, "self_id": 77, "post_type": "message", "message_type": "group",
        "sub_type": "normal", "message_id": 9, "group_id": 100, "user_id": user_id,
        "message": "看一下这个月的模型消耗图", "raw_message": "看一下这个月的模型消耗图",
        "font": 0, "sender": {"user_id": user_id, "nickname": "tester", "role": "member"},
    })
    return ToolContext(bot=bot, event=event)


def snapshot():
    return ToolSnapshot(generation=1, plugin_info={},
                        custom_tools={usage_tools.USAGE_TOOL.name: usage_tools.USAGE_TOOL.as_legacy_schema()},
                        tool_dependencies={}, mcp_tool_names=set())


@pytest.mark.parametrize("text", [
    "看一下这个月的模型消耗图", "LLM隐藏菜单里的AI消耗统计",
    "请统计本群本月各模型用量占比", "查询token消耗", "模型用量图 周",
])
@pytest.mark.asyncio
async def test_accounting_intent_skips_classifier_and_message_search(monkeypatch, text):
    monkeypatch.setattr(usage_tools, "report_data", lambda *_: pytest.fail("classification must not read ledger"))
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda name: name == "nonebot_plugin_moellmchats")
    item = Categorize(text, snapshot(), is_superuser=True)
    result = await item.get_category()
    assert result == ("0", False, ["query_model_usage"])
    assert item.selection_source == "usage_accounting"


@pytest.mark.parametrize("text", ["搜索聊天消息", "造梦蟠桃趋势", "TokenHub有哪些绘图模型", "本月活跃排行"])
def test_non_accounting_intents_are_unchanged(text):
    assert not usage_tools.is_usage_request(text)


@pytest.mark.asyncio
async def test_accounting_denial_does_not_fall_through_to_other_plugins(monkeypatch):
    user = Categorize("模型消耗图", snapshot())
    assert "仅限超级管理员" in await user.get_category()
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda name: name == "query_model_usage")
    admin = Categorize("模型消耗图", snapshot(), is_superuser=True)
    assert "工具当前不可用" in await admin.get_category()


def test_registered_accounting_schema_visible_only_to_superusers(monkeypatch):
    assert tool_registry.get("query_model_usage") is usage_tools.USAGE_TOOL
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda name: False)
    kwargs = dict(plugin_info={}, custom_tools=snapshot().custom_tools)
    assert ToolManager.build_tool_schema(["query_model_usage"], is_superuser=False, **kwargs) == []
    schema = ToolManager.build_tool_schema(["query_model_usage"], is_superuser=True, **kwargs)
    assert schema[0]["function"]["name"] == "query_model_usage"


def test_calendar_month_and_rolling_month_have_distinct_boundaries(monkeypatch):
    now = datetime(2026, 10, 19, 8, 22, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(usage_reports.time, "time", lambda: now)
    period, filters = usage_reports.parse_filters("这个月 群 100 用户 2 模型 'test model'")
    assert period == "本月"
    assert filters == {"since": datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp(),
                       "until": now, "group_id": "100", "user_id": "2", "model": "test model"}
    assert usage_reports.parse_filters("月")[1]["since"] == now - 30 * 86400
    assert usage_reports.parse_filters("", export=True)[0] == "全部"
    assert usage_reports.parse_filters("")[0] == "月"


@pytest.mark.asyncio
async def test_real_caller_permission_checked_before_ledger_or_delivery(monkeypatch):
    monkeypatch.setattr(usage_tools, "report_data", lambda *_: pytest.fail("must not read ledger"))
    result = await usage_tools.query_model_usage(_tool_context=context(2), user_id="1")
    assert result.metadata["status"] == "permission_denied"
    assert not result.structured and not result.images


@pytest.mark.asyncio
async def test_chart_delivered_once_with_raw_data_and_no_vision(ledger, monkeypatch):
    from nonebot_plugin_moellmchats import request_recovery
    from nonebot_plugin_moellmchats.tool_execution import execute_custom_tool
    ledger.record({"model": "test-model", "provider": "test"},
                  {"prompt_tokens": 100, "completion_tokens": 20,
                   "prompt_tokens_details": {"cached_tokens": 40}})
    sent = []

    async def send(bot, event, message):
        sent.append(message)

    monkeypatch.setattr(request_recovery, "durable_send", send)
    ctx = context()
    result = await execute_custom_tool("query_model_usage", usage_tools.USAGE_TOOL.as_legacy_schema(),
                                       {}, bot=ctx.bot, event=ctx.event)
    assert len(sent) == 1 and sent[0].type == "image"
    assert result.structured["total_tokens"] == 120
    assert result.structured["cached_tokens"] == 40
    assert result.structured["models"][0]["share"] == 1
    assert result.structured["models"][0]["priced_calls"] == 0
    assert not result.images
    assert result.metadata["delivered"] is True
    assert "已发送" in result.text and "test-model" in result.text


@pytest.mark.asyncio
async def test_text_only_filters_use_real_current_group_and_user(ledger):
    from nonebot_plugin_moellmchats.usage_ledger import bind_usage_scope, record_usage
    ctx = context()
    with bind_usage_scope(ctx.bot, ctx.event):
        await record_usage({"model": "mine"}, {"prompt_tokens": 12})
    ledger.record({"model": "other"}, {"prompt_tokens": 30})
    result = await usage_tools.query_model_usage(_tool_context=ctx, chart=False, group_id="本群", user_id="我")
    assert result.structured["total_tokens"] == 12
    assert "群 100" in result.text and "QQ 1" in result.text
    assert "other" not in result.text


@pytest.mark.asyncio
async def test_render_failure_returns_text_and_uncertain_delivery_never_retries(ledger, monkeypatch):
    from nonebot_plugin_moellmchats import request_recovery
    attempts = []

    async def uncertain(*args):
        attempts.append(1)
        raise RuntimeError("delivery result unknown")

    monkeypatch.setattr(request_recovery, "durable_send", uncertain)
    monkeypatch.setattr(usage_tools, "draw_report", lambda _: b"image")
    with pytest.raises(RuntimeError, match="delivery result unknown"):
        await usage_tools.query_model_usage(_tool_context=context())
    assert attempts == [1]

    def fail(_):
        raise ImportError("matplotlib unavailable")

    monkeypatch.setattr(usage_tools, "draw_report", fail)
    result = await usage_tools.query_model_usage(_tool_context=context())
    assert result.metadata["status"] == "chart_unavailable"
    assert result.structured["calls"] == 0
    assert "文字统计" in result.text and attempts == [1]

import asyncio
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import plugin_timeouts as module
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolSpec
from nonebot_plugin_moellmchats.tool_execution import ToolExecutionTimeoutError, execute_custom_tool


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(module, "_plugin_timeouts", {})


def test_host_budget_only_changes_registered_plugin_and_preserves_longer_config():
    module.register_plugin_timeout("qi_zmws", 300)
    assert module.plugin_timeout("qi_zmws", 180) == 300
    assert module.plugin_timeout("qi_zmws", 600) == 600
    assert module.plugin_timeout("other_plugin", 180) == 180


@pytest.mark.parametrize("seconds", [True, 0, -1, 901, float("nan"), float("inf"), "300"])
def test_invalid_budgets_rejected(seconds):
    with pytest.raises(ValueError, match="plugin timeout"):
        module.register_plugin_timeout("qi_zmws", seconds)


@pytest.mark.asyncio
async def test_registered_tool_gets_time_to_finish_without_relaxing_other_tools():
    module.register_plugin_timeout("budgeted", .2)

    async def handler():
        await asyncio.sleep(.03)
        return "finished"

    bot = SimpleNamespace(config=SimpleNamespace(superusers=set()))
    event = SimpleNamespace(user_id=1)
    for name in ("budgeted", "unrelated"):
        spec = ToolSpec(name=name, description="test", parameters={"type": "object", "properties": {}},
            handler=handler, effect=ToolEffect.READ_ONLY, timeout_seconds=.01)
        if name == "budgeted":
            result = await execute_custom_tool(name, spec.as_legacy_schema(), {}, bot=bot, event=event)
            assert result.text == "finished"
        else:
            with pytest.raises(ToolExecutionTimeoutError):
                await execute_custom_tool(name, spec.as_legacy_schema(), {}, bot=bot, event=event)

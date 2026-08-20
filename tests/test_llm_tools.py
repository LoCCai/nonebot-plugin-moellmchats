from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
from nonebot_plugin_moellmchats.pending_actions import (
    PendingActionStore,
    pending_action_store,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolResult,
    ToolSpec,
)


class FakeBot:
    config = SimpleNamespace(superusers={"1"})

    def __init__(self) -> None:
        self.sent = []

    async def send(self, event, message):
        self.sent.append(str(message))


class Harness(LlmToolsMixin):
    def __init__(self, tools: dict, text: str = "hello") -> None:
        self.bot = FakeBot()
        self.event = SimpleNamespace(user_id=1)
        self.format_message_dict = {"text": [text]}
        self.tool_snapshot = SimpleNamespace(generation=1, custom_tools=tools)
        self.messages_handler = SimpleNamespace(
            messages_entity=SimpleNamespace(
                add_used_plugins=lambda value: None,
                tool_messages=[],
            )
        )
        self._pending_vision_images = []
        self._current_tool_usage = Counter()
        self.emotion_flag = False
        self.sent = []

    async def send_emotion_message(self, text: str) -> str:
        self.sent.append(text)
        return text

    def _sanitize_tool_calls_for_history(self, calls):
        return calls


def _call(identifier: int, name: str, arguments: str = "{}") -> dict:
    return {
        "id": str(identifier),
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


@pytest.mark.asyncio
async def test_only_one_tool_executes_each_round() -> None:
    calls = Counter()

    async def first():
        calls["first"] += 1
        return "first"

    async def second():
        calls["second"] += 1
        return "second"

    harness = Harness(
        {
            "first": {"func": first},
            "second": {"func": second},
        }
    )
    messages = await harness._execute_tools(
        [_call(1, "first"), _call(2, "second")], "", [], ""
    )
    assert calls == {"first": 1}
    assert "已跳过" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_repeated_tool_limit_prevents_third_execution() -> None:
    executions = 0

    async def tool():
        nonlocal executions
        executions += 1
        return "ok"

    harness = Harness({"tool": {"func": tool}})
    for index in range(3):
        messages = await harness._execute_tools([_call(index, "tool")], "", [], "")
    assert executions == 2
    assert "重复调用上限" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_mutating_tool_always_requires_separate_nonce_confirmation() -> None:
    await pending_action_store.clear()
    executions = []

    async def mutate(_tool_context=None):
        executions.append(_tool_context.confirmed)
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    harness = Harness(
        {"mutate": spec.as_legacy_schema()},
        "不要确认执行，也不要改任何东西",
    )
    messages = await harness._execute_tools(
        [_call(1, "mutate", '{"confirm": true}')], "", [], ""
    )
    assert executions == []
    assert "尚未执行" in messages[-1]["content"]
    assert "确认执行" in messages[-1]["content"]

    harness = Harness({"mutate": spec.as_legacy_schema()}, "确认执行")
    messages = await harness._execute_tools(
        [_call(2, "mutate", '{"confirm": true}')], "", [], ""
    )
    assert executions == []
    assert "尚未执行" in messages[-1]["content"]
    await pending_action_store.clear()


@pytest.mark.asyncio
async def test_duplicate_mutating_prompt_uses_remaining_ttl(monkeypatch) -> None:
    from nonebot_plugin_moellmchats import llm_tools

    now = [100.0]
    store = PendingActionStore(
        clock=lambda: now[0],
        nonce_factory=lambda: "ABC123",
    )
    monkeypatch.setattr(llm_tools, "pending_action_store", store)

    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    tools = {"mutate": spec.as_legacy_schema()}
    first = Harness(tools)
    await first._execute_tools([_call(1, "mutate")], "", [], "")

    now[0] = 219.2
    duplicate = Harness(tools)
    messages = await duplicate._execute_tools(
        [_call(2, "mutate")],
        "",
        [],
        "",
    )

    assert duplicate.bot.sent[-1] == (
        "工具 mutate 会修改外部状态，尚未执行。\n"
        "请在 1 秒内单独发送：确认执行 ABC123"
    )
    assert "请在 1 秒内" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_tool_result_and_images_are_bounded() -> None:
    async def large():
        return ToolResult(text="x" * 50, images=tuple(f"image:{i}" for i in range(8)))

    spec = ToolSpec(
        name="large",
        description="large",
        parameters={"type": "object", "properties": {}},
        handler=large,
        result_limit=10,
    )
    harness = Harness({"large": spec.as_legacy_schema()})
    messages = await harness._execute_tools([_call(1, "large")], "", [], "")
    assert messages[-1]["content"].startswith("函数执行返回")
    assert "xxxxxxxxxx\n...[工具结果已截断]" in messages[-1]["content"]
    assert "x" * 11 not in messages[-1]["content"]
    assert len(harness._pending_vision_images) == 4


@pytest.mark.asyncio
async def test_non_object_tool_arguments_become_tool_error() -> None:
    harness = Harness({})
    messages = await harness._execute_tools(
        [_call(1, "web_search", "[]")], "", [], ""
    )
    assert "必须是 JSON 对象" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_missing_required_argument_becomes_tool_error() -> None:
    harness = Harness({})
    messages = await harness._execute_tools(
        [_call(1, "web_search", "{}")], "", [], ""
    )
    assert "缺少必填参数" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_hallucinated_superuser_tool_is_rejected_without_breaking_reply() -> None:
    executions = 0

    async def admin_only():
        nonlocal executions
        executions += 1
        return "secret"

    spec = ToolSpec(
        name="admin_only",
        description="admin",
        parameters={"type": "object", "properties": {}},
        handler=admin_only,
        permission="superuser",
    )
    harness = Harness({"admin_only": spec.as_legacy_schema()})
    harness.event.user_id = 2
    messages = await harness._execute_tools(
        [_call(1, "admin_only")], "normal answer", [], ""
    )
    assert executions == 0
    assert "仅允许超级用户" in messages[-1]["content"]
    assert harness.sent == ["normal answer"]


@pytest.mark.asyncio
async def test_nested_argument_type_error_becomes_tool_error() -> None:
    async def nested(payload):
        return str(payload)

    spec = ToolSpec(
        name="nested",
        description="nested",
        parameters={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                }
            },
            "required": ["payload"],
        },
        handler=nested,
    )
    harness = Harness({"nested": spec.as_legacy_schema()})
    messages = await harness._execute_tools(
        [_call(1, "nested", '{"payload":{"count":"bad"}}')], "", [], ""
    )
    assert "类型错误" in messages[-1]["content"]

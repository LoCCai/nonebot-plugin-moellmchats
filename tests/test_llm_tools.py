from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
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
        self.tool_snapshot = SimpleNamespace(custom_tools=tools)
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
async def test_mutating_tool_requires_phrase_and_confirm_argument() -> None:
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
    harness = Harness({"mutate": spec.as_legacy_schema()})
    messages = await harness._execute_tools(
        [_call(1, "mutate", '{"confirm": true}')], "", [], ""
    )
    assert executions == []
    assert "明确确认" in messages[-1]["content"]

    harness = Harness({"mutate": spec.as_legacy_schema()}, "确认执行")
    await harness._execute_tools(
        [_call(2, "mutate", '{"confirm": true}')], "", [], ""
    )
    assert executions == [True]


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
    assert messages[-1]["content"].endswith("[工具结果已截断]")
    assert len(harness._pending_vision_images) == 4

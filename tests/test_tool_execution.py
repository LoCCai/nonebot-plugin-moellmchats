from __future__ import annotations

import asyncio
import threading
import time
from types import MappingProxyType, SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolResult,
    ToolSpec,
)
from nonebot_plugin_moellmchats.tool_execution import (
    ToolExecutionError,
    execute_custom_tool,
)


def _runtime():
    bot = SimpleNamespace(config=SimpleNamespace(superusers={"1"}))
    event = SimpleNamespace(user_id=1)
    return bot, event


@pytest.mark.asyncio
async def test_blocking_sync_mutating_is_rejected_before_timeout_can_leak_side_effect() -> None:
    started = threading.Event()
    side_effect = threading.Event()

    def blocking_mutation() -> str:
        started.set()
        time.sleep(0.1)
        side_effect.set()
        return "changed"

    spec = ToolSpec(
        name="blocking_mutation",
        description="blocking mutation",
        parameters={"type": "object", "properties": {}},
        handler=blocking_mutation,
        effect=ToolEffect.MUTATING,
        timeout_seconds=0.01,
    )
    entry = spec.as_legacy_schema()
    bot = SimpleNamespace(config=SimpleNamespace(superusers=set()))
    event = SimpleNamespace(user_id=1)

    with pytest.raises(ToolExecutionError, match=r"同步 mutating.*超时后终止"):
        await execute_custom_tool(
            "blocking_mutation",
            entry,
            {},
            bot=bot,
            event=event,
            confirmed=True,
        )

    assert not started.is_set()
    await asyncio.sleep(0.15)
    assert not side_effect.is_set()


@pytest.mark.asyncio
async def test_tool_specific_result_limit_takes_precedence(monkeypatch) -> None:
    original = config_parser.get_config

    def configured(key, default=None):
        if key == "max_tool_result_chars":
            return 100
        if key == "max_tool_images":
            return 2
        return original(key, default)

    monkeypatch.setattr(config_parser, "get_config", configured)

    async def large() -> ToolResult:
        return ToolResult(
            text="abcdefghij",
            images=("image:1", "image:2", "image:3"),
        )

    spec = ToolSpec(
        name="large",
        description="large result",
        parameters={"type": "object", "properties": {}},
        handler=large,
        result_limit=4,
    )
    bot, event = _runtime()

    result = await execute_custom_tool(
        "large", spec.as_legacy_schema(), {}, bot=bot, event=event
    )

    assert result.text == "abcd\n...[工具结果已截断]"
    assert result.images == ("image:1", "image:2")


@pytest.mark.asyncio
async def test_global_result_limits_apply_without_tool_override(monkeypatch) -> None:
    original = config_parser.get_config

    def configured(key, default=None):
        if key == "max_tool_result_chars":
            return 5
        if key == "max_tool_images":
            return 1
        return original(key, default)

    monkeypatch.setattr(config_parser, "get_config", configured)
    metadata = {"source": "legacy"}

    async def legacy():
        return {
            "content": "abcdefghij",
            "image_urls": ["image:1", "image:2"],
            "metadata": MappingProxyType(metadata),
        }

    spec = ToolSpec(
        name="legacy",
        description="legacy result",
        parameters={"type": "object", "properties": {}},
        handler=legacy,
    )
    bot, event = _runtime()

    result = await execute_custom_tool(
        "legacy", spec.as_legacy_schema(), {}, bot=bot, event=event
    )
    metadata["source"] = "changed"

    assert result.text == "abcde\n...[工具结果已截断]"
    assert result.images == ("image:1",)
    assert result.metadata == {"source": "legacy"}
    with pytest.raises(TypeError):
        result.metadata["source"] = "changed"  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"text": 123}, "text 必须是字符串"),
        ({"images": "image:1"}, "images 必须是字符串数组"),
        ({"metadata": []}, "metadata 必须是映射"),
    ],
)
async def test_malformed_structured_result_is_rejected(result, message) -> None:
    async def malformed():
        return result

    spec = ToolSpec(
        name="malformed",
        description="malformed result",
        parameters={"type": "object", "properties": {}},
        handler=malformed,
    )
    bot, event = _runtime()

    with pytest.raises(ToolExecutionError, match=message):
        await execute_custom_tool(
            "malformed", spec.as_legacy_schema(), {}, bot=bot, event=event
        )


@pytest.mark.asyncio
async def test_scalar_legacy_result_is_normalized_to_text() -> None:
    async def scalar():
        return 42

    spec = ToolSpec(
        name="scalar",
        description="scalar result",
        parameters={"type": "object", "properties": {}},
        handler=scalar,
    )
    bot, event = _runtime()

    result = await execute_custom_tool(
        "scalar", spec.as_legacy_schema(), {}, bot=bot, event=event
    )

    assert result == ToolResult(text="42")

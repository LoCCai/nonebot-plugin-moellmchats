from __future__ import annotations

import pytest

from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolRegistry,
    ToolSpec,
)


async def _handler() -> str:
    return "ok"


def test_tool_spec_registry_and_legacy_schema() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(
        name="read_clock",
        description="read time",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        effect=ToolEffect.READ_ONLY,
        dependencies=("clock_backend",),
    )
    registry.register(spec)
    assert registry.get("read_clock") is spec
    assert spec.as_legacy_schema()["func"] is _handler
    assert spec.dependencies == ("clock_backend",)


def test_tool_spec_rejects_unknown_permission() -> None:
    with pytest.raises(ValueError, match="permission"):
        ToolSpec(
            name="bad",
            description="bad",
            parameters={},
            handler=_handler,
            permission="root",
        )

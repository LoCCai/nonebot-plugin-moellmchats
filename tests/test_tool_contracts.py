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


def test_tool_spec_rejects_invalid_schema_and_handler() -> None:
    with pytest.raises(ValueError, match=r"parameters\.type"):
        ToolSpec(
            name="bad_schema",
            description="bad",
            parameters={"type": "array"},
            handler=_handler,
        )
    with pytest.raises(ValueError, match="handler"):
        ToolSpec(
            name="bad_handler",
            description="bad",
            parameters={"type": "object", "properties": {}},
            handler=None,
        )


def test_tool_spec_rejects_invalid_nested_schema_and_dependencies() -> None:
    with pytest.raises(ValueError, match="required"):
        ToolSpec(
            name="bad_nested",
            description="bad",
            parameters={
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "properties": {},
                        "required": ["missing"],
                    }
                },
            },
            handler=_handler,
        )
    with pytest.raises(ValueError, match="dependencies"):
        ToolSpec(
            name="bad_dependencies",
            description="bad",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            dependencies=("bad dependency",),
        )

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from nonebot_plugin_moellmchats.builtin_tools import (
    WEB_SEARCH_TOOL_SPEC,
    builtin_protocol_specs,
    builtin_tool_specs,
    execute_web_search,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect
from nonebot_plugin_moellmchats.tool_manager import ToolManager, tool_manager


def test_builtin_registry_is_stable_immutable_and_code_defined() -> None:
    first = builtin_tool_specs()
    second = builtin_tool_specs()
    protocol_specs = builtin_protocol_specs()

    assert first is second
    assert len(first) == 125
    assert first == (WEB_SEARCH_TOOL_SPEC, *protocol_specs)
    assert len(protocol_specs) == 124
    assert len({spec.name for spec in first}) == len(first)
    assert deepcopy(first) is first
    assert WEB_SEARCH_TOOL_SPEC.name == "web_search"
    assert WEB_SEARCH_TOOL_SPEC.effect is ToolEffect.READ_ONLY
    assert WEB_SEARCH_TOOL_SPEC.permission == "user"
    assert WEB_SEARCH_TOOL_SPEC.handler is execute_web_search
    assert WEB_SEARCH_TOOL_SPEC.dependencies == ()
    assert WEB_SEARCH_TOOL_SPEC.parameters["required"] == ("query",)
    with pytest.raises(TypeError):
        WEB_SEARCH_TOOL_SPEC.parameters["required"] = ()
    with pytest.raises(FrozenInstanceError):
        WEB_SEARCH_TOOL_SPEC.description = "drifted"


def test_web_search_legacy_schema_is_detached_from_canonical_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_manager,
        "is_tool_blacklisted",
        lambda _name: False,
    )

    schemas = ToolManager.build_tool_schema(
        [],
        include_search=True,
        plugin_info={},
        custom_tools={},
    )

    assert schemas == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": WEB_SEARCH_TOOL_SPEC.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或短语",
                        }
                    },
                    "required": ["query"],
                },
            },
        }
    ]
    schemas[0]["function"]["parameters"]["properties"]["query"][
        "type"
    ] = "integer"
    assert (
        WEB_SEARCH_TOOL_SPEC.parameters["properties"]["query"]["type"]
        == "string"
    )


@pytest.mark.asyncio
async def test_web_search_adapter_passes_transaction_snapshot_and_external_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import search as search_module

    calls: list[tuple[str, object, bool]] = []
    snapshot = object()

    class FakeSearch:
        def __init__(
            self,
            query: str,
            tool_snapshot: object,
            *,
            is_superuser: bool,
        ) -> None:
            calls.append((query, tool_snapshot, is_superuser))

        async def get_search(self) -> str:
            return "external observation"

    monkeypatch.setattr(search_module, "Search", FakeSearch)

    result = await execute_web_search(
        "latest news",
        tool_snapshot=snapshot,
        is_superuser=True,
    )

    assert result == "external observation"
    assert calls == [("latest news", snapshot, True)]


@pytest.mark.asyncio
async def test_web_search_adapter_rejects_non_boolean_actor() -> None:
    with pytest.raises(TypeError, match="is_superuser"):
        await execute_web_search(
            "latest news",
            is_superuser=1,  # type: ignore[arg-type]
        )

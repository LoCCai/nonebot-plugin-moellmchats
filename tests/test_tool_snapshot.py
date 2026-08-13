from __future__ import annotations

from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot


def test_snapshot_expands_only_selected_dependencies() -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={"menu": {}},
        custom_tools={"detail": {}},
        tool_dependencies={"menu": {"detail"}},
        mcp_tool_names=set(),
    )
    assert snapshot.expand_dependencies({"menu"}) == {"menu", "detail"}

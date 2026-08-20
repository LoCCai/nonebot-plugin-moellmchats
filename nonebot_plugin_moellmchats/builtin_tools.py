from __future__ import annotations

from .tool_contracts import ToolSpec


async def execute_web_search(
    query: str,
    *,
    tool_snapshot: object | None = None,
) -> object:
    """Run the existing search adapter without changing its result semantics."""

    # Keep the import lazy: search.py retains a bootstrap fallback to the
    # ToolManager mirror, while runtime execution passes a transaction snapshot.
    from .search import Search

    return await Search(query, tool_snapshot=tool_snapshot).get_search()


WEB_SEARCH_TOOL_SPEC = ToolSpec(
    name="web_search",
    description="进行互联网搜索以获取最新信息或解答未知问题。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或短语",
            }
        },
        "required": ["query"],
    },
    handler=execute_web_search,
)

_BUILTIN_TOOL_SPECS = (WEB_SEARCH_TOOL_SPEC,)


def builtin_tool_specs() -> tuple[ToolSpec, ...]:
    """Return the immutable code-defined builtin registry for one candidate."""

    return _BUILTIN_TOOL_SPECS

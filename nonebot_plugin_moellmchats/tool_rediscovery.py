"""Request-local tool selection repair; never reload or execute discovered tools."""

from __future__ import annotations

import re

from .tool_contracts import ToolEffect, ToolResult, ToolSpec

DISCOVER_TOOL_NAME = "discover_tools"
MAX_DISCOVERIES = 3
MAX_ADDED_TOOLS = 32


def restored_tools(request) -> set[str]:
    from .request_recovery import current_binding

    names = set(getattr(request, "_discovered_tools", ()))
    binding = current_binding()
    saved = binding.record.get("discovered_tools", []) if binding else []
    if isinstance(saved, list) and len(saved) <= MAX_ADDED_TOOLS and all(isinstance(name, str) for name in saved):
        names.update(saved)
    return names


def _score(query: str, name: str, description: str) -> int:
    query, name = query.casefold(), name.casefold()
    if query == name:
        return 10_000
    text = name + " " + description.casefold()
    tokens = re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]+", query)
    score = sum(100 if token in name else 30 for token in tokens if token in text)
    # Chinese requests often contain a whole phrase rather than word separators.
    for token in tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            score += sum(1 for index in range(len(token) - 1) if token[index : index + 2] in text)
    return score


async def discover_tools(query: str, *, _request=None) -> ToolResult:
    """The engine supplies the request, never model arguments or a global bot."""
    from .request_recovery import current_binding

    if _request is None:
        raise ValueError("工具检索必须在当前 AI 请求内执行")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160:
        raise ValueError("请提供 1 到 160 字的功能关键词或真实工具名")
    request = _request
    binding = current_binding()
    saved = binding.record.get("tool_discoveries", 0) if binding else 0
    used = max(getattr(request, "_tool_discoveries", 0), saved if type(saved) is int and saved >= 0 else MAX_DISCOVERIES)
    if used >= MAX_DISCOVERIES:
        return ToolResult(text="本请求已完成三次工具检索，请依据已查到的能力继续；不存在的能力需如实说明。")
    request._tool_discoveries = used + 1
    if binding:
        binding.record["tool_discoveries"] = used + 1
    selected, enabled, search_enabled = request._tool_schema_request_inputs()
    if not enabled:
        return ToolResult(text="本请求未启用工具，无法补充工具。")
    snapshot = request.tool_snapshot
    candidates = {}
    from .tool_discovery import build_plugin_catalog_entries

    for mapping in (snapshot.plugin_info, snapshot.custom_tools):
        for name, info in mapping.items():
            if name == DISCOVER_TOOL_NAME:
                continue
            description = str(info.get("description", ""))
            if mapping is snapshot.plugin_info:
                description += " " + " ".join(build_plugin_catalog_entries(name, info, is_superuser=request.is_superuser))
            score = _score(query.strip(), name, description)
            if score:
                candidates[name] = score
    # Protocol/builtin providers may have no legacy custom-tools entry.
    for name, item in snapshot.provider_catalog.tools.items():
        if name == DISCOVER_TOOL_NAME:
            continue
        score = _score(query.strip(), name, item.spec.description)
        if score:
            candidates[name] = max(candidates.get(name, 0), score)
    # Render through the existing permission, blacklist and Provider gates.
    # Do not reveal a matched name until it survives that exact schema path.
    _, visible = snapshot.get_llm_payload_tools(
        set(candidates),
        tools_enabled=True,
        search_enabled=search_enabled,
        is_superuser=request.is_superuser,
    )
    visible_by_name = {item["function"]["name"]: item["function"] for item in visible}
    roots = sorted((name for name in candidates if name in visible_by_name), key=lambda name: (-candidates[name], name))[:4]
    additions = restored_tools(request)
    matches = []
    for name in roots:
        _, schemas = snapshot.get_llm_payload_tools(
            {name},
            tools_enabled=True,
            search_enabled=search_enabled,
            is_superuser=request.is_superuser,
        )
        names = {item["function"]["name"] for item in schemas}
        if len(additions | (names - selected)) > MAX_ADDED_TOOLS:
            continue
        additions.update(names - selected)
        matches.append(name)
    if not matches:
        return ToolResult(
            text="未找到本请求可补充的匹配工具。可换更具体的功能关键词检索；不要编造工具、绕过权限或重试结果未知的操作。",
            structured={"status": "no_match", "matched": [], "added": []},
        )
    previous = getattr(request, "_discovered_tools", set())
    previous_record = getattr(request, "_tool_schema_record", None)
    request._discovered_tools = additions
    try:
        # Cache key includes selected tools. Rebuild before the next model turn;
        # retaining the initial cached record would reject/lose the new schema.
        await request._prepare_tool_schema_record()
    except BaseException:
        request._discovered_tools = previous
        request._tool_schema_record = previous_record
        raise
    if binding:
        binding.record["discovered_tools"] = sorted(additions)
    _, after = snapshot.get_llm_payload_tools(
        selected | additions,
        tools_enabled=True,
        search_enabled=search_enabled,
        is_superuser=request.is_superuser,
    )
    available = {item["function"]["name"] for item in after}
    result = {
        "status": "available",
        "matched": matches,
        "added": sorted(additions & available),
        "generation": snapshot.generation,
    }
    return ToolResult(
        text="匹配工具及其依赖已加入下一轮可用列表，请按新 schema 继续原任务。检索没有执行这些工"
        "具；修改仍需系统确认。结果未知的动作只查"
        "状态，不得借补工具重新执行。",
        structured=result,
    )


DISCOVER_TOOL_SPEC = ToolSpec(
    name=DISCOVER_TOOL_NAME,
    handler=discover_tools,
    description=(
        "当前任务缺少工具/函数、预览后缺少执行或状态工具时，先调用本工具检索并补齐当前请求的可用列表，再继续任务。"
        "不要未经检索就声称工具不存在。query 可用功能关键词或工具精确名称。只检索已注册且有权限的能力；"
        "不安装代码、不执行目标动作、不绕过确认。每请求最多检索3次，计入累计工具调用预算。"
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 160}},
        "required": ["query"],
        "additionalProperties": False,
    },
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=10,
    result_limit=6000,
)

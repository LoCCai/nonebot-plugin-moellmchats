from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
import heapq
import re
from types import MappingProxyType
from typing import Any

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CAPABILITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


class ToolGraphRelation(str, Enum):
    """Stable relation vocabulary carried by ToolGraph edges."""

    DEPENDS_ON = "depends_on"
    PARALLEL_WITH = "parallel_with"
    CONFLICTS_WITH = "conflicts_with"


_UNDIRECTED_RELATIONS = frozenset(
    {
        ToolGraphRelation.PARALLEL_WITH,
        ToolGraphRelation.CONFLICTS_WITH,
    }
)


def _require_tool_name(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _TOOL_NAME_RE.fullmatch(value):
        raise ValueError(f"{label} 必须是安全工具名")
    return value


@dataclass(frozen=True)
class ToolGraphEdge:
    """One directed dependency or canonical undirected tool relation."""

    source: str
    target: str
    relation: ToolGraphRelation

    def __post_init__(self) -> None:
        source = _require_tool_name(self.source, label="ToolGraphEdge.source")
        target = _require_tool_name(self.target, label="ToolGraphEdge.target")
        if not isinstance(self.relation, ToolGraphRelation):
            raise ValueError("ToolGraphEdge.relation 必须是 ToolGraphRelation")
        if source == target:
            raise ValueError("ToolGraphEdge 不得引用自身")
        if self.relation in _UNDIRECTED_RELATIONS and target < source:
            object.__setattr__(self, "source", target)
            object.__setattr__(self, "target", source)

    def as_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation.value,
        }


def _edge_sort_key(edge: ToolGraphEdge) -> tuple[str, str, str]:
    return (edge.relation.value, edge.source, edge.target)


def _topological_order(
    tools: tuple[str, ...],
    edges: tuple[ToolGraphEdge, ...],
) -> tuple[str, ...]:
    dependencies = {tool: set[str]() for tool in tools}
    dependents = {tool: set[str]() for tool in tools}
    for edge in edges:
        if edge.relation is not ToolGraphRelation.DEPENDS_ON:
            continue
        dependencies[edge.source].add(edge.target)
        dependents[edge.target].add(edge.source)

    ready = [tool for tool, required in dependencies.items() if not required]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        current = heapq.heappop(ready)
        ordered.append(current)
        for dependent in sorted(dependents[current]):
            dependencies[dependent].remove(current)
            if not dependencies[dependent]:
                heapq.heappush(ready, dependent)
    if len(ordered) != len(tools):
        raise ValueError("ToolGraph depends_on 不得包含循环")
    return tuple(ordered)


@dataclass(frozen=True)
class ToolGraph:
    """Immutable, validated graph policy for one set of named tools."""

    tools: tuple[str, ...]
    edges: tuple[ToolGraphEdge, ...] = ()
    requires_confirmation: AbstractSet[str] = frozenset()
    requires_capability: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tools, tuple) or not all(
            isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in self.tools
        ):
            raise ValueError("ToolGraph.tools 必须是安全工具名元组")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("ToolGraph.tools 不得重复")
        tools = tuple(sorted(self.tools))
        tool_set = frozenset(tools)

        if not isinstance(self.edges, tuple) or not all(isinstance(edge, ToolGraphEdge) for edge in self.edges):
            raise ValueError("ToolGraph.edges 必须是 ToolGraphEdge 元组")
        edges = tuple(sorted(self.edges, key=_edge_sort_key))
        if len(set(edges)) != len(edges):
            raise ValueError("ToolGraph.edges 不得重复")
        pair_relations: dict[tuple[str, str], ToolGraphRelation] = {}
        for edge in edges:
            if edge.source not in tool_set or edge.target not in tool_set:
                raise ValueError("ToolGraph edge 引用了未知工具")
            pair = (edge.source, edge.target) if edge.source < edge.target else (edge.target, edge.source)
            previous = pair_relations.get(pair)
            if previous is not None and previous is not edge.relation:
                raise ValueError("ToolGraph 同一工具对的关系互相矛盾")
            pair_relations[pair] = edge.relation

        if not isinstance(self.requires_confirmation, AbstractSet) or not all(
            isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in self.requires_confirmation
        ):
            raise ValueError("ToolGraph.requires_confirmation 必须是工具名集合")
        confirmation = frozenset(self.requires_confirmation)
        if not confirmation <= tool_set:
            raise ValueError("ToolGraph confirmation 引用了未知工具")

        if not isinstance(self.requires_capability, Mapping):
            raise ValueError("ToolGraph.requires_capability 必须是映射")
        capabilities: dict[str, tuple[str, ...]] = {}
        for tool, required in self.requires_capability.items():
            _require_tool_name(tool, label="ToolGraph capability tool")
            if tool not in tool_set:
                raise ValueError("ToolGraph capability 引用了未知工具")
            if (
                not isinstance(required, tuple)
                or not required
                or not all(isinstance(capability, str) and _CAPABILITY_NAME_RE.fullmatch(capability) for capability in required)
            ):
                raise ValueError("ToolGraph requires_capability 必须是非空安全 capability 元组")
            if len(set(required)) != len(required):
                raise ValueError("ToolGraph requires_capability 不得重复")
            capabilities[tool] = tuple(sorted(required))

        _topological_order(tools, edges)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "requires_confirmation", confirmation)
        object.__setattr__(
            self,
            "requires_capability",
            MappingProxyType(dict(sorted(capabilities.items()))),
        )

    def _require_member(self, tool: str) -> str:
        _require_tool_name(tool, label="ToolGraph.tool")
        if tool not in self.tools:
            raise ValueError(f"ToolGraph 不包含工具 {tool}")
        return tool

    def topological_order(self) -> tuple[str, ...]:
        return _topological_order(self.tools, self.edges)

    def dependencies_for(self, tool: str) -> tuple[str, ...]:
        member = self._require_member(tool)
        return tuple(
            edge.target for edge in self.edges if edge.relation is ToolGraphRelation.DEPENDS_ON and edge.source == member
        )

    def transitive_dependencies_for(self, tool: str) -> tuple[str, ...]:
        member = self._require_member(tool)
        seen: set[str] = set()
        pending = list(self.dependencies_for(member))
        while pending:
            dependency = pending.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            pending.extend(self.dependencies_for(dependency))
        return tuple(candidate for candidate in self.topological_order() if candidate in seen)

    def dependents_for(self, tool: str) -> tuple[str, ...]:
        member = self._require_member(tool)
        return tuple(
            edge.source for edge in self.edges if edge.relation is ToolGraphRelation.DEPENDS_ON and edge.target == member
        )

    def parallel_tools_for(self, tool: str) -> tuple[str, ...]:
        return self._undirected_neighbors(tool, ToolGraphRelation.PARALLEL_WITH)

    def conflicts_for(self, tool: str) -> tuple[str, ...]:
        return self._undirected_neighbors(tool, ToolGraphRelation.CONFLICTS_WITH)

    def _undirected_neighbors(
        self,
        tool: str,
        relation: ToolGraphRelation,
    ) -> tuple[str, ...]:
        member = self._require_member(tool)
        neighbors: list[str] = []
        for edge in self.edges:
            if edge.relation is not relation:
                continue
            if edge.source == member:
                neighbors.append(edge.target)
            elif edge.target == member:
                neighbors.append(edge.source)
        return tuple(sorted(neighbors))

    def confirmation_required_for(self, tool: str) -> bool:
        return self._require_member(tool) in self.requires_confirmation

    def capabilities_required_for(self, tool: str) -> tuple[str, ...]:
        return self.requires_capability.get(self._require_member(tool), ())

    def as_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "edges": [edge.as_dict() for edge in self.edges],
            "requires_confirmation": sorted(self.requires_confirmation),
            "requires_capability": {tool: list(capabilities) for tool, capabilities in self.requires_capability.items()},
        }

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from .tool_contracts import ToolEffect
from .tool_graph import ToolGraph

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_PARALLELISM_LIMIT = 64


class ToolSchedulingError(ValueError):
    """The requested tools cannot be represented by a safe schedule."""


class ToolScheduleMode(str, Enum):
    SERIAL = "serial"
    PARALLEL = "parallel"


@dataclass(frozen=True)
class ToolScheduleBatch:
    """One immutable execution batch; serial batches contain exactly one tool."""

    tools: tuple[str, ...]
    mode: ToolScheduleMode

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tools, tuple)
            or not self.tools
            or not all(isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in self.tools)
        ):
            raise ValueError("ToolScheduleBatch.tools 必须是非空安全工具名元组")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("ToolScheduleBatch.tools 不得重复")
        if not isinstance(self.mode, ToolScheduleMode):
            raise ValueError("ToolScheduleBatch.mode 必须是 ToolScheduleMode")
        if self.mode is ToolScheduleMode.SERIAL and len(self.tools) != 1:
            raise ValueError("serial ToolScheduleBatch 必须恰好包含一个工具")
        if self.mode is ToolScheduleMode.PARALLEL and len(self.tools) < 2:
            raise ValueError("parallel ToolScheduleBatch 必须至少包含两个工具")
        object.__setattr__(self, "tools", tuple(sorted(self.tools)))

    @property
    def is_parallel(self) -> bool:
        return self.mode is ToolScheduleMode.PARALLEL

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "tools": list(self.tools),
        }


@dataclass(frozen=True)
class ToolSchedule:
    """A deterministic sequence of immutable tool batches."""

    batches: tuple[ToolScheduleBatch, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.batches, tuple) or not all(isinstance(batch, ToolScheduleBatch) for batch in self.batches):
            raise ValueError("ToolSchedule.batches 必须是 ToolScheduleBatch 元组")
        tools = tuple(tool for batch in self.batches for tool in batch.tools)
        if len(set(tools)) != len(tools):
            raise ValueError("ToolSchedule 不得重复调度工具")

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(tool for batch in self.batches for tool in batch.tools)

    @property
    def has_parallel_batches(self) -> bool:
        return any(batch.is_parallel for batch in self.batches)

    def as_dict(self) -> dict[str, Any]:
        return {"batches": [batch.as_dict() for batch in self.batches]}


@dataclass(frozen=True)
class ReadOnlyParallelToolScheduler:
    """Build plans that parallelize only explicitly compatible read-only tools."""

    max_parallelism: int = 4

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_parallelism, int)
            or isinstance(self.max_parallelism, bool)
            or not 1 <= self.max_parallelism <= _MAX_PARALLELISM_LIMIT
        ):
            raise ValueError("ReadOnlyParallelToolScheduler.max_parallelism 必须是 1 到 64 的整数")

    def plan(
        self,
        *,
        graph: ToolGraph,
        selected_tools: tuple[str, ...],
        effects: Mapping[str, ToolEffect],
    ) -> ToolSchedule:
        if not isinstance(graph, ToolGraph):
            raise ToolSchedulingError("scheduler graph 必须是 ToolGraph")
        selected = self._validate_selected_tools(graph, selected_tools)
        self._validate_effects(selected, effects)
        if not selected:
            return ToolSchedule()

        dependencies = {tool: frozenset(graph.dependencies_for(tool)) for tool in selected}
        for tool in sorted(selected):
            missing = set(graph.transitive_dependencies_for(tool)) - selected
            if missing:
                raise ToolSchedulingError(f"工具 {tool} 缺少依赖闭包: {', '.join(sorted(missing))}")

        self._reject_unresolved_conflicts(graph, selected)
        parallel_neighbors = {tool: frozenset(graph.parallel_tools_for(tool)) & selected for tool in selected}
        order = tuple(tool for tool in graph.topological_order() if tool in selected)
        remaining = set(selected)
        completed: set[str] = set()
        batches: list[ToolScheduleBatch] = []

        while remaining:
            ready = [tool for tool in order if tool in remaining and dependencies[tool] <= completed]
            if not ready:
                raise ToolSchedulingError("scheduler 无法为依赖图生成 ready batch")

            batch_tools = [ready[0]]
            if self._parallel_eligible(graph, effects, ready[0]):
                for candidate in ready[1:]:
                    if len(batch_tools) >= self.max_parallelism:
                        break
                    if not self._parallel_eligible(graph, effects, candidate):
                        continue
                    if all(candidate in parallel_neighbors[scheduled] for scheduled in batch_tools):
                        batch_tools.append(candidate)

            mode = ToolScheduleMode.PARALLEL if len(batch_tools) > 1 else ToolScheduleMode.SERIAL
            batch = ToolScheduleBatch(tuple(batch_tools), mode)
            batches.append(batch)
            remaining.difference_update(batch.tools)
            completed.update(batch.tools)

        return ToolSchedule(tuple(batches))

    @staticmethod
    def _validate_selected_tools(
        graph: ToolGraph,
        selected_tools: tuple[str, ...],
    ) -> set[str]:
        if not isinstance(selected_tools, tuple) or not all(
            isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in selected_tools
        ):
            raise ToolSchedulingError("scheduler selected_tools 必须是安全工具名元组")
        selected = set(selected_tools)
        if len(selected) != len(selected_tools):
            raise ToolSchedulingError("scheduler selected_tools 不得重复")
        unknown = selected - set(graph.tools)
        if unknown:
            raise ToolSchedulingError(f"scheduler selected_tools 包含未知工具: {', '.join(sorted(unknown))}")
        return selected

    @staticmethod
    def _validate_effects(
        selected: set[str],
        effects: Mapping[str, ToolEffect],
    ) -> None:
        if not isinstance(effects, Mapping):
            raise ToolSchedulingError("scheduler effects 必须是工具 effect 映射")
        effect_names = set(effects)
        if effect_names != selected:
            missing = selected - effect_names
            extra = effect_names - selected
            details: list[str] = []
            if missing:
                details.append(f"缺少 {', '.join(sorted(missing))}")
            if extra:
                details.append("多余 " + ", ".join(sorted(str(name) for name in extra)))
            raise ToolSchedulingError(
                "scheduler effects 必须精确覆盖 selected_tools" + (f": {'; '.join(details)}" if details else "")
            )
        if not all(isinstance(effect, ToolEffect) for effect in effects.values()):
            raise ToolSchedulingError("scheduler effects 必须使用 ToolEffect")

    @staticmethod
    def _reject_unresolved_conflicts(
        graph: ToolGraph,
        selected: set[str],
    ) -> None:
        for tool in sorted(selected):
            conflicts = selected.intersection(graph.conflicts_for(tool))
            if conflicts:
                other = min(conflicts)
                raise ToolSchedulingError(f"工具冲突 {tool} / {other} 必须先由 E-08 policy 裁决")

    @staticmethod
    def _parallel_eligible(
        graph: ToolGraph,
        effects: Mapping[str, ToolEffect],
        tool: str,
    ) -> bool:
        return effects[tool] is ToolEffect.READ_ONLY and not graph.confirmation_required_for(tool)

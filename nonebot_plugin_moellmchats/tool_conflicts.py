from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from .tool_graph import ToolGraph

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ToolConflictPolicyError(ValueError):
    """Conflict policy input or configuration is invalid."""


class ToolConflictAction(str, Enum):
    REJECT = "reject"
    PREFER = "prefer"


class ToolConflictResolutionStatus(str, Enum):
    ALLOWED = "allowed"
    REJECTED = "rejected"


def _require_tool_name(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not _TOOL_NAME_RE.fullmatch(value):
        raise ValueError(f"{label} 必须是安全工具名")
    return value


def _canonical_pair(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first < second else (second, first)


@dataclass(frozen=True)
class ToolConflictRule:
    """An explicit reject or winner for one canonical conflict pair."""

    first: str
    second: str
    action: ToolConflictAction
    winner: str | None = None

    def __post_init__(self) -> None:
        first = _require_tool_name(self.first, label="ToolConflictRule.first")
        second = _require_tool_name(self.second, label="ToolConflictRule.second")
        if first == second:
            raise ValueError("ToolConflictRule 不得引用自身")
        if not isinstance(self.action, ToolConflictAction):
            raise ValueError("ToolConflictRule.action 必须是 ToolConflictAction")
        pair = _canonical_pair(first, second)
        if self.action is ToolConflictAction.REJECT:
            if self.winner is not None:
                raise ValueError("reject ToolConflictRule 不得指定 winner")
        elif self.winner not in pair:
            raise ValueError("prefer ToolConflictRule.winner 必须是冲突端点")
        object.__setattr__(self, "first", pair[0])
        object.__setattr__(self, "second", pair[1])

    @property
    def pair(self) -> tuple[str, str]:
        return (self.first, self.second)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.pair),
            "action": self.action.value,
            "winner": self.winner,
        }


@dataclass(frozen=True)
class ToolConflictDecision:
    """The auditable decision for one selected conflict pair."""

    tools: tuple[str, str]
    action: ToolConflictAction
    winner: str | None
    loser: str | None
    explicit_rule: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tools, tuple)
            or len(self.tools) != 2
            or not all(isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in self.tools)
            or self.tools[0] == self.tools[1]
        ):
            raise ValueError("ToolConflictDecision.tools 必须是两个不同安全工具名")
        pair = _canonical_pair(*self.tools)
        if not isinstance(self.action, ToolConflictAction):
            raise ValueError("ToolConflictDecision.action 必须是 ToolConflictAction")
        if not isinstance(self.explicit_rule, bool):
            raise ValueError("ToolConflictDecision.explicit_rule 必须是布尔值")
        if self.action is ToolConflictAction.REJECT:
            if self.winner is not None or self.loser is not None:
                raise ValueError("reject ToolConflictDecision 不得指定 winner/loser")
        else:
            if not self.explicit_rule:
                raise ValueError("prefer ToolConflictDecision 必须来自显式规则")
            if self.winner not in pair or self.loser not in pair or self.winner == self.loser:
                raise ValueError("prefer ToolConflictDecision 必须指定不同 winner/loser 端点")
        object.__setattr__(self, "tools", pair)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tools": list(self.tools),
            "action": self.action.value,
            "winner": self.winner,
            "loser": self.loser,
            "explicit_rule": self.explicit_rule,
        }


@dataclass(frozen=True)
class ToolConflictResolution:
    """An immutable allow/reject result suitable for audit and scheduling."""

    status: ToolConflictResolutionStatus
    requested_tools: tuple[str, ...]
    selected_tools: tuple[str, ...]
    dropped_tools: tuple[str, ...]
    decisions: tuple[ToolConflictDecision, ...]
    denial_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ToolConflictResolutionStatus):
            raise ValueError("ToolConflictResolution.status 必须是 ToolConflictResolutionStatus")
        for label, tools in (
            ("requested_tools", self.requested_tools),
            ("selected_tools", self.selected_tools),
            ("dropped_tools", self.dropped_tools),
        ):
            if not isinstance(tools, tuple) or not all(isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in tools):
                raise ValueError(f"ToolConflictResolution.{label} 必须是安全工具名元组")
            if len(set(tools)) != len(tools):
                raise ValueError(f"ToolConflictResolution.{label} 不得重复")
        if not isinstance(self.decisions, tuple) or not all(
            isinstance(decision, ToolConflictDecision) for decision in self.decisions
        ):
            raise ValueError("ToolConflictResolution.decisions 必须是 ToolConflictDecision 元组")

        requested = frozenset(self.requested_tools)
        selected = frozenset(self.selected_tools)
        dropped = frozenset(self.dropped_tools)
        if not selected <= requested or not dropped <= requested:
            raise ValueError("ToolConflictResolution 结果包含未请求工具")
        if selected & dropped:
            raise ValueError("ToolConflictResolution 工具不得同时 selected 和 dropped")
        decision_pairs = [decision.tools for decision in self.decisions]
        if len(set(decision_pairs)) != len(decision_pairs):
            raise ValueError("ToolConflictResolution.decisions 不得重复工具对")
        if any(not set(pair) <= requested for pair in decision_pairs):
            raise ValueError("ToolConflictResolution decision 包含未请求工具")

        if self.status is ToolConflictResolutionStatus.ALLOWED:
            if self.denial_reason is not None:
                raise ValueError("allowed ToolConflictResolution 不得有 denial_reason")
            if selected | dropped != requested:
                raise ValueError("allowed ToolConflictResolution 必须完整划分请求工具")
            if any(decision.action is not ToolConflictAction.PREFER for decision in self.decisions):
                raise ValueError("allowed ToolConflictResolution 不得包含 reject decision")
            decision_losers = {decision.loser for decision in self.decisions if decision.loser is not None}
            if decision_losers != dropped:
                raise ValueError("allowed ToolConflictResolution.dropped_tools 必须匹配 decision losers")
        else:
            if self.selected_tools or self.dropped_tools:
                raise ValueError("rejected ToolConflictResolution 不得暴露部分执行工具")
            if not isinstance(self.denial_reason, str) or not self.denial_reason:
                raise ValueError("rejected ToolConflictResolution 必须有 denial_reason")

        object.__setattr__(self, "requested_tools", tuple(sorted(requested)))
        object.__setattr__(self, "selected_tools", tuple(sorted(selected)))
        object.__setattr__(self, "dropped_tools", tuple(sorted(dropped)))
        object.__setattr__(
            self,
            "decisions",
            tuple(sorted(self.decisions, key=lambda decision: decision.tools)),
        )

    @property
    def allowed(self) -> bool:
        return self.status is ToolConflictResolutionStatus.ALLOWED

    def require_selected_tools(self) -> tuple[str, ...]:
        if not self.allowed:
            raise ToolConflictPolicyError(self.denial_reason or "工具冲突决议拒绝执行")
        return self.selected_tools

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "requested_tools": list(self.requested_tools),
            "selected_tools": list(self.selected_tools),
            "dropped_tools": list(self.dropped_tools),
            "decisions": [decision.as_dict() for decision in self.decisions],
            "denial_reason": self.denial_reason,
        }


@dataclass(frozen=True)
class ToolConflictPolicy:
    """Resolve selected graph conflicts only through explicit pair rules."""

    rules: tuple[ToolConflictRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple) or not all(isinstance(rule, ToolConflictRule) for rule in self.rules):
            raise ValueError("ToolConflictPolicy.rules 必须是 ToolConflictRule 元组")
        pairs = [rule.pair for rule in self.rules]
        if len(set(pairs)) != len(pairs):
            raise ValueError("ToolConflictPolicy.rules 不得重复工具对")
        object.__setattr__(
            self,
            "rules",
            tuple(sorted(self.rules, key=lambda rule: rule.pair)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"rules": [rule.as_dict() for rule in self.rules]}

    def resolve(
        self,
        *,
        graph: ToolGraph,
        selected_tools: tuple[str, ...],
    ) -> ToolConflictResolution:
        if not isinstance(graph, ToolGraph):
            raise ToolConflictPolicyError("conflict graph 必须是 ToolGraph")
        self._validate_rules_for_graph(graph)
        selected = self._validate_selected_tools(graph, selected_tools)
        requested = tuple(sorted(selected))
        pairs = self._selected_conflict_pairs(graph, selected)
        rules = {rule.pair: rule for rule in self.rules}
        decisions: list[ToolConflictDecision] = []

        for pair in pairs:
            rule = rules.get(pair)
            if rule is None:
                decisions.append(
                    ToolConflictDecision(
                        tools=pair,
                        action=ToolConflictAction.REJECT,
                        winner=None,
                        loser=None,
                        explicit_rule=False,
                    )
                )
            elif rule.action is ToolConflictAction.REJECT:
                decisions.append(
                    ToolConflictDecision(
                        tools=pair,
                        action=ToolConflictAction.REJECT,
                        winner=None,
                        loser=None,
                        explicit_rule=True,
                    )
                )
            else:
                winner = rule.winner
                if winner is None:
                    raise ToolConflictPolicyError("prefer conflict rule 缺少 winner")
                loser = pair[1] if winner == pair[0] else pair[0]
                decisions.append(
                    ToolConflictDecision(
                        tools=pair,
                        action=ToolConflictAction.PREFER,
                        winner=winner,
                        loser=loser,
                        explicit_rule=True,
                    )
                )

        rejected_pairs = [decision.tools for decision in decisions if decision.action is ToolConflictAction.REJECT]
        if rejected_pairs:
            labels = [f"{first}/{second}" for first, second in rejected_pairs]
            return self._rejected(
                requested,
                decisions,
                "工具冲突未获显式 prefer: " + ", ".join(labels),
            )

        dropped = {decision.loser for decision in decisions if decision.loser is not None}
        survivors = selected - dropped
        if selected and not survivors:
            return self._rejected(
                requested,
                decisions,
                "工具冲突规则移除了全部选中工具",
            )
        for tool in sorted(survivors):
            missing = set(graph.transitive_dependencies_for(tool)) - survivors
            if missing:
                return self._rejected(
                    requested,
                    decisions,
                    f"冲突决议破坏工具 {tool} 的依赖闭包: " + ", ".join(sorted(missing)),
                )

        return ToolConflictResolution(
            status=ToolConflictResolutionStatus.ALLOWED,
            requested_tools=requested,
            selected_tools=tuple(survivors),
            dropped_tools=tuple(dropped),
            decisions=tuple(decisions),
        )

    def _validate_rules_for_graph(self, graph: ToolGraph) -> None:
        graph_tools = set(graph.tools)
        for rule in self.rules:
            if not set(rule.pair) <= graph_tools:
                raise ToolConflictPolicyError(f"conflict rule 引用未知工具: {rule.first}/{rule.second}")
            if rule.second not in graph.conflicts_for(rule.first):
                raise ToolConflictPolicyError(f"conflict rule 不匹配 ToolGraph: {rule.first}/{rule.second}")

    @staticmethod
    def _validate_selected_tools(
        graph: ToolGraph,
        selected_tools: tuple[str, ...],
    ) -> set[str]:
        if not isinstance(selected_tools, tuple) or not all(
            isinstance(tool, str) and _TOOL_NAME_RE.fullmatch(tool) for tool in selected_tools
        ):
            raise ToolConflictPolicyError("conflict selected_tools 必须是安全工具名元组")
        selected = set(selected_tools)
        if len(selected) != len(selected_tools):
            raise ToolConflictPolicyError("conflict selected_tools 不得重复")
        unknown = selected - set(graph.tools)
        if unknown:
            raise ToolConflictPolicyError("conflict selected_tools 包含未知工具: " + ", ".join(sorted(unknown)))
        return selected

    @staticmethod
    def _selected_conflict_pairs(
        graph: ToolGraph,
        selected: set[str],
    ) -> tuple[tuple[str, str], ...]:
        pairs = {_canonical_pair(tool, other) for tool in selected for other in graph.conflicts_for(tool) if other in selected}
        return tuple(sorted(pairs))

    @staticmethod
    def _rejected(
        requested: tuple[str, ...],
        decisions: list[ToolConflictDecision],
        reason: str,
    ) -> ToolConflictResolution:
        return ToolConflictResolution(
            status=ToolConflictResolutionStatus.REJECTED,
            requested_tools=requested,
            selected_tools=(),
            dropped_tools=(),
            decisions=tuple(decisions),
            denial_reason=reason,
        )

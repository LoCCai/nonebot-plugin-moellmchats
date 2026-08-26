from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nonebot_plugin_moellmchats.tool_conflicts import (
    ToolConflictAction,
    ToolConflictDecision,
    ToolConflictPolicy,
    ToolConflictPolicyError,
    ToolConflictResolution,
    ToolConflictResolutionStatus,
    ToolConflictRule,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect
from nonebot_plugin_moellmchats.tool_graph import (
    ToolGraph,
    ToolGraphEdge,
    ToolGraphRelation,
)
from nonebot_plugin_moellmchats.tool_scheduler import (
    ReadOnlyParallelToolScheduler,
)


def _conflict(first: str, second: str) -> ToolGraphEdge:
    return ToolGraphEdge(first, second, ToolGraphRelation.CONFLICTS_WITH)


def _prefer(
    first: str,
    second: str,
    winner: str,
) -> ToolConflictRule:
    return ToolConflictRule(
        first,
        second,
        ToolConflictAction.PREFER,
        winner,
    )


def _prefer_decision(
    first: str = "tool_a",
    second: str = "tool_b",
    winner: str = "tool_a",
) -> ToolConflictDecision:
    loser = second if winner == first else first
    return ToolConflictDecision(
        tools=(first, second),
        action=ToolConflictAction.PREFER,
        winner=winner,
        loser=loser,
        explicit_rule=True,
    )


def test_policy_allows_a_nonconflicting_selection_unchanged() -> None:
    graph = ToolGraph(tools=("tool_b", "tool_a"))

    resolution = ToolConflictPolicy().resolve(
        graph=graph,
        selected_tools=("tool_b", "tool_a"),
    )

    assert resolution.allowed is True
    assert resolution.require_selected_tools() == ("tool_a", "tool_b")
    assert resolution.as_dict() == {
        "status": "allowed",
        "requested_tools": ["tool_a", "tool_b"],
        "selected_tools": ["tool_a", "tool_b"],
        "dropped_tools": [],
        "decisions": [],
        "denial_reason": None,
    }


def test_policy_allows_an_empty_selection() -> None:
    resolution = ToolConflictPolicy().resolve(
        graph=ToolGraph(tools=("available",)),
        selected_tools=(),
    )

    assert resolution.allowed is True
    assert resolution.require_selected_tools() == ()
    assert resolution.as_dict()["requested_tools"] == []


def test_policy_rejects_an_unruled_conflict_by_default() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b"),
        edges=(_conflict("tool_b", "tool_a"),),
    )

    resolution = ToolConflictPolicy().resolve(
        graph=graph,
        selected_tools=("tool_b", "tool_a"),
    )

    assert resolution.allowed is False
    assert resolution.selected_tools == ()
    assert resolution.dropped_tools == ()
    assert resolution.as_dict() == {
        "status": "rejected",
        "requested_tools": ["tool_a", "tool_b"],
        "selected_tools": [],
        "dropped_tools": [],
        "decisions": [
            {
                "tools": ["tool_a", "tool_b"],
                "action": "reject",
                "winner": None,
                "loser": None,
                "explicit_rule": False,
            }
        ],
        "denial_reason": "工具冲突未获显式 prefer: tool_a/tool_b",
    }
    with pytest.raises(ToolConflictPolicyError, match="未获显式"):
        resolution.require_selected_tools()


def test_policy_supports_an_explicit_reject_rule() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b"),
        edges=(_conflict("tool_a", "tool_b"),),
    )
    policy = ToolConflictPolicy(
        (
            ToolConflictRule(
                "tool_b",
                "tool_a",
                ToolConflictAction.REJECT,
            ),
        )
    )

    resolution = policy.resolve(
        graph=graph,
        selected_tools=("tool_a", "tool_b"),
    )

    assert resolution.allowed is False
    assert resolution.decisions[0].explicit_rule is True
    assert resolution.decisions[0].action is ToolConflictAction.REJECT


def test_policy_applies_an_explicit_canonical_winner() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b"),
        edges=(_conflict("tool_a", "tool_b"),),
    )
    policy = ToolConflictPolicy((_prefer("tool_b", "tool_a", "tool_b"),))

    resolution = policy.resolve(
        graph=graph,
        selected_tools=("tool_a", "tool_b"),
    )

    assert policy.as_dict() == {
        "rules": [
            {
                "tools": ["tool_a", "tool_b"],
                "action": "prefer",
                "winner": "tool_b",
            }
        ]
    }
    assert resolution.allowed is True
    assert resolution.selected_tools == ("tool_b",)
    assert resolution.dropped_tools == ("tool_a",)
    assert resolution.decisions[0].winner == "tool_b"
    assert resolution.decisions[0].loser == "tool_a"


def test_policy_resolves_multiple_pairs_simultaneously_and_deterministically() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b", "tool_c", "safe_tool"),
        edges=(
            _conflict("tool_a", "tool_b"),
            _conflict("tool_b", "tool_c"),
        ),
    )
    policy = ToolConflictPolicy(
        (
            _prefer("tool_c", "tool_b", "tool_b"),
            _prefer("tool_b", "tool_a", "tool_a"),
        )
    )

    resolution = policy.resolve(
        graph=graph,
        selected_tools=("tool_c", "safe_tool", "tool_b", "tool_a"),
    )

    assert resolution.allowed is True
    assert resolution.selected_tools == ("safe_tool", "tool_a")
    assert resolution.dropped_tools == ("tool_b", "tool_c")
    assert tuple(decision.tools for decision in resolution.decisions) == (
        ("tool_a", "tool_b"),
        ("tool_b", "tool_c"),
    )


def test_policy_rejects_cyclic_preferences_that_remove_every_tool() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b", "tool_c"),
        edges=(
            _conflict("tool_a", "tool_b"),
            _conflict("tool_b", "tool_c"),
            _conflict("tool_c", "tool_a"),
        ),
    )
    policy = ToolConflictPolicy(
        (
            _prefer("tool_a", "tool_b", "tool_a"),
            _prefer("tool_b", "tool_c", "tool_b"),
            _prefer("tool_c", "tool_a", "tool_c"),
        )
    )

    resolution = policy.resolve(
        graph=graph,
        selected_tools=("tool_a", "tool_b", "tool_c"),
    )

    assert resolution.allowed is False
    assert resolution.selected_tools == ()
    assert resolution.dropped_tools == ()
    assert resolution.denial_reason == "工具冲突规则移除了全部选中工具"


def test_policy_rejects_a_resolution_that_breaks_dependency_closure() -> None:
    graph = ToolGraph(
        tools=("preferred", "dependency", "consumer"),
        edges=(
            _conflict("preferred", "dependency"),
            ToolGraphEdge(
                "consumer",
                "dependency",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
    )
    policy = ToolConflictPolicy((_prefer("preferred", "dependency", "preferred"),))

    resolution = policy.resolve(
        graph=graph,
        selected_tools=("preferred", "dependency", "consumer"),
    )

    assert resolution.allowed is False
    assert resolution.denial_reason == ("冲突决议破坏工具 consumer 的依赖闭包: dependency")


def test_policy_rejects_an_incomplete_dependency_selection_without_conflicts() -> None:
    graph = ToolGraph(
        tools=("dependency", "consumer"),
        edges=(
            ToolGraphEdge(
                "consumer",
                "dependency",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
    )

    resolution = ToolConflictPolicy().resolve(
        graph=graph,
        selected_tools=("consumer",),
    )

    assert resolution.allowed is False
    assert resolution.decisions == ()
    assert resolution.denial_reason == ("冲突决议破坏工具 consumer 的依赖闭包: dependency")


def test_policy_does_not_require_rules_for_unselected_conflicts() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b", "safe_tool"),
        edges=(_conflict("tool_a", "tool_b"),),
    )

    resolution = ToolConflictPolicy().resolve(
        graph=graph,
        selected_tools=("safe_tool", "tool_a"),
    )

    assert resolution.allowed is True
    assert resolution.selected_tools == ("safe_tool", "tool_a")


def test_policy_requires_every_selected_conflict_to_be_preferred() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b", "tool_c", "tool_d"),
        edges=(
            _conflict("tool_a", "tool_b"),
            _conflict("tool_c", "tool_d"),
        ),
    )
    policy = ToolConflictPolicy((_prefer("tool_a", "tool_b", "tool_a"),))

    resolution = policy.resolve(
        graph=graph,
        selected_tools=("tool_a", "tool_b", "tool_c", "tool_d"),
    )

    assert resolution.allowed is False
    assert resolution.selected_tools == ()
    assert tuple(decision.action for decision in resolution.decisions) == (
        ToolConflictAction.PREFER,
        ToolConflictAction.REJECT,
    )


def test_resolution_can_feed_a_conflict_free_read_only_schedule() -> None:
    graph = ToolGraph(
        tools=("preferred", "rejected", "parallel_read"),
        edges=(
            _conflict("preferred", "rejected"),
            ToolGraphEdge(
                "preferred",
                "parallel_read",
                ToolGraphRelation.PARALLEL_WITH,
            ),
        ),
    )
    resolution = ToolConflictPolicy((_prefer("preferred", "rejected", "preferred"),)).resolve(
        graph=graph,
        selected_tools=("rejected", "parallel_read", "preferred"),
    )

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=resolution.require_selected_tools(),
        effects={
            "parallel_read": ToolEffect.READ_ONLY,
            "preferred": ToolEffect.READ_ONLY,
        },
    )

    assert schedule.as_dict() == {
        "batches": [
            {
                "mode": "parallel",
                "tools": ["parallel_read", "preferred"],
            }
        ]
    }


def test_policy_rejects_rules_with_unknown_graph_tools() -> None:
    policy = ToolConflictPolicy((_prefer("tool_a", "missing_tool", "tool_a"),))

    with pytest.raises(ToolConflictPolicyError, match="未知工具"):
        policy.resolve(
            graph=ToolGraph(tools=("tool_a",)),
            selected_tools=("tool_a",),
        )


def test_policy_rejects_rules_that_do_not_match_graph_conflicts() -> None:
    policy = ToolConflictPolicy((_prefer("tool_a", "tool_b", "tool_a"),))

    with pytest.raises(ToolConflictPolicyError, match="不匹配 ToolGraph"):
        policy.resolve(
            graph=ToolGraph(tools=("tool_a", "tool_b")),
            selected_tools=("tool_a", "tool_b"),
        )


@pytest.mark.parametrize(
    ("first", "second", "action", "winner", "message"),
    [
        ("", "tool_b", ToolConflictAction.REJECT, None, "first"),
        ("tool a", "tool_b", ToolConflictAction.REJECT, None, "first"),
        ("tool_a", "tool/b", ToolConflictAction.REJECT, None, "second"),
        ("tool_a", "tool_a", ToolConflictAction.REJECT, None, "自身"),
        ("tool_a", "tool_b", "reject", None, "action"),
        ("tool_a", "tool_b", ToolConflictAction.REJECT, "tool_a", "winner"),
        ("tool_a", "tool_b", ToolConflictAction.PREFER, None, "winner"),
        (
            "tool_a",
            "tool_b",
            ToolConflictAction.PREFER,
            "tool_c",
            "winner",
        ),
    ],
)
def test_conflict_rule_rejects_invalid_fields(
    first: str,
    second: str,
    action: object,
    winner: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolConflictRule(
            first,
            second,
            action,  # type: ignore[arg-type]
            winner,  # type: ignore[arg-type]
        )


def test_conflict_rule_canonicalizes_endpoints_without_changing_winner() -> None:
    rule = _prefer("tool_z", "tool_a", "tool_z")

    assert rule.first == "tool_a"
    assert rule.second == "tool_z"
    assert rule.pair == ("tool_a", "tool_z")
    assert rule.winner == "tool_z"


def test_conflict_policy_rejects_invalid_or_duplicate_rule_collections() -> None:
    first = _prefer("tool_a", "tool_b", "tool_a")
    reversed_duplicate = _prefer("tool_b", "tool_a", "tool_b")

    with pytest.raises(ValueError, match="rules"):
        ToolConflictPolicy([first])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rules"):
        ToolConflictPolicy((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="重复工具对"):
        ToolConflictPolicy((first, reversed_duplicate))


def test_policy_rejects_non_graph_inputs() -> None:
    with pytest.raises(ToolConflictPolicyError, match="graph"):
        ToolConflictPolicy().resolve(
            graph=object(),  # type: ignore[arg-type]
            selected_tools=(),
        )


@pytest.mark.parametrize(
    "selected_tools",
    [
        ["tool_a"],
        ("tool a",),
        ("tool_a", "tool_a"),
        ("missing_tool",),
    ],
)
def test_policy_rejects_invalid_selected_tools(selected_tools: object) -> None:
    with pytest.raises(ToolConflictPolicyError, match="selected_tools"):
        ToolConflictPolicy().resolve(
            graph=ToolGraph(tools=("tool_a",)),
            selected_tools=selected_tools,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("tools", "action", "winner", "loser", "explicit_rule", "message"),
    [
        (["tool_a", "tool_b"], ToolConflictAction.REJECT, None, None, False, "tools"),
        (("tool_a",), ToolConflictAction.REJECT, None, None, False, "tools"),
        (("tool a", "tool_b"), ToolConflictAction.REJECT, None, None, False, "tools"),
        (("tool_a", "tool_a"), ToolConflictAction.REJECT, None, None, False, "tools"),
        (("tool_a", "tool_b"), "reject", None, None, False, "action"),
        (("tool_a", "tool_b"), ToolConflictAction.REJECT, None, None, 0, "explicit_rule"),
        (("tool_a", "tool_b"), ToolConflictAction.REJECT, "tool_a", None, True, "winner/loser"),
        (("tool_a", "tool_b"), ToolConflictAction.PREFER, "tool_a", "tool_b", False, "显式规则"),
        (("tool_a", "tool_b"), ToolConflictAction.PREFER, None, "tool_b", True, "端点"),
        (("tool_a", "tool_b"), ToolConflictAction.PREFER, "tool_a", "tool_a", True, "端点"),
        (("tool_a", "tool_b"), ToolConflictAction.PREFER, "tool_a", "tool_c", True, "端点"),
    ],
)
def test_conflict_decision_rejects_invalid_fields(
    tools: object,
    action: object,
    winner: object,
    loser: object,
    explicit_rule: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolConflictDecision(
            tools=tools,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            winner=winner,  # type: ignore[arg-type]
            loser=loser,  # type: ignore[arg-type]
            explicit_rule=explicit_rule,  # type: ignore[arg-type]
        )


def test_conflict_decision_canonicalizes_tools() -> None:
    decision = ToolConflictDecision(
        tools=("tool_z", "tool_a"),
        action=ToolConflictAction.PREFER,
        winner="tool_z",
        loser="tool_a",
        explicit_rule=True,
    )

    assert decision.tools == ("tool_a", "tool_z")
    assert decision.winner == "tool_z"


def test_resolution_rejects_invalid_status_and_collections() -> None:
    decision = _prefer_decision()
    valid = {
        "status": ToolConflictResolutionStatus.ALLOWED,
        "requested_tools": ("tool_a", "tool_b"),
        "selected_tools": ("tool_a",),
        "dropped_tools": ("tool_b",),
        "decisions": (decision,),
        "denial_reason": None,
    }

    with pytest.raises(ValueError, match="status"):
        ToolConflictResolution(**{**valid, "status": "allowed"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requested_tools"):
        ToolConflictResolution(**{**valid, "requested_tools": ["tool_a", "tool_b"]})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requested_tools"):
        ToolConflictResolution(**{**valid, "requested_tools": ("tool_a", "tool_a")})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="decisions"):
        ToolConflictResolution(**{**valid, "decisions": [decision]})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="decisions"):
        ToolConflictResolution(**{**valid, "decisions": (object(),)})  # type: ignore[arg-type]


def test_allowed_resolution_rejects_inconsistent_results() -> None:
    prefer = _prefer_decision()
    reject = ToolConflictDecision(
        tools=("tool_a", "tool_b"),
        action=ToolConflictAction.REJECT,
        winner=None,
        loser=None,
        explicit_rule=True,
    )
    valid = {
        "status": ToolConflictResolutionStatus.ALLOWED,
        "requested_tools": ("tool_a", "tool_b"),
        "selected_tools": ("tool_a",),
        "dropped_tools": ("tool_b",),
        "decisions": (prefer,),
        "denial_reason": None,
    }

    with pytest.raises(ValueError, match="未请求"):
        ToolConflictResolution(**{**valid, "selected_tools": ("tool_c",)})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="同时"):
        ToolConflictResolution(**{**valid, "selected_tools": ("tool_a", "tool_b")})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="denial_reason"):
        ToolConflictResolution(**{**valid, "denial_reason": "no"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="完整划分"):
        ToolConflictResolution(**{**valid, "selected_tools": (), "dropped_tools": ()})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reject decision"):
        ToolConflictResolution(**{**valid, "decisions": (reject,)})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="decision losers"):
        ToolConflictResolution(**{**valid, "selected_tools": ("tool_a", "tool_b"), "dropped_tools": ()})  # type: ignore[arg-type]


def test_rejected_resolution_rejects_partial_or_unexplained_results() -> None:
    valid = {
        "status": ToolConflictResolutionStatus.REJECTED,
        "requested_tools": ("tool_a", "tool_b"),
        "selected_tools": (),
        "dropped_tools": (),
        "decisions": (),
        "denial_reason": "rejected",
    }

    with pytest.raises(ValueError, match="部分执行"):
        ToolConflictResolution(**{**valid, "selected_tools": ("tool_a",)})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="部分执行"):
        ToolConflictResolution(**{**valid, "dropped_tools": ("tool_b",)})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="denial_reason"):
        ToolConflictResolution(**{**valid, "denial_reason": None})  # type: ignore[arg-type]


def test_resolution_rejects_duplicate_or_unknown_decision_pairs() -> None:
    decision = _prefer_decision()
    duplicate = _prefer_decision(winner="tool_b")

    with pytest.raises(ValueError, match="重复工具对"):
        ToolConflictResolution(
            status=ToolConflictResolutionStatus.REJECTED,
            requested_tools=("tool_a", "tool_b"),
            selected_tools=(),
            dropped_tools=(),
            decisions=(decision, duplicate),
            denial_reason="rejected",
        )
    with pytest.raises(ValueError, match="未请求工具"):
        ToolConflictResolution(
            status=ToolConflictResolutionStatus.REJECTED,
            requested_tools=("tool_a", "tool_b"),
            selected_tools=(),
            dropped_tools=(),
            decisions=(_prefer_decision("tool_a", "tool_c", "tool_a"),),
            denial_reason="rejected",
        )


def test_policy_resolution_and_rules_are_frozen_with_fresh_serialization() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b"),
        edges=(_conflict("tool_a", "tool_b"),),
    )
    rule = _prefer("tool_b", "tool_a", "tool_a")
    policy = ToolConflictPolicy((rule,))
    resolution = policy.resolve(
        graph=graph,
        selected_tools=("tool_b", "tool_a"),
    )
    serialized = resolution.as_dict()

    serialized["requested_tools"].clear()
    serialized["decisions"][0]["tools"].clear()
    assert resolution.requested_tools == ("tool_a", "tool_b")
    assert resolution.decisions[0].tools == ("tool_a", "tool_b")
    with pytest.raises(FrozenInstanceError):
        rule.winner = "tool_b"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        policy.rules = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        resolution.status = ToolConflictResolutionStatus.REJECTED  # type: ignore[misc]

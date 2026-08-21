from __future__ import annotations

from dataclasses import FrozenInstanceError
from itertools import combinations

import pytest

from nonebot_plugin_moellmchats.tool_contracts import ToolEffect
from nonebot_plugin_moellmchats.tool_graph import (
    ToolGraph,
    ToolGraphEdge,
    ToolGraphRelation,
)
from nonebot_plugin_moellmchats.tool_scheduler import (
    ReadOnlyParallelToolScheduler,
    ToolSchedule,
    ToolScheduleBatch,
    ToolScheduleMode,
    ToolSchedulingError,
)


def _read_only_effects(*tools: str) -> dict[str, ToolEffect]:
    return dict.fromkeys(tools, ToolEffect.READ_ONLY)


def _parallel_clique(tools: tuple[str, ...]) -> tuple[ToolGraphEdge, ...]:
    return tuple(ToolGraphEdge(source, target, ToolGraphRelation.PARALLEL_WITH) for source, target in combinations(tools, 2))


def test_scheduler_parallelizes_the_documented_read_only_weather_queries() -> None:
    tools = ("beijing_weather", "shanghai_weather", "guangzhou_weather")
    graph = ToolGraph(tools=tools, edges=_parallel_clique(tools))

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=(
            "shanghai_weather",
            "guangzhou_weather",
            "beijing_weather",
        ),
        effects=_read_only_effects(*tools),
    )

    assert schedule == ToolSchedule(
        (
            ToolScheduleBatch(
                (
                    "beijing_weather",
                    "guangzhou_weather",
                    "shanghai_weather",
                ),
                ToolScheduleMode.PARALLEL,
            ),
        )
    )
    assert schedule.has_parallel_batches is True
    assert schedule.tools == (
        "beijing_weather",
        "guangzhou_weather",
        "shanghai_weather",
    )


def test_scheduler_respects_the_documented_analysis_pipeline() -> None:
    graph = ToolGraph(
        tools=("price_search", "fx_search", "analysis", "chart"),
        edges=(
            ToolGraphEdge(
                "price_search",
                "fx_search",
                ToolGraphRelation.PARALLEL_WITH,
            ),
            ToolGraphEdge(
                "analysis",
                "price_search",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "analysis",
                "fx_search",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "chart",
                "analysis",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
    )

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=("chart", "analysis", "price_search", "fx_search"),
        effects=_read_only_effects(
            "chart",
            "analysis",
            "price_search",
            "fx_search",
        ),
    )

    assert schedule.as_dict() == {
        "batches": [
            {
                "mode": "parallel",
                "tools": ["fx_search", "price_search"],
            },
            {"mode": "serial", "tools": ["analysis"]},
            {"mode": "serial", "tools": ["chart"]},
        ]
    }


def test_scheduler_parallelizes_tools_after_their_shared_dependency() -> None:
    graph = ToolGraph(
        tools=("base", "north", "south", "report"),
        edges=(
            ToolGraphEdge("north", "base", ToolGraphRelation.DEPENDS_ON),
            ToolGraphEdge("south", "base", ToolGraphRelation.DEPENDS_ON),
            ToolGraphEdge(
                "north",
                "south",
                ToolGraphRelation.PARALLEL_WITH,
            ),
            ToolGraphEdge("report", "north", ToolGraphRelation.DEPENDS_ON),
            ToolGraphEdge("report", "south", ToolGraphRelation.DEPENDS_ON),
        ),
    )

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=("report", "south", "base", "north"),
        effects=_read_only_effects("base", "north", "south", "report"),
    )

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "serial", "tools": ["base"]},
            {"mode": "parallel", "tools": ["north", "south"]},
            {"mode": "serial", "tools": ["report"]},
        ]
    }


def test_scheduler_bounds_parallel_batches_deterministically() -> None:
    tools = ("tool_a", "tool_b", "tool_c", "tool_d", "tool_e")
    graph = ToolGraph(tools=tools, edges=_parallel_clique(tools))

    schedule = ReadOnlyParallelToolScheduler(max_parallelism=2).plan(
        graph=graph,
        selected_tools=tuple(reversed(tools)),
        effects=_read_only_effects(*tools),
    )

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "parallel", "tools": ["tool_a", "tool_b"]},
            {"mode": "parallel", "tools": ["tool_c", "tool_d"]},
            {"mode": "serial", "tools": ["tool_e"]},
        ]
    }


def test_scheduler_never_parallelizes_mutating_tools() -> None:
    tools = ("a_mutate", "b_read", "c_read")
    graph = ToolGraph(tools=tools, edges=_parallel_clique(tools))
    effects = _read_only_effects(*tools)
    effects["a_mutate"] = ToolEffect.MUTATING

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=tools,
        effects=effects,
    )

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "serial", "tools": ["a_mutate"]},
            {"mode": "parallel", "tools": ["b_read", "c_read"]},
        ]
    }


def test_scheduler_never_parallelizes_confirmation_gated_tools() -> None:
    tools = ("a_confirm", "b_read")
    graph = ToolGraph(
        tools=tools,
        edges=(
            ToolGraphEdge(
                "a_confirm",
                "b_read",
                ToolGraphRelation.PARALLEL_WITH,
            ),
        ),
        requires_confirmation={"a_confirm"},
    )

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=tools,
        effects=_read_only_effects(*tools),
    )

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "serial", "tools": ["a_confirm"]},
            {"mode": "serial", "tools": ["b_read"]},
        ]
    }


def test_scheduler_requires_explicit_pairwise_parallel_relations() -> None:
    graph = ToolGraph(tools=("tool_a", "tool_b", "tool_c"))

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=("tool_c", "tool_b", "tool_a"),
        effects=_read_only_effects("tool_a", "tool_b", "tool_c"),
    )

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "serial", "tools": ["tool_a"]},
            {"mode": "serial", "tools": ["tool_b"]},
            {"mode": "serial", "tools": ["tool_c"]},
        ]
    }


def test_scheduler_uses_deterministic_greedy_pairwise_batches() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b", "tool_c"),
        edges=(
            ToolGraphEdge(
                "tool_a",
                "tool_b",
                ToolGraphRelation.PARALLEL_WITH,
            ),
            ToolGraphEdge(
                "tool_b",
                "tool_c",
                ToolGraphRelation.PARALLEL_WITH,
            ),
        ),
    )

    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=graph,
        selected_tools=("tool_c", "tool_b", "tool_a"),
        effects=_read_only_effects("tool_a", "tool_b", "tool_c"),
    )

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "parallel", "tools": ["tool_a", "tool_b"]},
            {"mode": "serial", "tools": ["tool_c"]},
        ]
    }


def test_scheduler_can_disable_parallel_batches_with_a_limit_of_one() -> None:
    tools = ("tool_a", "tool_b")
    graph = ToolGraph(tools=tools, edges=_parallel_clique(tools))

    schedule = ReadOnlyParallelToolScheduler(max_parallelism=1).plan(
        graph=graph,
        selected_tools=tools,
        effects=_read_only_effects(*tools),
    )

    assert schedule.has_parallel_batches is False
    assert schedule.as_dict() == {
        "batches": [
            {"mode": "serial", "tools": ["tool_a"]},
            {"mode": "serial", "tools": ["tool_b"]},
        ]
    }


def test_scheduler_is_independent_of_selected_tool_input_order() -> None:
    tools = ("tool_a", "tool_b", "tool_c")
    graph = ToolGraph(tools=tools, edges=_parallel_clique(tools))
    scheduler = ReadOnlyParallelToolScheduler(max_parallelism=2)
    effects = _read_only_effects(*tools)

    first = scheduler.plan(
        graph=graph,
        selected_tools=tools,
        effects=effects,
    )
    second = scheduler.plan(
        graph=graph,
        selected_tools=tuple(reversed(tools)),
        effects=effects,
    )

    assert first == second


def test_scheduler_supports_an_empty_selection() -> None:
    schedule = ReadOnlyParallelToolScheduler().plan(
        graph=ToolGraph(tools=("available",)),
        selected_tools=(),
        effects={},
    )

    assert schedule == ToolSchedule()
    assert schedule.tools == ()
    assert schedule.has_parallel_batches is False
    assert schedule.as_dict() == {"batches": []}


def test_scheduler_rejects_missing_transitive_dependency_closure() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b", "tool_c"),
        edges=(
            ToolGraphEdge(
                "tool_a",
                "tool_b",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "tool_b",
                "tool_c",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
    )

    with pytest.raises(ToolSchedulingError, match=r"缺少依赖闭包.*tool_c"):
        ReadOnlyParallelToolScheduler().plan(
            graph=graph,
            selected_tools=("tool_a", "tool_b"),
            effects=_read_only_effects("tool_a", "tool_b"),
        )


def test_scheduler_rejects_conflicts_without_choosing_a_winner() -> None:
    graph = ToolGraph(
        tools=("tool_a", "tool_b"),
        edges=(
            ToolGraphEdge(
                "tool_b",
                "tool_a",
                ToolGraphRelation.CONFLICTS_WITH,
            ),
        ),
    )

    with pytest.raises(
        ToolSchedulingError,
        match=r"工具冲突 tool_a / tool_b.*E-08",
    ):
        ReadOnlyParallelToolScheduler().plan(
            graph=graph,
            selected_tools=("tool_b", "tool_a"),
            effects=_read_only_effects("tool_a", "tool_b"),
        )


@pytest.mark.parametrize("max_parallelism", [0, -1, 65, True, 2.5, "4"])
def test_scheduler_rejects_invalid_parallelism_limits(
    max_parallelism: object,
) -> None:
    with pytest.raises(ValueError, match="max_parallelism"):
        ReadOnlyParallelToolScheduler(
            max_parallelism=max_parallelism  # type: ignore[arg-type]
        )


def test_scheduler_rejects_non_graph_inputs() -> None:
    with pytest.raises(ToolSchedulingError, match="graph"):
        ReadOnlyParallelToolScheduler().plan(
            graph=object(),  # type: ignore[arg-type]
            selected_tools=(),
            effects={},
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
def test_scheduler_rejects_invalid_selected_tools(
    selected_tools: object,
) -> None:
    with pytest.raises(ToolSchedulingError, match="selected_tools"):
        ReadOnlyParallelToolScheduler().plan(
            graph=ToolGraph(tools=("tool_a",)),
            selected_tools=selected_tools,  # type: ignore[arg-type]
            effects={},
        )


@pytest.mark.parametrize(
    "effects",
    [
        [],
        {},
        {"tool_a": ToolEffect.READ_ONLY, "extra": ToolEffect.READ_ONLY},
        {"tool_a": "read_only"},
    ],
)
def test_scheduler_rejects_invalid_effect_mappings(effects: object) -> None:
    with pytest.raises(ToolSchedulingError, match="effects"):
        ReadOnlyParallelToolScheduler().plan(
            graph=ToolGraph(tools=("tool_a",)),
            selected_tools=("tool_a",),
            effects=effects,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("tools", "mode", "message"),
    [
        ([], ToolScheduleMode.SERIAL, "tools"),
        ((), ToolScheduleMode.SERIAL, "tools"),
        (("tool a",), ToolScheduleMode.SERIAL, "tools"),
        (("tool_a", "tool_a"), ToolScheduleMode.PARALLEL, "重复"),
        (("tool_a", "tool_b"), ToolScheduleMode.SERIAL, "恰好"),
        (("tool_a",), ToolScheduleMode.PARALLEL, "至少"),
        (("tool_a",), "serial", "mode"),
    ],
)
def test_tool_schedule_batch_rejects_invalid_fields(
    tools: object,
    mode: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolScheduleBatch(
            tools=tools,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
        )


def test_tool_schedule_rejects_invalid_or_duplicate_batches() -> None:
    first = ToolScheduleBatch(("tool_a",), ToolScheduleMode.SERIAL)
    duplicate = ToolScheduleBatch(("tool_a",), ToolScheduleMode.SERIAL)

    with pytest.raises(ValueError, match="batches"):
        ToolSchedule([first])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="batches"):
        ToolSchedule((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="重复调度"):
        ToolSchedule((first, duplicate))


def test_tool_schedule_is_frozen_and_serializes_fresh_copies() -> None:
    batch = ToolScheduleBatch(
        ("tool_b", "tool_a"),
        ToolScheduleMode.PARALLEL,
    )
    schedule = ToolSchedule((batch,))

    assert batch.tools == ("tool_a", "tool_b")
    assert batch.is_parallel is True
    serialized = schedule.as_dict()
    serialized["batches"][0]["tools"].clear()

    assert schedule.as_dict() == {
        "batches": [
            {"mode": "parallel", "tools": ["tool_a", "tool_b"]},
        ]
    }
    with pytest.raises(FrozenInstanceError):
        batch.tools = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        schedule.batches = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ReadOnlyParallelToolScheduler().max_parallelism = 8  # type: ignore[misc]

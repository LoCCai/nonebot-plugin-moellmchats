from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nonebot_plugin_moellmchats.tool_graph import (
    ToolGraph,
    ToolGraphEdge,
    ToolGraphRelation,
)


def _example_graph() -> ToolGraph:
    return ToolGraph(
        tools=("price_search", "chart", "analysis", "fx_search"),
        edges=(
            ToolGraphEdge(
                "analysis",
                "price_search",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "chart",
                "analysis",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "analysis",
                "fx_search",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "price_search",
                "fx_search",
                ToolGraphRelation.PARALLEL_WITH,
            ),
        ),
        requires_confirmation={"chart"},
        requires_capability={
            "chart": ("chart.render", "artifact.write"),
        },
    )


def test_tool_graph_models_the_documented_analysis_pipeline() -> None:
    graph = _example_graph()

    assert graph.tools == ("analysis", "chart", "fx_search", "price_search")
    assert graph.topological_order() == (
        "fx_search",
        "price_search",
        "analysis",
        "chart",
    )
    assert graph.dependencies_for("analysis") == ("fx_search", "price_search")
    assert graph.dependencies_for("chart") == ("analysis",)
    assert graph.transitive_dependencies_for("chart") == (
        "fx_search",
        "price_search",
        "analysis",
    )
    assert graph.dependents_for("price_search") == ("analysis",)
    assert graph.parallel_tools_for("price_search") == ("fx_search",)
    assert graph.parallel_tools_for("fx_search") == ("price_search",)
    assert graph.conflicts_for("chart") == ()
    assert graph.confirmation_required_for("chart") is True
    assert graph.confirmation_required_for("analysis") is False
    assert graph.capabilities_required_for("chart") == (
        "artifact.write",
        "chart.render",
    )
    assert graph.capabilities_required_for("analysis") == ()


@pytest.mark.parametrize(
    "relation",
    [
        ToolGraphRelation.PARALLEL_WITH,
        ToolGraphRelation.CONFLICTS_WITH,
    ],
)
def test_tool_graph_edge_canonicalizes_undirected_relations(
    relation: ToolGraphRelation,
) -> None:
    edge = ToolGraphEdge("z_tool", "a_tool", relation)

    assert edge.source == "a_tool"
    assert edge.target == "z_tool"
    assert edge.as_dict() == {
        "source": "a_tool",
        "target": "z_tool",
        "relation": relation.value,
    }


def test_tool_graph_keeps_dependency_direction() -> None:
    edge = ToolGraphEdge(
        "z_tool",
        "a_tool",
        ToolGraphRelation.DEPENDS_ON,
    )
    graph = ToolGraph(tools=("a_tool", "z_tool"), edges=(edge,))

    assert edge.source == "z_tool"
    assert edge.target == "a_tool"
    assert graph.dependencies_for("z_tool") == ("a_tool",)
    assert graph.dependencies_for("a_tool") == ()


def test_tool_graph_is_deeply_frozen_and_detached_from_inputs() -> None:
    confirmation = {"chart"}
    capabilities = {"chart": ("chart.render",)}
    graph = ToolGraph(
        tools=("chart", "analysis"),
        edges=(
            ToolGraphEdge(
                "chart",
                "analysis",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
        requires_confirmation=confirmation,
        requires_capability=capabilities,
    )

    confirmation.clear()
    capabilities.clear()

    assert graph.requires_confirmation == frozenset({"chart"})
    assert dict(graph.requires_capability) == {"chart": ("chart.render",)}
    with pytest.raises(FrozenInstanceError):
        graph.tools = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        graph.requires_capability["chart"] = ()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        graph.edges[0].source = "analysis"  # type: ignore[misc]


def test_tool_graph_serialization_returns_fresh_primitive_copies() -> None:
    graph = _example_graph()
    serialized = graph.as_dict()

    assert serialized == {
        "tools": ["analysis", "chart", "fx_search", "price_search"],
        "edges": [
            {
                "source": "analysis",
                "target": "fx_search",
                "relation": "depends_on",
            },
            {
                "source": "analysis",
                "target": "price_search",
                "relation": "depends_on",
            },
            {
                "source": "chart",
                "target": "analysis",
                "relation": "depends_on",
            },
            {
                "source": "fx_search",
                "target": "price_search",
                "relation": "parallel_with",
            },
        ],
        "requires_confirmation": ["chart"],
        "requires_capability": {
            "chart": ["artifact.write", "chart.render"],
        },
    }

    serialized["tools"].clear()
    serialized["edges"][0]["source"] = "changed"
    serialized["requires_confirmation"].clear()
    serialized["requires_capability"]["chart"].clear()

    assert graph.as_dict()["tools"] == [
        "analysis",
        "chart",
        "fx_search",
        "price_search",
    ]
    assert graph.as_dict()["edges"][0]["source"] == "analysis"
    assert graph.confirmation_required_for("chart") is True
    assert graph.capabilities_required_for("chart") == (
        "artifact.write",
        "chart.render",
    )


@pytest.mark.parametrize(
    ("source", "target", "relation"),
    [
        ("", "tool_b", ToolGraphRelation.DEPENDS_ON),
        ("tool a", "tool_b", ToolGraphRelation.DEPENDS_ON),
        ("tool_a", "tool/b", ToolGraphRelation.DEPENDS_ON),
        ("tool_a", "tool_b", "depends_on"),
    ],
)
def test_tool_graph_edge_rejects_invalid_fields(
    source: str,
    target: str,
    relation: object,
) -> None:
    with pytest.raises(ValueError, match="ToolGraphEdge"):
        ToolGraphEdge(source, target, relation)  # type: ignore[arg-type]


@pytest.mark.parametrize("relation", list(ToolGraphRelation))
def test_tool_graph_edge_rejects_self_references(
    relation: ToolGraphRelation,
) -> None:
    with pytest.raises(ValueError, match="自身"):
        ToolGraphEdge("tool_a", "tool_a", relation)


@pytest.mark.parametrize(
    "tools",
    [
        ["tool_a"],
        ("tool a",),
        ("tool_a", "tool_a"),
    ],
)
def test_tool_graph_rejects_invalid_or_duplicate_tools(tools: object) -> None:
    with pytest.raises(ValueError, match="tools"):
        ToolGraph(tools=tools)  # type: ignore[arg-type]


def test_tool_graph_rejects_invalid_edge_collections() -> None:
    edge = ToolGraphEdge(
        "tool_a",
        "tool_b",
        ToolGraphRelation.DEPENDS_ON,
    )

    with pytest.raises(ValueError, match="edges"):
        ToolGraph(tools=("tool_a", "tool_b"), edges=[edge])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="edges"):
        ToolGraph(tools=("tool_a",), edges=(object(),))  # type: ignore[arg-type]


def test_tool_graph_rejects_unknown_edge_nodes() -> None:
    edge = ToolGraphEdge(
        "tool_a",
        "missing_tool",
        ToolGraphRelation.DEPENDS_ON,
    )

    with pytest.raises(ValueError, match="未知工具"):
        ToolGraph(tools=("tool_a",), edges=(edge,))


def test_tool_graph_rejects_duplicate_directed_and_undirected_edges() -> None:
    dependency = ToolGraphEdge(
        "tool_a",
        "tool_b",
        ToolGraphRelation.DEPENDS_ON,
    )
    parallel = ToolGraphEdge(
        "tool_a",
        "tool_b",
        ToolGraphRelation.PARALLEL_WITH,
    )
    reversed_parallel = ToolGraphEdge(
        "tool_b",
        "tool_a",
        ToolGraphRelation.PARALLEL_WITH,
    )

    with pytest.raises(ValueError, match="不得重复"):
        ToolGraph(
            tools=("tool_a", "tool_b"),
            edges=(dependency, dependency),
        )
    with pytest.raises(ValueError, match="不得重复"):
        ToolGraph(
            tools=("tool_a", "tool_b"),
            edges=(parallel, reversed_parallel),
        )


@pytest.mark.parametrize(
    "edges",
    [
        (
            ToolGraphEdge(
                "tool_a",
                "tool_b",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "tool_b",
                "tool_a",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
        (
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
            ToolGraphEdge(
                "tool_c",
                "tool_a",
                ToolGraphRelation.DEPENDS_ON,
            ),
        ),
    ],
)
def test_tool_graph_rejects_dependency_cycles(
    edges: tuple[ToolGraphEdge, ...],
) -> None:
    with pytest.raises(ValueError, match="循环"):
        ToolGraph(tools=("tool_a", "tool_b", "tool_c"), edges=edges)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (ToolGraphRelation.DEPENDS_ON, ToolGraphRelation.PARALLEL_WITH),
        (ToolGraphRelation.DEPENDS_ON, ToolGraphRelation.CONFLICTS_WITH),
        (ToolGraphRelation.PARALLEL_WITH, ToolGraphRelation.CONFLICTS_WITH),
    ],
)
def test_tool_graph_rejects_contradictory_pair_relations(
    first: ToolGraphRelation,
    second: ToolGraphRelation,
) -> None:
    with pytest.raises(ValueError, match="矛盾"):
        ToolGraph(
            tools=("tool_a", "tool_b"),
            edges=(
                ToolGraphEdge("tool_a", "tool_b", first),
                ToolGraphEdge("tool_a", "tool_b", second),
            ),
        )


@pytest.mark.parametrize(
    "confirmation",
    [
        ["tool_a"],
        {"tool a"},
        {"missing_tool"},
    ],
)
def test_tool_graph_rejects_invalid_confirmation_sets(
    confirmation: object,
) -> None:
    with pytest.raises(ValueError, match="confirmation"):
        ToolGraph(
            tools=("tool_a",),
            requires_confirmation=confirmation,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "capabilities",
    [
        [],
        {"tool a": ("safe.read",)},
        {"missing_tool": ("safe.read",)},
        {"tool_a": ["safe.read"]},
        {"tool_a": ()},
        {"tool_a": ("Safe.read",)},
        {"tool_a": ("safe read",)},
        {"tool_a": ("safe.read", "safe.read")},
    ],
)
def test_tool_graph_rejects_invalid_capability_requirements(
    capabilities: object,
) -> None:
    with pytest.raises(ValueError, match="capability"):
        ToolGraph(
            tools=("tool_a",),
            requires_capability=capabilities,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "method_name",
    [
        "dependencies_for",
        "transitive_dependencies_for",
        "dependents_for",
        "parallel_tools_for",
        "conflicts_for",
        "confirmation_required_for",
        "capabilities_required_for",
    ],
)
def test_tool_graph_queries_reject_unknown_members(method_name: str) -> None:
    graph = ToolGraph(tools=("tool_a",))

    with pytest.raises(ValueError, match="不包含"):
        getattr(graph, method_name)("missing_tool")

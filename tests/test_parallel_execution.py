from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

from nonebot_plugin_moellmchats.agent_runtime import DeadlineContext
from nonebot_plugin_moellmchats.parallel_execution import (
    ReadOnlyParallelExecutionError,
    ReadOnlyParallelExecutionReport,
    ReadOnlyParallelExecutionTimeout,
    ReadOnlyParallelToolExecutor,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect
from nonebot_plugin_moellmchats.tool_graph import (
    ToolGraph,
    ToolGraphEdge,
    ToolGraphRelation,
)
from nonebot_plugin_moellmchats.tool_scheduler import (
    ToolSchedule,
    ToolScheduleBatch,
    ToolScheduleMode,
    ToolSchedulingError,
)


def _parallel_graph(*tools: str) -> ToolGraph:
    edges = tuple(
        ToolGraphEdge(
            tools[left],
            tools[right],
            ToolGraphRelation.PARALLEL_WITH,
        )
        for left in range(len(tools))
        for right in range(left + 1, len(tools))
    )
    return ToolGraph(tools=tuple(tools), edges=edges)


def _effects(*tools: str) -> dict[str, ToolEffect]:
    return dict.fromkeys(tools, ToolEffect.READ_ONLY)


def _deadline(seconds: float = 1.0) -> DeadlineContext:
    return DeadlineContext.from_timeout(seconds)


@pytest.mark.asyncio
async def test_executor_runs_explicit_read_only_clique_concurrently() -> None:
    tools = ("beijing_weather", "shanghai_weather", "guangzhou_weather")
    graph = _parallel_graph(*tools)
    started: set[str] = set()
    release = asyncio.Event()

    def invocation(tool_name: str):
        async def run(dependencies: Mapping[str, object]) -> str:
            assert dict(dependencies) == {}
            with pytest.raises(TypeError):
                dependencies["unexpected"] = True  # type: ignore[index]
            started.add(tool_name)
            if len(started) == len(tools):
                release.set()
            await release.wait()
            return f"result:{tool_name}"

        return run

    report = await ReadOnlyParallelToolExecutor().execute(
        graph=graph,
        selected_tools=tuple(reversed(tools)),
        effects=_effects(*tools),
        invocations={tool: invocation(tool) for tool in tools},
        deadline=_deadline(),
    )

    assert started == set(tools)
    assert report.schedule.as_dict() == {
        "batches": [
            {
                "mode": "parallel",
                "tools": [
                    "beijing_weather",
                    "guangzhou_weather",
                    "shanghai_weather",
                ],
            }
        ]
    }
    assert tuple(report.results) == report.schedule.tools
    assert report.result_for("beijing_weather") == "result:beijing_weather"
    assert report.max_parallelism_observed == 3
    assert report.parallel_batch_count == 1


@pytest.mark.asyncio
async def test_executor_exposes_only_declared_transitive_dependency_results() -> None:
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
    observed: dict[str, tuple[str, ...]] = {}
    middle_started: set[str] = set()
    release_middle = asyncio.Event()

    async def base(dependencies: Mapping[str, object]) -> str:
        observed["base"] = tuple(dependencies)
        return "base-result"

    def middle(tool_name: str):
        async def run(dependencies: Mapping[str, object]) -> str:
            observed[tool_name] = tuple(dependencies)
            assert dependencies["base"] == "base-result"
            middle_started.add(tool_name)
            if len(middle_started) == 2:
                release_middle.set()
            await release_middle.wait()
            return f"{tool_name}-result"

        return run

    async def report(dependencies: Mapping[str, object]) -> str:
        observed["report"] = tuple(dependencies)
        return "/".join(str(value) for value in dependencies.values())

    result = await ReadOnlyParallelToolExecutor().execute(
        graph=graph,
        selected_tools=("report", "south", "base", "north"),
        effects=_effects("base", "north", "south", "report"),
        invocations={
            "base": base,
            "north": middle("north"),
            "south": middle("south"),
            "report": report,
        },
        deadline=_deadline(),
    )

    assert observed == {
        "base": (),
        "north": ("base",),
        "south": ("base",),
        "report": ("base", "north", "south"),
    }
    assert result.result_for("report") == ("base-result/north-result/south-result")


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [ToolEffect.MUTATING])
async def test_executor_rejects_non_read_only_before_any_invocation(
    unsafe: ToolEffect,
) -> None:
    called = False

    async def invoke(_dependencies: Mapping[str, object]) -> None:
        nonlocal called
        called = True

    with pytest.raises(ReadOnlyParallelExecutionError, match=r"只执行.*read_only"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=ToolGraph(tools=("mutate",)),
            selected_tools=("mutate",),
            effects={"mutate": unsafe},
            invocations={"mutate": invoke},
            deadline=_deadline(),
        )

    assert called is False


@pytest.mark.asyncio
async def test_executor_rejects_confirmation_gate_before_any_invocation() -> None:
    called = False

    async def invoke(_dependencies: Mapping[str, object]) -> None:
        nonlocal called
        called = True

    with pytest.raises(ReadOnlyParallelExecutionError, match="需要确认"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=ToolGraph(
                tools=("confirm",),
                requires_confirmation={"confirm"},
            ),
            selected_tools=("confirm",),
            effects=_effects("confirm"),
            invocations={"confirm": invoke},
            deadline=_deadline(),
        )

    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invocations", "message"),
    [
        ({}, "精确覆盖"),
        ({"tool": lambda _dependencies: None}, "async callable"),
    ],
)
async def test_executor_rejects_invalid_invocations_before_start(
    invocations: object,
    message: str,
) -> None:
    with pytest.raises(ReadOnlyParallelExecutionError, match=message):
        await ReadOnlyParallelToolExecutor().execute(
            graph=ToolGraph(tools=("tool",)),
            selected_tools=("tool",),
            effects=_effects("tool"),
            invocations=invocations,  # type: ignore[arg-type]
            deadline=_deadline(),
        )


@pytest.mark.asyncio
async def test_executor_rejects_bad_async_signature_before_start() -> None:
    called = False

    async def bad_signature(
        _dependencies: Mapping[str, object],
        _required: object,
    ) -> None:
        nonlocal called
        called = True

    with pytest.raises(ReadOnlyParallelExecutionError, match="依赖结果映射"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=ToolGraph(tools=("tool",)),
            selected_tools=("tool",),
            effects=_effects("tool"),
            invocations={"tool": bad_signature},  # type: ignore[dict-item]
            deadline=_deadline(),
        )

    assert called is False


@pytest.mark.asyncio
async def test_scheduler_failure_happens_before_invocations() -> None:
    called = False

    async def invoke(_dependencies: Mapping[str, object]) -> None:
        nonlocal called
        called = True

    graph = ToolGraph(
        tools=("tool_a", "tool_b"),
        edges=(
            ToolGraphEdge(
                "tool_a",
                "tool_b",
                ToolGraphRelation.CONFLICTS_WITH,
            ),
        ),
    )
    with pytest.raises(ToolSchedulingError, match="E-08"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=graph,
            selected_tools=("tool_a", "tool_b"),
            effects=_effects("tool_a", "tool_b"),
            invocations={"tool_a": invoke, "tool_b": invoke},
            deadline=_deadline(),
        )

    assert called is False


@pytest.mark.asyncio
async def test_expired_shared_deadline_rejects_before_start() -> None:
    called = False

    async def invoke(_dependencies: Mapping[str, object]) -> None:
        nonlocal called
        called = True

    with pytest.raises(ReadOnlyParallelExecutionTimeout, match="执行前"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=ToolGraph(tools=("tool",)),
            selected_tools=("tool",),
            effects=_effects("tool"),
            invocations={"tool": invoke},
            deadline=DeadlineContext.from_timeout(0),
        )

    assert called is False


@pytest.mark.asyncio
async def test_shared_deadline_cancels_and_drains_the_whole_parallel_batch() -> None:
    tools = ("tool_a", "tool_b")
    started: set[str] = set()
    finalized: set[str] = set()
    never = asyncio.Event()

    def invocation(tool_name: str):
        async def run(_dependencies: Mapping[str, object]) -> None:
            started.add(tool_name)
            try:
                await never.wait()
            finally:
                finalized.add(tool_name)

        return run

    with pytest.raises(ReadOnlyParallelExecutionTimeout, match="共享 deadline"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=_parallel_graph(*tools),
            selected_tools=tools,
            effects=_effects(*tools),
            invocations={tool: invocation(tool) for tool in tools},
            deadline=_deadline(0.02),
        )

    assert started == set(tools)
    assert finalized == set(tools)


@pytest.mark.asyncio
async def test_failure_cancels_siblings_hides_message_and_skips_later_batches() -> None:
    graph = ToolGraph(
        tools=("fail", "peer", "later"),
        edges=(
            ToolGraphEdge(
                "fail",
                "peer",
                ToolGraphRelation.PARALLEL_WITH,
            ),
            ToolGraphEdge("later", "fail", ToolGraphRelation.DEPENDS_ON),
            ToolGraphEdge("later", "peer", ToolGraphRelation.DEPENDS_ON),
        ),
    )
    peer_started = asyncio.Event()
    peer_finalized = asyncio.Event()
    later_called = False

    async def fail(_dependencies: Mapping[str, object]) -> None:
        await peer_started.wait()
        raise RuntimeError("postgresql://secret@example.invalid")

    async def peer(_dependencies: Mapping[str, object]) -> None:
        peer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            peer_finalized.set()

    async def later(_dependencies: Mapping[str, object]) -> None:
        nonlocal later_called
        later_called = True

    with pytest.raises(ReadOnlyParallelExecutionError) as captured:
        await ReadOnlyParallelToolExecutor().execute(
            graph=graph,
            selected_tools=("later", "peer", "fail"),
            effects=_effects("fail", "peer", "later"),
            invocations={"fail": fail, "peer": peer, "later": later},
            deadline=_deadline(),
        )

    assert "fail" in str(captured.value)
    assert "secret" not in str(captured.value)
    assert peer_finalized.is_set()
    assert later_called is False


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_after_children_are_drained() -> None:
    tools = ("tool_a", "tool_b")
    started: set[str] = set()
    finalized: set[str] = set()
    all_started = asyncio.Event()

    def invocation(tool_name: str):
        async def run(_dependencies: Mapping[str, object]) -> None:
            started.add(tool_name)
            if len(started) == len(tools):
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.add(tool_name)

        return run

    execution = asyncio.create_task(
        ReadOnlyParallelToolExecutor().execute(
            graph=_parallel_graph(*tools),
            selected_tools=tools,
            effects=_effects(*tools),
            invocations={tool: invocation(tool) for tool in tools},
            deadline=_deadline(10),
        )
    )
    await asyncio.wait_for(all_started.wait(), timeout=1)
    execution.cancel()

    with pytest.raises(asyncio.CancelledError):
        await execution

    assert finalized == set(tools)


@pytest.mark.asyncio
async def test_child_self_cancellation_becomes_safe_execution_error() -> None:
    async def cancelled(_dependencies: Mapping[str, object]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(ReadOnlyParallelExecutionError, match="非调用方取消"):
        await ReadOnlyParallelToolExecutor().execute(
            graph=ToolGraph(tools=("tool",)),
            selected_tools=("tool",),
            effects=_effects("tool"),
            invocations={"tool": cancelled},
            deadline=_deadline(),
        )


@pytest.mark.asyncio
async def test_empty_selection_needs_no_budget_or_invocation() -> None:
    report = await ReadOnlyParallelToolExecutor().execute(
        graph=ToolGraph(tools=("available",)),
        selected_tools=(),
        effects={},
        invocations={},
        deadline=DeadlineContext.from_timeout(0),
    )

    assert report == ReadOnlyParallelExecutionReport(ToolSchedule(), {})
    assert report.max_parallelism_observed == 0
    assert report.parallel_batch_count == 0


def test_report_is_frozen_and_orders_a_detached_result_mapping() -> None:
    schedule = ToolSchedule(
        (
            ToolScheduleBatch(
                ("tool_b", "tool_a"),
                ToolScheduleMode.PARALLEL,
            ),
        )
    )
    source = {"tool_b": 2, "tool_a": 1}
    report = ReadOnlyParallelExecutionReport(schedule, source)
    source.clear()

    assert tuple(report.results) == ("tool_a", "tool_b")
    assert dict(report.results) == {"tool_a": 1, "tool_b": 2}
    with pytest.raises(TypeError):
        report.results["tool_a"] = 3  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        report.schedule = ToolSchedule()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("schedule", "results", "message"),
    [
        (object(), {}, "schedule"),
        (ToolSchedule(), [], "results"),
        (ToolSchedule(), {"extra": 1}, "精确覆盖"),
    ],
)
def test_report_rejects_invalid_fields(
    schedule: object,
    results: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ReadOnlyParallelExecutionReport(
            schedule=schedule,  # type: ignore[arg-type]
            results=results,  # type: ignore[arg-type]
        )

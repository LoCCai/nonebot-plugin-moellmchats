from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import inspect
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias

from .agent_runtime import DeadlineContext
from .compat import timeout as timeout_scope
from .tool_contracts import ToolEffect
from .tool_scheduler import (
    ReadOnlyParallelToolScheduler,
    ToolSchedule,
    ToolScheduleBatch,
)

if TYPE_CHECKING:
    from .tool_graph import ToolGraph

ReadOnlyToolInvocation: TypeAlias = Callable[
    [Mapping[str, Any]],
    Awaitable[Any],
]


class ReadOnlyParallelExecutionError(RuntimeError):
    """A read-only execution plan or invocation failed safely."""


class ReadOnlyParallelExecutionTimeout(ReadOnlyParallelExecutionError):
    """The one shared Agent deadline expired before execution completed."""


class _InvocationFailed(RuntimeError):
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(tool_name)


class _InvocationCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadOnlyParallelExecutionReport:
    """One completed schedule with results ordered by the trusted plan."""

    schedule: ToolSchedule
    results: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.schedule, ToolSchedule):
            raise ValueError("ReadOnlyParallelExecutionReport.schedule 必须是 ToolSchedule")
        if not isinstance(self.results, Mapping):
            raise ValueError("ReadOnlyParallelExecutionReport.results 必须是结果映射")
        snapshot = dict(self.results)
        if set(snapshot) != set(self.schedule.tools):
            raise ValueError("ReadOnlyParallelExecutionReport.results 必须精确覆盖 schedule")
        ordered = {tool: snapshot[tool] for tool in self.schedule.tools}
        object.__setattr__(self, "results", MappingProxyType(ordered))

    @property
    def max_parallelism_observed(self) -> int:
        return max((len(batch.tools) for batch in self.schedule.batches), default=0)

    @property
    def parallel_batch_count(self) -> int:
        return sum(batch.is_parallel for batch in self.schedule.batches)

    def result_for(self, tool_name: str) -> Any:
        return self.results[tool_name]


@dataclass(frozen=True)
class ReadOnlyParallelToolExecutor:
    """Execute a freshly planned set of cancellable read-only invocations.

    Invocation factories are runtime ports that have already passed trust and
    capability authorization.  This coordinator never grants a capability and
    deliberately does not connect itself to the live chat/tool runtime.
    """

    max_parallelism: int = 4

    def __post_init__(self) -> None:
        # Reuse the scheduler's single canonical bound and bool rejection.
        ReadOnlyParallelToolScheduler(max_parallelism=self.max_parallelism)

    async def execute(
        self,
        *,
        graph: ToolGraph,
        selected_tools: tuple[str, ...],
        effects: Mapping[str, ToolEffect],
        invocations: Mapping[str, ReadOnlyToolInvocation],
        deadline: DeadlineContext,
    ) -> ReadOnlyParallelExecutionReport:
        effect_snapshot = dict(effects) if isinstance(effects, Mapping) else effects
        schedule = ReadOnlyParallelToolScheduler(max_parallelism=self.max_parallelism).plan(
            graph=graph,
            selected_tools=selected_tools,
            effects=effect_snapshot,
        )
        if not isinstance(deadline, DeadlineContext):
            raise ReadOnlyParallelExecutionError("parallel executor deadline 必须是 DeadlineContext")

        unsafe_tools = tuple(tool for tool in schedule.tools if effect_snapshot[tool] is not ToolEffect.READ_ONLY)
        if unsafe_tools:
            raise ReadOnlyParallelExecutionError("parallel executor 只执行强类型 read_only 工具")
        confirmation_tools = tuple(tool for tool in schedule.tools if graph.confirmation_required_for(tool))
        if confirmation_tools:
            raise ReadOnlyParallelExecutionError("parallel executor 不执行需要确认的工具")

        invocation_snapshot = self._validate_invocations(
            schedule,
            invocations,
        )
        if not schedule.tools:
            return ReadOnlyParallelExecutionReport(schedule, {})

        remaining = deadline.remaining()
        if remaining <= 0:
            raise ReadOnlyParallelExecutionTimeout("read_only 工具执行前共享 deadline 已耗尽")

        results: dict[str, Any] = {}
        try:
            async with timeout_scope(remaining):
                for batch in schedule.batches:
                    batch_results = await self._execute_batch(
                        graph=graph,
                        batch=batch,
                        invocations=invocation_snapshot,
                        completed=results,
                    )
                    results.update(batch_results)
        except _InvocationFailed as error:
            raise ReadOnlyParallelExecutionError(f"read_only 工具 {error.tool_name} 执行失败") from None
        except _InvocationCancelled:
            raise ReadOnlyParallelExecutionError("read_only 工具任务发生非调用方取消") from None
        except TimeoutError:
            raise ReadOnlyParallelExecutionTimeout("read_only 工具执行耗尽共享 deadline") from None
        except asyncio.CancelledError:
            raise

        return ReadOnlyParallelExecutionReport(schedule, results)

    @staticmethod
    def _validate_invocations(
        schedule: ToolSchedule,
        invocations: Mapping[str, ReadOnlyToolInvocation],
    ) -> dict[str, ReadOnlyToolInvocation]:
        if not isinstance(invocations, Mapping):
            raise ReadOnlyParallelExecutionError("parallel executor invocations 必须是工具映射")
        snapshot = dict(invocations)
        if set(snapshot) != set(schedule.tools):
            raise ReadOnlyParallelExecutionError("parallel executor invocations 必须精确覆盖 schedule")
        probe = MappingProxyType({})
        for tool_name in schedule.tools:
            invocation = snapshot[tool_name]
            async_callable = inspect.iscoroutinefunction(invocation) or (
                callable(invocation) and inspect.iscoroutinefunction(getattr(invocation, "__call__", None))
            )
            if not async_callable:
                raise ReadOnlyParallelExecutionError(f"read_only 工具 {tool_name} invocation 必须是 async callable")
            try:
                inspect.signature(invocation).bind(probe)
            except (TypeError, ValueError):
                raise ReadOnlyParallelExecutionError(f"read_only 工具 {tool_name} invocation 必须只需依赖结果映射") from None
        return snapshot

    @staticmethod
    def _dependency_results(
        graph: ToolGraph,
        tool_name: str,
        completed: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        dependency_names = graph.transitive_dependencies_for(tool_name)
        if not all(name in completed for name in dependency_names):
            raise ReadOnlyParallelExecutionError(f"read_only 工具 {tool_name} 的依赖结果不完整")
        return MappingProxyType({name: completed[name] for name in dependency_names})

    @staticmethod
    async def _invoke(
        tool_name: str,
        invocation: ReadOnlyToolInvocation,
        dependencies: Mapping[str, Any],
    ) -> Any:
        try:
            return await invocation(dependencies)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The runtime boundary may log the original exception privately.
            # Do not copy a handler message (which may contain secrets) into the
            # public coordinator error.
            raise _InvocationFailed(tool_name) from None

    async def _execute_batch(
        self,
        *,
        graph: ToolGraph,
        batch: ToolScheduleBatch,
        invocations: Mapping[str, ReadOnlyToolInvocation],
        completed: Mapping[str, Any],
    ) -> dict[str, Any]:
        tasks: list[tuple[str, asyncio.Task[Any]]] = []
        for tool_name in batch.tools:
            dependencies = self._dependency_results(
                graph,
                tool_name,
                completed,
            )
            task = asyncio.create_task(
                self._invoke(
                    tool_name,
                    invocations[tool_name],
                    dependencies,
                )
            )
            tasks.append((tool_name, task))

        pending = {task for _, task in tasks}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for _, task in tasks:
                    if task not in done:
                        continue
                    if task.cancelled():
                        raise _InvocationCancelled
                    error = task.exception()
                    if error is not None:
                        raise error
        except BaseException:
            for _, task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for _, task in tasks),
                return_exceptions=True,
            )
            raise

        return {tool_name: task.result() for tool_name, task in tasks}

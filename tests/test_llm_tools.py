from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message
import pytest

from nonebot_plugin_moellmchats.agent_context_runtime import (
    AgentGenerationCoordinator,
    AgentRequestIdentity,
    AgentRequestRuntime,
)
from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRunState,
    AgentStep,
    DeadlineContext,
    ToolCall,
    ToolCallStatus,
)
from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.event_simulator import (
    PluginDispatchResult,
    PluginDispatchStatus,
)
from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.pending_actions import (
    PendingActionStore,
    pending_action_store,
)
from nonebot_plugin_moellmchats.protocol_context import protocol_request_scope
from nonebot_plugin_moellmchats.redis_client import (
    RedisClientManager,
    RedisClientSettings,
)
from nonebot_plugin_moellmchats.redis_pending_actions import (
    RedisPendingActionSettings,
)
from nonebot_plugin_moellmchats.runtime_resources import (
    RuntimeGenerationResources,
    RuntimeResourceBuilder,
    RuntimeResourceSettings,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolContext,
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolResultCitation,
    ToolResultFile,
    ToolSpec,
)
from nonebot_plugin_moellmchats.tool_graph import (
    ToolGraph,
    ToolGraphEdge,
    ToolGraphRelation,
)
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderRegistration,
    builtin_tool_provider,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
    nonebot_plugin_provider,
    registered_tool_provider,
)


class FakeBot:
    config = SimpleNamespace(superusers={"1"})

    def __init__(self) -> None:
        self.sent = []

    async def send(self, event, message):
        self.sent.append(str(message))


class Harness(LlmToolsMixin):
    def __init__(
        self,
        tools: dict,
        text: str = "hello",
        *,
        plugins: dict | None = None,
        snapshot: ToolSnapshot | None = None,
    ) -> None:
        self.bot = FakeBot()
        self.event = SimpleNamespace(user_id=1)
        self.format_message_dict = {"text": [text]}
        self.tool_snapshot = snapshot or ToolSnapshot(
            generation=1,
            custom_tools=tools,
            plugin_info=plugins or {},
            tool_dependencies={},
            mcp_tool_names=set(),
        )
        self.messages_handler = SimpleNamespace(
            messages_entity=SimpleNamespace(
                add_used_plugins=lambda value: None,
                tool_messages=[],
            )
        )
        self._pending_vision_images = []
        self._current_tool_usage = Counter()
        self.emotion_flag = False
        self.is_superuser = True
        self.agent_runtime: AgentRequestRuntime | None = None
        self.sent = []

    async def send_emotion_message(self, content: str) -> str:
        self.sent.append(content)
        return content

    def _sanitize_tool_calls_for_history(self, tool_calls):
        return tool_calls


def _call(identifier: int, name: str, arguments: str = "{}") -> dict:
    return {
        "id": str(identifier),
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


async def _agent_request_runtime() -> AgentRequestRuntime:
    snapshot = RuntimeSnapshot(
        generation=1,
        config={},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=1,
    )
    resources = RuntimeResourceBuilder().build(snapshot)
    runtime = await AgentRequestRuntime.begin(
        AgentGenerationCoordinator(resources),
        AgentRequestIdentity(
            platform="onebot-v11",
            platform_user_id="1",
            group_id=None,
            display_name="tester",
        ),
        request_id=1,
        deadline=DeadlineContext.from_timeout(30),
    )
    await runtime.advance(AgentRunState.PLANNING, model="model")
    await runtime.advance(AgentRunState.EXECUTING)
    return runtime


async def _parallel_agent_request_runtime(
    tool_snapshot: ToolSnapshot,
    graph: ToolGraph,
    *,
    runner_tools: tuple[str, ...],
) -> tuple[AgentRequestRuntime, RuntimeGenerationResources]:
    snapshot = RuntimeSnapshot(
        generation=tool_snapshot.generation,
        config={"request_timeout_seconds": 30},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=tool_snapshot,
        emotions=(),
        reloaded_at=1,
    )
    resources = RuntimeResourceBuilder(
        RuntimeResourceSettings(
            trusted_runner_tools=runner_tools,
            parallel_tool_graph=graph,
        )
    ).build(snapshot)
    await resources.start()
    runtime = await AgentRequestRuntime.begin(
        AgentGenerationCoordinator(resources),
        AgentRequestIdentity(
            platform="onebot-v11",
            platform_user_id="1",
            group_id=None,
            display_name="tester",
        ),
        request_id=1,
        deadline=DeadlineContext.from_timeout(30),
    )
    await runtime.advance(AgentRunState.PLANNING, model="model")
    await runtime.advance(AgentRunState.EXECUTING)
    return runtime, resources


def _complete_catalog(
    generation: int,
    *,
    registered: tuple[ToolSpec, ...] = (),
    nonebot_plugins: tuple[ToolSpec, ...] = (),
) -> ProviderCatalogSnapshot:
    providers = (
        registered_tool_provider,
        file_tool_provider,
        generated_tool_provider,
        mcp_tool_provider,
        builtin_tool_provider,
        nonebot_plugin_provider,
    )
    registrations = {
        registration.provider_id: registration
        for registration in (
            ProviderRegistration.from_provider(provider)
            for provider in providers
        )
    }
    records: dict[str, DiscoveredTool] = {}
    for provider, specs in (
        (registered_tool_provider, registered),
        (builtin_tool_provider, builtin_tool_specs()),
        (nonebot_plugin_provider, nonebot_plugins),
    ):
        for spec in specs:
            records[spec.name] = DiscoveredTool(
                provider_id=provider.provider_id,
                source=provider.source,
                trust=provider.trust,
                generation=generation,
                spec=spec,
            )
    return ProviderCatalogSnapshot(
        generation=generation,
        registrations=registrations,
        tools=records,
    )


def _registered_tool_snapshot(
    generation: int,
    specs: tuple[ToolSpec, ...],
) -> ToolSnapshot:
    return ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={spec.name: {**spec.as_legacy_schema(), "source": "registered"} for spec in specs},
        tool_dependencies={spec.name: set(spec.dependencies) for spec in specs},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(generation, registered=specs),
    )


def _parallel_graph(*tool_names: str) -> ToolGraph:
    edges = tuple(
        ToolGraphEdge(
            first,
            second,
            ToolGraphRelation.PARALLEL_WITH,
        )
        for index, first in enumerate(tool_names)
        for second in tool_names[index + 1 :]
    )
    return ToolGraph(tools=tuple(tool_names), edges=edges)


@pytest.mark.asyncio
async def test_only_one_tool_executes_each_round() -> None:
    calls = Counter()

    async def first():
        calls["first"] += 1
        return "first"

    async def second():
        calls["second"] += 1
        return "second"

    harness = Harness(
        {
            "first": {"func": first},
            "second": {"func": second},
        }
    )
    messages = await harness._execute_tools(
        [_call(1, "first"), _call(2, "second")], "", [], ""
    )
    assert calls == {"first": 1}
    assert "已跳过" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_explicit_trusted_read_only_graph_uses_real_parallel_path() -> None:
    active = 0
    max_active = 0
    entered: set[str] = set()
    both_entered = asyncio.Event()

    def build_handler(name: str):
        async def handler() -> ToolResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            entered.add(name)
            if len(entered) == 2:
                both_entered.set()
            try:
                await both_entered.wait()
                return ToolResult(
                    text=f"{name}:" + "x" * 20,
                    images=tuple(f"image:{name}:{index}" for index in range(3)),
                )
            finally:
                active -= 1

        return handler

    specs = tuple(
        ToolSpec(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=build_handler(name),
            result_limit=8,
        )
        for name in ("first", "second")
    )
    tool_snapshot = _registered_tool_snapshot(10, specs)
    graph = _parallel_graph("first", "second")
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        graph,
        runner_tools=("first", "second"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await asyncio.wait_for(
            harness._execute_tools(
                [_call(1, "second"), _call(2, "first")],
                "",
                [],
                "",
            ),
            timeout=2,
        )

        assert max_active == 2
        assert entered == {"first", "second"}
        assert [message["tool_call_id"] for message in messages[1:]] == [
            "1",
            "2",
        ]
        assert all("工具结果已截断" in message["content"] for message in messages[1:])
        assert len(harness._pending_vision_images) == 4
        assert {call.tool_name for call in runtime.tool_calls} == {
            "first",
            "second",
        }
        assert {call.status for call in runtime.tool_calls} == {ToolCallStatus.COMPLETED}
        assert resources.trusted_runner is not None
        runner_state = resources.trusted_runner.snapshot()
        assert runner_state.completed == 2
        assert runner_state.active == 0
        assert runner_state.pending == 0
        assert harness.messages_handler.messages_entity.tool_messages[1]["tool_call_id"] == "1"
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_parallel_trace_persistence_is_serialized_with_unique_step_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: set[str] = set()
    both_entered = asyncio.Event()

    def build_handler(name: str):
        async def handler() -> str:
            entered.add(name)
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()
            return name

        return handler

    specs = tuple(
        ToolSpec(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=build_handler(name),
        )
        for name in ("first", "second")
    )
    tool_snapshot = _registered_tool_snapshot(18, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        _parallel_graph("first", "second"),
        runner_tools=("first", "second"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime
    original_persist_step = runtime.coordinator.persist_step
    persistence_active = 0
    max_persistence_active = 0
    observed_indexes: list[int] = []

    async def observed_persist_step(
        step: AgentStep,
        *,
        call: ToolCall | None = None,
        actor_user_id: str,
        created_at: datetime,
    ):
        nonlocal persistence_active, max_persistence_active
        persistence_active += 1
        max_persistence_active = max(max_persistence_active, persistence_active)
        observed_indexes.append(step.index)
        try:
            await asyncio.sleep(0.01)
            return await original_persist_step(
                step,
                call=call,
                actor_user_id=actor_user_id,
                created_at=created_at,
            )
        finally:
            persistence_active -= 1

    monkeypatch.setattr(runtime.coordinator, "persist_step", observed_persist_step)

    try:
        await asyncio.wait_for(
            harness._execute_tools(
                [_call(1, "first"), _call(2, "second")],
                "",
                [],
                "",
            ),
            timeout=2,
        )
        assert max_persistence_active == 1
        assert len(observed_indexes) == 2
        assert len(set(observed_indexes)) == 2
        assert observed_indexes[1] == observed_indexes[0] + 1
        assert [step.index for step in runtime.steps] == observed_indexes
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_parallel_critical_trace_failure_propagates_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence_failed = asyncio.Event()
    peer_drained = asyncio.Event()

    async def first() -> str:
        return "first"

    async def peer() -> str:
        await persistence_failed.wait()
        try:
            await asyncio.Event().wait()
        finally:
            peer_drained.set()
        return "unreachable"

    specs = (
        ToolSpec(
            name="first",
            description="first",
            parameters={"type": "object", "properties": {}},
            handler=first,
        ),
        ToolSpec(
            name="peer",
            description="peer",
            parameters={"type": "object", "properties": {}},
            handler=peer,
        ),
    )
    tool_snapshot = _registered_tool_snapshot(19, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        _parallel_graph("first", "peer"),
        runner_tools=("first", "peer"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime
    original_persist_step = runtime.coordinator.persist_step
    persistence_attempts: Counter[str] = Counter()

    async def failing_persist_step(
        step: AgentStep,
        *,
        call: ToolCall | None = None,
        actor_user_id: str,
        created_at: datetime,
    ):
        persistence_attempts[step.tool or "unknown"] += 1
        if step.tool == "first":
            persistence_failed.set()
            raise RuntimeError("critical persistence failure")
        return await original_persist_step(
            step,
            call=call,
            actor_user_id=actor_user_id,
            created_at=created_at,
        )

    monkeypatch.setattr(runtime.coordinator, "persist_step", failing_persist_step)
    messages: list[dict] = []

    try:
        with pytest.raises(RuntimeError, match="trace 持久化失败"):
            await asyncio.wait_for(
                harness._execute_tools(
                    [_call(1, "first"), _call(2, "peer")],
                    "",
                    messages,
                    "",
                ),
                timeout=2,
            )
        assert persistence_attempts["first"] == 1
        assert peer_drained.is_set()
        assert not any(message.get("role") == "tool" for message in messages)
        assert "first" not in {call.tool_name for call in runtime.tool_calls}
        assert resources.trusted_runner is not None
        runner_state = resources.trusted_runner.snapshot()
        assert runner_state.active == 0
        assert runner_state.pending == 0
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_parallel_runtime_honors_complete_typed_dependency_dag() -> None:
    root_names: set[str] = set()
    roots_entered = asyncio.Event()
    base_completed = asyncio.Event()
    execution_order: list[str] = []

    async def base() -> str:
        root_names.add("base")
        if len(root_names) == 2:
            roots_entered.set()
        await roots_entered.wait()
        execution_order.append("base")
        base_completed.set()
        return "base"

    async def peer() -> str:
        root_names.add("peer")
        if len(root_names) == 2:
            roots_entered.set()
        await roots_entered.wait()
        execution_order.append("peer")
        return "peer"

    async def dependent() -> str:
        assert base_completed.is_set()
        execution_order.append("dependent")
        return "dependent"

    specs = (
        ToolSpec(
            name="base",
            description="base",
            parameters={"type": "object", "properties": {}},
            handler=base,
        ),
        ToolSpec(
            name="peer",
            description="peer",
            parameters={"type": "object", "properties": {}},
            handler=peer,
        ),
        ToolSpec(
            name="dependent",
            description="dependent",
            parameters={"type": "object", "properties": {}},
            handler=dependent,
            dependencies=("base",),
        ),
    )
    graph = ToolGraph(
        tools=("base", "peer", "dependent"),
        edges=(
            ToolGraphEdge(
                "dependent",
                "base",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "base",
                "peer",
                ToolGraphRelation.PARALLEL_WITH,
            ),
        ),
    )
    tool_snapshot = _registered_tool_snapshot(15, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        graph,
        runner_tools=("base", "peer", "dependent"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await asyncio.wait_for(
            harness._execute_tools(
                [
                    _call(1, "dependent"),
                    _call(2, "peer"),
                    _call(3, "base"),
                ],
                "",
                [],
                "",
            ),
            timeout=2,
        )
        assert execution_order[-1] == "dependent"
        assert set(execution_order[:2]) == {"base", "peer"}
        assert [message["tool_call_id"] for message in messages[1:]] == [
            "1",
            "2",
            "3",
        ]
        assert {call.status for call in runtime.tool_calls} == {ToolCallStatus.COMPLETED}
        assert resources.trusted_runner is not None
        assert resources.trusted_runner.snapshot().completed == 3
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_missing_dependency_closure_never_enters_parallel_runner() -> None:
    calls = Counter()

    def build_handler(name: str):
        async def handler() -> str:
            calls[name] += 1
            return name

        return handler

    specs = (
        ToolSpec(
            name="base",
            description="base",
            parameters={"type": "object", "properties": {}},
            handler=build_handler("base"),
        ),
        ToolSpec(
            name="dependent",
            description="dependent",
            parameters={"type": "object", "properties": {}},
            handler=build_handler("dependent"),
            dependencies=("base",),
        ),
        ToolSpec(
            name="peer",
            description="peer",
            parameters={"type": "object", "properties": {}},
            handler=build_handler("peer"),
        ),
    )
    graph = ToolGraph(
        tools=("base", "dependent", "peer"),
        edges=(
            ToolGraphEdge(
                "dependent",
                "base",
                ToolGraphRelation.DEPENDS_ON,
            ),
            ToolGraphEdge(
                "dependent",
                "peer",
                ToolGraphRelation.PARALLEL_WITH,
            ),
        ),
    )
    tool_snapshot = _registered_tool_snapshot(16, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        graph,
        runner_tools=("base", "dependent", "peer"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await harness._execute_tools(
            [_call(1, "dependent"), _call(2, "peer")],
            "",
            [],
            "",
        )
        assert calls == {"dependent": 1}
        assert "已跳过" in messages[-1]["content"]
        assert resources.trusted_runner is not None
        assert resources.trusted_runner.snapshot().completed == 0
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_mutating_call_in_mixed_batch_stays_on_pending_action_path() -> None:
    await pending_action_store.clear()
    executions = Counter()

    async def mutate() -> str:
        executions["mutate"] += 1
        return "changed"

    async def safe() -> str:
        executions["safe"] += 1
        return "safe"

    mutating_spec = ToolSpec(
        name="mutate",
        description="mutate",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    safe_spec = ToolSpec(
        name="safe",
        description="safe",
        parameters={"type": "object", "properties": {}},
        handler=safe,
    )
    tool_snapshot = _registered_tool_snapshot(
        17,
        (mutating_spec, safe_spec),
    )
    graph = _parallel_graph("mutate", "safe")
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        graph,
        runner_tools=("safe",),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await harness._execute_tools(
            [_call(1, "mutate"), _call(2, "safe")],
            "",
            [],
            "",
        )
        assert executions == {}
        assert "尚未执行" in messages[-2]["content"]
        assert "已跳过" in messages[-1]["content"]
        assert [call.status for call in runtime.tool_calls] == [
            ToolCallStatus.WAITING_CONFIRMATION,
            ToolCallStatus.REJECTED,
        ]
        assert resources.trusted_runner is not None
        assert resources.trusted_runner.snapshot().completed == 0
    finally:
        await pending_action_store.clear()
        await resources.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "graph",
    [
        ToolGraph(
            tools=("first", "second"),
            edges=(
                ToolGraphEdge(
                    "first",
                    "second",
                    ToolGraphRelation.CONFLICTS_WITH,
                ),
            ),
        ),
        ToolGraph(
            tools=("first", "second"),
            edges=(
                ToolGraphEdge(
                    "first",
                    "second",
                    ToolGraphRelation.PARALLEL_WITH,
                ),
            ),
            requires_confirmation={"first"},
        ),
        ToolGraph(
            tools=("first", "second"),
            edges=(
                ToolGraphEdge(
                    "first",
                    "second",
                    ToolGraphRelation.PARALLEL_WITH,
                ),
            ),
            requires_capability={"first": ("network.read",)},
        ),
    ],
    ids=("conflict", "confirmation", "capability"),
)
async def test_unsafe_graph_batch_keeps_existing_serial_and_reject_semantics(
    graph: ToolGraph,
) -> None:
    calls = Counter()

    def build_handler(name: str):
        async def handler() -> str:
            calls[name] += 1
            return name

        return handler

    specs = tuple(
        ToolSpec(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=build_handler(name),
        )
        for name in ("first", "second")
    )
    tool_snapshot = _registered_tool_snapshot(11, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        graph,
        runner_tools=("first", "second"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await harness._execute_tools(
            [_call(1, "first"), _call(2, "second")],
            "",
            [],
            "",
        )
        assert calls == {"first": 1}
        assert "已跳过" in messages[-1]["content"]
        assert [call.status for call in runtime.tool_calls] == [
            ToolCallStatus.COMPLETED,
            ToolCallStatus.REJECTED,
        ]
        assert resources.trusted_runner is not None
        assert resources.trusted_runner.snapshot().completed == 0
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_non_allowlisted_or_duplicate_tools_never_enter_parallel_runner() -> None:
    calls = Counter()

    def build_handler(name: str):
        async def handler() -> str:
            calls[name] += 1
            return name

        return handler

    specs = tuple(
        ToolSpec(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=build_handler(name),
        )
        for name in ("first", "second")
    )
    tool_snapshot = _registered_tool_snapshot(12, specs)
    graph = _parallel_graph("first", "second")
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        graph,
        runner_tools=("first",),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await harness._execute_tools(
            [_call(1, "first"), _call(2, "second")],
            "",
            [],
            "",
        )
        assert calls == {"first": 1}
        assert "已跳过" in messages[-1]["content"]
        assert resources.trusted_runner is not None
        assert resources.trusted_runner.snapshot().completed == 0

        duplicate_messages = await harness._execute_tools(
            [_call(3, "first"), _call(4, "first")],
            "",
            [],
            "",
        )
        assert calls == {"first": 2}
        assert "已跳过" in duplicate_messages[-1]["content"]
        assert resources.trusted_runner.snapshot().completed == 0
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_parallel_first_error_cancels_and_drains_sibling_without_leak() -> None:
    entered: set[str] = set()
    both_entered = asyncio.Event()
    peer_drained = asyncio.Event()
    failing_attempts = 0

    async def failing() -> str:
        nonlocal failing_attempts
        failing_attempts += 1
        entered.add("failing")
        if len(entered) == 2:
            both_entered.set()
        await both_entered.wait()
        raise RuntimeError("private parallel credential")

    async def peer() -> str:
        entered.add("peer")
        if len(entered) == 2:
            both_entered.set()
        try:
            await both_entered.wait()
            await asyncio.Event().wait()
        finally:
            peer_drained.set()
        return "unreachable"

    specs = (
        ToolSpec(
            name="failing",
            description="failing",
            parameters={"type": "object", "properties": {}},
            handler=failing,
        ),
        ToolSpec(
            name="peer",
            description="peer",
            parameters={"type": "object", "properties": {}},
            handler=peer,
        ),
    )
    tool_snapshot = _registered_tool_snapshot(13, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        _parallel_graph("failing", "peer"),
        runner_tools=("failing", "peer"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await asyncio.wait_for(
            harness._execute_tools(
                [_call(1, "failing"), _call(2, "peer")],
                "",
                [],
                "",
            ),
            timeout=2,
        )
        assert peer_drained.is_set()
        statuses = {call.tool_name: call.status for call in runtime.tool_calls}
        assert statuses == {
            "failing": ToolCallStatus.FAILED,
            "peer": ToolCallStatus.CANCELLED,
        }
        assert "private parallel credential" not in str(messages)
        assert resources.trusted_runner is not None
        runner_state = resources.trusted_runner.snapshot()
        assert runner_state.active == 0
        assert runner_state.pending == 0
        assert runner_state.failed == 1
        assert runner_state.cancelled == 1

        retried = await harness._execute_tools(
            [_call(3, "failing"), _call(4, "peer")],
            "",
            [],
            "",
        )
        assert failing_attempts == 1
        assert "禁止原样重复" in retried[-2]["content"]
        assert "已跳过" in retried[-1]["content"]
        assert resources.trusted_runner.snapshot().failed == 1
    finally:
        await resources.close()


@pytest.mark.asyncio
async def test_parallel_caller_cancellation_propagates_after_full_drain() -> None:
    entered: set[str] = set()
    both_entered = asyncio.Event()
    drained: set[str] = set()

    def build_handler(name: str):
        async def handler() -> str:
            entered.add(name)
            if len(entered) == 2:
                both_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.add(name)
            return "unreachable"

        return handler

    specs = tuple(
        ToolSpec(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=build_handler(name),
        )
        for name in ("first", "second")
    )
    tool_snapshot = _registered_tool_snapshot(14, specs)
    runtime, resources = await _parallel_agent_request_runtime(
        tool_snapshot,
        _parallel_graph("first", "second"),
        runner_tools=("first", "second"),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime
    task = asyncio.create_task(
        harness._execute_tools(
            [_call(1, "first"), _call(2, "second")],
            "",
            [],
            "",
        )
    )

    try:
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained == {"first", "second"}
        assert {call.status for call in runtime.tool_calls} == {ToolCallStatus.CANCELLED}
        assert resources.trusted_runner is not None
        runner_state = resources.trusted_runner.snapshot()
        assert runner_state.active == 0
        assert runner_state.pending == 0
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await resources.close()


@pytest.mark.asyncio
async def test_real_tool_path_records_completed_rejected_and_round_limit_calls() -> None:
    async def tool() -> str:
        return "ok"

    first = ToolSpec(
        name="first",
        description="first",
        parameters={"type": "object", "properties": {}},
        handler=tool,
    ).as_legacy_schema()
    first["source"] = "registered"
    second = ToolSpec(
        name="second",
        description="second",
        parameters={"type": "object", "properties": {}},
        handler=tool,
    ).as_legacy_schema()
    second["source"] = "registered"
    harness = Harness({"first": first, "second": second})
    harness.agent_runtime = await _agent_request_runtime()
    runtime = harness.agent_runtime
    assert runtime is not None

    await harness._execute_tools(
        [_call(1, "first"), _call(2, "second")],
        "",
        [],
        "",
    )
    await harness._execute_tools(
        [_call(3, "first", "[]")],
        "",
        [],
        "",
    )

    assert [call.status for call in runtime.tool_calls] == [
        ToolCallStatus.COMPLETED,
        ToolCallStatus.REJECTED,
        ToolCallStatus.REJECTED,
    ]
    assert [step.status.value for step in runtime.steps] == [
        "completed",
        "skipped",
        "skipped",
    ]
    assert all(
        call.tool_source.value == "registered"
        for call in runtime.tool_calls
    )


@pytest.mark.asyncio
async def test_custom_tool_timeout_records_timed_out_trace() -> None:
    async def slow_tool() -> str:
        await asyncio.sleep(1)
        return "late"

    spec = ToolSpec(
        name="slow_tool",
        description="slow tool",
        parameters={"type": "object", "properties": {}},
        handler=slow_tool,
        timeout_seconds=0.01,
    )
    harness = Harness(
        {
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
        }
    )
    harness.agent_runtime = await _agent_request_runtime()

    messages = await harness._execute_tools(
        [_call(1, spec.name)],
        "",
        [],
        "",
    )

    runtime = harness.agent_runtime
    assert runtime is not None
    assert runtime.tool_calls[-1].status is ToolCallStatus.TIMED_OUT
    assert runtime.steps[-1].status.value == "timed_out"
    assert "超时" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_custom_tool_failure_records_failed_trace_without_secret_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module

    async def failing_tool() -> str:
        raise RuntimeError("private tool credential")

    spec = ToolSpec(
        name="failing_tool",
        description="failing tool",
        parameters={"type": "object", "properties": {}},
        handler=failing_tool,
    )
    harness = Harness(
        {
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
        }
    )
    harness.agent_runtime = await _agent_request_runtime()
    logs: list[str] = []
    monkeypatch.setattr(
        module.logger,
        "error",
        lambda message: logs.append(str(message)),
    )

    messages = await harness._execute_tools(
        [_call(1, spec.name)],
        "",
        [],
        "",
    )

    runtime = harness.agent_runtime
    assert runtime is not None
    assert runtime.tool_calls[-1].status is ToolCallStatus.FAILED
    assert runtime.steps[-1].status.value == "failed"
    assert "private tool credential" not in messages[-1]["content"]
    assert logs == ["自定义工具执行失败，异常详情已安全省略"]


@pytest.mark.asyncio
async def test_custom_tool_cancellation_records_cancelled_trace_and_propagates() -> None:
    entered = asyncio.Event()

    async def cancellable_tool() -> str:
        entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    spec = ToolSpec(
        name="cancellable_tool",
        description="cancellable tool",
        parameters={"type": "object", "properties": {}},
        handler=cancellable_tool,
    )
    harness = Harness(
        {
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
        }
    )
    harness.agent_runtime = await _agent_request_runtime()
    task = asyncio.create_task(
        harness._execute_tools(
            [_call(1, spec.name)],
            "",
            [],
            "",
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    runtime = harness.agent_runtime
    assert runtime is not None
    assert runtime.tool_calls[-1].status is ToolCallStatus.CANCELLED
    assert runtime.steps[-1].status.value == "cancelled"


@pytest.mark.asyncio
async def test_repeated_tool_limit_prevents_third_execution() -> None:
    executions = 0

    async def tool():
        nonlocal executions
        executions += 1
        return "ok"

    harness = Harness({"tool": {"func": tool}})
    messages: list[dict] = []
    for index in range(3):
        messages = await harness._execute_tools([_call(index, "tool")], "", [], "")
    assert executions == 2
    assert "重复调用上限" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_mutating_tool_always_requires_separate_nonce_confirmation() -> None:
    await pending_action_store.clear()
    executions = []

    async def mutate(_tool_context: ToolContext | None = None):
        assert _tool_context is not None
        executions.append(_tool_context.confirmed)
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    harness = Harness(
        {"mutate": spec.as_legacy_schema()},
        "不要确认执行，也不要改任何东西",
    )
    messages = await harness._execute_tools(
        [_call(1, "mutate", '{"confirm": true}')], "", [], ""
    )
    assert executions == []
    assert "尚未执行" in messages[-1]["content"]
    assert "确认执行" in messages[-1]["content"]

    harness = Harness({"mutate": spec.as_legacy_schema()}, "确认执行")
    messages = await harness._execute_tools(
        [_call(2, "mutate", '{"confirm": true}')], "", [], ""
    )
    assert executions == []
    assert "尚未执行" in messages[-1]["content"]
    await pending_action_store.clear()


@pytest.mark.asyncio
async def test_generation_redis_pending_failure_rejects_without_memory_fallback() -> None:
    await pending_action_store.clear()
    executions = 0
    client_calls = 0

    async def mutate() -> str:
        nonlocal executions
        executions += 1
        return "changed"

    def client_factory(*_args: object, **_kwargs: object):
        nonlocal client_calls
        client_calls += 1
        raise RuntimeError("redis://user:private-secret@redis.invalid/0")

    snapshot = RuntimeSnapshot(
        generation=1,
        config={},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=1,
    )
    resources = RuntimeResourceBuilder(
        RuntimeResourceSettings(
            redis=RedisClientSettings(
                redis_url="redis://user:private-secret@redis.invalid/0"
            ),
            redis_pending_actions=RedisPendingActionSettings(),
        ),
        redis_manager_factory=lambda settings: RedisClientManager(
            settings,
            client_factory=client_factory,
        ),
    ).build(snapshot)
    runtime = await AgentRequestRuntime.begin(
        AgentGenerationCoordinator(resources),
        AgentRequestIdentity(
            platform="onebot-v11",
            platform_user_id="1",
            group_id=None,
            display_name="tester",
        ),
        request_id=1,
        deadline=DeadlineContext.from_timeout(30),
    )
    await runtime.advance(AgentRunState.PLANNING, model="model")
    await runtime.advance(AgentRunState.EXECUTING)
    spec = ToolSpec(
        name="mutate",
        description="change",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        policy=ToolPolicy.configured(),
    )
    tool_snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(1, registered=(spec,)),
    )
    harness = Harness({}, snapshot=tool_snapshot)
    harness.agent_runtime = runtime

    try:
        messages = await harness._execute_tools(
            [_call(1, "mutate")],
            "",
            [],
            "",
        )
    finally:
        await resources.close()

    assert executions == 0
    assert client_calls == 1
    assert "尚未执行" not in messages[-1]["content"]
    assert "确认执行" not in messages[-1]["content"]
    assert "private-secret" not in messages[-1]["content"]
    assert "redis.invalid" not in messages[-1]["content"]
    assert runtime.tool_calls[-1].status is ToolCallStatus.FAILED
    assert runtime.tool_calls[-1].confirmation_id is None
    assert await pending_action_store.size() == 0


@pytest.mark.asyncio
async def test_duplicate_mutating_prompt_uses_remaining_ttl(monkeypatch) -> None:
    from nonebot_plugin_moellmchats import llm_tools

    now = [100.0]
    store = PendingActionStore(
        clock=lambda: now[0],
        nonce_factory=lambda: "ABC123",
    )
    monkeypatch.setattr(llm_tools, "pending_action_store", store)

    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    tools = {"mutate": spec.as_legacy_schema()}
    first = Harness(tools)
    await first._execute_tools([_call(1, "mutate")], "", [], "")

    now[0] = 219.2
    duplicate = Harness(tools)
    messages = await duplicate._execute_tools(
        [_call(2, "mutate")],
        "",
        [],
        "",
    )

    assert duplicate.bot.sent[-1] == (
        "工具 mutate 会修改外部状态，尚未执行。\n"
        "请在 1 秒内单独发送：确认执行 ABC123"
    )
    assert "请在 1 秒内" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_tool_result_and_images_are_bounded() -> None:
    async def large():
        return ToolResult(text="x" * 50, images=tuple(f"image:{i}" for i in range(8)))

    spec = ToolSpec(
        name="large",
        description="large",
        parameters={"type": "object", "properties": {}},
        handler=large,
        result_limit=10,
    )
    harness = Harness({"large": spec.as_legacy_schema()})
    messages = await harness._execute_tools([_call(1, "large")], "", [], "")
    assert messages[-1]["content"].startswith("函数执行返回")
    assert "xxxxxxxxxx\n...[工具结果已截断]" in messages[-1]["content"]
    assert "x" * 11 not in messages[-1]["content"]
    assert len(harness._pending_vision_images) == 4


@pytest.mark.asyncio
async def test_structured_tool_result_uses_one_model_and_history_rendering() -> None:
    async def structured() -> ToolResult:
        return ToolResult(
            text="weather",
            images=("private-image-reference",),
            metadata={"provider": "demo"},
            files=(
                ToolResultFile(
                    locator="attachment:forecast",
                    name="forecast.json",
                    media_type="application/json",
                ),
            ),
            structured={"temperature": 26, "condition": "rain"},
            citations=(
                ToolResultCitation(
                    title="Forecast source",
                    url="https://example.com/forecast",
                ),
            ),
        )

    spec = ToolSpec(
        name="structured",
        description="structured result",
        parameters={"type": "object", "properties": {}},
        handler=structured,
        result_limit=2_000,
    )
    harness = Harness({"structured": spec.as_legacy_schema()})

    messages = await harness._execute_tools(
        [_call(1, "structured")],
        "",
        [],
        "",
    )

    content = messages[-1]["content"]
    assert content.startswith("函数执行返回结果：\nweather")
    assert "[结构化工具结果]" in content
    assert '"structured":{"condition":"rain","temperature":26}' in content
    assert '"locator":"attachment:forecast"' in content
    assert '"url":"https://example.com/forecast"' in content
    assert '"metadata":{"provider":"demo"}' in content
    assert '"image_count":1' in content
    assert "private-image-reference" not in content
    assert harness._pending_vision_images == ["private-image-reference"]
    assert harness.messages_handler.messages_entity.tool_messages[-1][
        "content"
    ] == content[:300]


@pytest.mark.asyncio
async def test_non_object_tool_arguments_become_tool_error() -> None:
    harness = Harness({})
    messages = await harness._execute_tools(
        [_call(1, "web_search", "[]")], "", [], ""
    )
    assert "必须是 JSON 对象" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_missing_required_argument_becomes_tool_error() -> None:
    harness = Harness({})
    messages = await harness._execute_tools(
        [_call(1, "web_search", "{}")], "", [], ""
    )
    assert "缺少必填参数" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_web_search_legacy_branch_uses_canonical_builtin_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import search as search_module

    calls = []

    class FakeSearch:
        def __init__(
            self,
            query,
            tool_snapshot=None,
            *,
            is_superuser: bool,
        ) -> None:
            calls.append((query, tool_snapshot, is_superuser))

        async def get_search(self) -> str:
            return "external observation"

    monkeypatch.setattr(search_module, "Search", FakeSearch)
    harness = Harness({})

    messages = await harness._execute_tools(
        [_call(1, "web_search", '{"query":"latest"}')],
        "",
        [],
        "",
    )

    assert calls == [("latest", harness.tool_snapshot, True)]
    assert harness.bot.sent == ["正在搜索: latest..."]
    assert "external observation" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_nonebot_plugin_legacy_branch_keeps_bounded_dispatch_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module

    legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "description": "demo plugin",
                "usage": "/demo",
            }
        }
    )

    async def forbidden_handler(**_kwargs):
        raise AssertionError("D-05b must not cut the legacy consumer over")

    legacy["plugin_demo"]["tool_spec"] = replace(
        specs[0],
        handler=forbidden_handler,
    )
    calls = []

    async def dispatch(bot, event, command, source, *, plugin_name):
        calls.append((bot, event, command, source, plugin_name))
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text="visible output",
            matcher_checked=1,
            matcher_matched=1,
            successful_captures=1,
            api_succeeded=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    harness = Harness({}, plugins=legacy)

    messages = await harness._execute_tools(
        [_call(1, "plugin_demo", '{"command":"/demo"}')],
        "",
        [],
        "",
    )

    assert calls == [
        (
            harness.bot,
            harness.event,
            "/demo",
            harness.format_message_dict,
            "plugin_demo",
        )
    ]
    assert "visible output" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_plugin_failure_fingerprint_blocks_only_identical_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module

    legacy, _specs = build_nonebot_plugin_candidate(
        {"plugin_demo": {"description": "demo", "usage": "/demo"}}
    )
    commands: list[str] = []

    async def dispatch(_bot, _event, command, _source, *, plugin_name):
        assert plugin_name == "plugin_demo"
        commands.append(command)
        return PluginDispatchResult(
            status=PluginDispatchStatus.NOT_MATCHED,
            matcher_checked=2,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    harness = Harness({}, plugins=legacy)

    first = await harness._execute_tools(
        [_call(1, "plugin_demo", '{"command":"/demo bad"}')],
        "",
        [],
        "",
    )
    repeated = await harness._execute_tools(
        [_call(2, "plugin_demo", '{"command":"/demo bad"}')],
        "",
        [],
        "",
    )
    changed = await harness._execute_tools(
        [_call(3, "plugin_demo", '{"command":"/demo good"}')],
        "",
        [],
        "",
    )

    assert commands == ["/demo bad", "/demo good"]
    assert "没有 Matcher 命中" in first[-1]["content"]
    assert "禁止原样重复" in repeated[-1]["content"]
    assert "没有 Matcher 命中" in changed[-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        PluginDispatchStatus.RESULT_UNKNOWN,
        PluginDispatchStatus.PARTIAL_SUCCESS,
    ],
)
async def test_uncertain_or_partial_plugin_result_blocks_whole_tool(
    monkeypatch: pytest.MonkeyPatch,
    status: PluginDispatchStatus,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module

    legacy, _specs = build_nonebot_plugin_candidate(
        {"plugin_demo": {"description": "demo", "usage": "/demo"}}
    )
    calls = 0

    async def dispatch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return PluginDispatchResult(
            status=status,
            matcher_checked=1,
            matcher_matched=1,
            api_failed=1,
            api_unknown=(1 if status is PluginDispatchStatus.RESULT_UNKNOWN else 0),
            successful_captures=(
                1 if status is PluginDispatchStatus.PARTIAL_SUCCESS else 0
            ),
            text=("已发送部分内容" if status is PluginDispatchStatus.PARTIAL_SUCCESS else ""),
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    harness = Harness({}, plugins=legacy)
    first = await harness._execute_tools(
        [_call(1, "plugin_demo", '{"command":"/demo one"}')],
        "",
        [],
        "",
    )
    second = await harness._execute_tools(
        [_call(2, "plugin_demo", '{"command":"/demo two"}')],
        "",
        [],
        "",
    )

    assert calls == 1
    assert (
        "结果不确定" in first[-1]["content"]
        or "部分可验证结果" in first[-1]["content"]
    )
    assert "禁止再次调用" in second[-1]["content"]


@pytest.mark.asyncio
async def test_progress_switch_hides_only_pre_execution_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def query() -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(text="最终工具结果")

    spec = ToolSpec(
        name="query_once",
        description="query",
        parameters={"type": "object", "properties": {}},
        handler=query,
    )
    original_get_config = config_parser.get_config
    monkeypatch.setattr(
        config_parser,
        "get_config",
        lambda key, default=None: (
            False
            if key == "tool_progress_messages_enabled"
            else original_get_config(key, default)
        ),
    )
    harness = Harness({"query_once": spec.as_legacy_schema()})
    messages = await harness._execute_tools(
        [_call(1, "query_once")],
        "我来查询一下",
        [],
        "",
    )

    assert calls == 1
    assert harness.sent == []
    assert harness.bot.sent == []
    assert "最终工具结果" in messages[-1]["content"]


def test_command_audit_preview_never_exposes_arguments_or_locations() -> None:
    preview = LlmToolsMixin._safe_command_preview(
        "/排行 123456 https://example.invalid/path?token=secret"
    )
    assert preview.startswith("/排行 <args> [tokens=3,chars=")
    assert "123456" not in preview
    assert "example.invalid" not in preview
    assert "secret" not in preview
    assert "secret" not in LlmToolsMixin._safe_command_preview(
        "Authorization secret-token"
    )
    assert LlmToolsMixin._safe_command_preview(
        "/var/private/config.json token"
    ).startswith("<redacted>")


@pytest.mark.asyncio
async def test_hallucinated_superuser_tool_is_rejected_without_breaking_reply() -> None:
    executions = 0

    async def admin_only():
        nonlocal executions
        executions += 1
        return "secret"

    spec = ToolSpec(
        name="admin_only",
        description="admin",
        parameters={"type": "object", "properties": {}},
        handler=admin_only,
        permission="superuser",
    )
    harness = Harness({"admin_only": spec.as_legacy_schema()})
    harness.event.user_id = 2
    messages = await harness._execute_tools(
        [_call(1, "admin_only")], "normal answer", [], ""
    )
    assert executions == 0
    assert "仅允许超级用户" in messages[-1]["content"]
    assert harness.sent == ["normal answer"]


@pytest.mark.asyncio
async def test_nested_argument_type_error_becomes_tool_error() -> None:
    async def nested(payload):
        return str(payload)

    spec = ToolSpec(
        name="nested",
        description="nested",
        parameters={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                }
            },
            "required": ["payload"],
        },
        handler=nested,
    )
    harness = Harness({"nested": spec.as_legacy_schema()})
    messages = await harness._execute_tools(
        [_call(1, "nested", '{"payload":{"count":"bad"}}')], "", [], ""
    )
    assert "类型错误" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_provider_catalog_nonebot_execution_uses_canonical_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module
    from nonebot_plugin_moellmchats import nonebot_plugin_tools as plugin_module

    plugin_info, specs = build_nonebot_plugin_candidate(
        {
            "provider_plugin": {
                "description": "provider plugin",
                "usage": "/provider",
            }
        }
    )
    snapshot = ToolSnapshot(
        generation=2,
        plugin_info=plugin_info,
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(
            2,
            nonebot_plugins=specs,
        ),
    )
    calls = []

    class ForbiddenRollbackSimulator:
        async def dispatch_event(self, *_args, **_kwargs):
            raise AssertionError("Provider path must not use llm_tools rollback bus")

    async def canonical_dispatch(bot, event, command, source, *, plugin_name):
        calls.append((bot, event, command, source, plugin_name))
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text="canonical visible output",
            matcher_checked=1,
            matcher_matched=1,
            successful_captures=1,
            api_succeeded=1,
        )

    monkeypatch.setattr(module, "event_simulator", ForbiddenRollbackSimulator())
    monkeypatch.setattr(
        plugin_module.event_simulator,
        "dispatch_event",
        canonical_dispatch,
    )
    harness = Harness({}, snapshot=snapshot)

    messages = await harness._execute_tools(
        [_call(1, "provider_plugin", '{"command":"/provider"}')],
        "",
        [],
        "",
    )

    assert calls == [
        (
            harness.bot,
            harness.event,
            "/provider",
            harness.format_message_dict,
            "provider_plugin",
        )
    ]
    assert "canonical visible output" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_llm_tools_rejects_unknown_name_before_legacy_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module

    async def forbidden_dispatch(*_args, **_kwargs):
        raise AssertionError("unknown tool must fail before dispatch")

    monkeypatch.setattr(module.event_simulator, "dispatch_event", forbidden_dispatch)
    harness = Harness({})
    messages = await harness._execute_tools(
        [_call(1, "hallucinated_tool", '{"command":"/unsafe"}')],
        "normal answer",
        [],
        "",
    )

    assert "不在当前 generation" in messages[-1]["content"]
    assert "已拒绝执行" in messages[-1]["content"]
    assert harness.sent == ["normal answer"]


@pytest.mark.asyncio
async def test_provider_catalog_execution_enforces_trust_permission() -> None:
    executions = 0

    async def admin_only() -> str:
        nonlocal executions
        executions += 1
        return "secret"

    spec = ToolSpec(
        name="provider_admin_only",
        description="provider admin",
        parameters={"type": "object", "properties": {}},
        handler=admin_only,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=3,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(3, registered=(spec,)),
    )
    harness = Harness({}, snapshot=snapshot)
    harness.event.user_id = 2
    harness.is_superuser = False

    messages = await harness._execute_tools(
        [_call(1, spec.name)],
        "normal answer",
        [],
        "",
    )

    assert executions == 0
    assert "工具契约只允许超级用户" in messages[-1]["content"]
    assert harness.sent == ["normal answer"]


@pytest.mark.asyncio
async def test_provider_mutating_confirmation_remains_visible_when_progress_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await pending_action_store.clear()
    executions = 0
    original_get_config = config_parser.get_config
    monkeypatch.setattr(
        config_parser,
        "get_config",
        lambda key, default=None: (
            False
            if key == "tool_progress_messages_enabled"
            else original_get_config(key, default)
        ),
    )

    async def mutate() -> str:
        nonlocal executions
        executions += 1
        return "changed"

    spec = ToolSpec(
        name="provider_mutate",
        description="provider mutate",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=4,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(4, registered=(spec,)),
    )
    harness = Harness({}, snapshot=snapshot)
    harness.agent_runtime = await _agent_request_runtime()

    messages = await harness._execute_tools(
        [_call(1, spec.name, '{"confirm":true}')],
        "",
        [],
        "",
    )

    assert executions == 0
    assert "尚未执行" in messages[-1]["content"]
    assert "确认执行" in messages[-1]["content"]
    runtime = harness.agent_runtime
    assert runtime is not None
    assert runtime.tool_calls[-1].status is ToolCallStatus.WAITING_CONFIRMATION
    assert runtime.tool_calls[-1].confirmation_id is not None
    assert AgentRunState.WAITING_CONFIRMATION in {
        run.state for run in runtime.run_history
    }
    assert runtime.run.state is AgentRunState.EXECUTING
    assert harness.sent == []
    assert len(harness.bot.sent) == 1
    assert "确认执行" in harness.bot.sent[0]
    await pending_action_store.clear()


@pytest.mark.asyncio
async def test_protocol_confirmation_remains_visible_when_progress_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await pending_action_store.clear()
    original_get_config = config_parser.get_config
    monkeypatch.setattr(
        config_parser,
        "get_config",
        lambda key, default=None: (
            False
            if key == "tool_progress_messages_enabled"
            else True
            if key == "protocol_tools_enabled"
            else original_get_config(key, default)
        ),
    )

    class ProtocolBot(FakeBot):
        self_id = "10000"
        adapter = SimpleNamespace(get_name=lambda: "OneBot V11")

        def __init__(self) -> None:
            super().__init__()
            self.api_calls: list[tuple[str, dict]] = []

        async def call_api(self, api: str, **data):
            self.api_calls.append((api, data))
            if api == "get_version_info":
                return {
                    "app_name": "Lagrange.OneBot",
                    "app_version": "1.0.0",
                    "protocol_version": "11",
                }
            raise AssertionError("确认前不得执行真实协议动作")

    event = SimpleNamespace(
        time=1,
        user_id=1,
        group_id=456,
        message_id=789,
        message=Message("踢出成员"),
        sender=SimpleNamespace(user_id=1, card="tester", nickname="tester"),
        reply=None,
    )
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(1),
    )
    harness = Harness({}, snapshot=snapshot)
    harness.bot = ProtocolBot()
    harness.event = event
    harness.agent_runtime = await _agent_request_runtime()

    async with protocol_request_scope(
        harness.bot,
        event,
        generation=1,
        is_superuser=True,
    ):
        messages = await harness._execute_tools(
            [
                _call(
                    1,
                    "onebot_v11__set_group_kick",
                    '{"group_id":456,"user_id":2}',
                )
            ],
            "",
            [],
            "",
        )

    assert harness.sent == []
    assert harness.bot.api_calls == [("get_version_info", {})]
    assert len(harness.bot.sent) == 1
    assert "确认" in harness.bot.sent[0]
    assert "尚未执行" in messages[-1]["content"]
    runtime = harness.agent_runtime
    assert runtime is not None
    assert runtime.tool_calls[-1].status is ToolCallStatus.WAITING_CONFIRMATION
    await pending_action_store.clear()


@pytest.mark.asyncio
async def test_llm_tools_config_rollback_keeps_legacy_nonebot_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats import llm_tools as module
    from nonebot_plugin_moellmchats import nonebot_plugin_tools as plugin_module

    plugin_info, specs = build_nonebot_plugin_candidate(
        {
            "rollback_plugin": {
                "description": "rollback plugin",
                "usage": "/rollback",
            }
        }
    )
    snapshot = ToolSnapshot(
        generation=5,
        plugin_info=plugin_info,
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_complete_catalog(5, nonebot_plugins=specs),
    )
    rollback_calls = []

    class RollbackSimulator:
        async def dispatch_event(
            self,
            bot,
            event,
            command,
            source,
            *,
            plugin_name,
        ):
            rollback_calls.append((command, plugin_name))
            return PluginDispatchResult(
                status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
                text="rollback output",
                matcher_checked=1,
                matcher_matched=1,
                successful_captures=1,
                api_succeeded=1,
            )

    async def forbidden_provider_dispatch(*_args, **_kwargs):
        raise AssertionError("rollback switch must not use Provider handler")

    original_get_config = config_parser.get_config

    def rollback_config(key: str, default=None):
        if key == "provider_catalog_llm_tools_enabled":
            return False
        return original_get_config(key, default)

    monkeypatch.setattr(module, "event_simulator", RollbackSimulator())
    monkeypatch.setattr(
        plugin_module.event_simulator,
        "dispatch_event",
        forbidden_provider_dispatch,
    )
    monkeypatch.setattr(config_parser, "get_config", rollback_config)
    harness = Harness({}, snapshot=snapshot)

    messages = await harness._execute_tools(
        [_call(1, "rollback_plugin", '{"command":"/rollback"}')],
        "",
        [],
        "",
    )

    assert rollback_calls == [("/rollback", "rollback_plugin")]
    assert "rollback output" in messages[-1]["content"]

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.llm_tools import LlmToolsMixin
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.pending_actions import (
    PendingActionStore,
    pending_action_store,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolPolicy,
    ToolResult,
    ToolSpec,
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
        self.sent = []

    async def send_emotion_message(self, text: str) -> str:
        self.sent.append(text)
        return text

    def _sanitize_tool_calls_for_history(self, calls):
        return calls


def _call(identifier: int, name: str, arguments: str = "{}") -> dict:
    return {
        "id": str(identifier),
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


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
async def test_repeated_tool_limit_prevents_third_execution() -> None:
    executions = 0

    async def tool():
        nonlocal executions
        executions += 1
        return "ok"

    harness = Harness({"tool": {"func": tool}})
    for index in range(3):
        messages = await harness._execute_tools([_call(index, "tool")], "", [], "")
    assert executions == 2
    assert "重复调用上限" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_mutating_tool_always_requires_separate_nonce_confirmation() -> None:
    await pending_action_store.clear()
    executions = []

    async def mutate(_tool_context=None):
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
        def __init__(self, query, tool_snapshot=None) -> None:
            calls.append((query, tool_snapshot))

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

    assert calls == [("latest", harness.tool_snapshot)]
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
        return "visible output", []

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
        return "canonical visible output", []

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
async def test_provider_mutating_execution_still_creates_pending_action() -> None:
    await pending_action_store.clear()
    executions = 0

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

    messages = await harness._execute_tools(
        [_call(1, spec.name, '{"confirm":true}')],
        "",
        [],
        "",
    )

    assert executions == 0
    assert "尚未执行" in messages[-1]["content"]
    assert "确认执行" in messages[-1]["content"]
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
            return "rollback output", []

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

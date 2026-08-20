from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.pending_actions import (
    PendingActionError,
    PendingActionStore,
    execute_pending_action,
)
from nonebot_plugin_moellmchats.runtime_snapshot import (
    RuntimeSnapshot,
    immutable_mapping,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot


class FakeBot:
    def __init__(self, self_id: str = "10000", adapter_name: str = "fake") -> None:
        self.self_id = self_id
        self.adapter = SimpleNamespace(get_name=lambda: adapter_name)
        self.config = SimpleNamespace(superusers={"1"})


def _event(user_id: int = 1, group_id: int = 10):
    return SimpleNamespace(user_id=user_id, group_id=group_id)


def _runtime_snapshot(
    generation: int,
    custom_tools: dict[str, dict],
) -> RuntimeSnapshot:
    tool_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info=immutable_mapping({}),
        custom_tools=immutable_mapping(custom_tools),
        tool_dependencies=immutable_mapping({}),
        mcp_tool_names=frozenset(),
    )
    return RuntimeSnapshot(
        generation=generation,
        config=immutable_mapping({}),
        model_state=None,
        temperaments=immutable_mapping({}),
        temperament_assignments=immutable_mapping({}),
        replies=immutable_mapping({}),
        tool_snapshot=tool_snapshot,
        emotions=(),
        reloaded_at=1.0,
    )


@pytest.fixture
def pending_config(monkeypatch):
    original = config_parser.get_config

    def configured(key, default=None):
        if key == "pending_action_ttl_seconds":
            return 120
        if key == "pending_action_max_entries":
            return 16
        if key == "pending_action_max_argument_bytes":
            return 16_384
        if key == "pending_action_failure_window_seconds":
            return 60
        if key == "pending_action_max_failures":
            return 8
        if key == "pending_action_max_failure_keys":
            return 4_096
        return original(key, default)

    monkeypatch.setattr(config_parser, "get_config", configured)


@pytest.mark.asyncio
async def test_pending_action_is_bound_hashed_and_one_shot(pending_config) -> None:
    store = PendingActionStore(nonce_factory=lambda: "A7F42C")
    bot = FakeBot()
    event = _event()
    action = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"z": 1, "a": "fixed"},
        generation=7,
        bundle_digest="d" * 64,
    )
    assert action.arguments_json == '{"a":"fixed","z":1}'
    assert len(action.arguments_hash) == 64
    assert action.nonce == "A7F42C"

    consumed = await store.consume(
        "a7f42c", bot=bot, event=event, generation=7
    )
    assert consumed == action
    assert consumed.arguments() == {"a": "fixed", "z": 1}
    with pytest.raises(PendingActionError, match="已过期或已使用"):
        await store.consume("A7F42C", bot=bot, event=event, generation=7)


@pytest.mark.asyncio
async def test_duplicate_call_reuses_nonce_and_changed_arguments_invalidate_old(
    pending_config,
) -> None:
    nonces = iter(["111111", "222222"])
    store = PendingActionStore(nonce_factory=lambda: next(nonces))
    bot = FakeBot()
    event = _event()
    first = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": 1},
        generation=1,
    )
    duplicate = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": 1},
        generation=1,
    )
    assert duplicate == first
    replacement = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": 2},
        generation=1,
    )
    assert replacement.nonce == "222222"
    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("111111", bot=bot, event=event, generation=1)


@pytest.mark.asyncio
async def test_duplicate_call_reports_actual_remaining_ttl(pending_config) -> None:
    now = [100.0]
    store = PendingActionStore(
        clock=lambda: now[0],
        nonce_factory=lambda: "ABC123",
    )
    bot = FakeBot()
    event = _event()
    first = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": 1},
        generation=1,
    )

    now[0] = 219.2
    duplicate = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": 1},
        generation=1,
    )

    assert duplicate is first
    assert store.remaining_ttl_seconds(duplicate) == 1


@pytest.mark.asyncio
async def test_wrong_user_or_group_cannot_consume_action(pending_config) -> None:
    store = PendingActionStore(nonce_factory=lambda: "ABC123")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=2,
    )
    with pytest.raises(PendingActionError, match="不匹配"):
        await store.consume("ABC123", bot=bot, event=_event(2, 10), generation=2)
    with pytest.raises(PendingActionError, match="不匹配"):
        await store.consume("ABC123", bot=bot, event=_event(1, 11), generation=2)
    assert (await store.consume("ABC123", bot=bot, event=event, generation=2)).tool_name == "mutate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "other_bot",
    [
        FakeBot(self_id="20000", adapter_name="fake"),
        FakeBot(self_id="10000", adapter_name="other-adapter"),
    ],
    ids=["bot-id", "adapter"],
)
async def test_wrong_bot_or_adapter_cannot_consume_action(
    pending_config,
    other_bot: FakeBot,
) -> None:
    store = PendingActionStore(nonce_factory=lambda: "B07B07")
    owner_bot = FakeBot()
    event = _event()
    await store.create(
        bot=owner_bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=2,
    )

    with pytest.raises(PendingActionError, match="不匹配"):
        await store.consume(
            "B07B07",
            bot=other_bot,
            event=event,
            generation=2,
        )

    action = await store.consume(
        "B07B07",
        bot=owner_bot,
        event=event,
        generation=2,
    )
    assert action.tool_name == "mutate"


@pytest.mark.asyncio
async def test_expiry_and_generation_change_fail_closed(pending_config) -> None:
    now = [100.0]
    nonces = iter(["AAAAAA", "BBBBBB"])
    store = PendingActionStore(clock=lambda: now[0], nonce_factory=lambda: next(nonces))
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="first",
        arguments={},
        generation=3,
    )
    now[0] = 221.0
    with pytest.raises(PendingActionError, match="已过期"):
        await store.consume("AAAAAA", bot=bot, event=event, generation=3)

    now[0] = 300.0
    await store.create(
        bot=bot,
        event=event,
        tool_name="second",
        arguments={},
        generation=3,
    )
    with pytest.raises(PendingActionError, match="已重载"):
        await store.consume("BBBBBB", bot=bot, event=event, generation=4)
    with pytest.raises(PendingActionError, match="已过期或已使用"):
        await store.consume("BBBBBB", bot=bot, event=event, generation=3)


@pytest.mark.asyncio
async def test_concurrent_confirmation_executes_at_most_once(pending_config) -> None:
    store = PendingActionStore(nonce_factory=lambda: "CCCCCC")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=1,
    )
    results = await asyncio.gather(
        *(
            store.consume("CCCCCC", bot=bot, event=event, generation=1)
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, PendingActionError) for result in results) == 1


@pytest.mark.asyncio
async def test_confirmation_executes_fixed_snapshot_arguments_and_rechecks_permission(
    pending_config,
) -> None:
    executions = []

    async def mutate(value: str, _tool_context=None):
        executions.append((value, _tool_context.confirmed))
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change state",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=mutate,
        effect=ToolEffect.MUTATING,
        permission="superuser",
    )
    entry = spec.as_legacy_schema()
    store = PendingActionStore(nonce_factory=lambda: "DDDDDD")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": "original"},
        generation=9,
    )
    snapshot = _runtime_snapshot(9, {"mutate": entry})
    assert not isinstance(snapshot.tool_snapshot.custom_tools["mutate"], dict)
    action, result = await execute_pending_action(
        "DDDDDD", bot=bot, event=event, runtime_snapshot=snapshot, store=store
    )
    assert action.arguments_hash
    assert result.text == "changed"
    assert executions == [("original", True)]

    denied_store = PendingActionStore(nonce_factory=lambda: "EEEEEE")
    ordinary_event = _event(user_id=2)
    await denied_store.create(
        bot=bot,
        event=ordinary_event,
        tool_name="mutate",
        arguments={"value": "blocked"},
        generation=9,
    )
    with pytest.raises(PendingActionError, match="仅允许超级用户"):
        await execute_pending_action(
            "EEEEEE",
            bot=bot,
            event=ordinary_event,
            runtime_snapshot=snapshot,
            store=denied_store,
        )
    assert executions == [("original", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_change", "message"),
    [
        ("missing", "待确认工具已不可用"),
        ("effect", "待确认工具属性已变化"),
        ("bundle", "待确认工具版本已变化"),
    ],
)
async def test_confirmation_rejects_changed_runtime_tool_and_consumes_nonce(
    pending_config,
    runtime_change: str,
    message: str,
) -> None:
    executions = []

    async def mutate() -> str:
        executions.append("executed")
        return "changed"

    action_digest = "a" * 64
    spec = ToolSpec(
        name="mutate",
        description="change state",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=(
            ToolEffect.READ_ONLY
            if runtime_change == "effect"
            else ToolEffect.MUTATING
        ),
    )
    entry = spec.as_legacy_schema()
    entry["bundle_digest"] = (
        "b" * 64 if runtime_change == "bundle" else action_digest
    )
    tools = {} if runtime_change == "missing" else {"mutate": entry}

    store = PendingActionStore(nonce_factory=lambda: "C0DE42")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=9,
        bundle_digest=action_digest,
    )

    with pytest.raises(PendingActionError, match=message):
        await execute_pending_action(
            "C0DE42",
            bot=bot,
            event=event,
            runtime_snapshot=_runtime_snapshot(9, tools),
            store=store,
        )

    assert executions == []
    assert await store.size() == 0
    with pytest.raises(PendingActionError, match="已过期或已使用"):
        await execute_pending_action(
            "C0DE42",
            bot=bot,
            event=event,
            runtime_snapshot=_runtime_snapshot(9, tools),
            store=store,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "damaged_value", "message"),
    [
        ("arguments_json", "{damaged", "待确认参数已损坏"),
        ("arguments_json", '{"value":"changed"}', "待确认参数校验失败"),
        ("arguments_hash", "0" * 64, "待确认参数校验失败"),
    ],
)
async def test_confirmation_rejects_damaged_arguments_and_consumes_nonce(
    pending_config,
    field: str,
    damaged_value: str,
    message: str,
) -> None:
    executions = []

    async def mutate(value: str) -> str:
        executions.append(value)
        return "changed"

    spec = ToolSpec(
        name="mutate",
        description="change state",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    store = PendingActionStore(nonce_factory=lambda: "DA6A6E")
    bot = FakeBot()
    event = _event()
    action = await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={"value": "original"},
        generation=9,
    )
    object.__setattr__(action, field, damaged_value)

    with pytest.raises(PendingActionError, match=message):
        await execute_pending_action(
            "DA6A6E",
            bot=bot,
            event=event,
            runtime_snapshot=_runtime_snapshot(
                9,
                {"mutate": spec.as_legacy_schema()},
            ),
            store=store,
        )

    assert executions == []
    assert await store.size() == 0


@pytest.mark.asyncio
async def test_confirmation_result_uses_tool_specific_limit(pending_config) -> None:
    async def mutate() -> str:
        return "abcdefghij"

    spec = ToolSpec(
        name="mutate",
        description="change state",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        result_limit=4,
    )
    store = PendingActionStore(nonce_factory=lambda: "AB12CD")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=9,
    )
    snapshot = _runtime_snapshot(9, {"mutate": spec.as_legacy_schema()})

    _, result = await execute_pending_action(
        "AB12CD", bot=bot, event=event, runtime_snapshot=snapshot, store=store
    )

    assert result.text == "abcd\n...[工具结果已截断]"


def _failure_config(monkeypatch, *, failures: int = 1, window: int = 60, keys: int = 16):
    original = config_parser.get_config

    def configured(key, default=None):
        values = {
            "pending_action_failure_window_seconds": window,
            "pending_action_max_failures": failures,
            "pending_action_max_failure_keys": keys,
        }
        return values.get(key, original(key, default))

    monkeypatch.setattr(config_parser, "get_config", configured)


@pytest.mark.asyncio
async def test_expired_action_cannot_be_cancelled(
    pending_config, monkeypatch
) -> None:
    now = [10.0]
    store = PendingActionStore(clock=lambda: now[0], nonce_factory=lambda: "C0FFEE")
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="mutate",
        arguments={},
        generation=1,
    )
    now[0] = 131.0

    with pytest.raises(PendingActionError, match="不存在、已过期"):
        await store.cancel("C0FFEE", bot=bot, event=event)
    assert await store.size() == 0


@pytest.mark.asyncio
async def test_consume_failures_are_rate_limited_per_caller(
    pending_config, monkeypatch
) -> None:
    _failure_config(monkeypatch, failures=2)
    store = PendingActionStore()
    bot = FakeBot()
    event = _event()

    for nonce in ("BAD", "FFFFFF"):
        with pytest.raises(PendingActionError):
            await store.consume(nonce, bot=bot, event=event, generation=1)
    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.consume("EEEEEE", bot=bot, event=event, generation=1)


@pytest.mark.asyncio
async def test_cancel_failure_window_recovers_and_success_clears_budget(
    pending_config, monkeypatch
) -> None:
    _failure_config(monkeypatch, failures=1, window=10)
    now = [100.0]
    nonces = iter(["111111", "222222"])
    store = PendingActionStore(
        clock=lambda: now[0], nonce_factory=lambda: next(nonces)
    )
    bot = FakeBot()
    event = _event()
    await store.create(
        bot=bot,
        event=event,
        tool_name="first",
        arguments={},
        generation=1,
    )
    with pytest.raises(PendingActionError, match="格式错误"):
        await store.cancel("bad", bot=bot, event=event)
    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.cancel("111111", bot=bot, event=event)

    now[0] = 110.0
    await store.cancel("111111", bot=bot, event=event)
    await store.create(
        bot=bot,
        event=event,
        tool_name="second",
        arguments={},
        generation=1,
    )
    # A successful cancellation cleared the old caller window, so this is a normal
    # missing-code failure rather than an immediate rate-limit response.
    with pytest.raises(PendingActionError, match="不存在"):
        await store.cancel("FFFFFF", bot=bot, event=event)


@pytest.mark.asyncio
async def test_failure_budget_isolated_by_bot_adapter_user_and_group(
    pending_config, monkeypatch
) -> None:
    _failure_config(monkeypatch, failures=1)
    store = PendingActionStore()
    original_bot = FakeBot("10000", "adapter-a")
    original_event = _event(1, 10)
    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume(
            "FFFFFF", bot=original_bot, event=original_event, generation=1
        )
    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.consume(
            "EEEEEE", bot=original_bot, event=original_event, generation=1
        )

    isolated_callers = [
        (FakeBot("10001", "adapter-a"), _event(1, 10)),
        (FakeBot("10000", "adapter-b"), _event(1, 10)),
        (FakeBot("10000", "adapter-a"), _event(2, 10)),
        (FakeBot("10000", "adapter-a"), _event(1, 11)),
    ]
    for bot, event in isolated_callers:
        with pytest.raises(PendingActionError, match="不存在"):
            await store.consume("FFFFFF", bot=bot, event=event, generation=1)


@pytest.mark.asyncio
async def test_attacker_failures_do_not_block_owner_confirmation(
    pending_config, monkeypatch
) -> None:
    _failure_config(monkeypatch, failures=1)
    store = PendingActionStore(nonce_factory=lambda: "ABC123")
    bot = FakeBot()
    owner = _event(1, 10)
    attacker = _event(2, 10)
    await store.create(
        bot=bot,
        event=owner,
        tool_name="mutate",
        arguments={},
        generation=1,
    )
    with pytest.raises(PendingActionError, match="不匹配"):
        await store.consume("ABC123", bot=bot, event=attacker, generation=1)
    with pytest.raises(PendingActionError, match="失败尝试过多"):
        await store.consume("ABC123", bot=bot, event=attacker, generation=1)

    action = await store.consume("ABC123", bot=bot, event=owner, generation=1)
    assert action.tool_name == "mutate"


@pytest.mark.asyncio
async def test_failure_budget_key_table_is_bounded(
    pending_config, monkeypatch
) -> None:
    _failure_config(monkeypatch, failures=1, keys=2)
    store = PendingActionStore()
    bot = FakeBot()
    for user_id in (1, 2, 3):
        with pytest.raises(PendingActionError, match="不存在"):
            await store.consume(
                "FFFFFF", bot=bot, event=_event(user_id, 10), generation=1
            )

    assert len(store._failure_windows) == 2
    # The oldest key was evicted, so user 1 receives a normal failure again.
    with pytest.raises(PendingActionError, match="不存在"):
        await store.consume("EEEEEE", bot=bot, event=_event(1, 10), generation=1)

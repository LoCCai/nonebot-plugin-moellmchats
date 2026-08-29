from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Hashable
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import chat_runtime
from nonebot_plugin_moellmchats.admission import AdmissionRejected
from nonebot_plugin_moellmchats.admission_store import AdmissionStoreError
from nonebot_plugin_moellmchats.agent_context_runtime import (
    AgentRequestRuntime,
    RuntimeResourceHost,
)
from nonebot_plugin_moellmchats.agent_runtime import AgentRunState
from nonebot_plugin_moellmchats.cooldowns import CooldownClaim, CooldownLease
from nonebot_plugin_moellmchats.redis_admission import RedisAdmissionSettings
from nonebot_plugin_moellmchats.redis_client import (
    RedisClientManager,
    RedisClientSettings,
)
from nonebot_plugin_moellmchats.redis_cooldowns import (
    RedisCooldownSettings,
    RedisCooldownUnavailableError,
)
from nonebot_plugin_moellmchats.redis_failure_policy import (
    RedisComponentPorts,
    RedisFailurePolicy,
)
import nonebot_plugin_moellmchats.runtime_resources as resources_module
from nonebot_plugin_moellmchats.runtime_resources import (
    RuntimeResourceBuilder,
    RuntimeResourceSettings,
)
from nonebot_plugin_moellmchats.runtime_snapshot import (
    RuntimeSnapshot,
    runtime_snapshots,
)


class MatcherFinished(RuntimeError):
    pass


class FakeMatcher:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def finish(self, message: str) -> None:
        self.messages.append(str(message))
        raise MatcherFinished

    async def send(self, message: str) -> None:
        self.messages.append(str(message))


@pytest.mark.asyncio
async def test_cancel_notice_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowMatcher:
        async def send(self, _message: str) -> None:
            await asyncio.sleep(1)

    monkeypatch.setattr(chat_runtime, "_CANCEL_NOTICE_TIMEOUT_SECONDS", 0.01)
    await asyncio.wait_for(
        chat_runtime._send_cancel_notice(SlowMatcher()),
        timeout=0.2,
    )


@pytest.mark.asyncio
async def test_cancel_notice_preserves_repeated_cancellation() -> None:
    class CancelMatcher:
        async def send(self, _message: str) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await chat_runtime._send_cancel_notice(CancelMatcher())


def _event(*, user_id: int = 42, timestamp: int = 1_000):
    return SimpleNamespace(
        time=timestamp,
        sender=SimpleNamespace(user_id=user_id, card="测试用户", nickname="测试用户"),
    )


def _runtime_event(*, user_id: int = 42, timestamp: int = 1_000):
    return SimpleNamespace(
        user_id=user_id,
        time=timestamp,
        message_id=123,
        sender=SimpleNamespace(
            user_id=user_id,
            card="测试用户",
            nickname="测试用户",
        ),
    )


def _publish_snapshot() -> RuntimeSnapshot:
    current = runtime_snapshots.current()
    generation = 1 if current is None else current.generation + 1
    snapshot = RuntimeSnapshot(
        generation=generation,
        config={},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=float(generation),
    )
    runtime_snapshots.publish(snapshot)
    return snapshot


def _config(monkeypatch, *, cooldown: int, timeout: float) -> None:
    values = {"cd_seconds": cooldown, "request_timeout_seconds": timeout}
    monkeypatch.setattr(
        chat_runtime.config_parser,
        "get_config",
        lambda key, default=None: values.get(key, default),
    )


def _bound_generation() -> int | None:
    snapshot = runtime_snapshots.bound()
    return None if snapshot is None else snapshot.generation


class RecordingCooldownStore:
    def __init__(
        self,
        events: list[tuple[str, int | None]],
        *,
        failure: Exception | None = None,
        on_claim: Callable[[], None] | None = None,
    ) -> None:
        self.events = events
        self.failure = failure
        self.on_claim = on_claim
        self.lease = CooldownLease(
            user_id=42,
            token="a" * 32,
            claimed_at=1_000.0,
        )

    async def claim(
        self,
        *,
        user_id: int | str,
        event_time: float,
        cooldown_seconds: int,
    ) -> CooldownClaim:
        del user_id, event_time, cooldown_seconds
        self.events.append(("cooldown.claim", _bound_generation()))
        if self.on_claim is not None:
            self.on_claim()
        if self.failure is not None:
            raise self.failure
        return CooldownClaim(lease=self.lease, retry_after_seconds=0)

    async def release(self, lease: CooldownLease) -> bool:
        assert lease is self.lease
        self.events.append(("cooldown.release", _bound_generation()))
        return True


class RecordingAdmissionGate:
    def __init__(
        self,
        events: list[tuple[str, int | None]],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.events = events
        self.failure = failure

    @asynccontextmanager
    async def slot(
        self,
        key: Hashable | None = None,
    ) -> AsyncIterator[None]:
        del key
        self.events.append(("admission.enter", _bound_generation()))
        if self.failure is not None:
            raise self.failure
        try:
            yield
        finally:
            self.events.append(("admission.exit", _bound_generation()))


def _generation_redis_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cooldown_store: RecordingCooldownStore,
    admission_gate: RecordingAdmissionGate,
) -> RuntimeResourceHost:
    policy = RedisFailurePolicy()
    ports = RedisComponentPorts(
        pending_actions=None,
        cooldowns=cooldown_store,
        admission=admission_gate,
        policy=policy,
    )

    def build_ports(
        _manager: RedisClientManager,
        **_kwargs: object,
    ) -> RedisComponentPorts:
        return ports

    monkeypatch.setattr(resources_module, "build_redis_component_ports", build_ports)
    settings = RuntimeResourceSettings(
        redis=RedisClientSettings(redis_url="redis://offline.invalid/0"),
        redis_cooldowns=RedisCooldownSettings(),
        redis_admission=RedisAdmissionSettings(),
        redis_failure_policy=policy,
    )
    return RuntimeResourceHost(RuntimeResourceBuilder(settings))


@pytest.mark.asyncio
async def test_cooldown_rejects_before_entering_admission_queue(monkeypatch) -> None:
    chat_runtime.reset_all_runtime_state()
    chat_runtime.cd[42] = 990
    _config(monkeypatch, cooldown=120, timeout=180)
    entered = False

    class Controller:
        @asynccontextmanager
        async def slot(self, _key):
            nonlocal entered
            entered = True
            yield

    monkeypatch.setattr(chat_runtime, "get_llm_controller", lambda: Controller())
    matcher = FakeMatcher()

    with pytest.raises(MatcherFinished):
        await chat_runtime.handle_llm(object(), _event(), matcher, {})

    assert not entered
    assert matcher.messages == ["测试用户的 LLM 对话冷却中，请在 110 秒后重试。"]


@pytest.mark.asyncio
async def test_total_budget_includes_admission_queue_wait(monkeypatch) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=0, timeout=0.01)
    entered = asyncio.Event()

    class Controller:
        @asynccontextmanager
        async def slot(self, _key):
            entered.set()
            await asyncio.sleep(1)
            yield

    monkeypatch.setattr(chat_runtime, "get_llm_controller", lambda: Controller())
    matcher = FakeMatcher()

    with pytest.raises(MatcherFinished):
        await chat_runtime.handle_llm(object(), _event(), matcher, {})

    assert entered.is_set()
    assert matcher.messages == ["本次 LLM 任务已超过总时间预算，已安全终止。"]
    assert chat_runtime.cd[42] == 0


@pytest.mark.asyncio
async def test_rejected_admission_releases_claimed_cooldown(monkeypatch) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=180)

    class Controller:
        @asynccontextmanager
        async def slot(self, _key):
            raise AdmissionRejected("full")
            yield

    monkeypatch.setattr(chat_runtime, "get_llm_controller", lambda: Controller())
    matcher = FakeMatcher()

    with pytest.raises(MatcherFinished):
        await chat_runtime.handle_llm(object(), _event(), matcher, {})

    assert matcher.messages == ["当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。"]
    assert chat_runtime.cd[42] == 0


@pytest.mark.asyncio
async def test_chat_entry_binds_one_deadline_and_completes_memory_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=0, timeout=30)
    snapshot = _publish_snapshot()
    host = RuntimeResourceHost()
    captured = []

    class Chat:
        def __init__(
            self,
            _bot,
            _event,
            _message,
            *,
            temperament,
            agent_runtime,
        ) -> None:
            assert temperament == "测试性格"
            assert agent_runtime.coordinator.generation == snapshot.generation
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            return True

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    matcher = FakeMatcher()

    try:
        await chat_runtime.handle_llm(
            object(),
            _runtime_event(),
            matcher,
            {"text": ["hello"]},
            resource_host=host,
        )
    finally:
        await host.close()

    assert matcher.messages == []
    assert len(captured) == 1
    assert captured[0].run.state is AgentRunState.COMPLETED
    assert captured[0].deadline.deadline_at > 0


@pytest.mark.asyncio
async def test_timeout_records_terminal_state_before_user_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=0, timeout=0.01)
    _publish_snapshot()
    host = RuntimeResourceHost()
    captured = []

    class Chat:
        def __init__(
            self,
            _bot,
            _event,
            _message,
            *,
            temperament,
            agent_runtime,
        ) -> None:
            del temperament
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            await asyncio.sleep(1)
            return True

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    matcher = FakeMatcher()

    try:
        with pytest.raises(MatcherFinished):
            await chat_runtime.handle_llm(
                object(),
                _runtime_event(),
                matcher,
                {"text": ["hello"]},
                resource_host=host,
            )
    finally:
        await host.close()

    assert matcher.messages == ["本次 LLM 任务已超过总时间预算，已安全终止。"]
    assert captured[0].run.state is AgentRunState.TIMED_OUT


@pytest.mark.asyncio
async def test_chat_exception_records_failed_terminal_and_releases_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    _publish_snapshot()
    host = RuntimeResourceHost()
    captured = []

    class Chat:
        def __init__(
            self,
            _bot,
            _event,
            _message,
            *,
            temperament,
            agent_runtime,
        ) -> None:
            del temperament
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            raise RuntimeError("private model failure")

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    matcher = FakeMatcher()

    try:
        with pytest.raises(RuntimeError, match="private model failure"):
            await chat_runtime.handle_llm(
                object(),
                _runtime_event(),
                matcher,
                {"text": ["hello"]},
                resource_host=host,
            )
    finally:
        await host.close()

    assert captured[0].run.state is AgentRunState.FAILED
    assert captured[0].run.error_message is not None
    assert "private model failure" not in captured[0].run.error_message
    assert chat_runtime.cd[42] == 0


@pytest.mark.asyncio
async def test_chat_cancellation_records_cancelled_terminal_before_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    _publish_snapshot()
    host = RuntimeResourceHost()
    captured = []
    entered = asyncio.Event()

    class Chat:
        def __init__(
            self,
            _bot,
            _event,
            _message,
            *,
            temperament,
            agent_runtime,
        ) -> None:
            del temperament
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            entered.set()
            await asyncio.Event().wait()
            return True

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    matcher = FakeMatcher()
    task = asyncio.create_task(
        chat_runtime.handle_llm(
            object(),
            _runtime_event(),
            matcher,
            {"text": ["hello"]},
            resource_host=host,
        )
    )
    await entered.wait()
    task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await host.close()

    assert captured[0].run.state is AgentRunState.CANCELLED
    assert matcher.messages == ["当前 LLM 请求已被超级管理员终止。"]
    assert chat_runtime.cd[42] == 0


@pytest.mark.asyncio
async def test_generation_redis_cooldown_failure_precedes_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    snapshot = _publish_snapshot()
    events: list[tuple[str, int | None]] = []
    cooldown_store = RecordingCooldownStore(
        events,
        failure=RedisCooldownUnavailableError("Redis cooldown unavailable"),
    )
    host = _generation_redis_host(
        monkeypatch,
        cooldown_store=cooldown_store,
        admission_gate=RecordingAdmissionGate(events),
    )
    monkeypatch.setattr(
        chat_runtime,
        "get_llm_controller",
        lambda: pytest.fail("generation Redis cooldown 不得进入默认 admission"),
    )

    try:
        with pytest.raises(RedisCooldownUnavailableError, match="unavailable"):
            await chat_runtime.handle_llm(
                object(),
                _runtime_event(),
                FakeMatcher(),
                {},
                resource_host=host,
            )
    finally:
        await host.close()

    assert events == [("cooldown.claim", snapshot.generation)]


@pytest.mark.asyncio
async def test_generation_redis_admission_failure_releases_same_generation_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    snapshot = _publish_snapshot()
    events: list[tuple[str, int | None]] = []
    host = _generation_redis_host(
        monkeypatch,
        cooldown_store=RecordingCooldownStore(events),
        admission_gate=RecordingAdmissionGate(
            events,
            failure=AdmissionStoreError("Redis admission unavailable"),
        ),
    )
    monkeypatch.setattr(
        chat_runtime,
        "get_llm_controller",
        lambda: pytest.fail("generation Redis admission 不得进入默认 controller"),
    )
    matcher = FakeMatcher()

    try:
        with pytest.raises(MatcherFinished):
            await chat_runtime.handle_llm(
                object(),
                _runtime_event(),
                matcher,
                {},
                resource_host=host,
            )
    finally:
        await host.close()

    assert events == [
        ("cooldown.claim", snapshot.generation),
        ("admission.enter", snapshot.generation),
        ("cooldown.release", snapshot.generation),
    ]
    assert matcher.messages == ["当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。"]


@pytest.mark.asyncio
async def test_generation_redis_explicit_single_instance_fallback_completes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    snapshot = _publish_snapshot()
    client_calls = 0
    captured: list[AgentRequestRuntime] = []

    def unavailable_client(*_args: object, **_kwargs: object):
        nonlocal client_calls
        client_calls += 1
        raise RuntimeError("offline Redis client")

    def manager_factory(settings: RedisClientSettings) -> RedisClientManager:
        return RedisClientManager(settings, client_factory=unavailable_client)

    settings = RuntimeResourceSettings(
        redis=RedisClientSettings(redis_url="redis://offline.invalid/0"),
        redis_cooldowns=RedisCooldownSettings(),
        redis_admission=RedisAdmissionSettings(),
        redis_failure_policy=RedisFailurePolicy(
            single_instance_safe=True,
            cooldown_memory_fallback=True,
            admission_memory_fallback=True,
        ),
    )
    host = RuntimeResourceHost(
        RuntimeResourceBuilder(
            settings,
            redis_manager_factory=manager_factory,
        )
    )

    class Chat:
        def __init__(
            self,
            _bot,
            _event,
            _message,
            *,
            temperament,
            agent_runtime,
        ) -> None:
            assert temperament == "测试性格"
            assert agent_runtime.coordinator.generation == snapshot.generation
            assert _bound_generation() == snapshot.generation
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            return True

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    monkeypatch.setattr(
        chat_runtime,
        "get_llm_controller",
        lambda: pytest.fail("显式 generation fallback 不得进入默认 controller"),
    )

    try:
        await chat_runtime.handle_llm(
            object(),
            _runtime_event(),
            FakeMatcher(),
            {"text": ["hello"]},
            resource_host=host,
        )
    finally:
        await host.close()

    assert client_calls == 2
    assert len(captured) == 1
    assert captured[0].run.state is AgentRunState.COMPLETED


@pytest.mark.asyncio
async def test_generation_redis_lease_stays_pinned_when_snapshot_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat_runtime.reset_all_runtime_state()
    _config(monkeypatch, cooldown=120, timeout=30)
    first_snapshot = _publish_snapshot()
    second_snapshot: RuntimeSnapshot | None = None
    events: list[tuple[str, int | None]] = []
    captured: list[AgentRequestRuntime] = []

    def reload_snapshot() -> None:
        nonlocal second_snapshot
        second_snapshot = _publish_snapshot()

    host = _generation_redis_host(
        monkeypatch,
        cooldown_store=RecordingCooldownStore(
            events,
            on_claim=reload_snapshot,
        ),
        admission_gate=RecordingAdmissionGate(events),
    )

    class Chat:
        def __init__(
            self,
            _bot,
            _event,
            _message,
            *,
            temperament,
            agent_runtime,
        ) -> None:
            assert temperament == "测试性格"
            events.append(("agent.begin", _bound_generation()))
            captured.append(agent_runtime)

        async def get_llm_chat(self) -> bool:
            runtime = captured[-1]
            await runtime.advance(AgentRunState.PLANNING, model="model")
            await runtime.advance(AgentRunState.EXECUTING)
            return True

    monkeypatch.setattr(chat_runtime.llm, "MoeLlm", Chat)
    monkeypatch.setattr(
        chat_runtime.temperament_manager,
        "get_temperament",
        lambda _user_id: "测试性格",
    )
    monkeypatch.setattr(
        chat_runtime,
        "get_llm_controller",
        lambda: pytest.fail("generation Redis admission 不得进入默认 controller"),
    )

    try:
        await chat_runtime.handle_llm(
            object(),
            _runtime_event(),
            FakeMatcher(),
            {"text": ["hello"]},
            resource_host=host,
        )
        assert second_snapshot is not None
        assert runtime_snapshots.current() is second_snapshot
        assert host.current_generation == first_snapshot.generation
        assert captured[0].coordinator.generation == first_snapshot.generation
        assert events == [
            ("cooldown.claim", first_snapshot.generation),
            ("admission.enter", first_snapshot.generation),
            ("agent.begin", first_snapshot.generation),
            ("admission.exit", first_snapshot.generation),
        ]

        await host.synchronize(second_snapshot)
        assert host.current_generation == second_snapshot.generation
    finally:
        await host.close()

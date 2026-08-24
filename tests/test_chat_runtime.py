from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import chat_runtime
from nonebot_plugin_moellmchats.admission import AdmissionRejected
from nonebot_plugin_moellmchats.agent_context_runtime import RuntimeResourceHost
from nonebot_plugin_moellmchats.agent_runtime import AgentRunState
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

    assert matcher.messages == [
        "当前 LLM 请求较多，队列已满或你已有等待中的请求，请稍后再试。"
    ]
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
        with pytest.raises(MatcherFinished):
            await task
    finally:
        await host.close()

    assert captured[0].run.state is AgentRunState.CANCELLED
    assert matcher.messages == ["当前 LLM 请求已被超级管理员终止。"]
    assert chat_runtime.cd[42] == 0

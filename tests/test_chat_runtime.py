from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats import chat_runtime
from nonebot_plugin_moellmchats.admission import AdmissionRejected


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

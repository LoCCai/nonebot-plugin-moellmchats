from __future__ import annotations

import asyncio

from nonebot.adapters.onebot.v11.exception import (
    ActionFailed,
    ApiNotAvailable,
    NetworkError,
)
import pytest

from nonebot_plugin_moellmchats import moe_llm as module
from nonebot_plugin_moellmchats.moe_llm import MoeLlm


def _action_failed() -> ActionFailed:
    return ActionFailed(
        status="failed",
        retcode=1200,
        data=None,
        message="Timeout",
        wording="",
    )


def _network_error() -> NetworkError:
    return NetworkError("WebSocket call api send_msg timeout")


def _api_not_available() -> ApiNotAvailable:
    return ApiNotAvailable()


ADAPTER_FAILURE_FACTORIES = (
    _action_failed,
    _network_error,
    _api_not_available,
)


class _ScriptedBot:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[object] = []

    async def send(self, _event, message) -> None:
        self.sent.append(message)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


class _V12ScriptedBot(_ScriptedBot):
    class Adapter:
        @staticmethod
        def get_name() -> str:
            return "OneBot V12"

    adapter = Adapter()


def _llm(bot: _ScriptedBot) -> MoeLlm:
    llm = object.__new__(MoeLlm)
    llm.bot = bot
    llm.event = object()
    llm.emotion_flag = True
    return llm


@pytest.mark.asyncio
async def test_retry_notice_failure_is_bounded_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []

    class Recorder:
        def warning(self, message, *args) -> None:
            records.append(str(message).format(*args))

    class SlowBot:
        async def send(self, _event, _message) -> None:
            await asyncio.sleep(1)

    monkeypatch.setattr(module, "logger", Recorder())
    monkeypatch.setattr(module, "_PROGRESS_NOTICE_TIMEOUT_SECONDS", 0.01)
    llm = _llm(SlowBot())  # type: ignore[arg-type]

    await asyncio.wait_for(llm._send_retry_notice(1), timeout=0.2)

    assert records
    assert any("TimeoutError" in record for record in records)


@pytest.mark.asyncio
async def test_retry_notice_preserves_cancellation() -> None:
    class CancelBot:
        async def send(self, _event, _message) -> None:
            raise asyncio.CancelledError

    llm = _llm(CancelBot())  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await llm._send_retry_notice(1)


@pytest.mark.asyncio
async def test_tool_summary_failure_falls_back_without_private_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []

    class Recorder:
        def warning(self, message, *args) -> None:
            records.append(str(message).format(*args))

    monkeypatch.setattr(module, "logger", Recorder())
    llm = _llm(_ScriptedBot([]))
    llm.agent_runtime = None

    async def fail() -> str:
        raise RuntimeError("SECRET_SUMMARY_PAYLOAD")

    assert await llm._call_tool_summary_safely(fail) == ""
    assert records
    assert all("SECRET_SUMMARY_PAYLOAD" not in record for record in records)
    assert any("RuntimeError" in record for record in records)


@pytest.mark.asyncio
async def test_tool_summary_preserves_cancellation() -> None:
    llm = _llm(_ScriptedBot([]))
    llm.agent_runtime = None

    async def cancel() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await llm._call_tool_summary_safely(cancel)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_emotion_failure_is_isolated_after_body_delivery(
    monkeypatch: pytest.MonkeyPatch,
    failure_factory,
) -> None:
    bot = _ScriptedBot([None, failure_factory(), None])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑", "挥手"]),
    )
    monkeypatch.setattr(
        module,
        "get_emotion",
        lambda name, *, protocol: f"图片:{name}",
    )

    result = await llm.send_emotion_message("正文[微笑][挥手]")

    assert result == "正文"
    assert bot.sent == ["正文", "图片:微笑", "图片:挥手"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_body_delivery_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
    failure_factory,
) -> None:
    failure = failure_factory()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑"]),
    )
    monkeypatch.setattr(
        module,
        "get_emotion",
        lambda _name, *, protocol: "图片",
    )

    with pytest.raises(type(failure)) as captured:
        await llm.send_emotion_message("正文[微笑]")

    assert captured.value is failure
    assert bot.sent == ["正文"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_emotion_only_failure_propagates_when_nothing_was_delivered(
    monkeypatch: pytest.MonkeyPatch,
    failure_factory,
) -> None:
    failure = failure_factory()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("", ["微笑"]),
    )
    monkeypatch.setattr(
        module,
        "get_emotion",
        lambda _name, *, protocol: "图片",
    )

    with pytest.raises(type(failure)) as captured:
        await llm.send_emotion_message("[微笑]")

    assert captured.value is failure
    assert bot.sent == ["图片"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_factory", ADAPTER_FAILURE_FACTORIES)
async def test_plain_body_failure_is_never_downgraded(failure_factory) -> None:
    failure = failure_factory()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    llm.emotion_flag = False

    with pytest.raises(type(failure)) as captured:
        await llm.send_emotion_message("正文")

    assert captured.value is failure
    assert bot.sent == ["正文"]


@pytest.mark.asyncio
async def test_v12_body_succeeds_and_local_optional_emotion_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _V12ScriptedBot([None])
    llm = _llm(bot)
    protocols: list[str] = []
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑"]),
    )

    def no_v12_local_file(_name: str, *, protocol: str):
        protocols.append(protocol)
        return None

    monkeypatch.setattr(module, "get_emotion", no_v12_local_file)

    result = await llm.send_emotion_message("正文[微笑]")

    assert result == "正文"
    assert protocols == ["onebot_v12"]
    assert bot.sent == ["正文"]

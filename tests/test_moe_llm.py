from __future__ import annotations

from nonebot.adapters.onebot.v11.exception import ActionFailed
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


class _ScriptedBot:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[object] = []

    async def send(self, _event, message) -> None:
        self.sent.append(message)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome


def _llm(bot: _ScriptedBot) -> MoeLlm:
    llm = object.__new__(MoeLlm)
    llm.bot = bot
    llm.event = object()
    llm.emotion_flag = True
    return llm


@pytest.mark.asyncio
async def test_emotion_failure_is_isolated_after_body_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _ScriptedBot([None, _action_failed(), None])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑", "挥手"]),
    )
    monkeypatch.setattr(module, "get_emotion", lambda name: f"图片:{name}")

    result = await llm.send_emotion_message("正文[微笑][挥手]")

    assert result == "正文"
    assert bot.sent == ["正文", "图片:微笑", "图片:挥手"]


@pytest.mark.asyncio
async def test_body_delivery_failure_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = _action_failed()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("正文", ["微笑"]),
    )
    monkeypatch.setattr(module, "get_emotion", lambda _name: "图片")

    with pytest.raises(ActionFailed) as captured:
        await llm.send_emotion_message("正文[微笑]")

    assert captured.value is failure
    assert bot.sent == ["正文"]


@pytest.mark.asyncio
async def test_emotion_only_failure_propagates_when_nothing_was_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = _action_failed()
    bot = _ScriptedBot([failure])
    llm = _llm(bot)
    monkeypatch.setattr(
        module,
        "parse_emotion",
        lambda _content: ("", ["微笑"]),
    )
    monkeypatch.setattr(module, "get_emotion", lambda _name: "图片")

    with pytest.raises(ActionFailed) as captured:
        await llm.send_emotion_message("[微笑]")

    assert captured.value is failure
    assert bot.sent == ["图片"]

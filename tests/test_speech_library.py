from __future__ import annotations

import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import NetworkError
from nonebot.exception import FinishedException
import pytest

from nonebot_plugin_moellmchats import chat_policy, chat_runtime, request_budget
from nonebot_plugin_moellmchats import conversation_slots as slots
from nonebot_plugin_moellmchats import speech_library as library
from nonebot_plugin_moellmchats import speech_tools as speech
from nonebot_plugin_moellmchats.cooldowns import MemoryCooldownStore
from nonebot_plugin_moellmchats.delivery_policy import reserve_deliveries
from nonebot_plugin_moellmchats.tool_contracts import ToolContext
from nonebot_plugin_moellmchats.tool_manager import LlmToolExecutionRoute


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 3200)
    payload = buffer.getvalue()
    (tmp_path / "sample.wav").write_bytes(payload)
    clips = [
        {"title": title, "text": text, "file": "sample.wav", "sha256": hashlib.sha256(payload).hexdigest()}
        for title, text in [
            ("初次见面…", "我是七七，是个僵尸…啊，还要说什么来着。"),
            ("晚安…", "你先休息吧，我该去做柔软体操了…不要偷看。"),
            ("关于七七·闲聊", "咦，刚刚…我想说什么来着…"),
        ]
    ]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"character": "七七", "language": "zh-CN", "clips": clips}, ensure_ascii=False))
    monkeypatch.setattr(speech.speech_config, "moellm_speech_library", str(path))
    settings = {"speech_ping_rate": 1, "speech_enabled": True, "speech_auto_enabled": True, "cd_seconds": 30}
    monkeypatch.setattr(speech.config_parser, "_config", settings)
    monkeypatch.setattr(library.random, "random", lambda: 0.5)
    monkeypatch.setattr(request_budget, "budget_authorized", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_policy, "enforce_conduct", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_runtime, "acknowledge_mention", AsyncMock())
    monkeypatch.setattr(chat_runtime, "complete_mention", AsyncMock())
    monkeypatch.setattr(chat_runtime, "default_cooldown_store", MemoryCooldownStore({}))
    monkeypatch.setattr(slots, "conversation_slots", slots.ConversationSlots())
    return SimpleNamespace(settings=settings, path=path, clips=clips)


def context(message=None, group=456):
    original = message if message is not None else Message(MessageSegment.at(10000))
    event = SimpleNamespace(user_id=12345, group_id=group, original_message=original, message=Message(), reply=None)
    bot = SimpleNamespace(self_id="10000", adapter=SimpleNamespace(get_name=lambda: "OneBot V11"), send=AsyncMock())
    return ToolContext(bot, event)


@pytest.mark.asyncio
async def test_catalog_and_exact_normalized_title_lookup():
    ctx = context()
    catalog = await library.qiqi_voice(_tool_context=ctx)
    assert catalog.structured["total"] == 3
    assert len(catalog.structured["titles"]) == 3
    line = await library.qiqi_voice(_tool_context=ctx, query="晚安")
    assert line.structured["lines"][0]["text"].startswith("你先休息吧")
    assert line.structured["matches"] == 1
    ctx.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyword_search_and_ambiguous_play_do_not_send():
    ctx = context()
    found = await library.qiqi_voice(_tool_context=ctx, query="僵尸")
    assert found.structured["titles"] == ("初次见面…",)
    ambiguous = await library.qiqi_voice(_tool_context=ctx, action="play")
    assert ambiguous.metadata["status"] == "selection_required"
    ctx.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_playback_suppresses_further_random_synthesis():
    ctx = context()
    with speech.speech_scope():
        result = await library.qiqi_voice(_tool_context=ctx, query="晚安", action="play")
        assert result.metadata["status"] == "sent"
        assert speech._speech_state.get()["sent"]
    assert ctx.bot.send.await_args.args[1].type == "record"
    assert ctx.bot.send.await_args.args[1].data["file"].startswith("base64://")


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["path", "checksum", "missing"])
async def test_invalid_audio_never_sends(setup, damage):
    data = json.loads(setup.path.read_text())
    clip = data["clips"][0]
    if damage == "path":
        clip["file"] = "../private.wav"
    elif damage == "checksum":
        clip["sha256"] = "0" * 64
    else:
        clip["file"] = "absent.wav"
    setup.path.write_text(json.dumps(data))
    ctx = context()
    result = await library.qiqi_voice(_tool_context=ctx, query="初次见面", action="play")
    assert result.metadata["status"] == "failed"
    ctx.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_send_uncertainty_propagates_without_replay():
    ctx = context()
    ctx.bot.send.side_effect = NetworkError("uncertain")
    with pytest.raises(NetworkError):
        await library.qiqi_voice(_tool_context=ctx, query="晚安", action="play")
    assert ctx.bot.send.await_count == 1


@pytest.mark.parametrize(
    "message",
    [
        Message(MessageSegment.at(10000) + "你好"),
        Message(MessageSegment.at(10000) + MessageSegment.image("https://example.com/a.png")),
        Message(MessageSegment.at(10000) + MessageSegment.at(20000)),
        Message(""),
    ],
)
def test_only_empty_self_mentions_are_random_voice_candidates(message):
    ctx = context(message)
    assert not library.is_bare_mention(ctx.bot, ctx.event)


@pytest.mark.asyncio
async def test_bare_mention_uses_local_voice_and_shared_cross_group_cooldown():
    ctx = context()
    matcher = SimpleNamespace(finish=AsyncMock(side_effect=FinishedException))
    assert await library.try_ping_voice(ctx.bot, ctx.event, matcher)
    assert ctx.bot.send.await_count == 1
    ctx.event.group_id = 567
    with pytest.raises(FinishedException):
        await library.try_ping_voice(ctx.bot, ctx.event, matcher)
    assert ctx.bot.send.await_count == 1
    assert "30 秒" in matcher.finish.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_bare_mention_completion_waits_for_voice_receipt(confirmed):
    ctx = context()
    steps = []

    async def send(*_args, **_kwargs):
        steps.append("voice")
        return {"message_id": "789"} if confirmed else None

    ctx.bot.send.side_effect = send
    chat_runtime.complete_mention.side_effect = lambda *_args: steps.append("completed")
    assert await library.try_ping_voice(ctx.bot, ctx.event, SimpleNamespace())
    assert steps == (["voice", "completed"] if confirmed else ["voice"])


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["speech_enabled", "speech_auto_enabled", "speech_ping_rate"])
async def test_switches_and_zero_probability_stop_random_voice(setup, key):
    setup.settings[key] = 0 if key.endswith("rate") else False
    ctx = context()
    assert not await library.try_ping_voice(ctx.bot, ctx.event, SimpleNamespace())
    ctx.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_account_and_blocked_user_do_not_receive_random_voice(monkeypatch):
    ctx = context()
    matcher = SimpleNamespace(finish=AsyncMock(side_effect=FinishedException))
    async with slots.conversation_slots.slot(ctx.event.user_id):
        with pytest.raises(FinishedException):
            await library.try_ping_voice(ctx.bot, ctx.event, matcher)
    assert "正在生成回复" in matcher.finish.await_args.args[0]
    ctx.bot.send.assert_not_awaited()
    monkeypatch.setattr(chat_policy, "enforce_conduct", AsyncMock(return_value=False))
    assert await library.try_ping_voice(ctx.bot, ctx.event, matcher)
    ctx.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_group_cannot_receive_random_voice(setup):
    setup.settings["group_chat_policy"] = {"default_enabled": False, "groups": {}}
    ctx = context()
    assert not await library.try_ping_voice(ctx.bot, ctx.event, SimpleNamespace())
    ctx.bot.send.assert_not_awaited()


def test_voice_lookup_does_not_consume_ordinary_delivery_but_playback_does():
    target = SimpleNamespace(
        _verified_is_superuser=lambda: False,
        tool_snapshot=SimpleNamespace(
            resolve_llm_tool_execution=lambda *args, **kwargs: SimpleNamespace(route=LlmToolExecutionRoute.CUSTOM_TOOL)
        ),
    )

    def call(number, action, query):
        return {
            "id": str(number),
            "function": {"name": "qiqi_voice", "arguments": json.dumps({"action": action, "query": query})},
        }

    with request_budget.request_budget_scope(authorized=False):
        assert reserve_deliveries(target, [call(1, "search", "")]) == {}
        assert not hasattr(target, "_ordinary_delivery_reserved")
        assert reserve_deliveries(target, [call(2, "play", "晚安")]) == {}
        assert set(reserve_deliveries(target, [call(3, "play", "初次见面")])) == {"3"}

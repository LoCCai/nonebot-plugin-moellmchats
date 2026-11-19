from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from aiohttp import web
from nonebot.adapters.onebot.v11.exception import NetworkError
from pydantic import SecretStr
import pytest

from nonebot_plugin_moellmchats import speech_library as library
from nonebot_plugin_moellmchats import speech_tools as speech
from nonebot_plugin_moellmchats import usage_ledger, usage_reports
from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.moe_llm import MoeLlm
from nonebot_plugin_moellmchats.tool_contracts import ToolContext
from nonebot_plugin_moellmchats.tool_providers import (
    BuiltinToolResources,
    ProviderDiscoveryContext,
    ProviderDiscoveryPlan,
    ProviderRegistration,
    ProviderRegistry,
    RegisteredToolResources,
    ToolTrustPolicyError,
    builtin_tool_provider,
    registered_tool_provider,
)


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 3200)
    return buffer.getvalue()


async def speech_catalog(specs, provider=registered_tool_provider):
    registry = ProviderRegistry(
        tuple(ProviderRegistration.from_provider(p) for p in (registered_tool_provider, builtin_tool_provider))
    )
    registered = specs if provider is registered_tool_provider else ()
    builtins = builtin_tool_specs() + (specs if provider is builtin_tool_provider else ())
    return await registry.discover(
        1,
        tuple(
            ProviderDiscoveryPlan(provider=p, context=ProviderDiscoveryContext(generation=1, resources=resources))
            for p, resources in (
                (registered_tool_provider, RegisteredToolResources(registered)),
                (builtin_tool_provider, BuiltinToolResources(builtins)),
            )
        ),
    )


@pytest.mark.asyncio
async def test_actual_speech_startup_builds_provider_catalog(monkeypatch):
    monkeypatch.setattr(speech.tool_registry, "_tools", {})
    monkeypatch.setattr(speech.speech_config, "moellm_speech_library", "synthetic-library.json")
    monkeypatch.setattr(speech, "get_ledger", lambda: SimpleNamespace(put_prices=lambda _: None))
    await speech.register_speech_tools()
    await library.register_qiqi_voice_tools()
    catalog = await speech_catalog(tuple(speech.tool_registry._tools.values()))
    for name in ("speak_text", "qiqi_voice"):
        policy = catalog.trust_policy_for(name)
        assert not policy.confirmation_required
        assert policy.spec.policy.effective_v2.bot_send
        assert not policy.spec.policy.effective_v2.bot_manage


@pytest.mark.asyncio
@pytest.mark.parametrize("spec", [speech.SPEAK_TOOL, library.QIQI_VOICE_TOOL])
@pytest.mark.parametrize("provider", [registered_tool_provider, builtin_tool_provider])
async def test_speech_name_and_copied_spec_cannot_grant_bot_access(spec, provider):
    with pytest.raises(ToolTrustPolicyError, match="Bot capability"):
        await speech_catalog((replace(spec),), provider)


@pytest.mark.asyncio
async def test_speech_executor_rejects_replaced_handler():
    from nonebot_plugin_moellmchats.tool_execution import ToolExecutionError, execute_custom_tool

    forged = AsyncMock()
    entry = {**speech.SPEAK_TOOL.as_legacy_schema(), "func": forged}
    with pytest.raises(ToolExecutionError, match="二阶段确认"):
        await execute_custom_tool("speak_text", entry, {"text": "测试"}, bot=None, event=None)
    forged.assert_not_awaited()


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(speech.speech_config, "mi_api_key", SecretStr("test-key"))
    monkeypatch.setattr(speech.speech_config, "moellm_speech_reference", "")
    monkeypatch.setattr(speech.store, "get_plugin_data_dir", lambda: tmp_path)
    monkeypatch.setattr(speech, "_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(speech, "_reference_lock", asyncio.Lock())
    monkeypatch.setattr(speech, "record_usage", AsyncMock())
    settings = {"speech_auto_rate": 1.0}
    monkeypatch.setattr(speech.config_parser, "_config", settings)
    monkeypatch.setattr(speech.config_parser, "get_config", lambda key, default=None: settings.get(key, default))
    return settings


def chat(text="你好"):
    bot = SimpleNamespace(adapter=SimpleNamespace(get_name=lambda: "OneBot V11"), send=AsyncMock())
    return SimpleNamespace(bot=bot, event=object(), format_message_dict={"text": [text]}, _remaining_request_seconds=lambda: 30)


@pytest.mark.parametrize("kind", ["tts", "voiceclone", "voicedesign"])
def test_payload_matches_mimo_contract(kind):
    payload = speech.build_payload("你好……", kind, reference=wav())
    assert payload["model"] == speech.MODELS[kind]
    assert payload["messages"][-1] == {"role": "assistant", "content": "你好……"}
    assert payload["audio"]["format"] == "wav"
    assert payload["stream"] is False
    if kind == "voiceclone":
        assert payload["audio"]["voice"].startswith("data:audio/wav;base64,")
    elif kind == "tts":
        assert payload["audio"]["voice"] == "Chloe"
    else:
        assert payload["audio"]["optimize_text_preview"] is True


def test_sing_payload_prepends_official_tag_with_preset_voice():
    payload = speech.build_payload("一闪一闪亮晶晶", "sing")
    assert payload["model"] == "mimo-v2.5-tts"
    assert payload["messages"] == [{"role": "assistant", "content": "(唱歌)一闪一闪亮晶晶"}]
    assert payload["audio"]["voice"] == "茉莉"
    assert payload["audio"]["format"] == "wav"


def test_sing_payload_strips_model_supplied_tags_and_moves_style_to_user():
    payload = speech.build_payload("<style>唱歌</style>小星星", "sing", style="欢快地唱")
    assert payload["messages"] == [{"role": "user", "content": "欢快地唱"}, {"role": "assistant", "content": "(唱歌)小星星"}]
    payload = speech.build_payload("（唱歌）小星星", "sing")
    assert payload["messages"] == [{"role": "assistant", "content": "(唱歌)小星星"}]


@pytest.mark.parametrize("text", ["七七唱一段小星星", "给我唱首歌", "清唱两句", "哼唱一下", "来一首生日快乐", "唱个歌听听"])
def test_sing_intent_detected(text):
    assert speech.sing_requested(text)


@pytest.mark.parametrize("text", ["不要唱了", "别唱这首歌", "不想听你唱歌", "说唱是什么风格", "朗读这段话"])
def test_sing_intent_not_detected(text):
    assert not speech.sing_requested(text)


@pytest.mark.asyncio
async def test_sing_kind_skips_reference_and_reads_as_singing(monkeypatch):
    calls = []

    async def request(payload, *, timeout_seconds=None):
        calls.append(payload)
        return wav()

    reference = AsyncMock()
    monkeypatch.setattr(speech, "_request_audio", request)
    monkeypatch.setattr(speech, "_reference", reference)
    assert await speech.synthesize("小星星，亮晶晶", kind="sing") == wav()
    reference.assert_not_awaited()
    assert calls[0]["model"] == "mimo-v2.5-tts"
    assert calls[0]["messages"][-1]["content"] == "(唱歌)小星星，亮晶晶"


def _audio_response():
    return web.json_response({"choices": [{"message": {"audio": {"data": base64.b64encode(wav()).decode()}}}]})


async def _start_stub(monkeypatch, handler):
    app = web.Application()
    app.router.add_post("/chat", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(speech, "ENDPOINT", f"http://127.0.0.1:{port}/chat")
    return runner


@pytest.mark.asyncio
async def test_kind_budget_reaches_the_http_layer(monkeypatch):
    # A slow upstream: the speaking budget times out, the sing budget survives.
    monkeypatch.setattr(speech, "_RETRY_BACKOFF_SECONDS", 0.05)
    monkeypatch.setattr(speech.speech_config, "moellm_speech_timeout_seconds", 0.4)
    monkeypatch.setattr(speech.speech_config, "moellm_sing_timeout_seconds", 4.0)

    async def handler(request):
        await asyncio.sleep(0.8)
        return _audio_response()

    runner = await _start_stub(monkeypatch, handler)
    try:
        with pytest.raises((speech.SpeechError, TimeoutError)):
            await speech.synthesize("你好呀", kind="tts")
        assert await speech.synthesize("一闪一闪亮晶晶", kind="sing") == wav()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_transient_http_failure_is_retried_once(monkeypatch):
    monkeypatch.setattr(speech, "_RETRY_BACKOFF_SECONDS", 0.01)
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return web.json_response({"error": "rate limited"}, status=429)
        return _audio_response()

    runner = await _start_stub(monkeypatch, handler)
    try:
        assert await speech.synthesize("你好", kind="tts") == wav()
        assert len(calls) == 2
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_permanent_http_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(speech, "_RETRY_BACKOFF_SECONDS", 0.01)
    calls = []

    async def handler(request):
        calls.append(1)
        return web.json_response({"error": "bad text"}, status=400)

    runner = await _start_stub(monkeypatch, handler)
    try:
        with pytest.raises(speech.SpeechError, match="HTTP 400"):
            await speech.synthesize("你好", kind="tts")
        assert len(calls) == 1
        error = speech.SpeechError("x")
        assert error.transient is False
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_speak_text_failure_hint_discourages_repeated_calls(monkeypatch):
    monkeypatch.setattr(
        speech, "synthesize", AsyncMock(side_effect=speech.SpeechError("语音服务连接超时或中断，请稍后再试。", transient=True))
    )
    c = chat("唱一段小星星")
    result = await speech.speak_text(_tool_context=ToolContext(c.bot, c.event), text="小星星", model="sing")
    assert result.metadata["status"] == "failed"
    assert "已自动重试一次" in result.text
    assert "不要在本轮连续重调" in result.text


@pytest.mark.parametrize("text", ["我想听你说话", "七七，用语音说晚安", "发个语音", "朗读这段话", "听听你的声音"])
def test_explicit_voice_intent(text):
    assert speech.voice_requested(text)


@pytest.mark.parametrize("text", ["不要用语音", "不想听你说话", "查询语音模型消耗", "造梦排行榜"])
def test_other_intents_stay_text(text):
    assert not speech.voice_requested(text)


@pytest.mark.asyncio
async def test_reference_is_designed_once_and_shared_by_concurrent_calls(monkeypatch):
    calls = []

    async def request(payload, *, timeout_seconds=None):
        calls.append(payload)
        await asyncio.sleep(0)
        return wav()

    monkeypatch.setattr(speech, "_request_audio", request)
    await asyncio.gather(speech.synthesize("早安"), speech.synthesize("晚安"))
    assert [row["model"] for row in calls].count(speech.MODELS["voicedesign"]) == 1
    assert [row["model"] for row in calls].count(speech.MODELS["voiceclone"]) == 2
    assert speech.reference_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_missing_key_and_busy_fail_without_network(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(speech, "_request_audio", request)
    monkeypatch.setattr(speech.speech_config, "mi_api_key", SecretStr(""))
    with pytest.raises(speech.SpeechError, match="mi_api_key"):
        await speech.synthesize("你好")
    monkeypatch.setattr(speech.speech_config, "mi_api_key", SecretStr("test-key"))
    async with speech._slots, speech._slots:
        with pytest.raises(speech.SpeechError, match="正在忙"):
            await speech.synthesize("你好")
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_http_shape_records_usage_without_audio_or_key(monkeypatch):
    async def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        body = await request.json()
        assert body["model"] == "mimo-v2.5-tts"
        return web.json_response(
            {
                "choices": [{"message": {"audio": {"data": base64.b64encode(wav()).decode()}}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }
        )

    app = web.Application()
    app.router.add_post("/chat", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(speech, "ENDPOINT", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/chat")
    try:
        assert await speech.synthesize("你好", kind="tts") == wav()
        records = speech.record_usage.await_args_list
        assert len(records) == 2
        assert records[-1].args[1] == {"prompt_tokens": 12, "completion_tokens": 8}
        assert records[0].kwargs["attempt_id"] == records[1].kwargs["attempt_id"]
        assert records[1].kwargs["purpose"] == "speech"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_voice_tool_sends_standard_record_and_suppresses_auto_voice(monkeypatch):
    monkeypatch.setattr(speech, "synthesize", AsyncMock(return_value=wav()))
    c = chat("用语音说你好")
    with speech.speech_scope():
        # Tool execution may run in child tasks; state must reach the parent request.
        result = await asyncio.create_task(speech.speak_text(_tool_context=ToolContext(c.bot, c.event), text="你好"))
        assert result.metadata["status"] == "sent"
        assert not await speech.try_voice_reply(c, "好啦")
    segment = c.bot.send.await_args.args[1]
    assert segment.type == "record"
    assert segment.data["file"].startswith("base64://")
    assert c.bot.send.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "/造梦资料 我的",
        "复制 /造梦蟠桃",
        "|表格|",
        "$x^2$",
        "x=2",
        "[开心]早安",
        "```py\nx\n```",
        "https://example.com",
        "长" * 90,
    ],
)
async def test_copyable_and_structured_answers_never_become_random_voice(monkeypatch, content):
    synthesize = AsyncMock()
    monkeypatch.setattr(speech, "synthesize", synthesize)
    assert not await speech.try_voice_reply(chat(), content)
    synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_probability_and_explicit_voice_ignore_probability(monkeypatch, isolate):
    synthesize = AsyncMock(return_value=wav())
    monkeypatch.setattr(speech, "synthesize", synthesize)
    isolate["speech_auto_rate"] = 0
    assert not await speech.try_voice_reply(chat(), "你好")
    assert await speech.try_voice_reply(chat("我想听你说话"), "七七在……听到了。")
    synthesize.assert_awaited_once()


@pytest.mark.asyncio
async def test_synthesis_failure_falls_back_through_existing_text_delivery(monkeypatch):
    monkeypatch.setattr(speech, "synthesize", AsyncMock(side_effect=speech.SpeechError("服务繁忙")))
    c = chat("我想听你说话")
    c.emotion_flag = False
    result = await MoeLlm.send_emotion_message(c, "七七在")
    assert result == "七七在"
    assert c.bot.send.await_count == 1
    assert "七七在" in str(c.bot.send.await_args.args[1])


@pytest.mark.asyncio
async def test_uncertain_voice_send_does_not_fall_back_or_retry(monkeypatch):
    monkeypatch.setattr(speech, "synthesize", AsyncMock(return_value=wav()))
    c = chat("我想听你说话")
    c.bot.send.side_effect = NetworkError("lost response")
    with pytest.raises(NetworkError):
        await MoeLlm.send_emotion_message(c, "你好")
    assert c.bot.send.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_synthesis_propagates_and_releases_slot(monkeypatch):
    monkeypatch.setattr(speech, "_request_audio", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await speech.synthesize("你好", kind="tts")
    assert not speech._slots.locked()


@pytest.mark.asyncio
async def test_startup_registers_user_tool_and_limited_free_prices(monkeypatch):
    prices = {}
    monkeypatch.setattr(speech, "get_ledger", lambda: SimpleNamespace(put_prices=lambda rows: prices.update(rows)))
    monkeypatch.setattr(speech.tool_registry, "_tools", {})
    await speech.register_speech_tools()
    assert speech.tool_registry.get("speak_text") is speech.SPEAK_TOOL
    assert speech.SPEAK_TOOL.permission == "user"
    assert set(prices) == {f"{name} (MiMo)" for name in speech.MODELS.values()}
    assert all(row["input"] == "0" and row["currency"] == "CNY" for row in prices.values())
    monkeypatch.setattr(speech.speech_config, "moellm_speech_free_pricing", False)
    await speech.register_speech_tools()
    assert all("input" not in row for row in prices.values())


@pytest.mark.asyncio
async def test_saved_voice_during_shutdown_is_not_reported_as_sent(monkeypatch):
    monkeypatch.setattr(speech, "synthesize", AsyncMock(return_value=wav()))
    monkeypatch.setattr(speech, "durable_send", AsyncMock(return_value=None))
    c = chat()
    result = await speech.speak_text(_tool_context=ToolContext(c.bot, c.event), text="你好")
    assert result.metadata["status"] == "queued"


@pytest.mark.asyncio
async def test_disabled_switch_blocks_all_models_before_network(monkeypatch, isolate):
    from nonebot_plugin_moellmchats.config import ConfigParser
    from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot, runtime_snapshots

    # Existing requests retain enabled snapshots, but the administrative switch wins.
    old = RuntimeSnapshot(
        generation=1,
        config={"speech_enabled": True},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=0,
    )
    monkeypatch.setattr(speech.config_parser, "get_config", ConfigParser.get_config.__get__(speech.config_parser))
    isolate["speech_enabled"] = False
    request = AsyncMock()
    monkeypatch.setattr(speech, "_request_audio", request)
    with runtime_snapshots.bind(old):
        assert speech.config_parser.get_config("speech_enabled") is True
        for kind in ("auto", *speech.MODELS):
            with pytest.raises(speech.SpeechError, match="未启用"):
                await speech.synthesize("你好", kind=kind)
        assert not await speech.try_voice_reply(chat("用语音说你好"), "你好")
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["tool", "explicit", "random"])
async def test_close_during_synthesis_prevents_voice_send(monkeypatch, isolate, entry):
    async def synthesize(*args, **kwargs):
        isolate["speech_enabled"] = False
        return wav()

    monkeypatch.setattr(speech, "synthesize", synthesize)
    c = chat("用语音说你好" if entry == "explicit" else "你好")
    if entry == "tool":
        result = await speech.speak_text(_tool_context=ToolContext(c.bot, c.event), text="你好")
        assert result.metadata["status"] == "failed"
    else:
        assert not await speech.try_voice_reply(c, "你好")
    c.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_random_switch_preserves_explicit_voice(monkeypatch, isolate):
    isolate["speech_auto_enabled"] = False
    synthesize = AsyncMock(return_value=wav())
    monkeypatch.setattr(speech, "synthesize", synthesize)
    assert not await speech.try_voice_reply(chat(), "你好")
    synthesize.assert_not_awaited()
    assert await speech.try_voice_reply(chat("用语音说你好"), "你好")


@pytest.mark.asyncio
async def test_random_switch_closure_during_synthesis_keeps_text(monkeypatch, isolate):
    async def synthesize(*args, **kwargs):
        isolate["speech_auto_enabled"] = False
        return wav()

    monkeypatch.setattr(speech, "synthesize", synthesize)
    c = chat()
    assert not await speech.try_voice_reply(c, "你好")
    c.bot.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_startup_can_enable_without_reregistering(monkeypatch, isolate):
    isolate["speech_enabled"] = False
    monkeypatch.setattr(speech, "get_ledger", lambda: SimpleNamespace(put_prices=lambda rows: None))
    monkeypatch.setattr(speech.tool_registry, "_tools", {})
    await speech.register_speech_tools()
    assert speech.tool_registry.get("speak_text") is speech.SPEAK_TOOL
    isolate["speech_enabled"] = True
    monkeypatch.setattr(speech, "_request_audio", AsyncMock(return_value=wav()))
    assert await speech.synthesize("你好", kind="tts") == wav()


@pytest.mark.asyncio
async def test_three_models_reach_existing_reports_with_user_group_and_cache(monkeypatch, tmp_path):
    ledger = usage_ledger.UsageLedger(tmp_path / "usage.sqlite3")
    monkeypatch.setattr(usage_ledger, "get_ledger", lambda: ledger)
    monkeypatch.setattr(usage_reports, "get_ledger", lambda: ledger)
    monkeypatch.setattr(speech, "get_ledger", lambda: ledger)
    monkeypatch.setattr(speech, "record_usage", usage_ledger.record_usage)
    await speech.register_speech_tools()

    async def handler(request):
        body = await request.json()
        result = {"choices": [{"message": {"audio": {"data": base64.b64encode(wav()).decode()}}}]}
        if body["model"] != speech.MODELS["voicedesign"]:
            result["usage"] = {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "prompt_tokens_details": {"cached_tokens": 4},
                "completion_tokens_details": {"audio_tokens": 8},
            }
        return web.json_response(result)

    app = web.Application()
    app.router.add_post("/chat", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(speech, "ENDPOINT", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/chat")
    try:
        with usage_ledger.bind_usage_scope(
            SimpleNamespace(self_id="10000"), SimpleNamespace(group_id=456, get_user_id=lambda: "12345")
        ):
            for kind in speech.MODELS:
                await speech._request_audio(speech.build_payload("你好", kind, reference=wav()))
        data = await asyncio.to_thread(usage_reports.report_data, "全部 群 456 用户 12345")
        # tts and sing share the preset-voice model, so four calls over three models.
        assert data["calls"] == 4
        assert data["unknown"] == 1
        assert set(data["raw_model_calls"]) == set(speech.MODELS.values())
        assert data["total"] == 60
        assert data["cache"] == 12
        assert data["cost_cny"] == 0
        assert data["users"] == {"12345"}
        assert data["groups"] == {"456"}
        rows = ledger.rows()
        assert {row["purpose"] for row in rows} == {"speech"}
        assert len({row["request_id"] for row in rows}) == 1
        assert all(row["provider"] == "MiMo" for row in rows)
        assert '"audio_tokens": 8' in next(row["raw_usage"] for row in rows if row["usage_known"])
    finally:
        await runner.cleanup()

@pytest.mark.asyncio
async def test_managed_speech_uses_only_host_transport(monkeypatch):
    from nonebot_plugin_moellmchats import personal_billing
    async def host(payload, *, timeout_seconds):
        assert payload['model'] == 'mimo-v2.5-tts'
        raise RuntimeError('denied')
    monkeypatch.setattr(personal_billing,'speech_transport',lambda:host)
    with pytest.raises(speech.SpeechError, match='渠道'):
        await speech._request_audio({'model':'mimo-v2.5-tts'},timeout_seconds=5)

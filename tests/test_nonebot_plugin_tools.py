from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
import io
import random

import pytest


@pytest.fixture(autouse=True)
def enabled_legacy_image_feedback(monkeypatch):
    from nonebot_plugin_moellmchats.config import config_parser
    original = config_parser.get_config
    monkeypatch.setattr(config_parser, 'get_config', lambda key, default=None:
                        False if key == 'skip_self_image_vision' else original(key, default))

from nonebot_plugin_moellmchats import nonebot_plugin_tools as module
from nonebot_plugin_moellmchats.event_simulator import (
    PluginDispatchResult,
    PluginDispatchStatus,
)
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    PluginDispatchError,
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.tool_contracts import TOOL_RESULT_MAX_TEXT_CHARS, ToolEffect, ToolResult
from nonebot_plugin_moellmchats.tool_discovery import (
    COMPAT_COMMAND_PREFIXES_KEY,
)
from nonebot_plugin_moellmchats.tool_manager import ToolManager, tool_manager


@pytest.mark.asyncio
async def test_default_skip_switch_never_decodes_other_plugin_images(monkeypatch):
    from nonebot_plugin_moellmchats.config import DEFAULT_CONFIG, config_parser
    assert DEFAULT_CONFIG['skip_self_image_vision'] is True
    monkeypatch.setattr(config_parser, 'get_config', lambda key, default=None: DEFAULT_CONFIG.get(key, default))
    monkeypatch.setattr(module, '_normalize_dispatch_images', lambda *args: pytest.fail('must not decode'))
    receipt = PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
        text='已发送', images=('data:image/png;base64,not-decoded',), successful_captures=1)
    result = await module.plugin_dispatch_tool_result(receipt, plugin_name='other_plugin')
    assert not result.images and result.metadata['delivered_images'] == 1
    assert '已确认向用户发送图片 1 张' in result.text


@pytest.mark.asyncio
async def test_game_compatibility_dispatches_native_card_and_returns_text_without_images(monkeypatch):
    calls = []
    view = {'template': 'player.j2', 'text': '玩家甲 战力123456', 'render_id': 'a' * 32}
    async def dispatch(bot, event, command, source, **kwargs):
        calls.append((bot, event, command, source, kwargs))
        return PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text='[图片]', images=('https://example.org/card.png',), rendered_views=(view,),
            successful_captures=1, api_succeeded=1)
    monkeypatch.setattr(module.event_simulator, 'dispatch_event', dispatch)
    result = await module.execute_nonebot_plugin('/造梦资料', plugin_name='qi_zmws', bot='bot', event='event')
    assert calls == [('bot', 'event', '/造梦资料', None, {'plugin_name': 'qi_zmws', 'wait_for_result': True})]
    assert result.metadata['skip_image_vision']
    assert result.metadata['delivered_images'] == 1
    assert not result.metadata.get('data_only')
    assert list(result.structured['rendered_views']) == [view]
    assert result.images == ()


@pytest.mark.asyncio
async def test_game_receipt_never_decodes_images_even_without_render_evidence(monkeypatch):
    def forbidden(*args):
        pytest.fail('Game image previews must not be decoded')
    monkeypatch.setattr(module, '_normalize_dispatch_images', forbidden)
    dispatch = PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
        text='已发送游戏卡片', images=('data:image/png;base64,large',), successful_captures=1)
    result = await module.plugin_dispatch_tool_result(dispatch, plugin_name='qi_zmws')
    assert result.images == () and result.metadata['skip_image_vision']


@pytest.mark.asyncio
async def test_oversized_png_receipt_becomes_valid_preview_without_redispatch(monkeypatch):
    from nonebot_plugin_moellmchats.tool_contracts import TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS

    image_module = pytest.importorskip("PIL.Image")
    image = image_module.frombytes("RGB", (1100, 1100), random.Random(7).randbytes(1100 * 1100 * 3))
    output = io.BytesIO()
    image.save(output, format="PNG")
    original = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    assert len(original) > TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS
    with pytest.raises(ValueError, match=r"ToolResult\.images"):
        ToolResult(images=(original,))
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append(kwargs)
        return PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text="[图片]", images=(original,), matcher_matched=1,
            successful_captures=1, api_succeeded=1)

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    result = await module.execute_nonebot_plugin("/测试排行", plugin_name="demo", bot=object(), event=object())
    assert len(calls) == 1
    assert result.metadata["plugin_dispatch"]["status"] == "matched_with_output"
    assert result.metadata["output_image_normalization"]["resized"] == 1
    assert "已确认向用户发送图片 1 张" in result.text
    assert len(result.images[0]) < TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS
    with image_module.open(io.BytesIO(base64.b64decode(result.images[0].split(",", 1)[1]))) as decoded:
        assert decoded.size == (1100, 1100)


@pytest.mark.asyncio
async def test_missing_preview_decoder_preserves_confirmed_image_success(monkeypatch):
    from nonebot_plugin_moellmchats.tool_contracts import TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS

    monkeypatch.setattr(module, "_image_preview", lambda *args: None)
    dispatch = PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
        text="[图片]", images=("data:image/png;base64," + "A" * TOOL_RESULT_MAX_IMAGE_REFERENCE_CHARS,),
        matcher_matched=1, successful_captures=1, api_succeeded=1)
    result = await module.plugin_dispatch_tool_result(dispatch, plugin_name="demo")
    assert result.images == ()
    assert result.metadata["plugin_dispatch"]["status"] == "matched_with_output"
    assert result.metadata["output_image_normalization"]["omitted"] == 1
    assert "图片发送已成功，不要重复调用" in result.text


@pytest.mark.asyncio
async def test_multiple_image_receipts_obey_total_payload_bound():
    from nonebot_plugin_moellmchats.tool_contracts import TOOL_RESULT_MAX_PAYLOAD_BYTES

    reference = "https://example.org/" + "a" * 3_999_980
    dispatch = PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
        text="图片已发出", images=(reference,) * 6, successful_captures=6, api_succeeded=6)
    result = await module.plugin_dispatch_tool_result(dispatch, plugin_name="demo")
    assert result.metadata["output_image_normalization"]["confirmed_images"] == 6
    assert result.metadata["output_image_normalization"]["omitted"] > 0
    assert len(result.canonical_json().encode("utf-8")) < TOOL_RESULT_MAX_PAYLOAD_BYTES


@pytest.mark.asyncio
async def test_unsafe_preview_reference_does_not_undo_confirmed_send():
    dispatch = PluginDispatchResult(status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
        text="[图片]", images=("https://example.org/\x00x", "https://example.org/valid.png"),
        successful_captures=1, api_succeeded=1)
    result = await module.plugin_dispatch_tool_result(dispatch, plugin_name="demo")
    assert result.images == ("https://example.org/valid.png",)
    assert result.metadata["output_image_normalization"]["omitted"] == 1


def test_nonebot_plugin_candidate_is_detached_canonical_and_conservative() -> None:
    dependencies = ["helper_zeta", "helper_alpha", "helper_alpha"]
    raw = {
        "plugin_demo": {
            "name": "Demo",
            "description": "legacy demo plugin",
            "usage": "/demo <value>",
            "dependencies": dependencies,
        }
    }

    legacy, specs = build_nonebot_plugin_candidate(raw)
    spec = specs[0]
    dependencies.append("late_mutation")
    raw["plugin_demo"]["description"] = "drifted"

    assert tuple(legacy) == ("plugin_demo",)
    assert legacy["plugin_demo"]["description"] == "legacy demo plugin"
    assert legacy["plugin_demo"]["dependencies"] == [
        "helper_zeta",
        "helper_alpha",
        "helper_alpha",
    ]
    assert legacy["plugin_demo"]["source"] == "nonebot_plugin"
    assert legacy["plugin_demo"]["tool_spec"] is spec
    assert spec.name == "plugin_demo"
    assert spec.permission == "user"
    assert spec.effect is ToolEffect.MUTATING
    assert spec.dependencies == ("helper_alpha", "helper_zeta")
    assert spec.parameters["required"] == ("command",)
    assert "Demo" in spec.description
    assert "/demo <value>" in spec.description
    with pytest.raises(TypeError):
        spec.parameters["required"] = ()
    with pytest.raises(FrozenInstanceError):
        spec.permission = "superuser"


def test_nonebot_plugin_candidate_rejects_malformed_or_reserved_legacy_info() -> None:
    with pytest.raises(TypeError, match="plugin_info"):
        build_nonebot_plugin_candidate([])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="描述"):
        build_nonebot_plugin_candidate({"plugin_demo": []})
    with pytest.raises(ValueError, match="保留 Provider 字段"):
        build_nonebot_plugin_candidate(
            {"plugin_demo": {"source": "spoofed"}}
        )
    with pytest.raises(ValueError, match="保留 Provider 字段"):
        build_nonebot_plugin_candidate(
            {
                "plugin_demo": {
                    COMPAT_COMMAND_PREFIXES_KEY: ("!",),
                }
            }
        )
    with pytest.raises(ValueError, match="工具名"):
        build_nonebot_plugin_candidate(
            {"plugin.name": {"description": "invalid function name"}}
        )


@pytest.mark.asyncio
async def test_nonebot_plugin_adapter_uses_bounded_event_simulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = object()
    event = object()
    calls = []

    async def dispatch(
        received_bot,
        received_event,
        command,
        source,
        *,
        plugin_name,
        wait_for_result,
    ):
        assert wait_for_result is True
        calls.append(
            (received_bot, received_event, command, source, plugin_name)
        )
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_WITH_OUTPUT,
            text="visible plugin output",
            images=("image:one",),
            matcher_checked=2,
            matcher_matched=1,
            successful_captures=1,
            api_succeeded=1,
            duration_ms=12,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)

    _legacy, specs = build_nonebot_plugin_candidate(
        {"plugin_demo": {"description": "demo"}}
    )
    result = await specs[0].handler(
        "/demo",
        _bot=bot,
        _event=event,
        _format_message_dict={"mentions": [{"qq": 1}]},
    )

    assert result == ToolResult(
        text="visible plugin output",
        images=("image:one",),
        metadata={
            "plugin_dispatch": {
                "status": "matched_with_output",
                "matcher_checked": 2,
                "matcher_matched": 1,
                "matcher_failed": 0,
                "matcher_blocked": 0,
                "successful_captures": 1,
                "api_succeeded": 1,
                    "api_failed": 0,
                    "api_unknown": 0,
                    "api_read_failed": 0,
                    "api_read_recovered": 0,
                    "api_unresolved_failed": 0,
                    "api_unresolved_unknown": 0,
                    "mutating_api_succeeded": 0,
                "duration_ms": 12,
            }
        },
    )
    assert calls == [
        (
            bot,
            event,
            "/demo",
            {"mentions": [{"qq": 1}]},
            "plugin_demo",
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [PluginDispatchStatus.MATCHED_WITH_OUTPUT, PluginDispatchStatus.MATCHED_SIDE_EFFECT],
)
@pytest.mark.parametrize(
    ("text", "normalized", "changed_field"),
    [
        ("造梦\x00面板🙂", "造梦面板🙂", "nul_removed"),
        ("A\ud800B", "A?B", "surrogate_replaced"),
        ("甲" * (TOOL_RESULT_MAX_TEXT_CHARS + 80), None, "truncated"),
    ],
)
async def test_plugin_receipt_normalization_retains_success_without_redispatch(
    monkeypatch, status, text, normalized, changed_field,
):
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append(kwargs)
        return PluginDispatchResult(
            status=status, text=text, images=("image:one",), matcher_matched=1,
            successful_captures=1, api_succeeded=1, mutating_api_succeeded=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    result = await module.execute_nonebot_plugin("/demo", plugin_name="plugin_demo", bot=object(), event=object())
    assert len(calls) == 1
    assert result.metadata["plugin_dispatch"]["status"] == status.value
    assert result.metadata["plugin_dispatch"]["api_succeeded"] == 1
    assert result.metadata["output_text_normalization"][changed_field]
    if normalized is not None:
        assert result.text == normalized
    else:
        assert len(result.text) == TOOL_RESULT_MAX_TEXT_CHARS
        assert result.text.endswith("...[工具结果已截断]")
    assert "\x00" not in result.text
    result.text.encode("utf-8")
    if status is PluginDispatchStatus.MATCHED_WITH_OUTPUT:
        assert result.images == ("image:one",)


@pytest.mark.asyncio
async def test_normalized_partial_receipt_still_blocks_retry(monkeypatch):
    calls = []

    async def dispatch(*args, **kwargs):
        calls.append(kwargs)
        return PluginDispatchResult(
            status=PluginDispatchStatus.PARTIAL_SUCCESS, text="已完成\x00第一步",
            matcher_matched=1, api_succeeded=1, api_unknown=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    with pytest.raises(PluginDispatchError) as captured:
        await module.execute_nonebot_plugin("/demo", plugin_name="plugin_demo", bot=object(), event=object())
    assert len(calls) == 1
    assert captured.value.result.status is PluginDispatchStatus.PARTIAL_SUCCESS
    assert captured.value.result.text == "已完成第一步"
    assert "不要再次调用" in str(captured.value)
    assert "\x00" not in str(captured.value)
    with pytest.raises(ValueError, match=r"ToolResult\.text"):
        ToolResult(text="仍需拒绝\x00非法工具结果")


def test_nonebot_plugin_legacy_schema_uses_canonical_spec_without_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "name": "Demo",
                "description": "legacy demo plugin",
                "usage": "/demo",
            }
        }
    )
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda _name: False)

    schemas = ToolManager.build_tool_schema(
        ["plugin_demo"],
        plugin_info=legacy,
        custom_tools={},
    )

    assert schemas[0]["function"] == {
        "name": "plugin_demo",
        "description": specs[0].description,
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "严格根据该插件的原始用法说明和菜单功能提示，"
                        "生成可以直接触发该插件的机器人指令字符串。"
                        "原消息中的 @ 对象必须保留为 [at:1]、[at:2] 等身份占位符，"
                        "不要改成昵称、QQ号，也不要省略；显式输入的玩家 UID 保持原样，按原顺序排列。"
                        "[at:N]仅限当前消息；查询历史玩家时使用本轮提供的[history_at:...]映射，"
                        "没有身份映射就请用户补充真实@或UID，不能复用旧[at:N]或猜身份。"
                    ),
                    "minLength": 1,
                    "maxLength": 1024,
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    }
    schemas[0]["function"]["parameters"]["required"].clear()
    assert specs[0].parameters["required"] == ("command",)


def test_nonebot_plugin_schema_keeps_generation_frozen_command_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "name": "Demo",
                "usage": "<命令前缀>demo",
            }
        },
        command_prefixes=("##", "!"),
    )
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda _name: False)

    schemas = ToolManager.build_tool_schema(
        ["plugin_demo"],
        plugin_info=legacy,
        custom_tools={},
    )

    description = schemas[0]["function"]["description"]
    assert description == specs[0].description
    assert "!demo" in description
    assert "当前首选命令前缀为 '!'" in description
    assert "其他有效前缀：'##'" in description
    assert "<命令前缀>" not in description


@pytest.mark.asyncio
async def test_nonebot_plugin_adapter_preserves_background_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy, specs = build_nonebot_plugin_candidate(
        {"demo": {"description": "资料"}}
    )
    notice = "任务仍在后台运行，资料将稍后发送，无需再次调用。"

    async def dispatch(*_args, **_kwargs):
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_SIDE_EFFECT,
            text=notice,
            matcher_matched=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", dispatch)
    result = await specs[0].handler(
        "/造梦资料 我的 完整", _bot=object(), _event=object()
    )
    assert result.text == notice
    assert "副作用动作" not in result.text


@pytest.mark.asyncio
async def test_nonebot_plugin_adapter_accepts_only_verified_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy, specs = build_nonebot_plugin_candidate(
        {"plugin_demo": {"description": "demo"}}
    )

    async def side_effect(*_args, **_kwargs):
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_SIDE_EFFECT,
            matcher_checked=1,
            matcher_matched=1,
            api_succeeded=1,
            mutating_api_succeeded=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", side_effect)
    result = await specs[0].handler(
        "/demo", _bot=object(), _event=object()
    )
    assert "副作用动作" in result.text
    assert result.metadata["plugin_dispatch"]["status"] == (
        "matched_side_effect"
    )

    async def empty(*_args, **_kwargs):
        return PluginDispatchResult(
            status=PluginDispatchStatus.MATCHED_EMPTY,
            matcher_checked=1,
            matcher_matched=1,
        )

    monkeypatch.setattr(module.event_simulator, "dispatch_event", empty)
    with pytest.raises(PluginDispatchError) as captured:
        await specs[0].handler("/demo", _bot=object(), _event=object())
    assert captured.value.result.status is PluginDispatchStatus.MATCHED_EMPTY


@pytest.mark.parametrize(
    ("prefixes", "preferred", "alternatives"),
    [
        (("!", "/"), "/帮助", "'!'"),
        (("!!", "!"), "!帮助", "'!!'"),
        (("",), "帮助", None),
    ],
)
def test_nonebot_plugin_description_uses_real_command_start(
    prefixes: tuple[str, ...],
    preferred: str,
    alternatives: str | None,
) -> None:
    _legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_demo": {
                "description": "demo",
                "usage": "<命令前缀>帮助",
            }
        },
        command_prefixes=prefixes,
    )
    description = specs[0].description
    assert preferred in description
    assert "<命令前缀>" not in description
    if alternatives is not None:
        assert alternatives in description

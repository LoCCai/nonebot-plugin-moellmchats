from types import SimpleNamespace

from nonebot_plugin_moellmchats.config import config_parser
from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin
from nonebot_plugin_moellmchats.model_selector import model_selector


class PayloadHarness(LlmPayloadMixin):
    pass


def test_payload_delegates_tool_view_to_generation_snapshot(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    schema = [
        {
            "type": "function",
            "function": {
                "name": "required_tool",
                "description": "required tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    class Snapshot:
        def get_llm_payload_tools(
            self,
            plugin_names,
            *,
            tools_enabled,
            search_enabled,
            is_superuser,
        ):
            calls.append(
                {
                    "plugin_names": set(plugin_names),
                    "tools_enabled": tools_enabled,
                    "search_enabled": search_enabled,
                    "is_superuser": is_superuser,
                }
            )
            return {"required_tool", "resident_tool"}, list(schema)

    monkeypatch.setattr(
        model_selector,
        "get_resident_plugins",
        lambda: ["resident_tool"],
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: True)
    harness = PayloadHarness()
    harness.model_info = {"model": "test-model", "stream": True}
    harness.messages_handler = SimpleNamespace(current_images=[])
    harness.format_message_dict = {}
    harness.required_plugins = ["required_tool"]
    harness.tool_snapshot = Snapshot()
    harness.is_superuser = True
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    data, stream = harness._build_payload(messages)

    assert calls == [
        {
            "plugin_names": {"required_tool", "resident_tool"},
            "tools_enabled": True,
            "search_enabled": True,
            "is_superuser": True,
        }
    ]
    assert data["tools"] == schema
    assert harness._active_llm_tool_names == frozenset({"required_tool"})
    assert harness._active_llm_tool_descriptions == {"required_tool": "required tool"}
    assert data["stream"] is False
    assert stream is False
    assert "固定进度提示由运行时按配置发送" in messages[0]["content"]

    original_get_config = config_parser.get_config
    monkeypatch.setattr(
        config_parser,
        "get_config",
        lambda key, default=None: (
            True
            if key
            in {
                "tool_progress_messages_enabled",
                "tool_progress_model_preface_enabled",
            }
            else original_get_config(key, default)
        ),
    )
    natural_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    harness._build_payload(natural_messages)
    assert "附在可信的固定进度提示后" in natural_messages[0]["content"]
    assert "不得提前声称成功" in natural_messages[0]["content"]
    assert "只能调用本次 tools 字段明确列出的工具" in natural_messages[0]["content"]
    assert "先调用菜单中的帮助、列表、拓扑或全部" in natural_messages[0]["content"]


def test_payload_passes_disabled_model_boundary_without_search_read(
    monkeypatch,
) -> None:
    calls: list[tuple[bool, bool]] = []

    class Snapshot:
        def get_llm_payload_tools(
            self,
            _plugin_names,
            *,
            tools_enabled,
            search_enabled,
            is_superuser,
        ):
            assert is_superuser is False
            calls.append((tools_enabled, search_enabled))
            return {"resident_tool"}, []

    monkeypatch.setattr(
        model_selector,
        "get_resident_plugins",
        lambda: ["resident_tool"],
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(
        model_selector,
        "get_web_search",
        lambda: (_ for _ in ()).throw(AssertionError("no_tools 模型不应读取搜索开关")),
    )
    harness = PayloadHarness()
    harness.model_info = {
        "model": "no-tools-model",
        "stream": True,
        "no_tools": True,
    }
    harness.messages_handler = SimpleNamespace(current_images=[])
    harness.format_message_dict = {}
    harness.required_plugins = []
    harness.tool_snapshot = Snapshot()
    harness.is_superuser = False
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    data, stream = harness._build_payload(messages)

    assert calls == [(False, False)]
    assert "tools" not in data
    assert data["stream"] is True
    assert stream is True
    assert messages[0]["content"] == "system"
    assert harness._active_llm_tool_names == frozenset()


def test_nonebot_compatibility_tools_raise_simple_difficulty_floor() -> None:
    harness = PayloadHarness()
    harness.tool_snapshot = SimpleNamespace(
        plugin_info={"nonebot_plugin_picstatus_ng": {}},
    )

    assert (
        harness._apply_compatibility_tool_difficulty_floor(
            "0",
            ["nonebot_plugin_picstatus_ng"],
        )
        == "1"
    )
    assert (
        harness._apply_compatibility_tool_difficulty_floor(
            "0",
            ["web_search"],
        )
        == "0"
    )
    assert (
        harness._apply_compatibility_tool_difficulty_floor(
            "2",
            ["nonebot_plugin_picstatus_ng"],
        )
        == "2"
    )

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message
import pytest

from nonebot_plugin_moellmchats import protocol_context as module
from nonebot_plugin_moellmchats.model_selector import model_selector
from nonebot_plugin_moellmchats.protocol_context import (
    _snapshot_cache_digest,
    available_protocol_tool_names,
    business_conflicting_protocol_tools,
    probe_protocol_capabilities,
)
from nonebot_plugin_moellmchats.protocol_registry import protocol_registry
from nonebot_plugin_moellmchats.tool_manager import ToolManager, tool_manager


class _Adapter:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name


class _Bot:
    def __init__(
        self,
        *,
        protocol: str,
        self_id: str = "bot-1",
        app_name: str = "go-cqhttp",
        app_version: str = "1.0.0",
        supported_actions: list[str] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.adapter = _Adapter(protocol)
        self.self_id = self_id
        self.app_name = app_name
        self.app_version = app_version
        self.impl = "fake-v12"
        self.version = app_version
        self.supported_actions = list(supported_actions or [])
        self.failure = failure
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **data):
        self.calls.append((api, data))
        if self.failure is not None:
            raise self.failure
        if api == "get_version_info":
            return {
                "app_name": self.app_name,
                "app_version": self.app_version,
                "protocol_version": "11",
            }
        if api == "get_supported_actions":
            return list(self.supported_actions)
        raise AssertionError(f"unexpected probe API: {api}")


def _event(
    *,
    user_id: int | str = 123,
    group_id: int | str | None = 456,
    message_id: int | str = 789,
    reply_message_id: int | str | None = None,
    text: str = "普通消息",
):
    reply = None if reply_message_id is None else SimpleNamespace(message_id=reply_message_id)
    values = {
        "time": 1_725_000_000,
        "user_id": user_id,
        "message_id": message_id,
        "message": Message(text),
        "sender": SimpleNamespace(
            user_id=user_id,
            card="测试用户",
            nickname="测试用户",
        ),
        "reply": reply,
    }
    if group_id is not None:
        values["group_id"] = group_id
    return SimpleNamespace(**values)


@pytest.fixture
def protocol_config(monkeypatch: pytest.MonkeyPatch):
    original = module.config_parser.get_config
    values = {
        "protocol_tools_enabled": True,
        "protocol_tools_napcat_extensions_enabled": True,
        "protocol_tools_business_first": True,
    }
    monkeypatch.setattr(
        module.config_parser,
        "get_config",
        lambda key, default=None: values.get(key, original(key, default)),
    )
    return values


@pytest.mark.asyncio
async def test_protocol_tools_are_disabled_by_default_without_probing(
    protocol_config,
) -> None:
    protocol_config["protocol_tools_enabled"] = False
    bot = _Bot(protocol="OneBot V11")

    snapshot = await probe_protocol_capabilities(
        bot,
        _event(),
        generation=1,
        is_superuser=False,
    )

    assert snapshot.enabled is False
    assert snapshot.reason == "protocol_tools_enabled=false"
    assert snapshot.supported_actions == frozenset()
    assert bot.calls == []


@pytest.mark.asyncio
async def test_generic_v11_and_exact_napcat_detection(protocol_config) -> None:
    generic = _Bot(protocol="OneBot V11", app_name="Lagrange.OneBot")
    generic_snapshot = await probe_protocol_capabilities(
        generic,
        _event(),
        generation=2,
        is_superuser=False,
    )
    assert generic_snapshot.enabled
    assert generic_snapshot.protocol == "onebot_v11"
    assert generic_snapshot.implementation == "Lagrange.OneBot"
    assert generic_snapshot.supported_actions == (protocol_registry.standard_v11_actions)
    assert len(generic_snapshot.supported_actions) == 38

    napcat = _Bot(
        protocol="OneBot V11",
        app_name="NapCat.Onebot",
        app_version="4.18.19",
    )
    napcat_snapshot = await probe_protocol_capabilities(
        napcat,
        _event(),
        generation=2,
        is_superuser=False,
    )
    assert napcat_snapshot.enabled
    assert napcat_snapshot.implementation == "napcat"
    assert napcat_snapshot.implementation_version == "4.18.19"
    assert napcat_snapshot.supported_actions == protocol_registry.napcat_actions
    assert len(napcat_snapshot.supported_actions) == 175

    near_miss = _Bot(protocol="OneBot V11", app_name="NapCat.Onebot.dev")
    near_miss_snapshot = await probe_protocol_capabilities(
        near_miss,
        _event(),
        generation=2,
        is_superuser=False,
    )
    assert near_miss_snapshot.implementation != "napcat"
    assert near_miss_snapshot.supported_actions == (protocol_registry.standard_v11_actions)

    protocol_config["protocol_tools_napcat_extensions_enabled"] = False
    standard_only = await probe_protocol_capabilities(
        napcat,
        _event(),
        generation=2,
        is_superuser=False,
    )
    assert standard_only.supported_actions == (protocol_registry.napcat_actions & protocol_registry.standard_v11_actions)


@pytest.mark.asyncio
async def test_v12_supported_actions_are_filtered_and_probe_failure_is_local(
    protocol_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _Bot(
        protocol="OneBot V12",
        supported_actions=[
            "get_self_info",
            "get_group_info",
            "send_message",
            "implementation_extension",
        ],
    )
    snapshot = await probe_protocol_capabilities(
        bot,
        _event(user_id="actor", group_id="group", message_id="message"),
        generation=3,
        is_superuser=True,
    )

    assert snapshot.enabled
    assert snapshot.protocol == "onebot_v12"
    assert snapshot.supported_actions == frozenset({"get_self_info", "get_group_info", "send_message"})
    assert bot.calls == [("get_supported_actions", {})]

    failed = _Bot(protocol="OneBot V12", failure=TimeoutError())
    failed_snapshot = await probe_protocol_capabilities(
        failed,
        _event(user_id="actor", group_id=None, message_id="message"),
        generation=3,
        is_superuser=False,
    )
    assert failed_snapshot.enabled is False
    assert failed_snapshot.reason == "probe_failed:TimeoutError"
    assert available_protocol_tool_names(snapshot=failed_snapshot) == frozenset()

    slow = _Bot(protocol="OneBot V12")

    async def hanging_call(api: str, **data):
        slow.calls.append((api, data))
        await asyncio.sleep(60)

    monkeypatch.setattr(slow, "call_api", hanging_call)
    monkeypatch.setattr(module, "_PROBE_TIMEOUT_SECONDS", 0.01)
    timed_out_snapshot = await probe_protocol_capabilities(
        slow,
        _event(user_id="actor", group_id=None, message_id="message"),
        generation=3,
        is_superuser=False,
    )
    assert timed_out_snapshot.enabled is False
    assert timed_out_snapshot.reason == "probe_failed:TimeoutError"
    assert slow.calls == [("get_supported_actions", {})]


@pytest.mark.asyncio
async def test_discovery_filters_permission_scene_reply_and_napcat_capability(
    protocol_config,
) -> None:
    generic = await probe_protocol_capabilities(
        _Bot(protocol="OneBot V11"),
        _event(reply_message_id=None),
        generation=4,
        is_superuser=False,
    )
    user_names = available_protocol_tool_names(snapshot=generic)
    assert "qq__like_me" in user_names
    assert "onebot_v11__get_group_info" in user_names
    assert "onebot_v11__get_group_member_info" in user_names
    assert "onebot_v11__get_msg" not in user_names
    assert "onebot_v11__get_friend_list" not in user_names
    assert "onebot_v11__set_group_ban" not in user_names
    assert not any(name.startswith("napcat_v11__") for name in user_names)

    admin = await probe_protocol_capabilities(
        _Bot(protocol="OneBot V11"),
        _event(reply_message_id=321),
        generation=4,
        is_superuser=True,
    )
    admin_names = available_protocol_tool_names(snapshot=admin)
    assert "onebot_v11__get_msg" in admin_names
    assert "onebot_v11__get_friend_list" in admin_names
    assert "onebot_v11__set_group_ban" in admin_names

    napcat = await probe_protocol_capabilities(
        _Bot(protocol="OneBot V11", app_name="NapCat.Onebot"),
        _event(reply_message_id=321),
        generation=4,
        is_superuser=False,
    )
    napcat_names = available_protocol_tool_names(snapshot=napcat)
    assert {
        "qq__like_me",
        "qq__poke_current",
        "qq__react_current_message",
    } <= napcat_names
    assert "napcat_v11__get_cookies" not in napcat_names


def test_protocol_cache_digest_isolated_by_every_runtime_identity() -> None:
    base = {
        "protocol": "onebot_v11",
        "implementation": "napcat",
        "implementation_version": "4.18.19",
        "support_digest": "1" * 64,
        "adapter_id": "OneBot V11",
        "bot_id": "bot-1",
        "actor_user_id": "actor-1",
        "is_superuser": False,
        "scene": "group",
        "group_id": "group-1",
        "guild_id": None,
        "channel_id": None,
        "message_id": "message-1",
        "reply_message_id": "reply-1",
        "generation": 9,
        "enabled": True,
    }
    digests = {_snapshot_cache_digest(**base)}
    changes = {
        "protocol": "onebot_v12",
        "implementation": "generic",
        "implementation_version": "4.18.20",
        "support_digest": "2" * 64,
        "adapter_id": "OneBot V12",
        "bot_id": "bot-2",
        "actor_user_id": "actor-2",
        "is_superuser": True,
        "scene": "private",
        "group_id": "group-2",
        "guild_id": "guild-1",
        "channel_id": "channel-1",
        "message_id": "message-2",
        "reply_message_id": "reply-2",
        "generation": 10,
        "enabled": False,
    }
    for field, value in changes.items():
        candidate = dict(base)
        candidate[field] = value
        digests.add(_snapshot_cache_digest(**candidate))
    assert len(digests) == len(changes) + 1


@pytest.mark.asyncio
async def test_business_menu_trigger_suppresses_protocol_like_fallback(
    protocol_config,
) -> None:
    snapshot = await probe_protocol_capabilities(
        _Bot(protocol="OneBot V11"),
        _event(text="请给我点赞"),
        generation=5,
        is_superuser=False,
    )
    plugin_info = {
        "qi_group_admin": {
            "discovery_features": (
                {
                    "name": "点赞",
                    "summary": "给当前用户点赞",
                    "triggers": ({"type": "direct", "value": "给我点赞"},),
                    "invocable": True,
                    "hidden": False,
                },
            )
        }
    }

    conflicts = business_conflicting_protocol_tools(
        plugin_info,
        snapshot=snapshot,
    )
    assert "qq__like_me" in conflicts
    assert "onebot_v11__send_like" in conflicts

    protocol_config["protocol_tools_business_first"] = False
    assert (
        business_conflicting_protocol_tools(
            plugin_info,
            snapshot=snapshot,
        )
        == frozenset()
    )


@pytest.mark.asyncio
async def test_catalog_is_brief_and_only_selected_protocol_action_expands_schema(
    protocol_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(tool_manager, "is_tool_blacklisted", lambda _name: False)
    bot = _Bot(protocol="OneBot V11")

    async with module.protocol_request_scope(
        bot,
        _event(),
        generation=6,
        is_superuser=False,
    ):
        catalog = ToolManager.build_brief_catalog(
            plugin_info={},
            custom_tools={},
            mcp_tool_names=set(),
            is_superuser=False,
        )
        schema = ToolManager.build_tool_schema(
            ["qq__like_me", "onebot_v11__get_friend_list"],
            plugin_info={},
            custom_tools={},
            is_superuser=False,
        )

    assert "- qq__like_me | QQ安全封装 |" in catalog
    assert "onebot_v11__get_group_info" in catalog
    assert "onebot_v11__get_friend_list" not in catalog
    assert '"times"' not in catalog
    assert [item["function"]["name"] for item in schema] == ["qq__like_me"]
    assert schema[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "times": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 1,
                "description": "给当前发起用户点赞的次数，1 到 10",
            }
        },
        "required": [],
        "additionalProperties": False,
    }

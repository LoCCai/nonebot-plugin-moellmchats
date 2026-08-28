from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import json
from pathlib import Path
import re

import pytest

from nonebot_plugin_moellmchats.builtin_tools import (
    builtin_protocol_spec,
    builtin_protocol_specs,
)
from nonebot_plugin_moellmchats.protocol_registry import protocol_registry
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolCapabilityV2,
    ToolConfirmationMode,
    ToolEffect,
    ToolPolicy,
    ToolSpec,
    validate_tool_arguments,
)
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ToolSource,
    ToolTrustLevel,
    ToolTrustPolicy,
    ToolTrustPolicyError,
)
from scripts.generate_protocol_manifests import (
    ManifestGenerationError,
    _validate_policies,
    escape_action_name,
)

RESOURCE_DIR = Path(__file__).parents[1] / "nonebot_plugin_moellmchats" / "protocol_resources"


def _resource(name: str) -> dict:
    return json.loads((RESOURCE_DIR / name).read_text(encoding="utf-8"))


def test_pinned_protocol_inventory_counts_sources_and_digests() -> None:
    sources = _resource("sources.json")["sources"]
    inventory = _resource("actions.json")
    policies = _resource("policies.json")

    assert inventory["counts"] == {
        "napcat_v11": 175,
        "onebot_v11": 38,
        "onebot_v12": 31,
    }
    assert Counter(action.protocol for action in protocol_registry.actions) == {
        "napcat_v11": 175,
        "onebot_v11": 38,
        "onebot_v12": 31,
    }
    assert len(protocol_registry.actions) == 244
    assert len(protocol_registry.policies) == 244
    assert inventory["actions_sha256"] == ("674fe5b28be905192dfda36cf0952f27c548561b6aa8981d840e5ef52826bf7e")
    assert policies["policies_sha256"] == ("e16b43d3a5ab2abe63248ebbc2245b04e43121403b66e46ece96043fb8901526")
    assert policies["wrappers_sha256"] == ("0d4cb894677c5b03f6c88a9f3bfe8421474a529f2a654c4087a39fc8f55fc475")
    assert sources["onebot_v11"]["commit"] == ("d4456ee706f9ada9c2dfde56a2bcfc69752600e4")
    assert sources["onebot_v12"]["commit"] == ("d533f0fca3bd14781d4461776dba8d907d9de253")
    assert sources["nonebot_adapter_onebot"]["commit"] == ("3ac943fc4470d851219f368cacadf3dcdd649ee7")
    assert sources["napcat_docs"]["commit"] == ("14ad6896579abf17c761cdf8d9dfb7c3ea396305")
    assert sources["napcat_docs"]["sha256"] == ("905ff1faa265cdfa6401a91e8ed832ab15c9e32a7683c42dc11bb6752682ae39")


def test_protocol_tool_names_are_strict_deterministic_and_collision_free() -> None:
    names = [action.tool_name for action in protocol_registry.actions]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in names)
    assert all(action.tool_name.startswith(f"{action.protocol}__") for action in protocol_registry.actions)
    escaped = escape_action_name("send.msg/测试", "napcat_v11")
    assert escaped == "napcat_v11__send_x2e_msg_x2f__x6d4b__x8bd5_"
    assert escape_action_name("send.msg/测试", "napcat_v11") == escaped


def test_reviewed_runtime_specs_exclude_every_denied_or_internal_action() -> None:
    specs = builtin_protocol_specs()
    spec_names = {spec.name for spec in specs}
    denied = {
        action.tool_name
        for action in protocol_registry.actions
        if protocol_registry.policy_for(action).exposure in {"denied", "internal"}
    }

    assert Counter(policy.exposure for policy in protocol_registry.policies) == {
        "denied": 95,
        "superuser": 129,
        "user": 20,
    }
    assert len(protocol_registry.public_runtime_actions()) == 121
    assert len(specs) == 124
    assert len(spec_names) == len(specs)
    assert denied
    assert denied.isdisjoint(spec_names)
    assert builtin_protocol_spec("napcat_v11__get_cookies") is None
    assert builtin_protocol_spec("napcat_v11___send_group_notice") is None
    assert builtin_protocol_spec("napcat_v11__send_forward_msg") is None
    assert builtin_protocol_spec("napcat_v11__send_qzone_msg") is None
    assert builtin_protocol_spec("onebot_v11__set_group_anonymous_ban") is None
    assert not any(spec.name in {"call_api", "onebot__call_api"} for spec in specs)
    assert {spec.name for spec in specs if spec.name.startswith("qq__")} == {
        "qq__like_me",
        "qq__poke_current",
        "qq__react_current_message",
    }


def test_permanent_denials_and_management_permissions_cannot_be_elevated() -> None:
    permanently_denied = {
        "napcat_v11:.handle_quick_operation",
        "napcat_v11:bot_exit",
        "napcat_v11:download_file",
        "napcat_v11:fetch_emoji_like",
        "napcat_v11:get_clientkey",
        "napcat_v11:get_cookies",
        "napcat_v11:get_credentials",
        "napcat_v11:get_csrf_token",
        "napcat_v11:get_mini_app_ark",
        "napcat_v11:get_rkey",
        "napcat_v11:nc_get_rkey",
        "napcat_v11:send_forward_msg",
        "napcat_v11:send_packet",
        "napcat_v11:send_qzone_msg",
        "napcat_v11:set_group_kick_members",
        "napcat_v11:set_restart",
        "napcat_v11:upload_group_file",
        "onebot_v11:get_cookies",
        "onebot_v11:get_credentials",
        "onebot_v11:get_csrf_token",
        "onebot_v11:set_group_anonymous_ban",
        "onebot_v11:set_restart",
        "onebot_v12:get_file",
        "onebot_v12:get_latest_events",
        "onebot_v12:upload_file",
    }
    for action_id in permanently_denied:
        policy = protocol_registry.policy_by_id[action_id]
        assert policy.exposure == "denied"
        assert policy.risk == "forbidden"
        assert policy.denial_reason
        assert builtin_protocol_spec(protocol_registry.by_id[action_id].tool_name) is None

    management = [
        policy
        for policy in protocol_registry.policies
        if policy.capability == "bot_manage" and policy.exposure in {"user", "superuser"}
    ]
    assert management
    assert all(policy.permission == "superuser" for policy in management)
    assert all(policy.confirmation == "required" for policy in management)


def test_protocol_model_schemas_remove_trusted_event_targets() -> None:
    for action in protocol_registry.public_runtime_actions():
        policy = protocol_registry.policy_for(action)
        spec = builtin_protocol_spec(action.tool_name)
        assert spec is not None
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert not set(policy.injected_params) & set(spec.parameters.get("properties", {}))
        assert not set(policy.injected_params) & set(spec.parameters.get("required", ()))


def test_protocol_model_schemas_are_recursively_strict_and_text_messages_are_safe() -> None:
    def assert_strict_objects(value: object) -> None:
        if isinstance(value, Mapping):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_strict_objects(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                assert_strict_objects(child)

    for spec in builtin_protocol_specs():
        assert_strict_objects(spec.parameters)

    group_send = builtin_protocol_spec("onebot_v11__send_group_msg")
    routed_send = builtin_protocol_spec("onebot_v11__send_msg")
    v12_send = builtin_protocol_spec("onebot_v12__send_message")
    assert group_send is not None
    assert routed_send is not None
    assert v12_send is not None
    invalid_arguments = (
        {
            "group_id": 123,
            "message": [{"type": "image", "data": {"file": "file:///etc/passwd"}}],
        },
        {
            "message_type": "private",
            "user_id": "123",
            "message": [{"type": "image", "data": {"file": "file:///etc/passwd"}}],
        },
        {
            "detail_type": "private",
            "user_id": "123",
            "message": [{"type": "image", "data": {"file_id": "host-owned"}}],
        },
    )
    for spec, invalid in zip(
        (group_send, routed_send, v12_send),
        invalid_arguments,
        strict=True,
    ):
        properties = spec.parameters["properties"]
        assert properties["message"]["type"] == "string"
        assert properties["message"]["maxLength"] == 4000
        assert "auto_escape" not in properties
        assert validate_tool_arguments(invalid, spec.parameters) is not None

    assert (
        validate_tool_arguments(
            {"group_id": 123, "message": "安全纯文本"},
            group_send.parameters,
        )
        is None
    )
    assert (
        validate_tool_arguments(
            {"group_id": 123, "message": "x" * 4001},
            group_send.parameters,
        )
        is not None
    )


def test_new_or_unreviewed_action_set_fails_policy_generation() -> None:
    inventory = _resource("actions.json")
    policies = _resource("policies.json")["policies"]

    with pytest.raises(ManifestGenerationError, match="exactly match"):
        _validate_policies(inventory["actions"], policies[:-1])

    unreviewed = [dict(item) for item in policies]
    unreviewed[0]["reviewed"] = False
    with pytest.raises(ManifestGenerationError, match="unreviewed"):
        _validate_policies(inventory["actions"], unreviewed)


def test_low_risk_direct_mode_is_reserved_for_exact_protocol_builtins() -> None:
    async def handler() -> None:
        return None

    forged = ToolSpec(
        name="forged_direct",
        description="must not bypass confirmation",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        effect=ToolEffect.MUTATING,
        confirmation_mode=ToolConfirmationMode.TRUSTED_LOW_RISK_DIRECT,
    )
    discovered = DiscoveredTool(
        provider_id="registered",
        source=ToolSource.REGISTERED,
        trust=ToolTrustLevel.TRUSTED,
        generation=1,
        spec=forged,
    )
    with pytest.raises(ToolTrustPolicyError, match="包内可信协议"):
        ToolTrustPolicy.from_discovered(discovered)

    canonical = builtin_protocol_spec("qq__like_me")
    assert canonical is not None
    trusted = DiscoveredTool(
        provider_id="builtin",
        source=ToolSource.BUILTIN,
        trust=ToolTrustLevel.TRUSTED,
        generation=1,
        spec=canonical,
    )
    assert ToolTrustPolicy.from_discovered(trusted).confirmation_required is False


def test_bot_capabilities_are_reserved_for_exact_protocol_builtins() -> None:
    async def handler() -> None:
        return None

    forged = ToolSpec(
        name="forged_bot_reader",
        description="must not consume the protocol broker capability",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        policy=ToolPolicy.configured(ToolCapabilityV2(bot_read=True)),
    )
    discovered = DiscoveredTool(
        provider_id="registered",
        source=ToolSource.REGISTERED,
        trust=ToolTrustLevel.TRUSTED,
        generation=1,
        spec=forged,
    )
    with pytest.raises(ToolTrustPolicyError, match="Bot capability"):
        ToolTrustPolicy.from_discovered(discovered)

    canonical = builtin_protocol_spec("onebot_v11__get_login_info")
    assert canonical is not None
    trusted = DiscoveredTool(
        provider_id="builtin",
        source=ToolSource.BUILTIN,
        trust=ToolTrustLevel.TRUSTED,
        generation=1,
        spec=canonical,
    )
    assert ToolTrustPolicy.from_discovered(trusted).spec is canonical

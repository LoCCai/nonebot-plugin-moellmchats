from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from nonebot_plugin_moellmchats.tool_contracts import (
    CAPABILITY_DETECTOR_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    ToolCapability,
    ToolCapabilityV2,
    ToolEffect,
    ToolPolicy,
    ToolRegistry,
    ToolSpec,
)


async def _handler() -> str:
    return "ok"


def test_tool_spec_registry_and_legacy_schema() -> None:
    registry = ToolRegistry()
    spec = ToolSpec(
        name="read_clock",
        description="read time",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        effect=ToolEffect.READ_ONLY,
        dependencies=("clock_backend",),
    )
    registry.register(spec)
    assert registry.get("read_clock") is spec
    assert spec.as_legacy_schema()["func"] is _handler
    assert spec.dependencies == ("clock_backend",)


def test_tool_spec_detaches_and_freezes_parameters() -> None:
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    spec = ToolSpec(
        name="frozen_schema",
        description="frozen schema",
        parameters=parameters,
        handler=_handler,
    )

    parameters["properties"]["value"]["type"] = "integer"
    parameters["required"].clear()
    assert spec.parameters["properties"]["value"]["type"] == "string"
    assert spec.parameters["required"] == ("value",)
    with pytest.raises(TypeError):
        spec.parameters["properties"]["value"]["type"] = "number"

    legacy = spec.as_legacy_schema()
    json.dumps(legacy["parameters"])
    legacy["parameters"]["properties"]["value"]["type"] = "boolean"
    assert spec.parameters["properties"]["value"]["type"] == "string"


def test_tool_spec_rejects_unknown_permission() -> None:
    with pytest.raises(ValueError, match="permission"):
        ToolSpec(
            name="bad",
            description="bad",
            parameters={},
            handler=_handler,
            permission="root",
        )


def test_tool_spec_rejects_invalid_schema_and_handler() -> None:
    with pytest.raises(ValueError, match=r"parameters\.type"):
        ToolSpec(
            name="bad_schema",
            description="bad",
            parameters={"type": "array"},
            handler=_handler,
        )
    with pytest.raises(ValueError, match="handler"):
        ToolSpec(
            name="bad_handler",
            description="bad",
            parameters={"type": "object", "properties": {}},
            handler=None,
        )


def test_tool_spec_rejects_invalid_nested_schema_and_dependencies() -> None:
    with pytest.raises(ValueError, match="required"):
        ToolSpec(
            name="bad_nested",
            description="bad",
            parameters={
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "properties": {},
                        "required": ["missing"],
                    }
                },
            },
            handler=_handler,
        )
    with pytest.raises(ValueError, match="dependencies"):
        ToolSpec(
            name="bad_dependencies",
            description="bad",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            dependencies=("bad dependency",),
        )


def test_generated_policy_defaults_to_workspace_only() -> None:
    policy = ToolPolicy.generated()

    assert policy.requested == ToolCapability(
        network=False, process=False, workspace=True
    )
    assert policy.admin == policy.requested
    assert policy.effective == policy.requested


def test_policy_uses_strict_requested_and_admin_intersection() -> None:
    policy = ToolPolicy.generated(
        ToolCapability(network=True, process=True, workspace=True),
        admin=ToolCapability(network=False, process=True, workspace=False),
    )

    assert policy.requested.network
    assert policy.admin.process
    assert policy.effective == ToolCapability(
        network=False, process=True, workspace=False
    )
    with pytest.raises(FrozenInstanceError):
        policy.effective = ToolCapability()  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    [
        {"network": "yes"},
        {"process": 1},
        {"workspace": None},
        {"host_filesystem": "yes"},
        {"secrets": 1},
        {"kernel": True},
        {"network": False, 1: True},
    ],
)
def test_capability_mapping_rejects_unknown_or_non_boolean_values(value) -> None:
    with pytest.raises(ValueError, match="capabilit"):
        ToolCapability.from_mapping(value)


def test_policy_and_tool_spec_reject_invalid_policy_types() -> None:
    with pytest.raises(ValueError, match="requested capabilities"):
        ToolPolicy(requested={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy"):
        ToolSpec(
            name="bad_policy",
            description="bad",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
            policy="unsafe",  # type: ignore[arg-type]
        )


def test_tool_spec_carries_policy_without_model_controlled_confirmation() -> None:
    policy = ToolPolicy.generated(
        {"network": True, "process": False, "workspace": True},
        admin={"network": False, "process": False, "workspace": True},
    )
    spec = ToolSpec(
        name="mutate",
        description="change state",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        effect=ToolEffect.MUTATING,
        policy=policy,
    )

    schema = spec.as_legacy_schema()
    assert spec.policy is policy
    assert spec.policy.effective == ToolCapability(workspace=True)
    assert "confirm" not in schema["parameters"]["properties"]
    assert "confirm" not in schema["parameters"]["required"]


def test_host_filesystem_and_secrets_are_deny_by_default() -> None:
    default = ToolCapability()
    assert default.host_filesystem is False
    assert default.secrets is False
    requested = ToolCapability(host_filesystem=True, secrets=True)
    assert requested.restrict(default) == default


def test_structured_capability_profile_intersects_scopes_deterministically() -> None:
    requested = ToolCapabilityV2.from_mapping(
        {
            "network": {"allow": ["weather.example", "api.example"]},
            "process": True,
            "filesystem": {
                "workspace": {"read": True, "write": True},
                "host": False,
            },
            "database": {"read": True, "write": False},
            "bot": {"read": True, "send": True, "manage": False},
            "secrets": {"allow": ["WEATHER_TOKEN"]},
        }
    )
    admin = ToolCapabilityV2.from_mapping(
        {
            "network": {"allow": ["api.example"]},
            "process": False,
            "filesystem": {"workspace": True, "host": False},
            "database": True,
            "bot": {"read": True, "send": False, "manage": False},
            "secrets": {"allow": ["WEATHER_TOKEN"]},
        }
    )
    policy = ToolPolicy.generated(requested, admin=admin)

    assert policy.effective_v2.network_allow == ("api.example",)
    assert policy.effective_v2.process is False
    assert policy.effective_v2.database_read is True
    assert policy.effective_v2.database_write is False
    assert policy.effective_v2.bot_read is True
    assert policy.effective_v2.bot_send is False
    assert policy.effective_v2.secret_names == ("WEATHER_TOKEN",)
    assert policy.effective.network is True
    assert policy.effective.process is False


def test_detected_capability_is_bound_but_never_grants_authority() -> None:
    denied = ToolPolicy.generated(
        {"network": True},
        admin={"network": False},
    )
    with pytest.raises(ValueError, match=r"detected.*requested/admin"):
        denied.with_detected(ToolCapability(network=True, workspace=False))

    authorized = ToolPolicy.configured({"network": True}).with_detected(
        ToolCapability(network=True, workspace=False)
    )
    assert authorized.detected.network is True
    assert authorized.effective.network is True
    assert authorized.effective == ToolPolicy.configured(
        {"network": True}
    ).effective
    contract = authorized.capability_contract()
    assert contract["schema_version"] == CAPABILITY_SCHEMA_VERSION
    assert contract["detector_version"] == CAPABILITY_DETECTOR_VERSION
    assert contract["detected"]["network"] is True
    json.dumps(contract)


@pytest.mark.parametrize(
    "value",
    [
        {"network": {"allow": ["* "]}},
        {"network": {"allow": ["*", "api.example"]}},
        {"filesystem": {"workspace": {"read": False, "write": True}}},
        {"database": {"read": False, "write": True}},
        {"bot": {"read": False, "send": True}},
        {"secrets": {"allow": ["bad-name"]}},
        {"workspace": True, "filesystem": {"workspace": True}},
    ],
)
def test_structured_capability_profile_rejects_ambiguous_or_unsafe_values(
    value,
) -> None:
    with pytest.raises(ValueError, match="capabilit"):
        ToolCapabilityV2.from_mapping(value)

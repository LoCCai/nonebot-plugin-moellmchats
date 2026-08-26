from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from nonebot_plugin_moellmchats.tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolPolicy,
    ToolSpec,
)
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderRegistration,
    ToolExecutionBoundary,
    ToolResultProvenance,
    ToolSource,
    ToolTrustDenied,
    ToolTrustLevel,
    ToolTrustOperation,
    ToolTrustPolicy,
    ToolTrustPolicyError,
    trust_for_source,
)

_GENERATION = 61
_PROVIDER_IDS = {
    ToolSource.REGISTERED: "registered",
    ToolSource.CUSTOM_FILE: "custom-file",
    ToolSource.GENERATED: "generated",
    ToolSource.MCP: "mcp",
    ToolSource.BUILTIN: "builtin",
    ToolSource.NONEBOT_PLUGIN: "nonebot-plugin",
}


async def _handler(value: str = "ok") -> str:
    return value


def _spec(
    name: str,
    *,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    permission: str = "user",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} trust contract",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        handler=_handler,
        effect=effect,
        permission=permission,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )


def _schema(spec: ToolSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def _custom_artifact(spec: ToolSpec) -> ToolArtifact:
    source = f"async def {spec.name}(value='ok'):\n    return value\n".encode()
    return ToolArtifact(
        tool_name=spec.name,
        handler_name=spec.name,
        source=source,
        source_hash=source_sha256(source),
        schema=_schema(spec),
        spec=spec,
        contract=ToolContractSnapshot.from_spec(spec),
        source_type="custom_file",
        generation=_GENERATION,
        filename="trust_tools.py",
    )


def _generated_artifact(spec: ToolSpec) -> ToolArtifact:
    source = f"async def {spec.name}(value='ok'):\n    return value\n".encode()
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    manifest = {
        "bundle_id": "trust_bundle",
        "description": "trust bundle",
        "capabilities": {
            "network": False,
            "process": False,
            "workspace": True,
        },
        "tools": [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
                "handler": spec.name,
                "permission": spec.permission,
                "effect": spec.effect.value,
                "timeout_seconds": spec.timeout_seconds,
                "result_limit": spec.result_limit,
                "dependencies": [],
            }
        ],
    }
    digest = canonical_bundle_digest(manifest, source, tests_source)
    return ToolArtifact(
        tool_name=spec.name,
        handler_name=spec.name,
        source=source,
        source_hash=source_sha256(source),
        schema=_schema(spec),
        spec=spec,
        contract=ToolContractSnapshot.from_spec(spec),
        source_type="generated",
        generation=_GENERATION,
        filename="tool.py",
        tests_source=tests_source,
        bundle_manifest=manifest,
        bundle_id="trust_bundle",
        bundle_digest=digest,
    )


def _record(
    source: ToolSource,
    spec: ToolSpec,
    *,
    artifact: ToolArtifact | None = None,
) -> DiscoveredTool:
    return DiscoveredTool(
        provider_id=_PROVIDER_IDS[source],
        source=source,
        trust=trust_for_source(source),
        generation=_GENERATION,
        spec=spec,
        artifact=artifact,
    )


def _catalog() -> ProviderCatalogSnapshot:
    registered = _spec(
        "registered_admin",
        effect=ToolEffect.MUTATING,
        permission="superuser",
    )
    custom_file = _spec("reviewed_mutating", effect=ToolEffect.MUTATING)
    generated = _spec("generated_mutating", effect=ToolEffect.MUTATING)
    mcp = _spec("mcp_external")
    builtin = _spec("web_search")
    nonebot_plugin = _spec(
        "nonebot_compat",
        effect=ToolEffect.MUTATING,
    )
    records = (
        _record(ToolSource.REGISTERED, registered),
        _record(
            ToolSource.CUSTOM_FILE,
            custom_file,
            artifact=_custom_artifact(custom_file),
        ),
        _record(
            ToolSource.GENERATED,
            generated,
            artifact=_generated_artifact(generated),
        ),
        _record(ToolSource.MCP, mcp),
        _record(ToolSource.BUILTIN, builtin),
        _record(ToolSource.NONEBOT_PLUGIN, nonebot_plugin),
    )
    registrations = {
        source.value: ProviderRegistration(
            provider_id=provider_id,
            source=source,
            trust=trust_for_source(source),
        )
        for source, provider_id in _PROVIDER_IDS.items()
    }
    return ProviderCatalogSnapshot(
        generation=_GENERATION,
        registrations={
            registration.provider_id: registration
            for registration in registrations.values()
        },
        tools={record.spec.name: record for record in records},
    )


def test_catalog_materializes_complete_immutable_trust_policy_matrix() -> None:
    catalog = _catalog()
    policies = catalog.trust_policies

    assert tuple(policies) == (
        "generated_mutating",
        "mcp_external",
        "nonebot_compat",
        "registered_admin",
        "reviewed_mutating",
        "web_search",
    )
    assert {
        name: policy.boundary
        for name, policy in policies.items()
    } == {
        "registered_admin": ToolExecutionBoundary.IN_PROCESS,
        "reviewed_mutating": ToolExecutionBoundary.ISOLATED_ARTIFACT,
        "generated_mutating": ToolExecutionBoundary.GENERATED_SANDBOX,
        "mcp_external": ToolExecutionBoundary.EXTERNAL_PROXY,
        "web_search": ToolExecutionBoundary.IN_PROCESS,
        "nonebot_compat": ToolExecutionBoundary.BOUNDED_EVENT,
    }
    assert policies["generated_mutating"].result_provenance is (
        ToolResultProvenance.UNTRUSTED
    )
    assert policies["mcp_external"].result_provenance is (
        ToolResultProvenance.EXTERNAL
    )
    assert policies["web_search"].result_provenance is (
        ToolResultProvenance.EXTERNAL
    )
    assert policies["registered_admin"].result_provenance is (
        ToolResultProvenance.UNVERIFIED
    )
    assert catalog.trust_summary() == {
        "trusted": 2,
        "reviewed": 2,
        "untrusted": 1,
        "external": 1,
    }
    for name, record in catalog.tools.items():
        policy = policies[name]
        assert policy.spec is record.spec
        assert policy.generation == catalog.generation
    assert deepcopy(catalog) is catalog
    with pytest.raises(TypeError):
        policies["new"] = policies["web_search"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        policies["web_search"].trust = ToolTrustLevel.EXTERNAL


def test_trust_decisions_enforce_selection_execution_and_management() -> None:
    catalog = _catalog()
    registered = catalog.trust_policy_for("registered_admin")

    denied_selection = registered.decide(
        ToolTrustOperation.SELECTION,
        is_superuser=False,
    )
    assert not denied_selection.allowed
    assert denied_selection.audit_required
    assert registered.require(
        ToolTrustOperation.SELECTION,
        is_superuser=True,
    ).allowed
    with pytest.raises(ToolTrustDenied) as denied:
        registered.require(
            ToolTrustOperation.EXECUTION,
            is_superuser=True,
        )
    assert denied.value.decision.confirmation_required
    assert registered.require(
        ToolTrustOperation.EXECUTION,
        is_superuser=True,
        confirmed=True,
    ).allowed
    assert not registered.decide(
        ToolTrustOperation.MANAGEMENT,
        is_superuser=False,
    ).allowed
    assert registered.require(
        ToolTrustOperation.MANAGEMENT,
        is_superuser=True,
    ).allowed

    reviewed = catalog.trust_policy_for("reviewed_mutating")
    assert not reviewed.decide(
        ToolTrustOperation.EXECUTION,
        is_superuser=False,
    ).allowed
    assert reviewed.require(
        ToolTrustOperation.EXECUTION,
        is_superuser=False,
        confirmed=True,
    ).allowed

    compatibility = catalog.trust_policy_for("nonebot_compat")
    compatibility_decision = compatibility.require(
        ToolTrustOperation.EXECUTION,
        is_superuser=False,
    )
    assert compatibility_decision.legacy_bounded_compatibility
    assert not compatibility_decision.confirmation_required
    assert compatibility_decision.audit_required


def test_trust_audit_metadata_is_stable_and_argument_free() -> None:
    catalog = _catalog()
    decision = catalog.require_trust(
        "mcp_external",
        ToolTrustOperation.EXECUTION,
        is_superuser=False,
    )

    assert decision.audit_required
    assert decision.audit_metadata() == {
        "tool_name": "mcp_external",
        "provider_id": "mcp",
        "source": "mcp",
        "trust": "external",
        "generation": _GENERATION,
        "operation": "execution",
        "execution_boundary": "external_proxy",
        "result_provenance": "external",
        "permission": "user",
        "effect": "read_only",
        "allowed": True,
        "reason": "trust policy 允许",
        "confirmation_required": False,
        "legacy_bounded_compatibility": False,
        "audit_required": True,
    }
    assert deepcopy(decision) is decision


def test_trust_policy_rejects_identity_boundary_and_policy_drift() -> None:
    catalog = _catalog()
    policy = catalog.trust_policy_for("reviewed_mutating")

    with pytest.raises(ToolTrustPolicyError, match="boundary"):
        replace(policy, boundary=ToolExecutionBoundary.IN_PROCESS)
    with pytest.raises(ToolTrustPolicyError, match="provider_id"):
        replace(policy, provider_id="registered")
    with pytest.raises(ToolTrustPolicyError, match="provenance"):
        replace(policy, result_provenance=ToolResultProvenance.EXTERNAL)
    with pytest.raises(ToolTrustPolicyError, match="confirmation"):
        replace(policy, confirmation_required=False)
    with pytest.raises(ToolTrustPolicyError, match="缺少"):
        catalog.trust_policy_for("missing_tool")
    with pytest.raises(TypeError, match="ToolTrustOperation"):
        policy.decide("execution", is_superuser=False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bool"):
        policy.decide(
            ToolTrustOperation.SELECTION,
            is_superuser=1,  # type: ignore[arg-type]
        )


def test_catalog_rejects_noncanonical_provider_identity_for_trust_policy() -> None:
    spec = _spec("noncanonical_registered")
    registration = ProviderRegistration(
        provider_id="registered-v2",
        source=ToolSource.REGISTERED,
        trust=ToolTrustLevel.TRUSTED,
    )
    record = DiscoveredTool(
        provider_id=registration.provider_id,
        source=registration.source,
        trust=registration.trust,
        generation=_GENERATION,
        spec=spec,
    )

    with pytest.raises(ToolTrustPolicyError, match="provider_id"):
        ProviderCatalogSnapshot(
            generation=_GENERATION,
            registrations={registration.provider_id: registration},
            tools={spec.name: record},
        )


def test_allowed_trusted_local_selection_can_skip_high_volume_audit() -> None:
    catalog = _catalog()
    local_spec = _spec("local_read")
    local_catalog = ProviderCatalogSnapshot(
        generation=_GENERATION,
        registrations={
            "registered": ProviderRegistration(
                provider_id="registered",
                source=ToolSource.REGISTERED,
                trust=ToolTrustLevel.TRUSTED,
            )
        },
        tools={
            local_spec.name: _record(ToolSource.REGISTERED, local_spec),
        },
    )

    decision = local_catalog.require_trust(
        local_spec.name,
        ToolTrustOperation.SELECTION,
        is_superuser=False,
    )
    assert decision.allowed
    assert not decision.audit_required
    assert catalog.trust_policy_for("web_search").decide(
        ToolTrustOperation.SELECTION,
        is_superuser=False,
    ).audit_required


def test_tool_trust_policy_constructor_requires_discovered_identity() -> None:
    spec = _spec("direct_policy")
    with pytest.raises(TypeError, match="DiscoveredTool"):
        ToolTrustPolicy.from_discovered(spec)  # type: ignore[arg-type]

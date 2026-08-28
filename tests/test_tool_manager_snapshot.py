from __future__ import annotations

from dataclasses import replace
import importlib
import json
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.config import (
    DEFAULT_CONFIG,
    ConfigParser,
    config_parser,
)
from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    LifecycleState,
    VersionRecord,
    VersionState,
)
from nonebot_plugin_moellmchats.nonebot_plugin_tools import (
    build_nonebot_plugin_candidate,
)
from nonebot_plugin_moellmchats.runtime_metrics import runtime_metrics
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots
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
from nonebot_plugin_moellmchats.tool_discovery import with_menu_discovery
from nonebot_plugin_moellmchats.tool_manager import (
    LlmToolExecutionRoute,
    PendingActionExecutionView,
    ProviderConsumerParityError,
    SearchExtractorView,
    ToolManagementTargetKind,
    ToolManagementView,
    ToolManager,
    ToolSnapshot,
    model_selector,
    tool_manager,
)
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    FileToolResources,
    GeneratedToolResources,
    ProviderCatalogSnapshot,
    ProviderDiscoveryBatch,
    ProviderDiscoveryContext,
    ProviderRegistration,
    RegisteredToolResources,
    ToolSource,
    ToolTrustLevel,
    ToolTrustOperation,
    builtin_tool_provider,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
    nonebot_plugin_provider,
    provider_registry,
    registered_tool_provider,
)


async def _handler(value: str) -> str:
    return value


def _builtin_batch(generation: int) -> ProviderDiscoveryBatch:
    registration = ProviderRegistration.from_provider(builtin_tool_provider)
    records = tuple(
        DiscoveredTool(
            provider_id=registration.provider_id,
            source=registration.source,
            trust=registration.trust,
            generation=generation,
            spec=spec,
        )
        for spec in builtin_tool_specs()
    )
    return ProviderDiscoveryBatch(registration, generation, records)


def _nonebot_batch(
    generation: int,
    specs: tuple[ToolSpec, ...] = (),
) -> ProviderDiscoveryBatch:
    registration = ProviderRegistration.from_provider(
        nonebot_plugin_provider
    )
    records = tuple(
        DiscoveredTool(
            provider_id=registration.provider_id,
            source=registration.source,
            trust=registration.trust,
            generation=generation,
            spec=spec,
        )
        for spec in specs
    )
    return ProviderDiscoveryBatch(registration, generation, records)


def _registered_catalog(
    specs: tuple[ToolSpec, ...],
    *,
    generation: int,
) -> ProviderCatalogSnapshot:
    registration = ProviderRegistration.from_provider(registered_tool_provider)
    file_registration = ProviderRegistration.from_provider(file_tool_provider)
    generated_registration = ProviderRegistration.from_provider(
        generated_tool_provider
    )
    mcp_registration = ProviderRegistration.from_provider(mcp_tool_provider)
    records = tuple(
        DiscoveredTool(
            provider_id=registration.provider_id,
            source=registration.source,
            trust=registration.trust,
            generation=generation,
            spec=spec,
        )
        for spec in specs
    )
    return provider_registry.build_snapshot(
        generation,
        (
            ProviderDiscoveryBatch(
                registration=registration,
                generation=generation,
                tools=records,
            ),
            ProviderDiscoveryBatch(
                registration=file_registration,
                generation=generation,
                tools=(),
            ),
            ProviderDiscoveryBatch(
                registration=generated_registration,
                generation=generation,
                tools=(),
            ),
            ProviderDiscoveryBatch(
                registration=mcp_registration,
                generation=generation,
                tools=(),
            ),
            _builtin_batch(generation),
            _nonebot_batch(generation),
        ),
    )


def _file_artifact(
    spec: ToolSpec,
    *,
    generation: int,
    contract_version: int = 2,
) -> ToolArtifact:
    source = f"async def {spec.name}(value='ok'):\n    return value\n".encode()
    return ToolArtifact(
        tool_name=spec.name,
        handler_name=spec.name,
        source=source,
        source_hash=source_sha256(source),
        schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
        spec=spec,
        contract=ToolContractSnapshot.from_spec(
            spec,
            contract_version=contract_version,
        ),
        source_type="custom_file",
        generation=generation,
        filename="snapshot_tools.py",
    )


def _file_catalog(
    artifact: ToolArtifact,
    *,
    generation: int,
) -> ProviderCatalogSnapshot:
    registered = ProviderRegistration.from_provider(registered_tool_provider)
    custom_file = ProviderRegistration.from_provider(file_tool_provider)
    generated = ProviderRegistration.from_provider(generated_tool_provider)
    mcp = ProviderRegistration.from_provider(mcp_tool_provider)
    record = DiscoveredTool(
        provider_id=custom_file.provider_id,
        source=custom_file.source,
        trust=custom_file.trust,
        generation=generation,
        spec=artifact.spec,
        artifact=artifact,
    )
    return provider_registry.build_snapshot(
        generation,
        (
            ProviderDiscoveryBatch(registered, generation, ()),
            ProviderDiscoveryBatch(custom_file, generation, (record,)),
            ProviderDiscoveryBatch(generated, generation, ()),
            ProviderDiscoveryBatch(mcp, generation, ()),
            _builtin_batch(generation),
            _nonebot_batch(generation),
        ),
    )


def _file_legacy_schema(artifact: ToolArtifact) -> dict:
    spec = artifact.spec
    assert spec.policy is not None
    schema = {
        **spec.as_legacy_schema(),
        "source": "custom_file",
        "declared_effect": artifact.contract.declared_effect.value,
        "effective_effect": spec.effect.value,
        "tool_artifact": artifact,
        "artifact_digest": artifact.artifact_digest,
        "generation": artifact.generation,
    }
    if artifact.artifact_version == 2:
        schema.update(
            {
                "tool_contract_version": artifact.contract.contract_version,
                "artifact_digest_version": artifact.artifact_version,
                "requested_capabilities": (
                    artifact.contract.requested_capabilities
                ),
                "detected_capabilities": artifact.contract.detected_capabilities,
                "admin_capabilities": artifact.contract.admin_capabilities,
                "effective_capabilities": (
                    artifact.contract.effective_capabilities
                ),
                "capability_policy": spec.policy.capability_contract(),
            }
        )
    return schema


def _generated_artifact(
    spec: ToolSpec,
    *,
    generation: int,
    contract_version: int = 2,
) -> ToolArtifact:
    source = f"async def {spec.name}(value='ok'):\n    return value\n".encode()
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    manifest = {
        "bundle_id": "snapshot_bundle",
        "description": "snapshot bundle",
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
                "dependencies": list(spec.dependencies),
            }
        ],
    }
    digest = canonical_bundle_digest(manifest, source, tests_source)
    return ToolArtifact(
        tool_name=spec.name,
        handler_name=spec.name,
        source=source,
        source_hash=source_sha256(source),
        schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
        spec=spec,
        contract=ToolContractSnapshot.from_spec(
            spec,
            contract_version=contract_version,
        ),
        source_type="generated",
        generation=generation,
        filename="tool.py",
        tests_source=tests_source,
        bundle_manifest=manifest,
        bundle_id="snapshot_bundle",
        bundle_digest=digest,
    )


def _generated_catalog(
    artifact: ToolArtifact,
    *,
    generation: int,
) -> ProviderCatalogSnapshot:
    registered = ProviderRegistration.from_provider(registered_tool_provider)
    custom_file = ProviderRegistration.from_provider(file_tool_provider)
    generated = ProviderRegistration.from_provider(generated_tool_provider)
    mcp = ProviderRegistration.from_provider(mcp_tool_provider)
    record = DiscoveredTool(
        provider_id=generated.provider_id,
        source=generated.source,
        trust=generated.trust,
        generation=generation,
        spec=artifact.spec,
        artifact=artifact,
    )
    return provider_registry.build_snapshot(
        generation,
        (
            ProviderDiscoveryBatch(registered, generation, ()),
            ProviderDiscoveryBatch(custom_file, generation, ()),
            ProviderDiscoveryBatch(generated, generation, (record,)),
            ProviderDiscoveryBatch(mcp, generation, ()),
            _builtin_batch(generation),
            _nonebot_batch(generation),
        ),
    )


def _generated_legacy_schema(artifact: ToolArtifact) -> dict:
    spec = artifact.spec
    assert spec.policy is not None
    contract = artifact.contract
    schema = {
        **spec.as_legacy_schema(),
        "source": "generated",
        "bundle_id": artifact.bundle_id,
        "bundle_digest": artifact.bundle_digest,
        "requested_permission": contract.requested_permission,
        "effective_permission": contract.effective_permission,
        "declared_effect": contract.declared_effect.value,
        "effective_effect": contract.effective_effect.value,
        "user_policy_approved": spec.permission == "user",
        "requested_capabilities": contract.requested_capabilities,
        "effective_capabilities": contract.effective_capabilities,
        "tool_artifact": artifact,
        "artifact_digest": artifact.artifact_digest,
        "generation": artifact.generation,
    }
    if artifact.artifact_version == 2:
        schema.update(
            {
                "tool_contract_version": contract.contract_version,
                "artifact_digest_version": artifact.artifact_version,
                "detected_capabilities": contract.detected_capabilities,
                "admin_capabilities": contract.admin_capabilities,
                "capability_policy": spec.policy.capability_contract(),
            }
        )
    return schema


def _generated_state(artifact: ToolArtifact) -> LifecycleState:
    assert artifact.bundle_id is not None
    assert artifact.bundle_digest is not None
    version = VersionRecord(
        bundle_id=artifact.bundle_id,
        digest=artifact.bundle_digest,
        state=VersionState.ACTIVATED,
        source_draft_id="draft000001",
        created_at=1,
        approved_at=2,
        activated_at=3,
    )
    return LifecycleState(
        revision=1,
        drafts={},
        versions={artifact.bundle_id: {artifact.bundle_digest: version}},
        active={artifact.bundle_id: artifact.bundle_digest},
        permission_grants={},
    )


def _mcp_catalog(
    spec: ToolSpec,
    *,
    generation: int,
) -> ProviderCatalogSnapshot:
    registered = ProviderRegistration.from_provider(registered_tool_provider)
    custom_file = ProviderRegistration.from_provider(file_tool_provider)
    generated = ProviderRegistration.from_provider(generated_tool_provider)
    mcp = ProviderRegistration.from_provider(mcp_tool_provider)
    record = DiscoveredTool(
        provider_id=mcp.provider_id,
        source=mcp.source,
        trust=mcp.trust,
        generation=generation,
        spec=spec,
    )
    return provider_registry.build_snapshot(
        generation,
        (
            ProviderDiscoveryBatch(registered, generation, ()),
            ProviderDiscoveryBatch(custom_file, generation, ()),
            ProviderDiscoveryBatch(generated, generation, ()),
            ProviderDiscoveryBatch(mcp, generation, (record,)),
            _builtin_batch(generation),
            _nonebot_batch(generation),
        ),
    )


def _mcp_legacy_schema(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
        "func": spec.handler,
        "source": "mcp",
    }


def test_tool_snapshot_generated_stamp_is_detached_and_immutable() -> None:
    active = {"weather": "a" * 64}
    snapshot = ToolSnapshot(
        generation=3,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        generated_state_revision=8,
        generated_state_digest="b" * 64,
        generated_active=active,
    )
    active["weather"] = "c" * 64

    assert snapshot.generated_state_revision == 8
    assert snapshot.generated_state_digest == "b" * 64
    assert snapshot.generated_active == {"weather": "a" * 64}
    with pytest.raises(TypeError):
        snapshot.generated_active["weather"] = "d" * 64


def test_legacy_tool_snapshot_constructor_gets_empty_generated_stamp() -> None:
    snapshot = ToolSnapshot(
        generation=0,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )

    assert snapshot.generated_state_revision == 0
    assert snapshot.generated_state_digest == ""
    assert snapshot.generated_active == {}
    assert snapshot.provider_catalog is not None
    assert snapshot.provider_catalog.schema_version == 3
    assert snapshot.provider_catalog.registrations == {}
    assert snapshot.provider_catalog.tools == {}


def test_tool_snapshot_dual_view_keeps_exact_registered_identity() -> None:
    helper = ToolSpec(
        name="snapshot_helper",
        description="snapshot helper",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    registered = ToolSpec(
        name="snapshot_registered",
        description="snapshot registered",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        handler=_handler,
        dependencies=("snapshot_helper",),
    )
    catalog = _registered_catalog((registered, helper), generation=12)
    legacy_tools = {
        spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        for spec in (registered, helper)
    }

    snapshot = ToolSnapshot(
        generation=12,
        plugin_info={},
        custom_tools=legacy_tools,
        tool_dependencies={
            "snapshot_registered": {"snapshot_helper", "optional_plugin"}
        },
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )

    assert snapshot.provider_catalog is catalog
    assert snapshot.custom_tools[registered.name]["tool_spec"] is registered
    assert catalog.tools[registered.name].spec is registered
    assert catalog.tools_for_provider("registered")[1].spec is registered


def test_tool_snapshot_builtin_shadow_requires_canonical_spec_identity() -> None:
    catalog = _registered_catalog((), generation=14)
    builtin_spec = builtin_tool_specs()[0]
    record = catalog.tools[builtin_spec.name]

    snapshot = ToolSnapshot(
        generation=14,
        plugin_info={},
        custom_tools={},
        tool_dependencies={builtin_spec.name: {"optional_plugin"}},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )

    builtin_records = snapshot.provider_catalog.tools_for_provider("builtin")
    assert len(builtin_records) == len(builtin_tool_specs())
    assert record in builtin_records
    assert all(
        snapshot.provider_catalog.tools[spec.name].spec is spec
        for spec in builtin_tool_specs()
    )
    assert record.spec is builtin_spec
    assert builtin_spec.name not in snapshot.custom_tools

    drifted = replace(
        record,
        spec=replace(builtin_spec, description="drifted builtin"),
    )
    drifted_catalog = ProviderCatalogSnapshot(
        generation=14,
        registrations=catalog.registrations,
        tools={**catalog.tools, builtin_spec.name: drifted},
    )
    with pytest.raises(ValueError, match="ToolSpec"):
        ToolSnapshot(
            generation=14,
            plugin_info={},
            custom_tools={},
            tool_dependencies={},
            mcp_tool_names=set(),
            provider_catalog=drifted_catalog,
        )


def test_tool_snapshot_nonebot_plugin_shadow_keeps_adapter_identity() -> None:
    legacy, specs = build_nonebot_plugin_candidate(
        {
            "plugin_weather": {
                "name": "Weather",
                "description": "legacy weather plugin",
                "usage": "/weather",
                "dependencies": ["optional_plugin"],
            }
        }
    )
    base = _registered_catalog((), generation=15)
    registration = ProviderRegistration.from_provider(
        nonebot_plugin_provider
    )
    record = DiscoveredTool(
        provider_id=registration.provider_id,
        source=registration.source,
        trust=registration.trust,
        generation=15,
        spec=specs[0],
    )
    catalog = ProviderCatalogSnapshot(
        generation=15,
        registrations=base.registrations,
        tools={**base.tools, specs[0].name: record},
    )

    snapshot = ToolSnapshot(
        generation=15,
        plugin_info=legacy,
        custom_tools={},
        tool_dependencies={"plugin_weather": {"optional_plugin"}},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )

    entry = snapshot.plugin_info["plugin_weather"]
    assert entry["tool_spec"] is specs[0]
    assert entry["source"] == "nonebot_plugin"
    assert record.spec is specs[0]
    assert record.spec.effect is ToolEffect.MUTATING
    assert record.spec.permission == "user"
    assert "plugin_weather" not in snapshot.custom_tools

    drifted = {
        name: dict(info)
        for name, info in legacy.items()
    }
    drifted["plugin_weather"]["description"] = "drifted"
    with pytest.raises(ValueError, match="description"):
        ToolSnapshot(
            generation=15,
            plugin_info=drifted,
            custom_tools={},
            tool_dependencies={"plugin_weather": {"optional_plugin"}},
            mcp_tool_names=set(),
            provider_catalog=catalog,
        )


def test_tool_snapshot_dual_view_fails_closed_for_drift() -> None:
    helper = ToolSpec(
        name="parity_helper",
        description="parity helper",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    registered = ToolSpec(
        name="parity_registered",
        description="parity registered",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        dependencies=("parity_helper",),
    )
    catalog = _registered_catalog((registered, helper), generation=13)
    legacy_tools = {
        spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        for spec in (registered, helper)
    }
    common = {
        "generation": 13,
        "plugin_info": {},
        "custom_tools": legacy_tools,
        "tool_dependencies": {"parity_registered": {"parity_helper"}},
        "mcp_tool_names": set(),
        "provider_catalog": catalog,
    }

    missing = dict(legacy_tools)
    missing.pop("parity_registered")
    with pytest.raises(ValueError, match="工具集合"):
        ToolSnapshot(**{**common, "custom_tools": missing})

    mutated = {name: dict(schema) for name, schema in legacy_tools.items()}
    mutated["parity_registered"]["tool_spec"] = ToolSpec(
        name="parity_registered",
        description="parity registered",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        dependencies=("parity_helper",),
    )
    with pytest.raises(ValueError, match="ToolSpec"):
        ToolSnapshot(**{**common, "custom_tools": mutated})

    with pytest.raises(ValueError, match="dependencies"):
        ToolSnapshot(**{**common, "tool_dependencies": {}})
    with pytest.raises(ValueError, match="generation"):
        ToolSnapshot(**{**common, "generation": 14})

    wrong_registration = ProviderRegistration(
        provider_id="registered",
        source=ToolSource.BUILTIN,
        trust=ToolTrustLevel.TRUSTED,
    )
    wrong_catalog = ProviderCatalogSnapshot(
        generation=13,
        registrations={"registered": wrong_registration},
        tools={},
    )
    with pytest.raises(ValueError, match="identity"):
        ToolSnapshot(
            generation=13,
            plugin_info={},
            custom_tools={},
            tool_dependencies={},
            mcp_tool_names=set(),
            provider_catalog=wrong_catalog,
        )


def test_tool_snapshot_dual_view_keeps_exact_file_artifact_identity() -> None:
    spec = ToolSpec(
        name="snapshot_file",
        description="snapshot file",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    artifact = _file_artifact(spec, generation=15)
    catalog = _file_catalog(artifact, generation=15)
    legacy = _file_legacy_schema(artifact)

    snapshot = ToolSnapshot(
        generation=15,
        plugin_info={},
        custom_tools={spec.name: legacy},
        tool_dependencies={spec.name: {"optional_plugin"}},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )

    discovered = snapshot.provider_catalog.tools[spec.name]
    assert discovered.artifact is artifact
    assert discovered.spec is artifact.spec
    assert snapshot.custom_tools[spec.name]["tool_artifact"] is artifact
    assert snapshot.custom_tools[spec.name]["tool_spec"] is artifact.spec

    mutated = dict(legacy)
    mutated["artifact_digest"] = "0" * 64
    with pytest.raises(ValueError, match="artifact_digest"):
        ToolSnapshot(
            generation=15,
            plugin_info={},
            custom_tools={spec.name: mutated},
            tool_dependencies={},
            mcp_tool_names=set(),
            provider_catalog=catalog,
        )


def test_tool_snapshot_dual_view_keeps_exact_generated_artifact_identity() -> None:
    spec = ToolSpec(
        name="snapshot_generated",
        description="snapshot generated",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    artifact = _generated_artifact(spec, generation=16)
    catalog = _generated_catalog(artifact, generation=16)
    legacy = _generated_legacy_schema(artifact)

    snapshot = ToolSnapshot(
        generation=16,
        plugin_info={},
        custom_tools={spec.name: legacy},
        tool_dependencies={spec.name: {"optional_plugin"}},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )

    discovered = snapshot.provider_catalog.tools[spec.name]
    assert discovered.artifact is artifact
    assert discovered.spec is artifact.spec
    assert snapshot.custom_tools[spec.name]["tool_artifact"] is artifact
    assert snapshot.custom_tools[spec.name]["bundle_digest"] == (
        artifact.bundle_digest
    )

    mutated = dict(legacy)
    mutated["effective_capabilities"] = {}
    with pytest.raises(ValueError, match="effective_capabilities"):
        ToolSnapshot(
            generation=16,
            plugin_info={},
            custom_tools={spec.name: mutated},
            tool_dependencies={},
            mcp_tool_names=set(),
            provider_catalog=catalog,
        )


def test_tool_snapshot_dual_reads_v1_artifact_sidecars() -> None:
    file_spec = ToolSpec(
        name="legacy_snapshot_file",
        description="legacy snapshot file",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    file_artifact = _file_artifact(
        file_spec,
        generation=18,
        contract_version=1,
    )
    file_snapshot = ToolSnapshot(
        generation=18,
        plugin_info={},
        custom_tools={
            file_spec.name: _file_legacy_schema(file_artifact)
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_file_catalog(file_artifact, generation=18),
    )
    assert file_snapshot.custom_tools[file_spec.name][
        "tool_artifact"
    ].artifact_version == 1

    generated_spec = ToolSpec(
        name="legacy_snapshot_generated",
        description="legacy snapshot generated",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=19,
        contract_version=1,
    )
    generated_snapshot = ToolSnapshot(
        generation=19,
        plugin_info={},
        custom_tools={
            generated_spec.name: _generated_legacy_schema(
                generated_artifact
            )
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_generated_catalog(
            generated_artifact,
            generation=19,
        ),
    )
    assert generated_snapshot.custom_tools[generated_spec.name][
        "tool_artifact"
    ].artifact_version == 1


def test_tool_snapshot_dual_view_keeps_mcp_legacy_schema_and_sidecar() -> None:
    spec = ToolSpec(
        name="mcp__snapshot__echo",
        description="snapshot mcp",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        handler=_handler,
    )
    catalog = _mcp_catalog(spec, generation=17)
    legacy = _mcp_legacy_schema(spec)

    snapshot = ToolSnapshot(
        generation=17,
        plugin_info={},
        custom_tools={spec.name: legacy},
        tool_dependencies={},
        mcp_tool_names={spec.name},
        provider_catalog=catalog,
    )

    discovered = snapshot.provider_catalog.tools[spec.name]
    assert discovered.provider_id == "mcp"
    assert discovered.spec is spec
    assert discovered.artifact is None
    assert snapshot.custom_tools[spec.name]["func"] is spec.handler
    assert "tool_spec" not in snapshot.custom_tools[spec.name]
    assert snapshot.mcp_tool_names == {spec.name}

    mutated = dict(legacy)
    mutated["description"] = "drifted"
    with pytest.raises(ValueError, match="description"):
        ToolSnapshot(
            generation=17,
            plugin_info={},
            custom_tools={spec.name: mutated},
            tool_dependencies={},
            mcp_tool_names={spec.name},
            provider_catalog=catalog,
        )
    with pytest.raises(ValueError, match="sidecar"):
        ToolSnapshot(
            generation=17,
            plugin_info={},
            custom_tools={spec.name: legacy},
            tool_dependencies={},
            mcp_tool_names=set(),
            provider_catalog=catalog,
        )


def test_load_custom_tools_forwards_generated_state_and_source_overrides(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    generated_state = object()
    source_overrides = {("weather", "a" * 64): b"source"}
    received: dict = {}

    def load_files(_files, *, generation: int):
        assert generation == 23
        return {}, {}

    def load_generated(
        *,
        generation: int,
        generated_state,
        generated_source_overrides,
    ):
        received.update(
            generation=generation,
            generated_state=generated_state,
            generated_source_overrides=generated_source_overrides,
        )
        return {}, {}

    monkeypatch.setattr(manager_module.tool_registry, "snapshot", lambda: {})
    monkeypatch.setattr(manager_module, "load_file_tools", load_files)
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        load_generated,
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tools, dependencies = tool_manager.load_custom_tools(
        commit=False,
        generation=23,
        generated_state=generated_state,
        generated_source_overrides=source_overrides,
    )

    assert tools == {}
    assert dependencies == {}
    assert received["generation"] == 23
    assert received["generated_state"] is generated_state
    assert received["generated_source_overrides"] is source_overrides


def test_load_custom_tools_defaults_remain_compatible_with_legacy_store(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    calls: list[int] = []

    def load_files(_files, *, generation: int):
        return {}, {}

    def load_generated(*, generation: int):
        calls.append(generation)
        return {}, {}

    monkeypatch.setattr(manager_module.tool_registry, "snapshot", lambda: {})
    monkeypatch.setattr(manager_module, "load_file_tools", load_files)
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        load_generated,
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tool_manager.load_custom_tools(commit=False, generation=11)

    assert calls == [11]


@pytest.mark.asyncio
async def test_load_custom_tools_uses_explicit_registered_shadow_snapshot(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    helper = ToolSpec(
        name="registered_helper",
        description="helper",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    registered = ToolSpec(
        name="registered_shadow",
        description="registered shadow",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        handler=_handler,
        dependencies=("registered_helper",),
    )
    transaction_snapshot = {
        registered.name: registered,
        helper.name: helper,
    }
    discovery = await registered_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=31,
            resources=RegisteredToolResources(tuple(transaction_snapshot.values())),
        )
    )

    def forbidden_snapshot():
        raise AssertionError("legacy loader must reuse the transaction snapshot")

    monkeypatch.setattr(manager_module.tool_registry, "snapshot", forbidden_snapshot)
    monkeypatch.setattr(manager_module, "load_file_tools", lambda *_args, **_kwargs: ({}, {}))
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        lambda **_kwargs: ({}, {}),
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tools, dependencies = tool_manager.load_custom_tools(
        commit=False,
        generation=31,
        registered_tools=transaction_snapshot,
        registered_discovery=discovery,
    )

    assert set(tools) == {"registered_shadow", "registered_helper"}
    assert tools["registered_shadow"] == {
        **registered.as_legacy_schema(),
        "source": "registered",
    }
    assert dependencies == {"registered_shadow": {"registered_helper"}}


@pytest.mark.asyncio
async def test_load_custom_tools_reuses_explicit_file_candidate(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    spec = ToolSpec(
        name="file_shadow",
        description="file shadow",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    artifact = _file_artifact(spec, generation=32)
    file_tools = {spec.name: _file_legacy_schema(artifact)}
    file_dependencies = {spec.name: {"legacy_optional"}}
    discovery = await file_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=32,
            resources=FileToolResources.from_legacy_tools(file_tools),
        )
    )

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("legacy merge must reuse the transaction file candidate")

    monkeypatch.setattr(manager_module, "load_file_tools", forbidden_load)
    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        lambda **_kwargs: ({}, {}),
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tools, dependencies = tool_manager.load_custom_tools(
        commit=False,
        generation=32,
        registered_tools={},
        registered_discovery=(),
        file_tool_candidate=(file_tools, file_dependencies),
        file_discovery=discovery,
    )

    assert tools[spec.name]["tool_artifact"] is artifact
    assert tools[spec.name]["tool_spec"] is artifact.spec
    assert dependencies == {spec.name: {"legacy_optional"}}


@pytest.mark.asyncio
async def test_load_custom_tools_reuses_explicit_generated_candidate(
    monkeypatch,
) -> None:
    manager_module = importlib.import_module("nonebot_plugin_moellmchats.tool_manager")
    spec = ToolSpec(
        name="generated_shadow",
        description="generated shadow",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    artifact = _generated_artifact(spec, generation=33)
    generated_tools = {spec.name: _generated_legacy_schema(artifact)}
    generated_dependencies = {spec.name: {"legacy_optional"}}
    discovery = await generated_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=33,
            resources=GeneratedToolResources.from_legacy_tools(
                lifecycle_state=_generated_state(artifact),
                source_overrides=None,
                legacy_tools=generated_tools,
            ),
        )
    )

    def forbidden_load(*_args, **_kwargs):
        raise AssertionError(
            "legacy merge must reuse the transaction generated candidate"
        )

    monkeypatch.setattr(
        manager_module.generated_tool_store,
        "load_active_tools",
        forbidden_load,
    )
    monkeypatch.setattr(
        tool_manager,
        "_merge_dependencies_from_custom_plugin_info",
        lambda _dependencies: None,
    )

    tools, dependencies = tool_manager.load_custom_tools(
        commit=False,
        generation=33,
        registered_tools={},
        registered_discovery=(),
        file_tool_candidate=({}, {}),
        file_discovery=(),
        generated_tool_candidate=(
            generated_tools,
            generated_dependencies,
        ),
        generated_discovery=discovery,
    )

    assert tools[spec.name]["tool_artifact"] is artifact
    assert tools[spec.name]["tool_spec"] is artifact.spec
    assert dependencies == {spec.name: {"legacy_optional"}}


def test_tool_manager_snapshot_returns_active_runtime_snapshot(monkeypatch) -> None:
    authoritative = object()
    monkeypatch.setattr(
        runtime_snapshots,
        "active",
        lambda: SimpleNamespace(tool_snapshot=authoritative),
    )

    assert tool_manager.snapshot() is authoritative


def test_tool_manager_snapshot_bootstrap_falls_back_to_detached_mirrors(
    monkeypatch,
) -> None:
    plugins = {"plugin": {"description": "old"}}
    custom_tools = {"tool": {"description": "old"}}
    dependencies = {"tool": {"plugin"}}
    mcp_names = {"tool"}
    monkeypatch.setattr(runtime_snapshots, "active", lambda: None)
    monkeypatch.setattr(runtime_metrics, "reload_generation", 17)
    monkeypatch.setattr(tool_manager, "plugin_info", plugins)
    monkeypatch.setattr(tool_manager, "custom_tools", custom_tools)
    monkeypatch.setattr(tool_manager, "tool_dependencies", dependencies)
    monkeypatch.setattr(tool_manager, "mcp_tool_names", mcp_names)

    snapshot = tool_manager.snapshot()
    plugins["plugin"]["description"] = "changed"
    custom_tools["other"] = {"description": "changed"}
    dependencies["tool"].add("other")
    mcp_names.add("other")

    assert snapshot.generation == 17
    assert snapshot.plugin_info["plugin"]["description"] == "old"
    assert snapshot.custom_tools == {"tool": {"description": "old"}}
    assert snapshot.tool_dependencies == {"tool": {"plugin"}}
    assert snapshot.mcp_tool_names == {"tool"}
    with pytest.raises(TypeError):
        snapshot.custom_tools["tool"]["description"] = "tampered"


def test_tool_manager_brief_catalog_delegates_to_current_snapshot(
    monkeypatch,
) -> None:
    calls: list[bool] = []

    class AuthoritativeSnapshot:
        def get_brief_catalog(self, *, is_superuser: bool = False) -> str:
            calls.append(is_superuser)
            return "provider catalog"

    monkeypatch.setattr(tool_manager, "plugin_info", {"loaded": {}})
    monkeypatch.setattr(tool_manager, "snapshot", AuthoritativeSnapshot)

    assert tool_manager.get_brief_catalog(is_superuser=True) == "provider catalog"
    assert calls == [True]


def test_real_frozen_snapshot_filters_permissions_and_thaws_model_schema(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="admin_only",
        description="admin only",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=_handler,
        permission="superuser",
    )
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={"admin_only": spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)

    assert not isinstance(snapshot.custom_tools["admin_only"], dict)
    assert "admin_only" not in snapshot.get_brief_catalog(is_superuser=False)
    assert snapshot.get_tool_schema(["admin_only"], is_superuser=False) == []
    assert "admin_only" in snapshot.get_brief_catalog(is_superuser=True)

    schema = snapshot.get_tool_schema(["admin_only"], is_superuser=True)
    json.dumps(schema)
    parameters = schema[0]["function"]["parameters"]
    assert isinstance(parameters, dict)
    assert isinstance(parameters["properties"], dict)
    parameters["properties"]["value"]["type"] = "integer"
    assert (
        snapshot.custom_tools["admin_only"]["parameters"]["properties"]
        ["value"]["type"]
        == "string"
    )


def test_categorize_provider_cutover_matches_legacy_and_filters_trust(
    monkeypatch,
) -> None:
    user_spec = ToolSpec(
        name="user_tool",
        description="user tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    admin_spec = ToolSpec(
        name="admin_tool",
        description="admin tool",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    catalog = _registered_catalog(
        (user_spec, admin_spec),
        generation=41,
    )
    snapshot = ToolSnapshot(
        generation=41,
        plugin_info={},
        custom_tools={
            user_spec.name: {
                **user_spec.as_legacy_schema(),
                "source": "registered",
            },
            admin_spec.name: {
                **admin_spec.as_legacy_schema(),
                "source": "registered",
            },
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: True)
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])

    user_legacy = snapshot.get_brief_catalog(
        is_superuser=False,
        provider_cutover=False,
    )
    user_provider = snapshot.get_brief_catalog(
        is_superuser=False,
        provider_cutover=True,
    )
    assert user_provider == user_legacy
    assert "user_tool" in user_provider
    assert "admin_tool" not in user_provider
    assert "web_search" in user_provider

    admin_legacy = snapshot.get_brief_catalog(
        is_superuser=True,
        provider_cutover=False,
    )
    admin_provider = snapshot.get_brief_catalog(
        is_superuser=True,
        provider_cutover=True,
    )
    assert admin_provider == admin_legacy
    assert "admin_tool" in admin_provider


def test_categorize_provider_cutover_matches_all_sources_and_feature_filters(
    monkeypatch,
) -> None:
    generation = 45
    registered_spec = ToolSpec(
        name="catalog_registered",
        description="registered description",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    file_spec = ToolSpec(
        name="catalog_file",
        description="file description",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    generated_spec = ToolSpec(
        name="catalog_generated",
        description="generated description",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    mcp_spec = ToolSpec(
        name="mcp__catalog__echo",
        description="mcp description",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    plugin_info, plugin_specs = build_nonebot_plugin_candidate(
        {
            "catalog_plugin": with_menu_discovery(
                {
                    "name": "Catalog Plugin",
                    "description": "plugin description",
                    "usage": "/catalog",
                },
                [
                    {
                        "func": "目录动作",
                        "brief_des": "feature description",
                        "trigger_method": "命令触发",
                        "trigger_condition": "/catalog",
                    }
                ],
            )
        }
    )
    file_artifact = _file_artifact(file_spec, generation=generation)
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=generation,
    )
    base = _registered_catalog((registered_spec,), generation=generation)
    nonebot_registration = ProviderRegistration.from_provider(
        nonebot_plugin_provider
    )
    nonebot_record = DiscoveredTool(
        provider_id=nonebot_registration.provider_id,
        source=nonebot_registration.source,
        trust=nonebot_registration.trust,
        generation=generation,
        spec=plugin_specs[0],
    )
    catalog = ProviderCatalogSnapshot(
        generation=generation,
        registrations=base.registrations,
        tools={
            **base.tools,
            file_spec.name: _file_catalog(
                file_artifact,
                generation=generation,
            ).tools[file_spec.name],
            generated_spec.name: _generated_catalog(
                generated_artifact,
                generation=generation,
            ).tools[generated_spec.name],
            mcp_spec.name: _mcp_catalog(
                mcp_spec,
                generation=generation,
            ).tools[mcp_spec.name],
            plugin_specs[0].name: nonebot_record,
        },
    )
    snapshot = ToolSnapshot(
        generation=generation,
        plugin_info=plugin_info,
        custom_tools={
            registered_spec.name: {
                **registered_spec.as_legacy_schema(),
                "source": "registered",
            },
            file_spec.name: _file_legacy_schema(file_artifact),
            generated_spec.name: _generated_legacy_schema(
                generated_artifact
            ),
            mcp_spec.name: _mcp_legacy_schema(mcp_spec),
        },
        tool_dependencies={},
        mcp_tool_names={mcp_spec.name},
        provider_catalog=catalog,
    )
    feature_flags = {"tools": True, "search": True}
    blacklist: list[str] = []
    monkeypatch.setattr(
        model_selector,
        "get_use_tools",
        lambda: feature_flags["tools"],
    )
    monkeypatch.setattr(
        model_selector,
        "get_web_search",
        lambda: feature_flags["search"],
    )
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: blacklist)

    expected = "\n".join(
        (
            "- catalog_plugin | Catalog Plugin > 目录动作 | "
            "feature description | 命令: /catalog",
            "- catalog_registered | 自定义函数 | registered description",
            "- catalog_file | 自定义函数 | file description",
            "- catalog_generated | 自定义函数 | generated description",
            "- mcp__catalog__echo | MCP工具 | mcp description",
            "- web_search | 联网搜索 | 回答实时问题、新闻、天气与近期信息",
        )
    )
    assert snapshot.get_brief_catalog(provider_cutover=True) == expected
    assert snapshot.get_brief_catalog(provider_cutover=False) == expected

    blacklist.extend(
        (
            "catalog_plugin",
            "catalog_file",
            "mcp__catalog__*",
            "web_search",
        )
    )
    filtered = "\n".join(
        (
            "- catalog_registered | 自定义函数 | registered description",
            "- catalog_generated | 自定义函数 | generated description",
        )
    )
    assert snapshot.get_brief_catalog(provider_cutover=True) == filtered
    assert snapshot.get_brief_catalog(provider_cutover=False) == filtered

    blacklist.clear()
    feature_flags["tools"] = False
    assert snapshot.get_brief_catalog(provider_cutover=True) == (
        "- web_search | 联网搜索 | 回答实时问题、新闻、天气与近期信息"
    )
    feature_flags["search"] = False
    assert snapshot.get_brief_catalog(provider_cutover=True) == (
        "当前工具调用与联网功能均已关闭，无需返回任何插件。"
    )


def test_categorize_provider_cutover_handles_empty_and_legacy_snapshots(
    monkeypatch,
) -> None:
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    complete_empty = ToolSnapshot(
        generation=46,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((), generation=46),
    )
    assert complete_empty.get_brief_catalog(provider_cutover=True) == (
        "当前工具调用与联网功能均已关闭，无需返回任何插件。"
    )

    legacy_spec = ToolSpec(
        name="legacy_catalog",
        description="legacy catalog",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    legacy_snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={legacy_spec.name: legacy_spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    monkeypatch.setattr(
        ToolManager,
        "build_provider_brief_catalog",
        lambda **_kwargs: pytest.fail("旧快照不应进入 Provider consumer"),
    )
    assert legacy_snapshot.get_brief_catalog(provider_cutover=True) == (
        "- legacy_catalog | 自定义函数 | legacy catalog"
    )


def test_categorize_provider_cutover_fails_closed_on_parity_drift(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="provider_parity",
        description="provider parity",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=42,
        plugin_info={},
        custom_tools={
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=42),
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(
        ToolManager,
        "build_provider_brief_catalog",
        lambda **_kwargs: "drifted provider catalog",
    )

    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        snapshot.get_brief_catalog(provider_cutover=True)
    assert (
        snapshot.get_brief_catalog(provider_cutover=False)
        == "- provider_parity | 自定义函数 | provider parity"
    )


def test_categorize_provider_cutover_is_enabled_by_default(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="provider_default",
        description="provider default",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    catalog = _registered_catalog((spec,), generation=43)
    snapshot = ToolSnapshot(
        generation=43,
        plugin_info={},
        custom_tools={
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])

    def get_cutover_config(key: str, default=None):
        assert key == "provider_catalog_categorize_enabled"
        assert default is True
        return DEFAULT_CONFIG[key]

    provider_calls = []
    original_builder = ToolManager.build_provider_brief_catalog

    def track_provider_call(**kwargs):
        provider_calls.append(kwargs["provider_catalog"])
        return original_builder(**kwargs)

    monkeypatch.setattr(config_parser, "get_config", get_cutover_config)
    monkeypatch.setattr(
        ToolManager,
        "build_provider_brief_catalog",
        track_provider_call,
    )

    assert snapshot.get_brief_catalog() == (
        "- provider_default | 自定义函数 | provider default"
    )
    assert provider_calls == [catalog]


def test_categorize_provider_cutover_config_can_rollback_to_legacy(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="provider_rollback",
        description="provider rollback",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=44,
        plugin_info={},
        custom_tools={
            spec.name: {
                **spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=44),
    )
    monkeypatch.setattr(model_selector, "get_use_tools", lambda: True)
    monkeypatch.setattr(model_selector, "get_web_search", lambda: False)
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(config_parser, "get_config", lambda *_args: False)
    monkeypatch.setattr(
        ToolManager,
        "build_provider_brief_catalog",
        lambda **_kwargs: pytest.fail("rollback 不应读取 Provider consumer"),
    )

    assert snapshot.get_brief_catalog() == (
        "- provider_rollback | 自定义函数 | provider rollback"
    )


@pytest.mark.parametrize("flag", [1, "true", [], {}])
def test_categorize_provider_cutover_rejects_non_boolean_override(flag) -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    with pytest.raises(ValueError, match="cutover"):
        snapshot.get_brief_catalog(provider_cutover=flag)


@pytest.mark.parametrize("flag", [None, 1, "true", [], {}])
def test_categorize_provider_cutover_config_requires_boolean(flag) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate["provider_catalog_categorize_enabled"] = flag

    with pytest.raises(
        ValueError,
        match="provider_catalog_categorize_enabled",
    ):
        ConfigParser._validate(candidate)


def test_llm_payload_provider_cutover_matches_all_sources_and_dependencies(
    monkeypatch,
) -> None:
    generation = 47
    file_spec = ToolSpec(
        name="payload_file",
        description="file payload",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        dependencies=("mcp__payload__echo",),
        policy=ToolPolicy.configured(),
    )
    registered_spec = ToolSpec(
        name="payload_registered",
        description="registered payload",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        dependencies=(file_spec.name,),
        policy=ToolPolicy.configured(),
    )
    generated_spec = ToolSpec(
        name="payload_generated_admin",
        description="generated payload",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        permission="superuser",
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    mcp_spec = ToolSpec(
        name="mcp__payload__echo",
        description="mcp payload",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    plugin_info, plugin_specs = build_nonebot_plugin_candidate(
        {
            "payload_plugin": with_menu_discovery(
                {
                    "name": "Payload Plugin",
                    "description": "plugin payload",
                    "usage": "/payload",
                },
                [
                    {
                        "func": "公开动作",
                        "brief_des": "public payload action",
                        "trigger_method": "命令触发",
                        "trigger_condition": "/payload",
                    },
                    {
                        "func": "隐藏动作",
                        "brief_des": "hidden payload action",
                        "trigger_method": "命令触发",
                        "trigger_condition": "/payload-admin",
                        "pmn_hidden": True,
                    },
                ],
            )
        }
    )
    file_artifact = _file_artifact(file_spec, generation=generation)
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=generation,
    )
    base = _registered_catalog((registered_spec,), generation=generation)
    plugin_registration = ProviderRegistration.from_provider(
        nonebot_plugin_provider
    )
    plugin_record = DiscoveredTool(
        provider_id=plugin_registration.provider_id,
        source=plugin_registration.source,
        trust=plugin_registration.trust,
        generation=generation,
        spec=plugin_specs[0],
    )
    catalog = ProviderCatalogSnapshot(
        generation=generation,
        registrations=base.registrations,
        tools={
            **base.tools,
            file_spec.name: _file_catalog(
                file_artifact,
                generation=generation,
            ).tools[file_spec.name],
            generated_spec.name: _generated_catalog(
                generated_artifact,
                generation=generation,
            ).tools[generated_spec.name],
            mcp_spec.name: _mcp_catalog(
                mcp_spec,
                generation=generation,
            ).tools[mcp_spec.name],
            plugin_specs[0].name: plugin_record,
        },
    )
    snapshot = ToolSnapshot(
        generation=generation,
        plugin_info=plugin_info,
        custom_tools={
            registered_spec.name: {
                **registered_spec.as_legacy_schema(),
                "source": "registered",
            },
            file_spec.name: _file_legacy_schema(file_artifact),
            generated_spec.name: _generated_legacy_schema(generated_artifact),
            mcp_spec.name: _mcp_legacy_schema(mcp_spec),
        },
        tool_dependencies={
            registered_spec.name: {file_spec.name},
            file_spec.name: {mcp_spec.name},
        },
        mcp_tool_names={mcp_spec.name},
        provider_catalog=catalog,
    )
    blacklist: list[str] = []
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: blacklist)
    selected = {
        registered_spec.name,
        generated_spec.name,
        plugin_specs[0].name,
        "web_search",
        "stale_resident",
    }

    legacy_names, legacy_schema = snapshot.get_llm_payload_tools(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=False,
        provider_cutover=False,
    )
    provider_names, provider_schema = snapshot.get_llm_payload_tools(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=False,
        provider_cutover=True,
    )
    assert provider_names == legacy_names == {
        *selected,
        file_spec.name,
        mcp_spec.name,
    }
    assert provider_schema == legacy_schema
    assert {item["function"]["name"] for item in provider_schema} == {
        registered_spec.name,
        file_spec.name,
        mcp_spec.name,
        plugin_specs[0].name,
        "web_search",
    }
    public_plugin_description = next(
        item["function"]["description"]
        for item in provider_schema
        if item["function"]["name"] == plugin_specs[0].name
    )
    assert "公开动作" in public_plugin_description
    assert "隐藏动作" not in public_plugin_description

    admin_legacy_names, admin_legacy_schema = snapshot.get_llm_payload_tools(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=True,
        provider_cutover=False,
    )
    admin_provider_names, admin_schema = snapshot.get_llm_payload_tools(
        selected,
        tools_enabled=True,
        search_enabled=True,
        is_superuser=True,
        provider_cutover=True,
    )
    assert admin_provider_names == admin_legacy_names
    assert admin_schema == admin_legacy_schema
    assert generated_spec.name in {
        item["function"]["name"] for item in admin_schema
    }
    admin_plugin_description = next(
        item["function"]["description"]
        for item in admin_schema
        if item["function"]["name"] == plugin_specs[0].name
    )
    assert "隐藏动作" in admin_plugin_description

    blacklist.extend((file_spec.name, "web_search"))
    filtered_names, filtered_schema = snapshot.get_llm_payload_tools(
        selected,
        tools_enabled=True,
        search_enabled=True,
        provider_cutover=True,
    )
    assert file_spec.name not in filtered_names
    assert mcp_spec.name not in filtered_names
    assert "web_search" not in filtered_names
    assert {item["function"]["name"] for item in filtered_schema} == {
        registered_spec.name,
        plugin_specs[0].name,
    }


def test_llm_payload_provider_cutover_fails_closed_on_dependency_drift(
    monkeypatch,
) -> None:
    root = ToolSpec(
        name="payload_dependency_root",
        description="dependency root",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    legacy_only = ToolSpec(
        name="payload_legacy_only_dependency",
        description="legacy only dependency",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=48,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
            for spec in (root, legacy_only)
        },
        # Provider parity deliberately permits additional legacy edges while
        # dual-publishing. The payload cutover must surface them instead of
        # silently treating the sidecar as canonical.
        tool_dependencies={root.name: {legacy_only.name}},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog(
            (root, legacy_only),
            generation=48,
        ),
    )
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])

    with pytest.raises(ProviderConsumerParityError, match="依赖视图"):
        snapshot.get_llm_payload_tools(
            {root.name},
            tools_enabled=True,
            search_enabled=False,
            provider_cutover=True,
        )

    names, schema = snapshot.get_llm_payload_tools(
        {root.name},
        tools_enabled=True,
        search_enabled=False,
        provider_cutover=False,
    )
    assert names == {root.name, legacy_only.name}
    assert {item["function"]["name"] for item in schema} == names


def test_llm_payload_provider_cutover_fails_closed_on_schema_drift(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="payload_schema_parity",
        description="payload schema parity",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=49,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=49),
    )
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(
        ToolManager,
        "build_provider_llm_payload_schema",
        lambda **_kwargs: [],
    )

    with pytest.raises(ProviderConsumerParityError, match="schema"):
        snapshot.get_llm_payload_tools(
            {spec.name},
            tools_enabled=True,
            search_enabled=False,
            provider_cutover=True,
        )
    _names, rollback_schema = snapshot.get_llm_payload_tools(
        {spec.name},
        tools_enabled=True,
        search_enabled=False,
        provider_cutover=False,
    )
    assert rollback_schema[0]["function"]["name"] == spec.name


def test_llm_payload_provider_cutover_handles_disabled_and_legacy_snapshots(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="payload_legacy_snapshot",
        description="payload legacy snapshot",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={spec.name: spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    monkeypatch.setattr(
        ToolManager,
        "build_provider_llm_payload_schema",
        lambda **_kwargs: pytest.fail("旧快照不应进入 Provider payload"),
    )

    names, schema = snapshot.get_llm_payload_tools(
        {spec.name},
        tools_enabled=True,
        search_enabled=False,
        provider_cutover=True,
    )
    assert names == {spec.name}
    assert schema[0]["function"]["name"] == spec.name

    disabled_names, disabled_schema = snapshot.get_llm_payload_tools(
        {spec.name},
        tools_enabled=False,
        search_enabled=True,
        provider_cutover=True,
    )
    assert disabled_names == {spec.name}
    assert disabled_schema == []


def test_llm_payload_provider_cutover_default_and_config_rollback(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="payload_default_cutover",
        description="payload default cutover",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    catalog = _registered_catalog((spec,), generation=50)
    snapshot = ToolSnapshot(
        generation=50,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )
    monkeypatch.setattr(model_selector, "get_tool_blacklist", lambda: [])
    provider_calls: list[ProviderCatalogSnapshot] = []
    original_builder = ToolManager.build_provider_llm_payload_schema

    def track_provider_call(**kwargs):
        provider_calls.append(kwargs["provider_catalog"])
        return original_builder(**kwargs)

    def get_default(key: str, default=None):
        assert key == "provider_catalog_llm_payload_enabled"
        assert default is True
        return DEFAULT_CONFIG[key]

    monkeypatch.setattr(config_parser, "get_config", get_default)
    monkeypatch.setattr(
        ToolManager,
        "build_provider_llm_payload_schema",
        track_provider_call,
    )
    snapshot.get_llm_payload_tools(
        {spec.name},
        tools_enabled=True,
        search_enabled=False,
    )
    assert provider_calls == [catalog]

    monkeypatch.setattr(config_parser, "get_config", lambda *_args: False)
    monkeypatch.setattr(
        ToolManager,
        "build_provider_llm_payload_schema",
        lambda **_kwargs: pytest.fail("rollback 不应读取 Provider payload"),
    )
    _names, schema = snapshot.get_llm_payload_tools(
        {spec.name},
        tools_enabled=True,
        search_enabled=False,
    )
    assert schema[0]["function"]["name"] == spec.name


@pytest.mark.parametrize("flag", [1, "true", [], {}])
def test_llm_payload_provider_cutover_rejects_non_boolean_override(flag) -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    with pytest.raises(ValueError, match="cutover"):
        snapshot.get_llm_payload_tools(
            set(),
            tools_enabled=True,
            search_enabled=False,
            provider_cutover=flag,
        )


@pytest.mark.parametrize("flag", [None, 1, "true", [], {}])
def test_llm_payload_provider_cutover_config_requires_boolean(flag) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate["provider_catalog_llm_payload_enabled"] = flag

    with pytest.raises(
        ValueError,
        match="provider_catalog_llm_payload_enabled",
    ):
        ConfigParser._validate(candidate)


def test_llm_tools_provider_cutover_resolves_all_six_sources() -> None:
    generation = 51
    registered_spec = ToolSpec(
        name="execution_registered",
        description="registered execution",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    registered_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            registered_spec.name: {
                **registered_spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog(
            (registered_spec,),
            generation=generation,
        ),
    )

    file_spec = ToolSpec(
        name="execution_file",
        description="file execution",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    file_artifact = _file_artifact(file_spec, generation=generation)
    file_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={file_spec.name: _file_legacy_schema(file_artifact)},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_file_catalog(
            file_artifact,
            generation=generation,
        ),
    )

    generated_spec = ToolSpec(
        name="execution_generated",
        description="generated execution",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=generation,
    )
    generated_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            generated_spec.name: _generated_legacy_schema(
                generated_artifact
            )
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_generated_catalog(
            generated_artifact,
            generation=generation,
        ),
    )

    mcp_spec = ToolSpec(
        name="mcp__execution__lookup",
        description="mcp execution",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
        handler=_handler,
    )
    mcp_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={mcp_spec.name: _mcp_legacy_schema(mcp_spec)},
        tool_dependencies={},
        mcp_tool_names={mcp_spec.name},
        provider_catalog=_mcp_catalog(mcp_spec, generation=generation),
    )

    plugin_info, plugin_specs = build_nonebot_plugin_candidate(
        {
            "execution_plugin": {
                "description": "plugin execution",
                "usage": "/execution",
            }
        }
    )
    base = _registered_catalog((), generation=generation)
    plugin_registration = ProviderRegistration.from_provider(
        nonebot_plugin_provider
    )
    plugin_record = DiscoveredTool(
        provider_id=plugin_registration.provider_id,
        source=plugin_registration.source,
        trust=plugin_registration.trust,
        generation=generation,
        spec=plugin_specs[0],
    )
    plugin_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info=plugin_info,
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=ProviderCatalogSnapshot(
            generation=generation,
            registrations=base.registrations,
            tools={**base.tools, plugin_specs[0].name: plugin_record},
        ),
    )
    builtin_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=base,
    )

    cases = (
        (
            registered_snapshot,
            registered_spec,
            ToolSource.REGISTERED,
            LlmToolExecutionRoute.CUSTOM_TOOL,
        ),
        (
            file_snapshot,
            file_spec,
            ToolSource.CUSTOM_FILE,
            LlmToolExecutionRoute.CUSTOM_TOOL,
        ),
        (
            generated_snapshot,
            generated_spec,
            ToolSource.GENERATED,
            LlmToolExecutionRoute.CUSTOM_TOOL,
        ),
        (
            mcp_snapshot,
            mcp_spec,
            ToolSource.MCP,
            LlmToolExecutionRoute.CUSTOM_TOOL,
        ),
        (
            builtin_snapshot,
            builtin_tool_specs()[0],
            ToolSource.BUILTIN,
            LlmToolExecutionRoute.BUILTIN_SEARCH,
        ),
        (
            plugin_snapshot,
            plugin_specs[0],
            ToolSource.NONEBOT_PLUGIN,
            LlmToolExecutionRoute.NONEBOT_PLUGIN,
        ),
    )
    for snapshot, spec, source, route in cases:
        legacy = snapshot.resolve_llm_tool_execution(
            spec.name,
            is_superuser=True,
            provider_cutover=False,
        )
        provider = snapshot.resolve_llm_tool_execution(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
        assert legacy is not None
        assert provider is not None
        assert not legacy.provider_authoritative
        assert provider.provider_authoritative
        assert provider.route is legacy.route is route
        assert provider.source is legacy.source is source
        assert provider.spec is spec
        assert provider.legacy_entry is legacy.legacy_entry
        assert provider.trust_decision is not None
        assert provider.trust_decision.operation is ToolTrustOperation.EXECUTION
        assert provider.trust_decision.allowed


def test_llm_tools_provider_trust_preserves_pending_transition() -> None:
    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="execution_mutating_admin",
        description="mutating admin execution",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=52,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=52),
    )

    user_view = snapshot.resolve_llm_tool_execution(
        spec.name,
        is_superuser=False,
        provider_cutover=True,
    )
    assert user_view is not None
    assert user_view.trust_decision is not None
    assert not user_view.trust_decision.allowed
    assert "超级用户" in user_view.trust_decision.reason

    admin_view = snapshot.resolve_llm_tool_execution(
        spec.name,
        is_superuser=True,
        provider_cutover=True,
    )
    assert admin_view is not None
    assert admin_view.trust_decision is not None
    assert not admin_view.trust_decision.allowed
    assert admin_view.trust_decision.confirmation_required
    assert "二阶段确认" in admin_view.trust_decision.reason


def test_llm_tools_provider_cutover_fails_closed_on_identity_drift(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="execution_parity",
        description="execution parity",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=53,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=53),
    )
    original = ToolSnapshot._legacy_llm_tool_execution_view

    def drifted_legacy(self, tool_name):
        view = original(self, tool_name)
        assert view is not None
        return replace(view, source=ToolSource.MCP)

    monkeypatch.setattr(
        ToolSnapshot,
        "_legacy_llm_tool_execution_view",
        drifted_legacy,
    )
    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        snapshot.resolve_llm_tool_execution(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
    rollback = snapshot.resolve_llm_tool_execution(
        spec.name,
        is_superuser=True,
        provider_cutover=False,
    )
    assert rollback is not None
    assert rollback.source is ToolSource.MCP


def test_llm_tools_provider_cutover_handles_unknown_and_legacy_snapshot() -> None:
    complete = ToolSnapshot(
        generation=54,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((), generation=54),
    )
    assert (
        complete.resolve_llm_tool_execution(
            "hallucinated_tool",
            is_superuser=False,
            provider_cutover=True,
        )
        is None
    )

    spec = ToolSpec(
        name="execution_legacy_snapshot",
        description="legacy execution",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    legacy = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={spec.name: spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    view = legacy.resolve_llm_tool_execution(
        spec.name,
        is_superuser=False,
        provider_cutover=True,
    )
    assert view is not None
    assert not view.provider_authoritative
    assert view.source is None
    assert view.spec is spec


def test_llm_tools_provider_cutover_default_and_config_rollback(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="execution_default_cutover",
        description="execution default",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=55,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=55),
    )

    def get_default(key: str, default=None):
        assert key == "provider_catalog_llm_tools_enabled"
        assert default is True
        return DEFAULT_CONFIG[key]

    monkeypatch.setattr(config_parser, "get_config", get_default)
    provider = snapshot.resolve_llm_tool_execution(
        spec.name,
        is_superuser=True,
    )
    assert provider is not None
    assert provider.provider_authoritative

    monkeypatch.setattr(config_parser, "get_config", lambda *_args: False)
    rollback = snapshot.resolve_llm_tool_execution(
        spec.name,
        is_superuser=True,
    )
    assert rollback is not None
    assert not rollback.provider_authoritative


@pytest.mark.parametrize("flag", [1, "true", [], {}])
def test_llm_tools_provider_cutover_rejects_non_boolean_override(flag) -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    with pytest.raises(ValueError, match="cutover"):
        snapshot.resolve_llm_tool_execution(
            "unknown",
            is_superuser=False,
            provider_cutover=flag,
        )


@pytest.mark.parametrize("flag", [None, 1, "true", [], {}])
def test_llm_tools_provider_cutover_config_requires_boolean(flag) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate["provider_catalog_llm_tools_enabled"] = flag

    with pytest.raises(
        ValueError,
        match="provider_catalog_llm_tools_enabled",
    ):
        ConfigParser._validate(candidate)


def test_pending_action_provider_view_is_canonical_for_custom_sources() -> None:
    async def mutate() -> str:
        return "changed"

    generation = 56
    registered_spec = ToolSpec(
        name="pending_registered",
        description="registered mutation",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        policy=ToolPolicy.configured(),
    )
    registered_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            registered_spec.name: {
                **registered_spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog(
            (registered_spec,),
            generation=generation,
        ),
    )

    file_spec = ToolSpec(
        name="pending_file",
        description="file mutation",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        policy=ToolPolicy.configured(),
    )
    file_artifact = _file_artifact(file_spec, generation=generation)
    file_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={file_spec.name: _file_legacy_schema(file_artifact)},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_file_catalog(
            file_artifact,
            generation=generation,
        ),
    )

    generated_spec = ToolSpec(
        name="pending_generated",
        description="generated mutation",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=generation,
    )
    generated_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            generated_spec.name: _generated_legacy_schema(
                generated_artifact
            )
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_generated_catalog(
            generated_artifact,
            generation=generation,
        ),
    )

    mcp_spec = ToolSpec(
        name="mcp__pending__mutate",
        description="mcp mutation",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    mcp_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={mcp_spec.name: _mcp_legacy_schema(mcp_spec)},
        tool_dependencies={},
        mcp_tool_names={mcp_spec.name},
        provider_catalog=_mcp_catalog(mcp_spec, generation=generation),
    )

    cases = (
        (registered_snapshot, registered_spec, ToolSource.REGISTERED, None),
        (file_snapshot, file_spec, ToolSource.CUSTOM_FILE, None),
        (
            generated_snapshot,
            generated_spec,
            ToolSource.GENERATED,
            generated_artifact.bundle_digest,
        ),
        (mcp_snapshot, mcp_spec, ToolSource.MCP, None),
    )
    for snapshot, spec, source, bundle_digest in cases:
        rollback = snapshot.resolve_pending_action_execution(
            spec.name,
            is_superuser=True,
            provider_cutover=False,
        )
        provider = snapshot.resolve_pending_action_execution(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
        assert isinstance(rollback, PendingActionExecutionView)
        assert isinstance(provider, PendingActionExecutionView)
        assert not rollback.provider_authoritative
        assert provider.provider_authoritative
        assert provider.source is rollback.source is source
        assert provider.spec is spec
        assert provider.legacy_entry is rollback.legacy_entry
        assert provider.execution_entry is not provider.legacy_entry
        assert provider.execution_entry["func"] is spec.handler
        assert provider.execution_entry["tool_spec"] is spec
        assert provider.bundle_digest == rollback.bundle_digest == bundle_digest
        assert provider.trust_decision is not None
        assert provider.trust_decision.operation is ToolTrustOperation.EXECUTION
        assert provider.trust_decision.confirmation_required
        assert provider.trust_decision.allowed


def test_pending_action_provider_rechecks_permission_after_confirmation() -> None:
    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="pending_admin",
        description="admin mutation",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=57,
        plugin_info={},
        custom_tools={spec.name: {**spec.as_legacy_schema(), "source": "registered"}},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=57),
    )

    denied = snapshot.resolve_pending_action_execution(
        spec.name,
        is_superuser=False,
        provider_cutover=True,
    )
    assert denied is not None
    assert denied.trust_decision is not None
    assert not denied.trust_decision.allowed
    assert "超级用户" in denied.trust_decision.reason

    allowed = snapshot.resolve_pending_action_execution(
        spec.name,
        is_superuser=True,
        provider_cutover=True,
    )
    assert allowed is not None
    assert allowed.trust_decision is not None
    assert allowed.trust_decision.allowed
    assert allowed.trust_decision.confirmation_required


def test_pending_action_provider_cutover_fails_closed_on_parity_drift(
    monkeypatch,
) -> None:
    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="pending_parity",
        description="pending parity",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=58,
        plugin_info={},
        custom_tools={spec.name: {**spec.as_legacy_schema(), "source": "registered"}},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=58),
    )
    original = ToolSnapshot._legacy_pending_action_execution_view

    def drifted_legacy(self, tool_name):
        view = original(self, tool_name)
        assert view is not None
        return replace(view, source=ToolSource.MCP)

    monkeypatch.setattr(
        ToolSnapshot,
        "_legacy_pending_action_execution_view",
        drifted_legacy,
    )
    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        snapshot.resolve_pending_action_execution(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
    rollback = snapshot.resolve_pending_action_execution(
        spec.name,
        is_superuser=True,
        provider_cutover=False,
    )
    assert rollback is not None
    assert rollback.source is ToolSource.MCP


def test_pending_action_provider_cutover_handles_unknown_and_legacy_snapshot() -> None:
    complete = ToolSnapshot(
        generation=59,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((), generation=59),
    )
    assert (
        complete.resolve_pending_action_execution(
            "unknown_pending",
            is_superuser=False,
            provider_cutover=True,
        )
        is None
    )

    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="pending_legacy",
        description="legacy pending",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
    )
    legacy = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={spec.name: spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    view = legacy.resolve_pending_action_execution(
        spec.name,
        is_superuser=True,
        provider_cutover=True,
    )
    assert view is not None
    assert not view.provider_authoritative
    assert view.source is None
    assert view.spec is spec


def test_pending_action_provider_cutover_default_and_config_rollback(
    monkeypatch,
) -> None:
    async def mutate() -> str:
        return "changed"

    spec = ToolSpec(
        name="pending_default_cutover",
        description="pending default",
        parameters={"type": "object", "properties": {}},
        handler=mutate,
        effect=ToolEffect.MUTATING,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=60,
        plugin_info={},
        custom_tools={spec.name: {**spec.as_legacy_schema(), "source": "registered"}},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=60),
    )

    def get_default(key: str, default=None):
        assert key == "provider_catalog_pending_actions_enabled"
        assert default is True
        return DEFAULT_CONFIG[key]

    monkeypatch.setattr(config_parser, "get_config", get_default)
    provider = snapshot.resolve_pending_action_execution(
        spec.name,
        is_superuser=True,
    )
    assert provider is not None
    assert provider.provider_authoritative

    monkeypatch.setattr(config_parser, "get_config", lambda *_args: False)
    rollback = snapshot.resolve_pending_action_execution(
        spec.name,
        is_superuser=True,
    )
    assert rollback is not None
    assert not rollback.provider_authoritative


@pytest.mark.parametrize("flag", [1, "true", [], {}])
def test_pending_action_provider_cutover_rejects_non_boolean_override(flag) -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    with pytest.raises(ValueError, match="cutover"):
        snapshot.resolve_pending_action_execution(
            "unknown",
            is_superuser=False,
            provider_cutover=flag,
        )


@pytest.mark.parametrize("flag", [None, 1, "true", [], {}])
def test_pending_action_provider_cutover_config_requires_boolean(flag) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate["provider_catalog_pending_actions_enabled"] = flag

    with pytest.raises(
        ValueError,
        match="provider_catalog_pending_actions_enabled",
    ):
        ConfigParser._validate(candidate)


def test_search_extractor_provider_view_is_canonical_for_custom_sources() -> None:
    generation = 61
    registered_spec = ToolSpec(
        name="extract_webpage",
        description="registered extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    registered_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            registered_spec.name: {
                **registered_spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog(
            (registered_spec,),
            generation=generation,
        ),
    )

    file_spec = ToolSpec(
        name="extract_webpage",
        description="file extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    file_artifact = _file_artifact(file_spec, generation=generation)
    file_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={file_spec.name: _file_legacy_schema(file_artifact)},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_file_catalog(file_artifact, generation=generation),
    )

    generated_spec = ToolSpec(
        name="extract_webpage",
        description="generated extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=generation,
    )
    generated_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            generated_spec.name: _generated_legacy_schema(
                generated_artifact
            )
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_generated_catalog(
            generated_artifact,
            generation=generation,
        ),
    )

    mcp_spec = ToolSpec(
        name="extract_webpage",
        description="mcp extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    mcp_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={mcp_spec.name: _mcp_legacy_schema(mcp_spec)},
        tool_dependencies={},
        mcp_tool_names={mcp_spec.name},
        provider_catalog=_mcp_catalog(mcp_spec, generation=generation),
    )

    cases = (
        (registered_snapshot, registered_spec, ToolSource.REGISTERED),
        (file_snapshot, file_spec, ToolSource.CUSTOM_FILE),
        (generated_snapshot, generated_spec, ToolSource.GENERATED),
        (mcp_snapshot, mcp_spec, ToolSource.MCP),
    )
    for snapshot, spec, source in cases:
        rollback = snapshot.resolve_search_extractor(
            is_superuser=True,
            provider_cutover=False,
        )
        provider = snapshot.resolve_search_extractor(
            is_superuser=True,
            provider_cutover=True,
        )
        assert isinstance(rollback, SearchExtractorView)
        assert isinstance(provider, SearchExtractorView)
        assert not rollback.provider_authoritative
        assert provider.provider_authoritative
        assert provider.source is rollback.source is source
        assert provider.spec is spec
        assert provider.legacy_entry is rollback.legacy_entry
        assert provider.trust_decision is not None
        assert provider.trust_decision.operation is ToolTrustOperation.SELECTION
        assert provider.trust_decision.allowed


def test_search_extractor_provider_selection_rechecks_actor_permission() -> None:
    spec = ToolSpec(
        name="extract_webpage",
        description="admin extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        permission="superuser",
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=62,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=62),
    )

    denied = snapshot.resolve_search_extractor(
        is_superuser=False,
        provider_cutover=True,
    )
    assert denied is not None
    assert denied.trust_decision is not None
    assert not denied.trust_decision.allowed
    assert "超级用户" in denied.trust_decision.reason

    allowed = snapshot.resolve_search_extractor(
        is_superuser=True,
        provider_cutover=True,
    )
    assert allowed is not None
    assert allowed.trust_decision is not None
    assert allowed.trust_decision.allowed


def test_search_extractor_provider_cutover_fails_closed_on_identity_drift(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="extract_webpage",
        description="parity extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=63,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=63),
    )
    original = ToolSnapshot._legacy_search_extractor_view

    def drifted_legacy(self):
        view = original(self)
        assert view is not None
        return replace(view, source=ToolSource.MCP)

    monkeypatch.setattr(
        ToolSnapshot,
        "_legacy_search_extractor_view",
        drifted_legacy,
    )
    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        snapshot.resolve_search_extractor(
            is_superuser=True,
            provider_cutover=True,
        )
    rollback = snapshot.resolve_search_extractor(
        is_superuser=True,
        provider_cutover=False,
    )
    assert rollback is not None
    assert rollback.source is ToolSource.MCP


def test_search_extractor_provider_cutover_handles_missing_and_legacy_snapshot() -> None:
    complete = ToolSnapshot(
        generation=64,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((), generation=64),
    )
    assert (
        complete.resolve_search_extractor(
            is_superuser=False,
            provider_cutover=True,
        )
        is None
    )

    spec = ToolSpec(
        name="extract_webpage",
        description="legacy extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    legacy = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={spec.name: spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    view = legacy.resolve_search_extractor(
        is_superuser=False,
        provider_cutover=True,
    )
    assert view is not None
    assert not view.provider_authoritative
    assert view.source is None
    assert view.spec is spec


def test_search_extractor_provider_cutover_default_and_config_rollback(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="extract_webpage",
        description="default extractor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=65,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=65),
    )

    def get_default(key: str, default=None):
        assert key == "provider_catalog_search_enabled"
        assert default is True
        return DEFAULT_CONFIG[key]

    monkeypatch.setattr(config_parser, "get_config", get_default)
    provider = snapshot.resolve_search_extractor(is_superuser=True)
    assert provider is not None
    assert provider.provider_authoritative

    monkeypatch.setattr(config_parser, "get_config", lambda *_args: False)
    rollback = snapshot.resolve_search_extractor(is_superuser=True)
    assert rollback is not None
    assert not rollback.provider_authoritative


@pytest.mark.parametrize("flag", [1, "true", [], {}])
def test_search_extractor_provider_cutover_rejects_non_boolean_override(
    flag,
) -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    with pytest.raises(ValueError, match="cutover"):
        snapshot.resolve_search_extractor(
            is_superuser=False,
            provider_cutover=flag,
        )


def test_search_extractor_provider_cutover_rejects_non_boolean_actor() -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
    )
    with pytest.raises(TypeError, match="is_superuser"):
        snapshot.resolve_search_extractor(
            is_superuser=1,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("flag", [None, 1, "true", [], {}])
def test_search_extractor_provider_cutover_config_requires_boolean(flag) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate["provider_catalog_search_enabled"] = flag

    with pytest.raises(
        ValueError,
        match="provider_catalog_search_enabled",
    ):
        ConfigParser._validate(candidate)


def test_management_provider_cutover_resolves_all_six_sources() -> None:
    generation = 67
    registered_spec = ToolSpec(
        name="management_registered",
        description="registered management",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    registered_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            registered_spec.name: {
                **registered_spec.as_legacy_schema(),
                "source": "registered",
            }
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog(
            (registered_spec,),
            generation=generation,
        ),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )

    file_spec = ToolSpec(
        name="management_file",
        description="file management",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    file_artifact = _file_artifact(file_spec, generation=generation)
    file_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={file_spec.name: _file_legacy_schema(file_artifact)},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_file_catalog(file_artifact, generation=generation),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )

    generated_spec = ToolSpec(
        name="management_generated",
        description="generated management",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )
    generated_artifact = _generated_artifact(
        generated_spec,
        generation=generation,
    )
    generated_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            generated_spec.name: _generated_legacy_schema(
                generated_artifact
            )
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_generated_catalog(
            generated_artifact,
            generation=generation,
        ),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )

    mcp_spec = ToolSpec(
        name="mcp__management__lookup",
        description="mcp management",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    mcp_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={mcp_spec.name: _mcp_legacy_schema(mcp_spec)},
        tool_dependencies={},
        mcp_tool_names={mcp_spec.name},
        provider_catalog=_mcp_catalog(mcp_spec, generation=generation),
        legacy_plugin_names=set(),
        mcp_server_identifiers={"management"},
    )

    plugin_info, plugin_specs = build_nonebot_plugin_candidate(
        {
            "management_plugin": {
                "description": "plugin management",
                "usage": "/management",
            }
        }
    )
    base = _registered_catalog((), generation=generation)
    plugin_registration = ProviderRegistration.from_provider(
        nonebot_plugin_provider
    )
    plugin_record = DiscoveredTool(
        provider_id=plugin_registration.provider_id,
        source=plugin_registration.source,
        trust=plugin_registration.trust,
        generation=generation,
        spec=plugin_specs[0],
    )
    plugin_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info=plugin_info,
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=ProviderCatalogSnapshot(
            generation=generation,
            registrations=base.registrations,
            tools={**base.tools, plugin_specs[0].name: plugin_record},
        ),
        legacy_plugin_names={plugin_specs[0].name},
        mcp_server_identifiers=set(),
    )
    builtin_snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=base,
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )

    cases = (
        (
            registered_snapshot,
            registered_spec,
            ToolSource.REGISTERED,
            "自定义函数工具",
        ),
        (
            file_snapshot,
            file_spec,
            ToolSource.CUSTOM_FILE,
            "自定义函数工具",
        ),
        (
            generated_snapshot,
            generated_spec,
            ToolSource.GENERATED,
            "自定义函数工具",
        ),
        (mcp_snapshot, mcp_spec, ToolSource.MCP, "MCP 工具"),
        (
            builtin_snapshot,
            builtin_tool_specs()[0],
            ToolSource.BUILTIN,
            "联网搜索工具",
        ),
        (
            plugin_snapshot,
            plugin_specs[0],
            ToolSource.NONEBOT_PLUGIN,
            "NoneBot 插件",
        ),
    )
    for snapshot, spec, source, label in cases:
        rollback = snapshot.resolve_tool_management(
            spec.name,
            is_superuser=True,
            provider_cutover=False,
        )
        provider = snapshot.resolve_tool_management(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
        assert isinstance(rollback, ToolManagementView)
        assert isinstance(provider, ToolManagementView)
        assert not rollback.provider_authoritative
        assert provider.provider_authoritative
        assert provider.kind is ToolManagementTargetKind.EXACT_TOOL
        assert provider.source is rollback.source is source
        assert provider.spec is spec
        assert provider.legacy_entry is rollback.legacy_entry
        assert provider.label == rollback.label == label
        assert provider.matched_tool_names == (spec.name,)
        assert len(provider.trust_decisions) == 1
        decision = provider.trust_decisions[0]
        assert decision.operation is ToolTrustOperation.MANAGEMENT
        assert decision.allowed
        assert decision.audit_required
        assert provider.allowed


def test_management_provider_requires_superuser_even_for_user_tool() -> None:
    spec = ToolSpec(
        name="management_actor",
        description="management actor",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        permission="user",
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=68,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=68),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )

    denied = snapshot.resolve_tool_management(
        spec.name,
        is_superuser=False,
        provider_cutover=True,
    )
    assert denied is not None
    assert not denied.allowed
    assert denied.denial_reason == "工具管理只允许超级用户"

    allowed = snapshot.resolve_tool_management(
        spec.name,
        is_superuser=True,
        provider_cutover=True,
    )
    assert allowed is not None
    assert allowed.allowed

    rollback = snapshot.resolve_tool_management(
        spec.name,
        is_superuser=False,
        provider_cutover=False,
    )
    assert rollback is not None
    assert rollback.allowed
    assert rollback.trust_decisions == ()


def test_management_provider_preserves_mcp_service_and_wildcard_selectors() -> None:
    generation = 69
    specs = (
        ToolSpec(
            name="mcp__files__read",
            description="read",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
        ),
        ToolSpec(
            name="mcp__files__write",
            description="write",
            parameters={"type": "object", "properties": {}},
            handler=_handler,
        ),
    )
    base = _registered_catalog((), generation=generation)
    registration = ProviderRegistration.from_provider(mcp_tool_provider)
    records = {
        spec.name: DiscoveredTool(
            provider_id=registration.provider_id,
            source=registration.source,
            trust=registration.trust,
            generation=generation,
            spec=spec,
        )
        for spec in specs
    }
    catalog = ProviderCatalogSnapshot(
        generation=generation,
        registrations=base.registrations,
        tools={**base.tools, **records},
    )
    snapshot = ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools={
            spec.name: _mcp_legacy_schema(spec) for spec in specs
        },
        tool_dependencies={},
        mcp_tool_names={spec.name for spec in specs},
        provider_catalog=catalog,
        legacy_plugin_names=set(),
        mcp_server_identifiers={"files", "empty"},
    )

    expected_names = tuple(sorted(spec.name for spec in specs))
    for identifier in ("mcp__files", "mcp__files__*"):
        view = snapshot.resolve_tool_management(
            identifier,
            is_superuser=True,
            provider_cutover=True,
        )
        assert view is not None
        assert view.provider_authoritative
        assert view.kind is ToolManagementTargetKind.MCP_SERVICE
        assert view.label == "MCP 服务"
        assert view.matched_tool_names == expected_names
        assert tuple(
            decision.tool_name for decision in view.trust_decisions
        ) == expected_names
        assert view.allowed

    empty = snapshot.resolve_tool_management(
        "mcp__empty",
        is_superuser=True,
        provider_cutover=True,
    )
    assert empty is not None
    assert empty.provider_authoritative
    assert empty.matched_tool_names == ()
    assert empty.trust_decisions == ()
    assert empty.allowed
    assert empty.selector_audit_metadata() == {
        "identifier": "mcp__empty",
        "generation": generation,
        "target_kind": "mcp_service",
        "source": "mcp",
        "provider_authoritative": True,
        "matched_tool_count": 0,
        "allowed": True,
        "reason": "trust policy 允许",
    }

    empty_denied = snapshot.resolve_tool_management(
        "mcp__empty",
        is_superuser=False,
        provider_cutover=True,
    )
    assert empty_denied is not None
    assert not empty_denied.allowed
    assert empty_denied.denial_reason == "工具管理只允许超级用户"

    assert (
        snapshot.resolve_tool_management(
            "mcp__unknown",
            is_superuser=True,
            provider_cutover=True,
        )
        is None
    )
    assert (
        snapshot.resolve_tool_management(
            "mcp__files__missing",
            is_superuser=True,
            provider_cutover=True,
        )
        is None
    )


def test_management_provider_cutover_fails_closed_on_identity_drift(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="management_parity",
        description="management parity",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=70,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=70),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )
    original = ToolSnapshot._legacy_tool_management_view

    def drifted_legacy(self, identifier):
        view = original(self, identifier)
        assert view is not None
        return replace(view, source=ToolSource.MCP)

    monkeypatch.setattr(
        ToolSnapshot,
        "_legacy_tool_management_view",
        drifted_legacy,
    )
    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        snapshot.resolve_tool_management(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
    rollback = snapshot.resolve_tool_management(
        spec.name,
        is_superuser=True,
        provider_cutover=False,
    )
    assert rollback is not None
    assert rollback.source is ToolSource.MCP


def test_management_provider_cutover_checks_mcp_member_parity(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="mcp__parity__lookup",
        description="mcp parity",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    snapshot = ToolSnapshot(
        generation=71,
        plugin_info={},
        custom_tools={spec.name: _mcp_legacy_schema(spec)},
        tool_dependencies={},
        mcp_tool_names={spec.name},
        provider_catalog=_mcp_catalog(spec, generation=71),
        legacy_plugin_names=set(),
        mcp_server_identifiers={"parity"},
    )
    original = ToolSnapshot._legacy_tool_management_view

    def drifted_legacy(self, identifier):
        view = original(self, identifier)
        assert view is not None
        if identifier == spec.name:
            legacy_entry = dict(view.legacy_entry or {})
            legacy_entry["description"] = "drifted"
            return replace(view, legacy_entry=legacy_entry)
        return view

    monkeypatch.setattr(
        ToolSnapshot,
        "_legacy_tool_management_view",
        drifted_legacy,
    )
    with pytest.raises(ProviderConsumerParityError, match="rollback view"):
        snapshot.resolve_tool_management(
            spec.name,
            is_superuser=True,
            provider_cutover=True,
        )
    with pytest.raises(ProviderConsumerParityError, match="selector member"):
        snapshot.resolve_tool_management(
            "mcp__parity__*",
            is_superuser=True,
            provider_cutover=True,
        )


def test_management_provider_cutover_handles_unknown_and_legacy_snapshot() -> None:
    complete = ToolSnapshot(
        generation=72,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((), generation=72),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )
    assert (
        complete.resolve_tool_management(
            "unknown_management",
            is_superuser=True,
            provider_cutover=True,
        )
        is None
    )

    spec = ToolSpec(
        name="management_legacy",
        description="legacy management",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
    legacy = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={spec.name: spec.as_legacy_schema()},
        tool_dependencies={},
        mcp_tool_names=set(),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )
    view = legacy.resolve_tool_management(
        spec.name,
        is_superuser=False,
        provider_cutover=True,
    )
    assert view is not None
    assert not view.provider_authoritative
    assert view.label == "自定义函数工具"
    assert view.allowed


def test_management_provider_cutover_default_and_config_rollback(
    monkeypatch,
) -> None:
    spec = ToolSpec(
        name="management_default_cutover",
        description="management default",
        parameters={"type": "object", "properties": {}},
        handler=_handler,
        policy=ToolPolicy.configured(),
    )
    snapshot = ToolSnapshot(
        generation=73,
        plugin_info={},
        custom_tools={
            spec.name: {**spec.as_legacy_schema(), "source": "registered"}
        },
        tool_dependencies={},
        mcp_tool_names=set(),
        provider_catalog=_registered_catalog((spec,), generation=73),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )

    def get_default(key: str, default=None):
        assert key == "provider_catalog_management_enabled"
        assert default is True
        return DEFAULT_CONFIG[key]

    monkeypatch.setattr(config_parser, "get_config", get_default)
    provider = snapshot.resolve_tool_management(
        spec.name,
        is_superuser=True,
    )
    assert provider is not None
    assert provider.provider_authoritative

    monkeypatch.setattr(config_parser, "get_config", lambda *_args: False)
    rollback = snapshot.resolve_tool_management(
        spec.name,
        is_superuser=True,
    )
    assert rollback is not None
    assert not rollback.provider_authoritative


@pytest.mark.parametrize("flag", [1, "true", [], {}])
def test_management_provider_cutover_rejects_non_boolean_override(flag) -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )
    with pytest.raises(ValueError, match="cutover"):
        snapshot.resolve_tool_management(
            "unknown",
            is_superuser=True,
            provider_cutover=flag,
        )


def test_management_provider_cutover_rejects_non_boolean_actor() -> None:
    snapshot = ToolSnapshot(
        generation=1,
        plugin_info={},
        custom_tools={},
        tool_dependencies={},
        mcp_tool_names=set(),
        legacy_plugin_names=set(),
        mcp_server_identifiers=set(),
    )
    with pytest.raises(TypeError, match="is_superuser"):
        snapshot.resolve_tool_management(
            "unknown",
            is_superuser=1,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("flag", [None, 1, "true", [], {}])
def test_management_provider_cutover_config_requires_boolean(flag) -> None:
    candidate = dict(DEFAULT_CONFIG)
    candidate["provider_catalog_management_enabled"] = flag

    with pytest.raises(
        ValueError,
        match="provider_catalog_management_enabled",
    ):
        ConfigParser._validate(candidate)


def test_permission_filter_fails_closed_for_malformed_entries() -> None:
    assert not tool_manager.is_tool_allowed(
        object(), is_superuser=False  # type: ignore[arg-type]
    )
    assert not tool_manager.is_tool_allowed(
        {"tool_spec": object()}, is_superuser=False
    )

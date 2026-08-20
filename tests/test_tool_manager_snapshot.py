from __future__ import annotations

from dataclasses import replace
import importlib
import json
from types import SimpleNamespace

import pytest

from nonebot_plugin_moellmchats.builtin_tools import builtin_tool_specs
from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    LifecycleState,
    VersionRecord,
    VersionState,
)
from nonebot_plugin_moellmchats.runtime_metrics import runtime_metrics
from nonebot_plugin_moellmchats.runtime_snapshot import runtime_snapshots
from nonebot_plugin_moellmchats.tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolPolicy, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import (
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
    builtin_tool_provider,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
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
        ),
    )


def _file_artifact(spec: ToolSpec, *, generation: int) -> ToolArtifact:
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
        contract=ToolContractSnapshot.from_spec(spec),
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
        ),
    )


def _file_legacy_schema(artifact: ToolArtifact) -> dict:
    spec = artifact.spec
    return {
        **spec.as_legacy_schema(),
        "source": "custom_file",
        "declared_effect": artifact.contract.declared_effect.value,
        "effective_effect": spec.effect.value,
        "tool_artifact": artifact,
        "artifact_digest": artifact.artifact_digest,
        "generation": artifact.generation,
    }


def _generated_artifact(spec: ToolSpec, *, generation: int) -> ToolArtifact:
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
        contract=ToolContractSnapshot.from_spec(spec),
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
        ),
    )


def _generated_legacy_schema(artifact: ToolArtifact) -> dict:
    spec = artifact.spec
    contract = artifact.contract
    return {
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
    assert snapshot.provider_catalog.schema_version == 2
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
        plugin_info={"optional_plugin": {}},
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
        plugin_info={"optional_plugin": {}},
        custom_tools={},
        tool_dependencies={builtin_spec.name: {"optional_plugin"}},
        mcp_tool_names=set(),
        provider_catalog=catalog,
    )

    assert snapshot.provider_catalog.tools_for_provider("builtin") == (record,)
    assert record.spec is builtin_spec
    assert builtin_spec.name not in snapshot.custom_tools

    drifted = replace(
        record,
        spec=replace(builtin_spec, description="drifted builtin"),
    )
    drifted_catalog = ProviderCatalogSnapshot(
        generation=14,
        registrations=catalog.registrations,
        tools={builtin_spec.name: drifted},
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
    )
    artifact = _file_artifact(spec, generation=15)
    catalog = _file_catalog(artifact, generation=15)
    legacy = _file_legacy_schema(artifact)

    snapshot = ToolSnapshot(
        generation=15,
        plugin_info={"optional_plugin": {}},
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
        plugin_info={"optional_plugin": {}},
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


def test_permission_filter_fails_closed_for_malformed_entries() -> None:
    assert not tool_manager.is_tool_allowed(
        object(), is_superuser=False  # type: ignore[arg-type]
    )
    assert not tool_manager.is_tool_allowed(
        {"tool_spec": object()}, is_superuser=False
    )

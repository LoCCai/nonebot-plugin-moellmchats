from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    LifecycleState,
    VersionRecord,
    VersionState,
)
from nonebot_plugin_moellmchats.tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolPolicy, ToolSpec
from nonebot_plugin_moellmchats.tool_providers import (
    BuiltinToolResources,
    DiscoveredTool,
    FileToolProvider,
    FileToolResources,
    GeneratedSourceOverride,
    GeneratedToolProvider,
    GeneratedToolResources,
    MCPToolProvider,
    MCPToolResources,
    NoneBotPluginToolResources,
    ProviderCatalogSnapshot,
    ProviderDiscoveryBatch,
    ProviderDiscoveryContext,
    ProviderDiscoveryPlan,
    ProviderRegistration,
    ProviderRegistry,
    RegisteredToolProvider,
    RegisteredToolResources,
    ToolSource,
    ToolTrustLevel,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
    provider_registry,
    registered_tool_provider,
    trust_for_source,
)


async def _handler(value: str = "ok") -> str:
    return value


class _BuiltinProvider:
    __slots__ = ()

    @property
    def provider_id(self) -> str:
        return "builtin"

    @property
    def source(self) -> ToolSource:
        return ToolSource.BUILTIN

    @property
    def trust(self) -> ToolTrustLevel:
        return ToolTrustLevel.TRUSTED

    async def discover(
        self,
        context: ProviderDiscoveryContext[BuiltinToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if type(context.resources) is not BuiltinToolResources:
            raise TypeError("builtin provider resources mismatch")
        return tuple(
            DiscoveredTool(
                provider_id=self.provider_id,
                source=self.source,
                trust=self.trust,
                generation=context.generation,
                spec=spec,
            )
            for spec in context.resources.specs
        )


def _spec(name: str = "echo") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} one value",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        handler=_handler,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )


def _schema(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def _custom_artifact(spec: ToolSpec, *, generation: int = 4) -> ToolArtifact:
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
        generation=generation,
        filename="tools.py",
    )


def _generated_artifact(spec: ToolSpec, *, generation: int = 4) -> ToolArtifact:
    source = f"async def {spec.name}(value='ok'):\n    return value\n".encode()
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    manifest = {
        "bundle_id": "echo_bundle",
        "description": "echo bundle",
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
        schema=_schema(spec),
        spec=spec,
        contract=ToolContractSnapshot.from_spec(spec),
        source_type="generated",
        generation=generation,
        filename="tool.py",
        tests_source=tests_source,
        bundle_manifest=manifest,
        bundle_id="echo_bundle",
        bundle_digest=digest,
    )


def _active_state(digest: str) -> LifecycleState:
    version = VersionRecord(
        bundle_id="echo_bundle",
        digest=digest,
        state=VersionState.ACTIVATED,
        source_draft_id="draft000001",
        created_at=1,
        approved_at=2,
        activated_at=3,
    )
    return LifecycleState(
        revision=1,
        drafts={},
        versions={"echo_bundle": {digest: version}},
        active={"echo_bundle": digest},
        permission_grants={},
    )


def test_source_trust_identity_is_stable() -> None:
    assert trust_for_source(ToolSource.REGISTERED) is ToolTrustLevel.TRUSTED
    assert trust_for_source(ToolSource.CUSTOM_FILE) is ToolTrustLevel.REVIEWED
    assert trust_for_source(ToolSource.GENERATED) is ToolTrustLevel.UNTRUSTED
    assert trust_for_source(ToolSource.MCP) is ToolTrustLevel.EXTERNAL
    assert trust_for_source(ToolSource.BUILTIN) is ToolTrustLevel.TRUSTED
    assert trust_for_source(ToolSource.NONEBOT_PLUGIN) is ToolTrustLevel.REVIEWED
    with pytest.raises(ValueError, match="ToolSource"):
        trust_for_source("registered")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "resources_type",
    [
        RegisteredToolResources,
        MCPToolResources,
        BuiltinToolResources,
        NoneBotPluginToolResources,
    ],
)
def test_spec_resources_detach_and_freeze_input(resources_type) -> None:
    specs = [_spec()]
    resources = resources_type(specs)  # type: ignore[arg-type]
    specs.clear()

    assert [spec.name for spec in resources.specs] == ["echo"]
    assert deepcopy(resources) is resources
    with pytest.raises(FrozenInstanceError):
        resources.specs = ()

    with pytest.raises(ValueError, match="重名"):
        resources_type((_spec(), _spec()))
    with pytest.raises(TypeError, match="ToolSpec"):
        resources_type((object(),))


def test_discovery_context_rejects_raw_or_mutable_resource_shapes() -> None:
    resources = RegisteredToolResources((_spec(),))
    context = ProviderDiscoveryContext(generation=4, resources=resources)

    assert context.resources is resources
    assert deepcopy(context) is context
    with pytest.raises(FrozenInstanceError):
        context.generation = 5
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError, match="非负整数"):
            ProviderDiscoveryContext(generation=invalid, resources=resources)
    with pytest.raises(TypeError, match="typed resource"):
        ProviderDiscoveryContext(generation=4, resources={"specs": ()})


def test_file_resources_bind_immutable_artifact_generation() -> None:
    spec = _spec()
    artifact = _custom_artifact(spec)
    artifacts = [artifact]
    resources = FileToolResources(artifacts)  # type: ignore[arg-type]
    artifacts.clear()

    assert resources.artifacts == (artifact,)
    ProviderDiscoveryContext(generation=4, resources=resources)
    with pytest.raises(ValueError, match="generation"):
        ProviderDiscoveryContext(generation=5, resources=resources)
    with pytest.raises(ValueError, match="来源类型"):
        FileToolResources((_generated_artifact(spec),))


def test_generated_resources_pin_after_state_and_typed_source_override(
    tmp_path: Path,
) -> None:
    digest = "a" * 64
    state = _active_state(digest)
    overrides = [
        GeneratedSourceOverride(
            bundle_id="echo_bundle",
            bundle_digest=digest,
            source_directory=tmp_path,
        )
    ]
    resources = GeneratedToolResources(
        lifecycle_state=state,
        source_overrides=overrides,  # type: ignore[arg-type]
    )
    overrides.clear()

    assert resources.lifecycle_state is state
    assert resources.source_overrides[0].source_directory == tmp_path
    assert deepcopy(resources) is resources
    with pytest.raises(FrozenInstanceError):
        resources.source_overrides = ()
    with pytest.raises(TypeError, match="GeneratedSourceOverride"):
        GeneratedToolResources(state, (("echo_bundle", digest),))
    with pytest.raises(ValueError, match="after-state active"):
        GeneratedToolResources(
            state,
            (
                GeneratedSourceOverride(
                    bundle_id="echo_bundle",
                    bundle_digest="b" * 64,
                    source_directory=tmp_path,
                ),
            ),
        )


def test_discovered_tool_enforces_artifact_and_generation_boundaries() -> None:
    spec = _spec()
    custom = _custom_artifact(spec)
    discovered = DiscoveredTool(
        provider_id="custom-file",
        source=ToolSource.CUSTOM_FILE,
        trust=ToolTrustLevel.REVIEWED,
        generation=4,
        spec=spec,
        artifact=custom,
    )

    assert deepcopy(discovered) is discovered
    with pytest.raises(FrozenInstanceError):
        discovered.generation = 5
    with pytest.raises(ValueError, match="generation"):
        replace(discovered, generation=5)
    with pytest.raises(ValueError, match="稳定身份"):
        replace(discovered, trust=ToolTrustLevel.TRUSTED)
    with pytest.raises(ValueError, match="安全小写标识"):
        replace(discovered, provider_id="Custom File")
    with pytest.raises(ValueError, match="精确 ToolSpec"):
        replace(discovered, spec=_spec())


def test_artifact_requirement_depends_only_on_source_identity() -> None:
    spec = _spec()
    generated = _generated_artifact(spec)
    DiscoveredTool(
        provider_id="generated",
        source=ToolSource.GENERATED,
        trust=ToolTrustLevel.UNTRUSTED,
        generation=4,
        spec=spec,
        artifact=generated,
    )
    DiscoveredTool(
        provider_id="registered",
        source=ToolSource.REGISTERED,
        trust=ToolTrustLevel.TRUSTED,
        generation=4,
        spec=spec,
    )

    with pytest.raises(ValueError, match="必须携带"):
        DiscoveredTool(
            provider_id="generated",
            source=ToolSource.GENERATED,
            trust=ToolTrustLevel.UNTRUSTED,
            generation=4,
            spec=spec,
        )
    with pytest.raises(ValueError, match="不得伪造"):
        DiscoveredTool(
            provider_id="registered",
            source=ToolSource.REGISTERED,
            trust=ToolTrustLevel.TRUSTED,
            generation=4,
            spec=spec,
            artifact=_custom_artifact(spec),
        )
    with pytest.raises(ValueError, match="source_type"):
        DiscoveredTool(
            provider_id="generated",
            source=ToolSource.GENERATED,
            trust=ToolTrustLevel.UNTRUSTED,
            generation=4,
            spec=spec,
            artifact=_custom_artifact(spec),
        )


@pytest.mark.asyncio
async def test_registered_provider_is_deterministic_immutable_and_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nonebot_plugin_moellmchats.tool_contracts import tool_registry

    specs = (_spec("zeta"), _spec("alpha"))
    context = ProviderDiscoveryContext(
        generation=19,
        resources=RegisteredToolResources(specs),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("registered discovery must not perform I/O/global reads")

    monkeypatch.setattr(tool_registry, "snapshot", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)

    first = await registered_tool_provider.discover(context)
    second = await registered_tool_provider.discover(context)

    assert first == second
    assert [item.spec.name for item in first] == ["alpha", "zeta"]
    assert all(item.provider_id == "registered" for item in first)
    assert all(item.source is ToolSource.REGISTERED for item in first)
    assert all(item.trust is ToolTrustLevel.TRUSTED for item in first)
    assert all(item.generation == 19 and item.artifact is None for item in first)
    assert deepcopy(first) is first
    with pytest.raises(FrozenInstanceError):
        first[0].generation = 20
    assert not hasattr(registered_tool_provider, "execute")
    assert not hasattr(registered_tool_provider, "reload")
    with pytest.raises(FrozenInstanceError):
        registered_tool_provider.source = ToolSource.MCP


@pytest.mark.asyncio
async def test_registered_provider_rejects_other_resource_records() -> None:
    provider = RegisteredToolProvider()
    context = ProviderDiscoveryContext(
        generation=1,
        resources=MCPToolResources((_spec(),)),
    )

    with pytest.raises(TypeError, match="RegisteredToolResources"):
        await provider.discover(context)  # type: ignore[arg-type]


def _registered_legacy_projection(
    specs: tuple[ToolSpec, ...],
) -> tuple[dict[str, dict], dict[str, set[str]]]:
    tools = {spec.name: {**spec.as_legacy_schema(), "source": "registered"} for spec in specs}
    dependencies = {spec.name: set(spec.dependencies) for spec in specs if spec.dependencies}
    return tools, dependencies


def _file_legacy_projection(
    artifacts: tuple[ToolArtifact, ...],
) -> tuple[dict[str, dict], dict[str, set[str]]]:
    tools: dict[str, dict] = {}
    dependencies: dict[str, set[str]] = {}
    for artifact in artifacts:
        spec = artifact.spec
        tools[spec.name] = {
            **spec.as_legacy_schema(),
            "source": "custom_file",
            "declared_effect": artifact.contract.declared_effect.value,
            "effective_effect": spec.effect.value,
            "tool_artifact": artifact,
            "artifact_digest": artifact.artifact_digest,
            "generation": artifact.generation,
        }
        if spec.dependencies:
            dependencies[spec.name] = set(spec.dependencies)
    return tools, dependencies


def _file_plan(
    generation: int,
    artifacts: tuple[ToolArtifact, ...] = (),
) -> ProviderDiscoveryPlan[FileToolResources]:
    return ProviderDiscoveryPlan(
        provider=file_tool_provider,
        context=ProviderDiscoveryContext(
            generation=generation,
            resources=FileToolResources(artifacts),
        ),
    )


def _generated_plan(
    generation: int,
    artifacts: tuple[ToolArtifact, ...] = (),
    *,
    state: LifecycleState | None = None,
) -> ProviderDiscoveryPlan[GeneratedToolResources]:
    if state is None:
        state = (
            _active_state(artifacts[0].bundle_digest)
            if artifacts
            else LifecycleState.empty()
        )
    return ProviderDiscoveryPlan(
        provider=generated_tool_provider,
        context=ProviderDiscoveryContext(
            generation=generation,
            resources=GeneratedToolResources(
                lifecycle_state=state,
                artifacts=artifacts,
            ),
        ),
    )


def _mcp_plan(
    generation: int,
    specs: tuple[ToolSpec, ...] = (),
) -> ProviderDiscoveryPlan[MCPToolResources]:
    return ProviderDiscoveryPlan(
        provider=mcp_tool_provider,
        context=ProviderDiscoveryContext(
            generation=generation,
            resources=MCPToolResources(specs),
        ),
    )


def _generated_legacy_projection(
    artifacts: tuple[ToolArtifact, ...],
) -> tuple[dict[str, dict], dict[str, set[str]]]:
    tools: dict[str, dict] = {}
    dependencies: dict[str, set[str]] = {}
    for artifact in artifacts:
        spec = artifact.spec
        contract = artifact.contract
        tools[spec.name] = {
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
        if spec.dependencies:
            dependencies[spec.name] = set(spec.dependencies)
    return tools, dependencies


def _mcp_legacy_projection(
    specs: tuple[ToolSpec, ...],
) -> tuple[dict[str, dict], dict[str, set[str]]]:
    tools: dict[str, dict] = {}
    dependencies: dict[str, set[str]] = {}
    for spec in specs:
        tools[spec.name] = {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
            "func": spec.handler,
            "source": "mcp",
        }
        if spec.dependencies:
            dependencies[spec.name] = set(spec.dependencies)
    return tools, dependencies


@pytest.mark.asyncio
async def test_registered_provider_accepts_complete_legacy_parity() -> None:
    helper = _spec("helper")
    echo = replace(_spec("echo"), dependencies=("helper",))
    records = await registered_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=7,
            resources=RegisteredToolResources((echo, helper)),
        )
    )
    tools, dependencies = _registered_legacy_projection((echo, helper))

    registered_tool_provider.validate_legacy_parity(
        records,
        tools,
        dependencies,
        generation=7,
    )


@pytest.mark.asyncio
async def test_registered_provider_fails_closed_for_every_legacy_mismatch() -> None:
    helper = _spec("helper")
    echo = replace(_spec("echo"), dependencies=("helper",))
    specs = (echo, helper)
    records = await registered_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=7,
            resources=RegisteredToolResources(specs),
        )
    )

    def reject(
        tools: dict[str, dict],
        dependencies: dict[str, set[str]],
        *,
        discovery: tuple[DiscoveredTool, ...] = records,
    ) -> None:
        with pytest.raises((TypeError, ValueError), match="registered"):
            registered_tool_provider.validate_legacy_parity(
                discovery,
                tools,
                dependencies,
                generation=7,
            )

    tools, dependencies = _registered_legacy_projection(specs)
    tools.pop("echo")
    reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    extra = _spec("extra")
    tools["extra"] = {**extra.as_legacy_schema(), "source": "registered"}
    reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    tools["echo"]["tool_spec"] = replace(echo)
    reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    tools["echo"]["func"] = lambda: None
    reject(tools, dependencies)

    for field_name, value in (
        ("name", "renamed"),
        ("description", "mutated description"),
        ("parameters", {"type": "object", "properties": {}}),
        ("source", "custom_file"),
    ):
        tools, dependencies = _registered_legacy_projection(specs)
        tools["echo"][field_name] = value
        reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    tools["echo"]["unexpected"] = True
    reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    dependencies["echo"] = set()
    reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    dependencies["echo"].add("extra")
    reject(tools, dependencies)

    tools, dependencies = _registered_legacy_projection(specs)
    reject(tools, dependencies, discovery=(records[0], replace(records[1], generation=8)))

    tools, dependencies = _registered_legacy_projection(specs)
    reject(
        tools,
        dependencies,
        discovery=(replace(records[0], provider_id="other"), records[1]),
    )


def test_file_resources_bind_exact_legacy_artifacts() -> None:
    artifact = _custom_artifact(_spec())
    tools, _dependencies = _file_legacy_projection((artifact,))

    resources = FileToolResources.from_legacy_tools(tools)
    tools.clear()

    assert resources.artifacts == (artifact,)
    with pytest.raises(ValueError, match="source"):
        FileToolResources.from_legacy_tools(
            {"echo": {"source": "generated", "tool_artifact": artifact}}
        )
    with pytest.raises(ValueError, match="ToolArtifact"):
        FileToolResources.from_legacy_tools(
            {"echo": {"source": "custom_file"}}
        )
    with pytest.raises(ValueError, match="identity"):
        FileToolResources.from_legacy_tools(
            {"renamed": {"source": "custom_file", "tool_artifact": artifact}}
        )


@pytest.mark.asyncio
async def test_file_provider_is_deterministic_immutable_and_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = (
        _custom_artifact(_spec("zeta"), generation=19),
        _custom_artifact(_spec("alpha"), generation=19),
    )
    context = ProviderDiscoveryContext(
        generation=19,
        resources=FileToolResources(artifacts),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("file discovery must not read source or rerun AST policy")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.custom_tool_loader.analyze_ast_policy",
        forbidden,
    )

    first = await file_tool_provider.discover(context)
    second = await file_tool_provider.discover(context)

    assert first == second
    assert [item.spec.name for item in first] == ["alpha", "zeta"]
    assert all(item.provider_id == "custom-file" for item in first)
    assert all(item.source is ToolSource.CUSTOM_FILE for item in first)
    assert all(item.trust is ToolTrustLevel.REVIEWED for item in first)
    assert all(item.generation == 19 for item in first)
    assert [item.artifact for item in first] == [artifacts[1], artifacts[0]]
    assert deepcopy(first) is first
    assert not hasattr(file_tool_provider, "execute")
    assert not hasattr(file_tool_provider, "reload")
    with pytest.raises(FrozenInstanceError):
        file_tool_provider.source = ToolSource.GENERATED


@pytest.mark.asyncio
async def test_file_provider_rejects_other_resource_records() -> None:
    provider = FileToolProvider()
    context = ProviderDiscoveryContext(
        generation=1,
        resources=RegisteredToolResources((_spec(),)),
    )

    with pytest.raises(TypeError, match="FileToolResources"):
        await provider.discover(context)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_file_provider_accepts_complete_legacy_parity() -> None:
    helper = _custom_artifact(_spec("helper"), generation=7)
    echo_spec = replace(_spec("echo"), dependencies=("helper",))
    echo = _custom_artifact(echo_spec, generation=7)
    artifacts = (echo, helper)
    records = await file_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=7,
            resources=FileToolResources(artifacts),
        )
    )
    tools, dependencies = _file_legacy_projection(artifacts)
    dependencies["echo"].add("legacy_plugin_dependency")

    file_tool_provider.validate_legacy_parity(
        records,
        tools,
        dependencies,
        generation=7,
        allow_additional_dependencies=True,
    )


@pytest.mark.asyncio
async def test_file_provider_fails_closed_for_every_legacy_mismatch() -> None:
    helper = _custom_artifact(_spec("helper"), generation=7)
    echo_spec = replace(_spec("echo"), dependencies=("helper",))
    echo = _custom_artifact(echo_spec, generation=7)
    artifacts = (echo, helper)
    records = await file_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=7,
            resources=FileToolResources(artifacts),
        )
    )

    def reject(
        tools: dict[str, dict],
        dependencies: dict[str, set[str]],
        *,
        discovery: tuple[DiscoveredTool, ...] = records,
    ) -> None:
        with pytest.raises((TypeError, ValueError), match="custom-file"):
            file_tool_provider.validate_legacy_parity(
                discovery,
                tools,
                dependencies,
                generation=7,
            )

    tools, dependencies = _file_legacy_projection(artifacts)
    tools.pop("echo")
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    extra = _custom_artifact(_spec("extra"), generation=7)
    tools.update(_file_legacy_projection((extra,))[0])
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    tools["echo"]["tool_artifact"] = _custom_artifact(
        echo_spec,
        generation=7,
    )
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    tools["echo"]["tool_spec"] = replace(echo_spec)
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    tools["echo"]["func"] = lambda: None
    reject(tools, dependencies)

    for field_name, value in (
        ("name", "renamed"),
        ("description", "mutated description"),
        ("parameters", {"type": "object", "properties": {}}),
        ("source", "generated"),
        ("declared_effect", "external_side_effect"),
        ("effective_effect", "external_side_effect"),
        ("artifact_digest", "0" * 64),
        ("generation", 8),
    ):
        tools, dependencies = _file_legacy_projection(artifacts)
        tools["echo"][field_name] = value
        reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    tools["echo"]["unexpected"] = True
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    dependencies["echo"] = set()
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    dependencies["echo"].add("extra")
    reject(tools, dependencies)

    tools, dependencies = _file_legacy_projection(artifacts)
    reject(
        tools,
        dependencies,
        discovery=(replace(records[0], provider_id="other"), records[1]),
    )


def test_generated_resources_bind_after_state_overrides_and_legacy_artifacts(
    tmp_path: Path,
) -> None:
    artifact = _generated_artifact(_spec(), generation=8)
    assert artifact.bundle_digest is not None
    state = _active_state(artifact.bundle_digest)
    tools, _dependencies = _generated_legacy_projection((artifact,))

    resources = GeneratedToolResources.from_legacy_tools(
        lifecycle_state=state,
        source_overrides={
            ("echo_bundle", artifact.bundle_digest): tmp_path,
        },
        legacy_tools=tools,
    )
    tools.clear()

    assert resources.lifecycle_state is state
    assert resources.artifacts == (artifact,)
    assert resources.source_overrides == (
        GeneratedSourceOverride(
            bundle_id="echo_bundle",
            bundle_digest=artifact.bundle_digest,
            source_directory=tmp_path,
        ),
    )
    ProviderDiscoveryContext(generation=8, resources=resources)
    with pytest.raises(ValueError, match="generation"):
        ProviderDiscoveryContext(generation=9, resources=resources)
    with pytest.raises(ValueError, match="bundle 集合"):
        GeneratedToolResources.from_legacy_tools(
            lifecycle_state=state,
            source_overrides=None,
            legacy_tools={},
        )
    with pytest.raises(ValueError, match="source"):
        GeneratedToolResources.from_legacy_tools(
            lifecycle_state=state,
            source_overrides=None,
            legacy_tools={
                "echo": {
                    "source": "custom_file",
                    "tool_artifact": artifact,
                }
            },
        )


@pytest.mark.asyncio
async def test_generated_provider_is_deterministic_immutable_and_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _generated_artifact(_spec(), generation=19)
    assert artifact.bundle_digest is not None
    state = _active_state(artifact.bundle_digest)
    context = ProviderDiscoveryContext(
        generation=19,
        resources=GeneratedToolResources(
            lifecycle_state=state,
            artifacts=(artifact,),
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError(
            "generated discovery must not read canonical state or source"
        )

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.generated_tools.generated_tool_store.read_lifecycle_state",
        forbidden,
    )
    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.generated_tools.generated_tool_store.validate_bundle",
        forbidden,
    )

    first = await generated_tool_provider.discover(context)
    second = await generated_tool_provider.discover(context)

    assert first == second
    assert first[0].provider_id == "generated"
    assert first[0].source is ToolSource.GENERATED
    assert first[0].trust is ToolTrustLevel.UNTRUSTED
    assert first[0].generation == 19
    assert first[0].artifact is artifact
    assert first[0].spec is artifact.spec
    assert deepcopy(first) is first
    assert not hasattr(generated_tool_provider, "execute")
    assert not hasattr(generated_tool_provider, "reload")
    with pytest.raises(FrozenInstanceError):
        generated_tool_provider.source = ToolSource.CUSTOM_FILE


@pytest.mark.asyncio
async def test_generated_provider_rejects_other_resource_records() -> None:
    provider = GeneratedToolProvider()
    context = ProviderDiscoveryContext(
        generation=1,
        resources=RegisteredToolResources((_spec(),)),
    )

    with pytest.raises(TypeError, match="GeneratedToolResources"):
        await provider.discover(context)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_generated_provider_accepts_complete_legacy_parity() -> None:
    spec = replace(_spec("echo"), dependencies=("helper",))
    artifact = _generated_artifact(spec, generation=7)
    assert artifact.bundle_digest is not None
    state = _active_state(artifact.bundle_digest)
    records = await generated_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=7,
            resources=GeneratedToolResources(
                lifecycle_state=state,
                artifacts=(artifact,),
            ),
        )
    )
    tools, dependencies = _generated_legacy_projection((artifact,))
    dependencies["echo"].add("legacy_plugin_dependency")

    generated_tool_provider.validate_legacy_parity(
        records,
        tools,
        dependencies,
        generation=7,
        allow_additional_dependencies=True,
    )


@pytest.mark.asyncio
async def test_generated_provider_fails_closed_for_legacy_drift() -> None:
    artifact = _generated_artifact(_spec("echo"), generation=7)
    assert artifact.bundle_digest is not None
    state = _active_state(artifact.bundle_digest)
    records = await generated_tool_provider.discover(
        ProviderDiscoveryContext(
            generation=7,
            resources=GeneratedToolResources(
                lifecycle_state=state,
                artifacts=(artifact,),
            ),
        )
    )

    def reject(
        tools: dict[str, dict],
        dependencies: dict[str, set[str]],
        *,
        discovery: tuple[DiscoveredTool, ...] = records,
    ) -> None:
        with pytest.raises((TypeError, ValueError), match="generated"):
            generated_tool_provider.validate_legacy_parity(
                discovery,
                tools,
                dependencies,
                generation=7,
            )

    tools, dependencies = _generated_legacy_projection((artifact,))
    tools["echo"]["tool_artifact"] = _generated_artifact(
        artifact.spec,
        generation=7,
    )
    reject(tools, dependencies)

    for field_name, value in (
        ("bundle_id", "other_bundle"),
        ("bundle_digest", "0" * 64),
        ("requested_permission", "superuser"),
        ("effective_permission", "superuser"),
        ("declared_effect", "mutating"),
        ("effective_effect", "mutating"),
        ("user_policy_approved", False),
        ("requested_capabilities", {}),
        ("effective_capabilities", {}),
        ("artifact_digest", "0" * 64),
        ("generation", 8),
    ):
        tools, dependencies = _generated_legacy_projection((artifact,))
        tools["echo"][field_name] = value
        reject(tools, dependencies)

    tools, dependencies = _generated_legacy_projection((artifact,))
    tools["echo"].pop("bundle_id")
    reject(tools, dependencies)

    tools, dependencies = _generated_legacy_projection((artifact,))
    reject(
        tools,
        dependencies,
        discovery=(replace(records[0], provider_id="other"),),
    )


def test_mcp_resources_build_typed_specs_without_changing_legacy_view() -> None:
    legacy_spec = _spec("mcp__demo__echo")
    tools, _dependencies = _mcp_legacy_projection((legacy_spec,))
    schema = tools[legacy_spec.name]

    resources = MCPToolResources.from_legacy_tools(tools)

    assert tuple(schema) == (
        "name",
        "description",
        "parameters",
        "func",
        "source",
    )
    assert "tool_spec" not in schema
    assert resources.specs[0].name == legacy_spec.name
    assert resources.specs[0].handler is legacy_spec.handler
    assert deepcopy(resources) is resources
    tools.clear()
    assert resources.specs[0].description == legacy_spec.description

    invalid, _ = _mcp_legacy_projection((legacy_spec,))
    invalid[legacy_spec.name]["source"] = "registered"
    with pytest.raises(ValueError, match="source"):
        MCPToolResources.from_legacy_tools(invalid)


@pytest.mark.asyncio
async def test_mcp_provider_is_deterministic_immutable_and_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = (_spec("mcp__demo__zeta"), _spec("mcp__demo__alpha"))
    context = ProviderDiscoveryContext(
        generation=29,
        resources=MCPToolResources(specs),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("mcp provider shadow discovery must not perform I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(
        "nonebot_plugin_moellmchats.mcp_manager.mcp_manager.discover_tools",
        forbidden,
    )

    first = await mcp_tool_provider.discover(context)
    second = await mcp_tool_provider.discover(context)

    assert first == second
    assert [item.spec.name for item in first] == [
        "mcp__demo__alpha",
        "mcp__demo__zeta",
    ]
    assert all(item.provider_id == "mcp" for item in first)
    assert all(item.source is ToolSource.MCP for item in first)
    assert all(item.trust is ToolTrustLevel.EXTERNAL for item in first)
    assert all(item.generation == 29 and item.artifact is None for item in first)
    assert deepcopy(first) is first
    assert not hasattr(mcp_tool_provider, "execute")
    assert not hasattr(mcp_tool_provider, "reload")
    with pytest.raises(FrozenInstanceError):
        mcp_tool_provider.source = ToolSource.REGISTERED


@pytest.mark.asyncio
async def test_mcp_provider_rejects_other_resource_records() -> None:
    provider = MCPToolProvider()
    context = ProviderDiscoveryContext(
        generation=1,
        resources=RegisteredToolResources((_spec(),)),
    )

    with pytest.raises(TypeError, match="MCPToolResources"):
        await provider.discover(context)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mcp_provider_accepts_complete_shadow_parity() -> None:
    legacy_spec = _spec("mcp__demo__echo")
    tools, dependencies = _mcp_legacy_projection((legacy_spec,))
    resources = MCPToolResources.from_legacy_tools(tools)
    records = await mcp_tool_provider.discover(
        ProviderDiscoveryContext(generation=31, resources=resources)
    )
    dependencies[legacy_spec.name] = {"legacy_optional"}

    mcp_tool_provider.validate_legacy_parity(
        records,
        tools,
        dependencies,
        {legacy_spec.name},
        generation=31,
        allow_additional_dependencies=True,
    )


@pytest.mark.asyncio
async def test_mcp_provider_fails_closed_for_legacy_or_sidecar_drift() -> None:
    legacy_spec = _spec("mcp__demo__echo")
    tools, dependencies = _mcp_legacy_projection((legacy_spec,))
    resources = MCPToolResources.from_legacy_tools(tools)
    records = await mcp_tool_provider.discover(
        ProviderDiscoveryContext(generation=37, resources=resources)
    )

    def reject(
        candidate_tools: dict[str, dict],
        names: set[str],
        *,
        discovery: tuple[DiscoveredTool, ...] = records,
    ) -> None:
        with pytest.raises((TypeError, ValueError), match="mcp"):
            mcp_tool_provider.validate_legacy_parity(
                discovery,
                candidate_tools,
                dependencies,
                names,
                generation=37,
            )

    mutated, _ = _mcp_legacy_projection((legacy_spec,))
    mutated[legacy_spec.name]["description"] = "drifted"
    reject(mutated, {legacy_spec.name})

    mutated, _ = _mcp_legacy_projection((legacy_spec,))
    mutated[legacy_spec.name]["tool_spec"] = legacy_spec
    reject(mutated, {legacy_spec.name})

    reject(tools, set())
    reject(
        tools,
        {legacy_spec.name},
        discovery=(replace(records[0], provider_id="other"),),
    )


@pytest.mark.asyncio
async def test_provider_registry_builds_deterministic_immutable_v2_catalog() -> None:
    specs = (_spec("zeta"), _spec("alpha"))
    plan = ProviderDiscoveryPlan(
        provider=registered_tool_provider,
        context=ProviderDiscoveryContext(
            generation=23,
            resources=RegisteredToolResources(specs),
        ),
    )

    catalog = await provider_registry.discover(
        23,
        (plan, _file_plan(23), _generated_plan(23), _mcp_plan(23)),
    )

    assert ProviderCatalogSnapshot.schema_version == 2
    assert catalog.generation == 23
    assert tuple(catalog.registrations) == (
        "custom-file",
        "generated",
        "mcp",
        "registered",
    )
    assert tuple(catalog.tools) == ("alpha", "zeta")
    assert [
        item.spec.name for item in catalog.tools_for_provider("registered")
    ] == ["alpha", "zeta"]
    assert catalog.tools_for_provider("missing") == ()
    assert deepcopy(catalog) is catalog
    assert deepcopy(provider_registry) is provider_registry
    with pytest.raises(TypeError):
        catalog.tools["other"] = catalog.tools["alpha"]
    with pytest.raises(TypeError):
        catalog.registrations["other"] = catalog.registrations["registered"]
    with pytest.raises(FrozenInstanceError):
        catalog.generation = 24


@pytest.mark.asyncio
async def test_provider_registry_fails_closed_for_operation_set_and_conflicts() -> None:
    registered_plan = ProviderDiscoveryPlan(
        provider=registered_tool_provider,
        context=ProviderDiscoveryContext(
            generation=9,
            resources=RegisteredToolResources((_spec("same_name"),)),
        ),
    )
    builtin_provider = _BuiltinProvider()
    builtin_plan = ProviderDiscoveryPlan(
        provider=builtin_provider,
        context=ProviderDiscoveryContext(
            generation=9,
            resources=BuiltinToolResources((_spec("same_name"),)),
        ),
    )
    registry = ProviderRegistry(
        (
            ProviderRegistration.from_provider(registered_tool_provider),
            ProviderRegistration.from_provider(builtin_provider),
        )
    )

    with pytest.raises(ValueError, match="工具名冲突"):
        await registry.discover(9, (registered_plan, builtin_plan))
    file_artifact = _custom_artifact(_spec("same_name"), generation=9)
    with pytest.raises(ValueError, match="工具名冲突"):
        await provider_registry.discover(
            9,
            (
                registered_plan,
                _file_plan(9, (file_artifact,)),
                _generated_plan(9),
                _mcp_plan(9),
            ),
        )
    generated_artifact = _generated_artifact(
        _spec("same_name"),
        generation=9,
    )
    with pytest.raises(ValueError, match="工具名冲突"):
        await provider_registry.discover(
            9,
            (
                registered_plan,
                _file_plan(9),
                _generated_plan(9, (generated_artifact,)),
                _mcp_plan(9),
            ),
        )
    with pytest.raises(ValueError, match="工具名冲突"):
        await provider_registry.discover(
            9,
            (
                registered_plan,
                _file_plan(9),
                _generated_plan(9),
                _mcp_plan(9, (_spec("same_name"),)),
            ),
        )
    with pytest.raises(ValueError, match="不完整"):
        await provider_registry.discover(9, ())
    with pytest.raises(ValueError, match="重复执行"):
        await provider_registry.discover(9, (registered_plan, registered_plan))
    with pytest.raises(ValueError, match="generation"):
        await provider_registry.discover(
            10,
            (
                registered_plan,
                _file_plan(9),
                _generated_plan(9),
                _mcp_plan(9),
            ),
        )
    with pytest.raises(ValueError, match="未注册"):
        await provider_registry.discover(9, (builtin_plan,))
    with pytest.raises(TypeError, match="typed plan"):
        await provider_registry.discover(9, (object(),))  # type: ignore[arg-type]

    duplicate = ProviderRegistration.from_provider(registered_tool_provider)
    with pytest.raises(ValueError, match="重复 provider_id"):
        ProviderRegistry((duplicate, duplicate))


@pytest.mark.asyncio
async def test_provider_batch_and_catalog_reject_identity_or_generation_drift() -> None:
    plan = ProviderDiscoveryPlan(
        provider=registered_tool_provider,
        context=ProviderDiscoveryContext(
            generation=5,
            resources=RegisteredToolResources((_spec(),)),
        ),
    )
    batch = await plan.discover_batch()
    file_batch = await _file_plan(5).discover_batch()
    generated_batch = await _generated_plan(5).discover_batch()
    mcp_batch = await _mcp_plan(5).discover_batch()
    catalog = provider_registry.build_snapshot(
        5,
        (batch, file_batch, generated_batch, mcp_batch),
    )

    assert isinstance(batch, ProviderDiscoveryBatch)
    assert catalog.tools["echo"].spec is batch.tools[0].spec
    with pytest.raises(ValueError, match="generation"):
        replace(batch, generation=6)
    with pytest.raises(ValueError, match="generation"):
        provider_registry.build_snapshot(6, (batch,))
    with pytest.raises(ValueError, match="name"):
        ProviderCatalogSnapshot(
            generation=5,
            registrations=catalog.registrations,
            tools={"renamed": batch.tools[0]},
        )
    with pytest.raises(ValueError, match="未注册"):
        ProviderCatalogSnapshot(
            generation=5,
            registrations={},
            tools={"echo": batch.tools[0]},
        )

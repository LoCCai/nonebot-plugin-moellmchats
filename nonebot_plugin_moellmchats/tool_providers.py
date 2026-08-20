from __future__ import annotations

import asyncio
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from types import MappingProxyType
from typing import (
    ClassVar,
    Generic,
    Protocol,
    TypeAlias,
    TypeVar,
    runtime_checkable,
)

from .generated_tool_lifecycle import LifecycleState
from .tool_artifacts import ToolArtifact
from .tool_contracts import ToolSpec

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ToolTrustLevel(str, Enum):
    TRUSTED = "trusted"
    REVIEWED = "reviewed"
    UNTRUSTED = "untrusted"
    EXTERNAL = "external"


class ToolSource(str, Enum):
    REGISTERED = "registered"
    CUSTOM_FILE = "custom_file"
    GENERATED = "generated"
    MCP = "mcp"
    BUILTIN = "builtin"
    NONEBOT_PLUGIN = "nonebot_plugin"


def trust_for_source(source: ToolSource) -> ToolTrustLevel:
    """Return the stable code-origin identity assigned to one source."""

    if not isinstance(source, ToolSource):
        raise ValueError("工具 source 必须是 ToolSource")
    if source in {ToolSource.REGISTERED, ToolSource.BUILTIN}:
        return ToolTrustLevel.TRUSTED
    if source in {ToolSource.CUSTOM_FILE, ToolSource.NONEBOT_PLUGIN}:
        return ToolTrustLevel.REVIEWED
    if source is ToolSource.GENERATED:
        return ToolTrustLevel.UNTRUSTED
    return ToolTrustLevel.EXTERNAL


def _require_generation(generation: object) -> int:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("provider generation 必须是非负整数")
    return generation


def _legacy_value_equal(left: object, right: object) -> bool:
    """Compare a legacy JSON value before or after runtime deep-freezing."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _legacy_value_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _legacy_value_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _freeze_specs(value: object, *, label: str) -> tuple[ToolSpec, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label}.specs 必须是 ToolSpec 序列")
    specs = tuple(value)
    if not all(isinstance(spec, ToolSpec) for spec in specs):
        raise TypeError(f"{label}.specs 必须只包含 ToolSpec")
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError(f"{label}.specs 不得包含重名工具")
    return specs


def _freeze_artifacts(
    value: object,
    *,
    source_type: str,
    label: str,
) -> tuple[ToolArtifact, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{label}.artifacts 必须是 ToolArtifact 序列")
    artifacts = tuple(value)
    if not all(isinstance(artifact, ToolArtifact) for artifact in artifacts):
        raise TypeError(f"{label}.artifacts 必须只包含 ToolArtifact")
    if any(artifact.source_type != source_type for artifact in artifacts):
        raise ValueError(f"{label}.artifacts 与来源类型不一致")
    names = [artifact.tool_name for artifact in artifacts]
    if len(names) != len(set(names)):
        raise ValueError(f"{label}.artifacts 不得包含重名工具")
    return artifacts


@dataclass(frozen=True)
class RegisteredToolResources:
    specs: tuple[ToolSpec, ...]
    source: ClassVar[ToolSource] = ToolSource.REGISTERED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specs",
            _freeze_specs(self.specs, label="RegisteredToolResources"),
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> RegisteredToolResources:
        return self


@dataclass(frozen=True)
class FileToolResources:
    artifacts: tuple[ToolArtifact, ...]
    source: ClassVar[ToolSource] = ToolSource.CUSTOM_FILE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifacts",
            _freeze_artifacts(
                self.artifacts,
                source_type="custom_file",
                label="FileToolResources",
            ),
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> FileToolResources:
        return self


@dataclass(frozen=True)
class GeneratedSourceOverride:
    bundle_id: str
    bundle_digest: str
    source_directory: Path

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_id, str) or not _BUNDLE_ID_RE.fullmatch(self.bundle_id):
            raise ValueError("Generated source override bundle_id 非法")
        if not isinstance(self.bundle_digest, str) or not _SHA256_RE.fullmatch(self.bundle_digest):
            raise ValueError("Generated source override bundle_digest 非法")
        try:
            directory = Path(self.source_directory)
        except TypeError as error:
            raise TypeError("Generated source override source_directory 必须是路径") from error
        object.__setattr__(self, "source_directory", directory)

    def __deepcopy__(self, _memo: dict[int, object]) -> GeneratedSourceOverride:
        return self


@dataclass(frozen=True)
class GeneratedToolResources:
    lifecycle_state: LifecycleState
    source_overrides: tuple[GeneratedSourceOverride, ...] = ()
    source: ClassVar[ToolSource] = ToolSource.GENERATED

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle_state, LifecycleState):
            raise TypeError("GeneratedToolResources.lifecycle_state 必须是 LifecycleState")
        if not isinstance(self.source_overrides, (list, tuple)):
            raise TypeError("GeneratedToolResources.source_overrides 必须是 typed override 序列")
        overrides = tuple(self.source_overrides)
        if not all(isinstance(item, GeneratedSourceOverride) for item in overrides):
            raise TypeError("GeneratedToolResources.source_overrides 必须只包含 GeneratedSourceOverride")
        keys = [(item.bundle_id, item.bundle_digest) for item in overrides]
        if len(keys) != len(set(keys)):
            raise ValueError("GeneratedToolResources.source_overrides 不得重复")
        for override in overrides:
            if self.lifecycle_state.active.get(override.bundle_id) != override.bundle_digest:
                raise ValueError("Generated source override 必须精确指向 after-state active 版本")
        object.__setattr__(self, "source_overrides", overrides)

    def __deepcopy__(self, _memo: dict[int, object]) -> GeneratedToolResources:
        return self


@dataclass(frozen=True)
class MCPToolResources:
    specs: tuple[ToolSpec, ...]
    source: ClassVar[ToolSource] = ToolSource.MCP

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specs",
            _freeze_specs(self.specs, label="MCPToolResources"),
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> MCPToolResources:
        return self


@dataclass(frozen=True)
class BuiltinToolResources:
    specs: tuple[ToolSpec, ...]
    source: ClassVar[ToolSource] = ToolSource.BUILTIN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specs",
            _freeze_specs(self.specs, label="BuiltinToolResources"),
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> BuiltinToolResources:
        return self


@dataclass(frozen=True)
class NoneBotPluginToolResources:
    specs: tuple[ToolSpec, ...]
    source: ClassVar[ToolSource] = ToolSource.NONEBOT_PLUGIN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "specs",
            _freeze_specs(self.specs, label="NoneBotPluginToolResources"),
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> NoneBotPluginToolResources:
        return self


ProviderResourceRecord: TypeAlias = (
    RegisteredToolResources
    | FileToolResources
    | GeneratedToolResources
    | MCPToolResources
    | BuiltinToolResources
    | NoneBotPluginToolResources
)
_RESOURCE_RECORD_TYPES = (
    RegisteredToolResources,
    FileToolResources,
    GeneratedToolResources,
    MCPToolResources,
    BuiltinToolResources,
    NoneBotPluginToolResources,
)
ProviderResourcesT = TypeVar(
    "ProviderResourcesT",
    bound=ProviderResourceRecord,
)


@dataclass(frozen=True)
class ProviderDiscoveryContext(Generic[ProviderResourcesT]):
    generation: int
    resources: ProviderResourcesT

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        if type(self.resources) not in _RESOURCE_RECORD_TYPES:
            raise TypeError("provider resources 必须是来源专属的 frozen typed resource record")
        if isinstance(self.resources, FileToolResources):
            mismatched = [artifact.tool_name for artifact in self.resources.artifacts if artifact.generation != self.generation]
            if mismatched:
                raise ValueError(f"File ToolArtifact generation 与 discovery context 不一致: {sorted(mismatched)}")

    def __deepcopy__(
        self,
        _memo: dict[int, object],
    ) -> ProviderDiscoveryContext[ProviderResourcesT]:
        return self


@dataclass(frozen=True)
class DiscoveredTool:
    provider_id: str
    source: ToolSource
    trust: ToolTrustLevel
    generation: int
    spec: ToolSpec
    artifact: ToolArtifact | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID_RE.fullmatch(self.provider_id):
            raise ValueError("provider_id 必须是稳定的安全小写标识")
        if not isinstance(self.source, ToolSource):
            raise ValueError("工具 source 必须是 ToolSource")
        if not isinstance(self.trust, ToolTrustLevel):
            raise ValueError("工具 trust 必须是 ToolTrustLevel")
        if self.trust is not trust_for_source(self.source):
            raise ValueError("工具 trust 与 source 的稳定身份不一致")
        _require_generation(self.generation)
        if not isinstance(self.spec, ToolSpec):
            raise TypeError("DiscoveredTool.spec 必须是 ToolSpec")

        needs_artifact = self.source in {
            ToolSource.CUSTOM_FILE,
            ToolSource.GENERATED,
        }
        if needs_artifact and not isinstance(self.artifact, ToolArtifact):
            raise ValueError("Custom File / Generated discovery 必须携带 ToolArtifact")
        if not needs_artifact and self.artifact is not None:
            raise ValueError("该工具来源不得伪造 ToolArtifact")
        if self.artifact is None:
            return
        if self.artifact.source_type != self.source.value:
            raise ValueError("ToolArtifact source_type 与 discovery source 不一致")
        if self.artifact.generation != self.generation:
            raise ValueError("ToolArtifact generation 与 discovery generation 不一致")
        if self.artifact.spec is not self.spec:
            raise ValueError("DiscoveredTool.spec 必须是 ToolArtifact 固定的精确 ToolSpec")

    def __deepcopy__(self, _memo: dict[int, object]) -> DiscoveredTool:
        return self


class ToolProvider(Protocol[ProviderResourcesT]):
    @property
    def provider_id(self) -> str: ...

    @property
    def source(self) -> ToolSource: ...

    @property
    def trust(self) -> ToolTrustLevel: ...

    async def discover(
        self,
        context: ProviderDiscoveryContext[ProviderResourcesT],
    ) -> tuple[DiscoveredTool, ...]: ...


@runtime_checkable
class ProviderIdentity(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def source(self) -> ToolSource: ...

    @property
    def trust(self) -> ToolTrustLevel: ...


@dataclass(frozen=True)
class ProviderRegistration:
    provider_id: str
    source: ToolSource
    trust: ToolTrustLevel

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID_RE.fullmatch(
            self.provider_id
        ):
            raise ValueError("provider registration id 非法")
        if not isinstance(self.source, ToolSource):
            raise TypeError("provider registration source 必须是 ToolSource")
        if not isinstance(self.trust, ToolTrustLevel):
            raise TypeError("provider registration trust 必须是 ToolTrustLevel")
        if self.trust is not trust_for_source(self.source):
            raise ValueError("provider registration trust 与 source 不一致")

    @classmethod
    def from_provider(cls, provider: ProviderIdentity) -> ProviderRegistration:
        if not isinstance(provider, ProviderIdentity):
            raise TypeError("provider 必须公开稳定 identity")
        return cls(
            provider_id=provider.provider_id,
            source=provider.source,
            trust=provider.trust,
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> ProviderRegistration:
        return self


@dataclass(frozen=True)
class ProviderDiscoveryBatch:
    registration: ProviderRegistration
    generation: int
    tools: tuple[DiscoveredTool, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.registration, ProviderRegistration):
            raise TypeError("provider discovery batch 缺少 registration")
        _require_generation(self.generation)
        if not isinstance(self.tools, tuple) or not all(
            isinstance(item, DiscoveredTool) for item in self.tools
        ):
            raise TypeError("provider discovery batch tools 必须是不可变发现记录")
        names: set[str] = set()
        for item in self.tools:
            if (
                item.provider_id != self.registration.provider_id
                or item.source is not self.registration.source
                or item.trust is not self.registration.trust
            ):
                raise ValueError("provider discovery batch identity 不一致")
            if item.generation != self.generation:
                raise ValueError("provider discovery batch generation 不一致")
            if item.spec.name in names:
                raise ValueError("provider discovery batch 不得包含重名工具")
            names.add(item.spec.name)

    def __deepcopy__(self, _memo: dict[int, object]) -> ProviderDiscoveryBatch:
        return self


@runtime_checkable
class ProviderDiscoveryOperation(Protocol):
    @property
    def registration(self) -> ProviderRegistration: ...

    @property
    def generation(self) -> int: ...

    async def discover_batch(self) -> ProviderDiscoveryBatch: ...


@dataclass(frozen=True)
class ProviderDiscoveryPlan(Generic[ProviderResourcesT]):
    provider: ToolProvider[ProviderResourcesT]
    context: ProviderDiscoveryContext[ProviderResourcesT]
    registration: ProviderRegistration = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context, ProviderDiscoveryContext):
            raise TypeError("provider discovery plan context 非法")
        object.__setattr__(
            self,
            "registration",
            ProviderRegistration.from_provider(self.provider),
        )

    @property
    def generation(self) -> int:
        return self.context.generation

    async def discover_batch(self) -> ProviderDiscoveryBatch:
        tools = await self.provider.discover(self.context)
        return ProviderDiscoveryBatch(
            registration=self.registration,
            generation=self.generation,
            tools=tools,
        )


@dataclass(frozen=True)
class ProviderCatalogSnapshot:
    generation: int
    registrations: Mapping[str, ProviderRegistration]
    tools: Mapping[str, DiscoveredTool]
    schema_version: ClassVar[int] = 2
    _tools_by_provider: Mapping[str, tuple[str, ...]] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        if not isinstance(self.registrations, Mapping):
            raise TypeError("provider catalog registrations 必须是映射")
        registrations = dict(self.registrations)
        if any(
            not isinstance(provider_id, str)
            or not isinstance(registration, ProviderRegistration)
            or provider_id != registration.provider_id
            for provider_id, registration in registrations.items()
        ):
            raise ValueError("provider catalog registration identity 不一致")
        if not isinstance(self.tools, Mapping):
            raise TypeError("provider catalog tools 必须是映射")
        tools = dict(self.tools)
        tools_by_provider: dict[str, list[str]] = {
            provider_id: [] for provider_id in registrations
        }
        for name, item in tools.items():
            if not isinstance(name, str) or not isinstance(item, DiscoveredTool):
                raise TypeError("provider catalog tools 必须按名称映射发现记录")
            if name != item.spec.name:
                raise ValueError("provider catalog tool name 不一致")
            registration = registrations.get(item.provider_id)
            if registration is None:
                raise ValueError("provider catalog 包含未注册 provider")
            if (
                item.source is not registration.source
                or item.trust is not registration.trust
            ):
                raise ValueError("provider catalog tool identity 不一致")
            if item.generation != self.generation:
                raise ValueError("provider catalog generation 不一致")
            tools_by_provider[item.provider_id].append(name)

        ordered_registrations = dict(sorted(registrations.items()))
        ordered_tools = dict(sorted(tools.items()))
        object.__setattr__(
            self,
            "registrations",
            MappingProxyType(ordered_registrations),
        )
        object.__setattr__(self, "tools", MappingProxyType(ordered_tools))
        object.__setattr__(
            self,
            "_tools_by_provider",
            MappingProxyType(
                {
                    provider_id: tuple(sorted(names))
                    for provider_id, names in sorted(tools_by_provider.items())
                }
            ),
        )

    @classmethod
    def empty(cls, generation: int) -> ProviderCatalogSnapshot:
        return cls(generation=generation, registrations={}, tools={})

    def tools_for_provider(
        self,
        provider_id: str,
    ) -> tuple[DiscoveredTool, ...]:
        names = self._tools_by_provider.get(provider_id, ())
        return tuple(self.tools[name] for name in names)

    def __deepcopy__(self, _memo: dict[int, object]) -> ProviderCatalogSnapshot:
        return self


@dataclass(frozen=True)
class ProviderRegistry:
    registrations: tuple[ProviderRegistration, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.registrations, tuple) or not all(
            isinstance(item, ProviderRegistration) for item in self.registrations
        ):
            raise TypeError("provider registry 必须由 registration 元组构成")
        provider_ids = [item.provider_id for item in self.registrations]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider registry 不得包含重复 provider_id")
        object.__setattr__(
            self,
            "registrations",
            tuple(sorted(self.registrations, key=lambda item: item.provider_id)),
        )

    async def discover(
        self,
        generation: int,
        operations: tuple[ProviderDiscoveryOperation, ...],
    ) -> ProviderCatalogSnapshot:
        _require_generation(generation)
        if not isinstance(operations, tuple) or not all(
            isinstance(item, ProviderDiscoveryOperation) for item in operations
        ):
            raise TypeError("provider registry operations 必须是 typed plan 元组")
        expected = {item.provider_id: item for item in self.registrations}
        actual: dict[str, ProviderDiscoveryOperation] = {}
        for operation in operations:
            provider_id = operation.registration.provider_id
            if provider_id in actual:
                raise ValueError("provider registry 不得重复执行 provider")
            registration = expected.get(provider_id)
            if registration is None or operation.registration != registration:
                raise ValueError("provider registry operation 未注册或 identity 不一致")
            if operation.generation != generation:
                raise ValueError("provider registry operation generation 不一致")
            actual[provider_id] = operation
        if set(actual) != set(expected):
            raise ValueError(
                "provider registry operation 集合不完整: "
                f"missing={sorted(set(expected) - set(actual))}, "
                f"extra={sorted(set(actual) - set(expected))}"
            )

        batches = tuple(
            await asyncio.gather(
                *(actual[provider_id].discover_batch() for provider_id in sorted(actual))
            )
        )
        return self.build_snapshot(generation, batches)

    def build_snapshot(
        self,
        generation: int,
        batches: tuple[ProviderDiscoveryBatch, ...],
    ) -> ProviderCatalogSnapshot:
        _require_generation(generation)
        if not isinstance(batches, tuple) or not all(
            isinstance(item, ProviderDiscoveryBatch) for item in batches
        ):
            raise TypeError("provider registry batches 必须是 typed batch 元组")
        expected = {item.provider_id: item for item in self.registrations}
        actual: dict[str, ProviderDiscoveryBatch] = {}
        tools: dict[str, DiscoveredTool] = {}
        owners: dict[str, str] = {}
        for batch in batches:
            provider_id = batch.registration.provider_id
            if provider_id in actual:
                raise ValueError("provider registry 不得包含重复 batch")
            registration = expected.get(provider_id)
            if registration is None or batch.registration != registration:
                raise ValueError("provider registry batch 未注册或 identity 不一致")
            if batch.generation != generation:
                raise ValueError("provider registry batch generation 不一致")
            actual[provider_id] = batch
            for item in batch.tools:
                name = item.spec.name
                if name in tools:
                    raise ValueError(
                        f"provider 工具名冲突: {name} "
                        f"({owners[name]} vs {provider_id})"
                    )
                tools[name] = item
                owners[name] = provider_id
        if set(actual) != set(expected):
            raise ValueError(
                "provider registry batch 集合不完整: "
                f"missing={sorted(set(expected) - set(actual))}, "
                f"extra={sorted(set(actual) - set(expected))}"
            )
        return ProviderCatalogSnapshot(
            generation=generation,
            registrations=expected,
            tools=tools,
        )

    def __deepcopy__(self, _memo: dict[int, object]) -> ProviderRegistry:
        return self


@dataclass(frozen=True)
class RegisteredToolProvider:
    """Discover the transaction-pinned, trusted in-process tool registry.

    Discovery deliberately consumes only ``RegisteredToolResources``.  The
    provider does not read the global registry and does not own an execution
    path; its first use is a shadow comparison with the existing legacy view.
    """

    provider_id: str = field(default="registered", init=False)
    source: ToolSource = field(default=ToolSource.REGISTERED, init=False)
    trust: ToolTrustLevel = field(default=ToolTrustLevel.TRUSTED, init=False)

    async def discover(
        self,
        context: ProviderDiscoveryContext[RegisteredToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if not isinstance(context, ProviderDiscoveryContext) or type(context.resources) is not RegisteredToolResources:
            raise TypeError("RegisteredToolProvider 只接受 RegisteredToolResources")
        return tuple(
            DiscoveredTool(
                provider_id=self.provider_id,
                source=self.source,
                trust=self.trust,
                generation=context.generation,
                spec=spec,
            )
            for spec in sorted(context.resources.specs, key=lambda item: item.name)
        )

    def validate_legacy_parity(
        self,
        discovered: tuple[DiscoveredTool, ...],
        legacy_tools: Mapping[str, object],
        legacy_dependencies: Mapping[str, object],
        *,
        generation: int,
        allow_additional_dependencies: bool = False,
    ) -> None:
        """Fail closed unless the registered legacy projection is exact."""

        _require_generation(generation)
        if type(allow_additional_dependencies) is not bool:
            raise TypeError("allow_additional_dependencies 必须是 bool")
        if not isinstance(discovered, tuple) or not all(isinstance(item, DiscoveredTool) for item in discovered):
            raise TypeError("registered discovery 必须是 DiscoveredTool 元组")
        if not isinstance(legacy_tools, Mapping) or not isinstance(legacy_dependencies, Mapping):
            raise TypeError("registered legacy parity 输入必须是映射")

        expected: dict[str, ToolSpec] = {}
        for item in discovered:
            if (
                item.provider_id != self.provider_id
                or item.source is not self.source
                or item.trust is not self.trust
                or item.artifact is not None
            ):
                raise ValueError("registered discovery 来源身份不一致")
            if item.spec.name in expected:
                raise ValueError("registered discovery 不得包含重名工具")
            if item.generation != generation:
                raise ValueError("registered discovery generation 不一致")
            expected[item.spec.name] = item.spec

        actual_names = {
            name
            for name, schema in legacy_tools.items()
            if isinstance(name, str) and isinstance(schema, Mapping) and schema.get("source") == self.source.value
        }
        expected_names = set(expected)
        if actual_names != expected_names:
            raise ValueError(
                "registered legacy 工具集合不一致: "
                f"missing={sorted(expected_names - actual_names)}, "
                f"extra={sorted(actual_names - expected_names)}"
            )

        for name, spec in expected.items():
            schema = legacy_tools.get(name)
            if not isinstance(schema, Mapping):
                raise ValueError(f"registered legacy 工具 {name} Schema 缺失")
            expected_schema = {
                **spec.as_legacy_schema(),
                "source": self.source.value,
            }
            if set(schema) != set(expected_schema):
                raise ValueError(f"registered legacy 工具 {name} 字段不一致")
            if schema.get("tool_spec") is not spec:
                raise ValueError(f"registered legacy 工具 {name} ToolSpec 不一致")
            if schema.get("func") is not spec.handler:
                raise ValueError(f"registered legacy 工具 {name} handler 不一致")
            for field_name in ("name", "description", "parameters", "source"):
                if not _legacy_value_equal(
                    schema.get(field_name),
                    expected_schema[field_name],
                ):
                    raise ValueError(f"registered legacy 工具 {name} {field_name} 不一致")

            dependencies = legacy_dependencies.get(name, set())
            if not isinstance(dependencies, AbstractSet) or not all(isinstance(item, str) for item in dependencies):
                raise ValueError(f"registered legacy 工具 {name} dependencies 非法")
            expected_dependencies = set(spec.dependencies)
            dependencies_match = (
                expected_dependencies <= dependencies
                if allow_additional_dependencies
                else expected_dependencies == dependencies
            )
            if not dependencies_match:
                raise ValueError(f"registered legacy 工具 {name} dependencies 不一致")


registered_tool_provider = RegisteredToolProvider()
_registered_tool_provider_contract: ToolProvider[RegisteredToolResources] = registered_tool_provider
provider_registry = ProviderRegistry(
    (ProviderRegistration.from_provider(registered_tool_provider),)
)

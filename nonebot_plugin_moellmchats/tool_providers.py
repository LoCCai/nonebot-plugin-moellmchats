from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import ClassVar, Generic, Protocol, TypeAlias, TypeVar

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
    provider_id: str
    source: ToolSource
    trust: ToolTrustLevel

    async def discover(
        self,
        context: ProviderDiscoveryContext[ProviderResourcesT],
    ) -> tuple[DiscoveredTool, ...]: ...

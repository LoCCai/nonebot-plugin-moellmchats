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
from .tool_contracts import ToolEffect, ToolPolicy, ToolSpec
from .tool_discovery import build_compatibility_description

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


class ToolTrustOperation(str, Enum):
    SELECTION = "selection"
    EXECUTION = "execution"
    MANAGEMENT = "management"


class ToolExecutionBoundary(str, Enum):
    IN_PROCESS = "in_process"
    ISOLATED_ARTIFACT = "isolated_artifact"
    GENERATED_SANDBOX = "generated_sandbox"
    EXTERNAL_PROXY = "external_proxy"
    BOUNDED_EVENT = "bounded_event"


class ToolResultProvenance(str, Enum):
    """Data provenance never inherits the executable adapter's trust."""

    UNVERIFIED = "unverified"
    UNTRUSTED = "untrusted"
    EXTERNAL = "external"


class ToolTrustPolicyError(ValueError):
    """A catalog trust policy is missing, malformed, or cannot be applied."""


class ToolCapabilityPolicyError(ValueError):
    """A catalog capability policy is missing or not generation-bound."""


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

    @classmethod
    def from_legacy_tools(
        cls,
        legacy_tools: Mapping[str, object],
    ) -> FileToolResources:
        """Bind the exact artifacts produced by one legacy file-load pass."""

        if not isinstance(legacy_tools, Mapping):
            raise TypeError("FileToolResources legacy tools 必须是映射")
        if not all(isinstance(name, str) for name in legacy_tools):
            raise TypeError("FileToolResources legacy tool name 必须是字符串")
        artifacts: list[ToolArtifact] = []
        for name in sorted(legacy_tools):
            schema = legacy_tools[name]
            if not isinstance(schema, Mapping):
                raise TypeError(f"FileToolResources legacy 工具 {name} Schema 非法")
            if schema.get("source") != cls.source.value:
                raise ValueError(f"FileToolResources legacy 工具 {name} source 不一致")
            artifact = schema.get("tool_artifact")
            if not isinstance(artifact, ToolArtifact):
                raise ValueError(f"FileToolResources legacy 工具 {name} 缺少 ToolArtifact")
            if artifact.tool_name != name:
                raise ValueError(f"FileToolResources legacy 工具 {name} artifact identity 不一致")
            artifacts.append(artifact)
        return cls(tuple(artifacts))


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
    artifacts: tuple[ToolArtifact, ...] = ()
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
        artifacts = _freeze_artifacts(
            self.artifacts,
            source_type="generated",
            label="GeneratedToolResources",
        )
        for artifact in artifacts:
            if (
                not isinstance(artifact.bundle_id, str)
                or not isinstance(artifact.bundle_digest, str)
                or self.lifecycle_state.active.get(artifact.bundle_id)
                != artifact.bundle_digest
            ):
                raise ValueError(
                    "Generated ToolArtifact 必须精确指向 after-state active 版本"
                )
        object.__setattr__(self, "artifacts", artifacts)

    def __deepcopy__(self, _memo: dict[int, object]) -> GeneratedToolResources:
        return self

    @classmethod
    def from_legacy_tools(
        cls,
        *,
        lifecycle_state: LifecycleState,
        source_overrides: Mapping[tuple[str, str], Path] | None,
        legacy_tools: Mapping[str, object],
    ) -> GeneratedToolResources:
        """Bind one after-state and the exact artifacts built from it."""

        if source_overrides is None:
            typed_overrides: tuple[GeneratedSourceOverride, ...] = ()
        else:
            if not isinstance(source_overrides, Mapping):
                raise TypeError("Generated source overrides 必须是映射或 None")
            overrides: list[GeneratedSourceOverride] = []
            for key, directory in source_overrides.items():
                if (
                    not isinstance(key, tuple)
                    or len(key) != 2
                    or not all(isinstance(item, str) for item in key)
                ):
                    raise TypeError(
                        "Generated source override key 必须是 (bundle_id, digest)"
                    )
                overrides.append(
                    GeneratedSourceOverride(
                        bundle_id=key[0],
                        bundle_digest=key[1],
                        source_directory=directory,
                    )
                )
            typed_overrides = tuple(
                sorted(
                    overrides,
                    key=lambda item: (item.bundle_id, item.bundle_digest),
                )
            )

        if not isinstance(legacy_tools, Mapping):
            raise TypeError("GeneratedToolResources legacy tools 必须是映射")
        if not all(isinstance(name, str) for name in legacy_tools):
            raise TypeError("GeneratedToolResources legacy tool name 必须是字符串")
        artifacts: list[ToolArtifact] = []
        for name in sorted(legacy_tools):
            schema = legacy_tools[name]
            if not isinstance(schema, Mapping):
                raise TypeError(
                    f"GeneratedToolResources legacy 工具 {name} Schema 非法"
                )
            if schema.get("source") != cls.source.value:
                raise ValueError(
                    f"GeneratedToolResources legacy 工具 {name} source 不一致"
                )
            artifact = schema.get("tool_artifact")
            if not isinstance(artifact, ToolArtifact):
                raise ValueError(
                    f"GeneratedToolResources legacy 工具 {name} 缺少 ToolArtifact"
                )
            if artifact.tool_name != name:
                raise ValueError(
                    f"GeneratedToolResources legacy 工具 {name} artifact identity 不一致"
                )
            artifacts.append(artifact)
        resources = cls(
            lifecycle_state=lifecycle_state,
            source_overrides=typed_overrides,
            artifacts=tuple(artifacts),
        )
        artifact_bundles = {
            artifact.bundle_id for artifact in resources.artifacts
        }
        if artifact_bundles != set(lifecycle_state.active):
            raise ValueError(
                "GeneratedToolResources artifact bundle 集合与 after-state active 不一致"
            )
        return resources


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

    @classmethod
    def from_legacy_tools(
        cls,
        legacy_tools: Mapping[str, object],
    ) -> MCPToolResources:
        """Build typed specs from one already-discovered MCP candidate."""

        if not isinstance(legacy_tools, Mapping):
            raise TypeError("MCPToolResources legacy tools 必须是映射")
        if not all(isinstance(name, str) for name in legacy_tools):
            raise TypeError("MCPToolResources legacy tool name 必须是字符串")
        specs: list[ToolSpec] = []
        for name in sorted(legacy_tools):
            schema = legacy_tools[name]
            if not isinstance(schema, Mapping):
                raise TypeError("MCPToolResources legacy 工具结构非法")
            if schema.get("source") != cls.source.value:
                raise ValueError(
                    f"MCPToolResources legacy 工具 {name} source 不一致"
                )
            if schema.get("name") != name:
                raise ValueError(
                    f"MCPToolResources legacy 工具 {name} identity 不一致"
                )
            specs.append(
                ToolSpec(
                    name=name,
                    description=schema.get("description"),  # type: ignore[arg-type]
                    parameters=schema.get("parameters"),  # type: ignore[arg-type]
                    handler=schema.get("func"),  # type: ignore[arg-type]
                )
            )
        return cls(tuple(specs))


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
        if isinstance(self.resources, GeneratedToolResources):
            mismatched = [
                artifact.tool_name
                for artifact in self.resources.artifacts
                if artifact.generation != self.generation
            ]
            if mismatched:
                raise ValueError(
                    "Generated ToolArtifact generation 与 discovery context 不一致: "
                    f"{sorted(mismatched)}"
                )

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


_TRUST_BOUNDARY_BY_SOURCE = {
    ToolSource.REGISTERED: ToolExecutionBoundary.IN_PROCESS,
    ToolSource.CUSTOM_FILE: ToolExecutionBoundary.ISOLATED_ARTIFACT,
    ToolSource.GENERATED: ToolExecutionBoundary.GENERATED_SANDBOX,
    ToolSource.MCP: ToolExecutionBoundary.EXTERNAL_PROXY,
    ToolSource.BUILTIN: ToolExecutionBoundary.IN_PROCESS,
    ToolSource.NONEBOT_PLUGIN: ToolExecutionBoundary.BOUNDED_EVENT,
}
_PROVIDER_ID_BY_SOURCE = {
    ToolSource.REGISTERED: "registered",
    ToolSource.CUSTOM_FILE: "custom-file",
    ToolSource.GENERATED: "generated",
    ToolSource.MCP: "mcp",
    ToolSource.BUILTIN: "builtin",
    ToolSource.NONEBOT_PLUGIN: "nonebot-plugin",
}


def _expected_result_provenance(
    source: ToolSource,
    tool_name: str,
) -> ToolResultProvenance:
    if source is ToolSource.MCP or (
        source is ToolSource.BUILTIN and tool_name == "web_search"
    ):
        return ToolResultProvenance.EXTERNAL
    if source is ToolSource.GENERATED:
        return ToolResultProvenance.UNTRUSTED
    return ToolResultProvenance.UNVERIFIED


@dataclass(frozen=True)
class ToolTrustDecision:
    tool_name: str
    provider_id: str
    source: ToolSource
    trust: ToolTrustLevel
    generation: int
    operation: ToolTrustOperation
    boundary: ToolExecutionBoundary
    result_provenance: ToolResultProvenance
    permission: str
    effect: ToolEffect
    allowed: bool
    reason: str
    confirmation_required: bool
    legacy_bounded_compatibility: bool
    audit_required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ToolTrustPolicyError("trust decision 工具名不能为空")
        if not isinstance(self.provider_id, str) or not _PROVIDER_ID_RE.fullmatch(
            self.provider_id
        ):
            raise ToolTrustPolicyError("trust decision provider_id 非法")
        if not isinstance(self.source, ToolSource) or not isinstance(
            self.trust,
            ToolTrustLevel,
        ):
            raise ToolTrustPolicyError("trust decision 来源身份非法")
        if self.trust is not trust_for_source(self.source):
            raise ToolTrustPolicyError("trust decision trust 与 source 不一致")
        if self.provider_id != _PROVIDER_ID_BY_SOURCE[self.source]:
            raise ToolTrustPolicyError(
                "trust decision provider_id 与 source 不一致"
            )
        _require_generation(self.generation)
        if not isinstance(self.operation, ToolTrustOperation):
            raise ToolTrustPolicyError("trust decision operation 非法")
        if not isinstance(self.boundary, ToolExecutionBoundary):
            raise ToolTrustPolicyError("trust decision execution boundary 非法")
        if self.boundary is not _TRUST_BOUNDARY_BY_SOURCE[self.source]:
            raise ToolTrustPolicyError(
                "trust decision execution boundary 不一致"
            )
        if not isinstance(self.result_provenance, ToolResultProvenance):
            raise ToolTrustPolicyError("trust decision result provenance 非法")
        if self.result_provenance is not _expected_result_provenance(
            self.source,
            self.tool_name,
        ):
            raise ToolTrustPolicyError(
                "trust decision result provenance 不一致"
            )
        if self.permission not in {"user", "superuser"}:
            raise ToolTrustPolicyError("trust decision permission 非法")
        if not isinstance(self.effect, ToolEffect):
            raise ToolTrustPolicyError("trust decision effect 非法")
        for field_name in (
            "allowed",
            "confirmation_required",
            "legacy_bounded_compatibility",
            "audit_required",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ToolTrustPolicyError(
                    f"trust decision {field_name} 必须是 bool"
                )
        if not isinstance(self.reason, str) or not self.reason:
            raise ToolTrustPolicyError("trust decision reason 不能为空")
        expected_compatibility = self.source is ToolSource.NONEBOT_PLUGIN
        if self.legacy_bounded_compatibility is not expected_compatibility:
            raise ToolTrustPolicyError(
                "trust decision legacy compatibility 不一致"
            )
        if expected_compatibility and self.effect is not ToolEffect.MUTATING:
            raise ToolTrustPolicyError(
                "NoneBot compatibility decision 必须保守标记为 mutating"
            )

    def audit_metadata(self) -> dict[str, object]:
        """Return argument-free structured fields safe for an audit event."""

        return {
            "tool_name": self.tool_name,
            "provider_id": self.provider_id,
            "source": self.source.value,
            "trust": self.trust.value,
            "generation": self.generation,
            "operation": self.operation.value,
            "execution_boundary": self.boundary.value,
            "result_provenance": self.result_provenance.value,
            "permission": self.permission,
            "effect": self.effect.value,
            "allowed": self.allowed,
            "reason": self.reason,
            "confirmation_required": self.confirmation_required,
            "legacy_bounded_compatibility": (
                self.legacy_bounded_compatibility
            ),
            "audit_required": self.audit_required,
        }

    def __deepcopy__(self, _memo: dict[int, object]) -> ToolTrustDecision:
        return self


class ToolTrustDenied(PermissionError):
    def __init__(self, decision: ToolTrustDecision) -> None:
        if not isinstance(decision, ToolTrustDecision) or decision.allowed:
            raise TypeError("ToolTrustDenied 必须绑定被拒绝的 trust decision")
        self.decision = decision
        super().__init__(
            f"工具 {decision.tool_name} trust policy 拒绝"
            f" {decision.operation.value}: {decision.reason}"
        )


@dataclass(frozen=True)
class ToolTrustPolicy:
    """Generation-bound policy derived only from one discovered tool."""

    tool_name: str
    provider_id: str
    source: ToolSource
    trust: ToolTrustLevel
    generation: int
    spec: ToolSpec
    boundary: ToolExecutionBoundary
    result_provenance: ToolResultProvenance
    confirmation_required: bool
    legacy_bounded_compatibility: bool

    def __post_init__(self) -> None:
        if not isinstance(self.spec, ToolSpec) or self.tool_name != self.spec.name:
            raise ToolTrustPolicyError("trust policy 必须绑定精确 ToolSpec")
        if not isinstance(self.source, ToolSource) or not isinstance(
            self.trust,
            ToolTrustLevel,
        ):
            raise ToolTrustPolicyError("trust policy 来源身份非法")
        if self.trust is not trust_for_source(self.source):
            raise ToolTrustPolicyError("trust policy trust 与 source 不一致")
        if self.provider_id != _PROVIDER_ID_BY_SOURCE[self.source]:
            raise ToolTrustPolicyError("trust policy provider_id 与 source 不一致")
        _require_generation(self.generation)
        if self.boundary is not _TRUST_BOUNDARY_BY_SOURCE[self.source]:
            raise ToolTrustPolicyError("trust policy execution boundary 不一致")
        if not isinstance(self.result_provenance, ToolResultProvenance):
            raise ToolTrustPolicyError("trust policy result provenance 非法")
        if self.result_provenance is not _expected_result_provenance(
            self.source,
            self.tool_name,
        ):
            raise ToolTrustPolicyError("trust policy result provenance 不一致")
        for field_name in (
            "confirmation_required",
            "legacy_bounded_compatibility",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ToolTrustPolicyError(
                    f"trust policy {field_name} 必须是 bool"
                )
        expected_compatibility = self.source is ToolSource.NONEBOT_PLUGIN
        if self.legacy_bounded_compatibility is not expected_compatibility:
            raise ToolTrustPolicyError("trust policy legacy compatibility 不一致")
        if expected_compatibility and self.spec.effect is not ToolEffect.MUTATING:
            raise ToolTrustPolicyError(
                "NoneBot compatibility tool 必须保守标记为 mutating"
            )
        expected_confirmation = (
            self.spec.effect is ToolEffect.MUTATING
            and not expected_compatibility
        )
        if self.confirmation_required is not expected_confirmation:
            raise ToolTrustPolicyError("trust policy confirmation policy 不一致")

    @classmethod
    def from_discovered(cls, item: DiscoveredTool) -> ToolTrustPolicy:
        if not isinstance(item, DiscoveredTool):
            raise TypeError("trust policy 只能从 DiscoveredTool 构建")
        compatibility = item.source is ToolSource.NONEBOT_PLUGIN
        return cls(
            tool_name=item.spec.name,
            provider_id=item.provider_id,
            source=item.source,
            trust=item.trust,
            generation=item.generation,
            spec=item.spec,
            boundary=_TRUST_BOUNDARY_BY_SOURCE[item.source],
            result_provenance=_expected_result_provenance(
                item.source,
                item.spec.name,
            ),
            confirmation_required=(
                item.spec.effect is ToolEffect.MUTATING and not compatibility
            ),
            legacy_bounded_compatibility=compatibility,
        )

    def decide(
        self,
        operation: ToolTrustOperation,
        *,
        is_superuser: bool,
        confirmed: bool = False,
    ) -> ToolTrustDecision:
        if not isinstance(operation, ToolTrustOperation):
            raise TypeError("trust operation 必须是 ToolTrustOperation")
        if type(is_superuser) is not bool or type(confirmed) is not bool:
            raise TypeError("trust actor/confirmation 标志必须是 bool")

        allowed = True
        reason = "trust policy 允许"
        if operation is ToolTrustOperation.MANAGEMENT and not is_superuser:
            allowed = False
            reason = "工具管理只允许超级用户"
        elif (
            operation
            in {ToolTrustOperation.SELECTION, ToolTrustOperation.EXECUTION}
            and self.spec.permission == "superuser"
            and not is_superuser
        ):
            allowed = False
            reason = "工具契约只允许超级用户"
        elif (
            operation is ToolTrustOperation.EXECUTION
            and self.confirmation_required
            and not confirmed
        ):
            allowed = False
            reason = "mutating 工具尚未完成二阶段确认"

        audit_required = (
            not allowed
            or operation is not ToolTrustOperation.SELECTION
            or self.trust is not ToolTrustLevel.TRUSTED
            or self.result_provenance is not ToolResultProvenance.UNVERIFIED
        )
        return ToolTrustDecision(
            tool_name=self.tool_name,
            provider_id=self.provider_id,
            source=self.source,
            trust=self.trust,
            generation=self.generation,
            operation=operation,
            boundary=self.boundary,
            result_provenance=self.result_provenance,
            permission=self.spec.permission,
            effect=self.spec.effect,
            allowed=allowed,
            reason=reason,
            confirmation_required=self.confirmation_required,
            legacy_bounded_compatibility=(
                self.legacy_bounded_compatibility
            ),
            audit_required=audit_required,
        )

    def require(
        self,
        operation: ToolTrustOperation,
        *,
        is_superuser: bool,
        confirmed: bool = False,
    ) -> ToolTrustDecision:
        decision = self.decide(
            operation,
            is_superuser=is_superuser,
            confirmed=confirmed,
        )
        if not decision.allowed:
            raise ToolTrustDenied(decision)
        return decision

    def __deepcopy__(self, _memo: dict[int, object]) -> ToolTrustPolicy:
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
    schema_version: ClassVar[int] = 3
    _tools_by_provider: Mapping[str, tuple[str, ...]] = field(
        init=False,
        repr=False,
    )
    _trust_policies: Mapping[str, ToolTrustPolicy] = field(
        init=False,
        repr=False,
    )
    _capability_policies: Mapping[str, ToolPolicy] = field(
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
        trust_policies: dict[str, ToolTrustPolicy] = {}
        capability_policies: dict[str, ToolPolicy] = {}
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
            policy = ToolTrustPolicy.from_discovered(item)
            if policy.spec is not item.spec:
                raise ToolTrustPolicyError(
                    "provider catalog trust policy ToolSpec identity 不一致"
                )
            trust_policies[name] = policy
            if item.spec.policy is not None:
                capability_policies[name] = item.spec.policy
            if item.artifact is not None:
                if item.spec.policy is None:
                    raise ToolCapabilityPolicyError(
                        "artifact provider 工具缺少 capability policy"
                    )
                if item.artifact.contract.contract_version not in {1, 2}:
                    raise ToolCapabilityPolicyError(
                        "artifact provider 工具 contract version 非法"
                    )

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
            "_trust_policies",
            MappingProxyType(dict(sorted(trust_policies.items()))),
        )
        object.__setattr__(
            self,
            "_capability_policies",
            MappingProxyType(dict(sorted(capability_policies.items()))),
        )
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

    @property
    def trust_policies(self) -> Mapping[str, ToolTrustPolicy]:
        return self._trust_policies

    def trust_policy_for(self, tool_name: str) -> ToolTrustPolicy:
        if not isinstance(tool_name, str) or not tool_name:
            raise ToolTrustPolicyError("trust policy 工具名不能为空")
        policy = self._trust_policies.get(tool_name)
        if policy is None:
            raise ToolTrustPolicyError(
                f"provider catalog 缺少工具 trust policy: {tool_name}"
            )
        return policy

    @property
    def capability_policies(self) -> Mapping[str, ToolPolicy]:
        return self._capability_policies

    def capability_policy_for(self, tool_name: str) -> ToolPolicy:
        if not isinstance(tool_name, str) or not tool_name:
            raise ToolCapabilityPolicyError(
                "capability policy 工具名不能为空"
            )
        policy = self._capability_policies.get(tool_name)
        if policy is None:
            raise ToolCapabilityPolicyError(
                f"provider catalog 工具没有 capability policy: {tool_name}"
            )
        return policy

    def decide_trust(
        self,
        tool_name: str,
        operation: ToolTrustOperation,
        *,
        is_superuser: bool,
        confirmed: bool = False,
    ) -> ToolTrustDecision:
        return self.trust_policy_for(tool_name).decide(
            operation,
            is_superuser=is_superuser,
            confirmed=confirmed,
        )

    def require_trust(
        self,
        tool_name: str,
        operation: ToolTrustOperation,
        *,
        is_superuser: bool,
        confirmed: bool = False,
    ) -> ToolTrustDecision:
        return self.trust_policy_for(tool_name).require(
            operation,
            is_superuser=is_superuser,
            confirmed=confirmed,
        )

    def trust_summary(self) -> Mapping[str, int]:
        counts = {level.value: 0 for level in ToolTrustLevel}
        for policy in self._trust_policies.values():
            counts[policy.trust.value] += 1
        return MappingProxyType(counts)

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


@dataclass(frozen=True)
class FileToolProvider:
    """Discover artifacts prepared by the current runtime transaction only."""

    provider_id: str = field(default="custom-file", init=False)
    source: ToolSource = field(default=ToolSource.CUSTOM_FILE, init=False)
    trust: ToolTrustLevel = field(default=ToolTrustLevel.REVIEWED, init=False)

    async def discover(
        self,
        context: ProviderDiscoveryContext[FileToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if (
            not isinstance(context, ProviderDiscoveryContext)
            or type(context.resources) is not FileToolResources
        ):
            raise TypeError("FileToolProvider 只接受 FileToolResources")
        discovered: list[DiscoveredTool] = []
        for artifact in sorted(
            context.resources.artifacts,
            key=lambda item: item.tool_name,
        ):
            artifact.verify(
                expected_artifact_digest=artifact.artifact_digest,
                expected_bundle_digest=None,
                generation=context.generation,
            )
            discovered.append(
                DiscoveredTool(
                    provider_id=self.provider_id,
                    source=self.source,
                    trust=self.trust,
                    generation=context.generation,
                    spec=artifact.spec,
                    artifact=artifact,
                )
            )
        return tuple(discovered)

    def validate_legacy_parity(
        self,
        discovered: tuple[DiscoveredTool, ...],
        legacy_tools: Mapping[str, object],
        legacy_dependencies: Mapping[str, object],
        *,
        generation: int,
        allow_additional_dependencies: bool = False,
    ) -> None:
        """Fail closed unless file discovery and its legacy view are equivalent."""

        _require_generation(generation)
        if type(allow_additional_dependencies) is not bool:
            raise TypeError("allow_additional_dependencies 必须是 bool")
        if not isinstance(discovered, tuple) or not all(
            isinstance(item, DiscoveredTool) for item in discovered
        ):
            raise TypeError("custom-file discovery 必须是 DiscoveredTool 元组")
        if not isinstance(legacy_tools, Mapping) or not isinstance(
            legacy_dependencies,
            Mapping,
        ):
            raise TypeError("custom-file legacy parity 输入必须是映射")

        expected: dict[str, ToolArtifact] = {}
        for item in discovered:
            if (
                item.provider_id != self.provider_id
                or item.source is not self.source
                or item.trust is not self.trust
                or not isinstance(item.artifact, ToolArtifact)
            ):
                raise ValueError("custom-file discovery 来源身份不一致")
            if item.generation != generation:
                raise ValueError("custom-file discovery generation 不一致")
            if item.spec is not item.artifact.spec:
                raise ValueError("custom-file discovery ToolSpec identity 不一致")
            if item.spec.name in expected:
                raise ValueError("custom-file discovery 不得包含重名工具")
            expected[item.spec.name] = item.artifact

        actual_names = {
            name
            for name, schema in legacy_tools.items()
            if isinstance(name, str)
            and isinstance(schema, Mapping)
            and schema.get("source") == self.source.value
        }
        expected_names = set(expected)
        if actual_names != expected_names:
            raise ValueError(
                "custom-file legacy 工具集合不一致: "
                f"missing={sorted(expected_names - actual_names)}, "
                f"extra={sorted(actual_names - expected_names)}"
            )

        for name, artifact in expected.items():
            schema = legacy_tools.get(name)
            if not isinstance(schema, Mapping):
                raise ValueError(f"custom-file legacy 工具 {name} Schema 缺失")
            spec = artifact.spec
            expected_schema = {
                **spec.as_legacy_schema(),
                "source": self.source.value,
                "declared_effect": artifact.contract.declared_effect.value,
                "effective_effect": spec.effect.value,
                "tool_artifact": artifact,
                "artifact_digest": artifact.artifact_digest,
                "generation": generation,
            }
            if artifact.artifact_version == 2:
                expected_schema.update(
                    {
                        "tool_contract_version": (
                            artifact.contract.contract_version
                        ),
                        "artifact_digest_version": artifact.artifact_version,
                        "requested_capabilities": (
                            artifact.contract.requested_capabilities
                        ),
                        "detected_capabilities": (
                            artifact.contract.detected_capabilities
                        ),
                        "admin_capabilities": (
                            artifact.contract.admin_capabilities
                        ),
                        "effective_capabilities": (
                            artifact.contract.effective_capabilities
                        ),
                        "capability_policy": (
                            spec.policy.capability_contract()
                            if spec.policy is not None
                            else None
                        ),
                    }
                )
            if set(schema) != set(expected_schema):
                raise ValueError(f"custom-file legacy 工具 {name} 字段不一致")
            if schema.get("tool_artifact") is not artifact:
                raise ValueError(f"custom-file legacy 工具 {name} ToolArtifact 不一致")
            if schema.get("tool_spec") is not spec:
                raise ValueError(f"custom-file legacy 工具 {name} ToolSpec 不一致")
            if schema.get("func") is not spec.handler:
                raise ValueError(f"custom-file legacy 工具 {name} handler 不一致")
            for field_name in set(expected_schema) - {
                "func",
                "tool_spec",
                "tool_artifact",
            }:
                if not _legacy_value_equal(
                    schema.get(field_name),
                    expected_schema[field_name],
                ):
                    raise ValueError(
                        f"custom-file legacy 工具 {name} {field_name} 不一致"
                    )
            artifact.verify(
                expected_artifact_digest=artifact.artifact_digest,
                expected_bundle_digest=None,
                generation=generation,
            )

            dependencies = legacy_dependencies.get(name, set())
            if not isinstance(dependencies, AbstractSet) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"custom-file legacy 工具 {name} dependencies 非法")
            expected_dependencies = set(spec.dependencies)
            dependencies_match = (
                expected_dependencies <= dependencies
                if allow_additional_dependencies
                else expected_dependencies == dependencies
            )
            if not dependencies_match:
                raise ValueError(f"custom-file legacy 工具 {name} dependencies 不一致")


@dataclass(frozen=True)
class GeneratedToolProvider:
    """Discover artifacts pinned to the transaction's exact after-state."""

    provider_id: str = field(default="generated", init=False)
    source: ToolSource = field(default=ToolSource.GENERATED, init=False)
    trust: ToolTrustLevel = field(default=ToolTrustLevel.UNTRUSTED, init=False)

    async def discover(
        self,
        context: ProviderDiscoveryContext[GeneratedToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if (
            not isinstance(context, ProviderDiscoveryContext)
            or type(context.resources) is not GeneratedToolResources
        ):
            raise TypeError("GeneratedToolProvider 只接受 GeneratedToolResources")
        artifact_bundles = {
            artifact.bundle_id for artifact in context.resources.artifacts
        }
        if artifact_bundles != set(context.resources.lifecycle_state.active):
            raise ValueError(
                "Generated discovery artifact bundle 集合与 after-state active 不一致"
            )
        discovered: list[DiscoveredTool] = []
        for artifact in sorted(
            context.resources.artifacts,
            key=lambda item: item.tool_name,
        ):
            assert artifact.bundle_digest is not None
            artifact.verify(
                expected_artifact_digest=artifact.artifact_digest,
                expected_bundle_digest=artifact.bundle_digest,
                generation=context.generation,
            )
            discovered.append(
                DiscoveredTool(
                    provider_id=self.provider_id,
                    source=self.source,
                    trust=self.trust,
                    generation=context.generation,
                    spec=artifact.spec,
                    artifact=artifact,
                )
            )
        return tuple(discovered)

    def validate_legacy_parity(
        self,
        discovered: tuple[DiscoveredTool, ...],
        legacy_tools: Mapping[str, object],
        legacy_dependencies: Mapping[str, object],
        *,
        generation: int,
        allow_additional_dependencies: bool = False,
    ) -> None:
        """Fail closed unless Generated discovery and legacy views are exact."""

        _require_generation(generation)
        if type(allow_additional_dependencies) is not bool:
            raise TypeError("allow_additional_dependencies 必须是 bool")
        if not isinstance(discovered, tuple) or not all(
            isinstance(item, DiscoveredTool) for item in discovered
        ):
            raise TypeError("generated discovery 必须是 DiscoveredTool 元组")
        if not isinstance(legacy_tools, Mapping) or not isinstance(
            legacy_dependencies,
            Mapping,
        ):
            raise TypeError("generated legacy parity 输入必须是映射")

        expected: dict[str, ToolArtifact] = {}
        for item in discovered:
            if (
                item.provider_id != self.provider_id
                or item.source is not self.source
                or item.trust is not self.trust
                or not isinstance(item.artifact, ToolArtifact)
            ):
                raise ValueError("generated discovery 来源身份不一致")
            if item.generation != generation:
                raise ValueError("generated discovery generation 不一致")
            if item.spec is not item.artifact.spec:
                raise ValueError("generated discovery ToolSpec identity 不一致")
            if item.spec.name in expected:
                raise ValueError("generated discovery 不得包含重名工具")
            expected[item.spec.name] = item.artifact

        actual_names = {
            name
            for name, schema in legacy_tools.items()
            if isinstance(name, str)
            and isinstance(schema, Mapping)
            and schema.get("source") == self.source.value
        }
        expected_names = set(expected)
        if actual_names != expected_names:
            raise ValueError(
                "generated legacy 工具集合不一致: "
                f"missing={sorted(expected_names - actual_names)}, "
                f"extra={sorted(actual_names - expected_names)}"
            )

        for name, artifact in expected.items():
            schema = legacy_tools.get(name)
            if not isinstance(schema, Mapping):
                raise ValueError(f"generated legacy 工具 {name} Schema 缺失")
            if artifact.bundle_id is None or artifact.bundle_digest is None:
                raise ValueError(f"generated legacy 工具 {name} bundle identity 缺失")
            spec = artifact.spec
            contract = artifact.contract
            expected_schema = {
                **spec.as_legacy_schema(),
                "source": self.source.value,
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
                "generation": generation,
            }
            if artifact.artifact_version == 2:
                expected_schema.update(
                    {
                        "tool_contract_version": contract.contract_version,
                        "artifact_digest_version": artifact.artifact_version,
                        "detected_capabilities": contract.detected_capabilities,
                        "admin_capabilities": contract.admin_capabilities,
                        "capability_policy": (
                            spec.policy.capability_contract()
                            if spec.policy is not None
                            else None
                        ),
                    }
                )
            if set(schema) != set(expected_schema):
                raise ValueError(f"generated legacy 工具 {name} 字段不一致")
            if schema.get("tool_artifact") is not artifact:
                raise ValueError(f"generated legacy 工具 {name} ToolArtifact 不一致")
            if schema.get("tool_spec") is not spec:
                raise ValueError(f"generated legacy 工具 {name} ToolSpec 不一致")
            if schema.get("func") is not spec.handler:
                raise ValueError(f"generated legacy 工具 {name} handler 不一致")
            for field_name in set(expected_schema) - {
                "func",
                "tool_spec",
                "tool_artifact",
            }:
                if not _legacy_value_equal(
                    schema.get(field_name),
                    expected_schema[field_name],
                ):
                    raise ValueError(
                        f"generated legacy 工具 {name} {field_name} 不一致"
                    )
            artifact.verify(
                expected_artifact_digest=artifact.artifact_digest,
                expected_bundle_digest=artifact.bundle_digest,
                generation=generation,
            )

            dependencies = legacy_dependencies.get(name, set())
            if not isinstance(dependencies, AbstractSet) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"generated legacy 工具 {name} dependencies 非法")
            expected_dependencies = set(spec.dependencies)
            dependencies_match = (
                expected_dependencies <= dependencies
                if allow_additional_dependencies
                else expected_dependencies == dependencies
            )
            if not dependencies_match:
                raise ValueError(f"generated legacy 工具 {name} dependencies 不一致")


@dataclass(frozen=True)
class MCPToolProvider:
    """Shadow MCP discovery from one transaction-pinned network candidate."""

    provider_id: str = field(default="mcp", init=False)
    source: ToolSource = field(default=ToolSource.MCP, init=False)
    trust: ToolTrustLevel = field(default=ToolTrustLevel.EXTERNAL, init=False)

    async def discover(
        self,
        context: ProviderDiscoveryContext[MCPToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if (
            not isinstance(context, ProviderDiscoveryContext)
            or type(context.resources) is not MCPToolResources
        ):
            raise TypeError("MCPToolProvider 只接受 MCPToolResources")
        return tuple(
            DiscoveredTool(
                provider_id=self.provider_id,
                source=self.source,
                trust=self.trust,
                generation=context.generation,
                spec=spec,
            )
            for spec in sorted(
                context.resources.specs,
                key=lambda item: item.name,
            )
        )

    def validate_legacy_parity(
        self,
        discovered: tuple[DiscoveredTool, ...],
        legacy_tools: Mapping[str, object],
        legacy_dependencies: Mapping[str, object],
        legacy_mcp_names: AbstractSet[str],
        *,
        generation: int,
        allow_additional_dependencies: bool = False,
    ) -> None:
        """Fail closed unless MCP provider, legacy tools and sidecar agree."""

        _require_generation(generation)
        if type(allow_additional_dependencies) is not bool:
            raise TypeError("allow_additional_dependencies 必须是 bool")
        if not isinstance(discovered, tuple) or not all(
            isinstance(item, DiscoveredTool) for item in discovered
        ):
            raise TypeError("mcp discovery 必须是 DiscoveredTool 元组")
        if not isinstance(legacy_tools, Mapping) or not isinstance(
            legacy_dependencies,
            Mapping,
        ):
            raise TypeError("mcp legacy parity 输入必须是映射")
        if not isinstance(legacy_mcp_names, AbstractSet) or not all(
            isinstance(name, str) for name in legacy_mcp_names
        ):
            raise TypeError("mcp legacy tool names 必须是字符串集合")

        expected: dict[str, ToolSpec] = {}
        for item in discovered:
            if (
                item.provider_id != self.provider_id
                or item.source is not self.source
                or item.trust is not self.trust
                or item.artifact is not None
            ):
                raise ValueError("mcp discovery 来源身份不一致")
            if item.generation != generation:
                raise ValueError("mcp discovery generation 不一致")
            if item.spec.name in expected:
                raise ValueError("mcp discovery 不得包含重名工具")
            expected[item.spec.name] = item.spec

        actual_names = {
            name
            for name, schema in legacy_tools.items()
            if isinstance(name, str)
            and isinstance(schema, Mapping)
            and schema.get("source") == self.source.value
        }
        expected_names = set(expected)
        if actual_names != expected_names:
            raise ValueError(
                "mcp legacy 工具集合不一致: "
                f"missing={sorted(expected_names - actual_names)}, "
                f"extra={sorted(actual_names - expected_names)}"
            )
        if set(legacy_mcp_names) != expected_names:
            raise ValueError("mcp legacy sidecar 工具集合不一致")

        for name, spec in expected.items():
            schema = legacy_tools.get(name)
            if not isinstance(schema, Mapping):
                raise ValueError(f"mcp legacy 工具 {name} Schema 缺失")
            expected_schema = {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
                "func": spec.handler,
                "source": self.source.value,
            }
            if set(schema) != set(expected_schema):
                raise ValueError(f"mcp legacy 工具 {name} 字段不一致")
            if schema.get("func") is not spec.handler:
                raise ValueError(f"mcp legacy 工具 {name} handler 不一致")
            for field_name in (
                "name",
                "description",
                "parameters",
                "source",
            ):
                if not _legacy_value_equal(
                    schema.get(field_name),
                    expected_schema[field_name],
                ):
                    raise ValueError(
                        f"mcp legacy 工具 {name} {field_name} 不一致"
                    )

            dependencies = legacy_dependencies.get(name, set())
            if not isinstance(dependencies, AbstractSet) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"mcp legacy 工具 {name} dependencies 非法")
            expected_dependencies = set(spec.dependencies)
            dependencies_match = (
                expected_dependencies <= dependencies
                if allow_additional_dependencies
                else expected_dependencies == dependencies
            )
            if not dependencies_match:
                raise ValueError(f"mcp legacy 工具 {name} dependencies 不一致")


@dataclass(frozen=True)
class BuiltinToolProvider:
    """Shadow code-defined builtin adapters without trusting external results."""

    provider_id: str = field(default="builtin", init=False)
    source: ToolSource = field(default=ToolSource.BUILTIN, init=False)
    trust: ToolTrustLevel = field(default=ToolTrustLevel.TRUSTED, init=False)

    async def discover(
        self,
        context: ProviderDiscoveryContext[BuiltinToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if (
            not isinstance(context, ProviderDiscoveryContext)
            or type(context.resources) is not BuiltinToolResources
        ):
            raise TypeError("BuiltinToolProvider 只接受 BuiltinToolResources")
        return tuple(
            DiscoveredTool(
                provider_id=self.provider_id,
                source=self.source,
                trust=self.trust,
                generation=context.generation,
                spec=spec,
            )
            for spec in sorted(
                context.resources.specs,
                key=lambda item: item.name,
            )
        )

    def validate_legacy_parity(
        self,
        discovered: tuple[DiscoveredTool, ...],
        legacy_specs: tuple[ToolSpec, ...],
        legacy_dependencies: Mapping[str, object],
        *,
        generation: int,
        allow_additional_dependencies: bool = False,
    ) -> None:
        """Fail closed unless the code-defined branch and catalog are exact."""

        _require_generation(generation)
        if type(allow_additional_dependencies) is not bool:
            raise TypeError("allow_additional_dependencies 必须是 bool")
        if not isinstance(discovered, tuple) or not all(
            isinstance(item, DiscoveredTool) for item in discovered
        ):
            raise TypeError("builtin discovery 必须是 DiscoveredTool 元组")
        if not isinstance(legacy_specs, tuple) or not all(
            isinstance(spec, ToolSpec) for spec in legacy_specs
        ):
            raise TypeError("builtin legacy specs 必须是 ToolSpec 元组")
        if not isinstance(legacy_dependencies, Mapping):
            raise TypeError("builtin legacy dependencies 必须是映射")

        expected: dict[str, ToolSpec] = {}
        for item in discovered:
            if (
                item.provider_id != self.provider_id
                or item.source is not self.source
                or item.trust is not self.trust
                or item.artifact is not None
            ):
                raise ValueError("builtin discovery 来源身份不一致")
            if item.generation != generation:
                raise ValueError("builtin discovery generation 不一致")
            if item.spec.name in expected:
                raise ValueError("builtin discovery 不得包含重名工具")
            expected[item.spec.name] = item.spec

        actual: dict[str, ToolSpec] = {}
        for spec in legacy_specs:
            if spec.name in actual:
                raise ValueError("builtin legacy specs 不得包含重名工具")
            actual[spec.name] = spec
        expected_names = set(expected)
        actual_names = set(actual)
        if actual_names != expected_names:
            raise ValueError(
                "builtin legacy 工具集合不一致: "
                f"missing={sorted(expected_names - actual_names)}, "
                f"extra={sorted(actual_names - expected_names)}"
            )

        for name, spec in expected.items():
            if actual[name] is not spec:
                raise ValueError(f"builtin legacy 工具 {name} ToolSpec 不一致")
            dependencies = legacy_dependencies.get(name, set())
            if not isinstance(dependencies, AbstractSet) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"builtin legacy 工具 {name} dependencies 非法")
            expected_dependencies = set(spec.dependencies)
            dependencies_match = (
                expected_dependencies <= dependencies
                if allow_additional_dependencies
                else expected_dependencies == dependencies
            )
            if not dependencies_match:
                raise ValueError(
                    f"builtin legacy 工具 {name} dependencies 不一致"
                )


@dataclass(frozen=True)
class NoneBotPluginProvider:
    """Shadow reviewed legacy plugin adapters without cutting consumers over."""

    provider_id: str = field(default="nonebot-plugin", init=False)
    source: ToolSource = field(default=ToolSource.NONEBOT_PLUGIN, init=False)
    trust: ToolTrustLevel = field(default=ToolTrustLevel.REVIEWED, init=False)

    async def discover(
        self,
        context: ProviderDiscoveryContext[NoneBotPluginToolResources],
    ) -> tuple[DiscoveredTool, ...]:
        if (
            not isinstance(context, ProviderDiscoveryContext)
            or type(context.resources) is not NoneBotPluginToolResources
        ):
            raise TypeError(
                "NoneBotPluginProvider 只接受 NoneBotPluginToolResources"
            )
        return tuple(
            DiscoveredTool(
                provider_id=self.provider_id,
                source=self.source,
                trust=self.trust,
                generation=context.generation,
                spec=spec,
            )
            for spec in sorted(
                context.resources.specs,
                key=lambda item: item.name,
            )
        )

    def validate_legacy_parity(
        self,
        discovered: tuple[DiscoveredTool, ...],
        legacy_info: Mapping[str, object],
        legacy_dependencies: Mapping[str, object],
        *,
        generation: int,
        allow_additional_dependencies: bool = False,
    ) -> None:
        """Fail closed unless plugin metadata and adapters match the catalog."""

        _require_generation(generation)
        if type(allow_additional_dependencies) is not bool:
            raise TypeError("allow_additional_dependencies 必须是 bool")
        if not isinstance(discovered, tuple) or not all(
            isinstance(item, DiscoveredTool) for item in discovered
        ):
            raise TypeError("nonebot-plugin discovery 必须是 DiscoveredTool 元组")
        if not isinstance(legacy_info, Mapping) or not isinstance(
            legacy_dependencies,
            Mapping,
        ):
            raise TypeError("nonebot-plugin legacy parity 输入必须是映射")

        expected: dict[str, ToolSpec] = {}
        for item in discovered:
            if (
                item.provider_id != self.provider_id
                or item.source is not self.source
                or item.trust is not self.trust
                or item.artifact is not None
            ):
                raise ValueError("nonebot-plugin discovery 来源身份不一致")
            if item.generation != generation:
                raise ValueError("nonebot-plugin discovery generation 不一致")
            if item.spec.name in expected:
                raise ValueError("nonebot-plugin discovery 不得包含重名工具")
            expected[item.spec.name] = item.spec

        actual_names = set(legacy_info)
        expected_names = set(expected)
        if actual_names != expected_names:
            raise ValueError(
                "nonebot-plugin legacy 工具集合不一致: "
                f"missing={sorted(expected_names - actual_names)}, "
                f"extra={sorted(actual_names - expected_names)}"
            )

        for name, spec in expected.items():
            entry = legacy_info[name]
            if not isinstance(entry, Mapping):
                raise ValueError(f"nonebot-plugin legacy 工具 {name} 描述非法")
            if entry.get("source") != self.source.value:
                raise ValueError(f"nonebot-plugin legacy 工具 {name} source 不一致")
            if entry.get("tool_spec") is not spec:
                raise ValueError(f"nonebot-plugin legacy 工具 {name} ToolSpec 不一致")
            description = build_compatibility_description(name, entry)
            if spec.description != description:
                raise ValueError(
                    f"nonebot-plugin legacy 工具 {name} description 不一致"
                )
            if (
                spec.permission != "user"
                or spec.effect is not ToolEffect.MUTATING
                or not callable(spec.handler)
            ):
                raise ValueError(
                    f"nonebot-plugin legacy 工具 {name} 兼容契约不一致"
                )
            dependencies = legacy_dependencies.get(name, set())
            if not isinstance(dependencies, AbstractSet) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError(
                    f"nonebot-plugin legacy 工具 {name} dependencies 非法"
                )
            expected_dependencies = set(spec.dependencies)
            dependencies_match = (
                expected_dependencies <= dependencies
                if allow_additional_dependencies
                else expected_dependencies == dependencies
            )
            if not dependencies_match:
                raise ValueError(
                    f"nonebot-plugin legacy 工具 {name} dependencies 不一致"
                )


registered_tool_provider = RegisteredToolProvider()
_registered_tool_provider_contract: ToolProvider[RegisteredToolResources] = registered_tool_provider
file_tool_provider = FileToolProvider()
_file_tool_provider_contract: ToolProvider[FileToolResources] = file_tool_provider
generated_tool_provider = GeneratedToolProvider()
_generated_tool_provider_contract: ToolProvider[GeneratedToolResources] = generated_tool_provider
mcp_tool_provider = MCPToolProvider()
_mcp_tool_provider_contract: ToolProvider[MCPToolResources] = mcp_tool_provider
builtin_tool_provider = BuiltinToolProvider()
_builtin_tool_provider_contract: ToolProvider[BuiltinToolResources] = builtin_tool_provider
nonebot_plugin_provider = NoneBotPluginProvider()
_nonebot_plugin_provider_contract: ToolProvider[NoneBotPluginToolResources] = nonebot_plugin_provider
provider_registry = ProviderRegistry(
    (
        ProviderRegistration.from_provider(registered_tool_provider),
        ProviderRegistration.from_provider(file_tool_provider),
        ProviderRegistration.from_provider(generated_tool_provider),
        ProviderRegistration.from_provider(mcp_tool_provider),
        ProviderRegistration.from_provider(builtin_tool_provider),
        ProviderRegistration.from_provider(nonebot_plugin_provider),
    )
)

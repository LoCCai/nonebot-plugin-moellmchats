from collections import deque
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import nonebot
from nonebot.log import logger
import ujson as json

from .builtin_tools import WEB_SEARCH_TOOL_SPEC, builtin_tool_specs
from .custom_tool_loader import load_file_tools
from .generated_tools import generated_tool_store
from .mcp_manager import mcp_manager
from .model_selector import config_path, model_selector
from .nonebot_plugin_tools import build_nonebot_plugin_candidate
from .runtime_snapshot import (
    immutable_mapping,
    mutable_value,
    validate_generated_stamp,
)
from .tool_artifacts import ToolArtifact
from .tool_catalog_cache import ToolCatalogRecord, ToolCatalogRenderContext
from .tool_contracts import ToolSpec, tool_registry, validate_parameters_schema
from .tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderRegistration,
    ToolSource,
    ToolTrustDecision,
    ToolTrustOperation,
    builtin_tool_provider,
    file_tool_provider,
    generated_tool_provider,
    mcp_tool_provider,
    nonebot_plugin_provider,
    registered_tool_provider,
)

_FileToolCandidate = tuple[
    Mapping[str, Mapping[str, Any]],
    Mapping[str, AbstractSet[str]],
]
_GeneratedToolCandidate = tuple[
    Mapping[str, Mapping[str, Any]],
    Mapping[str, AbstractSet[str]],
]

_PROVIDER_CONSUMER_IDS = frozenset(
    {
        "registered",
        "custom-file",
        "generated",
        "mcp",
        "builtin",
        "nonebot-plugin",
    }
)


class ProviderConsumerParityError(RuntimeError):
    """A cut-over Provider view no longer matches its legacy rollback view."""


class LlmToolExecutionRoute(str, Enum):
    BUILTIN_SEARCH = "builtin_search"
    CUSTOM_TOOL = "custom_tool"
    NONEBOT_PLUGIN = "nonebot_plugin"


@dataclass(frozen=True)
class LlmToolExecutionView:
    """One request-bound tool identity consumed by ``llm_tools``.

    ``legacy_entry`` remains the rollback execution adapter until the later
    PendingAction/Search consumers are migrated.  On the Provider path,
    ``source``, ``spec`` and ``trust_decision`` are canonical and are checked
    against that adapter before this view can be returned.
    """

    tool_name: str
    generation: int
    route: LlmToolExecutionRoute
    source: ToolSource | None
    spec: ToolSpec | None
    legacy_entry: Mapping[str, Any] | None
    provider_authoritative: bool
    trust_decision: ToolTrustDecision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("llm_tools 执行视图工具名不能为空")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("llm_tools 执行视图 generation 非法")
        if not isinstance(self.route, LlmToolExecutionRoute):
            raise TypeError("llm_tools 执行视图 route 非法")
        if self.source is not None and not isinstance(self.source, ToolSource):
            raise TypeError("llm_tools 执行视图 source 非法")
        if self.source is not None:
            expected_route = (
                LlmToolExecutionRoute.BUILTIN_SEARCH
                if self.source is ToolSource.BUILTIN
                else LlmToolExecutionRoute.NONEBOT_PLUGIN
                if self.source is ToolSource.NONEBOT_PLUGIN
                else LlmToolExecutionRoute.CUSTOM_TOOL
            )
            if self.route is not expected_route:
                raise ValueError("llm_tools 执行视图 source/route 不一致")
        if self.spec is not None:
            if not isinstance(self.spec, ToolSpec) or self.spec.name != self.tool_name:
                raise ValueError("llm_tools 执行视图 ToolSpec identity 不一致")
        if self.legacy_entry is not None and not isinstance(
            self.legacy_entry,
            Mapping,
        ):
            raise TypeError("llm_tools 执行视图 legacy entry 必须是映射")
        if self.route is LlmToolExecutionRoute.BUILTIN_SEARCH:
            if self.legacy_entry is not None:
                raise ValueError("builtin llm_tools 执行视图不得携带 sidecar entry")
        elif self.legacy_entry is None:
            raise ValueError("legacy adapter llm_tools 执行视图缺少 sidecar entry")
        if type(self.provider_authoritative) is not bool:
            raise TypeError("llm_tools 执行视图 authority 标志必须是布尔值")
        if self.provider_authoritative:
            if self.source is None or self.spec is None:
                raise ValueError("Provider llm_tools 执行视图缺少 canonical identity")
            if not isinstance(self.trust_decision, ToolTrustDecision):
                raise ValueError("Provider llm_tools 执行视图缺少 trust decision")
            if (
                self.trust_decision.tool_name != self.tool_name
                or self.trust_decision.generation != self.generation
                or self.trust_decision.operation
                is not ToolTrustOperation.EXECUTION
                or self.trust_decision.source is not self.source
                or self.trust_decision.effect is not self.spec.effect
                or self.trust_decision.permission != self.spec.permission
            ):
                raise ValueError("Provider llm_tools trust decision identity 不一致")
        elif self.trust_decision is not None:
            raise ValueError("legacy llm_tools 执行视图不得伪造 trust decision")


@dataclass(frozen=True)
class PendingActionExecutionView:
    """One confirmation-bound mutating tool execution view.

    The Provider path executes a canonical adapter derived from ``ToolSpec``.
    ``legacy_entry`` remains available only for per-confirmation parity and the
    independent rollback path.
    """

    tool_name: str
    generation: int
    source: ToolSource | None
    spec: ToolSpec | None
    execution_entry: Mapping[str, Any]
    legacy_entry: Mapping[str, Any]
    bundle_digest: str | None
    provider_authoritative: bool
    trust_decision: ToolTrustDecision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("PendingAction 执行视图工具名不能为空")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("PendingAction 执行视图 generation 非法")
        if self.source is not None and self.source not in {
            ToolSource.REGISTERED,
            ToolSource.CUSTOM_FILE,
            ToolSource.GENERATED,
            ToolSource.MCP,
        }:
            raise ValueError("PendingAction 执行视图 source 非法")
        if self.spec is not None and (
            not isinstance(self.spec, ToolSpec)
            or self.spec.name != self.tool_name
        ):
            raise ValueError("PendingAction 执行视图 ToolSpec identity 不一致")
        if not isinstance(self.execution_entry, Mapping) or not isinstance(
            self.legacy_entry,
            Mapping,
        ):
            raise TypeError("PendingAction 执行适配器必须是映射")
        if self.bundle_digest is not None and not isinstance(
            self.bundle_digest,
            str,
        ):
            raise TypeError("PendingAction bundle digest 非法")
        if type(self.provider_authoritative) is not bool:
            raise TypeError("PendingAction authority 标志必须是布尔值")
        if self.provider_authoritative:
            if self.source is None or self.spec is None:
                raise ValueError("Provider PendingAction 视图缺少 canonical identity")
            if (
                self.execution_entry.get("func") is not self.spec.handler
                or self.execution_entry.get("tool_spec") is not self.spec
            ):
                raise ValueError("Provider PendingAction canonical adapter 不一致")
            if not isinstance(self.trust_decision, ToolTrustDecision):
                raise ValueError("Provider PendingAction 视图缺少 trust decision")
            if (
                self.trust_decision.tool_name != self.tool_name
                or self.trust_decision.generation != self.generation
                or self.trust_decision.operation
                is not ToolTrustOperation.EXECUTION
                or self.trust_decision.source is not self.source
                or self.trust_decision.effect is not self.spec.effect
                or self.trust_decision.permission != self.spec.permission
            ):
                raise ValueError("Provider PendingAction trust decision identity 不一致")
        else:
            if self.execution_entry is not self.legacy_entry:
                raise ValueError("legacy PendingAction 必须执行 rollback adapter")
            if self.trust_decision is not None:
                raise ValueError("legacy PendingAction 不得伪造 trust decision")


@dataclass(frozen=True)
class SearchExtractorView:
    """One request-bound ``extract_webpage`` selection view.

    The Provider path uses the canonical identity and selection trust decision
    to decide whether search results may advertise source URLs.  The legacy
    entry is retained only for per-call parity and the independent rollback
    path; the extractor is not executed by this consumer.
    """

    tool_name: str
    generation: int
    source: ToolSource | None
    spec: ToolSpec | None
    legacy_entry: Mapping[str, Any]
    provider_authoritative: bool
    trust_decision: ToolTrustDecision | None = None

    def __post_init__(self) -> None:
        if self.tool_name != "extract_webpage":
            raise ValueError("Search extractor 执行视图工具名非法")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("Search extractor 执行视图 generation 非法")
        if self.source is not None and self.source not in {
            ToolSource.REGISTERED,
            ToolSource.CUSTOM_FILE,
            ToolSource.GENERATED,
            ToolSource.MCP,
        }:
            raise ValueError("Search extractor 执行视图 source 非法")
        if self.spec is not None and (
            not isinstance(self.spec, ToolSpec)
            or self.spec.name != self.tool_name
        ):
            raise ValueError("Search extractor ToolSpec identity 不一致")
        if not isinstance(self.legacy_entry, Mapping):
            raise TypeError("Search extractor legacy adapter 必须是映射")
        if type(self.provider_authoritative) is not bool:
            raise TypeError("Search extractor authority 标志必须是布尔值")
        if self.provider_authoritative:
            if self.source is None or self.spec is None:
                raise ValueError("Provider Search extractor 缺少 canonical identity")
            if not isinstance(self.trust_decision, ToolTrustDecision):
                raise ValueError("Provider Search extractor 缺少 trust decision")
            if (
                self.trust_decision.tool_name != self.tool_name
                or self.trust_decision.generation != self.generation
                or self.trust_decision.operation
                is not ToolTrustOperation.SELECTION
                or self.trust_decision.source is not self.source
                or self.trust_decision.effect is not self.spec.effect
                or self.trust_decision.permission != self.spec.permission
            ):
                raise ValueError("Provider Search extractor trust decision identity 不一致")
        elif self.trust_decision is not None:
            raise ValueError("legacy Search extractor 不得伪造 trust decision")


class ToolManagementTargetKind(str, Enum):
    EXACT_TOOL = "exact_tool"
    MCP_SERVICE = "mcp_service"


@dataclass(frozen=True)
class ToolManagementView:
    """One generation-bound identifier consumed by tool management commands.

    Exact tools are bound to one canonical ``ToolSpec`` and management trust
    decision on the Provider path.  MCP service and wildcard selectors bind to
    the frozen server identifier plus every canonical MCP member in the same
    generation; a configured service is still a valid selector before it
    discovers its first tool.
    """

    identifier: str
    generation: int
    kind: ToolManagementTargetKind
    label: str
    source: ToolSource | None
    spec: ToolSpec | None
    legacy_entry: Mapping[str, Any] | None
    matched_tool_names: tuple[str, ...]
    provider_authoritative: bool
    trust_decisions: tuple[ToolTrustDecision, ...] = ()
    selector_allowed: bool = True
    selector_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not self.identifier:
            raise ValueError("工具管理视图标识不能为空")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("工具管理视图 generation 非法")
        if not isinstance(self.kind, ToolManagementTargetKind):
            raise TypeError("工具管理视图 target kind 非法")
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("工具管理视图 label 不能为空")
        if self.source is not None and not isinstance(self.source, ToolSource):
            raise TypeError("工具管理视图 source 非法")
        if self.spec is not None and (
            not isinstance(self.spec, ToolSpec)
            or self.spec.name != self.identifier
        ):
            raise ValueError("工具管理视图 ToolSpec identity 不一致")
        if self.legacy_entry is not None and not isinstance(
            self.legacy_entry,
            Mapping,
        ):
            raise TypeError("工具管理 legacy entry 必须是映射")
        if (
            not isinstance(self.matched_tool_names, tuple)
            or tuple(sorted(set(self.matched_tool_names)))
            != self.matched_tool_names
            or not all(
                isinstance(name, str) and name
                for name in self.matched_tool_names
            )
        ):
            raise ValueError("工具管理匹配工具名必须是有序唯一字符串元组")
        if type(self.provider_authoritative) is not bool:
            raise TypeError("工具管理 authority 标志必须是布尔值")
        if not isinstance(self.trust_decisions, tuple) or not all(
            isinstance(decision, ToolTrustDecision)
            for decision in self.trust_decisions
        ):
            raise TypeError("工具管理 trust decisions 必须是不可变元组")
        if type(self.selector_allowed) is not bool:
            raise TypeError("工具管理 selector allowed 必须是布尔值")
        if self.selector_allowed:
            if self.selector_reason is not None:
                raise ValueError("允许的工具管理 selector 不得携带拒绝原因")
        elif not isinstance(self.selector_reason, str) or not self.selector_reason:
            raise ValueError("拒绝的工具管理 selector 必须携带原因")

        if self.kind is ToolManagementTargetKind.EXACT_TOOL:
            if self.matched_tool_names != (self.identifier,):
                raise ValueError("精确工具管理视图匹配 identity 不一致")
        else:
            if self.source is not ToolSource.MCP or self.spec is not None:
                raise ValueError("MCP 服务管理视图 identity 非法")
            if self.legacy_entry is not None:
                raise ValueError("MCP 服务管理视图不得携带工具 sidecar")

        if self.provider_authoritative:
            decision_names = tuple(
                sorted(decision.tool_name for decision in self.trust_decisions)
            )
            if decision_names != self.matched_tool_names:
                raise ValueError("Provider 工具管理 trust decision 集合不一致")
            if self.kind is ToolManagementTargetKind.EXACT_TOOL and (
                self.source is None
                or self.spec is None
                or len(self.trust_decisions) != 1
            ):
                raise ValueError("Provider 精确工具管理视图缺少 canonical identity")
            exact_spec = self.spec
            for decision in self.trust_decisions:
                if (
                    decision.generation != self.generation
                    or decision.operation is not ToolTrustOperation.MANAGEMENT
                ):
                    raise ValueError("Provider 工具管理 trust decision identity 不一致")
                if self.kind is ToolManagementTargetKind.EXACT_TOOL:
                    assert exact_spec is not None
                    if (
                        decision.source is not self.source
                        or decision.effect is not exact_spec.effect
                        or decision.permission != exact_spec.permission
                    ):
                        raise ValueError(
                            "Provider 精确工具管理 decision/spec 不一致"
                        )
                if self.kind is ToolManagementTargetKind.MCP_SERVICE and (
                    decision.source is not ToolSource.MCP
                ):
                    raise ValueError("MCP 服务管理 decision source 不一致")
        else:
            if self.trust_decisions:
                raise ValueError("legacy 工具管理视图不得伪造 trust decision")
            if not self.selector_allowed or self.selector_reason is not None:
                raise ValueError("legacy 工具管理视图不得伪造 selector policy")

    @property
    def allowed(self) -> bool:
        return self.selector_allowed and all(
            decision.allowed for decision in self.trust_decisions
        )

    @property
    def denial_reason(self) -> str | None:
        if not self.selector_allowed:
            return self.selector_reason
        return next(
            (
                decision.reason
                for decision in self.trust_decisions
                if not decision.allowed
            ),
            None,
        )

    def selector_audit_metadata(self) -> dict[str, object]:
        """Return argument-free fields for an MCP group-selector audit."""

        return {
            "identifier": self.identifier,
            "generation": self.generation,
            "target_kind": self.kind.value,
            "source": self.source.value if self.source is not None else None,
            "provider_authoritative": self.provider_authoritative,
            "matched_tool_count": len(self.matched_tool_names),
            "allowed": self.allowed,
            "reason": self.denial_reason or "trust policy 允许",
        }


@dataclass(frozen=True)
class ToolSnapshot:
    generation: int
    plugin_info: Mapping[str, Mapping[str, Any]]
    custom_tools: Mapping[str, Mapping[str, Any]]
    tool_dependencies: Mapping[str, AbstractSet[str]]
    mcp_tool_names: AbstractSet[str]
    provider_catalog: ProviderCatalogSnapshot | None = None
    legacy_plugin_names: AbstractSet[str] | None = None
    mcp_server_identifiers: AbstractSet[str] | None = None
    generated_state_revision: int = 0
    generated_state_digest: str = ""
    generated_active: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("plugin_info", "custom_tools", "tool_dependencies"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ValueError(f"ToolSnapshot.{field_name} 必须是映射")
            object.__setattr__(self, field_name, immutable_mapping(value))
        if not isinstance(self.mcp_tool_names, AbstractSet) or not all(
            isinstance(name, str) for name in self.mcp_tool_names
        ):
            raise ValueError("ToolSnapshot.mcp_tool_names 必须是工具名集合")
        object.__setattr__(self, "mcp_tool_names", frozenset(self.mcp_tool_names))
        for field_name in ("legacy_plugin_names", "mcp_server_identifiers"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, AbstractSet) or not all(
                isinstance(name, str) and name for name in value
            ):
                raise ValueError(f"ToolSnapshot.{field_name} 必须是字符串集合")
            object.__setattr__(self, field_name, frozenset(value))
        provider_catalog = self.provider_catalog
        if provider_catalog is None:
            provider_catalog = ProviderCatalogSnapshot.empty(self.generation)
        if not isinstance(provider_catalog, ProviderCatalogSnapshot):
            raise ValueError("ToolSnapshot.provider_catalog 必须是 v2 provider catalog")
        if provider_catalog.generation != self.generation:
            raise ValueError("ToolSnapshot.provider_catalog generation 不一致")
        object.__setattr__(self, "provider_catalog", provider_catalog)
        registered = provider_catalog.registrations.get("registered")
        if registered is not None:
            expected = ProviderRegistration.from_provider(
                registered_tool_provider
            )
            if registered != expected:
                raise ValueError("ToolSnapshot registered provider identity 不一致")
            registered_tool_provider.validate_legacy_parity(
                provider_catalog.tools_for_provider("registered"),
                self.custom_tools,
                self.tool_dependencies,
                generation=self.generation,
                allow_additional_dependencies=True,
            )
        custom_file = provider_catalog.registrations.get("custom-file")
        if custom_file is not None:
            expected = ProviderRegistration.from_provider(file_tool_provider)
            if custom_file != expected:
                raise ValueError("ToolSnapshot custom-file provider identity 不一致")
            file_tool_provider.validate_legacy_parity(
                provider_catalog.tools_for_provider("custom-file"),
                self.custom_tools,
                self.tool_dependencies,
                generation=self.generation,
                allow_additional_dependencies=True,
            )
        generated = provider_catalog.registrations.get("generated")
        if generated is not None:
            expected = ProviderRegistration.from_provider(
                generated_tool_provider
            )
            if generated != expected:
                raise ValueError("ToolSnapshot generated provider identity 不一致")
            generated_tool_provider.validate_legacy_parity(
                provider_catalog.tools_for_provider("generated"),
                self.custom_tools,
                self.tool_dependencies,
                generation=self.generation,
                allow_additional_dependencies=True,
            )
        mcp = provider_catalog.registrations.get("mcp")
        if mcp is not None:
            expected = ProviderRegistration.from_provider(mcp_tool_provider)
            if mcp != expected:
                raise ValueError("ToolSnapshot mcp provider identity 不一致")
            mcp_tool_provider.validate_legacy_parity(
                provider_catalog.tools_for_provider("mcp"),
                self.custom_tools,
                self.tool_dependencies,
                self.mcp_tool_names,
                generation=self.generation,
                allow_additional_dependencies=True,
            )
        builtin = provider_catalog.registrations.get("builtin")
        if builtin is not None:
            expected = ProviderRegistration.from_provider(builtin_tool_provider)
            if builtin != expected:
                raise ValueError("ToolSnapshot builtin provider identity 不一致")
            builtin_tool_provider.validate_legacy_parity(
                provider_catalog.tools_for_provider("builtin"),
                builtin_tool_specs(),
                self.tool_dependencies,
                generation=self.generation,
                allow_additional_dependencies=True,
            )
        nonebot_plugin = provider_catalog.registrations.get("nonebot-plugin")
        if nonebot_plugin is not None:
            expected = ProviderRegistration.from_provider(
                nonebot_plugin_provider
            )
            if nonebot_plugin != expected:
                raise ValueError(
                    "ToolSnapshot nonebot-plugin provider identity 不一致"
                )
            nonebot_plugin_provider.validate_legacy_parity(
                provider_catalog.tools_for_provider("nonebot-plugin"),
                self.plugin_info,
                self.tool_dependencies,
                generation=self.generation,
                allow_additional_dependencies=True,
            )
        object.__setattr__(
            self,
            "generated_active",
            validate_generated_stamp(
                self.generated_state_revision,
                self.generated_state_digest,
                self.generated_active,
            ),
        )

    def expand_dependencies(self, plugins: set) -> set:
        expanded = {p for p in plugins if not tool_manager.is_tool_blacklisted(p)}
        queue = deque(expanded)
        while queue:
            current = queue.popleft()
            for dependency in self.tool_dependencies.get(current, set()):
                if tool_manager.is_tool_blacklisted(dependency):
                    continue
                if dependency not in expanded and (
                    dependency in self.custom_tools or dependency in self.plugin_info
                ):
                    expanded.add(dependency)
                    queue.append(dependency)
        return expanded

    def get_tool_schema(
        self,
        plugin_names: list,
        include_search: bool = False,
        *,
        is_superuser: bool = False,
    ) -> list:
        return ToolManager.build_tool_schema(
            plugin_names,
            include_search=include_search,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            is_superuser=is_superuser,
        )

    def get_llm_payload_tools(
        self,
        plugin_names: AbstractSet[str],
        *,
        tools_enabled: bool,
        search_enabled: bool,
        is_superuser: bool = False,
        provider_cutover: bool | None = None,
    ) -> tuple[set[str], list[dict[str, Any]]]:
        """Resolve one LLM payload tool view with Provider/legacy parity."""

        if not isinstance(plugin_names, AbstractSet) or not all(
            isinstance(name, str) for name in plugin_names
        ):
            raise TypeError("llm_payload plugin_names 必须是字符串集合")
        for field_name, value in (
            ("tools_enabled", tools_enabled),
            ("search_enabled", search_enabled),
            ("is_superuser", is_superuser),
        ):
            if type(value) is not bool:
                raise TypeError(f"llm_payload {field_name} 必须是布尔值")

        initial_plugins = set(plugin_names)
        legacy_plugins = self.expand_dependencies(initial_plugins)
        legacy_schema = ToolManager.build_llm_payload_schema(
            plugin_names=list(legacy_plugins),
            tools_enabled=tools_enabled,
            search_enabled=search_enabled,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            is_superuser=is_superuser,
        )
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_llm_payload_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("llm_payload Provider cutover 开关必须是布尔值")

        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not provider_cutover
            or not tools_enabled
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return legacy_plugins, legacy_schema

        provider_plugins = ToolManager.expand_provider_dependencies(
            provider_catalog=provider_catalog,
            plugin_names=initial_plugins,
        )
        if provider_plugins != legacy_plugins:
            raise ProviderConsumerParityError(
                "llm_payload Provider 依赖视图与 legacy rollback view 不一致"
            )
        provider_schema = ToolManager.build_provider_llm_payload_schema(
            provider_catalog=provider_catalog,
            plugin_names=list(legacy_plugins),
            search_enabled=search_enabled,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            is_superuser=is_superuser,
        )
        if provider_schema != legacy_schema:
            raise ProviderConsumerParityError(
                "llm_payload Provider schema 与 legacy rollback view 不一致"
            )
        return provider_plugins, provider_schema

    def _legacy_llm_tool_execution_view(
        self,
        tool_name: str,
    ) -> LlmToolExecutionView | None:
        if tool_name == WEB_SEARCH_TOOL_SPEC.name:
            return LlmToolExecutionView(
                tool_name=tool_name,
                generation=self.generation,
                route=LlmToolExecutionRoute.BUILTIN_SEARCH,
                source=ToolSource.BUILTIN,
                spec=WEB_SEARCH_TOOL_SPEC,
                legacy_entry=None,
                provider_authoritative=False,
            )

        custom_entry = self.custom_tools.get(tool_name)
        if custom_entry is not None:
            raw_source = custom_entry.get("source")
            source = (
                {
                    "registered": ToolSource.REGISTERED,
                    "custom_file": ToolSource.CUSTOM_FILE,
                    "generated": ToolSource.GENERATED,
                    "mcp": ToolSource.MCP,
                }.get(raw_source)
                if isinstance(raw_source, str)
                else None
            )
            if tool_name in self.mcp_tool_names:
                source = ToolSource.MCP
            spec = custom_entry.get("tool_spec")
            return LlmToolExecutionView(
                tool_name=tool_name,
                generation=self.generation,
                route=LlmToolExecutionRoute.CUSTOM_TOOL,
                source=source,
                spec=spec if isinstance(spec, ToolSpec) else None,
                legacy_entry=custom_entry,
                provider_authoritative=False,
            )

        plugin_entry = self.plugin_info.get(tool_name)
        if plugin_entry is not None:
            spec = plugin_entry.get("tool_spec")
            return LlmToolExecutionView(
                tool_name=tool_name,
                generation=self.generation,
                route=LlmToolExecutionRoute.NONEBOT_PLUGIN,
                source=ToolSource.NONEBOT_PLUGIN,
                spec=spec if isinstance(spec, ToolSpec) else None,
                legacy_entry=plugin_entry,
                provider_authoritative=False,
            )
        return None

    def resolve_llm_tool_execution(
        self,
        tool_name: str,
        *,
        is_superuser: bool,
        provider_cutover: bool | None = None,
    ) -> LlmToolExecutionView | None:
        """Resolve one model tool call with Provider/legacy parity.

        Unknown names return ``None`` and are rejected by ``llm_tools`` before
        any adapter runs.  A known name that drifts between the two published
        views raises instead of silently choosing either identity.
        """

        if not isinstance(tool_name, str) or not tool_name:
            raise TypeError("llm_tools tool_name 必须是非空字符串")
        if type(is_superuser) is not bool:
            raise TypeError("llm_tools is_superuser 必须是布尔值")
        legacy_view = self._legacy_llm_tool_execution_view(tool_name)
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_llm_tools_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("llm_tools Provider cutover 开关必须是布尔值")

        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not provider_cutover
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return legacy_view

        item = provider_catalog.tools.get(tool_name)
        if item is None:
            if legacy_view is not None:
                raise ProviderConsumerParityError(
                    f"llm_tools Provider identity 缺失: {tool_name}"
                )
            return None
        if legacy_view is None:
            raise ProviderConsumerParityError(
                f"llm_tools legacy rollback identity 缺失: {tool_name}"
            )

        route = (
            LlmToolExecutionRoute.BUILTIN_SEARCH
            if item.source is ToolSource.BUILTIN
            else LlmToolExecutionRoute.NONEBOT_PLUGIN
            if item.source is ToolSource.NONEBOT_PLUGIN
            else LlmToolExecutionRoute.CUSTOM_TOOL
        )
        if item.source is ToolSource.BUILTIN and tool_name != WEB_SEARCH_TOOL_SPEC.name:
            raise ProviderConsumerParityError(
                f"llm_tools 未知 builtin 执行工具: {tool_name}"
            )
        spec_matches = legacy_view.spec is item.spec
        if (
            item.source is ToolSource.MCP
            and legacy_view.spec is None
            and legacy_view.legacy_entry is not None
        ):
            entry = legacy_view.legacy_entry
            spec_matches = (
                entry.get("name", tool_name) == tool_name
                and entry.get("func") is item.spec.handler
                and entry.get("description") == item.spec.description
                and mutable_value(entry.get("parameters"))
                == mutable_value(item.spec.parameters)
            )
        if (
            legacy_view.route is not route
            or legacy_view.source is not item.source
            or not spec_matches
        ):
            raise ProviderConsumerParityError(
                f"llm_tools Provider 执行视图与 legacy rollback view 不一致: {tool_name}"
            )

        decision = provider_catalog.decide_trust(
            tool_name,
            ToolTrustOperation.EXECUTION,
            is_superuser=is_superuser,
            confirmed=False,
        )
        return LlmToolExecutionView(
            tool_name=tool_name,
            generation=self.generation,
            route=route,
            source=item.source,
            spec=item.spec,
            legacy_entry=legacy_view.legacy_entry,
            provider_authoritative=True,
            trust_decision=decision,
        )

    def _legacy_pending_action_execution_view(
        self,
        tool_name: str,
    ) -> PendingActionExecutionView | None:
        legacy_entry = self.custom_tools.get(tool_name)
        if legacy_entry is None:
            return None
        raw_source = legacy_entry.get("source")
        source = (
            {
                "registered": ToolSource.REGISTERED,
                "custom_file": ToolSource.CUSTOM_FILE,
                "generated": ToolSource.GENERATED,
                "mcp": ToolSource.MCP,
            }.get(raw_source)
            if isinstance(raw_source, str)
            else None
        )
        if tool_name in self.mcp_tool_names:
            source = ToolSource.MCP
        spec = legacy_entry.get("tool_spec")
        bundle_digest = legacy_entry.get("bundle_digest")
        if bundle_digest is not None and not isinstance(bundle_digest, str):
            raise ProviderConsumerParityError(
                f"PendingAction legacy bundle digest 非法: {tool_name}"
            )
        return PendingActionExecutionView(
            tool_name=tool_name,
            generation=self.generation,
            source=source,
            spec=spec if isinstance(spec, ToolSpec) else None,
            execution_entry=legacy_entry,
            legacy_entry=legacy_entry,
            bundle_digest=bundle_digest,
            provider_authoritative=False,
        )

    def resolve_pending_action_execution(
        self,
        tool_name: str,
        *,
        is_superuser: bool,
        provider_cutover: bool | None = None,
    ) -> PendingActionExecutionView | None:
        """Resolve a confirmed mutating call with Provider/legacy parity."""

        if not isinstance(tool_name, str) or not tool_name:
            raise TypeError("PendingAction tool_name 必须是非空字符串")
        if type(is_superuser) is not bool:
            raise TypeError("PendingAction is_superuser 必须是布尔值")
        legacy_view = self._legacy_pending_action_execution_view(tool_name)
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_pending_actions_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("PendingAction Provider cutover 开关必须是布尔值")

        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not provider_cutover
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return legacy_view

        item = provider_catalog.tools.get(tool_name)
        if item is None:
            if legacy_view is not None:
                raise ProviderConsumerParityError(
                    f"PendingAction Provider identity 缺失: {tool_name}"
                )
            return None
        if item.source not in {
            ToolSource.REGISTERED,
            ToolSource.CUSTOM_FILE,
            ToolSource.GENERATED,
            ToolSource.MCP,
        }:
            raise ProviderConsumerParityError(
                f"PendingAction Provider source 不可确认执行: {tool_name}"
            )
        if legacy_view is None:
            raise ProviderConsumerParityError(
                f"PendingAction legacy rollback identity 缺失: {tool_name}"
            )

        spec_matches = legacy_view.spec is item.spec
        if item.source is ToolSource.MCP and legacy_view.spec is None:
            entry = legacy_view.legacy_entry
            spec_matches = (
                entry.get("name", tool_name) == tool_name
                and entry.get("func") is item.spec.handler
                and entry.get("description") == item.spec.description
                and mutable_value(entry.get("parameters"))
                == mutable_value(item.spec.parameters)
            )
        provider_bundle_digest = (
            item.artifact.bundle_digest if item.artifact is not None else None
        )
        if (
            legacy_view.source is not item.source
            or not spec_matches
            or legacy_view.bundle_digest != provider_bundle_digest
        ):
            raise ProviderConsumerParityError(
                "PendingAction Provider 执行视图与 legacy rollback view 不一致: "
                f"{tool_name}"
            )

        decision = provider_catalog.decide_trust(
            tool_name,
            ToolTrustOperation.EXECUTION,
            is_superuser=is_superuser,
            confirmed=True,
        )
        return PendingActionExecutionView(
            tool_name=tool_name,
            generation=self.generation,
            source=item.source,
            spec=item.spec,
            execution_entry=immutable_mapping(item.spec.as_legacy_schema()),
            legacy_entry=legacy_view.legacy_entry,
            bundle_digest=provider_bundle_digest,
            provider_authoritative=True,
            trust_decision=decision,
        )

    def _legacy_search_extractor_view(self) -> SearchExtractorView | None:
        tool_name = "extract_webpage"
        legacy_entry = self.custom_tools.get(tool_name)
        if legacy_entry is None:
            return None
        raw_source = legacy_entry.get("source")
        source = (
            {
                "registered": ToolSource.REGISTERED,
                "custom_file": ToolSource.CUSTOM_FILE,
                "generated": ToolSource.GENERATED,
                "mcp": ToolSource.MCP,
            }.get(raw_source)
            if isinstance(raw_source, str)
            else None
        )
        if tool_name in self.mcp_tool_names:
            source = ToolSource.MCP
        spec = legacy_entry.get("tool_spec")
        return SearchExtractorView(
            tool_name=tool_name,
            generation=self.generation,
            source=source,
            spec=spec if isinstance(spec, ToolSpec) else None,
            legacy_entry=legacy_entry,
            provider_authoritative=False,
        )

    def resolve_search_extractor(
        self,
        *,
        is_superuser: bool,
        provider_cutover: bool | None = None,
    ) -> SearchExtractorView | None:
        """Resolve the optional webpage extractor with Provider parity."""

        if type(is_superuser) is not bool:
            raise TypeError("Search extractor is_superuser 必须是布尔值")
        tool_name = "extract_webpage"
        legacy_view = self._legacy_search_extractor_view()
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_search_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("Search Provider cutover 开关必须是布尔值")

        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not provider_cutover
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return legacy_view

        item = provider_catalog.tools.get(tool_name)
        if item is None:
            if legacy_view is not None:
                raise ProviderConsumerParityError(
                    "Search extractor Provider identity 缺失"
                )
            return None
        if item.source not in {
            ToolSource.REGISTERED,
            ToolSource.CUSTOM_FILE,
            ToolSource.GENERATED,
            ToolSource.MCP,
        }:
            raise ProviderConsumerParityError(
                "Search extractor Provider source 非法"
            )
        if legacy_view is None:
            raise ProviderConsumerParityError(
                "Search extractor legacy rollback identity 缺失"
            )

        spec_matches = legacy_view.spec is item.spec
        if item.source is ToolSource.MCP and legacy_view.spec is None:
            entry = legacy_view.legacy_entry
            spec_matches = (
                entry.get("name", tool_name) == tool_name
                and entry.get("func") is item.spec.handler
                and entry.get("description") == item.spec.description
                and mutable_value(entry.get("parameters"))
                == mutable_value(item.spec.parameters)
            )
        if legacy_view.source is not item.source or not spec_matches:
            raise ProviderConsumerParityError(
                "Search extractor Provider 选择视图与 legacy rollback view 不一致"
            )

        decision = provider_catalog.decide_trust(
            tool_name,
            ToolTrustOperation.SELECTION,
            is_superuser=is_superuser,
        )
        return SearchExtractorView(
            tool_name=tool_name,
            generation=self.generation,
            source=item.source,
            spec=item.spec,
            legacy_entry=legacy_view.legacy_entry,
            provider_authoritative=True,
            trust_decision=decision,
        )

    @staticmethod
    def _management_label_for_source(source: ToolSource) -> str:
        if source is ToolSource.BUILTIN:
            return "联网搜索工具"
        if source is ToolSource.NONEBOT_PLUGIN:
            return "NoneBot 插件"
        if source is ToolSource.MCP:
            return "MCP 工具"
        return "自定义函数工具"

    @staticmethod
    def _mcp_service_token(identifier: str) -> str | None:
        if not identifier.startswith("mcp__"):
            return None
        suffix = identifier.removeprefix("mcp__")
        if identifier.endswith("__*"):
            token = suffix.removesuffix("__*")
        elif "__" not in suffix:
            token = suffix
        else:
            return None
        return token or None

    def _legacy_management_plugin_names(self) -> frozenset[str]:
        if self.legacy_plugin_names is not None:
            return frozenset(self.legacy_plugin_names)
        return frozenset(
            plugin.name for plugin in nonebot.plugin.get_loaded_plugins()
        )

    def _legacy_management_mcp_server_identifiers(self) -> frozenset[str]:
        if self.mcp_server_identifiers is not None:
            return frozenset(self.mcp_server_identifiers)
        # Old/bootstrap snapshots did not freeze this sidecar.  Keep their
        # bounded rollback behavior; formal runtime candidates always publish
        # the generation-bound set.
        mcp_manager.load_config()
        return mcp_manager.configured_server_identifiers()

    def _legacy_tool_management_view(
        self,
        identifier: str,
    ) -> ToolManagementView | None:
        if identifier == WEB_SEARCH_TOOL_SPEC.name:
            return ToolManagementView(
                identifier=identifier,
                generation=self.generation,
                kind=ToolManagementTargetKind.EXACT_TOOL,
                label="联网搜索工具",
                source=ToolSource.BUILTIN,
                spec=WEB_SEARCH_TOOL_SPEC,
                legacy_entry=None,
                matched_tool_names=(identifier,),
                provider_authoritative=False,
            )

        if identifier in self._legacy_management_plugin_names():
            legacy_entry = self.plugin_info.get(identifier)
            raw_spec = (
                legacy_entry.get("tool_spec")
                if legacy_entry is not None
                else None
            )
            return ToolManagementView(
                identifier=identifier,
                generation=self.generation,
                kind=ToolManagementTargetKind.EXACT_TOOL,
                label="NoneBot 插件",
                source=ToolSource.NONEBOT_PLUGIN,
                spec=raw_spec if isinstance(raw_spec, ToolSpec) else None,
                legacy_entry=legacy_entry,
                matched_tool_names=(identifier,),
                provider_authoritative=False,
            )

        legacy_entry = self.custom_tools.get(identifier)
        if legacy_entry is not None:
            raw_source = legacy_entry.get("source")
            source = (
                {
                    "registered": ToolSource.REGISTERED,
                    "custom_file": ToolSource.CUSTOM_FILE,
                    "generated": ToolSource.GENERATED,
                    "mcp": ToolSource.MCP,
                }.get(raw_source)
                if isinstance(raw_source, str)
                else None
            )
            if identifier in self.mcp_tool_names:
                source = ToolSource.MCP
            raw_spec = legacy_entry.get("tool_spec")
            return ToolManagementView(
                identifier=identifier,
                generation=self.generation,
                kind=ToolManagementTargetKind.EXACT_TOOL,
                label=(
                    "MCP 工具"
                    if source is ToolSource.MCP
                    else "自定义函数工具"
                ),
                source=source,
                spec=raw_spec if isinstance(raw_spec, ToolSpec) else None,
                legacy_entry=legacy_entry,
                matched_tool_names=(identifier,),
                provider_authoritative=False,
            )

        server_token = self._mcp_service_token(identifier)
        if server_token is None:
            return None
        prefix = f"mcp__{server_token}__"
        matched_tool_names = tuple(
            sorted(
                name
                for name in self.mcp_tool_names
                if name.startswith(prefix)
            )
        )
        if (
            server_token
            not in self._legacy_management_mcp_server_identifiers()
            and not matched_tool_names
        ):
            return None
        return ToolManagementView(
            identifier=identifier,
            generation=self.generation,
            kind=ToolManagementTargetKind.MCP_SERVICE,
            label="MCP 服务",
            source=ToolSource.MCP,
            spec=None,
            legacy_entry=None,
            matched_tool_names=matched_tool_names,
            provider_authoritative=False,
        )

    @staticmethod
    def _management_exact_identity_matches(
        legacy_view: ToolManagementView,
        item: DiscoveredTool,
    ) -> bool:
        if (
            legacy_view.kind is not ToolManagementTargetKind.EXACT_TOOL
            or legacy_view.identifier != item.spec.name
            or legacy_view.source is not item.source
            or legacy_view.label
            != ToolSnapshot._management_label_for_source(item.source)
        ):
            return False
        if legacy_view.spec is item.spec:
            return True
        if (
            item.source is not ToolSource.MCP
            or legacy_view.spec is not None
            or legacy_view.legacy_entry is None
        ):
            return False
        entry = legacy_view.legacy_entry
        return (
            entry.get("name", item.spec.name) == item.spec.name
            and entry.get("func") is item.spec.handler
            and entry.get("description") == item.spec.description
            and mutable_value(entry.get("parameters"))
            == mutable_value(item.spec.parameters)
        )

    def resolve_tool_management(
        self,
        identifier: str,
        *,
        is_superuser: bool,
        provider_cutover: bool | None = None,
    ) -> ToolManagementView | None:
        """Resolve one blacklist-add target with Provider/legacy parity."""

        identifier = str(identifier or "").strip()
        if not identifier:
            return None
        if type(is_superuser) is not bool:
            raise TypeError("工具管理 is_superuser 必须是布尔值")
        legacy_view = self._legacy_tool_management_view(identifier)
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_management_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("工具管理 Provider cutover 开关必须是布尔值")

        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not provider_cutover
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return legacy_view

        item = provider_catalog.tools.get(identifier)
        if item is not None:
            if legacy_view is None:
                raise ProviderConsumerParityError(
                    f"工具管理 legacy rollback identity 缺失: {identifier}"
                )
            if not self._management_exact_identity_matches(legacy_view, item):
                raise ProviderConsumerParityError(
                    "工具管理 Provider identity 与 legacy rollback view 不一致: "
                    f"{identifier}"
                )
            decision = provider_catalog.decide_trust(
                identifier,
                ToolTrustOperation.MANAGEMENT,
                is_superuser=is_superuser,
            )
            return ToolManagementView(
                identifier=identifier,
                generation=self.generation,
                kind=ToolManagementTargetKind.EXACT_TOOL,
                label=self._management_label_for_source(item.source),
                source=item.source,
                spec=item.spec,
                legacy_entry=legacy_view.legacy_entry,
                matched_tool_names=(identifier,),
                provider_authoritative=True,
                trust_decisions=(decision,),
            )

        server_token = self._mcp_service_token(identifier)
        if server_token is not None:
            prefix = f"mcp__{server_token}__"
            provider_names = tuple(
                sorted(
                    name
                    for name, provider_item in provider_catalog.tools.items()
                    if provider_item.source is ToolSource.MCP
                    and name.startswith(prefix)
                )
            )
            if legacy_view is None:
                if provider_names:
                    raise ProviderConsumerParityError(
                        "工具管理 MCP Provider selector 缺少 legacy rollback view: "
                        f"{identifier}"
                    )
                return None
            if (
                legacy_view.kind is not ToolManagementTargetKind.MCP_SERVICE
                or legacy_view.matched_tool_names != provider_names
            ):
                raise ProviderConsumerParityError(
                    "工具管理 MCP Provider selector 与 legacy rollback view "
                    f"不一致: {identifier}"
                )

            decisions: list[ToolTrustDecision] = []
            for tool_name in provider_names:
                provider_item = provider_catalog.tools[tool_name]
                legacy_member = self._legacy_tool_management_view(tool_name)
                if legacy_member is None or not self._management_exact_identity_matches(
                    legacy_member,
                    provider_item,
                ):
                    raise ProviderConsumerParityError(
                        "工具管理 MCP selector member 与 legacy rollback view "
                        f"不一致: {tool_name}"
                    )
                decisions.append(
                    provider_catalog.decide_trust(
                        tool_name,
                        ToolTrustOperation.MANAGEMENT,
                        is_superuser=is_superuser,
                    )
                )
            return ToolManagementView(
                identifier=identifier,
                generation=self.generation,
                kind=ToolManagementTargetKind.MCP_SERVICE,
                label="MCP 服务",
                source=ToolSource.MCP,
                spec=None,
                legacy_entry=None,
                matched_tool_names=provider_names,
                provider_authoritative=True,
                trust_decisions=tuple(decisions),
                selector_allowed=is_superuser,
                selector_reason=(
                    None if is_superuser else "工具管理只允许超级用户"
                ),
            )

        if legacy_view is not None:
            raise ProviderConsumerParityError(
                f"工具管理 Provider identity 缺失: {identifier}"
            )
        return None

    def get_brief_catalog(
        self,
        *,
        is_superuser: bool = False,
        provider_cutover: bool | None = None,
    ) -> str:
        legacy_catalog = ToolManager.build_brief_catalog(
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            mcp_tool_names=self.mcp_tool_names,
            is_superuser=is_superuser,
        )
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_categorize_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("categorize Provider cutover 开关必须是布尔值")
        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not provider_cutover
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return legacy_catalog
        catalog = ToolManager.build_provider_brief_catalog(
            provider_catalog=provider_catalog,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            is_superuser=is_superuser,
        )
        if catalog != legacy_catalog:
            raise ProviderConsumerParityError(
                "categorize Provider catalog 与 legacy rollback view 不一致"
            )
        return catalog

    def capture_brief_catalog_context(
        self,
        *,
        is_superuser: bool = False,
        provider_cutover: bool | None = None,
    ) -> ToolCatalogRenderContext:
        """Capture all dynamic catalog inputs before cache lookup or rendering."""

        if type(is_superuser) is not bool:
            raise TypeError("categorize is_superuser 必须是布尔值")
        if provider_cutover is None:
            from .config import config_parser

            provider_cutover = config_parser.get_config(
                "provider_catalog_categorize_enabled",
                True,
            )
        if type(provider_cutover) is not bool:
            raise ValueError("categorize Provider cutover 开关必须是布尔值")
        return ToolCatalogRenderContext.capture(
            generation=self.generation,
            is_superuser=is_superuser,
            provider_cutover=provider_cutover,
            tools_enabled=model_selector.get_use_tools(),
            web_search_enabled=model_selector.get_web_search(),
            blacklist_patterns=tuple(model_selector.get_tool_blacklist() or ()),
        )

    def build_brief_catalog_record(
        self,
        context: ToolCatalogRenderContext,
    ) -> ToolCatalogRecord:
        """Render and parity-check one explicit context before making it cacheable."""

        if not isinstance(context, ToolCatalogRenderContext):
            raise TypeError("context 必须是 ToolCatalogRenderContext")
        if context.generation != self.generation:
            raise ValueError("tool catalog context generation 与 ToolSnapshot 不一致")
        legacy_catalog = ToolManager.build_brief_catalog(
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            mcp_tool_names=self.mcp_tool_names,
            is_superuser=context.is_superuser,
            render_context=context,
        )
        provider_catalog = self.provider_catalog
        assert provider_catalog is not None
        if (
            not context.provider_cutover
            or provider_catalog.schema_version < 3
            or not _PROVIDER_CONSUMER_IDS.issubset(
                provider_catalog.registrations
            )
        ):
            return ToolCatalogRecord(context.cache_key, legacy_catalog)
        catalog = ToolManager.build_provider_brief_catalog(
            provider_catalog=provider_catalog,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            is_superuser=context.is_superuser,
            render_context=context,
        )
        if catalog != legacy_catalog:
            raise ProviderConsumerParityError(
                "categorize Provider catalog 与 legacy rollback view 不一致"
            )
        return ToolCatalogRecord(context.cache_key, catalog)

    def get_brief_catalog_record(
        self,
        *,
        is_superuser: bool = False,
        provider_cutover: bool | None = None,
    ) -> ToolCatalogRecord:
        context = self.capture_brief_catalog_context(
            is_superuser=is_superuser,
            provider_cutover=provider_cutover,
        )
        return self.build_brief_catalog_record(context)


class ToolManager:
    def __init__(self):
        self.plugin_info = {}
        self.custom_tools = {}  # 存储自定义普通函数: name -> dict

        # 初始化自定义插件说明的配置路径
        self.custom_info_file = Path(config_path / "custom_plugin_info.json")
        # 初始化自定义函数的文件夹路径
        self.custom_tools_dir = Path(config_path / "custom_tools")

        self._init_files()
        self.tool_dependencies = {}
        self.mcp_tool_names = set()
        self.load_custom_tools()

    def _init_files(self):
        """初始化配置文件和文件夹，并生成模板以供用户参考"""
        # 1. 生成自定义插件描述模板
        if not self.custom_info_file.exists():
            default_info = {
                "_comment": "键名必须是你想修改的 nonebot 插件的真实包名（比如 nonebot_plugin_tarot）",
                "nonebot_plugin_example": {
                    "name": "示例插件名称",
                    "description": "详细描述该插件的功能，告诉大模型在什么场景下应该调用它。",
                    "usage": "严格写明该插件的触发指令格式。例如：发送'塔罗牌'或'抽牌'",
                    "dependencies": [
                        "可选：需要一并注入的工具标识，例如 mcp__danbooru_searcher__search_tags"
                    ],
                },
            }
            with open(self.custom_info_file, "w", encoding="utf-8") as f:
                json.dump(default_info, f, ensure_ascii=False, indent=4)

        # 2. 生成自定义函数文件夹及代码模板
        is_first_time_dir = not self.custom_tools_dir.exists()
        self.custom_tools_dir.mkdir(parents=True, exist_ok=True)

        template_file = self.custom_tools_dir / "_example.py"
        # 如果是首次创建文件夹，则生成模板
        if is_first_time_dir:
            template_content = '''
"""
这是一个自定义大模型工具（Function Calling）的示例文件。
你可以参考此模板，在此目录下编写自己的原生 Python 函数。

【编写规范】
1. 零依赖：不需要导入 nonebot 或任何插件依赖，纯 Python 原生写法。
2. 异步函数：工具函数必须是 `async def` 定义的异步函数。
3. 工具描述：将函数的主要用途写在 `docstring`（三重引号注释）中，大模型会据此判断何时调用该工具。
4. 参数描述：引入 Python 原生的 `typing.Annotated`，格式为 `参数名: Annotated[类型, "参数说明"]`，以便大模型准确提取参数。
5. 返回值：最好返回字符串（str），大模型会直接读取此返回结果。
6. 多工具支持：你可以在同一个 .py 脚本中编写多个异步函数，插件会自动扫描并全部加载为独立工具，无需分拆文件。

【生效方式】
编写或修改完后，在群聊中发送管理员指令：`/刷新工具` 或 `/重载工具` 即可即时生效！
"""

import re
import datetime
import aiohttp
from typing import Annotated

# ==========================================
# 示例 1：无参数的工具
# ==========================================
async def get_current_datetime() -> str:
    """
    获取当前的系统日期、时间和星期几。
    当用户询问现在几点、今天几号、今天星期几等与当前时间相关的问题时，调用此工具获取准确时间。
    """
    try:
        now = datetime.datetime.now()
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday_str = weekdays[now.weekday()]

        formatted = now.strftime(f'%Y年%m月%d日 %H:%M:%S {weekday_str}')
        return f"当前系统时间是: {formatted}"
    except Exception as e:
        return f"获取时间失败: {str(e)}"

# ==========================================
# 示例 2：带参数的工具
# ==========================================
# 【依赖拓扑声明】
# 键为“触发条件”，值为“需要一并注入的工具列表”
# 表示：当大模型被分配了 web_search 工具时，强制将本脚本中的 extract_webpage 工具也提供给它。
TOOL_DEPENDENCIES = {
    "web_search": ["extract_webpage"]
}
async def extract_webpage(
    url: Annotated[str, "需要提取的完整网页链接，必须包含 http:// 或 https://"]
) -> str:
    """
    读取并提取指定URL网页的正文内容。
    当需要深入了解搜索结果中的链接，或用户要求分析某个网页时调用。
    """
    if not url.startswith(("http://", "https://")):
        return "提取失败：请提供有效的URL（以http://或https://开头）"

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MoEllmChats custom tool example)"
    }
    timeout = aiohttp.ClientTimeout(total=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return f"提取失败：网页返回状态码 {response.status}"
                html = await response.text()

        # 使用正则移除 script 和 style 标签及其内容
        text = re.sub(r'<(script|style).*?>.*?</\1>', '', html, flags=re.IGNORECASE | re.DOTALL)
        # 移除所有剩余的 HTML 标签
        text = re.sub(r'<[^>]+>', ' ', text)

        # 清理多余的空白符和空行
        text = re.sub(r'\\n\\s*\\n', '\\n', text).strip()
        text = re.sub(r' {2,}', ' ', text)

        max_length = 4000
        if len(text) > max_length:
            text = text[:max_length] + "\\n\\n...[由于内容过长，为防止上下文超出限制，已自动截断]"

        return f"网页提取成功，以下是内容摘要：\\n{text}"
    except Exception as e:
        return f"提取网页失败，发生错误：{str(e)}"
'''
            with open(template_file, "w", encoding="utf-8") as f:
                f.write(template_content)

    def load_file_tools_candidate(
        self,
        *,
        generation: int = 0,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        """Build one file-tool candidate for legacy and Provider dual views."""

        return load_file_tools(
            self.custom_tools_dir.glob("*.py"),
            generation=generation,
        )

    @staticmethod
    def load_generated_tools_candidate(
        *,
        generation: int = 0,
        generated_state=None,
        generated_source_overrides=None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        generated_load_kwargs = {"generation": generation}
        if generated_state is not None or generated_source_overrides is not None:
            generated_load_kwargs.update(
                generated_state=generated_state,
                generated_source_overrides=generated_source_overrides,
            )
        return generated_tool_store.load_active_tools(**generated_load_kwargs)

    @staticmethod
    def _copy_file_tool_candidate(
        candidate: object,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        if not isinstance(candidate, tuple) or len(candidate) != 2:
            raise TypeError("file_tool_candidate 必须是 tools/dependencies 元组")
        raw_tools, raw_dependencies = candidate
        if not isinstance(raw_tools, Mapping) or not isinstance(
            raw_dependencies,
            Mapping,
        ):
            raise TypeError("file_tool_candidate 必须包含两个映射")

        tools: dict[str, dict[str, Any]] = {}
        for name, schema in raw_tools.items():
            if not isinstance(name, str) or not isinstance(schema, Mapping):
                raise TypeError("file_tool_candidate tools 结构非法")
            tools[name] = dict(schema)
        dependencies: dict[str, set[str]] = {}
        for name, items in raw_dependencies.items():
            if (
                not isinstance(name, str)
                or not isinstance(items, AbstractSet)
                or not all(isinstance(item, str) for item in items)
            ):
                raise TypeError("file_tool_candidate dependencies 结构非法")
            dependencies[name] = set(items)
        return tools, dependencies

    @staticmethod
    def _copy_generated_tool_candidate(
        candidate: object,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
        if not isinstance(candidate, tuple) or len(candidate) != 2:
            raise TypeError(
                "generated_tool_candidate 必须是 tools/dependencies 元组"
            )
        raw_tools, raw_dependencies = candidate
        if not isinstance(raw_tools, Mapping) or not isinstance(
            raw_dependencies,
            Mapping,
        ):
            raise TypeError("generated_tool_candidate 必须包含两个映射")

        tools: dict[str, dict[str, Any]] = {}
        for name, schema in raw_tools.items():
            if not isinstance(name, str) or not isinstance(schema, Mapping):
                raise TypeError("generated_tool_candidate tools 结构非法")
            tools[name] = dict(schema)
        dependencies: dict[str, set[str]] = {}
        for name, items in raw_dependencies.items():
            if (
                not isinstance(name, str)
                or not isinstance(items, AbstractSet)
                or not all(isinstance(item, str) for item in items)
            ):
                raise TypeError(
                    "generated_tool_candidate dependencies 结构非法"
                )
            dependencies[name] = set(items)
        return tools, dependencies

    def load_custom_tools(
        self,
        *,
        commit: bool = True,
        generation: int = 0,
        generated_state=None,
        generated_source_overrides=None,
        registered_tools: Mapping[str, ToolSpec] | None = None,
        registered_discovery: tuple[DiscoveredTool, ...] | None = None,
        file_tool_candidate: _FileToolCandidate | None = None,
        file_discovery: tuple[DiscoveredTool, ...] | None = None,
        generated_tool_candidate: _GeneratedToolCandidate | None = None,
        generated_discovery: tuple[DiscoveredTool, ...] | None = None,
    ):
        """Parse file tools without importing them into the NoneBot process."""
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
        ):
            raise ValueError("generation 必须是非负整数")
        if registered_tools is None:
            registered_tools = tool_registry.snapshot()
        elif not isinstance(registered_tools, Mapping):
            raise TypeError("registered_tools 必须是 ToolSpec 映射")
        registered_tools = dict(registered_tools)
        if any(
            not isinstance(name, str)
            or not isinstance(spec, ToolSpec)
            or name != spec.name
            for name, spec in registered_tools.items()
        ):
            raise ValueError("registered_tools 必须按精确工具名映射 ToolSpec")
        new_tools = {
            name: {**spec.as_legacy_schema(), "source": "registered"}
            for name, spec in registered_tools.items()
        }
        # 每次重载前清空旧的依赖，防止热重载时叠加死循环
        new_dependencies = {
            name: set(spec.dependencies)
            for name, spec in registered_tools.items()
            if spec.dependencies
        }
        if registered_discovery is not None:
            registered_tool_provider.validate_legacy_parity(
                registered_discovery,
                new_tools,
                new_dependencies,
                generation=generation,
            )
        candidate_was_provided = file_tool_candidate is not None
        if file_tool_candidate is None:
            file_tool_candidate = self.load_file_tools_candidate(
                generation=generation
            )
        file_tools, file_dependencies = self._copy_file_tool_candidate(
            file_tool_candidate
        )
        if file_discovery is not None:
            if not candidate_was_provided:
                raise ValueError(
                    "file discovery 必须复用显式 transaction file candidate"
                )
            file_tool_provider.validate_legacy_parity(
                file_discovery,
                file_tools,
                file_dependencies,
                generation=generation,
                allow_additional_dependencies=True,
            )
        self._merge_unique_tools(new_tools, file_tools)
        for trigger, dependencies in file_dependencies.items():
            new_dependencies.setdefault(trigger, set()).update(dependencies)
        generated_candidate_was_provided = generated_tool_candidate is not None
        if generated_tool_candidate is None:
            generated_tool_candidate = self.load_generated_tools_candidate(
                generation=generation,
                generated_state=generated_state,
                generated_source_overrides=generated_source_overrides,
            )
        generated_tools, generated_dependencies = (
            self._copy_generated_tool_candidate(generated_tool_candidate)
        )
        if generated_discovery is not None:
            if not generated_candidate_was_provided:
                raise ValueError(
                    "generated discovery 必须复用显式 transaction candidate"
                )
            generated_tool_provider.validate_legacy_parity(
                generated_discovery,
                generated_tools,
                generated_dependencies,
                generation=generation,
                allow_additional_dependencies=True,
            )
        self._merge_unique_tools(new_tools, generated_tools)
        for trigger, dependencies in generated_dependencies.items():
            new_dependencies.setdefault(trigger, set()).update(dependencies)
        self._merge_dependencies_from_custom_plugin_info(new_dependencies)
        logger.debug(f"最终的工具依赖拓扑: {new_dependencies}")
        if commit:
            self.custom_tools = new_tools
            self.tool_dependencies = new_dependencies
        logger.debug(f"最终加载的自定义工具: {list(self.custom_tools.keys())}")
        return 0 if commit else (new_tools, new_dependencies)

    @staticmethod
    def _merge_unique_tools(target: dict, incoming: dict) -> None:
        for name, schema in incoming.items():
            if name in target:
                old_source = target[name].get("source", "unknown")
                new_source = schema.get("source", "unknown")
                raise ValueError(
                    f"工具名冲突: {name} ({old_source} vs {new_source})"
                )
            target[name] = schema

    @staticmethod
    def validate_dependencies(dependencies: dict, known_tools: set[str]) -> None:
        known = set(known_tools) | {
            spec.name for spec in builtin_tool_specs()
        }
        for trigger, items in dependencies.items():
            if trigger not in known:
                # custom_plugin_info.json may describe an optional plugin that is
                # not installed in this generation; it cannot be selected anyway.
                continue
            missing = sorted(set(items) - known)
            if missing:
                raise ValueError(f"工具 {trigger} 引用了不存在的依赖: {missing}")

    @staticmethod
    def validate_tool_schemas(tools: dict) -> None:
        for name, schema in tools.items():
            if not isinstance(schema, dict):
                raise ValueError(f"工具 {name} Schema 必须是对象")
            if schema.get("name") != name:
                raise ValueError(f"工具 {name} Schema 名称不一致")
            description = schema.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"工具 {name} description 不能为空")
            validate_parameters_schema(schema.get("parameters"))
            if not callable(schema.get("func")):
                raise ValueError(f"工具 {name} handler 必须可调用")
            source = schema.get("source")
            if source not in {"custom_file", "generated"}:
                continue
            artifact = schema.get("tool_artifact")
            if not isinstance(artifact, ToolArtifact):
                raise ValueError(f"工具 {name} 缺少 ToolArtifact")
            generation = schema.get("generation")
            if generation != artifact.generation:
                raise ValueError(f"工具 {name} generation 与 ToolArtifact 不一致")
            if schema.get("artifact_digest") != artifact.artifact_digest:
                raise ValueError(f"工具 {name} artifact digest 不一致")
            if schema.get("tool_spec") is not artifact.spec:
                raise ValueError(f"工具 {name} ToolSpec 与 ToolArtifact 不一致")
            if schema.get("func") is not artifact.spec.handler:
                raise ValueError(f"工具 {name} handler 与 ToolArtifact 不一致")
            policy = artifact.spec.policy
            if policy is None:
                raise ValueError(f"工具 {name} 缺少 capability policy")
            expected_capability_fields = {}
            if artifact.artifact_version == 2:
                expected_capability_fields = {
                    "artifact_digest_version": artifact.artifact_version,
                    "tool_contract_version": (
                        artifact.contract.contract_version
                    ),
                    "requested_capabilities": (
                        artifact.contract.requested_capabilities
                    ),
                    "detected_capabilities": (
                        artifact.contract.detected_capabilities
                    ),
                    "admin_capabilities": artifact.contract.admin_capabilities,
                    "effective_capabilities": (
                        artifact.contract.effective_capabilities
                    ),
                    "capability_policy": policy.capability_contract(),
                }
            elif source == "generated":
                expected_capability_fields = {
                    "requested_capabilities": (
                        artifact.contract.requested_capabilities
                    ),
                    "effective_capabilities": (
                        artifact.contract.effective_capabilities
                    ),
                }
                unexpected = {
                    "artifact_digest_version",
                    "tool_contract_version",
                    "detected_capabilities",
                    "admin_capabilities",
                    "capability_policy",
                } & set(schema)
                if unexpected:
                    raise ValueError(
                        f"工具 {name} v1 Schema 混入 v2 字段: {sorted(unexpected)}"
                    )
            else:
                unexpected = {
                    "artifact_digest_version",
                    "tool_contract_version",
                    "requested_capabilities",
                    "detected_capabilities",
                    "admin_capabilities",
                    "effective_capabilities",
                    "capability_policy",
                } & set(schema)
                if unexpected:
                    raise ValueError(
                        f"工具 {name} v1 Schema 混入 v2 字段: {sorted(unexpected)}"
                    )
            for field_name, expected in expected_capability_fields.items():
                if schema.get(field_name) != expected:
                    raise ValueError(f"工具 {name} {field_name} 不一致")
            bundle_digest = schema.get("bundle_digest") if source == "generated" else None
            artifact.verify(
                expected_artifact_digest=artifact.artifact_digest,
                expected_bundle_digest=bundle_digest,
                generation=generation,
            )

    def expand_dependencies(self, plugins: set) -> set:
        """
        展开工具依赖关系，确保多步任务所需的伴生工具被一并注入。
        同时过滤黑名单，避免依赖工具绕过黑名单。
        """
        expanded = {p for p in plugins if not self.is_tool_blacklisted(p)}
        queue = deque(expanded)

        while queue:
            current = queue.popleft()

            if current in self.tool_dependencies:
                for dep in self.tool_dependencies[current]:
                    if self.is_tool_blacklisted(dep):
                        logger.debug(f"依赖工具 [{dep}] 已被黑名单禁用，跳过注入")
                        continue

                    if dep not in expanded:
                        logger.debug(
                            f"尝试注入依赖 [{dep}]。存在性检查 custom_tools: "
                            f"{dep in getattr(self, 'custom_tools', {})}, "
                            f"plugin_info: {dep in getattr(self, 'plugin_info', {})}"
                        )

                        if dep in getattr(self, "custom_tools", {}) or dep in getattr(
                            self, "plugin_info", {}
                        ):
                            expanded.add(dep)
                            queue.append(dep)

        logger.debug(f"收到初始插件集合: {plugins}，依赖展开后: {expanded}")
        return expanded

    @staticmethod
    def expand_provider_dependencies(
        *,
        provider_catalog: ProviderCatalogSnapshot,
        plugin_names: AbstractSet[str],
    ) -> set[str]:
        """Expand canonical ToolSpec dependencies for the payload consumer."""

        if not isinstance(provider_catalog, ProviderCatalogSnapshot):
            raise TypeError("llm_payload provider_catalog 非法")
        if not isinstance(plugin_names, AbstractSet) or not all(
            isinstance(name, str) for name in plugin_names
        ):
            raise TypeError("llm_payload plugin_names 必须是字符串集合")

        expanded = {
            name
            for name in plugin_names
            if not tool_manager.is_tool_blacklisted(name)
        }
        queue = deque(expanded)
        while queue:
            current = queue.popleft()
            item = provider_catalog.tools.get(current)
            # A stale resident entry or a classifier hallucination was ignored by
            # the legacy payload. Keep that bounded behavior, while refusing any
            # legacy-only dependency edge through the final parity comparison.
            if item is None:
                continue
            for dependency in item.spec.dependencies:
                if dependency not in provider_catalog.tools:
                    raise ProviderConsumerParityError(
                        "llm_payload Provider 依赖缺少 catalog 工具: "
                        f"{current} -> {dependency}"
                    )
                if tool_manager.is_tool_blacklisted(dependency):
                    continue
                if dependency not in expanded:
                    expanded.add(dependency)
                    queue.append(dependency)
        return expanded

    def build_plugin_info(self) -> dict:
        plugin_info = {}
        # 读取自定义插件描述
        custom_info = self._load_custom_plugin_info()

        for plugin in nonebot.plugin.get_loaded_plugins():
            if "saa" in plugin.name:
                continue

            info = None

            # 优先使用用户的自定义配置
            if plugin.name in custom_info:
                info = custom_info[plugin.name]
            elif plugin.metadata:
                info = {
                    "name": plugin.metadata.name,
                    "description": plugin.metadata.description,
                    "usage": plugin.metadata.usage,
                }

            if info:
                plugin_info[plugin.name] = info
        return plugin_info

    @staticmethod
    def loaded_plugin_names() -> frozenset[str]:
        """Capture the legacy management namespace for one reload candidate."""

        return frozenset(
            plugin.name for plugin in nonebot.plugin.get_loaded_plugins()
        )

    def refresh_plugins(self):
        self.plugin_info = build_nonebot_plugin_candidate(
            self.build_plugin_info()
        )[0]

    def snapshot(self) -> ToolSnapshot:
        from .runtime_snapshot import runtime_snapshots

        runtime_snapshot = runtime_snapshots.active()
        if runtime_snapshot is not None:
            return runtime_snapshot.tool_snapshot

        from .runtime_metrics import runtime_metrics

        return ToolSnapshot(
            generation=runtime_metrics.reload_generation,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            tool_dependencies=self.tool_dependencies,
            mcp_tool_names=self.mcp_tool_names,
        )

    def get_brief_catalog(self, *, is_superuser: bool = False) -> str:
        """
        给分类模型看的简版工具目录。
        注意：这里不要再调用 load_custom_tools()，否则会把已加载的 MCP 工具清掉。
        工具刷新统一交给 启动流程 / 刷新工具 命令 / 黑名单变更命令。
        """
        if not self.plugin_info:
            self.refresh_plugins()
        return self.snapshot().get_brief_catalog(is_superuser=is_superuser)

    @staticmethod
    def build_brief_catalog(
        *,
        plugin_info: Mapping[str, Mapping[str, Any]],
        custom_tools: Mapping[str, Mapping[str, Any]],
        mcp_tool_names: AbstractSet[str],
        is_superuser: bool = False,
        render_context: ToolCatalogRenderContext | None = None,
    ) -> str:
        catalog = []

        if render_context is None:
            tools_enabled = model_selector.get_use_tools()
            web_search_enabled = model_selector.get_web_search()
            is_blacklisted = tool_manager.is_tool_blacklisted
        else:
            if not isinstance(render_context, ToolCatalogRenderContext):
                raise TypeError("render_context 必须是 ToolCatalogRenderContext")
            if render_context.is_superuser is not is_superuser:
                raise ValueError("render_context permission 与 is_superuser 不一致")
            tools_enabled = render_context.tools_enabled
            web_search_enabled = render_context.web_search_enabled
            is_blacklisted = render_context.is_blacklisted

        if tools_enabled:
            # 1. NoneBot 原生插件
            for name, info in plugin_info.items():
                if is_blacklisted(name):
                    continue

                plugin_name = info.get("name") or name
                description = info.get("description") or "无描述"
                catalog.append(
                    f"- {name} | {plugin_name} | {str(description)[:160]}"
                )

            # 2. 自定义函数 + MCP 工具
            for name, info in custom_tools.items():
                if is_blacklisted(name):
                    continue
                if not ToolManager.is_tool_allowed(info, is_superuser=is_superuser):
                    continue

                tool_type = (
                    "MCP工具"
                    if name in mcp_tool_names
                    else "自定义函数"
                )

                description = info.get("description") or "无描述"

                catalog.append(
                    f"- {name} | {tool_type} | {str(description)[:160]}"
                )

        # 3. 联网搜索
        if web_search_enabled and not is_blacklisted(WEB_SEARCH_TOOL_SPEC.name):
            catalog.append(
                f"- {WEB_SEARCH_TOOL_SPEC.name} | 联网搜索 | "
                "回答实时问题、新闻、天气与近期信息"
            )

        return (
            "\n".join(catalog)
            if catalog
            else "当前工具调用与联网功能均已关闭，无需返回任何插件。"
        )

    @staticmethod
    def build_provider_brief_catalog(
        *,
        provider_catalog: ProviderCatalogSnapshot,
        plugin_info: Mapping[str, Mapping[str, Any]],
        custom_tools: Mapping[str, Mapping[str, Any]],
        is_superuser: bool = False,
        render_context: ToolCatalogRenderContext | None = None,
    ) -> str:
        """Build the categorize view from canonical Provider identities.

        Legacy mappings are retained only for stable presentation order and
        historical NoneBot display fields. Authorization, source identity,
        permission, and non-NoneBot descriptions come from the
        generation-bound Provider catalog.
        """

        if not isinstance(provider_catalog, ProviderCatalogSnapshot):
            raise TypeError("categorize provider_catalog 非法")
        if type(is_superuser) is not bool:
            raise TypeError("categorize is_superuser 必须是布尔值")
        if render_context is None:
            tools_enabled = model_selector.get_use_tools()
            web_search_enabled = model_selector.get_web_search()
            is_blacklisted = tool_manager.is_tool_blacklisted
        else:
            if not isinstance(render_context, ToolCatalogRenderContext):
                raise TypeError("render_context 必须是 ToolCatalogRenderContext")
            if render_context.generation != provider_catalog.generation:
                raise ValueError(
                    "render_context generation 与 Provider catalog 不一致"
                )
            if render_context.is_superuser is not is_superuser:
                raise ValueError("render_context permission 与 is_superuser 不一致")
            tools_enabled = render_context.tools_enabled
            web_search_enabled = render_context.web_search_enabled
            is_blacklisted = render_context.is_blacklisted
        catalog: list[str] = []

        if tools_enabled:
            for name, info in plugin_info.items():
                item = provider_catalog.tools.get(name)
                if item is None or item.source is not ToolSource.NONEBOT_PLUGIN:
                    raise ProviderConsumerParityError(
                        f"categorize NoneBot Provider identity 缺失: {name}"
                    )
                if is_blacklisted(name):
                    continue
                decision = provider_catalog.decide_trust(
                    name,
                    ToolTrustOperation.SELECTION,
                    is_superuser=is_superuser,
                )
                if not decision.allowed:
                    continue
                plugin_name = info.get("name") or name
                description = info.get("description") or "无描述"
                catalog.append(
                    f"- {name} | {plugin_name} | {str(description)[:160]}"
                )

            for name in custom_tools:
                item = provider_catalog.tools.get(name)
                if item is None or item.source in {
                    ToolSource.BUILTIN,
                    ToolSource.NONEBOT_PLUGIN,
                }:
                    raise ProviderConsumerParityError(
                        f"categorize Tool Provider identity 缺失: {name}"
                    )
                if is_blacklisted(name):
                    continue
                decision = provider_catalog.decide_trust(
                    name,
                    ToolTrustOperation.SELECTION,
                    is_superuser=is_superuser,
                )
                if not decision.allowed:
                    continue
                tool_type = (
                    "MCP工具"
                    if item.source is ToolSource.MCP
                    else "自定义函数"
                )
                catalog.append(
                    f"- {name} | {tool_type} | "
                    f"{item.spec.description[:160]}"
                )

        if web_search_enabled and not is_blacklisted(WEB_SEARCH_TOOL_SPEC.name):
            item = provider_catalog.tools.get(WEB_SEARCH_TOOL_SPEC.name)
            if item is None or item.source is not ToolSource.BUILTIN:
                raise ProviderConsumerParityError(
                    "categorize builtin web_search Provider identity 缺失"
                )
            decision = provider_catalog.decide_trust(
                WEB_SEARCH_TOOL_SPEC.name,
                ToolTrustOperation.SELECTION,
                is_superuser=is_superuser,
            )
            if decision.allowed:
                catalog.append(
                    f"- {WEB_SEARCH_TOOL_SPEC.name} | 联网搜索 | "
                    "回答实时问题、新闻、天气与近期信息"
                )

        return (
            "\n".join(catalog)
            if catalog
            else "当前工具调用与联网功能均已关闭，无需返回任何插件。"
        )

    def is_tool_blacklisted(self, tool_name: str) -> bool:
        """统一判断普通插件、自定义函数、MCP 工具是否被黑名单禁用。"""
        blacklist = model_selector.get_tool_blacklist() or []

        for item in blacklist:
            item = str(item).strip()
            if not item:
                continue

            # 精确禁用：extract_webpage / nonebot_plugin_xxx / mcp__filesystem__read_file
            if item == tool_name:
                return True

            # 通配禁用：mcp__filesystem__*
            if item.endswith("*") and tool_name.startswith(item[:-1]):
                return True

            # 服务级禁用：mcp__filesystem 禁用 mcp__filesystem__read_file 等
            if tool_name.startswith(item + "__"):
                return True

        return False

    @staticmethod
    def is_tool_allowed(
        schema: Mapping[str, Any], *, is_superuser: bool
    ) -> bool:
        if not isinstance(schema, Mapping):
            return False
        spec = schema.get("tool_spec")
        if spec is not None and not isinstance(spec, ToolSpec):
            return False
        return not (
            spec is not None
            and spec.permission == "superuser"
            and not is_superuser
        )

    @staticmethod
    def tool_identifier_not_found_message(tool_name: str) -> str:
        return (
            f"找不到工具标识：{tool_name}\n"
            "请确认它是已加载的 NoneBot 插件包名、自定义函数名，"
            "或已配置/已发现的 MCP 标识。可先发送“刷新工具”后重试。\n"
            "MCP 示例：mcp__filesystem、mcp__filesystem__read_file、"
            "mcp__filesystem__*"
        )

    def validate_tool_identifier(
        self,
        tool_name: str,
        *,
        is_superuser: bool = True,
        provider_cutover: bool | None = None,
    ) -> tuple[bool, str]:
        """
        Compatibility facade for generation-bound management resolution.

        支持 MCP 服务级标识：
        - mcp__server
        - mcp__server__*
        """
        tool_name = str(tool_name or "").strip()
        if not tool_name:
            return False, "工具标识不能为空"
        from .runtime_snapshot import runtime_snapshots

        runtime_snapshot = runtime_snapshots.active()
        snapshot = (
            runtime_snapshot.tool_snapshot
            if runtime_snapshot is not None
            else self.snapshot()
        )
        if provider_cutover is None and runtime_snapshot is not None:
            provider_cutover = runtime_snapshot.config.get(
                "provider_catalog_management_enabled",
                True,
            )
        view = snapshot.resolve_tool_management(
            tool_name,
            is_superuser=is_superuser,
            provider_cutover=provider_cutover,
        )
        if view is None:
            return False, self.tool_identifier_not_found_message(tool_name)
        if not view.allowed:
            return False, view.denial_reason or "工具管理 trust policy 拒绝"
        return True, view.label

    def _is_known_mcp_identifier(self, tool_name: str) -> bool:
        if not tool_name.startswith("mcp__"):
            return False

        if tool_name in getattr(self, "mcp_tool_names", set()):
            return True

        server_token = None
        if tool_name.endswith("__*"):
            server_token = tool_name.removeprefix("mcp__").removesuffix("__*")
        elif "__" not in tool_name.removeprefix("mcp__"):
            server_token = tool_name.removeprefix("mcp__")

        if not server_token:
            return False

        mcp_manager.load_config()
        if server_token in mcp_manager.configured_server_identifiers():
            return True

        prefix = f"mcp__{server_token}__"
        return any(
            name.startswith(prefix) for name in getattr(self, "mcp_tool_names", set())
        )

    async def load_mcp_tools(self) -> int:
        """
        从 mcp_servers.toml 发现 MCP 工具，并合并进 custom_tools。
        黑名单在这里过滤一次，get_brief_catalog/get_tool_schema 里也会再兜底过滤。
        """
        # 清理旧 MCP tools
        for name in list(getattr(self, "mcp_tool_names", set())):
            self.custom_tools.pop(name, None)

        self.mcp_tool_names = set()

        mcp_tools = await mcp_manager.discover_tools()

        for name, schema in mcp_tools.items():
            if self.is_tool_blacklisted(name):
                continue
            if name in self.custom_tools:
                raise ValueError(f"MCP 工具名与现有工具冲突: {name}")
            schema["source"] = "mcp"
            self.custom_tools[name] = schema
            self.mcp_tool_names.add(name)

        logger.info(f"已加载 MCP 工具: {list(self.mcp_tool_names)}")
        return len(self.mcp_tool_names)

    def _load_custom_plugin_info(self) -> dict:
        """读取 custom_plugin_info.json。"""
        try:
            with open(self.custom_info_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"读取自定义插件描述文件失败: {e}")
            return {}

    def _merge_dependencies_from_custom_plugin_info(self, dependencies=None):
        """
        从 custom_plugin_info.json 读取 dependencies 字段，并合并进 tool_dependencies。

        示例：
        {
          "nonebot_plugin_xxx": {
            "name": "随机图",
            "description": "...",
            "usage": "...",
            "dependencies": ["mcp__danbooru_searcher__search_tags"]
          }
        }
        """
        dependencies = self.tool_dependencies if dependencies is None else dependencies
        custom_info = self._load_custom_plugin_info()

        for plugin_name, info in custom_info.items():
            if plugin_name.startswith("_"):
                continue

            if not isinstance(info, dict):
                continue

            deps = info.get("dependencies") or info.get("tool_dependencies")
            if not deps:
                continue

            if isinstance(deps, str):
                deps = [deps]

            if not isinstance(deps, list):
                logger.warning(
                    f"custom_plugin_info.json 中 {plugin_name}.dependencies 格式错误，应为字符串列表"
                )
                continue

            clean_deps = {
                str(dep).strip() for dep in deps if isinstance(dep, str) and dep.strip()
            }

            if not clean_deps:
                continue

            dependencies.setdefault(plugin_name, set()).update(clean_deps)

            logger.debug(
                f"custom_plugin_info.json 注入依赖: {plugin_name} -> {list(clean_deps)}"
            )

    @staticmethod
    def build_llm_payload_schema(
        plugin_names: list[str],
        *,
        tools_enabled: bool,
        search_enabled: bool,
        plugin_info: Mapping[str, Mapping[str, Any]],
        custom_tools: Mapping[str, Mapping[str, Any]],
        is_superuser: bool = False,
    ) -> list[dict[str, Any]]:
        """Build the exact legacy rollback view consumed by llm_payload."""

        if not tools_enabled or not plugin_names:
            return []
        normal_plugins = [
            name for name in plugin_names if name != WEB_SEARCH_TOOL_SPEC.name
        ]
        tools = ToolManager.build_tool_schema(
            normal_plugins,
            include_search=False,
            plugin_info=plugin_info,
            custom_tools=custom_tools,
            is_superuser=is_superuser,
        )
        if search_enabled and WEB_SEARCH_TOOL_SPEC.name in plugin_names:
            tools.extend(
                ToolManager.build_tool_schema(
                    [],
                    include_search=True,
                    plugin_info=plugin_info,
                    custom_tools=custom_tools,
                    is_superuser=is_superuser,
                )
            )
        return tools

    @staticmethod
    def build_provider_llm_payload_schema(
        *,
        provider_catalog: ProviderCatalogSnapshot,
        plugin_names: list[str],
        search_enabled: bool,
        plugin_info: Mapping[str, Mapping[str, Any]],
        custom_tools: Mapping[str, Mapping[str, Any]],
        is_superuser: bool = False,
    ) -> list[dict[str, Any]]:
        """Build one payload schema from generation-bound Provider records."""

        if not isinstance(provider_catalog, ProviderCatalogSnapshot):
            raise TypeError("llm_payload provider_catalog 非法")
        if not isinstance(plugin_names, list) or not all(
            isinstance(name, str) for name in plugin_names
        ):
            raise TypeError("llm_payload plugin_names 必须是字符串列表")
        if type(search_enabled) is not bool or type(is_superuser) is not bool:
            raise TypeError("llm_payload feature/actor 标志必须是布尔值")

        tools: list[dict[str, Any]] = []
        for name in plugin_names:
            if name == WEB_SEARCH_TOOL_SPEC.name:
                continue
            if tool_manager.is_tool_blacklisted(name):
                continue
            item = provider_catalog.tools.get(name)
            if item is None:
                if name in plugin_info or name in custom_tools:
                    raise ProviderConsumerParityError(
                        f"llm_payload Provider identity 缺失: {name}"
                    )
                continue
            if item.source is ToolSource.NONEBOT_PLUGIN:
                if name not in plugin_info:
                    raise ProviderConsumerParityError(
                        f"llm_payload NoneBot rollback identity 缺失: {name}"
                    )
            elif item.source is ToolSource.BUILTIN:
                raise ProviderConsumerParityError(
                    f"llm_payload 未知 builtin payload 工具: {name}"
                )
            elif name not in custom_tools:
                raise ProviderConsumerParityError(
                    f"llm_payload Tool rollback identity 缺失: {name}"
                )
            decision = provider_catalog.decide_trust(
                name,
                ToolTrustOperation.SELECTION,
                is_superuser=is_superuser,
            )
            if not decision.allowed:
                continue
            if item.source in {
                ToolSource.REGISTERED,
                ToolSource.CUSTOM_FILE,
                ToolSource.GENERATED,
            }:
                # These three legacy projections are emitted through
                # ToolSpec.as_legacy_schema(), which normalizes an omitted
                # JSON-Schema `required` field to an empty list. Reproduce the
                # wire contract from the canonical spec, not from the sidecar.
                parameters = item.spec.as_legacy_schema()["parameters"]
            else:
                parameters = mutable_value(item.spec.parameters)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": item.spec.name,
                        "description": item.spec.description,
                        "parameters": parameters,
                    },
                }
            )

        if (
            search_enabled
            and WEB_SEARCH_TOOL_SPEC.name in plugin_names
            and not tool_manager.is_tool_blacklisted(WEB_SEARCH_TOOL_SPEC.name)
        ):
            item = provider_catalog.tools.get(WEB_SEARCH_TOOL_SPEC.name)
            if item is None or item.source is not ToolSource.BUILTIN:
                raise ProviderConsumerParityError(
                    "llm_payload builtin web_search Provider identity 缺失"
                )
            decision = provider_catalog.decide_trust(
                WEB_SEARCH_TOOL_SPEC.name,
                ToolTrustOperation.SELECTION,
                is_superuser=is_superuser,
            )
            if decision.allowed:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": item.spec.name,
                            "description": item.spec.description,
                            "parameters": mutable_value(item.spec.parameters),
                        },
                    }
                )
        return tools

    @staticmethod
    def build_tool_schema(
        plugin_names: list[str],
        *,
        include_search: bool = False,
        plugin_info: Mapping[str, Mapping[str, Any]],
        custom_tools: Mapping[str, Mapping[str, Any]],
        is_superuser: bool = False,
    ) -> list:
        tools = []

        for name in plugin_names:
            if tool_manager.is_tool_blacklisted(name):
                continue

            if name in plugin_info:
                info = plugin_info[name]
                spec = info.get("tool_spec")
                if spec is not None and not isinstance(spec, ToolSpec):
                    continue
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": spec.description
                            if spec is not None
                            else (
                                f"插件名称：{info.get('name') or name}。"
                                f"功能描述：{info.get('description') or '无描述'}。"
                                f"原始用法说明：{info.get('usage') or '无用法说明'}"
                            ),
                            "parameters": mutable_value(spec.parameters)
                            if spec is not None
                            else {
                                "type": "object",
                                "properties": {
                                    "command": {
                                        "type": "string",
                                        "description": (
                                            "严格根据该插件的'原始用法说明'，"
                                            "生成可以直接触发该插件的机器人指令字符串。"
                                        ),
                                    }
                                },
                                "required": ["command"],
                            },
                        },
                    }
                )

            elif name in custom_tools:
                info = custom_tools[name]
                if not ToolManager.is_tool_allowed(
                    info, is_superuser=is_superuser
                ):
                    continue
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": info.get("description") or name,
                            "parameters": mutable_value(
                                info.get("parameters")
                                or {
                                    "type": "object",
                                    "properties": {},
                                }
                            ),
                        },
                    }
                )

        if include_search and not tool_manager.is_tool_blacklisted(
            WEB_SEARCH_TOOL_SPEC.name
        ):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": WEB_SEARCH_TOOL_SPEC.name,
                        "description": WEB_SEARCH_TOOL_SPEC.description,
                        "parameters": mutable_value(
                            WEB_SEARCH_TOOL_SPEC.parameters
                        ),
                    },
                }
            )

        return tools

    def get_tool_schema(
        self,
        plugin_names: list,
        include_search: bool = False,
        *,
        is_superuser: bool = False,
    ) -> list:
        return self.build_tool_schema(
            plugin_names,
            include_search=include_search,
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            is_superuser=is_superuser,
        )


tool_manager = ToolManager()

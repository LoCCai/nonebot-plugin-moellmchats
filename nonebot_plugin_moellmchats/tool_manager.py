from collections import deque
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
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
from .tool_contracts import ToolSpec, tool_registry, validate_parameters_schema
from .tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderRegistration,
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


@dataclass(frozen=True)
class ToolSnapshot:
    generation: int
    plugin_info: Mapping[str, Mapping[str, Any]]
    custom_tools: Mapping[str, Mapping[str, Any]]
    tool_dependencies: Mapping[str, AbstractSet[str]]
    mcp_tool_names: AbstractSet[str]
    provider_catalog: ProviderCatalogSnapshot | None = None
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

    def get_brief_catalog(self, *, is_superuser: bool = False) -> str:
        return ToolManager.build_brief_catalog(
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            mcp_tool_names=self.mcp_tool_names,
            is_superuser=is_superuser,
        )


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
        return self.build_brief_catalog(
            plugin_info=self.plugin_info,
            custom_tools=self.custom_tools,
            mcp_tool_names=self.mcp_tool_names,
            is_superuser=is_superuser,
        )

    @staticmethod
    def build_brief_catalog(
        *,
        plugin_info: Mapping[str, Mapping[str, Any]],
        custom_tools: Mapping[str, Mapping[str, Any]],
        mcp_tool_names: AbstractSet[str],
        is_superuser: bool = False,
    ) -> str:
        catalog = []

        if model_selector.get_use_tools():
            # 1. NoneBot 原生插件
            for name, info in plugin_info.items():
                if tool_manager.is_tool_blacklisted(name):
                    continue

                plugin_name = info.get("name") or name
                description = info.get("description") or "无描述"
                catalog.append(
                    f"- {name} | {plugin_name} | {str(description)[:160]}"
                )

            # 2. 自定义函数 + MCP 工具
            for name, info in custom_tools.items():
                if tool_manager.is_tool_blacklisted(name):
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
        if model_selector.get_web_search() and not tool_manager.is_tool_blacklisted(
            WEB_SEARCH_TOOL_SPEC.name
        ):
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

    def validate_tool_identifier(self, tool_name: str) -> tuple[bool, str]:
        """
        校验工具标识是否对应当前可识别的 NoneBot 插件、自定义函数或 MCP。

        支持 MCP 服务级标识：
        - mcp__server
        - mcp__server__*
        """
        tool_name = str(tool_name or "").strip()
        if not tool_name:
            return False, "工具标识不能为空"

        if tool_name == WEB_SEARCH_TOOL_SPEC.name:
            return True, "联网搜索工具"

        loaded_plugin_names = {
            plugin.name for plugin in nonebot.plugin.get_loaded_plugins()
        }
        if tool_name in loaded_plugin_names:
            return True, "NoneBot 插件"

        if tool_name in self.custom_tools:
            if tool_name in getattr(self, "mcp_tool_names", set()):
                return True, "MCP 工具"
            return True, "自定义函数工具"

        if self._is_known_mcp_identifier(tool_name):
            return True, "MCP 服务"

        return (
            False,
            (
                f"找不到工具标识：{tool_name}\n"
                "请确认它是已加载的 NoneBot 插件包名、自定义函数名，"
                "或已配置/已发现的 MCP 标识。可先发送“刷新工具”后重试。\n"
                "MCP 示例：mcp__filesystem、mcp__filesystem__read_file、mcp__filesystem__*"
            ),
        )

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
        for server_name, conf in getattr(mcp_manager, "servers", {}).items():
            if not isinstance(conf, dict):
                continue
            safe_server = mcp_manager._safe_identifier(server_name)
            if safe_server == server_token:
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

---
title: 02-plan-future-architecture
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-22T19:16:21+00:00
---

# 02-plan-future-architecture

# Plan 2：后续功能与架构优化

> 推荐目标版本：`0.26 → 0.30`

> 实施门禁（2026-08-22）：Plan 1、Plan 2 的 D-01a～D-08f、Milestone E、F-01～F-14 与 G-01 已完成精确 HEAD 双 run gate；legacy sidecar 继续保留，D-09 因无发布周期观察且禁止生产操作而保持锁定，G-02 依赖已解除。G-01 实现提交 `b3566d6513f142d86de91898a6c6b8f14a4e131d` 用不可变 Conversation/Message records 和调用方显式持有的 `AsyncSession` 实现 PostgreSQL Repository；历史查询固定显式列、`conversation_id`、`id DESC`、有限 `LIMIT+1` 与应用层反转，游标绑定会话指纹和 `before_message_id`，不使用 OFFSET。写入以 `RETURNING` 确认 statement 结果但不隐式 commit/rollback/retry，未知结果 fail closed 且错误脱敏。本地四版本定向各 `36 passed`、相关联合各 `173 passed`、普通全量各 `1244 passed, 1 skipped`，Sandbox `40 passed, 0 skipped`，静态、最低依赖、fresh 制品及四组包外零数据库 I/O smoke 均通过。G-01 本地证据 HEAD `d086e8ee87c5e25d8b692e8a7aadb239ef42464a` 的 push run `32593099818` / PR run `32593102078` 均为 11/11 green、各恰好一个成功 `release-gate`；远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。未读取生产 DSN、未创建全局 engine/session、未接配置、startup/shutdown、现有内存聊天路径或生产 runtime，未运行 migration，未连接真实数据库/Redis，未部署。逐项状态见 [Plan 1 完成审计](./05-plan1-completion-audit.md) 与 [实施 Backlog](./04-implementation-backlog.md)。

---

# 1. 计划目标

当 0.25 Runtime 安全边界稳定后，下一阶段的重点不应继续是“加更多 Function Calling”，而是把已有工具体系正式升级成 Agent Runtime。

最终目标：

```text
LLM Chat Plugin
      ↓
Agent Runtime
```

---

# 2. Tool Capability 模型

当前 ToolSpec 主要：

```text
permission
effect
timeout
result_limit
dependencies
```

建议在 Provider 来源与 trust identity 已稳定后，以版本化契约新增：

```python
ToolCapability:
    network
    filesystem
    process
    database
    bot
    secrets
```

Plan 1 已有严格五字段 `network/process/workspace/host_filesystem/secrets`，并把 requested/admin/effective 三份策略绑定 ToolArtifact digest；其中 `secrets` 仍是不会注入宿主密钥的预留位。这里规划的是把现有布尔能力扩展为 host allowlist、细粒度 filesystem/database/bot 等结构化能力，会改变 artifact contract 与 digest 语义，不应与首个 Provider PR 混合；必须保留旧契约验证兼容或显式升约。

---

## 2.1 示例

```yaml
weather:
  permission: user
  effect: read_only

  capabilities:
    network:
      allow:
        - api.weather.example

    workspace:
      read: true
      write: false
```

---

# 3. ToolProvider 抽象

工具来源已经越来越多：

```text
Registered Tool
Custom File Tool
Generated Tool
MCP
Web Search
NoneBot Plugin
```

ToolManager 不应继续直接处理所有来源。

---

## 3.1 Provider 接口

第一个可独立回滚的改动只定义发现契约，不接管执行：

```python
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


CandidateResourcesT = TypeVar("CandidateResourcesT")


@dataclass(frozen=True)
class ProviderDiscoveryContext(Generic[CandidateResourcesT]):
    generation: int
    resources: CandidateResourcesT


@dataclass(frozen=True)
class DiscoveredTool:
    provider_id: str
    source: ToolSource
    trust: ToolTrustLevel
    generation: int
    spec: ToolSpec
    artifact: ToolArtifact | None = None


class ToolProvider(Protocol[CandidateResourcesT]):
    provider_id: str

    async def discover(
        self,
        context: ProviderDiscoveryContext[CandidateResourcesT],
    ) -> tuple[DiscoveredTool, ...]:
        ...
```

约束：

- `provider_id/source/trust` 是稳定身份，统一来源时不得抹平信任边界。
- `discover()` 只构建候选集，不修改 `ToolManager`、MCP 全局镜像或 `RuntimeSnapshot`。
- D-01a 的 context 同时固化非负 generation 和来源专属候选资源。每种 Provider 必须定义自己的 frozen typed resource record（例如 Registered specs、Generated after-state/source override）；字段递归冻结并与构建输入脱离，禁止用 `Any` 或裸 `dict` 充当候选资源。
- 候选资源由同一 runtime transaction 构建并传入 `discover()`，不得藏在可变 Provider 实例或全局 sidecar 中。Generated Provider 必须接收精确 after-state/source override，不得在 `discover()` 中重读 live canonical。
- `ToolSpec` 必需；`ToolArtifact` 仅对 Custom File / Generated 必需，Registered / MCP / Builtin / NoneBot 不得伪造源码制品。
- 发现记录必须深冻结、与输入脱离，并校验 artifact generation 与候选 generation 一致。
- 首个 PR 不定义 `execute(Any | dict)`；执行契约等明确的 `ToolExecutionRequest` 或 E-03 `ToolCall` 后再引入。
- Provider `reload()` 不得自行 commit；任何可变操作都必须保持 build candidate → validate → single publish 事务边界。

实施状态（2026-08-20）：D-01a 已在 `nonebot_plugin_moellmchats/tool_providers.py` 定义上述发现契约与六类来源专属 resource record，并由 `tests/test_tool_providers.py` 覆盖深冻结、generation、source/trust、artifact 和 Generated after-state/source override 边界。实现提交 `67638980b8abe3d515ca8146ab68381692f6ac74` 保持 type-only/shadow，包含它的精确 HEAD `c8afc807138a02237d96b65c81bf7f38c1ec7f43` 已完成 push/PR 远端 gate。

D-02 实现提交 `0ebadc05c4cd1dde143312f2d6ddf38fb34c19ed` 新增 frozen `RegisteredToolProvider`。runtime candidate 只截取一次 registry snapshot，同一份 `ToolSpec` identity 同时输入无 I/O discovery 与 legacy 构造；完整比较注册工具集合、legacy 字段、handler、精确 `ToolSpec`、parameters、source、dependencies 和 generation，任一偏差均 fail closed。shadow 结果校验后丢弃，当前 `ToolSnapshot` / `RuntimeSnapshot` schema、执行/选择/catalog consumer、MCP 镜像均未改变。四版本定向各 `59 passed`、普通全量各 `363 passed, 1 skipped`，mandatory root Sandbox `40 passed`，fresh build/Twine/checksum 与 Python 3.10/3.12 × wheel/sdist 外加载 reload 均通过；包含该实现的精确 HEAD `8d42152e54a59b6f3d0d2b39c20b12c5f0dd4a5e` 双 run 远端 gate 已 green，未部署。

D-01b 实现提交 `3db538b8515a4359c73aa0e7fc341b67504d3ea2` 新增 frozen `ProviderRegistration`、typed `ProviderDiscoveryPlan/Batch`、不可变 `ProviderRegistry` 和 schema version 2 的 `ProviderCatalogSnapshot`。Registry 要求每个已注册 Provider 在候选 generation 中恰好一个 typed plan，统一拒绝缺失、重复、未注册或 identity/generation 漂移的 operation/batch，并在生成 catalog 前集中拒绝跨 Provider 工具重名。`ToolSnapshot` dual-publish legacy 四字段与 `provider_catalog`，构造时再次校验 Registered slice 的工具集合、`ToolSpec`/handler/Schema/source/dependencies 等价；额外 legacy plugin dependency 可保留，但 Registered 声明依赖不得丢失。所有现有 consumer 仍读 legacy 字段，未新增 Provider 执行接口。四版本普通全量各 `368 passed, 1 skipped`，mandatory root Sandbox `40 passed`，fresh build/Twine/checksum 与四组外加载均通过；包含该实现的精确 HEAD `c8a4211560f2f7214b971109c54d817628f843d5` 双 run 远端 gate 已 green，未部署。

D-03 实现提交 `72d82f7e3a4ab6fe7b40b538f45ebec817aef889` 新增 frozen `FileToolProvider`（`custom-file / CUSTOM_FILE / REVIEWED`）并把它加入 Registry。每个 runtime candidate 只运行一次既有文件加载器；`FileToolResources.from_legacy_tools()` 从该次 legacy 结果固定精确 `ToolArtifact`，Provider 只做纯内存 digest/generation/discovery，不重读源码、不重跑 AST Policy、不执行工具。同一 candidate 随后复用于 legacy merge，Provider parity 与最终 `ToolSnapshot` 分别验证工具集合、artifact/source snapshot/digest、精确 `ToolSpec`/handler、schema/source/effect/generation 和 Provider 声明依赖；文件级或 plugin 追加依赖可共存，但声明依赖不得丢失。Registry 在 legacy 合并前集中拒绝 Registered/File 重名。现有 consumer、执行路径、MCP 镜像与 `RuntimeSnapshot` schema 均未切换。四版本普通全量各 `375 passed, 1 skipped`，mandatory root Sandbox `40 passed`，fresh build/Twine/checksum 与四组外加载均通过；包含该实现的精确 HEAD `38a2da9cbec25e7dfeb07fb3cdd172a5e13396c9` 双 run 远端 gate 已 green，未部署。

D-04 实现提交 `95a57cfc7abeab59b310ae19a6e7872da0e01136` 新增 frozen `GeneratedToolProvider`（`generated / GENERATED / UNTRUSTED`）并把它加入 Registry。每个 runtime candidate 只运行一次既有 Generated loader；`GeneratedToolResources.from_legacy_tools()` 将该次结果绑定到事务精确 lifecycle after-state、source override 与 `ToolArtifact`，Provider 只做纯内存验证与 discovery，不重读 live canonical、源码或验证入口，也不执行工具。同一 candidate 随后复用于 legacy merge，Provider parity 与最终 `ToolSnapshot` 分别验证 active bundle 集合、bundle/artifact digest、精确 `ToolSpec`/handler、权限、effect、capability、generation 和声明依赖；Registry 在 legacy 合并前集中拒绝 Registered/File/Generated 跨来源重名。现有 consumer、执行路径、MCP 镜像与 `RuntimeSnapshot` schema 均未切换。D-04 定向 `78 passed`；Python 3.10～3.13 普通全量各 `382 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff、Pyright、fresh build/Twine/checksum 与 Python 3.10/3.12 × wheel/sdist 四组包外加载均通过。包含该实现的精确 HEAD `f03a8ab86bddc392b72beeaaa643c7642ed2687d` 双 run 远端 gate 已 green，未部署。

D-05 实现提交 `76c746c134807b99e23b67489db9a7d1185e3b26` 新增 frozen `MCPToolProvider`（`mcp / MCP / EXTERNAL`）并把它加入 Registry。每个 runtime candidate 仍只执行一次既有 MCP 网络发现且保持 `strict=True`；`MCPToolResources.from_legacy_tools()` 从该次候选派生不可变 `ToolSpec`，Provider 本身不读取网络、文件或全局 sidecar。route sidecar 必须与 MCP 工具集合完全一致且只包含非空 `server/tool`；candidate merge 与最终 `ToolSnapshot` 双重验证名称、Schema、handler、source、generation、dependencies 与 `mcp_tool_names`。发现、route、冲突或 parity 失败均拒绝整代 candidate 并保留上一代快照。现有 MCP manager/sidecar、consumer、执行路径与 `RuntimeSnapshot` schema 均未切换。D-05 定向 `85 passed`；Python 3.10～3.13 普通全量各 `389 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff、Pyright、fresh build/Twine/checksum 与 Python 3.10/3.12 × wheel/sdist 四组包外加载均通过。包含该实现的精确 HEAD `14fe2274d373e2e3a35443d3e0bedcb11f02bb28` 双 run 远端 gate 已 green，未部署。

D-05a 实现提交 `a09be4836318ddfcf7d72f7b2b8232fd67c6906c` 新增 frozen `BuiltinToolProvider`（`builtin / BUILTIN / TRUSTED`）并把它加入 Registry。当前唯一内置旁路 `web_search` 收口为 canonical `ToolSpec`，legacy Schema 与真实搜索适配器共享同一 spec/handler；Provider 只做纯内存 discovery 与 parity，不执行搜索。Registry、candidate merge 与最终 `ToolSnapshot` 会验证工具集合、精确 `ToolSpec` identity、generation、source/trust、artifact absence 与 dependencies；Registry 拒绝 Builtin 与 Registered/File/Generated/MCP 重名，runtime 另外拒绝 NoneBot 插件与 Builtin 重名。`TRUSTED` 仅表示本地适配器代码来源；搜索返回仍是 external observation，不提升数据可信度。现有开关、黑名单、超时、结果限制、legacy `if web_search` consumer 与执行路径全部保留，`web_search` 仍不进入 legacy `custom_tools`，也未修改 `RuntimeSnapshot` schema。四个 Python 版本普通全量各 `400 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，D-05a 定向 `94 passed`；Ruff、Pyright、fresh build/Twine/checksum 与 Python 3.10/3.12 × wheel/sdist 四组包外加载均通过。包含该实现的精确 HEAD `7a10d2ad575674fad063ffd3971e786bbb996854` 已完成 push run `32414490980` / PR run `32414496386` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 `CLEAN`。未部署。

D-05b 实现提交 `51686d0bd28f64f7cae7469db330033dc39b9109` 新增 frozen `NoneBotPluginProvider`（`nonebot-plugin / NONEBOT_PLUGIN / REVIEWED`）并把它加入 Registry。每个 runtime candidate 将同一份 legacy `plugin_info` 绑定到 canonical `ToolSpec`；默认权限保持 `user`，遗留插件能力保守标记为 `MUTATING`。Provider discovery 只消费事务传入的 typed resources，不执行插件、不读取 I/O；Registry 在 legacy merge 前统一拒绝六类 Provider 跨来源重名，candidate merge 与最终 `ToolSnapshot` 双重验证工具集合、精确 spec identity、description、permission/effect、generation、source/trust 与 dependencies。canonical handler 继续进入已有有界 `EventSimulator`，但本阶段所有 consumer 与真实执行仍读取 legacy 视图并直接走原伪事件分支，没有新增确认流程、没有切换 Provider consumer，也未修改 `ToolSnapshot` / `RuntimeSnapshot` dataclass schema。D-05b 定向 `102 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4）与 3.13.13 普通全量各 `411 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 与 Pyright `0 errors, 0 warnings`。fresh wheel/sdist、Twine 与 checksum 通过，Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。包含该实现的精确 HEAD `531ff204b0746cc34fdda13a5b4fd4e60e2c3c58` 已完成 push run `32417941584` / PR run `32417947550` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 `CLEAN`。未部署。

D-06 实现提交 `5d7b7a6734ba14650ef892dfffee369c88e8a4f4` 新增 frozen `ToolTrustPolicy` / `ToolTrustDecision`，并由每个 `ProviderCatalogSnapshot` 为全部发现工具派生完整、不可变、与精确 generation / `ToolSpec` identity 绑定的 trust policy。六类来源固定映射到 in-process、isolated artifact、generated sandbox、external proxy 与 bounded event 五类执行边界；MCP 与 `web_search` 结果标记 external，Generated 结果标记 untrusted，其余结果保持 unverified，避免把适配器代码 trust 错传给返回数据。selection 强制 effective permission，management 只允许超级用户，mutating execution 要求已完成二阶段确认；未显式注册的 NoneBot 遗留适配器保留有界 compatibility 例外并强制审计，不在本阶段改变现有权限/确认语义。拒绝、所有执行/管理、非 trusted selection 与外部结果 selection 均要求审计，`audit_metadata()` 只含固定身份/策略字段，不含参数或结果。当前 consumer 仍全部读取 legacy 视图；没有切换真实执行、pending action、search 或管理命令，没有引入 D-07 capability merge，也未修改 `ToolSnapshot` / `RuntimeSnapshot` dataclass schema。D-06 定向 `98 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4）与 3.13.13 普通全量各 `418 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 与 Pyright `0 errors, 0 warnings`。fresh wheel/sdist、Twine 与 checksum 通过，wheel SHA256 `ee916eac21ed6e744b29adc0816c8e3886238a170c4e7aa39c8f2306317a79a9`、sdist SHA256 `b9f448b8977699616685eefefd753fe7b12068d7b6c29dc05c8ad07001f79817`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")` 及 packaged trust policy 检查全部通过。包含该实现的精确 HEAD `c4ecaf9b7519b6c56fd5d20a6e5640993eb65f69` 已完成 push run `32420501280` / PR run `32420504608` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 `CLEAN`。未部署。

D-07 实现提交 `c2ca76332b9bfa97fadc7fc8f994b50b7e44dfdd` 新增 frozen `ToolCapabilityV2`，将 network / secrets allowlist、workspace/host filesystem read/write、database read/write 与 bot read/send/manage 纳入严格、deny-by-default 的结构化契约。effective 始终由 `requested ∩ admin` 派生；AST 按 handler 固化 coarse detected evidence，Generated policy 还合并测试源码证据，并强制 `detected ⊆ effective`，因此静态检测不能成为授权来源。`ToolContractSnapshot` / `ToolArtifact` 默认升为 v2，摘要域使用 `moellm-tool-artifact-v2` 并绑定 capability schema/detector version 及 requested/detected/admin/effective；v1 原摘要算法、File/Generated Provider、legacy sidecar 与 `ToolSnapshot` 保持 dual-read，版本伪装和 v1/v2 不可表达混用均 fail closed。Custom/Generated sidecar dual-publish 粗粒度字段和结构化 policy，`ProviderCatalogSnapshot.schema_version == 3` 并提供不可变 capability policy 索引。当前 runner 只接受可精确投影为旧五布尔能力的 v2 policy；scoped network、读写拆分、database/bot 等新权限在 D-08 consumer 迁移前明确拒绝，未切换 categorize、LLM payload、`llm_tools`、pending action、search、管理命令或 MCP sidecar consumer。

D-07 定向 `238 passed, 1 skipped`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 普通全量各 `442 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`，五份 JUnit 均为 0 failure / 0 error，Sandbox 为 0 skip；Ruff 0.16.2 通过，D-07 核心模块定向 Pyright 为 `0 errors, 0 warnings`。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `51b394c59dcc1624444ff4bde2915bf4191f7cfacf8d31361f9605a151e8d844`、sdist SHA256 `c325c0267e9aeb68eef97867f008c827fef8016ed48cb83049c45f219f7c0d1d`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")` 及 packaged contract v2 / artifact v2 / catalog schema v3 检查全部通过。包含该实现的精确 HEAD `8846acd8334953367bd5ee2aa48844c992d2e9df` 已完成 push run `32425100008` / PR run `32425104856` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 `CLEAN`。未合并、未发布、未部署，D-08 依赖已解除。

D-08a 实现提交 `ec273fe5d12589943fb603e5875f69fe79434f73` 只切换 categorize consumer。`ToolSnapshot.get_brief_catalog()` 先构建 legacy rollback view，再从完整 schema v3 Provider catalog 按 canonical source、selection trust decision 与 effective permission 构建新目录；legacy 映射只承担稳定展示顺序与 NoneBot 历史展示字段。两份字符串不完全相等即抛出 `ProviderConsumerParityError`，不会把漂移目录交给分类模型。`provider_catalog_categorize_enabled=true` 默认启用切换，设为 `false` 可独立回滚；缺少六类 registration 的启动期或旧式快照继续走有界 legacy。测试覆盖普通用户/超级用户、六类来源、MCP 标签、黑名单与通配符、工具/搜索开关、空目录、旧快照、默认开关、配置回滚、非布尔配置及 parity 漂移。`ToolManager` 旧入口也委托当前 generation 快照；`llm_payload`、`llm_tools`、pending action、search、管理命令与真实执行语义均未切换，legacy sidecar 未删除。

D-08a 定向为 `16 passed`，Provider/Snapshot/Reload 联合为 `105 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 普通全量各 `458 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，五份 JUnit 均为 0 failure / 0 error，Sandbox 为 0 skip；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 对 `config.py` / `tool_manager.py` 的干净 HEAD 与当前树均为同一组 8 个既有诊断，按文件、规则和消息完全一致，本实现新增行没有诊断；未改动这些无关旧问题。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `337ba9b0b24fef8cf6635fdd4758db0a27a509b8e7452b0726e13699bf0a9e48`、sdist SHA256 `924529d139465338a7bb213884d8ffd41cbdc6945422c40c38bb1e4da449eb39`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")`、完整六 Provider registration、默认开关与 catalog parity 检查全部通过。最终文档闭环精确 HEAD `760c95c7b1565bdd955c9b990b692c9fe097bdd5` 已完成 push run `32427890454` / PR run `32427895162` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 `CLEAN`。D-08b 依赖已解除；未合并、未发布、未部署。

D-08b 实现提交 `761dbe2df47fc553090a7f36e0a71285b61b03c2` 只切换 `llm_payload` consumer。`LlmPayloadMixin._build_payload()` 仍按模型配置合并 required/resident 名称，但把最终工具集合和 schema 构建委托给请求绑定的 `ToolSnapshot`。完整 schema v3 六 Provider catalog 下，工具身份、Provider 声明 dependencies、selection trust/effective permission 与 canonical schema 成为新视图权威；legacy rollback view 始终先构建，并要求工具集合、依赖闭包和完整线协议 schema 逐次等价，任一额外或缺失的 legacy-only 依赖边、字段或权限漂移均抛出 `ProviderConsumerParityError`。Registered/File/Generated 从 canonical spec 重现历史 `required: []`，MCP/NoneBot/Builtin 保持原参数形态，避免借迁移改变模型线协议。

独立严格布尔开关 `provider_catalog_llm_payload_enabled=true` 默认启用；设为 `false` 只回滚 payload consumer，不影响 categorize、真实执行、pending action、search、管理命令、生命周期或 sidecar。启动期或旧式不完整六 Provider 快照继续有界 legacy 兼容。D-08b payload/snapshot 定向 `51 passed`，Provider/Snapshot/Reload 联合 `129 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 普通全量各 `474 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 分别为 22/19 个诊断、归一化后均为同一组 11 条既有消息，没有新增诊断类别。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `60773c8c026c1ec45a0fee70239e75e93a67169db55439c54ed5ea86a8251a56`、sdist SHA256 `fed859e711b6dd9fe7be04cb884ab6d3368f0c6ad7339508f1a78619e32ece68`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、reload、完整六 Provider registration、默认开关与 `web_search` schema parity 均通过。最终文档闭环精确 HEAD `b1158a7debe86e74bba46aa9e652733fe3581bad` 已完成 push run `32430209088` / PR run `32430214661` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR #2 为 `OPEN / CLEAN`。D-08c 依赖已解除；未合并、未发布、未部署。

D-08c 实现提交 `c1f8580a1c8ebeca629fc8cfce015c63184cb0e6` 只切换 `llm_tools` consumer。新增 generation-bound `LlmToolExecutionView` 与 Builtin Search / Custom Tool / NoneBot Plugin 三类 route；完整 schema v3 六 Provider catalog 下，工具 identity、canonical source、精确 `ToolSpec` 与 execution trust decision 成为新视图权威。legacy rollback adapter 逐调用校验 route、source 与 spec identity，MCP 因历史 sidecar 没有 `tool_spec` 而严格比较 handler、description、parameters 与 name；漂移或未知工具均在 adapter/副作用前 fail closed。Provider execution decision 在执行前生效，只有 canonical mutating custom tool 的 confirmation-required denial 可进入既有 PendingAction 二阶段确认过渡。NoneBot 使用 canonical handler 但仍进入原有 bounded event bus；`web_search` 使用 canonical builtin handler，内部 extractor 留待 D-08e。

独立严格布尔开关 `provider_catalog_llm_tools_enabled=true` 默认启用；设为 `false` 只回滚执行 consumer。D-08c 定向 `81 passed`，Provider/Snapshot/Reload 联合 `143 passed`；四版本严格串行普通全量各 `493 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check、fresh build/Twine 与四组包外 smoke 均通过。Pyright 1.1.407 的 parent/current 均为 55 个诊断、归一化后同为 23 条既有消息，零新增、零删除。wheel SHA256 `3032eef9888425f293441ccef96cedbf4de871cd85057a540e93a691e587db0a`，sdist SHA256 `609ae32d1bb0d213577637e5a4fbe7d6a6ad25f258de0764cb4d414aa22b6865`。最终文档闭环精确 HEAD `bef9b56367e4b05cd31110216b84fd61a8158b38` 已完成 push run `32432675246` / PR run `32432677694` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR #2 为 `OPEN / CLEAN`。D-08d 依赖已解除；未合并、未发布、未部署。

D-08d 实现提交 `fbdc87235be13e9bd0fb9fe1b09791f8bd528ebf` 只切换 PendingAction 确认执行 consumer。新增 frozen、generation-bound `PendingActionExecutionView`；完整 schema v3 六 Provider catalog 下，只允许 Registered / Custom File / Generated / MCP 四类 custom source，以 canonical source、精确 `ToolSpec`、handler、bundle identity 与 `confirmed=True` execution trust decision 为权威。legacy rollback view 在每次确认时校验 source/spec/bundle；MCP 历史 sidecar 没有 `tool_spec` 时严格比较 handler、description、parameters 与 name。Provider 路径从 canonical spec 构建最小执行 adapter，不把 legacy sidecar 当作执行权威。

安全与回滚边界：nonce 在任何 parity、权限、参数校验或副作用前一次性消费；Bot/adapter/user/group、参数哈希、generation 与 bundle digest 绑定保持不变。确认阶段基于命令捕获的 `RuntimeSnapshot` 读取开关，并以当前 actor 和 `confirmed=True` 重做 trust/permission 决策，因此错用户、旧 generation、版本漂移、普通用户确认 superuser 工具或 Provider/legacy 漂移全部 fail closed。独立严格布尔开关 `provider_catalog_pending_actions_enabled=true` 默认启用；设为 `false` 只回滚 PendingAction consumer，启动期或旧式不完整六 Provider 快照继续有界 legacy 兼容。Search extractor、管理命令和 legacy sidecar 尚未切换。

D-08d 定向 `129 passed`，Provider/Snapshot/Reload/Pending 联合 `171 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `509 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 均为 236 个诊断、归一化后均为 151 条既有消息，零新增、零删除。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `f063ebbb92b31c797c2c5b28e5aa57c6329415ede0d187f4617f269368d5a325`、sdist SHA256 `f43956bd09eeafcb6c65fcb3936be5c2f6d86fe0bc4d08d913d992611391aa4a`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、默认开关与 canonical confirmed PendingAction view 均通过。上述为 D-08d 本地门禁证据；远端闭环如下。

D-08d 最终文档闭环精确 HEAD `2576fca54fc7086aca4716ef5f98864d5dd8d78e` 已完成 push run `32434441897` / PR run `32434445098` 双 run gate；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR #2 为 `OPEN / CLEAN`。D-08e 依赖已解除；未合并、未发布、未部署。

D-08e 实现提交 `e26729db158023fba482ebe8c13cc99909f91ddf` 只切换 Search 内部 `extract_webpage` consumer。新增 frozen、generation-bound `SearchExtractorView`；完整 schema v3 六 Provider catalog 下，仅 Registered / Custom File / Generated / MCP 四类 custom source 可成为 extractor，以 canonical source、精确 `ToolSpec` 和 selection trust decision 为权威。legacy rollback view 每次搜索调用都校验 source/spec identity；MCP 历史 sidecar 无 `tool_spec` 时严格比较 handler、description、parameters 与 name，任一缺失或漂移都在 Tavily 网络请求前 fail closed。

权限与回滚边界：`llm_tools → execute_web_search → Search` 显式传递当前 actor 的 `is_superuser`；Provider 路径只有 selection decision 允许且工具未被黑名单命中时才披露来源 URL 和 `extract_webpage` 调用提示，拒绝时只返回标题。独立严格布尔开关 `provider_catalog_search_enabled=true` 默认启用；设为 `false` 只回滚 Search consumer，并精确保留历史 membership-only 行为。启动期、无请求快照或旧式不完整六 Provider catalog 继续有界 legacy 兼容；管理命令与 legacy sidecar 尚未切换。

D-08e 定向 `121 passed`，Provider/Snapshot/Reload/Search 联合 `226 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `534 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 均为 77 errors、3 warnings、80 条既有诊断，归一化 multiset 零新增、零删除。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `039779623e9a8617e2e4578bccf215edfe69d53e2407c088975375bdb7bc0587`、sdist SHA256 `4986c57c514a30934bd4c99c17b5de3caf84f1b1af876b78fab93d684cd4f736`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、generation 1、完整六 Provider registration、默认 Search 开关与 canonical extractor absence 均通过。

D-08e 最终文档闭环精确 HEAD `9540938816f5a5b8e26fa9589f3be53b7a8f7ef4` 已完成 push run `32438803052` / PR run `32438809768` 双 run gate；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-08f 依赖已解除；未合并、未发布、未部署。

D-08f 实现提交 `9238bd7ff415550ccc27fad750b573a023755403` 只切换黑名单添加时的工具身份管理 consumer。新增 frozen、generation-bound `ToolManagementView`；完整 schema v3 六 Provider catalog 下，精确 Registered / Custom File / Generated / MCP / Builtin / NoneBot Plugin 目标以 canonical source、精确 `ToolSpec` 与 `ToolTrustOperation.MANAGEMENT` decision 为权威，legacy rollback view 每次添加均校验 source/spec identity，MCP 历史 sidecar 无 `tool_spec` 时严格比较 handler、description、parameters 与 name。Provider 管理决策对普通用户一律 fail closed，允许或拒绝均只记录不含调用参数的固定 audit metadata。

兼容与事务边界：正式 runtime candidate 将历史 loaded-plugin namespace 与 MCP configured-server selector 冻结进同一 `ToolSnapshot` generation；`mcp__server`、`mcp__server__*` 绑定该代全部 canonical MCP member，尚未发现工具的已配置服务仍可提前加入黑名单，但同样执行超级用户 selector policy 与审计。命令在预校验原子 reload 成功后捕获当前 `RuntimeSnapshot`，并从该快照读取独立严格布尔开关 `provider_catalog_management_enabled=true`；设为 `false` 只回滚管理 consumer，旧式或不完整 catalog 继续有界 legacy。添加发生前的 reload/parity/trust 任一失败都不写配置；移除路径仍可清理已失效 blacklist 项并保留写后 reload 语义。常驻列表继续允许 stale 配置，由 D-08b payload 视图忽略未知项，未借本次迁移新增存在性限制。刷新/重载事务、其他 consumer 与 legacy sidecar 均未改变。

D-08f 管理/Snapshot/Reload 定向 `148 passed`，Provider/Snapshot/Reload 与全部已切换 consumer 联合 `277 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `554 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 对父提交/当前树同一 8 文件分别为 103/101 errors、2/2 warnings，归一化 multiset 零新增并消除 2 条旧诊断。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `297c079c82139e7de7fe6200b25bfa34ac258571bb7a2e0494d410af2ea6e170`、sdist SHA256 `a2768b84bdd16872097396aa6fae639c7a3e91b72ae922c37e243cd361cb0db8`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、generation 1、完整六 Provider registration、默认管理开关、canonical management decision 与空 MCP 服务 selector 均通过。

D-08f 最终文档闭环精确 HEAD `ea022bd31020880c72a66802aa3f036389d0169d` 已完成 push run `32443308534` / PR run `32443313095` 双 run gate；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-09 仍因发布周期观察不足而锁定，legacy sidecar 未删除；未合并、未发布、未部署。

---

## 3.2 Provider 实现

```text
RegisteredToolProvider
FileToolProvider
GeneratedToolProvider
MCPToolProvider
BuiltinToolProvider
NoneBotPluginProvider
```

迁移顺序：

```text
D-01a discovery contract（type-only / shadow）
  → RegisteredToolProvider（无 I/O pilot + parity）
  → ProviderRegistry / ToolSnapshot v2 dual view
  → FileToolProvider
  → GeneratedToolProvider（保留 lifecycle state/source override）
  → MCPToolProvider
  → BuiltinToolProvider / NoneBotPluginProvider
  → trust enforcement + versioned capability merge
  → 切换 categorize / llm_payload / llm_tools / pending action / search / 管理命令消费端
  → 删除 legacy sidecar
```

全程 dual-publish / dual-read，每个 Provider 先做 shadow parity；不在 D-01a 中修改当前 `ToolSnapshot` schema、迁移执行路径或删除 legacy 字段。

---

# 4. Tool Registry 与 Snapshot

ToolManager 应退化为：

```text
Tool Registry
+
Tool Selection
+
Tool Snapshot
```

而不是：

```text
加载文件
读取 MCP
解析插件
处理黑名单
处理依赖
执行工具
```

---

# 5. Tool Graph

当前 Tool Dependency：

```text
A → B
```

仅表示：

```text
选择 A 时把 B 一起注入
```

后续升级为：

```text
Tool Graph
```

---

## 5.1 Graph 能表达

```text
depends_on
parallel_with
conflicts_with
requires_confirmation
requires_capability
```

例如：

```text
price_search ─┐
              ├── analysis ── chart
fx_search ────┘
```

E-06 实现提交 `8af287e1a99e4f837dcbfb9a35071b17d5097c96` 新增独立 `tool_graph.py`。`ToolGraphRelation` 固定 `depends_on / parallel_with / conflicts_with` 词汇；frozen `ToolGraphEdge` 保留有向依赖语义，并把 parallel/conflict 的无向端点规范为稳定顺序。frozen `ToolGraph` 深冻结并排序工具、边、确认集合与 capability 映射；拒绝重复工具/边、未知节点、自引用、同一工具对多重矛盾关系、非法 capability 以及二节点或多节点依赖环。

查询 API 确定性返回拓扑序、直接/传递依赖、直接 dependent、parallel/conflict 邻居、确认与 capability 要求；`as_dict()` 每次返回新的 JSON primitive 副本。E-06 只定义图模型及固有不变量，不选择或执行工具，不判断 read-only，不调度并发，也不执行 E-08 冲突裁决；不接管 request manager/chat runtime，不引入 Repository、PostgreSQL、Redis、迁移、生产配置或 D-09 sidecar。E-07 只能在 E-06 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Tool Graph 定向 `42 passed`，与 Agent Runtime 联合定向 `227 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `781 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `73bd52305aaf277481fe2c13ceafff0294744259f314d922e4ce289db3b9da3d`、sdist SHA256 `d8983ae6cbe30cc8d3f47516d6ea4968b627f9096ac1df6825ea99d7aff73351`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged ToolGraph 深冻结/确定性依赖/环路拒绝均通过。最终文档闭环 HEAD `95a780eeb36a9de1db506664f86fd02bef6968c9` 对应 push run `32450808012` / PR run `32450810445`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-07 依赖已解除；未合并、未发布、未部署。

---

# 6. 并行工具

当前每轮最多执行一个工具，后续可支持：

```text
多个 read_only
+
无依赖
+
资源不冲突
```

并发运行。

示例：

```text
北京天气
上海天气
广州天气
```

可以：

```python
await asyncio.gather(...)
```

---

## 6.1 不允许并行的情况

- mutating
- 有显式依赖
- 访问同一锁资源
- 顺序语义明确
- 同一事务

E-07 实现提交 `7c1c8aa961969eb876c26f26db40a463e737a94b` 新增独立 `tool_scheduler.py`。frozen `ToolScheduleBatch` 以强类型 `serial / parallel` 区分恰好一个工具的串行批次与至少两个工具的并行批次；frozen `ToolSchedule` 保证工具最多出现一次并提供稳定 primitive 副本。`ReadOnlyParallelToolScheduler` 只生成确定性计划：先验证 selected/effect 精确覆盖与完整传递依赖闭包，再按 Tool Graph 拓扑 ready 集合分批。

并行是显式 opt-in：同一批次的每一对工具都必须有 `parallel_with`，全部 effect 必须是强类型 `ToolEffect.READ_ONLY`，且不得要求确认；`max_parallelism` 限定为 1～64，值 1 可完全关闭并行。mutating、确认门禁、未知关系一律退化为单工具串行；缺失依赖闭包 fail closed；选中 `conflicts_with` 工具对时不擅自选赢家或排序，而是拒绝并要求先经 E-08 policy。E-07 不创建 asyncio task、不 `gather`、不调用 handler、不授权 capability、不消费或延长 DeadlineContext，也不接管真实 ToolCall/AgentStep、request manager/chat runtime、Repository、PostgreSQL、Redis、迁移、生产配置或 D-09 sidecar；真正并发执行仍属于 G-09。E-08 只能在 E-07 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Tool Graph/Scheduler 定向 `79 passed`，与 Agent Runtime 联合定向 `264 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `818 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `546962c7f0d4963c4604b5ba6ceeff6bc1171434a58e7a5422e375a9356be771`、sdist SHA256 `57aa7d15e7219cf01ee8c0519e0546b4e15c7177b15319b1920c11ff402d7122`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 分层/只读并行/mutating 串行/conflict 拒绝均通过。最终文档闭环 HEAD `b1c942679bb29fb9b1dca111ce1c6641b9d44d50` 对应 push run `32452551080` / PR run `32452556938`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-08 依赖已解除；未合并、未发布、未部署。

## 6.2 Tool Conflict Policy

E-08 实现提交 `4892e57757dab124d2739183b6a91d6a67420073` 新增独立 `tool_conflicts.py`。frozen `ToolConflictRule` 对规范化工具对只允许强类型 `reject / prefer`：reject 不得携带 winner，prefer 必须显式指定两个端点之一。frozen `ToolConflictDecision / ToolConflictResolution` 记录规则是否显式、winner/loser、请求/保留/移除集合、整体 allowed/rejected 状态与拒绝原因，并返回新的 JSON primitive 副本；拒绝状态强制 selected/dropped 均为空，避免调用方误用部分决议。

`ToolConflictPolicy` 默认拒绝所有未配置 prefer 的选中 conflict；规则必须引用图中节点并精确匹配真实 `conflicts_with` 边，陈旧或伪造规则 fail closed。所有选中冲突对按规范顺序同时决议，任一 missing/explicit reject 即拒绝整次选择；全部 prefer 后再统一移除 loser，若循环偏好移除全部工具、或 survivor 的传递依赖被移除/原本缺失，也整体拒绝。Policy 不按输入顺序、effect、权限或 capability 猜 winner，不读取配置或生产状态，不执行工具、不创建 task、不修改 E-07 Scheduler；允许结果可显式交给 Scheduler，仍由 Scheduler 再验证 conflict-free 与 effect/并行约束。本任务不接真实 ToolCall/AgentStep、request manager/chat runtime、Repository、PostgreSQL、Redis、迁移、生产配置或 D-09 sidecar。

本地门禁：Graph/Scheduler/Conflict 定向 `125 passed`，与 Agent Runtime 联合定向 `310 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `864 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `f33107c781b2eefac8e95577e891b4b58795f1979e6a0313d8418c7819cd644c`、sdist SHA256 `7a1ca883584d30d41edc5c7bad58c41b9c88f065886fbddbb0fc1658e3f5790b`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 默认拒绝/显式赢家/依赖破坏拒绝/安全交接 Scheduler 均通过。最终文档闭环 HEAD `11a7ca10c5400d4b776efa4824ffa11b9ad0de00` 对应 push run `32454187768` / PR run `32454191418`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-01 依赖已解除；未合并、未发布、未部署。

---

# 7. AgentRun

把一次用户请求正式定义成：

```python
AgentRun:
    run_id
    request_id
    user_id
    group_id

    generation
    state

    started_at
    finished_at
```

E-01 实现提交 `56dae036b4a2eaac8dd9060487e1bf1e18bb9e16` 新增 `agent_runtime.py`，定义 frozen、generation-bound `AgentRun` 与 `AgentRunState`。状态集合精确覆盖 `CREATED / ADMITTED / CLASSIFYING / PLANNING / EXECUTING / WAITING_CONFIRMATION / SUMMARIZING / COMPLETED / FAILED / CANCELLED / TIMED_OUT / REJECTED`；run/request/user/group identity、非负 runtime generation 与有限非负时间戳均严格校验，五种终态必须携带不早于 `started_at` 的 `finished_at`，非终态不得伪造结束时间。稳定 primitive `as_dict()` 可供后续 audit/API/Repository 共用，但不泄露或保存 live Bot/Event。

本阶段只定义共享领域对象和终态一致性，不生成 run、不接管 `request_manager` / `chat_runtime`、不执行 E-04 状态转换，也不接数据库、Redis、Repository 或 legacy sidecar；D-09 保持原门禁。E-01 定向 `40 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `594 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `31e19c2d2dc83a7d0e12331664939e92e37958939e4fbf2cb8349248dadab237`、sdist SHA256 `f3acf3811ffdf59fbf3ade885bc47d44bc1736dfa08ba228c46ca09e6756fd4b`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration 与 packaged `AgentRun` 构造均通过。

E-01 最终文档闭环精确 HEAD `be2e83d54db0021f909cad04e5bca7c6ac19fa12` 已完成 push run `32444347880` / PR run `32444351420` 双 run gate；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-02 依赖已解除；未合并、未发布、未部署。

---

# 8. AgentStep

每一步：

```python
AgentStep:
    step_id
    run_id
    index

    type
    model
    tool

    status
    input
    output

    started_at
    finished_at
```

Step Type：

```text
classification
model
tool
summary
vision
confirmation
memory
```

E-02 实现提交 `29aa74ac7a03e2beb71b6834644171cdceeec50c` 在同一领域模块新增 frozen `AgentStep`、七类 `AgentStepType`、独立 `AgentStepStatus` 与递归 `AgentJsonValue`。对象精确携带 `step_id / run_id / index / type / model / tool / status / input / output / started_at / finished_at`；step/run identity 与非负 index 严格校验，model/tool step 必须绑定对应 identity，pending/running/五种终态的起止时间一致性 fail closed，非终态不得伪造 output。

输入输出只接受 JSON primitive、字符串键 mapping 与 list/tuple；构造时递归脱离调用方并用只读 mapping/tuple 深冻结，拒绝非有限浮点、非字符串键、非 JSON 对象、循环引用及超过 32 层的嵌套，`as_dict()` 每次返回可 JSON 化的全新副本。E-02 不创建 step、不校验跨对象 index 唯一性、不接管现有请求/工具执行、不实现 E-04 状态转换，也不接数据库、Redis、Repository 或 D-09 sidecar。E-02 最终精确 HEAD 远端 gate 关闭后才开始 E-03。

E-02 AgentRun/Step 定向 `88 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `642 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `65f2b3356c6bddd4e3c656adee9a55ff07058c6e1f4a62b40adb741a52f1f693`、sdist SHA256 `ed08e853c076872c69b20b70992c5aa7d78a9c3cf3ec216f0537fbe6e3c591cf`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged `AgentStep` 深冻结与序列化均通过。最终文档闭环 HEAD `8ca202ef0c53355567f44c740dd31f006377e72c` 对应 push run `32445217116` / PR run `32445220594`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-03 依赖已解除；未合并、未发布、未部署。

---

# 9. ToolCall

独立对象：

```python
ToolCall:
    tool_call_id
    run_id
    step_id

    tool_name
    bundle_digest
    arguments

    status
    confirmed

    result
    elapsed
```

E-03 实现提交 `2b4af0ea847f18b074ade33f9f6abcb0520ce1cf` 在 `agent_runtime.py` 新增 frozen `ToolCall` 与独立 `ToolCallStatus`。对象精确携带 `tool_call_id / run_id / step_id / tool_name / bundle_digest / arguments / status / confirmed / result / elapsed`；工具名沿用统一安全命名规则，可选 bundle digest 只接受 64 位小写 SHA-256，identity、枚举和布尔字段均拒绝宽松转换。状态覆盖 `pending / waiting_confirmation / running / completed / failed / cancelled / timed_out / rejected`；等待确认时不得伪造 confirmed，非终态不得携带 result/elapsed，completed 必须携带 result，所有终态必须有有限非负 elapsed。

arguments 必须是字符串键 JSON object，arguments/result 复用 E-02 的有限、无环、最多 32 层 JSON 边界；构造时递归脱离调用方并深冻结，`as_dict()` 返回可 JSON 化的新副本。E-03 只定义工具调用领域记录及固有不变量，不创建调用、不校验跨 AgentRun/AgentStep 的引用完整性，不接管 handler、Bot/Event、真实执行、PendingAction、数据库、Redis、Repository 或 D-09 legacy sidecar，也不提前实现 E-04 状态转换。

本地门禁：Agent runtime 定向 `118 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `672 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `2c18d53efad7d0fc2927d3d7949163aac1f3ea8960c18a56f46773119bcc2718`、sdist SHA256 `6472298ceee587548ad82978a110edcc639640ac09142b7b318c6daa2c2d25c7`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged `ToolCall` 深冻结与序列化均通过。最终文档闭环 HEAD `69fbf5e76e5c74f6f5b35df23c3d310830c84976` 对应 push run `32447053702` / PR run `32447055942`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-04 依赖已解除；未合并、未发布、未部署。

---

# 10. Agent Runtime 状态机

推荐：

```text
CREATED
   ↓
ADMITTED
   ↓
CLASSIFYING
   ↓
PLANNING
   ↓
EXECUTING
   ↓
WAITING_CONFIRMATION
   ↓
SUMMARIZING
   ↓
COMPLETED
```

异常：

```text
FAILED
CANCELLED
TIMED_OUT
REJECTED
```

E-04 实现提交 `94a54a7196f7ede832490191f5dc15ae2999c2dc` 在 `agent_runtime.py` 新增纯、无内部状态的 `AgentStateMachine`。转换表严格执行 `CREATED → ADMITTED → CLASSIFYING → PLANNING → EXECUTING → SUMMARIZING → COMPLETED` 主链；确认是可选分支，`EXECUTING` 可进入 `WAITING_CONFIRMATION`，等待后可恢复 `EXECUTING` 或直接进入 `SUMMARIZING`。`FAILED / CANCELLED / TIMED_OUT / REJECTED` 可从任何非终态进入，所有终态均无出边；跳级、自循环与终态重入 fail closed。

`allowed_targets()` 返回不可变目标集合，`can_transition()` 只接受强类型枚举，`transition()` 不原地修改 run，而是保留全部 identity/generation/started_at 并返回新的 frozen `AgentRun`。进入终态必须由调用方显式提供有限且不早于 started_at 的 `finished_at`，进入非终态不得伪造结束时间；策略不读取墙钟，不保存转换历史或 live Bot/Event，不提供跨请求/进程 CAS，不接管 request manager/chat runtime，也不引入 Repository、PostgreSQL、Redis、迁移、生产配置或 D-09 sidecar。

本地门禁：Agent runtime 定向 `166 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `720 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `0a29b10c7541abe151f625d52df21d9fbf142950317dd08ec5379ce366bf2bf2`、sdist SHA256 `d53f3b303f13438b82c0e19830f950eec3737784fa925c6b483c0a25e428f476`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 正常链/确认回路/终态重入拒绝均通过。最终文档闭环 HEAD `ba72157e323a1e95d99ba5a1516b40b7b0e56c0e` 对应 push run `32448482843` / PR run `32448485118`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-05 依赖已解除；未合并、未发布、未部署。

---

# 11. Deadline Runtime

不要继续层层单独 timeout。

引入：

```python
DeadlineContext:
    deadline_at

    def remaining():
        ...
```

例如整个请求：

```text
180 sec
```

所有组件共享剩余预算。

E-05 实现提交 `4bb8b30b57a9fcb7a4f4f8873281ce8dfdecdd47` 在 `agent_runtime.py` 新增 frozen `DeadlineContext`。唯一持久字段 `deadline_at` 是与 `time.monotonic()` 同源的有限非负绝对截止点；`from_timeout(timeout)` 在请求入口把有限非负总秒数转换为截止点，`remaining()` 每次只读取一次当前 monotonic 值并返回共享剩余秒数，过期后一律钳制为 `0.0`。布尔值、字符串、负数、NaN、无穷和加法溢出均 fail closed；测试可通过显式 `now` 注入确定性时钟值。

该对象用于显式向后续组件传递同一总预算，不保存 clock callable、不创建分层子预算、不自动延长截止点，也不序列化或跨进程重启持久化 monotonic 值。本阶段只定义预算契约，尚未改写 `llm_api`、MCP、网络解析、工具执行等既有 timeout 调用链，不接管 request manager/chat runtime，不引入 Repository、PostgreSQL、Redis、迁移、生产配置或 D-09 sidecar。

本地门禁：Agent runtime 定向 `185 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `739 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `12453d2f5cee45ebb6e1437bce761232f9812c4d8caf607a287ee430fec6a355`、sdist SHA256 `9b679b8f28c2a6a9a0c0ad96d20d771d9cffcaffdcde346c89e384d067cbe1c7`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 确定性/默认 monotonic 预算与过期钳零均通过。最终文档闭环 HEAD `3ac210b28ecda3019b822bf961196f63cfd795ce` 对应 push run `32449378433` / PR run `32449381716`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-06 依赖已解除；未合并、未发布、未部署。

---

# 12. 模型 Capability

当前：

```text
selected_model
vision_model
category_model
summary_model
```

后续可演进为模型能力选择。

---

## 12.1 ModelCapability

```python
ModelCapability:
    text
    vision
    tools
    json_schema
    reasoning
    streaming
```

---

## 12.2 Model Limits

```python
ModelLimits:
    context_window
    max_output
```

---

## 12.3 Model Cost

```python
ModelCost:
    input_per_million
    output_per_million
```

---

# 13. 自动模型选择

需求：

```text
vision
+
tools
+
context >= 128k
```

Selector 自动选择满足条件的模型。

以后 MoE 可以从：

```text
difficulty → model
```

升级到：

```text
capability
cost
latency
quality
availability
```

多因素路由。

---

# 14. Runtime API

建议新增内部管理 API。

```http
GET /runtime/status
GET /runtime/generation

GET /tools
GET /tools/{name}

GET /tool-bundles
GET /tool-drafts

GET /agent-runs
GET /agent-runs/{id}

GET /models
GET /metrics
```

危险写操作：

```http
POST /tool-drafts/{id}/approve
POST /tool-bundles/{id}/activate
POST /agent-runs/{id}/cancel
```

必须鉴权。

---

# 15. Web 管理面板

有 Runtime API 后再做 UI。

可展示：

```text
运行 generation
当前请求
请求队列
Tool Bundles
Tool Drafts
Tool 风险
模型状态
MCP 状态
Token
Latency
Failure Rate
```

---

# 16. 可观测性

所有关键日志统一字段：

```text
request_id
run_id
step_id
tool_call_id
generation
user_id
group_id
model
tool
```

---

## 16.1 Metrics

建议：

```text
llm_request_duration
classification_duration
queue_duration

tool_wait_duration
tool_execution_duration
tool_failure_total

token_input
token_output
cost

cache_hit
cache_miss

reload_success
reload_failure
```

---

# 17. Audit Event

统一审计：

```python
AuditEvent:
    event_id
    type

    actor
    target

    run_id
    tool_call_id

    timestamp
    metadata
```

关键事件：

```text
tool_draft_created
tool_approved
tool_activated
tool_deactivated
tool_rollback

mutating_confirmed
mutating_executed

runtime_reload
runtime_reload_failed
```

---

# 18. Tool Trust Level

建议增加：

```text
TRUSTED
REVIEWED
UNTRUSTED
EXTERNAL
```

映射：

```text
register_tool  → TRUSTED
custom_file    → REVIEWED
generated      → UNTRUSTED
mcp            → EXTERNAL
builtin        → TRUSTED（仅指本地 Provider 代码）
nonebot_plugin → REVIEWED（默认，显式 register_tool 后按 TRUSTED）
```

trust level 描述可执行代码/适配器的来源，不代表返回数据可信。例如 `web_search` 的 Builtin Provider 代码可为 TRUSTED，其网络 observation 仍必须标记 external/untrusted data provenance。执行策略在 D-06 依 trust level 收紧，D-01a 只定义身份不做 enforcement。

---

# 19. Plugin Integration 演进

NoneBot 插件注册建议统一：

```python
register_tool(
    ToolSpec(...)
)
```

而不再依赖模拟 event。

对于必须调用 NoneBot Bot/Event 的能力：

```text
trusted registered tool
```

在主进程执行。

Generated Tool 永远不获得真实：

```text
Bot
Event
DB Session
Secrets
```

---

# 20. 工具返回类型扩展

当前 ToolResult：

```text
text
images
metadata
```

后续可以：

```python
ToolResult:
    text
    images
    files
    structured
    citations
    metadata
```

这样工具无需把结构化结果强行 stringify。

---

# 21. Structured Tool Output

例如：

```json
{
  "structured": {
    "temperature": 26,
    "condition": "rain"
  }
}
```

模型可以消费 JSON。

历史记录可以只存摘要。

---

# 22. Agent Runtime 与数据库解耦

Plan 2 只定义领域对象和 Repository Interface。

不要在 Agent Runtime 里直接：

```python
await db.execute(...)
```

而是：

```python
await run_repository.create(...)
```

Plan 3 再实现 PostgreSQL Repository。

F-01 实现提交 `71c9b4ceafe6bc5e4a0d16349d28bb7375f8dbbb` 新增独立 `repositories.py`，以 `Protocol` 固化 `Conversation / Message / AgentRun / AgentStep / ToolCall / Tool / Usage / Audit` 的异步 Repository 端口与显式 `RepositoryTransaction`。`RepositoryPageRequest` 把 limit 限定为 1～200，并只接受最长 512 字符的安全 opaque cursor；`RepositoryPage` 使用 tuple 和 frozen dataclass，空页不得伪造 next cursor。AgentRun replace 必须同时提供 `expected_state + expected_generation`，ToolCall replace 必须提供 `expected_status`；实现可用 `RepositoryConflictError` 与 `RepositoryUnavailableError` 区分乐观并发冲突和后端不可用。

F-01 只固化 backend-neutral 接口，不提供实现，不在运行时导入 Agent 类型，不安装 SQLAlchemy/asyncpg/Alembic，不建表、不迁移、不创建 engine/session、不读取生产配置，也不连接 PostgreSQL 或 Redis。Repository + Agent Runtime 定向 `216 passed`，联合 Graph/Scheduler/Conflict 为 `341 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `895 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、format/diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `373dcba1bcc9c782d933400dad2ac1215c3c754da455f02b17aae19211a76889`、sdist SHA256 `61c773e8780aade1766ac257f8d05f015851495ff307865ef386492ae4d8d9ff`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged Repository Protocol/分页/CAS 签名均通过。最终文档闭环 HEAD `678adb423e87fef8a851a8a792ae9c39a268dc15` 对应 push run `32455829891` / PR run `32455828489`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-02 依赖已解除；未合并、未发布、未部署。

F-02 实现提交 `cf7c236c3f78c0775ff513f291ef4a55a877e54d` 新增 `database_engine.py` 与 SQLAlchemy 2.x/asyncpg 运行依赖。`DatabaseEngineSettings` 只接受显式 `postgresql+asyncpg` URL 和数据库名，拒绝控制字符、超长 DSN 与 query 凭据字段；原始字符串不持久保留，私有 URL wrapper、`repr()`、错误与 `safe_diagnostics()` 均不渲染凭据或 endpoint。pool size 1～100、overflow 0～100 且总量不超过 150，pool/connect/statement/recycle timeout 均有界；engine 固定 `pool_pre_ping / pool_use_lifo / hide_parameters`，asyncpg 同时获得 connect、command 和服务端 statement timeout。

`DatabaseEngineManager` 在运行中 event loop 内同步、惰性且至多一次创建 `AsyncEngine`，不 checkout 连接；同一 manager 绑定创建时 PID 与 loop，跨进程/loop、释放中访问和重复释放并发均 fail closed。成功 dispose 后允许安全重建，取消或释放失败保留可重试状态；初始化/释放错误只记录异常类型，不串联可能含 DSN 的原异常。本阶段不创建全局 manager，不读取插件 JSON/环境变量/secret file，不注册 startup/shutdown，不调用 `connect()`，不创建 session、Repository 实现、Schema 或 Alembic，也不触碰 D-09 sidecar。

F-02 定向 `50 passed`，与 Repository/Agent/Graph/Scheduler/Conflict 联合 `391 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 最终普通全量各 `945 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、format/diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `c5c59aa4c556a4f16bb98a27c979ee174b2d1e46c5695f5ce596a7c617416c8c`、sdist SHA256 `c932056e00dbada7d55ab07f196996edc6daf3d6b6a5f3a31da6a09e79e4732f`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、依赖元数据、凭据脱敏、惰性单 engine 与 `checkedout=0` 均通过。最终文档闭环 HEAD `f8292f94c2dbeab80949436b495ee997382b5cac` 对应 push run `32458307603` / PR run `32458311280`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-03 依赖已解除；未合并、未发布、未部署。

F-03 实现提交 `f9598561247e40a5ce8327a0ccd8d9f21f3fe04e` 新增 `alembic>=1.13,<2`、共享空 `database_metadata`、可打包 migration 模板与 `database_migrations.py`。内存 Alembic config 不读取 ini、环境变量、插件配置、secret file 或 `sqlalchemy.url`；revision 标识有界，graph 必须是无 merge、branch label、`depends_on` 的单一线性 base/head。离线 renderer 只接受显式 upgrade 范围；空图在入口与 env 双重短路，兼容 Alembic 1.13 而不生成误导性的 `DROP TABLE alembic_version`。在线路径无条件抛出 `DatabaseMigrationOnlineDisabledError`。

本阶段没有 revision、业务表、ORM model、Repository 实现、engine/session 或生命周期接线，不调用 F-02 manager，也不连接 PostgreSQL/Redis。F-03 定向 `26 passed`，与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `417 passed`；四版本最终普通全量各 `971 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff、format/diff check 与 Pyright 目标文件零诊断。fresh wheel/sdist SHA256 为 `834c709638f6b618a4765ed5ad490678405bd761dcc61aa172290648701bba23` / `7a39dc3b3aaaa2c55e8d205dd7a555fcb3cae12338e09bbf1f42c7172b472935`，Twine 与 Python 3.10/3.12 × wheel/sdist 四组仓库外加载、Alembic 依赖、四份迁移资源、空 graph 和零 SQL 均通过。最终文档闭环 HEAD `a4eb771678587e6bfd32f793c8a6f7eda88f29ab` 对应 push run `32461256977` / PR run `32461262286`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-04 依赖已解除；未合并、未发布、未部署。

F-04 实现提交 `21810cf836d89d07c268076d6e3d96b34cdfd04b` 新增纯声明式 `database_schema.py` 与首个 revision `0001_users_conversations`。`users` 使用应用生成的有界 ID，并以 `(platform, platform_user_id)` 唯一标识平台用户；`conversations` 为群聊/私聊范围提供 partial unique index；`messages.id` 使用 PostgreSQL `BIGINT IDENTITY`，`structured_content` 使用 JSONB，并提供 `(conversation_id, id DESC)`、`created_at` 及会话内平台消息 ID 索引。时间字段均带时区，外键删除策略均为 `RESTRICT`；metadata/revision parity、离线 upgrade 与逆依赖 downgrade 顺序均有契约测试。

F-04 四版本定向各 `32 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `423 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 最终普通全量各 `977 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，Ruff、format/diff check 与 Pyright 目标/测试文件零诊断。fresh wheel/sdist SHA256 为 `7c22c093605dd4213e8c5bb8751fa81f26925cc45e56d391366016762d679739` / `b5b52c0125fea1a9f565e531241fb19fd20bfb2a89e00d325a28a47d9b88c262`，Twine、制品内容检查及 Python 3.10/3.12 × wheel/sdist 四组仓库外 Schema/graph/DDL smoke 均通过。最终文档闭环 HEAD `9a343cfcc71a2824257afd9f7537edf4ab8af4f2` 对应 push run `32463913845` / PR run `32463917189`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / CLEAN`。F-05 依赖已解除；未合并、未发布、未部署。

F-05 实现提交 `c177fc51e73b3961617cc2b09082ceeb0e436897` 追加不可变 revision `0002_agent_runtime` 与 `agent_runs` 表。字段覆盖 `request/user/group/conversation/generation/model/status/time/token/cost/error`；状态集合精确绑定现有 `AgentRunState`，终态必须有结束时间，非终态不得伪造结束时间。`request_id` 因进程重启后可能复用而不设唯一约束；用户和会话外键均为 `RESTRICT`。会话时间线、用户时间线与状态恢复查询各有有界索引，`generation + status` 列为后续 Repository CAS 保留，但本阶段不实现 Repository。

F-05 四版本定向各 `35 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `426 passed`；四版本普通全量各 `980 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，Ruff、format/diff check 与 Pyright 目标/测试文件零诊断。fresh wheel/sdist SHA256 为 `1d47c5d1eee9686c8e642fb8d52bfb50e01894a2a71c7ba560ac16df5d8c8f8b` / `41a8d65dd33c8343bcb64e91444e220099bcdf1a66fe210d6974f4f3cb5de2f1`，Twine、制品内容检查及 Python 3.10/3.12 × wheel/sdist 四组仓库外 AgentRun Schema/graph/DDL smoke 均通过。最终文档闭环 HEAD `d23e156e4df44442bc9b7382fef5e53c88433148` 对应 push run `32465645519` / PR run `32465649984`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / CLEAN`。F-06 依赖已解除；未合并、未发布、未部署。

F-06 实现提交 `ea405674e38082a5089304789a1628024da7d2ec` 追加不可变 revision `0003_agent_steps` 与 `agent_steps` 表。字段覆盖 step identity/type/status/model/tool、带时区起止时间、毫秒 duration 及有界 input/output/error preview；不持久化完整领域 JSON。`(run_id, step_index)` 唯一约束同时支持稳定顺序读取，run 外键为 `RESTRICT`。数据库约束精确执行 pending/running/terminal 时间语义、MODEL/TOOL identity 要求、非终态不得有 output 及 completed 不得伪造 error。

F-06 四版本定向各 `38 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `429 passed`；四版本普通全量各 `983 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，Ruff、format/diff check 与 Pyright 目标/测试文件零诊断。fresh wheel/sdist SHA256 为 `edcaac6c69f337d70b078dc0679b360db6bcc9d5228c39606380fbb0d2afeb80` / `b6dffce54dfed796625707e932881d996a52f6759e63e62752fd04903d1e5a26`，Twine、制品内容检查及四组仓库外 AgentStep Schema/graph/DDL smoke 均通过。最终文档闭环 HEAD `4e5cd600b1efa430bb785bdc5cb7f6a49988be9a` 对应 push run `32467140779` / PR run `32467144569`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / CLEAN`。F-07 依赖已解除；未合并、未发布、未部署。

F-07 实现提交 `83a571fbc79e13ce68f237ba7ed9c653607fbb66` 追加不可变 revision `0004_tool_calls` 与 `tool_calls` 表。字段覆盖 run/step/tool/source/bundle、JSONB arguments、有界 result preview、确认 identity、状态与时间；status/source 值域精确绑定现有 `ToolCallStatus / ToolSource`。Generated 来源必须绑定合法 bundle ID 与 64 位小写 digest，其他来源禁止伪造 bundle identity；等待确认与 confirmed 记录必须绑定唯一 confirmation ID。

F-07 通过 `(run_id, step_id) → agent_steps(run_id, id)` 复合外键拒绝跨 run 错挂 step，并在新 revision 中为父键追加支持约束，不回改 `0003`。终态必须携带结束时间与非负毫秒 duration，非终态不得携带结果，completed 必须有最长 6000 字符的 result preview；run 时间线、step 时间线、状态恢复及 confirmation 唯一索引均已声明。四版本定向各 `41 passed`，联合回归 `432 passed`，四版本普通全量各 `986 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff、format/diff check 与 Pyright 零诊断。fresh wheel/sdist SHA256 为 `2ce8bf699fe7919cfca345d90833be99e2453e5025adf9513a9d230eb96f4b4f` / `54a6a8f44d3da165c7fe34704d60d4765164877a4c7b6fbc48c605957f819424`，Twine、制品内容检查及四组仓库外 ToolCall Schema/graph/DDL smoke 均通过。最终文档闭环 HEAD `dcff410498a862bed302687e1383cab0f554da6c` 对应 push run `32469057942` / PR run `32469061094`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-08 依赖已解除；未合并、未发布、未部署。

F-08 实现提交 `7afa3c81a6604a09533b0b1b487d3c484f9f1909` 追加不可变 revision `0005_tool_bundle_metadata` 与 `tool_bundles / tool_bundle_versions` 两张表，不回改 `0001`～`0004`。版本状态精确绑定现有 `VersionState` 的 `approved / activated / deprecated / archived`，并补齐 `archived_at` 以执行当前领域生命周期；manifest、risks、capabilities 使用有界 JSONB，源码与测试源码各限制为 64 KiB。

bundle natural identity 与 digest 组合唯一；`active_version_id` 通过 `(bundle_id, active_version_id) → tool_bundle_versions(bundle_id, id)` 复合 `RESTRICT` 外键拒绝跨 bundle 指针，partial unique index 保证每个 bundle 至多一个 activated 版本。数据库约束同时执行 manifest bundle identity、时间顺序和各状态时间字段组合；本阶段只声明 metadata 与离线 DDL，不接 sidecar、runtime 或 Repository。

F-08 四版本定向各 `44 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `435 passed`，四版本普通全量各 `989 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff、format/diff check 与 Pyright `0 errors, 0 warnings`。fresh wheel/sdist SHA256 为 `441964bdd651746d1a61eadea63ea389ea40e42fd0bfe3599d1921ecc93230cf` / `6968f782e685c7fdfc55b5fa2d3c456d0a86f8dbb677c262ca9bd5639d60ee92`，两种制品各 66 文件，Twine、内容检查及 Python 3.10/3.12 × wheel/sdist 四组仓库外 8 表/5 revision/DDL/downgrade/reload smoke 均通过。当前仅本地门禁完成，F-08 精确 HEAD 双 run gate 待完成，F-09 继续锁定；未创建全局 engine/session，未读取 DSN，未运行 migration，未连接 PostgreSQL/Redis，未合并、未发布、未部署。

---

# 23. 计划二验收标准

- [ ] ToolProvider 接口
- [ ] Tool Capability
- [ ] Tool Trust Level
- [ ] AgentRun
- [ ] AgentStep
- [ ] ToolCall
- [ ] DeadlineContext
- [ ] Tool Graph
- [ ] read_only 并行工具
- [ ] ModelCapability
- [ ] capability based routing
- [ ] Runtime API
- [ ] structured audit
- [ ] structured metrics
- [ ] ToolResult structured output

---

# 24. 推荐版本拆分

## 0.26

- Provider discovery contract 与 shadow parity
- ProviderRegistry / ToolSnapshot v2 dual view
- Capability 版本化扩展
- Trust Level enforcement
- `DiscoveredTool` 统一，`ToolArtifact` 按来源可选

## 0.27

- AgentRun
- AgentStep
- ToolCall
- Tool Graph
- Deadline

## 0.29

- 并行工具
- 复杂 Agent Workflow
- Structured Tool Output

## 0.30

- Runtime API
- Web Admin
- Observability

---
title: 02-plan-future-architecture
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-20T00:00:00+00:00
---

# 02-plan-future-architecture

# Plan 2：后续功能与架构优化

> 推荐目标版本：`0.26 → 0.30`

> 实施门禁（2026-08-20）：Plan 1 精确 HEAD `f6c7628025cb5d34519499d86b979de448406d5b` 的 push/PR `release-gate` 均已 green，PR 基分支 required check 已配置，Plan 2 门禁解除。D-01a、D-02、D-01b、D-03、D-04、D-05、D-05a 与 D-05b 已完成精确 HEAD 远端 gate。D-06 Tool Trust Enforcement 实现提交 `5d7b7a6734ba14650ef892dfffee369c88e8a4f4` 已完成本地总门禁，精确 HEAD 远端 gate 待完成；D-07 继续等待该依赖。逐项状态见 [Plan 1 完成审计](./05-plan1-completion-audit.md) 与 [实施 Backlog](./04-implementation-backlog.md)。

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

D-06 实现提交 `5d7b7a6734ba14650ef892dfffee369c88e8a4f4` 新增 frozen `ToolTrustPolicy` / `ToolTrustDecision`，并由每个 `ProviderCatalogSnapshot` 为全部发现工具派生完整、不可变、与精确 generation / `ToolSpec` identity 绑定的 trust policy。六类来源固定映射到 in-process、isolated artifact、generated sandbox、external proxy 与 bounded event 五类执行边界；MCP 与 `web_search` 结果标记 external，Generated 结果标记 untrusted，其余结果保持 unverified，避免把适配器代码 trust 错传给返回数据。selection 强制 effective permission，management 只允许超级用户，mutating execution 要求已完成二阶段确认；未显式注册的 NoneBot 遗留适配器保留有界 compatibility 例外并强制审计，不在本阶段改变现有权限/确认语义。拒绝、所有执行/管理、非 trusted selection 与外部结果 selection 均要求审计，`audit_metadata()` 只含固定身份/策略字段，不含参数或结果。当前 consumer 仍全部读取 legacy 视图；没有切换真实执行、pending action、search 或管理命令，没有引入 D-07 capability merge，也未修改 `ToolSnapshot` / `RuntimeSnapshot` dataclass schema。D-06 定向 `98 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4）与 3.13.13 普通全量各 `418 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 与 Pyright `0 errors, 0 warnings`。fresh wheel/sdist、Twine 与 checksum 通过，wheel SHA256 `ee916eac21ed6e744b29adc0816c8e3886238a170c4e7aa39c8f2306317a79a9`、sdist SHA256 `b9f448b8977699616685eefefd753fe7b12068d7b6c29dc05c8ad07001f79817`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")` 及 packaged trust policy 检查全部通过。精确 HEAD 远端 gate 待完成，未部署。

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

---
title: 06-plan2-plan3-completion-audit
date: 2026-08-23T11:38:16+00:00
lastmod: 2026-08-23T12:09:24+00:00
---

# Plan 2 / Plan 3 完成度审计与最终集成顺序

## 1. 审计结论

截至 `66df2100cf5c0aaf209d0ae973f4524a75158aba`：

- Plan 1 / Milestone A～C 已完成，既有安全门禁不得回退。
- D-01a～D-08f、E-01～E-08、F-01～F-14、G-01～G-10 与 H-01～H-08 的既定增量 primitive 均有本地和精确 HEAD 双 run 证据。
- H-08 最终闭环 push run `32636423646` 与 PR run `32636425880` 均精确命中上述 SHA、各 11/11 success、`non_success=[]`，各恰好一个成功 `release-gate`；本地、origin、`ls-remote` 与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。
- 上述证据只证明已拆分 primitive 的质量，不等于 Plan 2 / Plan 3 的最终运行态验收。真实聊天路径仍未创建 Agent 领域记录，也未消费多数 G/H 阶段能力。
- D-09 仍要求至少一个真实发布周期的 Provider/legacy parity 观察。本任务禁止生产操作，因此 D-09 必须继续锁定；本地测试或 CI 不能替代发布观察。

因此，H-08 是“已规划 primitive gate”的终点，不是总体目标终点。后续以 Milestone I 完成开发仓库内的运行态集成；生产迁移、发布、部署和 D-09 观察仍属于独立的生产门禁。

### Milestone I 进展（2026-08-23）

- 规划审计基线 HEAD `56a038406d13d167de433271487af9b972d6402a` 的 push `32637481777` / PR `32637485121` 均为 11/11 success、`non_success=[]`、唯一 `release-gate` 成功；四方 HEAD 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。
- I-01 实现提交 `4a643e062b83055722351df12d402e518dc51b51` 已完成纯 stdlib Model Capability Domain、本地四版本/最低依赖/Sandbox/静态/制品/包外零 I/O 门禁；精确 HEAD 双 run 待关闭，I-02 仍锁定。
- I-01 不读取或改变现有模型配置，不包含 endpoint/key/proxy/credential，不接 selector/runtime，也不发送模型请求。

## 2. 状态口径

后续规划统一采用三层证据：

1. **Primitive**：领域对象、接口、Schema、独立服务或执行器可单独构造并通过门禁。
2. **Runtime integration**：真实聊天/runtime 路径实际创建或消费该能力，具备明确生命周期、失败策略和兼容边界。
3. **Release observation**：部署、在线迁移、真实后端与发布周期行为。

Plan 2 / Plan 3 验收清单中的 `[x]` 只表示前两层均已完成并验证。只完成 Primitive 的项目保留既有提交和门禁证据，但仍显示 `[ ]`。第三层不在本轮授权范围内，不得为了勾选而操作生产。

## 3. 源码证据

| 能力 | 已有证据 | 当前缺口 |
| --- | --- | --- |
| Provider / Capability / Trust | D-08 已将 categorize、payload、tool execution、pending action、search 与管理 consumer 切到 generation-bound Provider 视图，并保留 parity rollback | D-09 legacy 删除仍受生产发布周期门禁锁定 |
| AgentRun / AgentStep / ToolCall / Deadline | `agent_runtime.py` 已有不可变对象、状态机与共享 deadline；F 阶段已有 Schema | `__init__.py`、`chat_runtime.py`、`llm_tools.py` 不构造这些对象；领域字段与 Schema 仍不完全对齐 |
| Model Capability / Routing | 现有 `ModelSelector` 支持固定 selected/vision/category/summary 与难度映射 | 全仓没有 `ModelCapability / ModelLimits / ModelCost`，没有 capability/cost/limits 路由 |
| Structured ToolResult | 当前 `ToolResult` 仅有 `text / images / metadata` | 缺 `files / structured / citations`，现有 adapter 与模型消息没有统一结构化消费契约 |
| Agent persistence | 已有 `AgentRunRepository / AgentStepRepository / ToolCallRepository` Protocol 与三张表 | 没有对应 PostgreSQL Repository；Agent 领域字段缺少部分 Schema identity、时间、成本和错误边界 |
| History / Summary / Long-Term Memory | G-01/G-02/G-03/H-08 各自具备脱离态实现 | 真实 `MessagesHandler` / prompt 编排未消费，未定义组合失败策略 |
| Parallel execution / Runner pool | G-09/G-10 已有脱离态 executor/pool | `_execute_tools()` 仍固定 `max_tool_calls_per_round = 1` 并逐个执行 |
| Logging / Metrics / API / Admin | H-01～H-07 已有显式注入的 API、Web、日志和指标 primitive | 没有模块级运行资源、挂载和生命周期；现有 runtime 未写 Full Metrics 或 structured log |
| Database / Redis failure policy | 各 backend primitive 对自身错误 fail closed | DB failure spool、Redis 组件级组合降级与 database metrics 尚未实现 |

## 4. Plan 2 当前状态

### 已接真实运行路径

- ToolProvider consumer cutover
- Tool Capability versioning / merge / enforcement
- Tool Trust Level enforcement

### Primitive 已完成、最终集成未完成

- AgentRun / AgentStep / ToolCall
- DeadlineContext
- Tool Graph / read-only scheduler / parallel executor / trusted runner pool
- Runtime API / Web Admin
- structured audit / structured logging / Full Metrics
- Long-Term Memory retrieval boundary

### 尚缺核心实现

- ModelCapability / ModelLimits / ModelCost
- capability-based model routing
- 完整 structured ToolResult
- Agent runtime 对上述能力的真实消费

## 5. Plan 3 当前状态

### Schema 或独立 backend 已完成

- PostgreSQL 基础 Schema 与 append-only Alembic graph
- Redis client、PendingAction、cooldown、admission primitive
- Conversation/Message、Session Summary、Usage、Audit 的具体 PostgreSQL Repository
- History、Tool Catalog、Tool Schema、Classification cache primitive
- Usage/Audit batch queue

### 最终集成未完成

- AgentRun / AgentStep / ToolCall 具体 PostgreSQL Repository
- history/hot cache/summary/long-memory 与聊天路径编排
- token usage、audit batch 与真实 LLM/tool 路径编排
- Redis client 与各 Redis store 的统一生命周期和故障组合
- DB failure spool 与可证明的不重复 flush 协议
- database metrics
- read-only parallelism 的真实 runtime 接线

## 6. Milestone I：Plan 2 / Plan 3 Completion

严格依赖顺序如下：

```text
规划审计基线双 run gate
  → I-01 Model Capability Domain
  → I-02 Capability-based Model Routing
  → I-03 Structured ToolResult
  → I-04 Agent Domain / Schema / PostgreSQL Repository Alignment
  → I-05 Runtime Resource Composition and Lifecycle
  → I-06 Agent / History / Summary / Long-Memory Runtime Wiring
  → I-07 Read-only Parallel Runtime Wiring
  → I-08 Audit / Logging / Metrics / API / Admin / Failure Policy Wiring
  → I-09 Final Matrix and Remote Closure
```

### I-01 Model Capability Domain

- 新增独立、无凭据、深度不可变的 `ModelCapability / ModelLimits / ModelCost / ModelDescriptor`。
- capability、limits、cost 和 availability identity 必须 canonical、可摘要、可安全序列化。
- 成本使用精确十进制语义，不使用二进制浮点计价；窗口和输出限制必须有界。
- 本阶段不读取配置、不发模型请求、不改变现有选择结果。

### I-02 Capability-based Model Routing

- 用显式 request requirements 选择满足 text/vision/tools/json-schema/reasoning/streaming 和 context/output limits 的候选。
- 决策稳定绑定 generation、policy version 与 capability digest；按 capability、availability、质量、延迟、成本的明确顺序选择。
- 缺能力、目录漂移或未知可用性 fail closed；保留现有固定模型选择的显式兼容策略和回滚边界。

### I-03 Structured ToolResult

- 扩展为 `text / images / files / structured / citations / metadata`，所有集合和 JSON 递归脱离、冻结并有界。
- 文件只允许 opaque、安全 locator，不允许把任意主机路径变成模型数据。
- adapter、runner、历史 preview 与模型 payload 使用同一 canonical rendering；旧 `text/images/metadata` 构造保持兼容。

### I-04 Agent Domain / Schema / PostgreSQL Repository Alignment

- 对齐 AgentRun 的 conversation/model/token/cost/error，AgentStep 的持久化 preview/error，ToolCall 的 source/bundle/confirmation/time identity。
- 实现三类显式 `AsyncSession` PostgreSQL Repository，不拥有 commit/rollback/close，不自动重放未知结果。
- 只追加必要 migration；若现有 Schema 已足够则不得制造空 revision。
- 用 keyset/CAS、复合 identity 和有界字段拒绝跨 run/step 错挂与陈旧替换。

### I-05 Runtime Resource Composition and Lifecycle

- 建立显式 runtime resource container，组合 snapshot、repository、cache、queue、metrics、logger、API 与 runner ports。
- 默认安全兼容模式不得连接 PostgreSQL/Redis；只有显式配置通过校验后才允许惰性创建资源。
- startup/shutdown 次序、部分初始化回滚、取消、重复关闭与 reload generation 切换必须确定且可测试。

### I-06 Agent / History / Summary / Long-Memory Runtime Wiring

- 真实聊天入口创建 AgentRun/Step/ToolCall，并经状态机与单一 DeadlineContext 驱动。
- 编排 committed history、hot cache、session summary、long-memory prompt、usage 与 audit；明确每类后端不可用时的继续、降级或拒绝语义。
- 默认 Memory 兼容模式保持现有用户行为；持久化写入的 durable commit 与 cache invalidate 顺序必须可证明。

### I-07 Read-only Parallel Runtime Wiring

- 真实工具路径只对已完成 trust/capability/confirmation 检查的强类型 read-only DAG 启用 G-09/G-10。
- mutating、未知 effect、冲突或需要确认的调用保持串行/拒绝；共享 deadline、首错取消并 drain。
- 不以提高每轮数量为理由绕过现有重复调用、结果上限、审计和 PendingAction 边界。

### I-08 Platform and Failure-policy Wiring

- 真实 Agent/LLM/tool 生命周期写入 structured audit/logging/Full Metrics，并让 H-04 读取一致 generation 的指标。
- 显式挂载 H-01～H-05，鉴权、scope、只读 Admin 和写操作双 CAS 语义保持不变。
- 实现有界、私有、canonical、租约化的 Usage/Audit local spool；未知 durable 结果禁止自动重放。
- 组合 Redis failure policy：危险 PendingAction store 不可用时拒绝 mutating；单实例安全组件仅在显式策略允许时降级，不能把确认降级成免确认。
- 增加低基数 database/pool/spool 指标，不记录 DSN、SQL、user/group 或 payload。

### I-09 Final Matrix and Remote Closure

- 四个 Python 版本普通矩阵严格串行；mandatory root Sandbox 单独运行且 `tests > 0 / skipped = 0`。
- 最低依赖、Ruff/Pyright、fresh wheel/sdist、Twine、制品内容和仓库外 smoke 全部通过。
- 精确 HEAD push/PR 各 11 个 job、`non_success=[]`、各恰好一个成功 `release-gate`；四方 HEAD 一致且 PR 保持可合并。
- 最终文档逐项更新 Plan 2 / Plan 3 验收状态，并明确未观察的生产迁移、发布与 D-09。

## 7. 每项门禁

每个 I 任务都必须：

1. 以前一任务最终文档 HEAD 的精确 push/PR 双 `release-gate` 为依赖。
2. 先实现与定向测试，再跑相关联合、四版本串行全量、Sandbox、静态与 fresh 制品。
3. 记录实现提交、本地证据、制品摘要、零真实 I/O smoke 和最终远端证据。
4. 未关闭本项远端门禁前，不开始下一项。
5. 不修改、暂存或提交用户未跟踪的 `uv.lock`。

## 8. 非生产边界

Milestone I 只允许修改和验证开发仓库。禁止：

- 合并、promotion、发布、部署或重启服务；
- 读取生产 DSN、Redis URL、token 或 secret；
- 连接真实 PostgreSQL/Redis、运行在线 migration 或写入真实数据；
- 用 CI、本地 smoke 或模拟数据冒充生产发布周期观察；
- 删除 D-09 legacy sidecar。

当前精确恢复点：规划审计基线双 run gate 已关闭；I-01 本地证据已冻结，先关闭 I-01 精确 HEAD push/PR 双 `release-gate`，再开始 I-02。

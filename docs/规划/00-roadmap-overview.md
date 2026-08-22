---
title: 00-roadmap-overview
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-22T23:03:27+00:00
---

# 00-roadmap-overview

# MoEllmChats 0.25+ 后续推进总路线图

> 进度注记（2026-08-22）：Plan 1 的 Milestone A、B 与 C-01～C-07、Plan 2 的 D-01a～D-08f、Milestone E 的 E-01～E-08、F-01～F-14 与 G-01 已按依赖顺序完成精确 HEAD 双 run 门禁；D-09 因缺少发布周期 parity 观察且禁止生产操作而继续锁定，G-02 依赖已解除。G-01 实现提交 `b3566d6513f142d86de91898a6c6b8f14a4e131d` 新增深度不可变 `ConversationRecord / MessageRecord` 与显式 `AsyncSession` 注入的 PostgreSQL Conversation/Message Repository；最近历史只查询显式列，以 `(conversation_id, id DESC, LIMIT+1)` 做绑定会话指纹的稳定 keyset 分页，并在应用层恢复时间正序。Repository 不创建、提交、回滚、关闭 session，不隐式重试；`RETURNING` 只确认当前事务内 statement 结果，durable commit 仍由调用方负责。Integrity 冲突、缺失 replace、未知写入/读取结果与后端不可用分开处理，错误不泄漏 endpoint、凭据或消息内容，取消原样传播。本地四版本 G-01 定向各 `36 passed`、相关联合各 `173 passed`、普通全量各 `1244 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff/Pyright、最低 SQLAlchemy/Alembic/asyncpg 兼容、fresh 制品和四组包外 10 表/7 revision/DDL/reload/零数据库 execute/connect smoke 均通过。G-01 本地证据 HEAD `d086e8ee87c5e25d8b692e8a7aadb239ef42464a` 的 push run `32593099818` / PR run `32593102078` 均为 11/11 green、各恰好一个成功 `release-gate`；远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。未读取生产 DSN、未创建全局 engine/session、未接配置、startup/shutdown、legacy sidecar、现有内存聊天路径或生产 runtime，未运行 migration，未连接真实 PostgreSQL/Redis；未合并、未 promotion、未发布、未部署。逐项证据见 [Plan 1 完成审计](./05-plan1-completion-audit.md) 与 [实施 Backlog](./04-implementation-backlog.md)。

> G-02 本地门禁（2026-08-22）：G-01 闭环文档 HEAD `11531889583fd5d11cf0871f503c6ff037c38395` 的 push run `32593312310` / PR run `32593315775` 已各 11/11 green、各恰好一个成功 `release-gate`，本地、远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。在此前提下，实现提交 `e865838` 新增 backend-neutral `HistoryHotCacheProtocol`、受 PID/event-loop 约束的 TTL/LRU Memory backend 与显式注入 redis-py client 的 Redis backend。`HistoryWindow` 只接受同会话、正 BIGINT identity、严格递增的已持久化不可变消息；miss 会先保留短期 128-bit 失效代际，只有匹配代际的 committed source window 可 CAS 发布，durable commit 后的 invalidate 会拒绝此前启动的晚到加载。Redis key 只含会话 SHA-256 指纹，wire payload 采用有界 canonical JSON、固定 TTL 与 WATCH/MULTI；损坏、超限、无 TTL 或异常响应均不作为命中，错误脱敏且取消原样传播。本地四版本定向各 `84 passed`、相关联合各 `455 passed`、普通全量各 `1328 passed, 1 skipped`，Sandbox `40 passed, 0 skipped`；最低 Redis/SQLAlchemy/Alembic/asyncpg、Ruff/Pyright、fresh 制品与四组包外零 Redis command/数据库 I/O smoke 均通过。G-02 尚待精确 HEAD 双 run 远端门禁，G-03 继续锁定；未接配置、生命周期、`MessagesHandler`、PostgreSQL Repository 或生产 runtime，未读取连接信息、未连接真实服务、未迁移、未合并、未发布、未部署。

> G-02 远端闭环（2026-08-22）：本地证据 HEAD `fca62e2a97fdb1b9fcccc5dd67dc604458d754c3` 对应 push run `32595899079` 与 PR run `32595902263`；两者均为目标 SHA、各 11 个 job 全绿、无非 success job，并各恰好一个 `completed/success release-gate`。本地、远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-02 依赖门禁已关闭，G-03 依赖已解除；未触发 promotion、合并、发布、部署或任何生产操作。

> G-03 本地门禁（2026-08-22）：实现提交 `82ddd7ae89049fd173360ee7662e6d40387156c1` 新增不可变 `SessionSummaryRecord / SessionSummaryPlan / SessionSummaryPolicy`、显式 `AsyncSession` 注入的 PostgreSQL Repository，以及 append-only `0008_session_summaries`。默认策略在 oldest-first committed 窗口达到 50 条时压缩最老前缀并至少保留最近 10 条；canonical 输入绑定前一摘要、源消息、水位与策略并以 SHA-256 固化，64,000 字符上限不足时只缩小完整源前缀，绝不截断单条消息后推进水位。摘要 chain 使用会话内 generation、前驱与水位唯一约束、防跨会话复合外键及单条条件 INSERT CAS；Repository 不拥有事务，未知写入不自动重放。四版本定向各 `103 passed`、相关联合各 `551 passed`、普通全量各 `1381 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；最低数据库/Redis 依赖、Ruff/Pyright、fresh 制品和 Python 3.10/3.12 × wheel/sdist 四组 11 表/8 revision/离线 DDL/reload/零 I/O smoke 均通过。wheel SHA256 `db83341f418b0bcf8ae87e8aad5d3c29d1e32ff2bfc4babbc223238afcaca718`，sdist SHA256 `963bc7f6513dfb3b701ba228ca6d743444bf48d50678a7193e399fe73f9ffecf`。精确 HEAD 双 run 远端门禁待完成，G-04 保持锁定；未调用摘要模型，未接配置、`MessagesHandler`、G-01/G-02 编排、生命周期或生产 runtime，未读取连接信息、未运行 migration、未连接真实 PostgreSQL/Redis，未合并、未发布、未部署。

> G-03 远端闭环（2026-08-22）：本地证据 HEAD `3fb6792ec18566c571ab9e9628c0ea9ec1854a53` 对应 push run `32598610770` 与 PR run `32598613406`；两者各 11 个 job 全绿、无非 success job，并各恰好一个 `completed/success release-gate`。本地、远端分支与 PR head 精确一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-03 依赖门禁已关闭，G-04 依赖已解除；未触发 promotion、合并、发布、部署或任何生产操作。

> G-04 本地门禁（2026-08-22）：实现提交 `aa6e7d34a8b1335c34540bb50fe93868d70bc9f1` 新增 backend-neutral `ToolCatalogCacheProtocol`、不可变 `ToolCatalogRenderContext / ToolCatalogCacheKey / ToolCatalogRecord` 与受单 PID/event-loop 约束的 Memory LRU backend。cache identity 绑定 runtime generation、`user / superuser` 两级权限、Provider cutover、Tools/Search 开关及规范化黑名单 SHA-256；原始黑名单不进入 cache key/record repr。显式 ToolSnapshot 渲染入口只在 legacy/provider parity 成功后形成 record，构建失败、错误 identity、同 key 异值、超限、跨进程/loop 与不可信 backend 响应均 fail closed；generation 变化自然 miss，旧 generation 为在途请求保留到有界 LRU 淘汰。本地四版本定向各 `161 passed`、相关联合各 `306 passed`、严格串行普通全量各 `1433 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 / FakeRedis 2.31.0、Ruff、新模块/测试 Pyright、fresh 制品与 Python 3.10/3.12 × wheel/sdist 四组 11 表/8 revision/离线 DDL/reload/cache roundtrip/零 I/O smoke 均通过。wheel SHA256 `ab805c305183bddd1e49b3e417534ca09abc4d2f4970c9df3f40d477b61c06b0`，sdist SHA256 `0348bc4627dfbb6a6d227842fcbda072724b9838cc67ef7d1607e87807d3bb37`。精确 HEAD 双 run 远端门禁待完成，G-05 保持锁定；现有 `Categorize.get_brief_catalog()` 同步路径未接 cache，未新增全局 cache、配置、生命周期或 Redis backend，未读取连接信息、未运行 migration、未连接真实 PostgreSQL/Redis，未合并、未发布、未部署。

> G-04 远端闭环（2026-08-22）：本地证据 HEAD `6fd7509f11c0a851addc93dd78e52979b436215a` 对应 push run `32600965570` 与 PR run `32600967324`；两者均为目标 SHA、各 11 个 job 全绿、无非 success job，并各恰好一个 `completed/success release-gate`。本地、远端分支与 PR head 精确一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-04 依赖门禁已关闭，G-05 依赖已解除；未触发 promotion、合并、发布、部署或任何生产操作。

> G-05 本地门禁（2026-08-22）：G-04 最终闭环 HEAD `1668a9215c7b02515147c5367798beab513c62d2` 的 push run `32601224946` / PR run `32601227942` 已各 11/11 green、无非 success job且各恰好一个成功 `release-gate`。在此前提下，实现提交 `803fddb8ed062a61bbf9b38c3eb7714e735c30b9` 新增完整 typed `ToolSchemaRenderContext / ToolSchemaCacheKey`、canonical JSON `ToolSchemaRecord`、backend-neutral Protocol、resolver 与单 PID/event-loop Memory LRU。安全 key 为 `schema:{generation}:{toolset_hash}`，其 hash 绑定初始工具集、两级权限、Provider cutover、Tools/Search 与黑名单 digest，不暴露原始工具名或黑名单；显式 ToolSnapshot builder 固定同一 policy snapshot、稳定依赖顺序并只在 legacy/provider parity 后形成 record。四版本定向各 `64 passed`、相关联合各 `369 passed`、普通全量各 `1497 passed, 1 skipped`，Sandbox `40 passed, 0 skipped`；最低依赖、Ruff/Pyright、fresh 制品及 Python 3.10/3.12 × wheel/sdist 四组 11 表/8 revision/离线 DDL/reload/schema cache roundtrip/零 I/O smoke 均通过。wheel SHA256 `8360f85f99987721d877d7f587a62e4aca9bd8adaa4d6b7a205e8c3324662e0f`，sdist SHA256 `2aa60e39a8de7f889475a834ae61494e0f8f75bd1ccdfac5363fabf41e83c4df`。精确 HEAD 双 run 远端门禁待完成，G-06 保持锁定；现有 `get_llm_payload_tools()` / `_build_payload()` 未接 cache，未新增全局 cache、Redis backend、配置或生命周期，未运行 migration、未连接真实 PostgreSQL/Redis，未合并、未发布、未部署。

> G-05 远端闭环（2026-08-22）：本地证据 HEAD `86753abc14266f3ca055cdad71a271c359d9769f` 对应 push run `32604058382` 与 PR run `32604060824`；两者均为目标 SHA、各 11 个 job 全绿、无非 success job，并各恰好一个 `completed/success release-gate`。本地、远端分支与 PR head 精确一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-05 依赖门禁已关闭，G-06 依赖已解除；未触发 promotion、合并、发布、部署或任何生产操作。

> 适用仓库：`LoCCai/nonebot-plugin-moellmchats`
> 重点分支：`feat/generated-tool-bundles`
> 文档定位：后续研发总纲、版本规划、任务依赖与实施原则

---

## 1. 背景

`feat/generated-tool-bundles` 已经不再是单一功能补丁，而是一次接近运行时架构升级的改造。当前体系已经包含：

- Generated Tool / Custom Tool
- ToolSpec / ToolContext / ToolResult
- Tool 生命周期
- Runner 子进程执行
- UID/GID 降权
- `no_new_privs`
- CPU / 内存 / 文件 / 进程数限制
- Tool Snapshot
- Runtime Snapshot
- 当前进程原子热重载与多进程 watcher 最终收敛
- 回滚机制
- MCP 工具
- 模型路由
- LLM 请求 admission / backpressure
- 运行指标

这意味着后续开发不宜再继续以“哪里缺功能就在哪里加代码”的方式推进，而应进入明确分层的长期演进阶段。

---

# 2. 三条推进主线

后续工作拆成三份计划。

## 计划一：安全修复 + 核心架构重构

目标：

> 把 0.25 从“功能已经能跑”推进到“可以稳定上线、可审计、可回滚、长期维护”。

重点：

- Generated Tool 安全边界
- mutating 二阶段确认
- Capability 模型
- Source Snapshot
- Runner IPC
- Tool Bundle 生命周期
- Watcher / Reload / Rollback
- CI 中真实执行沙箱测试

计划一是后续所有工作的地基，应优先完成。

---

## 计划二：后续功能与架构优化

目标：

> 从“LLM + Function Calling 插件”逐渐演进成可扩展 Agent Runtime。

重点：

- ToolProvider 抽象
- Tool Capability
- Tool Graph
- AgentRun / AgentStep
- 并行工具
- 模型能力路由
- Runtime API
- 可观测性
- 管理能力

计划二在计划一接口趋于稳定后推进。

---

## 计划三：处理效率 + 数据库接入优化

目标：

> 解决用户数量、消息量、工具量、长期运行时间增加后出现的性能、状态和持久化问题。

重点：

- 内存 / Redis / PostgreSQL 分层
- 会话与消息持久化
- AgentRun / ToolCall 审计
- Token 与模型使用统计
- 上下文摘要
- 长期记忆
- 缓存
- 并发工具
- Batch Write
- 数据库故障降级

计划三可与计划二部分并行，但数据库表结构应等待核心领域模型稳定后再最终确定。

---

# 3. 推荐执行顺序

```text
                      ┌────────────────────────┐
                      │ Plan 1                 │
                      │ 安全修复 + Runtime 重构│
                      └───────────┬────────────┘
                                  │
                         核心接口与对象稳定
                                  │
             ┌────────────────────┴────────────────────┐
             │                                         │
    ┌────────▼────────┐                       ┌────────▼────────┐
    │ Plan 2          │                       │ Plan 3          │
    │ Agent 架构演进  │                       │ DB + 性能优化   │
    └────────┬────────┘                       └────────┬────────┘
             │                                         │
             └────────────────────┬────────────────────┘
                                  ▼
                       MoEllm Agent Runtime
```

---

# 4. 推荐版本路线

|版本|主要目标|
| ----| ---------------------------------------------------|
|`0.25.0-rc1`|修 P0 安全问题、Generated Tool 默认禁网、二阶段确认|
|`0.25.0-rc2`|Source Snapshot、Runner IPC、AST Policy、Sandbox CI 定义与本地实测|
|`0.25.x stable`|生命周期、File Lock、分阶段 Reload/Rollback 与 Watcher 已实现；最新 OS 隔离增量的本地总门禁、首次远端聚合 `release-gate` green 和 required check 均完成后才达到发布门禁|
|`0.26`|Provider discovery/source/trust 先 shadow 迁移，再版本化 Capabilities 与切换工具体系|
|`0.27`|AgentRun / AgentStep / Tool Graph|
|`0.28`|PostgreSQL + Redis 状态层|
|`0.29`|并行工具、上下文摘要、缓存、长期记忆|
|`0.30`|独立 Runner、Runtime API、完整 Observability|

---

# 5. 三个计划之间必须共享的领域对象

后续不应让各模块分别定义自己的状态结构。

建议统一：

```python
ToolArtifact
ToolBundle
ToolBundleVersion
ToolCapability
ToolSpec
ToolResult

RuntimeSnapshot

AgentRun
AgentStep
ToolCall

PendingAction

ModelCapability
ModelRequest
```

这些对象应成为后续：

- Runtime
- Database
- API
- Audit
- Metrics

之间的共同语言。

---

# 6. 核心设计原则

## 6.1 默认拒绝而不是默认允许

Generated Tool 应：

```text
默认：
network = false
host_filesystem = false
process = false
secrets = false

允许：
workspace = true
```

需要额外能力时显式申请。

---

## 6.2 Source 与 Generation 必须绑定

不能出现：

```text
Reload 时检查的是 A.py
执行时读取的是修改后的 A.py
```

正确方式：

```text
File Change
   ↓
Parse
   ↓
Snapshot Source
   ↓
Hash
   ↓
ToolArtifact
   ↓
Runtime Generation
```

请求始终执行自己进入时绑定的 generation。

---

## 6.3 Runtime 状态与持久化状态分离

PostgreSQL 不应该成为每次工具执行的同步依赖。

推荐：

```text
Runtime Hot State → Memory / Redis
Persistent State  → PostgreSQL
```

---

## 6.4 安全能力与业务权限分离

不要把：

```text
permission=user
```

理解为：

```text
可以联网
可以读文件
```

应拆分：

```text
permission
effect
capabilities
```

---

## 6.5 工具来源统一抽象，但信任等级不能统一

统一接口：

```text
ToolProvider
```

但工具信任等级应不同：

```text
registered/trusted
custom_file/reviewed
generated/untrusted
mcp/external
```

---

# 7. 最终目标架构

```text
                     ┌──────────────────────┐
                     │ Request / QQ / API   │
                     └──────────┬───────────┘
                                │
                        ┌───────▼────────┐
                        │ Agent Runtime  │
                        └───────┬────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
 ┌────────▼────────┐   ┌────────▼────────┐   ┌────────▼────────┐
 │ Model Router    │   │ Tool Runtime    │   │ State Manager   │
 └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
          │                     │                     │
      Providers           Tool Providers       Memory / Redis
                                │                     │
                        ┌───────▼────────┐            │
                        │ Tool Runner    │            │
                        │ Sandbox        │            │
                        └───────┬────────┘            │
                                │                     │
                                └──────────┬──────────┘
                                           ▼
                                      PostgreSQL
```

---

# 8. 完成后的项目能力

最终系统应具备：

- 请求级 Runtime Snapshot
- Tool Generation
- Tool Capability
- 工具版本化
- 人工审批
- 二阶段危险操作确认
- 工具热重载
- 原子回滚
- 沙箱执行
- Agent 多步骤任务
- 并行只读工具
- 请求恢复与审计
- Token / Cost / Latency 分析
- 上下文摘要
- 长期记忆
- PostgreSQL 持久化
- Redis 热状态
- 管理 API
- 完整运行指标

---

# 9. 不建议现在直接做的事情

## 不建议：直接把所有 dict 全部搬 PostgreSQL

原因：

- AgentRun / AgentStep 等领域对象与持久化 Schema 已固化，但除 G-01 聊天历史与 G-03 Session Summary 外的具体 Repository 和运行时持久化仍未实现
- ToolProvider、Repository 接口、engine、离线迁移、Schema、G-01 Chat History Repository、G-02 Memory/Redis History Hot Cache primitive、G-03 Session Summary、G-04 Tool Catalog Cache 与 G-05 Tool Schema Cache 均已完成精确 HEAD 远端双 gate；G-06 Classification Cache 依赖已解除但尚未实现
- G-01/G-02/G-03 均未接现有内存聊天路径、配置或生命周期，G-04/G-05 也未接现有 Categorize/LLM payload、配置或生命周期；Redis / PostgreSQL 的正式运行态编排与持久化边界尚未实现
- 跨进程只提供 canonical CAS 与 watcher 最终收敛，尚无分布式运行时事务

过早数据库化会导致很快再做第二次 schema migration。

---

## 不建议：把 Generated Tool 和人工 Custom Tool 当成同一安全等级

两者虽然可以共用 Runner，但：

```text
人工编写代码
```

和：

```text
LLM 自动生成代码
```

风险完全不同。

---

## 不建议：把 AST 风险扫描当成沙箱

AST 只能作为：

```text
预审 / Capability 推导
```

不能作为：

```text
最终安全边界
```

最终安全边界必须落在操作系统层。

---

# 10. 文档索引

- `01-plan-security-refactor.md`
  安全修复、核心重构、Runner、生命周期、CI。
- `02-plan-future-architecture.md`
  ToolProvider、Tool Graph、Agent Runtime、模型能力与 API。
- `03-plan-performance-database.md`
  Redis/PostgreSQL、性能、缓存、上下文、数据库设计。
- `04-implementation-backlog.md`
  可直接转换为 GitHub Issue / Milestone 的实施任务清单。
- `05-plan1-completion-audit.md`
  A-01～C-07 的源码、pytest node、门禁状态与最终关闭条件。

---
title: 04-implementation-backlog
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-20T00:00:00+00:00
---

# 04-implementation-backlog

# MoEllmChats 实施 Backlog 与 GitHub Milestone 建议

> 本文件可直接用于拆 GitHub Issue。

## 当前实施状态（2026-08-20）

- Milestone A、Milestone B 与 C-01～C-07 已在本地工作树完成实现，并按 A → B → C 顺序完成本轮定向复核：A 为 31 个非沙箱 node + 11 个真实 Sandbox case，B 为 24 个非沙箱 node + 12 个真实 Sandbox case，C-01～C-06 为 38 个定向 case。
- 最新本地总门禁已通过：Ruff 与 Actionlint 通过；Python 3.10～3.13 普通全量各 `347 passed, 1 skipped`，其中 Python 3.12 固定 NoneBot 2.4.4 / OneBot 2.4.6；mandatory root Sandbox 为 `40 passed, 0 skipped`。
- fresh sdist/wheel、Twine、checksum 与 Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过；本地来源元数据明确标记为未提交工作树，不冒充可发布提交。
- CI 已在本地定义一次构建、四组 package smoke、零 skip Sandbox 与 fail-closed 聚合 `release-gate`；手动 promotion 先验证 job 列表完整且恰好一个精确命名的 `release-gate` 已 `completed/success`，再下载原 run artifact，不构建也不发布 PyPI。
- 计划内实现已形成本地提交 `77c6872fa1df9f399952ab419c1d1f2ac6cdbeb5`；尚待推送、首次 GitHub `release-gate` green 与 required-check 配置，不能据本地结果声称远端发布完成。
- 每项状态只表示当前源码与测试已满足本节验收，不代表 GitHub 分支或 Qiqi 运行实例已经更新。
- Plan 1 当前下一步是推送本地提交并核对唯一 green SHA 和 required check 配置；远端门禁完成前不进入 Plan 2。逐项源码与测试映射见 [Plan 1 完成审计](./05-plan1-completion-audit.md)。

---

# Milestone A：0.25.0-rc1

目标：

> 先消除 Generated Tool 最重要的安全风险。

---

## A-01 Generated Tool 默认网络隔离

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P0**

涉及：

```text
generated_tool_runner.py
generated_tool_worker.py
generated_tools.py
config.py
tests/
```

任务：

- Generated Tool 默认 `disable_network=True`
- Custom File 与 Generated Tool 区分策略
- 无 net namespace 时 Generated Tool fail closed
- 加 network isolation test

验收：

- Generated Tool 无法访问公网
- Custom File 可依据策略决定是否联网

实现落点：所有文件/生成工具强制进入独立 PID/mount/IPC/UTS namespace 并设置固定 hostname，`network=false` 时再进入 network namespace；根挂载递归只读，仅 `workspace=true` 的私有 bind mount 恢复写权限，`host_filesystem=false` 时使用 Landlock read allowlist，并拒绝 xattr 读取/枚举。禁网时 seccomp 拒绝全部 socket；只联网但无 host-filesystem 时继续拒绝 AF_UNIX/AF_VSOCK；受限 socketpair 仅保留 AF_UNIX/STREAM，keyring syscall 始终拒绝。Custom File 只有静态 capability 声明才能放宽；namespace、hostname、mount 或策略前提缺失时 fail closed。

---

## A-02 PendingAction 二阶段确认

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P0**

涉及：

```text
tool_contracts.py
llm_tools.py
state_store.py
```

新增：

```text
PendingAction
PendingActionStore
```

验收：

```text
“不要确认执行”
```

绝不触发 mutating tool。

实现落点：内存 `PendingActionStore` 有界、带 TTL、一次性消费；动作绑定 Bot/adapter/user/group/tool/参数哈希/generation/bundle digest。首次调用只返回 nonce，必须由同一用户在同一会话另发 `确认执行 <nonce>`；重载、过期、重放、错用户或错群均 fail closed。

---

## A-03 Generated Tool 默认 superuser

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P0**

任务：

- Generated bundle 默认 effective permission=superuser
- 只有人工批准后允许 user
- UI 显示 effective permission

实现落点：manifest 的 `permission` 仅为 requested permission；有效权限默认 `superuser`。只有超级管理员针对精确 bundle digest 和工具写入人工授权策略后才能降为 `user`，策略损坏时统一回退为 `superuser`。

---

## A-04 Capability 基础结构

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P0**

新增：

```text
ToolCapability
ToolPolicy
```

当前严格字段：

```text
network
process
workspace
host_filesystem
secrets
```

实现落点：新增不可变 `ToolCapability` / `ToolPolicy`，effective policy 由 requested 与 admin ceiling 逐项取交集；未知字段和非布尔值拒绝。Generated 管理上限仅允许 private workspace，其余四项为 false；Custom File 必须静态显式声明放宽。`secrets` 当前为预留位，不注入宿主密钥。

---

## A-05 Draft 文件权限修复

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P1**

目标：

```text
metadata = 0600
config secrets = 0600
config dir = 0700
```

实现落点：配置树、Generated 草稿和权限策略均收紧为 owner-only；配置/草稿文件为 `0600`、目录为 `0700`，内容寻址的已批准版本保持无写位。原子替换后重新校验权限，并拒绝非当前用户拥有的路径或符号链接。

---

# Milestone B：0.25.0-rc2

---

## B-01 ToolArtifact

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P0**

新增：

```python
ToolArtifact
```

包含：

```text
source
hash
schema
spec
generation
```

实现落点：新增不可变 `ToolArtifact` / `ToolContractSnapshot`，把源码、Schema、ToolSpec、安全契约、generation 与摘要绑定；Custom File 和 Generated loader 均只向运行快照发布完整制品。

---

## B-02 Custom Tool Source Snapshot

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P1**

目标：

- reload 后不再执行活动源文件
- 请求绑定 snapshot source

实现落点：候选 generation 加载时只读一次源码 bytes；正式 handler 只调用 `execute_artifact()`，worker 从请求固定制品读取源码，不回读后来变化的文件。进行中的请求继续使用原 generation。

---

## B-03 Generated Bundle Runtime Digest

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P1**

每次执行前确认：

```text
expected digest == actual digest
```

实现落点：Store、Artifact 和 runner 共用 canonical bundle digest；每次执行前重算 artifact/bundle 摘要并校验 request-pinned generation。错误 expected bundle digest 会在 runner dispatch 前 fail closed。

---

## B-04 Runner Protocol FD 分离

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P1**

协议：

```text
FD 3
```

stdout/stderr 只保留日志。

实现落点：stdin 只传请求，worker 将结构化结果写入 literal FD3；stdout、stderr、FD3 分别有界读取。三路洪泛、超时和取消都会清理进程组、FD、队列槽与 Python 3.10 subprocess transport。FD3 只隔离意外日志污染，不是恶意代码认证边界。

---

## B-05 Workspace File Count 限制

**状态：✅ 本地实现已提交（待推送/远端 CI）**

**优先级：P1**

增加：

```text
max files
max bytes
max depth
max single-file bytes
```

同时拒绝符号链接和特殊文件；运行期周期扫描后还会进行不可绕过的最终复扫。

---

## B-06 Workspace Scanner Async 化

**状态：✅ 本地实现已提交（待推送/远端 CI）**

避免 Event Loop 同步 `rglob()`。

实现落点：所有 workspace 遍历统一经 `asyncio.to_thread()`，已有 heartbeat 回归证明大目录扫描不会同步阻塞事件循环。

---

## B-07 AST Policy Engine

**状态：✅ 本地实现已提交（待推送/远端 CI）**

结果类型：

```text
ALLOW
DENY
CAPABILITY_REQUIRED
RISK
```

实现落点：分别分析模块、handler、可达 helper、`tool.py` 与 `tests.py`；覆盖 alias、动态属性、文件/数据库/HTTP 写入和进程调用。系统命令即使已获 process capability 也强制推导为 `mutating`，不能绕过 PendingAction。AST 是保守预检，不替代 seccomp、namespace、RLIMIT 或人工审阅。

---

## B-08 禁止 Generated Tool subprocess

**状态：✅ 本地实现已提交（待推送/远端 CI）**

第一阶段直接 deny。

实现落点：Generated 在加载期由 AST Policy 直接 `DENY` process；runner 不允许 Generated Artifact 放宽 network/process/host_filesystem/secrets。worker 在编译不可信源码前设置 `no_new_privs` 和 `RLIMIT_NPROC=1`，并通过 libseccomp 拒绝 `execve`、`execveat`、`fork`、`vfork`、`clone`、`clone3`；缺少库、syscall 映射或规则加载失败时 fail closed。Custom File 只有显式 `capabilities.process=true` 才移除该进程 deny filter 并使用有界进程预算，但仍受固定 executable roots、其他 capability 与二阶段确认约束。

---

# Milestone C：0.25 Stable

---

## C-01 Tool Lifecycle State Machine

**状态：✅ 本地实现已提交（待推送/远端 CI）**

草稿状态：

```text
Draft
StaticValidated
SandboxTested
ModelReviewed
AwaitingApproval
Approved
```

失败状态：

```text
ValidationFailed
TestFailed
ReviewFailed
Rejected
```

版本状态：

```text
Approved
Activated
Deprecated
Archived
```

实现落点：`lifecycle_state.json` schema v3 是唯一 canonical 状态并兼容读取 schema v2；v2 草稿转换为带 `schema-v2-migration` evidence 的 v3 内存记录，下一次 canonical 写入持久化 v3。`DraftEvidence` 绑定草稿 digest 并严格校验 producer/outcome/summary/risks/时间/顺序。`create_draft()` 只创建 Draft，ToolAuthoring 用专用入口逐步推进。DraftRecord/VersionRecord 保持严格转移与唯一 Activated/active 不变量；`ExecutionBlocked` 仅是运行期结果。每个 plan 绑定 revision、before/after state digest 与 operation ID；批准可在单 revision 中完成 AwaitingApproval → Approved 和版本激活。

---

## C-02 Full Draft Review

**状态：✅ 本地实现已提交（待推送/远端 CI）**

管理员能分页查看：

```text
manifest
source
tests
risks
capabilities
diff
```

实现落点：七个区段从同一个 canonical read snapshot 组装，每页含页头不超过 1800 字，可无损拼接；页头携草稿哈希、lifecycle revision/state digest、同 bundle active digest、页码与区段 SHA-256，并给出完整 64 位 review stamp 与三参数批准命令。stamp 绑定 draft ID/digest、revision/state digest、active digest，`prepare_approval()` 会在同一 snapshot 重算，审阅后任一 lifecycle 变化都会拒绝旧 stamp。metadata 的 `lifecycle_evidence` 是 canonical、digest-bound `DraftEvidence` 的投影；只有原始模型 `metadata.review` 仍是 best-effort。

---

## C-03 Watcher 外层恢复

**状态：✅ 本地实现已提交（待推送/远端 CI）**

任何文件损坏不能永久终止 watcher。

实现落点：最外层永久循环单独传播 `CancelledError`，其他错误以 0.5→30 秒有界指数退避并在成功后重置；意外任务退出可见。指纹 glob/stat 和资源加载 I/O 均移出 event loop，失败继续使用旧 RuntimeSnapshot。

---

## C-04 Lifecycle File Lock

**状态：✅ 本地实现已提交（待推送/远端 CI）**

支持 OS File Lock。

实现落点：固定 `.lifecycle.lock` 经 owner/no-follow 检查后使用 POSIX `flock` shared/exclusive lock 与有界 timeout；durable state write 使用同目录临时文件、fsync、replace、目录 fsync与回读。revision/state digest CAS 拒绝 lost update。目录 fsync 有界重试 3 次；重试耗尽时即使 after-state 可见也保持 uncertain，只有 durability 已确认后的回读不确定才允许按完整 before/after identity 调和。

---

## C-05 Approve 原子事务

**状态：✅ 本地实现已提交（待推送/远端 CI）**

实现顺序：

```text
after_state 候选预构建
canonical durable CAS
当前进程 RuntimeSnapshot 发布
```

批准、拒绝、权限、停用和回滚统一从 `RuntimeReloader.apply_generated_change()` 进入候选预构建、canonical CAS、RuntimeSnapshot 发布；Store/LifecycleStore 的 commit/publish 方法均为 internal，不是生产 API。候选或 CAS 失败不发布；durable commit 成功而本进程发布失败时保留新 canonical revision 并让 watcher 重试，不 blind rollback。进入 durable finalization 后 shield 调用方取消并等待线程完成，不遗留后台写。legacy active/permission/metadata 只是尽力投影，失败标 stale，不阻断 canonical/runtime。

---

## C-06 Rollback 原子事务

**状态：✅ 本地实现已提交（待推送/远端 CI）**

权限、拒绝、停用与回滚复用 C-05 的 typed prepare/commit transaction；回滚只允许唯一匹配、未 Archived 的版本，并在同一 canonical snapshot 下验证 owner/no-follow、版本目录精确 `0500`、只含 `manifest.json`/`tool.py`/`tests.py` 三个 `0400` 普通文件、校验期间 inode 稳定及完整内容 digest。RuntimeSnapshot 和 ToolSnapshot 同时携带 lifecycle revision/state digest/active stamp，generation 从当前发布快照严格递增。

明确边界：filesystem commit、当前进程 runtime pointer 与其他 worker 内存不是跨进程 ACID；`converged` 只描述当次观察 stamp，其他新版 worker 由 watcher 最终收敛。首次持久化 v3 前，所有仍使用 legacy 或 schema v2 的旧进程必须先退出，否则旧进程无法读取新 schema，并可能保留旧 RuntimeSnapshot 或竞争兼容投影。

---

## C-07 Sandbox Integration CI

**状态：🟡 workflow 与最新本地总门禁已通过（mandatory root：`40 passed, 0 skipped`）；待推送及首次 GitHub CI green**

必须真实执行：

```text
UID
RLIMIT
network
process cleanup
memory
timeout
```

实现落点：普通 Python 3.10～3.13 job 排除 root-only 文件；独立 mandatory root job 对 Linux、root、PID/mount/IPC/UTS/network namespace、固定 hostname、UID/GID drop、Landlock 与 libseccomp 前提 fail closed，不允许 skip。测试覆盖 UID、RLIMIT、network、process cleanup、memory、timeout、cancel、三路洪泛、workspace、host filesystem/xattr、AF_UNIX/AF_VSOCK/socketpair、SysV IPC、keyring 与 UTS；最新本地执行结果为 `40 passed, 0 skipped`。当前不使用 cgroup，也不是完整 syscall allowlist，`stat`/`readlink` 路径元数据残余可见是明确边界。

---

# Milestone D：0.26 Tool Runtime

---

## D-01a Provider Discovery Contract（type-only / shadow）

**状态：⏸️ 等待 Plan 1 精确 green SHA 与远端发布门禁**

定义稳定 `provider_id/source`、`ToolTrustLevel` 类型和不可变 `DiscoveredTool`。`DiscoveredTool` 必须携带 `ToolSpec`，`ToolArtifact` 可选；只有 Custom File / Generated 必须携带 artifact。

`discover(ProviderDiscoveryContext[TResources])` 必须是 side-effect-free async 候选发现。context 同时携带非负 generation 和来源专属的 `TResources`；每个 Provider 都必须定义深冻结、与输入脱离的 typed resource record，禁止用 `Any` 或裸 `dict` 传递候选资源。资源由同一 runtime transaction 构建并显式传入；Generated Provider 的记录必须固定精确 after-state/source override，发现时不得重读 live canonical。本任务不接管执行、不改 `ToolManager` / MCP 镜像 / `RuntimeSnapshot`、不引入尚未定义的 `ToolCall`。

---

## D-02 RegisteredToolProvider

作为无 I/O、trusted 来源的首个 shadow pilot，对 legacy 目录做完整 parity，仍不切换执行。

---

## D-01b ProviderRegistry / ToolSnapshot v2 Dual View

在 Registered pilot 证明发现契约后，集中冲突规则并 dual-publish / dual-read。新旧视图必须等价，不允许一次替换 `plugin_info/custom_tools/tool_dependencies/mcp_tool_names`。

---

## D-03 FileToolProvider

保留 ToolArtifact 源码快照、AST Policy 和 generation 绑定。

---

## D-04 GeneratedToolProvider

必须使用事务已准备的精确 lifecycle after-state 与 source override，不得重新读取 live canonical。

---

## D-05 MCPToolProvider

发现失败保留上一代可用快照；在 shadow parity 前不删除现有 MCP sidecar。

---

## D-05a BuiltinToolProvider

收口 `web_search` 等内置旁路，但不把外部结果的信任等级与 Provider 代码信任等同。

---

## D-05b NoneBotPluginProvider

将受控伪事件适配器纳入统一目录；显式工具接口未覆盖的遗留插件仍保持当前权限语义与有界兼容通道。

---

## D-06 Tool Trust Enforcement

`ToolTrustLevel` 枚举与来源身份已在 D-01a 定义；本任务只实施执行、选择、审计与管理策略。

```text
trusted
reviewed
untrusted
external
```

---

## D-07 Capability Policy Merge

```text
requested
detected
admin policy
```

合并成 effective policy。

能力字段扩展必须版本化 ToolContract/Artifact digest 语义，在 live consumer cutover 前完成，不与 D-01a 混合。

---

## D-08 Consumer Cutover

按 `categorize → llm_payload → llm_tools → pending action → search → 管理命令` 逐个切换，每个消费端单独保留新旧视图等价回归与可回滚开关。

---

## D-09 Legacy Sidecar Removal

只在全部 Provider、扩展 capability 和 D-08 消费端切换门禁通过，且至少完成一个发布周期的 parity 观察后，才单独删除 legacy sidecar。本任务不与 consumer cutover 混在同一 PR。

---

# Milestone E：0.27 Agent Runtime

---

## E-01 AgentRun

---

## E-02 AgentStep

---

## E-03 ToolCall

---

## E-04 Agent State Machine

---

## E-05 DeadlineContext

---

## E-06 Tool Graph

---

## E-07 Read-only Parallel Tool Scheduler

---

## E-08 Tool Conflict Policy

---

# Milestone F：0.28 PostgreSQL + Redis

---

## F-01 Repository Interface

---

## F-02 SQLAlchemy Async Engine

---

## F-03 Alembic

---

## F-04 User / Conversation / Message Schema

---

## F-05 AgentRun Schema

---

## F-06 AgentStep Schema

---

## F-07 ToolCall Schema

---

## F-08 Tool Bundle Metadata Schema

---

## F-09 Audit Schema

---

## F-10 Usage Schema

---

## F-11 Redis Client

---

## F-12 PendingAction Redis

---

## F-13 Cooldown Redis

---

## F-14 Admission Redis

---

# Milestone G：0.29 Performance

---

## G-01 Chat History Repository

---

## G-02 History Hot Cache

---

## G-03 Session Summary

---

## G-04 Tool Catalog Cache

---

## G-05 Tool Schema Cache

---

## G-06 Classification Cache

---

## G-07 Batch Usage Write

---

## G-08 Batch Audit Write

---

## G-09 Read-only Parallel Execution

---

## G-10 Trusted Runner Pool

---

# Milestone H：0.30 Runtime Platform

---

## H-01 Runtime API

---

## H-02 Tool Bundle API

---

## H-03 Agent Run API

---

## H-04 Metrics API

---

## H-05 Web Admin

---

## H-06 Structured Logging

---

## H-07 Full Metrics

---

## H-08 Long-Term Memory

---

# Issue 模板建议

每个 Issue 建议：

```markdown
## 背景

## 当前问题

## 目标

## 设计

## 涉及文件

## 兼容性

## 测试

## 验收标准

## 依赖
```

---

# PR 拆分原则

不要做：

```text
PR: refactor runtime
+5000 -2000
```

下一阶段建议：

```text
PR 1 D-01a discovery/source/trust types（shadow only）
PR 2 D-02 Registered provider parity
PR 3 D-01b ProviderRegistry + dual view
PR 4 D-03 File provider parity
PR 5 D-04 Generated provider parity
PR 6 D-05/MCP，再分开 Builtin 与 NoneBot adapter
PR 7 D-06 trust enforcement
PR 8 D-07 versioned capability merge
PR 9 D-08 consumers（逐个）
PR 10 D-09 legacy removal
```

尽量做到：

```text
一个 PR
=
一个可独立回滚的设计点
```

---

# 推荐优先级

## P0

- Generated Tool Runtime 禁网
- mutating 二阶段确认
- ToolArtifact
- Generated Permission 保守化

## P1

- Runner IPC
- Source Snapshot
- Lifecycle
- Sandbox CI
- File Lock
- Config Permission

## P2

- ToolProvider
- AgentRun
- PostgreSQL
- Redis
- Parallel Tools

## P3

- Web Admin
- Long-Term Memory
- Advanced Model Router

---

# 最终阶段完成定义

项目进入 `0.30` 后，应具备：

```text
安全 Tool Runtime
Agent Runtime
Versioned Tools
PostgreSQL State
Redis Runtime
Parallel Scheduler
Audit
Metrics
Admin API
```

届时 MoEllmChats 的定位将不再只是聊天插件，而更接近：

> 面向 NoneBot / QQ 的可扩展 Agent Runtime。

---
title: 04-implementation-backlog
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-22T20:13:07+00:00
---

# 04-implementation-backlog

# MoEllmChats 实施 Backlog 与 GitHub Milestone 建议

> 本文件可直接用于拆 GitHub Issue。

## 当前实施状态（2026-08-22）

- Milestone A～F 已按依赖顺序完成各自精确 HEAD 双 run 门禁；D-09 因缺少至少一个发布周期 parity 观察且禁止生产操作而继续锁定。
- G-01 实现提交 `b3566d6513f142d86de91898a6c6b8f14a4e131d` 已完成四版本、本地 Sandbox、静态、最低依赖、fresh 制品、四组包外零数据库 I/O 与精确 HEAD 双 run 门禁；G-02 依赖已解除。
- G-01 只提供不可变 Conversation/Message records 与调用方显式 session 的 PostgreSQL Repository；未接配置、生命周期、现有内存聊天路径或生产 runtime，未读取 DSN，未运行 migration，未连接真实 PostgreSQL/Redis。
- G-01 闭环文档 HEAD `11531889583fd5d11cf0871f503c6ff037c38395` 的 push `32593312310` / PR `32593315775` 已完成最终 11/11 双 gate。G-02 实现提交 `e865838` 已完成四版本定向/联合/全量、Sandbox、最低依赖、静态、fresh 制品与四组包外零 I/O smoke；精确 HEAD 双 run gate 待完成，G-03 继续锁定。
- G-02 只提供 committed `HistoryWindow`、失效代际协议及显式 Memory/Redis backend；未接 G-01 Repository、`MessagesHandler`、配置、生命周期或生产 runtime，未读取 Redis URL/DSN，未连接真实服务。
- G-02 本地证据 HEAD `fca62e2a97fdb1b9fcccc5dd67dc604458d754c3` 的 push `32595899079` / PR `32595902263` 已各 11/11 green、各恰好一个成功 `release-gate`；本地、远端与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-03 依赖已解除。
- CI 继续要求一次构建、四组 package smoke、零 skip Sandbox 与 fail-closed 聚合 `release-gate`；本地成功不替代远端精确 HEAD 证据，也未触发 promotion、合并、发布或部署。
- Plan 1 修复后精确 HEAD `f6c7628025cb5d34519499d86b979de448406d5b` 的 push run `32396257506` 与 PR run `32396261932` 各 11 个 job 全绿、各只有一个成功 `release-gate`；PR 基分支 `feat/llm-runtime-backpressure` 已要求 `strict=true` 的 `release-gate`。
- 每项状态分别标明本地实现、远端门禁与部署边界；远端 green 不代表 Qiqi 运行实例已经更新。
- Plan 1 发布门禁已关闭且未部署。Plan 2 的 D-01a～D-08f 已完成各自精确 HEAD 双 run gate；D-08f 最终闭环 HEAD `ea022bd31020880c72a66802aa3f036389d0169d` 对应 push run `32443308534` / PR run `32443313095`，两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / CLEAN`。D-09 因尚无至少一个发布周期的 parity 观察且禁止生产操作而保持锁定，legacy sidecar 继续保留。
- Milestone E 已在不依赖 D-09 清理、也不接数据库的增量边界内闭环，F-01～F-06 已完成精确 HEAD 双 run gate。F-07 最终 HEAD `dcff410498a862bed302687e1383cab0f554da6c` 对应 push run `32469057942` / PR run `32469061094`；两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-08 实现提交 `7afa3c81a6604a09533b0b1b487d3c484f9f1909` 新增 Tool Bundle metadata 与线性 revision `0005_tool_bundle_metadata`；四版本定向各 `44 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `435 passed`，四版本普通全量各 `989 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，静态、fresh 制品及四组包外 Schema/graph/DDL/reload smoke 均通过。F-08 当前仅本地门禁完成，精确 HEAD 双 run gate 待完成，F-09 继续锁定；没有全局 engine/session、Repository 实现、数据库连接或在线 migration。逐项源码与测试映射见 [Plan 1 完成审计](./05-plan1-completion-audit.md)。

---

# Milestone A：0.25.0-rc1

目标：

> 先消除 Generated Tool 最重要的安全风险。

---

## A-01 Generated Tool 默认网络隔离

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

**优先级：P0**

任务：

- Generated bundle 默认 effective permission=superuser
- 只有人工批准后允许 user
- UI 显示 effective permission

实现落点：manifest 的 `permission` 仅为 requested permission；有效权限默认 `superuser`。只有超级管理员针对精确 bundle digest 和工具写入人工授权策略后才能降为 `user`，策略损坏时统一回退为 `superuser`。

---

## A-04 Capability 基础结构

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

**优先级：P1**

目标：

- reload 后不再执行活动源文件
- 请求绑定 snapshot source

实现落点：候选 generation 加载时只读一次源码 bytes；正式 handler 只调用 `execute_artifact()`，worker 从请求固定制品读取源码，不回读后来变化的文件。进行中的请求继续使用原 generation。

---

## B-03 Generated Bundle Runtime Digest

**状态：✅ 发布门禁完成（未部署）**

**优先级：P1**

每次执行前确认：

```text
expected digest == actual digest
```

实现落点：Store、Artifact 和 runner 共用 canonical bundle digest；每次执行前重算 artifact/bundle 摘要并校验 request-pinned generation。错误 expected bundle digest 会在 runner dispatch 前 fail closed。

---

## B-04 Runner Protocol FD 分离

**状态：✅ 发布门禁完成（未部署）**

**优先级：P1**

协议：

```text
FD 3
```

stdout/stderr 只保留日志。

实现落点：stdin 只传请求，worker 将结构化结果写入 literal FD3；stdout、stderr、FD3 分别有界读取。三路洪泛、超时和取消都会清理进程组、FD、队列槽与 Python 3.10 subprocess transport。FD3 只隔离意外日志污染，不是恶意代码认证边界。

---

## B-05 Workspace File Count 限制

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

避免 Event Loop 同步 `rglob()`。

实现落点：所有 workspace 遍历统一经 `asyncio.to_thread()`，已有 heartbeat 回归证明大目录扫描不会同步阻塞事件循环。

---

## B-07 AST Policy Engine

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

第一阶段直接 deny。

实现落点：Generated 在加载期由 AST Policy 直接 `DENY` process；runner 不允许 Generated Artifact 放宽 network/process/host_filesystem/secrets。worker 在编译不可信源码前设置 `no_new_privs` 和 `RLIMIT_NPROC=1`，并通过 libseccomp 拒绝 `execve`、`execveat`、`fork`、`vfork`、`clone`、`clone3`；缺少库、syscall 映射或规则加载失败时 fail closed。Custom File 只有显式 `capabilities.process=true` 才移除该进程 deny filter 并使用有界进程预算，但仍受固定 executable roots、其他 capability 与二阶段确认约束。

---

# Milestone C：0.25 Stable

---

## C-01 Tool Lifecycle State Machine

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

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

**状态：✅ 发布门禁完成（未部署）**

任何文件损坏不能永久终止 watcher。

实现落点：最外层永久循环单独传播 `CancelledError`，其他错误以 0.5→30 秒有界指数退避并在成功后重置；意外任务退出可见。指纹 glob/stat 和资源加载 I/O 均移出 event loop，失败继续使用旧 RuntimeSnapshot。

---

## C-04 Lifecycle File Lock

**状态：✅ 发布门禁完成（未部署）**

支持 OS File Lock。

实现落点：固定 `.lifecycle.lock` 经 owner/no-follow 检查后使用 POSIX `flock` shared/exclusive lock 与有界 timeout；durable state write 使用同目录临时文件、fsync、replace、目录 fsync与回读。revision/state digest CAS 拒绝 lost update。目录 fsync 有界重试 3 次；重试耗尽时即使 after-state 可见也保持 uncertain，只有 durability 已确认后的回读不确定才允许按完整 before/after identity 调和。

---

## C-05 Approve 原子事务

**状态：✅ 发布门禁完成（未部署）**

实现顺序：

```text
after_state 候选预构建
canonical durable CAS
当前进程 RuntimeSnapshot 发布
```

批准、拒绝、权限、停用和回滚统一从 `RuntimeReloader.apply_generated_change()` 进入候选预构建、canonical CAS、RuntimeSnapshot 发布；Store/LifecycleStore 的 commit/publish 方法均为 internal，不是生产 API。候选或 CAS 失败不发布；durable commit 成功而本进程发布失败时保留新 canonical revision 并让 watcher 重试，不 blind rollback。进入 durable finalization 后 shield 调用方取消并等待线程完成，不遗留后台写。legacy active/permission/metadata 只是尽力投影，失败标 stale，不阻断 canonical/runtime。

---

## C-06 Rollback 原子事务

**状态：✅ 发布门禁完成（未部署）**

权限、拒绝、停用与回滚复用 C-05 的 typed prepare/commit transaction；回滚只允许唯一匹配、未 Archived 的版本，并在同一 canonical snapshot 下验证 owner/no-follow、版本目录精确 `0500`、只含 `manifest.json`/`tool.py`/`tests.py` 三个 `0400` 普通文件、校验期间 inode 稳定及完整内容 digest。RuntimeSnapshot 和 ToolSnapshot 同时携带 lifecycle revision/state digest/active stamp，generation 从当前发布快照严格递增。

明确边界：filesystem commit、当前进程 runtime pointer 与其他 worker 内存不是跨进程 ACID；`converged` 只描述当次观察 stamp，其他新版 worker 由 watcher 最终收敛。首次持久化 v3 前，所有仍使用 legacy 或 schema v2 的旧进程必须先退出，否则旧进程无法读取新 schema，并可能保留旧 RuntimeSnapshot 或竞争兼容投影。

---

## C-07 Sandbox Integration CI

**状态：✅ 精确 HEAD 双 run 全绿，required `release-gate` 已配置；未部署**

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

**状态：✅ 精确 HEAD `c8afc807138a02237d96b65c81bf7f38c1ec7f43` 双 run 远端 gate green；未部署**

定义稳定 `provider_id/source`、`ToolTrustLevel` 类型和不可变 `DiscoveredTool`。`DiscoveredTool` 必须携带 `ToolSpec`，`ToolArtifact` 可选；只有 Custom File / Generated 必须携带 artifact。

`discover(ProviderDiscoveryContext[TResources])` 必须是 side-effect-free async 候选发现。context 同时携带非负 generation 和来源专属的 `TResources`；每个 Provider 都必须定义深冻结、与输入脱离的 typed resource record，禁止用 `Any` 或裸 `dict` 传递候选资源。资源由同一 runtime transaction 构建并显式传入；Generated Provider 的记录必须固定精确 after-state/source override，发现时不得重读 live canonical。本任务不接管执行、不改 `ToolManager` / MCP 镜像 / `RuntimeSnapshot`、不引入尚未定义的 `ToolCall`。

实现落点：新增 `tool_providers.py`，定义 source/trust 固定映射、六类 frozen typed resource record、`ProviderDiscoveryContext[TResources]`、不可变 `DiscoveredTool` 和只含 `discover()` 的 `ToolProvider` Protocol。运行时拒绝裸资源映射、负数/布尔 generation、source/trust 错配、缺失或伪造 artifact、artifact generation/spec 错配；Generated resource 只接受精确指向 after-state active 版本的 typed source override。Pyright 为 0 error/0 warning；Python 3.10～3.13 定向各 `10 passed`；Python 3.12 普通全量 `357 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`。

远端证据：包含 D-01a 的精确 HEAD `c8afc807138a02237d96b65c81bf7f38c1ec7f43` 对应 push run `32397753076` 与 PR run `32397766968`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 保持 CLEAN。

---

## D-02 RegisteredToolProvider

**状态：✅ 精确 HEAD `8d42152e54a59b6f3d0d2b39c20b12c5f0dd4a5e` 双 run 远端 gate green；未部署**

作为无 I/O、trusted 来源的首个 shadow pilot，对 legacy 目录做完整 parity，仍不切换执行。

实现落点：新增 frozen `RegisteredToolProvider`，身份固定为 `registered / REGISTERED / TRUSTED`，`discover()` 只消费事务显式传入的 `RegisteredToolResources`，按候选 generation 返回无 artifact 的不可变记录，不读取全局 registry 或 I/O。`RuntimeReloader` 每个 candidate transaction 只截取一次 registry snapshot，同一份 `ToolSpec` identity 显式传给 provider 与 legacy loader；legacy 注册工具集合、精确 `ToolSpec`、handler、name、description、parameters、source、dependencies、generation 或字段集合任一不一致均 fail closed。校验结果不发布，`ToolSnapshot` / `RuntimeSnapshot` schema、执行/选择/catalog consumer 与 MCP 镜像保持 legacy 不变。

本地门禁：Pyright `0 errors, 0 warnings`；Python 3.10～3.13 D-02 定向各 `59 passed`，四版本普通全量各 `363 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；fresh sdist/wheel、Twine、checksum 与 Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：精确 HEAD `8d42152e54a59b6f3d0d2b39c20b12c5f0dd4a5e` 对应 push run `32400562125` 与 PR run `32400564965`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-01b ProviderRegistry / ToolSnapshot v2 Dual View

**状态：✅ 精确 HEAD `c8a4211560f2f7214b971109c54d817628f843d5` 双 run 远端 gate green；未部署**

在 Registered pilot 证明发现契约后，集中冲突规则并 dual-publish / dual-read。新旧视图必须等价，不允许一次替换 `plugin_info/custom_tools/tool_dependencies/mcp_tool_names`。

实现落点：新增 frozen `ProviderRegistration`、泛型 `ProviderDiscoveryPlan`、不可变 `ProviderDiscoveryBatch`、`ProviderRegistry` 与 schema version 2 的 `ProviderCatalogSnapshot`。Registry 对每个注册 identity 要求同 generation 恰好一个 typed operation/batch，缺失、重复、未注册、source/trust/generation 漂移和跨 Provider 重名均 fail closed。runtime candidate 通过 Registry 构建 Registered catalog，再把它与 legacy 四字段一起写入 `ToolSnapshot`；Snapshot 构造时再次验证 Registered 新旧视图的工具集合、精确 `ToolSpec`、handler、字段、source 与依赖保留。当前 legacy plugin 追加依赖允许共存，但 Provider 声明依赖不能丢失。

兼容边界：旧 `ToolSnapshot(...)` 构造会获得同 generation 的空 v2 catalog；正式 runtime candidate 必须发布含 Registered registration 的 v2 catalog。现有 categorize、LLM payload、执行、pending action、search、管理命令和 MCP 镜像仍只消费 legacy 字段；没有 Provider `execute()` / `reload()`，没有删除 sidecar，也未部署。

本地门禁：Pyright `0 errors, 0 warnings`；D-01b 定向 `64 passed`；Python 3.10～3.13 普通全量各 `368 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；fresh sdist/wheel、Twine、checksum 与 Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：精确 HEAD `c8a4211560f2f7214b971109c54d817628f843d5` 对应 push run `32402611310` 与 PR run `32402617145`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-03 FileToolProvider

**状态：✅ 精确 HEAD `38a2da9cbec25e7dfeb07fb3cdd172a5e13396c9` 双 run 远端 gate green；未部署**

保留 ToolArtifact 源码快照、AST Policy 和 generation 绑定。

实现落点：新增 frozen `FileToolProvider`，稳定身份为 `custom-file / CUSTOM_FILE / REVIEWED`。runtime transaction 只调用一次既有 `load_file_tools()`，从该次 legacy 结果提取精确 `ToolArtifact` 构造 typed `FileToolResources`；Provider 只验证固化 digest/generation 并生成不可变记录，不重读文件、不重跑 AST Policy、不执行工具。Registry 扩为 Registered + File，并在 legacy 合并前集中拒绝两来源重名；同一 file candidate 再交给 legacy merge，避免二次读取和 TOCTOU。

等价门禁：candidate merge 与最终 `ToolSnapshot` 均验证 File slice 的工具集合、精确 artifact/`ToolSpec`/handler identity、源码 snapshot/digest、Schema、source、declared/effective effect、generation 与 Provider 声明依赖。文件级 `TOOL_DEPENDENCIES` 或 plugin 追加依赖允许共存，但 artifact contract 声明依赖不得丢失。现有 categorize、LLM payload、执行、pending action、search、管理命令与 MCP 镜像继续读取 legacy 字段；没有新增 Provider 执行/reload 接口，也未修改 `RuntimeSnapshot` schema。

本地门禁：Pyright `tool_providers.py` 为 `0 errors, 0 warnings`；D-03 定向 `60 passed`；Python 3.10～3.13 普通全量各 `375 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；fresh sdist/wheel、Twine、checksum 与 Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：精确 HEAD `38a2da9cbec25e7dfeb07fb3cdd172a5e13396c9` 对应 push run `32405397700` 与 PR run `32405401518`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-04 GeneratedToolProvider

**状态：✅ 精确 HEAD `f03a8ab86bddc392b72beeaaa643c7642ed2687d` 双 run 远端 gate green；未部署**

必须使用事务已准备的精确 lifecycle after-state 与 source override，不得重新读取 live canonical。

实现落点：新增 frozen `GeneratedToolProvider`，稳定身份为 `generated / GENERATED / UNTRUSTED`。runtime transaction 只调用一次既有 Generated loader，并用 `GeneratedToolResources.from_legacy_tools()` 将该次 candidate 的精确 `ToolArtifact` 绑定到事务 lifecycle after-state 与 source override；Provider 只在内存中校验固化 artifact/bundle digest 与 generation，不重读 live canonical、源码或验证入口，也不执行工具。同一 candidate 随后复用于 legacy merge，避免二次加载和 TOCTOU；Registry 在 legacy 合并前集中拒绝 Registered/File/Generated 跨来源重名。

等价门禁：candidate merge 与最终 `ToolSnapshot` 均验证 Generated slice 的 active bundle 集合、精确 artifact/`ToolSpec`/handler identity、bundle/artifact digest、Schema、source、requested/effective permission、effect、capability、generation 与 Provider 声明依赖。plugin 追加依赖可共存，但 artifact contract 声明依赖不得丢失。现有 categorize、LLM payload、执行、pending action、search、管理命令与 MCP 镜像继续读取 legacy 字段；未新增 Provider 执行/reload 接口，也未修改 `RuntimeSnapshot` schema。

本地门禁：实现提交 `95a57cfc7abeab59b310ae19a6e7872da0e01136`；D-04 定向 `78 passed`；Python 3.10～3.13 普通全量各 `382 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 与 Pyright `tool_providers.py` 均通过；fresh wheel/sdist 与 Twine 通过，wheel SHA256 `f6297cbbe965ea0a169148de0fbf2bbd6add0e8b0fe26bbf56dcabb55a7e47e4`、sdist SHA256 `c927af8b85d7d1cfe6b9735dae22e09f1745c6528f22e61e5ed9a1f1963f4780`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：精确 HEAD `f03a8ab86bddc392b72beeaaa643c7642ed2687d` 对应 push run `32407494606` 与 PR run `32407498396`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-05 MCPToolProvider

**状态：✅ 精确 HEAD `14fe2274d373e2e3a35443d3e0bedcb11f02bb28` 双 run 远端 gate green；未部署**

发现失败保留上一代可用快照；在 shadow parity 前不删除现有 MCP sidecar。

实现落点：新增 frozen `MCPToolProvider`，稳定身份为 `mcp / MCP / EXTERNAL`。runtime transaction 仍只调用一次既有 MCP 网络发现且保持 `strict=True`，随后用 `MCPToolResources.from_legacy_tools()` 从该次候选派生不可变 `ToolSpec`；Provider 只做纯内存 discovery，不读取网络、文件或全局 sidecar。Registry 扩为 Registered + File + Generated + MCP，并在 legacy merge 前集中拒绝跨来源重名。

等价门禁：route sidecar 名称必须与 MCP 工具集合完全一致，每条 route 只能包含非空 `server/tool`；candidate merge 与最终 `ToolSnapshot` 均验证 MCP slice 的工具集合、精确 `ToolSpec`/handler identity、Schema、source、generation、Provider 声明依赖与 `mcp_tool_names`。发现、route、冲突或 parity 失败均拒绝整代 candidate，上一代快照保持可用。现有 `mcp_manager.servers`、`tool_to_server`、`tool_manager.mcp_tool_names`、consumer 与执行路径继续保留；未修改 `RuntimeSnapshot` schema，也未删除 legacy sidecar。

本地门禁：实现提交 `76c746c134807b99e23b67489db9a7d1185e3b26`；D-05 定向 `85 passed`；Python 3.10～3.13 普通全量各 `389 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 与 Pyright `tool_providers.py` 均通过；fresh wheel/sdist 与 Twine 通过，wheel SHA256 `9277b948bff2c3b923f9c3f7c73236b85eee7a4275c21d9cd9d788c1cae8992d`、sdist SHA256 `a0fb20221ffe013de3ec010a234560caffd375ffa24c8c276d935bafe152005a`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：精确 HEAD `14fe2274d373e2e3a35443d3e0bedcb11f02bb28` 对应 push run `32410318053` 与 PR run `32410322758`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-05a BuiltinToolProvider

**状态：✅ 精确 HEAD `7a10d2ad575674fad063ffd3971e786bbb996854` 双 run 远端 gate green；未部署**

收口 `web_search` 等内置旁路，但不把外部结果的信任等级与 Provider 代码信任等同。

实现落点：新增 frozen `BuiltinToolProvider`，稳定身份为 `builtin / BUILTIN / TRUSTED`，并将当前唯一内置旁路 `web_search` 固化为 canonical `ToolSpec`。真实搜索适配器延迟导入既有 `Search`，并传递请求绑定的 `tool_snapshot`；Provider 本身只消费事务传入的 `BuiltinToolResources`，不读 I/O、不执行搜索。Registry 现包含 Registered/File/Generated/MCP/Builtin 五个 Provider，在 legacy merge 前统一拒绝跨来源重名；runtime 另外拒绝 NoneBot 插件与 Builtin 重名。candidate merge 与最终 `ToolSnapshot` 双重验证 canonical `ToolSpec` identity、generation、source/trust、artifact absence 和 dependencies。

信任边界：`TRUSTED` 只表示本地适配器代码来源，搜索结果仍作为 external observation 原样返回，不提升数据可信度。现有 `if web_search` consumer、开关、黑名单、超时、结果限制与执行路径继续保留，只共享同一 Schema/handler；`web_search` 仍不进入 legacy `custom_tools`，未修改 `ToolSnapshot` / `RuntimeSnapshot` dataclass schema。

本地门禁：D-05a 定向 `94 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4）与 3.13.13 普通全量各 `400 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 与 Pyright `0 errors, 0 warnings`。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `675e0be74944f38e8b63fa7f81db6facf15520b501659089ed3f8f2cd19f51da`、sdist SHA256 `7c56e3809a8c98e9110bbeae85b0160bfd6cd574817b8cf30940e908ca009bb1`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：包含 D-05a 的精确 HEAD `7a10d2ad575674fad063ffd3971e786bbb996854` 对应 push run `32414490980` 与 PR run `32414496386`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-05b NoneBotPluginProvider

**状态：✅ 精确 HEAD `531ff204b0746cc34fdda13a5b4fd4e60e2c3c58` 双 run 远端 gate green；未部署**

将受控伪事件适配器纳入统一目录；显式工具接口未覆盖的遗留插件仍保持当前权限语义与有界兼容通道。

实现落点：新增 `nonebot_plugin_tools.py` 与 frozen `NoneBotPluginProvider`，稳定身份为 `nonebot-plugin / NONEBOT_PLUGIN / REVIEWED`。每个 runtime candidate 从同一次 legacy `plugin_info` 构建 canonical `ToolSpec` 并保留精确 identity；默认权限继续为 `user`，遗留插件命令的 effect 保守标记为 `MUTATING`。Provider 只消费事务传入的 `NoneBotPluginToolResources`，不执行插件、不读 I/O、无 artifact。Registry 现包含 Registered/File/Generated/MCP/Builtin/NoneBot plugin 六个 Provider，并在 legacy merge 前集中拒绝跨来源重名。

等价门禁：candidate merge 与最终 `ToolSnapshot` 双重验证插件工具集合、精确 `ToolSpec` identity、description、permission/effect、generation、source/trust、artifact absence 与 Provider 声明 dependencies。canonical Schema 同时供 `build_tool_schema()` 和参数校验使用；canonical handler 仍进入已有 admission、队列、超时、结果与图片上限约束的 `EventSimulator`。本阶段 legacy `llm_tools` consumer 仍直接调用原 `dispatch_event()` 分支，未因 `MUTATING` 标记新增确认流程；categorize、LLM payload、pending action、search、管理命令和 MCP sidecar 均未切换，也未修改 `ToolSnapshot` / `RuntimeSnapshot` dataclass schema。

本地门禁：D-05b 定向 `102 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4）与 3.13.13 普通全量各 `411 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`，JUnit 为 0 failure / 0 error / 0 skip；Ruff 0.16.2 通过，Pyright 对 `nonebot_plugin_tools.py` / `tool_providers.py` 为 `0 errors, 0 warnings`。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `6a11e634ce272701d8d84441386c97dbd3729230397b055e00cccf91cea9470d`、sdist SHA256 `98200816ed260b6eb774894f4f34929c1d69a3edf747f51d5defcfde32fa1cee`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过。

远端证据：包含 D-05b 的精确 HEAD `531ff204b0746cc34fdda13a5b4fd4e60e2c3c58` 对应 push run `32417941584` 与 PR run `32417947550`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-06 Tool Trust Enforcement

**状态：✅ 精确 HEAD `c4ecaf9b7519b6c56fd5d20a6e5640993eb65f69` 双 run 远端 gate green；未部署**

`ToolTrustLevel` 枚举与来源身份已在 D-01a 定义；本任务只实施执行、选择、审计与管理策略。

```text
trusted
reviewed
untrusted
external
```

实现落点：新增 frozen `ToolTrustPolicy` / `ToolTrustDecision` 与 selection / execution / management 三类 operation。`ProviderCatalogSnapshot` 构造时为每个发现工具生成完整、不可变的 policy，固定精确 `provider_id/source/trust/generation/ToolSpec` identity；Registered/Builtin 在主进程，Custom File 走 isolated artifact，Generated 走 generated sandbox，MCP 走 external proxy，NoneBot 遗留适配器走 bounded event。MCP 与 `web_search` 的结果 provenance 为 external，Generated 为 untrusted，其余为 unverified；代码来源 trust 不会提升返回数据可信度。

策略门禁：selection 同时检查 effective permission，management 只允许超级用户，mutating execution 必须携带已验证的二阶段确认状态。未显式注册的 NoneBot 遗留适配器继续保留有界 compatibility 例外，不在 D-06 改变现有权限/确认语义，但其执行必须审计。所有拒绝、execution / management、非 trusted selection 与外部结果 selection 都返回 audit-required decision；`audit_metadata()` 只公开固定身份、策略、边界和结果 provenance，不包含调用参数或结果。catalog 缺失工具、来源/trust/provider/boundary/provenance 漂移与非 canonical Provider identity 均 fail closed。

兼容边界：本任务只提供 catalog policy API，未切换 categorize、LLM payload、`llm_tools`、pending action、search、管理命令或 MCP sidecar consumer；legacy 四字段、真实执行路径、`ProviderCatalogSnapshot.schema_version == 2` 以及 `ToolSnapshot` / `RuntimeSnapshot` dataclass schema 均保持不变。D-07 versioned capability digest/merge 未提前混入。

本地门禁：D-06 + Provider/Snapshot/Reload 定向 `98 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4）与 3.13.13 普通全量各 `418 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`，JUnit 为 0 failure / 0 error / 0 skip；Ruff 0.16.2 通过，Pyright 对 `tool_providers.py` 为 `0 errors, 0 warnings`。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `ee916eac21ed6e744b29adc0816c8e3886238a170c4e7aa39c8f2306317a79a9`、sdist SHA256 `b9f448b8977699616685eefefd753fe7b12068d7b6c29dc05c8ad07001f79817`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")` 与 packaged trust policy 检查全部通过。

远端证据：包含 D-06 的精确 HEAD `c4ecaf9b7519b6c56fd5d20a6e5640993eb65f69` 对应 push run `32420501280` 与 PR run `32420504608`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未部署。

---

## D-07 Capability Policy Merge

**状态：✅ 精确 HEAD `8846acd8334953367bd5ee2aa48844c992d2e9df` 双 run 远端 gate green；未部署**

```text
requested
detected
admin policy
```

合并成 effective policy。

实现落点：新增 frozen `ToolCapabilityV2`，严格解析 network / secrets allowlist、process、workspace/host filesystem read/write、database read/write 与 bot read/send/manage。effective policy 只能由 `requested ∩ admin` 派生；AST report 按 handler 固化 coarse detected evidence，Generated Tool 还将 `tests.py` 检测证据并入每个 handler，并强制 `detected ⊆ effective`。detected 只作为约束和审计证据，绝不自动授予权限；未知字段、非布尔值、不安全 allowlist、write-without-read 和 bot 层级倒置均拒绝。

版本与兼容边界：`ToolContractSnapshot` / `ToolArtifact` 默认升为 v2，`moellm-tool-artifact-v2` 摘要绑定 capability schema/detector version 以及 requested/detected/admin/effective 四份策略。v1 原摘要算法保持可验证；File/Generated Provider、legacy sidecar 与 `ToolSnapshot` dual-read v1/v2，并拒绝 bool 版本伪装、版本错配、sidecar 漂移及 v1 无法表达的 v2 scope。Custom/Generated sidecar 同时发布四份粗粒度能力与结构化 `capability_policy`；`ProviderCatalogSnapshot.schema_version == 3` 并提供递归不可变 capability policy 索引。

执行边界：当前 runner 只接受能精确投影为旧五布尔能力的 v2 policy；scoped network、filesystem 读写拆分、database/bot 等新权限在 D-08 consumer 迁移前明确 fail closed。categorize、LLM payload、`llm_tools`、pending action、search、管理命令和 MCP sidecar consumer 均未切换，真实执行与选择语义没有被静默改变。

本地门禁：D-07 + Provider/Snapshot/Reload 定向 `238 passed, 1 skipped`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 普通全量各 `442 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`，五份 JUnit 均为 0 failure / 0 error，Sandbox 为 0 skip；Ruff 0.16.2 通过，D-07 核心模块定向 Pyright 为 `0 errors, 0 warnings`。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `51b394c59dcc1624444ff4bde2915bf4191f7cfacf8d31361f9605a151e8d844`、sdist SHA256 `c325c0267e9aeb68eef97867f008c827fef8016ed48cb83049c45f219f7c0d1d`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")` 以及 packaged contract v2 / artifact v2 / catalog schema v3 检查全部通过。

远端证据：包含 D-07 的精确 HEAD `8846acd8334953367bd5ee2aa48844c992d2e9df` 对应 push run `32425100008` 与 PR run `32425104856`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 CLEAN。未合并、未发布、未部署。

---

## D-08 Consumer Cutover

**状态：✅ D-08a～D-08f 精确 HEAD 双 run 远端 gate green；未部署**

按 `categorize → llm_payload → llm_tools → pending action → search → 管理命令` 逐个切换，每个消费端单独保留新旧视图等价回归与可回滚开关。

### D-08a categorize

实现落点：`ToolSnapshot.get_brief_catalog()` 先生成 legacy rollback view；只有 catalog schema v3 且 `registered / custom-file / generated / mcp / builtin / nonebot-plugin` 六类 registration 完整时，才从 generation-bound `ProviderCatalogSnapshot` 构建新目录。工具身份、来源、selection trust decision、effective permission 与非 NoneBot 描述来自 canonical Provider 记录；legacy 映射只保留既有顺序和 NoneBot 展示字段。新旧目录逐次做完整字符串等价检查，任一漂移抛出 `ProviderConsumerParityError` 并 fail closed。`ToolManager.get_brief_catalog()` 的兼容入口也委托当前快照；启动期或旧式不完整 catalog 继续走有界 legacy。

回滚边界：新增严格布尔配置 `provider_catalog_categorize_enabled`，默认 `true`；设为 `false` 只回滚 categorize consumer，不改变其他 consumer、工具执行、生命周期或 sidecar。配置或显式测试 override 非布尔时拒绝。当前没有切换 `llm_payload`、`llm_tools`、pending action、search、管理命令或 MCP sidecar，也没有删除 legacy 字段。

本地门禁：D-08a 定向 `16 passed`，Provider/Snapshot/Reload 联合 `105 passed`；Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 普通全量各 `458 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`，五份 JUnit 均为 0 failure / 0 error，Sandbox 为 0 skip；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 对 `config.py` / `tool_manager.py` 的干净 HEAD 与当前树均报告同一组 8 个既有诊断，按文件、规则和消息完全一致，D-08a 新增行没有诊断；未为本任务修改无关旧问题。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `337ba9b0b24fef8cf6635fdd4758db0a27a509b8e7452b0726e13699bf0a9e48`、sdist SHA256 `924529d139465338a7bb213884d8ffd41cbdc6945422c40c38bb1e4da449eb39`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载、`reload("package-smoke")`、完整六 Provider registration、默认开关与 catalog parity 检查全部通过。

远端证据：D-08a 最终文档闭环精确 HEAD `760c95c7b1565bdd955c9b990b692c9fe097bdd5` 对应 push run `32427890454` 与 PR run `32427895162`；两者各 11 个 job 全绿、各恰好一个成功 `release-gate`，PR merge state 为 `CLEAN`。D-08b 依赖已解除。未合并、未发布、未部署。

### D-08b llm_payload

实现落点：实现提交 `761dbe2df47fc553090a7f36e0a71285b61b03c2` 将 `LlmPayloadMixin._build_payload()` 的 required/resident 工具集合交给请求绑定、generation-bound 的 `ToolSnapshot`。完整 schema v3 六 Provider catalog 下，工具身份、Provider 声明 dependencies、selection trust/effective permission 与 canonical schema 是新视图权威；legacy rollback view 仍先行构建，并与 Provider 视图逐次比较工具集合、依赖闭包和完整 schema，任一 legacy-only 附加依赖、缺失依赖或字段漂移均抛出 `ProviderConsumerParityError`，不会被吸收为 canonical。Registered/File/Generated 从 canonical spec 重现历史 `required: []`，MCP/NoneBot/Builtin 保持原参数形态。

回滚边界：新增严格布尔配置 `provider_catalog_llm_payload_enabled`，默认 `true`；设为 `false` 只回滚 payload consumer。启动期或旧式不完整六 Provider catalog 继续有界 legacy 兼容。本阶段未修改 `llm_tools` 执行、PendingAction、搜索执行、管理命令或 legacy sidecar，也未改变 categorize 的独立开关。

本地门禁：D-08b payload/snapshot 定向 `51 passed`，Provider/Snapshot/Reload 联合 `129 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 普通全量各 `474 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 分别为 22/19 个诊断，归一化后均为同一组 11 条既有消息，没有新增诊断类别。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `60773c8c026c1ec45a0fee70239e75e93a67169db55439c54ed5ea86a8251a56`、sdist SHA256 `fed859e711b6dd9fe7be04cb884ab6d3368f0c6ad7339508f1a78619e32ece68`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、完整六 Provider registration、默认开关与 `web_search` Provider/legacy schema parity 均通过。

远端证据：D-08b 最终文档闭环精确 HEAD `b1158a7debe86e74bba46aa9e652733fe3581bad` 对应 push run `32430209088` 与 PR run `32430214661`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-08c 依赖已解除。未合并、未发布、未部署。

### D-08c llm_tools

实现落点：实现提交 `c1f8580a1c8ebeca629fc8cfce015c63184cb0e6` 新增 frozen、generation-bound `LlmToolExecutionView` 以及 Builtin Search / Custom Tool / NoneBot Plugin 三类 route。完整 schema v3 六 Provider catalog 下，工具 identity、canonical source、精确 `ToolSpec` 与 execution trust decision 成为执行视图权威；legacy rollback view 仍逐调用构建并校验 route、source 与 `ToolSpec` identity。MCP 历史 sidecar 没有 `tool_spec`，因此严格比较 handler、description、parameters 与 name 的结构等价；任何漂移均抛出 `ProviderConsumerParityError`，未知工具也在进入 adapter 或副作用前拒绝。

安全与兼容边界：Provider execution decision 在实际执行前生效；权限拒绝 fail closed，只有 canonical `MUTATING` custom tool 的 confirmation-required denial 可以进入原有 PendingAction 二阶段确认过渡。NoneBot 改用 canonical Provider handler，仍进入既有 admission、队列、超时、结果与图片上限约束的 bounded event bus；`web_search` 改用 canonical builtin handler，但 Search 内部 extractor sidecar 留待 D-08e。新增严格布尔配置 `provider_catalog_llm_tools_enabled`，默认 `true`；设为 `false` 只回滚 `llm_tools` consumer。启动期或旧式不完整六 Provider catalog 继续有界 legacy 兼容；PendingAction 确认执行、Search extractor、管理命令及 legacy sidecar 均未切换。

本地门禁：D-08c 定向 `81 passed`，Provider/Snapshot/Reload 联合 `143 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `493 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 均为 55 个诊断，归一化后均为同一组 23 条既有消息，零新增、零删除。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `3032eef9888425f293441ccef96cedbf4de871cd85057a540e93a691e587db0a`、sdist SHA256 `609ae32d1bb0d213577637e5a4fbe7d6a6ad25f258de0764cb4d414aa22b6865`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、generation 1、完整六 Provider registration、默认开关与 `web_search` Provider authoritative execution view 均通过。四版本普通全量共享 `tests/.data`，因此最终证据来自串行运行，不采用此前并行产生的跨进程配置/临时工具竞争结果。

远端证据：D-08c 最终文档闭环精确 HEAD `bef9b56367e4b05cd31110216b84fd61a8158b38` 对应 push run `32432675246` 与 PR run `32432677694`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-08d 依赖已解除。未合并、未发布、未部署。

### D-08d pending action

实现落点：实现提交 `fbdc87235be13e9bd0fb9fe1b09791f8bd528ebf` 新增 frozen、generation-bound `PendingActionExecutionView`。完整 schema v3 六 Provider catalog 下，确认执行只接受 Registered / Custom File / Generated / MCP 四类 custom source，并以 canonical source、精确 `ToolSpec`、handler、bundle identity 与 `confirmed=True` execution trust decision 为权威。legacy rollback view 每次确认都校验 source/spec/bundle；MCP 历史 sidecar 没有 `tool_spec` 时严格比较 handler、description、parameters 与 name。Provider 路径从 canonical spec 构造执行 adapter，不把 legacy sidecar 当成 handler/spec 权威。

安全与回滚边界：nonce 仍在任何 parity、权限、参数校验或副作用前一次性消费；Bot/adapter/user/group、参数哈希、generation 与 bundle digest 绑定保持不变。确认阶段从命令捕获的 `RuntimeSnapshot` 读取开关，并按当前 actor、`confirmed=True` 重做 execution trust/permission 决策；错用户、旧 generation、版本/identity 漂移、普通用户确认 superuser 工具均 fail closed。新增严格布尔配置 `provider_catalog_pending_actions_enabled`，默认 `true`；设为 `false` 只回滚 PendingAction consumer。启动期或旧式不完整六 Provider catalog 继续有界 legacy 兼容；Search extractor、管理命令及 legacy sidecar 均未切换。

本地门禁：D-08d 定向 `129 passed`，Provider/Snapshot/Reload/Pending 联合 `171 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `509 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 均为 236 个诊断、归一化后均为 151 条既有消息，零新增、零删除。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `f063ebbb92b31c797c2c5b28e5aa57c6329415ede0d187f4617f269368d5a325`、sdist SHA256 `f43956bd09eeafcb6c65fcb3936be5c2f6d86fe0bc4d08d913d992611391aa4a`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、generation 1、完整六 Provider registration、默认开关与 canonical confirmed PendingAction view 均通过。

远端证据：D-08d 最终文档闭环精确 HEAD `2576fca54fc7086aca4716ef5f98864d5dd8d78e` 对应 push run `32434441897` 与 PR run `32434445098`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-08e 依赖已解除。未合并、未发布、未部署。

### D-08e search

实现落点：实现提交 `e26729db158023fba482ebe8c13cc99909f91ddf` 新增 frozen、generation-bound `SearchExtractorView`，并只切换 Search 内部 `extract_webpage` consumer。完整 schema v3 六 Provider catalog 下，仅 Registered / Custom File / Generated / MCP 四类 custom source 可作为 extractor，以 canonical source、精确 `ToolSpec` 与 selection trust decision 为权威；legacy rollback view 每次搜索都校验 source/spec identity，MCP 无 `tool_spec` 时严格比较 handler、description、parameters 与 name。Provider/legacy 任一缺失或漂移都在 Tavily 请求前 fail closed。

权限与回滚边界：当前 actor 的 `is_superuser` 从 `llm_tools` 经 canonical `web_search` handler 传到 Search；Provider 路径只有 selection 允许且 `extract_webpage` 未被黑名单命中时才披露来源 URL 与调用提示，拒绝时只披露标题。新增严格布尔配置 `provider_catalog_search_enabled`，默认 `true`；设为 `false` 只回滚 Search consumer，并保留历史 membership-only 语义。启动期、无请求快照或旧式不完整六 Provider catalog 继续有界 legacy 兼容；管理命令和 legacy sidecar 均未切换。

本地门禁：D-08e 定向 `121 passed`，Provider/Snapshot/Reload/Search 联合 `226 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `534 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 的 parent/current 均为 77 errors、3 warnings、80 条既有诊断，归一化 multiset 零新增、零删除。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `039779623e9a8617e2e4578bccf215edfe69d53e2407c088975375bdb7bc0587`、sdist SHA256 `4986c57c514a30934bd4c99c17b5de3caf84f1b1af876b78fab93d684cd4f736`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、generation 1、完整六 Provider registration、默认 Search 开关与 canonical extractor absence 均通过。

远端证据：D-08e 最终文档闭环精确 HEAD `9540938816f5a5b8e26fa9589f3be53b7a8f7ef4` 对应 push run `32438803052` 与 PR run `32438809768`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-08f 依赖已解除。未合并、未发布、未部署。

### D-08f 管理命令

实现落点：实现提交 `9238bd7ff415550ccc27fad750b573a023755403` 新增 frozen、generation-bound `ToolManagementView`，并只切换黑名单添加时的工具身份管理 consumer。完整 schema v3 六 Provider catalog 下，精确 Registered / Custom File / Generated / MCP / Builtin / NoneBot Plugin 目标以 canonical source、精确 `ToolSpec` 与 `ToolTrustOperation.MANAGEMENT` decision 为权威；legacy rollback view 每次添加均校验 source/spec identity，MCP 历史 sidecar 无 `tool_spec` 时严格比较 handler、description、parameters 与 name。Provider 管理决策对普通用户一律 fail closed，允许或拒绝均只记录不含调用参数的固定 audit metadata。

安全、事务与兼容边界：runtime candidate 将历史 loaded-plugin namespace 与 MCP configured-server selector 冻结进同一 `ToolSnapshot` generation；`mcp__server`、`mcp__server__*` 绑定该代全部 canonical MCP member，尚未发现工具的已配置服务仍可提前加入黑名单，但同样执行超级用户 selector policy 与审计。命令在预校验原子 reload 成功后捕获当前 `RuntimeSnapshot`，并从该快照读取独立严格布尔开关 `provider_catalog_management_enabled=true`；设为 `false` 只回滚管理 consumer，启动期或旧式不完整六 Provider catalog 继续有界 legacy。添加发生前的 reload/parity/trust 任一失败都不写配置；移除路径仍可清理已失效 blacklist 项并保留写后 reload 语义。常驻列表继续允许 stale 配置，由 D-08b payload 视图忽略未知项，未新增存在性限制。刷新/重载事务、其他 consumer 与 legacy sidecar 均未改变。

本地门禁：管理/Snapshot/Reload 定向 `148 passed`，Provider/Snapshot/Reload 与全部已切换 consumer 联合 `277 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `554 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2 与 diff check 通过。Pyright 1.1.407 对父提交/当前树同一 8 文件分别为 103/101 errors、2/2 warnings，归一化 multiset 零新增并消除 2 条旧诊断。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `297c079c82139e7de7fe6200b25bfa34ac258571bb7a2e0494d410af2ea6e170`、sdist SHA256 `a2768b84bdd16872097396aa6fae639c7a3e91b72ae922c37e243cd361cb0db8`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、`reload("package-smoke")`、generation 1、完整六 Provider registration、默认管理开关、canonical management decision 与空 MCP 服务 selector 均通过。

远端证据：D-08f 最终文档闭环精确 HEAD `ea022bd31020880c72a66802aa3f036389d0169d` 对应 push run `32443308534` 与 PR run `32443313095`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。D-08 consumer cutover 门禁已关闭，D-09 仍单独受发布周期观察门禁约束。未合并、未发布、未部署。

### D-08 依赖闭环

```text
D-08a categorize（远端 gate green）
  → D-08b llm_payload（远端 gate green）
  → D-08c llm_tools（远端 gate green）
  → D-08d pending action（远端 gate green）
  → D-08e search（远端 gate green）
  → D-08f 管理命令（远端 gate green）
```

每一项都必须有独立开关、legacy rollback view、Provider parity、定向/全量/打包门禁和精确 HEAD 远端 gate；前项未关闭时不提前实现后项。

---

## D-09 Legacy Sidecar Removal

**状态：🔒 发布周期观察门禁未满足；未实施**

只在全部 Provider、扩展 capability 和 D-08 消费端切换门禁通过，且至少完成一个发布周期的 parity 观察后，才单独删除 legacy sidecar。本任务不与 consumer cutover 混在同一 PR。

D-08f 远端 gate 已关闭，Provider/capability/consumer 前置条件已满足；当前仍没有可证明的发布周期 parity 观察，且本轮明确禁止生产操作，因此不得删除 sidecar、不得把测试或 CI 结果替代发布观察，也不提前标记 D-09 完成。该独立清理门禁不阻断不读取、不修改 legacy sidecar 的增量 Agent 领域对象，但任何 Milestone E 实现都不得借机移除兼容字段。

---

# Milestone E：0.27 Agent Runtime

**状态：✅ E-01～E-08 精确 HEAD 双 run 远端 gate green；D-09 继续锁定；未部署**

---

## E-01 AgentRun

实现落点：实现提交 `56dae036b4a2eaac8dd9060487e1bf1e18bb9e16` 新增 `agent_runtime.py`，定义 frozen、generation-bound `AgentRun` 与 `AgentRunState`。对象精确携带规划要求的 `run_id / request_id / user_id / group_id / generation / state / started_at / finished_at`；状态枚举覆盖正常链路 `CREATED → ADMITTED → CLASSIFYING → PLANNING → EXECUTING → WAITING_CONFIRMATION → SUMMARIZING → COMPLETED` 及 `FAILED / CANCELLED / TIMED_OUT / REJECTED` 异常终态。标识、正整数 request ID、非负 generation、有限非负时间戳与终态/结束时间一致性均严格校验，稳定 primitive `as_dict()` 为后续 audit/API/Repository 提供共同线协议。

边界：本任务只定义共享领域对象与固有数据不变量；不生成或存储 run，不接管 `request_manager`、`chat_runtime` 或任何 live Bot/Event，不实现 E-04 状态转换，不引入 Repository、PostgreSQL、Redis、迁移、生产配置，也不读取或删除 D-09 legacy sidecar。E-02 只能在 E-01 精确 HEAD 远端 gate 关闭后开始。

本地门禁：E-01 定向 `40 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `594 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `31e19c2d2dc83a7d0e12331664939e92e37958939e4fbf2cb8349248dadab237`、sdist SHA256 `f3acf3811ffdf59fbf3ade885bc47d44bc1736dfa08ba228c46ca09e6756fd4b`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration 与 packaged `AgentRun` 构造均通过。

远端证据：E-01 最终文档闭环精确 HEAD `be2e83d54db0021f909cad04e5bca7c6ac19fa12` 对应 push run `32444347880` 与 PR run `32444351420`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-02 依赖已解除。未合并、未发布、未部署。

---

## E-02 AgentStep

实现落点：实现提交 `29aa74ac7a03e2beb71b6834644171cdceeec50c` 在 `agent_runtime.py` 新增 frozen `AgentStep`、七类 `AgentStepType`、独立 `AgentStepStatus` 与递归 `AgentJsonValue`。对象精确携带 `step_id / run_id / index / type / model / tool / status / input / output / started_at / finished_at`；step/run identity、非负 index、model/tool 类型专属 identity、pending/running/五种终态时间线和非终态 output 均严格校验，不能以原始字符串伪造枚举或以不一致时间伪造完成状态。

输入输出边界：只接受 JSON primitive、字符串键 mapping 与 list/tuple；构造时递归脱离调用方并用只读 mapping/tuple 深冻结，拒绝非有限浮点、非字符串键、非 JSON 对象、循环引用与超过 32 层嵌套，`as_dict()` 每次返回可 JSON 化的新副本。E-02 不创建 step、不校验跨对象 index 唯一性、不接管请求/工具执行、不实现 E-04 状态转换，不引入 Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar。E-02 最终精确 HEAD 远端 gate 关闭后才开始 E-03。

本地门禁：AgentRun/Step 定向 `88 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `642 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine 通过，wheel SHA256 `65f2b3356c6bddd4e3c656adee9a55ff07058c6e1f4a62b40adb741a52f1f693`、sdist SHA256 `ed08e853c076872c69b20b70992c5aa7d78a9c3cf3ec216f0537fbe6e3c591cf`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged `AgentStep` 深冻结与序列化均通过。

远端证据：E-02 最终文档闭环精确 HEAD `8ca202ef0c53355567f44c740dd31f006377e72c` 对应 push run `32445217116` 与 PR run `32445220594`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-03 依赖已解除。未合并、未发布、未部署。

---

## E-03 ToolCall

实现落点：实现提交 `2b4af0ea847f18b074ade33f9f6abcb0520ce1cf` 在 `agent_runtime.py` 新增 frozen `ToolCall` 与独立 `ToolCallStatus`。对象精确携带 `tool_call_id / run_id / step_id / tool_name / bundle_digest / arguments / status / confirmed / result / elapsed`；工具名沿用统一安全命名规则，可选 bundle digest 只接受 64 位小写 SHA-256，identity、枚举与布尔字段均严格校验。状态覆盖 `pending / waiting_confirmation / running / completed / failed / cancelled / timed_out / rejected`；等待确认时 `confirmed` 必须为 false，非终态不得携带 result/elapsed，completed 必须携带 result，所有终态必须有有限非负 elapsed。

数据与执行边界：arguments 必须是字符串键 JSON object，arguments/result 复用 E-02 的有限、无环、最多 32 层 JSON 边界；构造时递归脱离调用方并深冻结，`as_dict()` 返回可 JSON 化的新副本。本任务不创建调用、不校验跨 AgentRun/AgentStep 的引用完整性，不保存 handler 或 live Bot/Event，不接管真实工具执行、PendingAction、Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar。E-04 只能在 E-03 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Agent runtime 定向 `118 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `672 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `2c18d53efad7d0fc2927d3d7949163aac1f3ea8960c18a56f46773119bcc2718`、sdist SHA256 `6472298ceee587548ad82978a110edcc639640ac09142b7b318c6daa2c2d25c7`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged `ToolCall` 深冻结与序列化均通过。

远端证据：E-03 最终文档闭环精确 HEAD `69fbf5e76e5c74f6f5b35df23c3d310830c84976` 对应 push run `32447053702` 与 PR run `32447055942`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-04 依赖已解除。未合并、未发布、未部署。

---

## E-04 Agent State Machine

实现落点：实现提交 `94a54a7196f7ede832490191f5dc15ae2999c2dc` 在 `agent_runtime.py` 新增纯、无内部状态的 `AgentStateMachine`。转换表严格执行 `CREATED → ADMITTED → CLASSIFYING → PLANNING → EXECUTING → SUMMARIZING → COMPLETED` 主链；确认是可选分支，`EXECUTING` 可进入 `WAITING_CONFIRMATION`，等待后可恢复 `EXECUTING` 或直接进入 `SUMMARIZING`。四类异常终态可从任一非终态进入，所有终态均无出边；跳级、自循环和终态重入一律拒绝。

数据与运行边界：`allowed_targets()` 返回不可变集合，`can_transition()` 拒绝原始字符串伪造枚举，`transition()` 保留 run 全部 identity/generation/started_at 并返回新的 frozen 对象。终态必须由调用方显式提供合法 `finished_at`，非终态不得伪造结束时间。策略不读取墙钟、不保存转换历史或 live Bot/Event、不提供跨请求/进程 CAS，不接管 request manager/chat runtime，不引入 Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar。E-05 只能在 E-04 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Agent runtime 定向 `166 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `720 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `0a29b10c7541abe151f625d52df21d9fbf142950317dd08ec5379ce366bf2bf2`、sdist SHA256 `d53f3b303f13438b82c0e19830f950eec3737784fa925c6b483c0a25e428f476`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 正常链/确认回路/终态重入拒绝均通过。

远端证据：E-04 最终文档闭环精确 HEAD `ba72157e323a1e95d99ba5a1516b40b7b0e56c0e` 对应 push run `32448482843` 与 PR run `32448485118`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-05 依赖已解除。未合并、未发布、未部署。

---

## E-05 DeadlineContext

实现落点：实现提交 `4bb8b30b57a9fcb7a4f4f8873281ce8dfdecdd47` 在 `agent_runtime.py` 新增 frozen `DeadlineContext`。`deadline_at` 是与 `time.monotonic()` 同源的有限非负绝对截止点；`from_timeout()` 在入口把有限非负总秒数转换为唯一截止点，`remaining()` 返回共享剩余预算并在过期后钳制为 `0.0`。布尔值、字符串、负数、NaN、无穷、非法时钟值和截止点加法溢出均严格拒绝。

数据与运行边界：Context 通过显式传参供所有后续组件共享，不保存 clock callable、不创建会延长总预算的分层 deadline，不序列化或跨进程重启持久化 monotonic 值。本任务尚未改写 `llm_api`、MCP、网络解析或工具执行的既有 timeout，不接管 request manager/chat runtime，不引入 Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar。E-06 只能在 E-05 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Agent runtime 定向 `185 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `739 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `12453d2f5cee45ebb6e1437bce761232f9812c4d8caf607a287ee430fec6a355`、sdist SHA256 `9b679b8f28c2a6a9a0c0ad96d20d771d9cffcaffdcde346c89e384d067cbe1c7`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 确定性/默认 monotonic 预算与过期钳零均通过。

远端证据：E-05 最终文档闭环精确 HEAD `3ac210b28ecda3019b822bf961196f63cfd795ce` 对应 push run `32449378433` 与 PR run `32449381716`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-06 依赖已解除。未合并、未发布、未部署。

---

## E-06 Tool Graph

实现落点：实现提交 `8af287e1a99e4f837dcbfb9a35071b17d5097c96` 新增 `tool_graph.py`，定义 frozen `ToolGraphRelation / ToolGraphEdge / ToolGraph`。关系词汇固定为有向 `depends_on` 与无向 `parallel_with / conflicts_with`；无向边端点自动规范化，图构造时深冻结并稳定排序工具、边、确认集合和 capability 映射。重复工具/边、未知节点、自引用、同一工具对多重关系、非法 capability 与二/多节点依赖环全部 fail closed。

数据与运行边界：查询 API 只确定性返回拓扑序、直接/传递依赖、直接 dependent、parallel/conflict 邻居及确认/capability 要求，`as_dict()` 返回可 JSON 化的新副本。本任务不选择或执行工具，不判断 read-only，不调度并发，不执行 E-08 冲突裁决，不保存 live Bot/Event，不接管 request manager/chat runtime，不引入 Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar。E-07 只能在 E-06 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Tool Graph 定向 `42 passed`，与 Agent Runtime 联合定向 `227 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `781 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `73bd52305aaf277481fe2c13ceafff0294744259f314d922e4ce289db3b9da3d`、sdist SHA256 `d8983ae6cbe30cc8d3f47516d6ea4968b627f9096ac1df6825ea99d7aff73351`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged ToolGraph 深冻结/确定性依赖/环路拒绝均通过。

远端证据：E-06 最终文档闭环精确 HEAD `95a780eeb36a9de1db506664f86fd02bef6968c9` 对应 push run `32450808012` 与 PR run `32450810445`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-07 依赖已解除。未合并、未发布、未部署。

---

## E-07 Read-only Parallel Tool Scheduler

实现落点：实现提交 `7c1c8aa961969eb876c26f26db40a463e737a94b` 新增 `tool_scheduler.py`，定义 frozen `ToolScheduleBatch / ToolSchedule`、强类型 `ToolScheduleMode` 与纯 `ReadOnlyParallelToolScheduler`。计划先验证选中工具、强类型 effect 映射与完整传递依赖闭包，再按 Tool Graph 的确定性拓扑 ready 集合分批；每批工具唯一，primitive `as_dict()` 每次返回新副本。

数据与运行边界：并行必须显式两两 `parallel_with`、全部为 `ToolEffect.READ_ONLY` 且无需确认，单批并行度限定 1～64；mutating、确认门禁与未知关系均保守退化为单工具串行。缺失依赖闭包直接拒绝；选中 conflict 不排序也不选赢家，而是 fail closed 并要求 E-08 policy。本任务不创建 asyncio task、不 `gather`、不调用 handler、不授权 capability、不消费或延长 DeadlineContext，不接真实 ToolCall/AgentStep、request manager/chat runtime、Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar；真实并发执行保留到 G-09。E-08 只能在 E-07 精确 HEAD 远端 gate 关闭后开始。

本地门禁：Tool Graph/Scheduler 定向 `79 passed`，与 Agent Runtime 联合定向 `264 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `818 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `546962c7f0d4963c4604b5ba6ceeff6bc1171434a58e7a5422e375a9356be771`、sdist SHA256 `57aa7d15e7219cf01ee8c0519e0546b4e15c7177b15319b1920c11ff402d7122`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 分层/只读并行/mutating 串行/conflict 拒绝均通过。

远端证据：E-07 最终文档闭环精确 HEAD `b1c942679bb29fb9b1dca111ce1c6641b9d44d50` 对应 push run `32452551080` 与 PR run `32452556938`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。E-08 依赖已解除。未合并、未发布、未部署。

---

## E-08 Tool Conflict Policy

实现落点：实现提交 `4892e57757dab124d2739183b6a91d6a67420073` 新增 `tool_conflicts.py`，定义 frozen `ToolConflictRule / ToolConflictDecision / ToolConflictResolution / ToolConflictPolicy` 与强类型 reject/prefer、allowed/rejected 状态。规则端点自动规范化；reject 禁止 winner，prefer 必须显式指定冲突端点之一。决议稳定记录规则是否显式、winner/loser、请求/保留/移除集合与拒绝原因，primitive `as_dict()` 每次返回新副本；拒绝状态强制 selected/dropped 为空，防止调用方误执行部分决议。

数据与运行边界：未配置 prefer 的选中冲突默认拒绝；Policy 规则必须与当前 ToolGraph 节点和真实 `conflicts_with` 边精确匹配，陈旧规则 fail closed。所有选中冲突对同时决议，任一 missing/explicit reject 就拒绝整次选择；全部 prefer 后统一移除 loser，移除全部工具或破坏 survivor 传递依赖闭包也整体拒绝。Policy 不按输入顺序、effect、权限或 capability 推断赢家，不读取配置/生产状态，不创建 task、不执行工具，也不修改 E-07 Scheduler；允许结果必须由调用方显式交给 Scheduler 并再次验证。本任务不接真实 ToolCall/AgentStep、request manager/chat runtime、Repository、PostgreSQL、Redis、迁移或生产配置，也不读取或删除 D-09 legacy sidecar。

本地门禁：Graph/Scheduler/Conflict 定向 `125 passed`，与 Agent Runtime 联合定向 `310 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `864 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `f33107c781b2eefac8e95577e891b4b58795f1979e6a0313d8418c7819cd644c`、sdist SHA256 `7a1ca883584d30d41edc5c7bad58c41b9c88f065886fbddbb0fc1658e3f5790b`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged 默认拒绝/显式赢家/依赖破坏拒绝/安全交接 Scheduler 均通过。

远端证据：E-08 最终文档闭环精确 HEAD `11a7ca10c5400d4b776efa4824ffa11b9ad0de00` 对应 push run `32454187768` 与 PR run `32454191418`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-01 依赖已解除。未合并、未发布、未部署。

---

# Milestone F：0.28 PostgreSQL + Redis

**状态：✅ F-01～F-14、G-01 与 G-02 精确 HEAD 双 run 远端 gate green；G-03 依赖已解除；未连接真实数据库/Redis；未部署**

---

## F-01 Repository Interface

实现落点：实现提交 `71c9b4ceafe6bc5e4a0d16349d28bb7375f8dbbb` 新增 `repositories.py`，用 runtime-checkable 异步 `Protocol` 定义 `Conversation / Message / AgentRun / AgentStep / ToolCall / Tool / Usage / Audit` Repository 与显式 `RepositoryTransaction`。Agent 类型只在 `TYPE_CHECKING` 导入，避免接口模块产生运行时耦合。`RepositoryPageRequest` 把 limit 限定为 1～200，并验证最长 512 字符的安全 opaque cursor；frozen `RepositoryPage` 只接受 tuple，空页不得提供 next cursor。错误明确区分乐观并发冲突与后端不可用。

并发边界：`AgentRunRepository.replace()` 强制调用方同时提供 `expected_state + expected_generation`，`ToolCallRepository.replace()` 强制提供 `expected_status`，为后续 PostgreSQL 实现预留单条条件更新语义。本任务只定义 backend-neutral 端口，不提供内存或数据库实现，不安装 SQLAlchemy/asyncpg/Alembic，不建表、不迁移、不创建 engine/session、不读取生产配置，也不连接 PostgreSQL 或 Redis；D-09 legacy sidecar 保持原样。

本地门禁：Repository + Agent Runtime 定向 `216 passed`，联合 Graph/Scheduler/Conflict 为 `341 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `895 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2 全量、目标文件 format、staged diff 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `373dcba1bcc9c782d933400dad2ac1215c3c754da455f02b17aae19211a76889`、sdist SHA256 `61c773e8780aade1766ac257f8d05f015851495ff307865ef386492ae4d8d9ff`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged Repository Protocol/分页/CAS 签名均通过。

远端证据：F-01 最终文档闭环精确 HEAD `678adb423e87fef8a851a8a792ae9c39a268dc15` 对应 push run `32455829891` 与 PR run `32455828489`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-02 依赖已解除。未合并、未发布、未部署。

---

## F-02 SQLAlchemy Async Engine

实现落点：实现提交 `cf7c236c3f78c0775ff513f291ef4a55a877e54d` 新增 `database_engine.py`，并在运行依赖加入 `sqlalchemy>=2,<3` 与 `asyncpg>=0.30,<1`。frozen `DatabaseEngineSettings` 只接受显式 `postgresql+asyncpg` URL 和非空数据库名，拒绝控制字符、超长 DSN 与 query 凭据字段；原始 DSN 字符串不持久保留，私有 URL wrapper、`repr()`、错误和 `safe_diagnostics()` 均不渲染凭据或 endpoint。pool size 1～100、overflow 0～100 且总量不超过 150；pool/connect/statement/recycle timeout 均有界，固定启用 pre-ping、LIFO、参数隐藏及 asyncpg 客户端/服务端 statement timeout。

生命周期边界：`DatabaseEngineManager` 只在运行中的 event loop 内惰性创建，每实例至多一个 `AsyncEngine`；创建 pool 对象不 checkout 连接。同一 manager 绑定首次创建的 PID 与 event-loop，跨进程/loop 复用、释放中访问及重复并发 dispose 全部 fail closed。成功释放后可重建；取消或释放失败恢复可重试状态，初始化/释放异常只公开类型，不串联可能含 DSN 的原异常。本任务不创建全局 manager，不读取插件配置、环境 DSN 或 secret file，不注册 startup/shutdown，不调用 `connect()`，不创建 session、Repository 实现、Schema、Alembic 或 Redis client，也不触碰 D-09 legacy sidecar。

本地门禁：F-02 定向 `50 passed`，与 Repository/Agent/Graph/Scheduler/Conflict 联合 `391 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 最终普通全量各 `945 passed, 1 skipped`。Python 3.11 首轮有一个既有 watcher 重试 node 在 3 秒门限发生时序超时；该 node 随后连续 `5/5` 通过，完整 3.11 全量重跑通过，未修改无关 watcher。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2 全量、目标文件 format、staged diff 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `c5c59aa4c556a4f16bb98a27c979ee174b2d1e46c5695f5ce596a7c617416c8c`、sdist SHA256 `c932056e00dbada7d55ab07f196996edc6daf3d6b6a5f3a31da6a09e79e4732f`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、SQLAlchemy/asyncpg 依赖元数据、凭据脱敏、惰性单 engine 与 `checkedout=0` 均通过。

远端证据：F-02 最终文档闭环精确 HEAD `f8292f94c2dbeab80949436b495ee997382b5cac` 对应 push run `32458307603` 与 PR run `32458311280`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-03 依赖已解除。未合并、未发布、未部署。

---

## F-03 Alembic

实现落点：实现提交 `f9598561247e40a5ce8327a0ccd8d9f21f3fe04e` 加入 `alembic>=1.13,<2`，新增空 `database_metadata` 及确定性 constraint/index naming convention，并把 `env.py`、revision template 与空 `versions/` 布局明确打入 wheel/sdist。`database_migrations.py` 只构造内存 Config，不读取 ini、插件 JSON、环境变量、secret file 或 `sqlalchemy.url`；revision selector 最长 128 字符，graph 必须保持无 merge、branch label、`depends_on` 的单一线性 base/head。

执行边界：只开放离线 PostgreSQL upgrade SQL 渲染，在线路径无条件抛出 `DatabaseMigrationOnlineDisabledError`，外部 Config 缺少显式 offline marker 或携带 URL 也 fail closed。当前 graph 为空；renderer 与 env 双重短路空图，避免最低支持 Alembic 1.13 生成误导性的 `DROP TABLE alembic_version`。本任务不创建任何 revision、业务表、ORM model、Repository 实现、engine/session 或生命周期接线，不读取生产 DSN，不调用 F-02 manager，不连接 PostgreSQL/Redis；D-09 legacy sidecar 保持原样。首个业务 Schema 与 revision 属于 F-04。

本地门禁：F-03 定向 `26 passed`，与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `417 passed`；Python 3.10.20（额外覆盖 Alembic 1.13.0）、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 严格串行普通全量各 `971 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2 全量、目标文件 format、diff check 与 Pyright 1.1.407 目标/测试文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `834c709638f6b618a4765ed5ad490678405bd761dcc61aa172290648701bba23`、sdist SHA256 `7a39dc3b3aaaa2c55e8d205dd7a555fcb3cae12338e09bbf1f42c7172b472935`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载均确认 Alembic 依赖、四份 packaged migration resource、`revisions=0` 与离线 SQL 0 字节。

远端证据：F-03 最终文档闭环精确 HEAD `a4eb771678587e6bfd32f793c8a6f7eda88f29ab` 对应 push run `32461256977` 与 PR run `32461262286`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-04 依赖已解除。未合并、未发布、未部署。

---

## F-04 User / Conversation / Message Schema

实现落点：实现提交 `21810cf836d89d07c268076d6e3d96b34cdfd04b` 新增纯声明式 `database_schema.py` 与首个线性 revision `0001_users_conversations`。`users.id / conversations.id` 为最长 128 字符的应用生成 ID；`messages.id` 为 PostgreSQL `BIGINT IDENTITY`，支持 `(conversation_id, id DESC)` 稳定分页；`structured_content` 为 JSONB，全部时间字段带时区。用户平台身份使用唯一约束，群聊/私聊范围及会话内平台消息 ID 使用 partial unique index；外键删除策略全部为 `RESTRICT`。

迁移边界：packaged graph 现在精确为单一 `revision/base/head = 0001_users_conversations`。离线 upgrade 创建 `users → conversations → messages` 及约束/索引且不含 `DROP`，离线 downgrade 按逆依赖顺序删除；临时空图继续保持零 SQL。metadata/revision parity 测试覆盖列顺序、类型、nullability、server default、Identity、PK/FK/unique/check/index 名称和 PostgreSQL 条件。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13 与 3.13.13 定向各 `32 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `423 passed`；四版本严格串行普通全量最终各 `977 passed, 1 skipped`。Python 3.13 首轮有一个既有 watcher 3 秒时序 node 超时；该 node 随后连续 `5/5` 通过，完整 3.13 全量重跑通过，未修改无关 watcher。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2、format/diff check 与 Pyright 目标/测试文件 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `7c22c093605dd4213e8c5bb8751fa81f26925cc45e56d391366016762d679739`、sdist SHA256 `b5b52c0125fea1a9f565e531241fb19fd20bfb2a89e00d325a28a47d9b88c262`；两种制品均包含 Schema、env/template 与首个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认依赖元数据、三张表、单线 graph、Identity/JSONB/partial index/`RESTRICT` DDL，并在 engine 创建函数被替换为拒绝桩时完成离线渲染。

远端证据：F-04 最终文档闭环精确 HEAD `9a343cfcc71a2824257afd9f7537edf4ab8af4f2` 对应 push run `32463913845` 与 PR run `32463917189`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-05 依赖已解除。未合并、未发布、未部署。

---

## F-05 AgentRun Schema

实现落点：实现提交 `c177fc51e73b3961617cc2b09082ceeb0e436897` 在共享 metadata 新增 `agent_runs`，并以不可变 revision `0002_agent_runtime` 追加到 `0001_users_conversations`。字段覆盖规划要求的 `id / request_id / user_id / group_id / conversation_id / generation / model / status / started_at / finished_at / input_tokens / output_tokens / cost / error_type / error_message`；应用生成 ID 最长 128 字符，request/generation/token 使用 BIGINT，cost 使用 `NUMERIC(24, 12)`，时间字段均带时区。

领域与并发边界：`status` 值域精确绑定现有 `AgentRunState`，五种终态必须带结束时间，其他状态必须保持 `finished_at IS NULL`；结束时间不得早于开始时间，generation/token/cost 不得为负。`request_id` 仅为当前进程请求编号，重启后可能复用，因此不设唯一约束。`user_id / conversation_id` 为非空 `RESTRICT` 外键；会话时间线索引包含 `started_at DESC, id DESC` 稳定游标，另有用户时间线与状态恢复索引。`generation + status` 列可供后续 Repository 单条条件更新，但本阶段不实现 Repository。

迁移边界：packaged graph 为 `0001_users_conversations → 0002_agent_runtime` 的单 base/head 线；F-05 只创建 `agent_runs`，F-06/F-07 必须追加后续 revision，不能回改已门禁 revision。离线 `0002:0001` downgrade 只删除 `agent_runs`，不触碰 F-04 三张表；metadata/revision parity 覆盖精确 PostgreSQL 类型、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13 与 3.13.13 定向各 `35 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `426 passed`；四版本严格串行普通全量各 `980 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2、format/diff check 与 Pyright 目标/测试文件 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `1d47c5d1eee9686c8e642fb8d52bfb50e01894a2a71c7ba560ac16df5d8c8f8b`、sdist SHA256 `41a8d65dd33c8343bcb64e91444e220099bcdf1a66fe210d6974f4f3cb5de2f1`；两种制品均包含两个 revision 且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认领域状态 parity、四张表、双 revision graph、Numeric/TIMESTAMPTZ/FK/index DDL 与定向 downgrade，并在 engine 创建函数被替换为拒绝桩时完成离线渲染。

远端证据：F-05 最终文档闭环精确 HEAD `d23e156e4df44442bc9b7382fef5e53c88433148` 对应 push run `32465645519` 与 PR run `32465649984`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-06 依赖已解除。未合并、未发布、未部署。

---

## F-06 AgentStep Schema

实现落点：实现提交 `ea405674e38082a5089304789a1628024da7d2ec` 在共享 metadata 新增 `agent_steps`，并以不可变 revision `0003_agent_steps` 追加到 `0002_agent_runtime`。字段覆盖规划要求的 `id / run_id / step_index / step_type / model / tool_name / status / started_at / finished_at / duration_ms / input_preview / output_preview / error`；应用生成 ID 最长 128 字符，step index/duration 使用 BIGINT，时间字段均带时区。

领域与数据边界：`step_type / status` 值域精确绑定现有 `AgentStepType / AgentStepStatus`，MODEL/TOOL 类型必须分别绑定 model/tool identity。pending 不得携带时间或 duration，running 必须只有开始时间，五种终态必须同时携带起止时间与非负 duration；结束时间不得早于开始时间。input/output/error 仅保存最长 6000 字符的非空预览，不保存完整领域 JSON；非终态不得携带 output，completed 不得携带 error。`run_id` 为非空 `RESTRICT` 外键，`(run_id, step_index)` 唯一约束同时提供稳定顺序。

迁移边界：packaged graph 为 `0001_users_conversations → 0002_agent_runtime → 0003_agent_steps` 的单 base/head 线；F-06 只创建 `agent_steps`，F-07 必须追加后续 revision，不能回改已门禁的 `0001`～`0003`。离线 `0003:0002` downgrade 只删除 `agent_steps`，不触碰前四张表；metadata/revision parity 覆盖精确 PostgreSQL 类型、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13 与 3.13.13 定向各 `38 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `429 passed`；四版本严格串行普通全量各 `983 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2、format/diff check 与 Pyright 目标/测试文件 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `edcaac6c69f337d70b078dc0679b360db6bcc9d5228c39606380fbb0d2afeb80`、sdist SHA256 `b6dffce54dfed796625707e932881d996a52f6759e63e62752fd04903d1e5a26`；两种制品各 64 个文件，均包含三个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认领域 type/status parity、五张表、三 revision graph、TIMESTAMPTZ/FK/unique/check DDL 与定向 downgrade，并在 engine 创建函数被替换为拒绝桩时完成离线渲染。

远端证据：F-06 最终文档闭环精确 HEAD `4e5cd600b1efa430bb785bdc5cb7f6a49988be9a` 对应 push run `32467140779` 与 PR run `32467144569`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / CLEAN`。F-07 依赖已解除。未合并、未发布、未部署。

---

## F-07 ToolCall Schema

实现落点：实现提交 `83a571fbc79e13ce68f237ba7ed9c653607fbb66` 在共享 metadata 新增 `tool_calls`，并以不可变 revision `0004_tool_calls` 追加到 `0003_agent_steps`。字段覆盖规划要求的 `id / run_id / step_id / tool_name / tool_source / bundle_id / bundle_digest / arguments_json / result_preview / confirmed / confirmation_id / status / duration_ms / created_at / finished_at`；arguments 为 JSONB object，result 只保存最长 6000 字符预览，不保存完整结果 JSON。

领域与数据边界：`status / tool_source` 值域精确绑定现有 `ToolCallStatus / ToolSource`。Generated 来源必须同时绑定合法 bundle ID 与 64 位小写 digest，其他来源不得伪造 bundle identity。waiting_confirmation 必须保持未确认并绑定 confirmation ID，confirmed 记录也必须绑定 confirmation ID；非空 confirmation ID 以 partial unique index 防止一份待确认动作被复用。五种终态必须携带结束时间与非负毫秒 duration，非终态不得携带 result，completed 必须携带有界 result preview。

引用与查询边界：`run_id` 以 `RESTRICT` 指向 `agent_runs`；为防止跨 run 错挂 step，新 revision 为 `agent_steps(run_id, id)` 追加支持约束，并使用 `(run_id, step_id)` 复合 `RESTRICT` 外键。run 时间线索引使用 `(run_id, created_at DESC, id DESC)` 稳定游标，另有 step 时间线与状态恢复索引。F-07 只声明 Schema，不实现 Repository 映射或真实持久化。

迁移边界：packaged graph 为 `0001_users_conversations → 0002_agent_runtime → 0003_agent_steps → 0004_tool_calls` 的单 base/head 线；不回改已门禁的 `0001`～`0003`，F-08 必须追加后续 revision。离线 `0004:0003` downgrade 先删除 `tool_calls` 再删除复合父键支持约束，不触碰前五张表；metadata/revision parity 覆盖精确 PostgreSQL 类型、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13 与 3.13.13 定向各 `41 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `432 passed`；四版本严格串行普通全量各 `986 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0；Ruff 0.16.2、format/diff check 与 Pyright 目标/测试文件 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `2ce8bf699fe7919cfca345d90833be99e2453e5025adf9513a9d230eb96f4b4f`、sdist SHA256 `54a6a8f44d3da165c7fe34704d60d4765164877a4c7b6fbc48c605957f819424`；两种制品各 65 个文件，均包含四个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认领域 status/source parity、六张表、四 revision graph、JSONB/复合 FK/unique/check DDL 与定向 upgrade，并在无生产配置的临时 cwd 完成 `reload("package-smoke")`。

远端证据：F-07 最终文档闭环精确 HEAD `dcff410498a862bed302687e1383cab0f554da6c` 对应 push run `32469057942` 与 PR run `32469061094`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-08 依赖已解除。未合并、未发布、未部署。

---

## F-08 Tool Bundle Metadata Schema

实现落点：实现提交 `7afa3c81a6604a09533b0b1b487d3c484f9f1909` 在共享 metadata 新增 `tool_bundles / tool_bundle_versions`，并以不可变 revision `0005_tool_bundle_metadata` 追加到 `0004_tool_calls`。字段覆盖 bundle 应用 ID/natural ID/description/时间/active pointer，以及 version 应用 ID/bundle/digest/manifest/source/tests/state/risks/capabilities/完整生命周期时间。

领域与数据边界：版本状态值域精确绑定 `VersionState` 的 `approved / activated / deprecated / archived`；为与当前领域对象一致，规划概要之外显式补齐 `archived_at`。manifest、risks、capabilities 分别要求 JSONB object/array/object 且均有 64 KiB 上限；manifest natural bundle identity 必须与版本列一致。源码与测试源码各限制为 1～65536 bytes，不在本表保存 handler、live Bot/Event 或凭据。

引用与查询边界：`tool_bundle_versions.bundle_id` 以 `RESTRICT` 指向 bundle natural ID，`(bundle_id, digest)` 唯一。bundle 的 `(bundle_id, active_version_id)` 复合 `RESTRICT` 外键只能指向同 bundle 版本，拒绝跨 bundle 错挂；版本侧 partial unique index 保证每个 bundle 至多一个 activated 版本。bundle 更新时间线、版本稳定时间线与状态恢复索引均已声明。跨表 active pointer 与 version state 的最终一致性仍由后续 Repository 事务/CAS 负责，本阶段不伪造触发器或 runtime 接线。

迁移边界：packaged graph 为 `0001_users_conversations → 0002_agent_runtime → 0003_agent_steps → 0004_tool_calls → 0005_tool_bundle_metadata` 的单 base/head 线；未修改已门禁的 `0001`～`0004`，F-09 必须追加后续 revision。为处理两张新表间的循环引用，upgrade 先建 bundle、再建 version，最后追加 active pointer 外键；离线 `0005:0004` downgrade 先删除该外键，再按 `tool_bundle_versions → tool_bundles` 删除，不触碰 `tool_calls` 及前六张表。metadata/revision parity 覆盖精确 PostgreSQL 类型、全部约束和索引；在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `44 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `435 passed`；四版本严格串行普通全量各 `989 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2、format/diff check、PostgreSQL 标识符上限检查与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `441964bdd651746d1a61eadea63ea389ea40e42fd0bfe3599d1921ecc93230cf`、sdist SHA256 `6968f782e685c7fdfc55b5fa2d3c456d0a86f8dbb677c262ca9bd5639d60ee92`；两种制品各 66 个文件，均包含五个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 8 张表、五段单线 graph、VersionState parity、JSONB/复合 FK/partial unique/check DDL、定向 downgrade 与 `reload("package-smoke")`。

远端证据：F-08 最终文档闭环精确 HEAD `6064c5beb387d06c796439255e3159310ecb70b6` 对应 push run `32578200654` 与 PR run `32578203172`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-09 依赖已解除。本阶段不接 legacy sidecar、runtime 或 Repository，不创建全局 engine/session，不读取生产 DSN，不运行 migration，不连接 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

---

## F-09 Audit Schema

实现落点：实现提交 `6fe1a4cf57cfec7c7d21342a32b19632a7c7de12` 在共享 metadata 新增 `audit_events`，并以不可变 revision `0006_audit_events` 追加到 `0005_tool_bundle_metadata`。字段覆盖规划要求的 `id / event_type / actor_user_id / actor_type / target_type / target_id / run_id / tool_call_id / metadata_json / created_at`；事件 ID 使用 PostgreSQL `BIGINT IDENTITY`，event/actor/target 类型使用有界 canonical token，metadata 必须是最多 64 KiB 的 JSONB object。

引用与查询边界：actor user 与 run 分别以可选 `RESTRICT` 外键指向 `users / agent_runs`；tool call identity 必须同时绑定 run。新 revision 为 `tool_calls(run_id, id)` 追加支持约束，并以 `(run_id, tool_call_id)` 复合 `RESTRICT` 外键拒绝跨 run 错挂。run、tool call、actor、target 与 event type 五类稳定时间线索引均包含 `created_at DESC, id DESC`。多态 target 不伪造跨表 FK；本阶段不把现有日志、参数、结果或 trust decision 接入持久化。

迁移边界：packaged graph 为 `0001_users_conversations → 0002_agent_runtime → 0003_agent_steps → 0004_tool_calls → 0005_tool_bundle_metadata → 0006_audit_events` 的单 base/head 线；未修改已门禁的 `0001`～`0005`，F-10 必须追加后续 revision。离线 `0006:0005` downgrade 先删除 `audit_events`，再删除新复合支持约束，不触碰 bundle/version、`tool_calls` 或前六张表；metadata/revision parity 覆盖精确 PostgreSQL 类型、Identity、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `47 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `438 passed`；四版本严格串行普通全量各 `992 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2、目标文件 format、diff check、216 个 PostgreSQL 命名项上限检查（最长 52）与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `72e04d75283bc7624b15608b3948922ed30d4d5775f2c5298c943ce1b7e2266e`、sdist SHA256 `70fa3d5fd779c2f83f9f31ab5b5705711cc99255da07acf6a5e131936874a5a2`；两种制品各 67 个文件，均包含六个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10 × wheel/sdist 使用 fresh venv 完整安装，Python 3.12 × wheel/sdist 在已验证的仓库外依赖环境中强制重装上述精确制品；四组均确认 9 张表、六段 graph、JSONB/复合 FK/unique/check DDL、定向 downgrade 与 `reload("package-smoke")`。

远端证据：F-09 最终文档闭环精确 HEAD `be2b3ab14fb7b9ce0d712fc52a2fa96830364993` 对应 push run `32580016797` 与 PR run `32580019661`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-10 依赖已解除。本阶段不接 legacy sidecar、runtime 或 Repository，不创建全局 engine/session，不读取生产 DSN，不运行 migration，不连接 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

---

## F-10 Usage Schema

实现落点：实现提交 `f96b1ffadf43283c77365246b1b065379c013c2e` 在共享 metadata 新增 `model_usage`，并以不可变 revision `0007_model_usage` 追加到 `0006_audit_events`。字段覆盖规划要求的 `id / run_id / provider / model / input_tokens / output_tokens / reasoning_tokens / cached_tokens / cost / created_at`；ID 使用 PostgreSQL `BIGINT IDENTITY`，run 使用非空 `RESTRICT` 外键，provider/model 为有界非空原始标识，四类 token 均为显式非负 BIGINT，cost 使用可空 `NUMERIC(24, 12)` 以区分未知成本与零成本。

数据与查询边界：不强行假定跨供应商 reasoning/cache 与 output/input 的包含关系；run 稳定时间线、provider+model 聚合时间线与全局时间线均包含 `created_at DESC, id DESC`。用户/群统计通过 run 关联，不在 usage 表重复身份。本阶段不改现有 50 条内存 `token_usage_history`，不接 `llm_api`、UsageRepository、批量写入或计价规则。

迁移边界：packaged graph 为 `0001_users_conversations → 0002_agent_runtime → 0003_agent_steps → 0004_tool_calls → 0005_tool_bundle_metadata → 0006_audit_events → 0007_model_usage` 的单 base/head 线；未修改已门禁的 `0001`～`0006`。离线 `0007:0006` downgrade 只删除 `model_usage`，不触碰 `audit_events`、`agent_runs` 或其他既有表；metadata/revision parity 覆盖精确 PostgreSQL 类型、Identity、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `50 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `441 passed`；四版本严格串行普通全量各 `995 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2、目标文件 format、diff check、233 个 PostgreSQL 命名项上限检查（最长 52）与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `b6e1dc17c58b7bca86ea00b84d30f693887d267e25023995d202a3ee32d8df57`、sdist SHA256 `9460a1f640ad2129ec79725637a758eda95161c30d96ab8388f8a38b896f1fec`；两种制品各 68 个文件，均包含七个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组已验证仓库外依赖环境均强制重装上述精确制品，并确认 10 张表、七段 graph、BIGINT/Numeric/FK/check/index DDL、定向 downgrade 与 `reload("package-smoke")`。

远端证据：F-10 最终文档闭环精确 HEAD `a55510697e05b4f0c17d20d36dd91643e8776890` 对应 push run `32580881668` 与 PR run `32580884647`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-11 依赖已解除。本阶段不接 legacy sidecar、runtime 或 Repository，不创建全局 engine/session 或 Redis client，不读取生产 DSN，不运行 migration，不连接 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

---

## F-11 Redis Client

实现落点：实现提交 `a98f9298e1bbf461498c46b689eadffcf606fcf1` 加入运行依赖 `redis>=5.2,<7`，并新增独立 `redis_client.py`。frozen `RedisClientSettings` 只接受显式 `redis://` / `rediss://`、合法 host 和单一 0～65535 database path，拒绝控制字符、错误百分号编码、超长 URL、query 覆盖与 fragment；原始 URL 只保存在私有 redacted wrapper，`repr()`、错误与 `safe_diagnostics()` 均不渲染 username、password 或 endpoint。`rediss` 固定启用证书校验和 hostname check；pool 上限 1～1000，connect/read timeout 与 health-check interval 均有界。

生命周期边界：`RedisClientManager` 只在运行中的 event loop 内惰性创建，每实例至多一个 redis-py asyncio client/pool；创建 client/pool 不执行 `PING`、DNS 或 socket connect。manager 绑定首次创建的 PID 与 event-loop，跨进程/loop 复用、关闭中访问及并发重复关闭全部 fail closed。成功关闭后可重建；取消或关闭失败恢复可重试状态，初始化/关闭异常只公开类型且不串联可能含 URL 的原异常。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `51 passed`，Redis 5.2.0 最低依赖兼容定向另为 `51 passed`；与 Database Engine/Migrations/Schema、Repository、Agent、Graph、Scheduler、Conflict 联合 `492 passed`。四版本严格串行普通全量最终各 `1046 passed, 1 skipped`；Python 3.12 首轮有一个既有 watcher 3 秒时序 node 超时，该 node 随后连续 `5/5` 通过，完整 3.12 全量重跑通过，未修改无关 watcher。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2 全量、目标文件 format、diff check 与 Pyright 1.1.407 目标/测试文件 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `dfbf90d8c3b1fe52ea199fbc3e4e6e44e0b5ef9a90df6421dcda3c885045ca0e`、sdist SHA256 `4fc5552c91f6f79c3dc7cdd0853c5d20ae662992eee9e1ce135cab0bf82832ec`；两种制品各 69 个文件，均包含 Redis Client、精确 Redis 依赖与七个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组已验证仓库外依赖环境均强制重装上述精确制品，并确认 Redis 6.4.0 元数据、TLS/pool 参数、10 张表、七段 graph、离线 DDL、定向 downgrade 与 `reload("package-smoke")`，Redis connect 计数始终为 0。

远端证据：F-11 最终文档闭环精确 HEAD `13383aee25fe90e8ecd3542a3df9af748f2e11f0` 对应 push run `32583576588` 与 PR run `32583578903`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-12 依赖已解除。本阶段不创建全局 manager/client，不读取插件配置、环境 Redis URL 或 secret file，不注册 startup/shutdown，不实现 PendingAction/Cooldown/Admission Redis，不接 legacy sidecar、Repository 或 runtime，不连接 Redis/PostgreSQL；D-09 保持锁定。未合并、未发布、未部署。

---

## F-12 PendingAction Redis

实现落点：实现提交 `ca992e967af943b4d9f1067c26deef762aceee4a` 新增 backend-neutral `PendingActionStoreProtocol` 与独立 `redis_pending_actions.py`。frozen `RedisPendingActionSettings` 把 key prefix、TTL、namespace 总容量、参数字节、失败窗口/次数/key 数与 WATCH 重试数全部限制在硬边界内；`RedisPendingActionStore` 只接受显式 redis-py asyncio client 注入，不读取插件配置、环境 Redis URL 或 secret file，不创建全局 client/store。现有内存 `pending_action_store` 及 runtime 默认行为保持不变；`execute_pending_action()` 仅在调用方显式传入 store 时使用 Redis，显式 falsey backend 也不会回退内存。`fakeredis>=2.31,<3` 仅为 dev/CI 测试依赖，不进入运行制品。

数据与 namespace 边界：所有 action、caller/tool slot、action/slot expiry index、caller failure key 与 failure expiry index 使用同一 `{pending-action}` Cluster hash tag，并由 1～96 位安全前缀隔离。Record schema 精确绑定 action ID、Bot/adapter/user/group、tool、canonical arguments JSON/hash、generation、bundle digest、created/expires 时间、nonce 及 caller/slot fingerprint；读取时严格校验 exact fields/version、类型、UTF-8、大小、record 生命周期与 fingerprint。caller/tool/arguments/generation/bundle 完全相同时复用原 action；参数或版本变化在单事务中删除旧 action 并生成不同 nonce，容量检查允许原子 slot replacement 但拒绝 namespace 超限。

原子与失败边界：create/consume/cancel/clear 使用同一 hash slot 上的 WATCH/MULTI；consume/cancel 在读取 action 前检查分布式 caller 失败预算，并按 Bot/adapter/user/group 隔离窗口。nonce 格式、TTL、caller、generation、arguments hash/JSON 或 record 完整性任一失败均拒绝；畸形、过期、generation 漂移及参数篡改 action 在事务内一次性移除。成功 consume 在任何外部副作用前删除 action，多个并发确认至多一个返回。只有明确 `WatchError` 可做有界重试；其他 Redis 异常和 EXEC 已提交但响应丢失均返回不含 endpoint/credential、无 exception cause 的 unavailable error，绝不返回 action，也不自动 fallback 到 Memory。failure key/window/index 与 clear 均有界，clear 只删除本 namespace。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 F-12 定向各 `50 passed`；Python 3.10 额外固定 Redis 5.2.0 / FakeRedis 2.31.0，其他版本使用 Redis 6.4.0 / FakeRedis 2.37.1。与 PendingAction/LLM Tools、Redis Client、Database Engine/Migrations/Schema、Repository、Agent、Graph、Scheduler、Conflict 联合 `582 passed`；四版本严格串行普通全量各 `1096 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2 全量、新文件 format、diff check，以及 Pyright 1.1.407 在 Redis 5.2 / 6.4 两套环境的目标/测试文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `48bdf9419f7edebea4489b71d963364d7ed89fff0e618344e9197c99dd0e1af5`、sdist SHA256 `7581d94865be2fa6f58c1a191bb38b11495f1af574b13398d37c5ed0780c93ec`；两种制品各 70 个文件，均包含 Redis PendingAction module、精确 Redis runtime dependency 与七个 revision，不包含 fakeredis runtime dependency、`uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-smoke")`、显式 manager→store 构造、模块无全局 Redis client/store，真实 Redis connect 计数始终为 0。

远端证据：F-12 最终本地证据 HEAD `23e548e76aa742686668c62405f053c363372e93` 对应 push run `32587036476` 与 PR run `32587039022`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-13 依赖已解除，F-14 继续锁定。本阶段不接 runtime/config/startup/shutdown，不实现 Cooldown/Admission Redis，不接 legacy sidecar、Repository 或生产配置，不读取生产 DSN/Redis URL，不运行 migration，不连接真实 Redis/PostgreSQL；D-09 保持锁定。未合并、未发布、未部署。

---

## F-13 Cooldown Redis

实现落点：实现提交 `04cf4e3a4d6cecacafc4609ec7bda54443cb0b9c` 新增 backend-neutral `CooldownStoreProtocol / CooldownClaim / CooldownLease`、默认 `MemoryCooldownStore` 与独立 `redis_cooldowns.py`。现有 `cd[user_id]` 的单一 user_id 作用域保持不变；claim 仍在 admission queue 前完成，AdmissionRejected、总预算超时、取消及 LLM falsey/string 结果释放本次 lease。默认内存 mapping 继续工作，只有 `handle_llm(..., cooldown_store=...)` 显式注入时才使用其他 backend，显式 falsey store 也不会回退默认 Memory。

原子与故障边界：Memory store 用异步锁原子 claim，并以 128-bit token + claim 时间做 owner-bound release，旧请求不会重置新 claim。Redis store 只接受显式 redis-py asyncio client，以 `<prefix>:cd:{<sha256(user_id)>}` 安全 key 执行 `SET NX PX` 并依靠 TTL 自动回收；重复 claim 用有 TTL 且不超过硬上限的 `PTTL` 返回向上取整等待时间。release 在 WATCH/MULTI 中比较 token 后删除，过期或替代 claim 不受旧 lease 影响。key prefix、最大 cooldown 与操作重试数均有界；只有 key 过期竞态与明确 `WatchError` 可重试。SET/EXEC 已提交但响应丢失、Redis 不可用、损坏 token、缺失/超限 TTL 与异常响应均 fail closed，错误只公开异常类型、不含 endpoint/credential 且无 exception cause；`CancelledError` 原样传播。没有 Redis→Memory 自动 fallback。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 F-13 定向各 `49 passed`；Python 3.10 固定 Redis 5.2.0 / FakeRedis 2.31.0，其他版本使用 Redis 6.4.0 / FakeRedis 2.37.1。四版本严格串行普通全量各 `1142 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0。Ruff 0.16.2 全量、新文件 format、diff check，以及 Pyright 1.1.407 在 Redis 5.2 / 6.4 两套依赖环境的目标/测试文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `f76e14e296309723a9bf2a9524361f52f259f4ddd784d2c7d8101894f72677ec`、sdist SHA256 `d476570b9f58a1a19cfc451fb99112e0b4ab7141cb58dde2aac1e9fa223e65c9`；两种制品各 72 个文件，均包含 Memory/Redis Cooldown module、精确 Redis runtime dependency 与七个 revision，不包含 fakeredis runtime dependency、`uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-smoke")`、显式 manager→PendingAction/Cooldown store 构造、模块无全局 Redis client/store，真实 Redis connect 计数始终为 0。

远端证据：F-13 最终本地证据 HEAD `12f0006784d654037cfeaca36356be481d9ec8a1` 对应 push run `32588890993` 与 PR run `32588892906`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-14 依赖已解除。本阶段不读取插件 Redis 配置、环境 Redis URL、生产 DSN 或 secret file，不注册 startup/shutdown，不接 Admission Redis、legacy sidecar、Repository 或生产 runtime，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

---

## F-14 Admission Redis

实现落点：实现提交 `9b095cceca5fee997d6884677579446127104499` 新增 backend-neutral `AdmissionGateProtocol / AdmissionStoreProtocol` 与 frozen lease/reservation/activation/renewal/release/snapshot value objects，并新增独立 `redis_admission.py`。默认单进程 `AdmissionController`、`get_llm_controller()` 配置解析和现有调用行为保持不变；`handle_llm(..., admission_controller=...)` 只有在调用方显式传入时才使用其他 gate，显式 falsey controller 也不会回退内存。Redis store/controller 只接受显式 redis-py asyncio client，不读取插件配置、环境 Redis URL 或 secret file，不创建全局 client/store/controller，也不注册 startup/shutdown。

原子、公平与恢复边界：每个 namespace 使用单个带 Cluster hash tag、总字节/记录数有界且自动 TTL 的 JSON state key；`int | str | None` key identity 经类型区分的 SHA-256 fingerprint 保存，不暴露原始用户标识。reserve/activate/renew/release/snapshot 均以 Redis server time 在 WATCH/MULTI 中原子执行，严格维持全局 active/pending、per-key active+pending 总量和同 key 至多一个 active。激活选择最早 eligible pending，避免同用户的等待项阻塞其他用户；pending 轮询续租、active heartbeat 续租，取消、失联、进程退出及未知结果遗留依靠 record/key TTL 回收。旧 lease、foreign namespace lease 与已过期 owner 不能释放当前记录。

故障边界：只有明确 `WatchError` 有界重试；Redis TIME/PTTL/SET/DELETE/EXEC 异常响应、严格 schema/TTL/容量损坏、重试耗尽、lease 丢失，以及 EXEC 已提交但响应丢失均 fail closed，不返回未确认 lease/成功状态且不自动 fallback 到 Memory。错误仅公开异常类型、不含 endpoint/credential 且无 exception cause；`CancelledError` 原样传播。Python 3.10 heartbeat 显式兼容 `asyncio.TimeoutError`。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 F-14 定向各 `66 passed`；Python 3.10 固定 Redis 5.2.0 / FakeRedis 2.31.0，其他版本使用 Redis 6.4.0 / FakeRedis 2.37.1。四版本 admission/chat/cooldown/tool-authoring/event-simulator 联合回归各 `125 passed`，严格串行普通全量各 `1208 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0。Ruff 0.16.2 全量、新文件 format、diff check，以及 Pyright 1.1.407 在 Redis 5.2 / 6.4 两套依赖环境的目标/测试文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `3ca59cca2f54320466184dd162ce57ded1b2c4721ef1fe2d8d99da9f13add2e4`、sdist SHA256 `1a5698dd2ed01795c824c946f394050c274fa7a0646bc15a4ee36436f9b9c640`；两种制品各 74 个文件，均包含 Admission Protocol/Redis module、精确 Redis runtime dependency 与七个 revision，不包含 fakeredis runtime dependency、`uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-smoke")`、显式 manager→PendingAction/Cooldown/Admission store 与 controller 构造、模块无全局 Redis client/store/controller，真实 Redis command/connect 计数始终为 0。

远端证据：F-14 最终本地证据 HEAD `7f0e2988db896feaf4ae8dd279b02152b8ff3a2f` 对应 push run `32591089687` 与 PR run `32591092104`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-01 依赖已解除。本阶段不读取插件 Redis 配置、环境 Redis URL、生产 DSN 或 secret file，不注册 startup/shutdown，不接 legacy sidecar、Repository 或生产 runtime，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

---

# Milestone G：0.29 Performance

---

## G-01 Chat History Repository

实现落点：实现提交 `b3566d6513f142d86de91898a6c6b8f14a4e131d` 新增深度不可变、UTC 规范化且与现有 Schema 上限一致的 `ConversationRecord / MessageRecord`，并新增显式 `AsyncSession` 注入的 `PostgresConversationRepository / PostgresMessageRepository`。structured content 在 I/O 前拒绝非有限浮点、NUL、循环及超限嵌套/节点，递归复制为只读值，绑定 JSONB 前再生成新鲜 mutable tree；draft message 使用 `message_id=None`，持久化 row 必须携带正 BIGINT identity。

查询与事务边界：recent history 只选择八个列，以 `conversation_id`、`id DESC` 和有限 `LIMIT+1` 查询；opaque cursor 绑定版本、会话 SHA-256 指纹与 `before_message_id`，下一页使用 `id < before_message_id`，拒绝跨会话、损坏、重复、乱序或错会话结果，并在应用层恢复旧到新顺序。create/replace/append 以 `RETURNING` 验证当前事务 statement 响应；Repository 不创建/提交/回滚/flush/close session，也不自动重试，最终 durable commit 归调用方。Integrity 与 replace 缺失映射 conflict，未知命令结果、损坏 row 和后端错误映射 unavailable；错误仅含安全操作名/异常类型且无 cause，取消原样传播。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `36 passed`，相关联合各 `173 passed`，严格串行普通全量各 `1244 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0。Python 3.10 最低 SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 联合 `167 passed`；Ruff 0.16.2、format/diff 与 Pyright 1.1.407 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `d300006def5f17f853430513d91c5b973d078aa043ad7617b32a4f85687b159b`、sdist SHA256 `d89af40a1268f341448a142f7489471dcfc9a84e6816846962af2c22c9810061`；两者各 76 个文件，包含 G-01 modules、精确数据库依赖与七个 revision，不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 表、7 revision、离线 DDL、reload、不可变 records 与显式 session→Repository 构造，数据库 execute/connect 始终为 0。

远端证据：G-01 最终本地证据 HEAD `d086e8ee87c5e25d8b692e8a7aadb239ef42464a` 对应 push run `32593099818` 与 PR run `32593102078`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-02 依赖已解除。本阶段不读 DSN/secret，不创建全局 engine/session，不接配置、startup/shutdown、legacy sidecar、现有内存聊天路径或生产 runtime，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

---

## G-02 History Hot Cache

实现落点：实现提交 `e865838` 新增 backend-neutral `HistoryWindow / HistoryCacheLoadToken / HistoryCacheLookup / HistoryHotCacheProtocol`、`MemoryHistoryHotCache` 与 `RedisHistoryHotCache`。window 只缓存同一会话、带正 BIGINT identity、严格递增的 frozen `MessageRecord`；最近后缀裁剪保持 `has_older` 真值，draft、跨会话、重复/乱序与超限载荷在 backend I/O 前拒绝。

一致性与故障边界：cache miss 先保留带 TTL 的 128-bit generation；publish 只有在会话指纹、generation、reservation TTL 和 loading state 全部匹配时才成功一次，invalidate 以新 generation 拒绝 durable commit 前启动的晚到 load。协议要求 publish 只接收已确认 committed source view，invalidate 只发生在 durable commit 成功后；cache 不写数据库、不提交事务，也不把 G-01 `RETURNING` 当成 commit 证明。Memory backend 为固定 TTL/LRU、有界会话/消息/载荷且绑定单 PID/loop。Redis backend 只接受显式 redis-py client，构造零命令；key 使用会话 SHA-256，canonical JSON value 重新经过 records 校验，TTL 与 WATCH/MULTI CAS 必须成立。损坏、缺 TTL、超限、异常响应或并发预算耗尽均不作为命中，错误脱敏且取消原样传播；runtime 的 cache-unavailable→PostgreSQL bypass 策略尚未接线。

本地门禁：Python 3.10.20、3.11.15、3.12.13 与 3.13.13 定向各 `84 passed`，与 Repository/Engine/Migration/Schema/G-01/Redis Stores/Context/Chat Runtime 联合各 `455 passed`，普通全量各 `1328 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped`。Python 3.10 最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 / fakeredis 2.31.0 联合门禁通过；Ruff 0.16.2、format 与 Pyright 1.1.407 为 0 错误。

制品门禁：实现提交 `e865838` 的 fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `afc4fdf0a95b476fba195adabac75e142ab323e4f2b20be4505e84a707163246`、sdist SHA256 `1fc9fec196ef41559c85264254fe94b55a1aa4bd04d77b5d192dd26f66488ba4`；两者各 78 个文件，包含两个 G-02 module、七个 revision，不含 `uv.lock`、cache/bytecode。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 表、7 revision、离线 DDL、reload、Memory cache roundtrip、显式 Redis cache 构造、无模块级 client，真实 Redis command 与数据库 connect/execute 均为 0。

远端证据：G-02 本地证据 HEAD `fca62e2a97fdb1b9fcccc5dd67dc604458d754c3` 对应 push run `32595899079` 与 PR run `32595902263`；两者均为目标 SHA、各 11 个 job 全绿、各恰好一个 `completed/success release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-03 依赖已解除。本阶段未读取连接配置或 secret，未创建全局 client/cache/engine/session，未接配置、startup/shutdown、legacy sidecar、G-01 Repository、现有内存历史或生产 runtime，未运行 migration，未连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未 promotion、未发布、未部署。

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

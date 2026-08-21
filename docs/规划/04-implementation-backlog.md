---
title: 04-implementation-backlog
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-21T05:25:58+00:00
---

# 04-implementation-backlog

# MoEllmChats 实施 Backlog 与 GitHub Milestone 建议

> 本文件可直接用于拆 GitHub Issue。

## 当前实施状态（2026-08-21）

- Milestone A、Milestone B 与 C-01～C-07 已在本地工作树完成实现，并按 A → B → C 顺序完成本轮定向复核：A 为 31 个非沙箱 node + 11 个真实 Sandbox case，B 为 24 个非沙箱 node + 12 个真实 Sandbox case，C-01～C-06 为 38 个定向 case。
- 修复后的最新本地总门禁已通过：Ruff 与 Actionlint 通过；真实非 root 隔离副本为 `335 passed, 13 skipped`；root 下 Python 3.10～3.13 普通全量各 `347 passed, 1 skipped`，其中 Python 3.12 固定 NoneBot 2.4.4 / OneBot 2.4.6；mandatory root Sandbox 为 `40 passed, 0 skipped`。
- fresh sdist/wheel、Twine、checksum 与 Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载和 `reload("package-smoke")` 全部通过；本地来源元数据明确标记为未提交工作树，不冒充可发布提交。
- CI 已在本地定义一次构建、四组 package smoke、零 skip Sandbox 与 fail-closed 聚合 `release-gate`；手动 promotion 先验证 job 列表完整且恰好一个精确命名的 `release-gate` 已 `completed/success`，再下载原 run artifact，不构建也不发布 PyPI。
- Plan 1 修复后精确 HEAD `f6c7628025cb5d34519499d86b979de448406d5b` 的 push run `32396257506` 与 PR run `32396261932` 各 11 个 job 全绿、各只有一个成功 `release-gate`；PR 基分支 `feat/llm-runtime-backpressure` 已要求 `strict=true` 的 `release-gate`。
- 每项状态分别标明本地实现、远端门禁与部署边界；远端 green 不代表 Qiqi 运行实例已经更新。
- Plan 1 发布门禁已关闭且未部署。Plan 2 的 D-01a～D-08f 已完成各自精确 HEAD 双 run gate；D-08f 最终闭环 HEAD `ea022bd31020880c72a66802aa3f036389d0169d` 对应 push run `32443308534` / PR run `32443313095`，两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / CLEAN`。D-09 因尚无至少一个发布周期的 parity 观察且禁止生产操作而保持锁定，legacy sidecar 继续保留。
- Milestone E 已在不依赖 D-09 清理、也不接数据库的增量边界内启动。E-01～E-04 已闭环；E-05 最终 HEAD `3ac210b28ecda3019b822bf961196f63cfd795ce` 的 push run `32449378433` / PR run `32449381716` 均为 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / CLEAN`。E-06 实现提交 `8af287e1a99e4f837dcbfb9a35071b17d5097c96` 定义不可变 Tool Graph；Tool Graph 定向 `42 passed`，四版本串行普通全量各 `781 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，Ruff、diff check、Pyright 目标文件、fresh build/Twine/checksum 与四组包外 ToolGraph smoke 均通过。E-06 当前仅本地门禁完成，精确 HEAD 远端双 run gate 待完成；E-07～E-08 尚未开始。逐项源码与测试映射见 [Plan 1 完成审计](./05-plan1-completion-audit.md)。

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

**状态：🟢 E-01～E-05 精确 HEAD 双 run 远端 gate green；🟡 E-06 本地门禁完成、远端 gate 待完成；E-07～E-08 未开始；D-09 继续锁定；未部署**

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

本地门禁：Tool Graph 定向 `42 passed`，与 Agent Runtime 联合定向 `227 passed`；Python 3.10.20、3.11.15、3.12.13 与 3.13.13 严格串行普通全量各 `781 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Ruff 0.16.2、diff check 与 Pyright 1.1.407 目标文件 `0 errors, 0 warnings` 均通过。fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `73bd52305aaf277481fe2c13ceafff0294744259f314d922e4ce289db3b9da3d`、sdist SHA256 `d8983ae6cbe30cc8d3f47516d6ea4968b627f9096ac1df6825ea99d7aff73351`；Python 3.10/3.12 × wheel/sdist 四组仓库外加载、generation 1、完整六 Provider registration、packaged ToolGraph 深冻结/确定性依赖/环路拒绝均通过。当前本地门禁完成，包含文档的精确 HEAD push/PR 双 run gate 待完成；未合并、未发布、未部署。

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

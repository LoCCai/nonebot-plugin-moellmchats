---
title: 01-plan-security-refactor
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-20T00:00:00+00:00
---

# 01-plan-security-refactor

# Plan 1：安全修复 + 核心架构重构

> 实施注记（2026-08-20）：`0.25.0-rc1`、`0.25.0-rc2` 与 stable 的 C-01～C-07 已完成实现、本地总门禁和远端发布门禁。精确 HEAD `f6c7628025cb5d34519499d86b979de448406d5b` 的 push/PR run `32396257506` / `32396261932` 各 11 个 job 全绿、各只有一个成功 `release-gate`；PR 基分支 `feat/llm-runtime-backpressure` 已要求 `strict=true` 的 `release-gate`。本轮未合并、未 promotion、未部署；Plan 2 只在上述门禁关闭后开始。逐项证据见 [Plan 1 完成审计](./05-plan1-completion-audit.md)。

> 推荐目标版本：`0.25.0-rc1 → 0.25.x stable`

---

# 1. 计划目标

当前 0.25 已经具备较完整的 Generated Tool 和 Runtime Snapshot 能力，但其安全模型仍属于：

> “受限制的 Python 子进程”

而不是：

> “真正可以承载模型生成代码的强隔离运行时”。

本计划目标是：

1. 修复现阶段 P0 / P1 问题；
2. 重新明确信任边界；
3. 固化 Tool 生命周期；
4. 固化 Source 与 Generation；
5. 提升 Runner 的协议和隔离能力；
6. 建立真实有效的 Sandbox CI。

---

# 2. P0：Generated Tool 默认禁止联网

当前 Generated Tool 在运行期可以使用 Python 网络库访问公网。

虽然：

- 环境变量被清理；
- UID 已降权；
- URL 参数有公共地址检测；

但这些不是完整安全边界。

Generated code 仍然可以：

```python
import socket
import urllib.request
```

自行创建请求。

---

## 2.1 第一阶段策略

Generated Tool 默认能力：

```yaml
capabilities:
  workspace: true
  network: false
  process: false
  host_filesystem: false
  secrets: false
```

Custom File Tool 可以配置更宽松策略，但必须显式声明。

---

## 2.2 Manifest 扩展

建议：

```json
{
  "bundle_id": "weather_tools",
  "capabilities": {
    "network": false,
    "process": false,
    "workspace": true,
    "host_filesystem": false,
    "secrets": false
  }
}
```

运行时实际权限：

```text
effective_capability = manifest_request AND admin_policy
```

五个 capability 字段都必须是布尔值；生成模型只能申请权限，不能决定最终权限。AST Policy 是独立的 fail-closed 预检，会拒绝或提升 effect，不作为第三份 capability 值参与交集。

---

# 3. P0：mutating 工具二阶段确认

当前不能继续使用：

```text
用户消息包含“确认执行”
+
confirm=true
```

作为危险操作授权。

---

## 3.1 引入 PendingAction

```python
@dataclass
class PendingAction:
    action_id: str
    user_id: str
    group_id: str | None

    tool_name: str
    arguments_hash: str
    generation: int

    created_at: float
    expires_at: float

    nonce: str
```

---

## 3.2 执行流程

```text
LLM 调用 mutating tool
         ↓
验证参数
         ↓
生成 PendingAction
         ↓
不执行
         ↓
回复：
此操作将修改外部状态。
确认请发送：
确认执行 A7F42C
         ↓
用户发送独立确认
         ↓
验证：
user
group
nonce
tool
args hash
generation
TTL
         ↓
执行
```

---

## 3.3 Redis 接入后的形式

后续计划三可以直接实现：

```text
moellm:pending_action:{nonce}
TTL = 120s
```

在计划三上线前，可以先使用 Memory Store。

---

# 4. P0：Generated Tool 权限保守化

AI 不能完全控制：

```text
permission
effect
capability
```

推荐安全合并规则。

---

## 4.1 Effect 推导

例如检测：

- 写文件
- 删除文件
- 修改数据库
- 网络 POST/PUT/PATCH/DELETE
- 系统命令

则：

```text
system_effect = mutating
```

最终：

```text
effective_effect =
strictest(manifest_effect, system_effect)
```

---

## 4.2 Permission 推导

第一阶段建议：

```text
Generated Tool 默认：
permission=superuser
```

如果管理员明确审批：

```text
allow_user_execution=true
```

才降到：

```text
user
```

---

# 5. Source Snapshot 重构

当前文件型工具存在 TOCTOU：

```text
Reload 读取源码
     ↓
生成 Schema
     ↓
用户修改原文件
     ↓
请求执行
     ↓
Runner 再次读取文件
```

Schema 与 Source 可能不一致。

---

## 5.1 引入 ToolArtifact

```python
@dataclass(frozen=True)
class ToolArtifact:
    tool_name: str
    source: bytes
    source_hash: str

    schema: Mapping
    spec: ToolSpec

    source_type: str
    generation: int
```

---

## 5.2 执行方式

从：

```python
runner.execute(path, handler, args)
```

改为：

```python
runner.execute(
    artifact=artifact,
    handler=handler,
    args=args,
)
```

Runner 只执行 Snapshot 里的固定源码。

---

# 6. Generated Tool 生命周期正式化

**状态：✅ 发布门禁完成（未部署）**

`generated_tools/lifecycle_state.json` schema v3 是唯一 canonical 状态；兼容读取 schema v2，并在内存中转换为 v3，下一次 canonical 写入会持久化 v3。legacy `active.json`、`permission_policy.json` 和草稿 metadata 状态只作单向兼容/审计投影。

---

## 6.1 生命周期

草稿记录使用独立、严格的状态机：

```text
Draft
  ↓
StaticValidated
  ↓
SandboxTested
  ↓
ModelReviewed
  ↓
AwaitingApproval
  ↓
Approved
```

失败路径：

```text
Rejected
ReviewFailed
ValidationFailed
TestFailed
```

`ExecutionBlocked` 是一次执行的运行期结果，不是持久生命周期状态。版本记录另用：

```text
Approved → Activated → Deprecated → Archived
```

`create_draft()` 只创建 `Draft`，不能再借 metadata 状态进行复合跃迁。ToolAuthoring 通过 `mark_static_validated`、`mark_sandbox_tested`、`mark_model_reviewed`、`mark_awaiting_approval` 以及对应失败入口逐步推进。批准命令仍允许在单个 revision 中把 `AwaitingApproval` 草稿推进为 `Approved`，同时创建并激活对应版本；任一 `bundle_id` 最多只有一个 `Activated` 版本，并与 `active` 映射保持不变量。

每个需要证据的草稿转移写入 canonical `DraftEvidence`，绑定当前草稿 digest，并严格校验 producer、outcome、summary、risks、时间范围和顺序。schema v2 迁移记录会明确使用 `schema-v2-migration` producer，并把未经新证据链验证的 review 标为 `legacy_unverified`，不会伪装成新流程已验证。

---

## 6.2 Tool Bundle 数据结构

```python
ToolBundle:
    bundle_id
    description

ToolBundleVersion:
    bundle_id
    digest
    manifest
    source
    tests

    state
    risks
    capabilities

    created_at
    approved_at
    activated_at
```

实现中这些记录由不可变 `DraftRecord` / `VersionRecord` 与 `LifecycleState` 表达。每次 plan 都绑定 expected revision、before digest 和 after digest，再通过 CAS 提交。

---

# 7. 完整 Review

**状态：✅ 发布门禁完成（未部署）**

管理员审批时必须能够看到：

```text
manifest.json
tool.py
tests.py
risks
capabilities
hash
diff
```

不能只展示截断后的 `tool.py`。

推荐 QQ 命令：

```text
查看LLM功能草稿 <id>
查看LLM功能草稿 <id> manifest
查看LLM功能草稿 <id> source 1
查看LLM功能草稿 <id> tests 1
```

也可以后续通过 Runtime API 提供网页查看。

当前 QQ 审阅按 `summary`、`manifest`、`source`、`tests`、`risks`、`capabilities`、`diff` 七个区段无损分页；每页含页头不超过 1800 字，并携完整草稿哈希、lifecycle revision/state digest、同 bundle 当前 active digest、页码及区段 SHA-256。Full Review 还生成完整 64 位 review stamp，绑定草稿 ID/digest、revision/state digest 与 active digest，并在页头给出 `批准LLM功能 <草稿ID> <至少8位哈希> <64位review stamp>`。`prepare_approval()` 在同一 canonical snapshot 重算 stamp；审阅后任一 lifecycle 变化都要求重新查看。

canonical `DraftEvidence` 本身绑定草稿 digest 并纳入 lifecycle state digest；metadata 中的 `lifecycle_evidence` 只是它的可变兼容投影。原始 `metadata.review` 仍是 best-effort 摘要，不能替代 canonical evidence。

---

# 8. Bundle Digest 二次验证

批准时计算 hash 不够。

每次执行前：

```text
Artifact Digest
      ↓
与 Snapshot / Active Version 对比
      ↓
一致
      ↓
允许执行
```

Generated immutable directory 的 chmod 只能算辅助措施。

---

# 9. Runner IPC 重构

当前：

```text
stdin  = request JSON
stdout = protocol response
```

存在 stdout 污染问题。

---

## 9.1 推荐 FD 模型

```text
FD 0 = request
FD 1 = tool logs
FD 2 = tool stderr
FD 3 = protocol
```

Worker 最终结果写入：

```text
FD 3
```

---

## 9.2 Protocol

```json
{
  "protocol_version": 1,
  "ok": true,
  "result": {
    "text": "..."
  },
  "metrics": {
    "elapsed_ms": 30
  }
}
```

---

# 10. Runner Workspace 改造

当前定时递归扫描目录：

```text
rglob
stat
```

可能被大量小文件拖慢。

---

## 10.1 增加限制

至少限制：

```text
max_workspace_bytes
max_workspace_files
max_directory_depth
max_single_file_size
```

---

## 10.2 避免阻塞 Event Loop

文件统计必须：

```python
await asyncio.to_thread(...)
```

而不是在 Event Loop 中直接遍历整个工作目录。

---

# 11. AST 静态策略重构

AST 扫描不能只是：

```text
发现 import os
→ risks += ...
```

应区分：

```text
Allowed
Risk
Denied
CapabilityRequired
```

---

## 11.1 Generated Tool 建议第一阶段禁用

- `ClassDef`
- decorator
- 非字面量默认参数
- `eval`
- `exec`
- `compile`
- `__import__`
- `ctypes`
- `pickle`
- `multiprocessing`
- `subprocess`
- 动态 import

---

## 11.2 风险扫描的真正定位

```text
AST
↓
Capability 推导
↓
Policy Engine
↓
Allow / Reject
```

不是：

```text
AST
↓
提示管理员有风险
↓
照样运行
```

---

# 12. Watcher 稳定性

**状态：✅ 发布门禁完成（未部署）**

Runtime Watcher 最外层不会因为：

```text
lifecycle_state.json 损坏
文件瞬时写入
临时 SyntaxError
目录暂不可访问
```

而永久退出。它在 0.5 秒到 30 秒范围内指数退避，成功后重置；`CancelledError` 单独传播，意外任务结束会写入可见日志。文件指纹及其 `glob`/`stat` I/O 已移到工作线程，坏资源继续沿用旧 RuntimeSnapshot。

实现形态：

```python
while True:
    try:
        ...
    except CancelledError:
        raise
    except Exception:
        logger.exception(...)
        await asyncio.sleep(backoff)
```

---

# 13. 多进程安全

**状态：✅ canonical 多进程并发管理与发布门禁完成（未部署）**

schema v3（兼容读取 v2）使用固定、受所有权和 no-follow 检查保护的：

```text
.lifecycle.lock
```

在 POSIX `flock` 的 shared/exclusive lock 下提供有界等待，并以 revision/state digest CAS 防止 lost update。durable 写入采用同目录临时文件、file fsync、atomic replace、directory fsync 和回读；directory fsync 最多重试 3 次。若目录 durability barrier 重试耗尽，即使 after-state 当前可见也保持 uncertain，不能推断 durable success；只有 directory fsync 已确认而后续回读不确定时，才允许按完整 revision/state digest 精确区分 before/after。

受保护的权威范围包括：

```text
draft transition
approve / activate
permission grant
deactivate / rollback
active version invariant
```

`active.json`、`permission_policy.json` 和 metadata status 不在决策路径，只是从最新 canonical snapshot 生成的 legacy 投影。投影失败标记 stale，不阻断 canonical commit 或当前进程运行时发布。

---

## 13.1 三阶段一致性边界

一次 Generated Tool 管理操作依次执行：

```text
after_state 候选预构建
        ↓
canonical durable CAS
        ↓
当前进程 RuntimeSnapshot 发布
```

批准、拒绝、权限、停用和回滚的生产调用都只能通过 `RuntimeReloader.apply_generated_change()` 进入这三个阶段；`GeneratedToolStore._commit_prepared_internal`、`LifecycleStore._commit_plan_internal`、`_compare_and_swap_internal`、`_publish_immutable_version_internal` 是内部实现，不是生产 API。候选构建或 CAS 失败时不会发布。durable commit 成功而当前进程发布失败时保留新 canonical revision，由 watcher 重试收敛；禁止把 before-image 盲写回去覆盖可能更晚的 revision。进入 durable finalization 后即使调用方反复取消，也会 shield 并等待线程收尾，不遗留后台写任务。

rollback 在同一 canonical snapshot 中先要求版本前缀唯一且记录未 Archived，再验证 versions root、bundle 目录和版本目录的 owner/no-follow；版本目录必须精确为 `0500`，内容必须且只能是 `manifest.json`、`tool.py`、`tests.py` 三个 `0400` 普通文件，校验期间 inode 不得替换，完整内容 digest 必须与目标版本一致。

---

## 13.2 明确不提供的保证

filesystem commit、当前进程内存指针和其他 worker 的内存状态不是跨进程 ACID。`converged=true` 只表示当次观察到的 canonical desired revision/digest 与当前进程 applied stamp 一致；其他新版 worker 通过 watcher 最终收敛。

共享 LocalStore 首次切换 schema v3 时，所有仍使用 legacy 或 schema v2 的旧插件进程必须先退出，再统一启动新版。旧进程无法读取 v3，且可能保留旧 RuntimeSnapshot 或与兼容投影竞争，导致 split-brain。

---

# 14. CI 重构

**状态：✅ 本地与远端发布门禁完成；精确 HEAD 双 run green，required `release-gate` 已配置；未部署**

普通 Python 3.10～3.13 job 排除 root-only Sandbox 文件；独立 `sandbox` job 在 Linux/root/namespace/UID drop/libseccomp 任一前提缺失时 fail closed，并从 JUnit 强制执行数大于 0、`skipped=0`。`build` 只构建一次 sdist/wheel 并生成 checksum/来源元数据；`package` 用同一 artifact 完成 Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载与 reload smoke；`release-gate` fail closed 聚合 test/sandbox/build/package。手动 promotion 必须先确认 job 列表完整，且名称精确为 `release-gate` 的 job 恰好一个、状态为 `completed`、结论为 `success`，随后下载该 CI run 的原 artifact；不重构也不发布 PyPI。

应拆分：

```text
lint
unit
integration
sandbox
package
```

---

## 14.1 Sandbox Integration

真实测试：

- UID drop
- GID drop
- no_new_privs
- RLIMIT CPU
- RLIMIT AS
- RLIMIT NPROC
- RLIMIT FSIZE
- RLIMIT NOFILE
- timeout kill
- process group cleanup
- orphan cleanup
- network namespace
- PID / mount / IPC / UTS namespace 与固定 hostname
- socket / AF_VSOCK / socketpair 分层 seccomp
- keyring syscall 永久拒绝与 hostfs=false xattr 拒绝
- workspace overflow
- output flood
- protocol fd isolation

当前不引入 cgroup，也不声称完整 syscall allowlist；`stat`、`lstat`、`readlink` 等宿主路径元数据残余可见是已知边界。

---

# 15. 配置文件权限

敏感配置：

```text
providers.toml
models.json
model_config.json
```

建议：

```text
config dir = 0700
secret-bearing files = 0600
```

Draft：

```text
metadata.json = 0600
```

不要统一 chmod `0644`。

---

# 16. 计划一验收标准

- [x] Generated Tool 默认不能联网
- [x] Generated Tool 默认不能 subprocess
- [x] mutating 使用二阶段确认
- [x] PendingAction 有 TTL
- [x] Source 固化进入 generation
- [x] Bundle 执行前 digest 校验
- [x] Tool Artifact 不重新读取活动源文件
- [x] Runner protocol 与 stdout 分离
- [x] Workspace 有 bytes/files/depth/single-file 限制
- [x] Watcher 异常不会退出，指纹 I/O 不阻塞 event loop
- [x] Config secret 文件 0600
- [x] Draft metadata 0600
- [x] 生命周期状态明确，canonical schema v3 为唯一权威并兼容读取 v2
- [x] Approve / permission / rollback / deactivate 使用 durable CAS、当前进程发布与 watcher 最终收敛；不夸大为跨进程 ACID
- [x] 最新 OS 隔离增量的 Sandbox Integration 与完整矩阵重新执行（mandatory root：`40 passed, 0 skipped`；普通矩阵四版本各 `347 passed, 1 skipped`）
- [x] 非 root 环境 fail closed

---

# 17. 推荐版本拆分

## 0.25.0-rc1

- Generated Tool Runtime 禁网
- mutating nonce
- metadata 权限
- safer manifest policy

## 0.25.0-rc2

- ToolArtifact
- Source Snapshot
- Runner FD protocol
- Workspace 限制
- AST Policy
- Sandbox Integration CI 定义与本地 root 实测

## 0.25 stable

- lifecycle state machine（本地完成）
- multi-process file lock（本地完成）
- watcher hardening（本地完成）
- approve / deactivate / rollback 分阶段崩溃一致性（本地完成）
- 精确 HEAD 双 run 的聚合 `release-gate` 已 green，PR 基分支 required check 已配置；生产发布仍需另行授权

---
title: 03-plan-performance-database
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-22T16:00:49+00:00
---

# 03-plan-performance-database

# Plan 3：处理效率与数据库接入优化

> 推荐目标版本：`0.28 → 0.30`

> 实施门禁（2026-08-22）：Plan 1 远端发布门禁、Plan 2 的 D-01a～D-08f 与 Milestone E 的 E-01～E-08 已完成；D-09 在不操作生产的约束下继续锁定。F-01～F-10 已闭环，F-10 最终文档闭环 HEAD `55c55bb2d77ef6c0a33a74fb7b1e476326c6458a` 的 push run `32581245744` / PR run `32581247621` 均为 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-11 实现提交 `a98f9298e1bbf461498c46b689eadffcf606fcf1` 已加入显式、安全脱敏、惰性且有界的 redis-py asyncio client/pool；本地全门禁、fresh 制品及四组包外 Redis dependency/10 表/7 revision/DDL/downgrade/reload smoke 已通过且未触发 Redis connect，精确 HEAD 双 run gate 待完成，F-12～F-14 继续锁定。在线 migration 仍无条件拒绝；未读取生产 DSN/Redis URL，未创建全局 engine/session/Redis client，未接 Repository、runtime 或 lifecycle，未运行 migration，也未 checkout 或连接数据库/Redis。

---

# 1. 计划目标

随着：

- 用户增加
- 群数量增加
- 历史消息增加
- Tool Bundle 增加
- Agent Run 增加
- Token Usage 增加
- Runtime 长期运行

继续依赖：

```text
dict
deque
JSON
TOML
目录文件
```

会逐渐出现：

- 内存膨胀
- 数据无法查询
- 重启丢状态
- 多实例不一致
- 数据分析困难
- Audit 不完整

因此需要明确：

```text
Memory
Redis
PostgreSQL
```

三层状态模型。

---

# 2. 数据分层原则

## 2.1 Memory

负责单进程、超短生命周期：

```text
RuntimeSnapshot
当前 MoeLlm Object
当前 Tool Process
当前 request context
短生命周期本地 cache
```

Memory 应是：

```text
fast
ephemeral
replaceable
```

---

## 2.2 Redis

负责：

```text
快速
跨进程
有 TTL
高频读写
```

包括：

- cooldown
- admission
- rate limit
- pending action
- confirmation nonce
- distributed lock
- request cancellation
- member cache
- session hot cache
- queue metadata

---

## 2.3 PostgreSQL

负责长期真实状态：

- user
- conversation
- message
- agent_run
- agent_step
- tool_call
- bundle
- bundle_version
- audit
- token_usage
- model_request
- configuration history

---

# 3. 推荐数据库技术栈

推荐：

```text
PostgreSQL
SQLAlchemy 2.x Async
asyncpg
Alembic
```

Redis：

```text
redis-py asyncio
```

F-11 实现提交 `a98f9298e1bbf461498c46b689eadffcf606fcf1` 加入 `redis>=5.2,<7` 与独立的 `RedisClientSettings / RedisClientManager`。URL 只接受显式 `redis://` 或 `rediss://`、合法 host 和单一 0～65535 database path，禁止 query 覆盖显式安全参数与 fragment；原始 URL 只保存在私有 redacted wrapper 中，`repr()`、错误和 `safe_diagnostics()` 均不暴露 username、password 或 endpoint。`rediss` 固定要求证书校验与 hostname check。

连接池与生命周期边界：每个 manager 最多惰性创建一个 redis-py asyncio client/pool，pool 上限为 1～1000，socket connect/read timeout 与 health-check interval 均有界，固定 keepalive、UTF-8 strict、RESP2 与 bytes response。创建 client/pool 不执行 `PING`、DNS 或 socket connect；首次创建绑定 PID 与运行中的 event loop，跨进程/loop 复用、关闭期间访问与并发重复关闭均 fail closed。成功关闭后可重建；取消或关闭失败恢复为可重试状态，初始化/关闭错误只公开异常类型且不串联可能含 URL 的原异常。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `51 passed`，Redis 5.2.0 最低依赖兼容定向另为 `51 passed`；与 Database Engine/Migrations/Schema、Repository、Agent、Graph、Scheduler、Conflict 联合 `492 passed`。四版本严格串行普通全量最终各 `1046 passed, 1 skipped`；Python 3.12 首轮命中一个既有 watcher 3 秒时序 node 超时，该 node 随后连续 `5/5` 通过且完整 3.12 全量重跑通过，未修改无关 watcher。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2 全量、目标文件 format、diff check 与 Pyright 1.1.407 目标/测试文件 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `dfbf90d8c3b1fe52ea199fbc3e4e6e44e0b5ef9a90df6421dcda3c885045ca0e`、sdist SHA256 `4fc5552c91f6f79c3dc7cdd0853c5d20ae662992eee9e1ce135cab0bf82832ec`；两种制品各 69 个文件，均包含 Redis Client、精确 Redis 依赖与七个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组已验证仓库外依赖环境均强制重装精确制品，确认 Redis 6.4.0 依赖、TLS/pool 参数、10 张表、七段 graph、离线 DDL、定向 downgrade 与 `reload("package-smoke")`，Redis connect 计数始终为 0。

当前仅 F-11 本地门禁完成；F-12 必须等待 F-11 最终 HEAD 的 push/PR 双 `release-gate` 成功。本阶段不创建全局 manager/client，不读取插件配置、环境 Redis URL 或 secret file，不注册 startup/shutdown，不实现 PendingAction/Cooldown/Admission Redis，不接 Repository 或 runtime，也不连接 Redis/PostgreSQL；未合并、未发布、未部署。

F-02 实现提交 `cf7c236c3f78c0775ff513f291ef4a55a877e54d` 已加入 `sqlalchemy>=2,<3` 与 `asyncpg>=0.30,<1`，但只提供显式构造的 `DatabaseEngineSettings / DatabaseEngineManager`。URL 必须使用 `postgresql+asyncpg`；原始 DSN 不持久保留，日志/诊断/错误不渲染凭据。连接池 size 1～100、overflow 0～100、总量不超过 150，pool/connect/statement/recycle timeout 均有上限；固定启用 pre-ping、LIFO、参数隐藏及 asyncpg 客户端/服务端 timeout。

Manager 惰性创建且每实例至多持有一个 `AsyncEngine`，创建只建立 SQLAlchemy pool 对象，不 checkout 网络连接；创建时绑定 PID/event-loop，跨边界复用与 dispose 竞态 fail closed，释放失败保持可重试。当前不创建全局实例、不接配置或生命周期、不创建 session，不执行 SQL。F-02 定向 `50 passed`，联合 Repository/Agent/Graph/Scheduler/Conflict `391 passed`；四版本最终普通全量各 `945 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，Ruff/format/diff/Pyright 与 fresh 制品门禁均通过。wheel/sdist SHA256 为 `c5c59aa4c556a4f16bb98a27c979ee174b2d1e46c5695f5ce596a7c617416c8c` / `c932056e00dbada7d55ab07f196996edc6daf3d6b6a5f3a31da6a09e79e4732f`；四组包外 smoke 均为 `checkedout=0`。最终文档闭环 HEAD `f8292f94c2dbeab80949436b495ee997382b5cac` 对应 push run `32458307603` / PR run `32458311280`；两者均 11/11 green、各恰好一个成功 `release-gate`。F-03 依赖已解除；未合并、未发布、未部署。

F-03 实现提交 `f9598561247e40a5ce8327a0ccd8d9f21f3fe04e` 加入 `alembic>=1.13,<2`，并定义共享但仍为空的 `database_metadata`、可随 wheel/sdist 分发的 env/template/versions 布局以及显式离线 API。graph 必须保持单 base/head 且拒绝 merge、branch label、`depends_on` 和不安全 revision；Config 不读取 ini、DSN、环境变量、插件配置或 secret file。空 graph 在 renderer 与 env 双重短路，在线路径始终 fail closed。F-03 不创建 revision、表、engine/session 或 Repository 实现；F-04 才开始首个业务 Schema 与 revision。

F-03 最终文档闭环 HEAD `a4eb771678587e6bfd32f793c8a6f7eda88f29ab` 对应 push run `32461256977` / PR run `32461262286`；两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / CLEAN`。F-04 依赖已解除；未合并、未发布、未部署。

F-04 实现提交 `21810cf836d89d07c268076d6e3d96b34cdfd04b` 将 `users / conversations / messages` 定义到共享 metadata，并新增首个线性 revision `0001_users_conversations`。Schema 与 revision 仍只支持离线 PostgreSQL DDL 渲染；本阶段不创建 engine/session，不提供 Repository 实现或 runtime 接线，不读取 DSN，也不连接数据库。

---

# 4. Repository Layer

核心代码不要出现到处直接 SQL。

建议：

```python
ConversationRepository
MessageRepository
AgentRunRepository
ToolRepository
UsageRepository
AuditRepository
```

Runtime 只面向接口。

F-01 已在实现提交 `71c9b4ceafe6bc5e4a0d16349d28bb7375f8dbbb` 固化这一边界：除上述六类接口外，补充 `AgentStepRepository / ToolCallRepository / RepositoryTransaction`，并以有界 opaque cursor 统一列表分页。AgentRun replace 使用 state + generation 双重 CAS，ToolCall replace 使用 status CAS；冲突和后端不可用是不同错误类别，避免实现层把未知写入结果误报为可安全重试。

该提交只有 Protocol、不可变分页值对象和契约测试，没有 engine、session、ORM model、SQL、DSN 或 I/O。Repository + Agent Runtime 定向 `216 passed`，联合 Graph/Scheduler/Conflict 为 `341 passed`；四个 Python 版本普通全量各 `895 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`，Ruff/format/diff/Pyright、fresh build/Twine/checksum 与 Python 3.10/3.12 × wheel/sdist 四组包外 Repository smoke 均通过。wheel SHA256 为 `373dcba1bcc9c782d933400dad2ac1215c3c754da455f02b17aae19211a76889`，sdist SHA256 为 `61c773e8780aade1766ac257f8d05f015851495ff307865ef386492ae4d8d9ff`。最终文档闭环 HEAD `678adb423e87fef8a851a8a792ae9c39a268dc15` 对应 push run `32455829891` / PR run `32455828489`；两者均 11/11 green、各恰好一个成功 `release-gate`。F-02 依赖已解除；未合并、未发布、未部署。

---

# 5. 基础 Schema

---

## 5.1 users

```text
id
platform
platform_user_id

display_name
created_at
updated_at
```

索引：

```text
(platform, platform_user_id) UNIQUE
```

---

## 5.2 conversations

```text
id
type
platform

group_id
user_id

created_at
updated_at
last_message_at
```

---

## 5.3 messages

```text
id
conversation_id

platform_message_id
role
sender_id

content
structured_content

created_at
```

索引：

```text
(conversation_id, id DESC)
created_at
```

F-04 已按以上边界实现 `database_schema.py` 与 `0001_users_conversations`。`users.id / conversations.id` 为最长 128 字符的应用生成 ID；用户平台身份使用 `(platform, platform_user_id)` 唯一约束。群聊范围用 `(platform, type, group_id)` partial unique index，私聊范围用 `(platform, type, user_id)` partial unique index。`messages.id` 使用 PostgreSQL `BIGINT GENERATED BY DEFAULT AS IDENTITY`，`structured_content` 使用 JSONB；平台消息 ID 在会话内 partial unique。全部时间字段使用带时区类型，外键删除策略为 `RESTRICT`，消息 payload 与各外部 ID 有非空约束。

metadata/revision parity 测试会从 revision 操作重建独立 metadata，并比较全部列、类型、nullability、server default、Identity、PK/FK/unique/check/index 及 PostgreSQL 条件。F-04 四版本定向各 `32 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `423 passed`；四版本普通全量最终各 `977 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`。fresh wheel/sdist SHA256 为 `7c22c093605dd4213e8c5bb8751fa81f26925cc45e56d391366016762d679739` / `b5b52c0125fea1a9f565e531241fb19fd20bfb2a89e00d325a28a47d9b88c262`，Twine、制品内容检查与 Python 3.10/3.12 × wheel/sdist 四组包外 Schema/graph/DDL smoke 均通过。最终文档闭环 HEAD `9a343cfcc71a2824257afd9f7537edf4ab8af4f2` 对应 push run `32463913845` / PR run `32463917189`；两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / CLEAN`。F-05 依赖已解除；未运行 migration，未连接或修改数据库。

---

# 6. Agent Runtime Schema

## 6.1 agent_runs

```text
id
request_id

user_id
group_id
conversation_id

generation
model

status

started_at
finished_at

input_tokens
output_tokens
cost

error_type
error_message
```

F-05 实现提交 `c177fc51e73b3961617cc2b09082ceeb0e436897` 将以上字段固化为 `agent_runs`，并以不可变 `0002_agent_runtime` 追加到 `0001_users_conversations`。`id` 为应用生成的有界标识；`request_id` 使用正数 BIGINT 但不设唯一约束，因为当前进程内计数在重启后可能复用。`user_id / conversation_id` 为非空 `RESTRICT` 外键，`group_id / model / token / cost / error` 按生命周期允许 NULL；cost 为 `NUMERIC(24, 12)`，token/cost/generation 均拒绝负数。

`status` 值域精确绑定当前 `AgentRunState`；五种终态必须带 `finished_at`，其他状态必须保持 `finished_at IS NULL`，并统一约束结束时间不得早于开始时间。会话时间线索引为 `(conversation_id, started_at DESC, id DESC)`，另有用户时间线与 `(status, started_at)` 恢复索引；`generation + status` 为后续条件更新保留，但 F-05 不实现 Repository 或运行时接线。四版本定向各 `35 passed`，联合回归 `426 passed`，四版本全量各 `980 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；fresh wheel/sdist SHA256 为 `1d47c5d1eee9686c8e642fb8d52bfb50e01894a2a71c7ba560ac16df5d8c8f8b` / `41a8d65dd33c8343bcb64e91444e220099bcdf1a66fe210d6974f4f3cb5de2f1`，四组包外 smoke 均通过。最终文档闭环 HEAD `d23e156e4df44442bc9b7382fef5e53c88433148` 对应 push run `32465645519` / PR run `32465649984`；两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / CLEAN`。F-06 依赖已解除；未运行 migration，未连接或修改数据库。

---

## 6.2 agent_steps

```text
id
run_id

step_index
step_type

model
tool_name

status

started_at
finished_at

duration_ms

input_preview
output_preview
error
```

索引：

```text
(run_id, step_index)
```

F-06 实现提交 `ea405674e38082a5089304789a1628024da7d2ec` 将以上字段固化为 `agent_steps`，并以不可变 `0003_agent_steps` 追加到 `0002_agent_runtime`。`id` 为应用生成的有界标识，`run_id` 使用 `RESTRICT` 外键指向 `agent_runs.id`；`(run_id, step_index)` 唯一约束既拒绝同一 run 的重复序号，也提供稳定步骤顺序。step type/status 值域精确绑定现有 `AgentStepType / AgentStepStatus`；MODEL/TOOL 类型必须分别携带 model/tool identity。

pending、running 与五种终态的 `started_at / finished_at / duration_ms` 组合由数据库约束精确执行，时间不得倒序且 duration 不得为负。input/output/error 只允许最长 6000 字符的非空预览，不持久化完整领域 JSON；非终态不得携带 output，completed 不得携带 error。四版本定向各 `38 passed`，联合回归 `429 passed`，四版本全量各 `983 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；fresh wheel/sdist SHA256 为 `edcaac6c69f337d70b078dc0679b360db6bcc9d5228c39606380fbb0d2afeb80` / `b6dffce54dfed796625707e932881d996a52f6759e63e62752fd04903d1e5a26`，四组包外 AgentStep Schema/graph/DDL smoke 均通过。最终文档闭环 HEAD `4e5cd600b1efa430bb785bdc5cb7f6a49988be9a` 对应 push run `32467140779` / PR run `32467144569`；两者均 11/11 green、各恰好一个成功 `release-gate`，远端分支与 PR head 一致，PR #2 为 `OPEN / CLEAN`。F-07 依赖已解除；未运行 migration，未连接或修改数据库。

---

## 6.3 tool_calls

```text
id
run_id
step_id

tool_name
tool_source
bundle_id
bundle_digest

arguments_json
result_preview

confirmed
confirmation_id

status
duration_ms

created_at
finished_at
```

F-07 实现提交 `83a571fbc79e13ce68f237ba7ed9c653607fbb66` 将以上字段固化为 `tool_calls`，并以不可变 `0004_tool_calls` 追加到 `0003_agent_steps`。`ToolCallStatus / ToolSource` 值域与领域枚举精确同步；arguments 使用非空 JSONB object，result 只保存最长 6000 字符预览。Generated 来源必须同时绑定合法 bundle ID 与 64 位小写 digest，其他来源必须保持两者为空；等待确认和 confirmed 记录必须绑定 confirmation ID，且同一 confirmation 只能对应一条调用。

`run_id` 直接以 `RESTRICT` 指向 `agent_runs`；新 revision 另为 `agent_steps(run_id, id)` 追加支持约束，并用复合 `RESTRICT` 外键保证 call 的 run/step 不会跨 run 错配。五种终态必须携带 `finished_at / duration_ms`，非终态必须保持两者为空且不得携带 result，completed 必须携带 result preview；时间不得倒序、duration 不得为负。run 稳定时间线、step 时间线与状态恢复索引均已声明。四版本定向各 `41 passed`，联合回归 `432 passed`，四版本全量各 `986 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；fresh wheel/sdist SHA256 为 `2ce8bf699fe7919cfca345d90833be99e2453e5025adf9513a9d230eb96f4b4f` / `54a6a8f44d3da165c7fe34704d60d4765164877a4c7b6fbc48c605957f819424`，四组包外 ToolCall Schema/graph/DDL smoke 均通过。最终 HEAD `dcff410498a862bed302687e1383cab0f554da6c` 的 push run `32469057942` / PR run `32469061094` 均为 11/11 green、各恰好一个成功 `release-gate`，F-08 依赖已解除；未运行 migration，未连接或修改数据库。

---

# 7. Tool Bundle Schema

## 7.1 tool_bundles

```text
id
bundle_id
description

created_at
updated_at

active_version_id
```

---

## 7.2 tool_bundle_versions

```text
id
bundle_id

digest

manifest_json
source
tests_source

state
risks_json
capabilities_json

created_at
approved_at
activated_at
deprecated_at
archived_at
```

F-08 实现提交 `7afa3c81a6604a09533b0b1b487d3c484f9f1909` 将以上模型固化为 `tool_bundles / tool_bundle_versions`，并以不可变 `0005_tool_bundle_metadata` 追加到 `0004_tool_calls`。`bundle_id` 使用与现有 Generated Tool 相同的安全标识规则；版本 digest 为 64 位小写 SHA-256，`(bundle_id, digest)` 唯一。manifest、risks、capabilities 使用有界 JSONB，源码与测试源码各限制为 64 KiB；manifest 中的 `bundle_id` 必须与版本列一致。

版本 `state` 精确绑定 `VersionState` 的 `approved / activated / deprecated / archived`，各状态的 `activated_at / deprecated_at / archived_at` 组合和全时间顺序由数据库约束执行。bundle 的 active pointer 使用同 bundle 复合 `RESTRICT` 外键，防止跨 bundle 错挂；partial unique index 保证每个 bundle 至多一个 activated 版本。版本时间线 `(bundle_id, created_at DESC, id DESC)`、状态恢复与 bundle 更新时间线均有确定性索引。

packaged graph 现在为五段单 base/head 线，唯一 head 为 `0005_tool_bundle_metadata`；离线 `0005:0004` downgrade 先移除循环 active FK，再按 `tool_bundle_versions → tool_bundles` 逆依赖顺序删除，不触碰 `tool_calls` 及前六张表。四版本定向各 `44 passed`，联合回归 `435 passed`，四版本全量各 `989 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；fresh wheel/sdist SHA256 为 `441964bdd651746d1a61eadea63ea389ea40e42fd0bfe3599d1921ecc93230cf` / `6968f782e685c7fdfc55b5fa2d3c456d0a86f8dbb677c262ca9bd5639d60ee92`，四组包外 smoke 均通过。最终 HEAD `6064c5beb387d06c796439255e3159310ecb70b6` 的 push/PR 双 run gate 已关闭，F-09 依赖已解除；未运行 migration，未连接或修改数据库。

注意：

即使代码主体继续存 filesystem，也建议数据库保存：

```text
digest
metadata
state
audit
```

Source 是否直接存 DB 可以后续决定。

---

# 8. Audit Schema

```text
audit_events

id
event_type

actor_user_id
actor_type

target_type
target_id

run_id
tool_call_id

metadata_json

created_at
```

F-09 实现提交 `6fe1a4cf57cfec7c7d21342a32b19632a7c7de12` 将以上模型固化为 `audit_events`，并以不可变 `0006_audit_events` 追加到 `0005_tool_bundle_metadata`。事件 ID 使用 PostgreSQL `BIGINT IDENTITY`；event/actor/target 类型使用有界 canonical token，target identity 必填，actor user、run 与 tool call identity 可选。`metadata_json` 必须是最多 64 KiB 的 JSONB object；本阶段只定义存储边界，不把现有日志、调用参数、结果或 trust decision 自动写入表。

引用与查询边界：可选 actor user 与 run 分别以 `RESTRICT` 指向 `users / agent_runs`。tool call audit 必须同时携带 run；新 revision 为 `tool_calls(run_id, id)` 追加支持约束，并以 `(run_id, tool_call_id)` 复合 `RESTRICT` 外键拒绝跨 run 错挂。run、tool call、actor、target 与 event type 五类索引均以 `created_at DESC, id DESC` 提供稳定游标；多态 target 不伪造无法由数据库统一验证的跨表外键。

packaged graph 现在为六段单 base/head 线，唯一 head 为 `0006_audit_events`。离线 `0006:0005` downgrade 先删除 `audit_events`，再删除 F-09 新增的 tool call 复合支持约束，不触碰 bundle/version、`tool_calls` 或前六张表；metadata/revision parity 覆盖精确 PostgreSQL 类型、Identity、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `47 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `438 passed`；四版本严格串行普通全量各 `992 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2、目标文件 format、diff check、216 个 PostgreSQL 命名项上限检查（最长 52）与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `72e04d75283bc7624b15608b3948922ed30d4d5775f2c5298c943ce1b7e2266e`、sdist SHA256 `70fa3d5fd779c2f83f9f31ab5b5705711cc99255da07acf6a5e131936874a5a2`；两种制品各 67 个文件，均包含六个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10 × wheel/sdist 使用 fresh venv 完整安装，Python 3.12 × wheel/sdist 在已验证的仓库外依赖环境中强制重装上述精确制品；四组均确认 9 张表、六段 graph、JSONB/复合 FK/unique/check DDL、定向 downgrade 与 `reload("package-smoke")`。精确 HEAD 的 clean CI package jobs 仍是下一道远端门禁。

远端证据：F-09 最终文档闭环精确 HEAD `be2b3ab14fb7b9ce0d712fc52a2fa96830364993` 对应 push run `32580016797` 与 PR run `32580019661`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-10 依赖已解除。本阶段不实现 Repository、session 或 runtime 写入，不读取 DSN、不运行 migration、不连接 PostgreSQL/Redis；未合并、未发布、未部署。

---

# 9. Token Usage

```text
model_usage

id
run_id

provider
model

input_tokens
output_tokens
reasoning_tokens
cached_tokens

cost

created_at
```

后续可统计：

```text
日
周
月
用户
群
模型
Provider
```

F-10 实现提交 `f96b1ffadf43283c77365246b1b065379c013c2e` 将以上模型固化为 `model_usage`，并以不可变 `0007_model_usage` 追加到 `0006_audit_events`。记录 ID 使用 PostgreSQL `BIGINT IDENTITY`，每条 usage 以非空 `RESTRICT` 外键绑定 `agent_runs`；provider/model 为有界非空原始标识，input/output/reasoning/cached token 均为显式非负 BIGINT。cost 使用可空 `NUMERIC(24, 12)`，保留“供应商/计价规则尚不能确定成本”与真实零成本的区别。

数据与查询边界：不强行假定不同供应商的 reasoning/cache 一定包含在 output/input 中，只执行各计数非负与 provider/model 非空不变量。run 稳定时间线、provider+model 聚合时间线与全局时间线均使用 `created_at DESC, id DESC`；按用户/群统计可经已索引的 run 关联完成，不在 usage 表重复用户身份。本阶段不改现有内存 `token_usage_history`，不接 `llm_api`、UsageRepository 或批量写入。

packaged graph 现在为七段单 base/head 线，唯一 head 为 `0007_model_usage`。离线 `0007:0006` downgrade 只删除 `model_usage`，不触碰 `audit_events`、`agent_runs` 或其他既有表；metadata/revision parity 覆盖精确 PostgreSQL 类型、Identity、全部约束和索引。在线 migration 仍在 engine 创建前无条件拒绝。

本地门禁：Python 3.10.20（Alembic 1.13.0）、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `50 passed`；与 Engine/Repository/Agent/Graph/Scheduler/Conflict 联合 `441 passed`；四版本严格串行普通全量各 `995 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0；Ruff 0.16.2、目标文件 format、diff check、233 个 PostgreSQL 命名项上限检查（最长 52）与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `b6e1dc17c58b7bca86ea00b84d30f693887d267e25023995d202a3ee32d8df57`、sdist SHA256 `9460a1f640ad2129ec79725637a758eda95161c30d96ab8388f8a38b896f1fec`；两种制品各 68 个文件，均包含七个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组已验证仓库外依赖环境均强制重装上述精确制品，并确认 10 张表、七段 graph、BIGINT/Numeric/FK/check/index DDL、定向 downgrade 与 `reload("package-smoke")`。精确 HEAD 的 clean CI package jobs 仍是下一道远端门禁。

远端证据：F-10 最终文档闭环精确 HEAD `a55510697e05b4f0c17d20d36dd91643e8776890` 对应 push run `32580881668` 与 PR run `32580884647`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-11 依赖已解除。本阶段不实现 Repository、session 或 runtime 写入，不读取 DSN、不运行 migration、不连接 PostgreSQL/Redis；未合并、未发布、未部署。

---

# 10. PendingAction + Redis

Plan 1 的二阶段确认在 Redis 中非常适合。

```text
moellm:pending-action:{nonce}
```

Value：

```json
{
  "user_id": "...",
  "group_id": "...",
  "tool": "...",
  "args_hash": "...",
  "generation": 42
}
```

TTL：

```text
120 sec
```

---

# 11. Cooldown Redis 化

当前：

```python
cd[user_id]
```

后续：

```text
SET moellm:cd:{user_id} 1 NX EX 120
```

优点：

- 多实例共享
- 自动 TTL
- 重启不产生异常状态
- 无需清理 dict

---

# 12. Admission Redis 化

如果未来多个 Bot Worker：

```text
global active
global pending
per-user active
```

都不能只用 asyncio.Semaphore。

需要：

```text
Redis counters
Redis Lua
或 Redis semaphore
```

单实例阶段仍可保留本地实现。

---

# 13. 聊天上下文数据库化

当前上下文最终应：

```text
Postgres = Source of Truth
Memory = Hot Cache
```

---

## 13.1 获取历史

不要：

```sql
SELECT *
```

推荐：

```sql
SELECT ...
FROM messages
WHERE conversation_id = ?
ORDER BY id DESC
LIMIT 20;
```

应用层反向恢复。

---

# 14. 上下文分层

未来上下文不应只有：

```text
最近 N 条消息
```

而应：

```text
Recent Messages
+
Session Summary
+
Long-Term Memory
```

---

# 15. Session Summary

当对话超过阈值：

```text
消息 1~50
     ↓
summary model
     ↓
SessionSummary
```

新的上下文：

```text
summary
+
最近 10 条消息
```

明显降低 Token。

---

# 16. Long-Term Memory

长期记忆不要自动把所有历史塞入 prompt。

应：

```text
用户问题
   ↓
Memory Retrieval
   ↓
相关 memories
```

后续可使用：

```text
PostgreSQL + pgvector
```

但建议放到 0.29+。

---

# 17. Cache Strategy

---

## 17.1 Tool Catalog Cache

Key：

```text
generation
permission
```

例如：

```text
catalog:user:42
catalog:superuser:42
```

---

## 17.2 Tool Schema Cache

```text
schema:{generation}:{toolset_hash}
```

---

## 17.3 Model Capability Cache

Provider 模型列表：

```text
provider:{provider}:models
```

保留：

```text
TTL
last-known-good
```

---

## 17.4 Classification Cache

对于高度相似的标准请求，可考虑：

```text
normalized_prompt_hash
→ classification result
```

但：

- 用户上下文相关分类不应缓存；
- 涉及权限时必须带 user capability；
- 缓存时间不能过长。

---

# 18. Tool 并行化

并发执行条件：

```text
effect = read_only
dependencies = none
resource_conflict = false
```

---

## 18.1 Scheduler

```text
ToolCall A ─┐
ToolCall B ─┼─ asyncio.gather
ToolCall C ─┘
```

---

## 18.2 Mutating

必须串行：

```text
mutating
transactional
ordered
```

---

# 19. Runner Pool

Generated Tool：

```text
one-call-one-process
```

继续保持。

Trusted Tool 可以优化。

---

## 19.1 Trusted Worker Pool

```text
worker 1
worker 2
worker 3
worker 4
```

适用于：

```text
人工 custom tool
高频纯计算 tool
稳定依赖 tool
```

不适用于 Generated Tool。

---

# 20. Deadline 优化

整任务一个 deadline：

```text
Request Deadline
```

所有子任务拿：

```text
remaining()
```

避免 timeout 叠加。

---

# 21. Batch Database Write

Token Usage、普通 Metrics、非关键 Audit 可：

```text
async queue
   ↓
100 events
or
1 second
   ↓
COPY / Batch INSERT
```

---

## 21.1 必须即时写

安全相关：

```text
tool approval
mutating confirmation
mutating execution
bundle activation
bundle rollback
```

---

# 22. 数据库连接池

建议：

```text
pool_size
max_overflow
pool_recycle
pool_pre_ping
```

注意不要每次请求创建新 Engine。

---

# 23. N+1 Query 避免

AgentRun 页面如果读取：

```text
run
steps
tool_calls
usage
```

应批量：

```text
JOIN / selectinload
```

而不是每个 step 再一次 SQL。

---

# 24. 数据保留策略

建议：

```text
messages → 长期
agent_runs → 90~180 天
tool_calls → 180 天
metrics raw → 30 天
aggregate metrics → 长期
audit security → 长期
```

具体根据部署规模调整。

---

# 25. 数据清理任务

定时：

```text
prune expired sessions
prune old metrics
archive old runs
vacuum strategy
```

不要让数据库无限增长。

---

# 26. 数据库故障降级

数据库挂掉时：

```text
聊天
→ 可继续 Memory Mode

历史
→ 暂时不可读取完整历史

Usage
→ 写本地 spool

Audit
→ 本地可靠队列
```

---

## 26.1 Local Spool

```text
data/spool/audit/*.jsonl
data/spool/usage/*.jsonl
```

恢复后批量 flush。

---

# 27. Redis 故障降级

Redis 不可用：

```text
single instance
→ local fallback

multi instance
→ fail limited / degraded
```

涉及危险确认时：

```text
PendingAction store 不可用
→ mutating 工具直接拒绝
```

不能自动降级为“不需要确认”。

---

# 28. 数据迁移

建议 Alembic。

版本：

```text
0001_users_conversations
0002_agent_runtime
0003_agent_steps
0004_tool_calls
0005_tool_bundle_metadata
```

F-09 及后续任务继续按依赖追加 revision，不预占或复用已经门禁的编号。

F-03 已完成离线迁移地基；实现提交 `f9598561247e40a5ce8327a0ccd8d9f21f3fe04e` 当时保持空 graph，并由入口和 env 双重短路 Alembic 1.13 空图可能生成的误导性 `DROP TABLE alembic_version`。最终文档闭环 HEAD `a4eb771678587e6bfd32f793c8a6f7eda88f29ab` 对应 push run `32461256977` / PR run `32461262286`；两者均 11/11 green、各恰好一个成功 `release-gate`，F-04 依赖已解除。

F-04 实现提交 `21810cf836d89d07c268076d6e3d96b34cdfd04b` 将 graph 推进为 `revisions=1 / bases=1 / heads=1`，三者唯一标识均为 `0001_users_conversations`。离线 upgrade 只生成 version table、`users / conversations / messages` 及其约束/索引，明确不含 `DROP`；离线 downgrade 按 `messages → conversations → users` 逆依赖顺序删除。最终文档闭环 HEAD `9a343cfcc71a2824257afd9f7537edf4ab8af4f2` 的 push run `32463913845` / PR run `32463917189` 均为 11/11 green、各恰好一个成功 `release-gate`，F-05 依赖已解除。

F-05 实现提交 `c177fc51e73b3961617cc2b09082ceeb0e436897` 以不可变 `0002_agent_runtime` 将 graph 推进为 `revisions=2 / bases=1 / heads=1`，唯一 head 为该 revision。本阶段只创建 `agent_runs`，F-06/F-07 必须通过后续 revision 扩展，不能回改已经门禁的 `0002`。离线 `0002:0001` downgrade 只删除 `agent_runs`，不触碰 F-04 三张表；在线 migration 继续在 engine 创建前 fail closed。最终文档闭环 HEAD `d23e156e4df44442bc9b7382fef5e53c88433148` 的 push run `32465645519` / PR run `32465649984` 均为 11/11 green、各恰好一个成功 `release-gate`，F-06 依赖已解除。

F-06 实现提交 `ea405674e38082a5089304789a1628024da7d2ec` 以不可变 `0003_agent_steps` 将 graph 推进为 `revisions=3 / bases=1 / heads=1`，唯一 head 为该 revision。本阶段只创建 `agent_steps`，F-07 必须追加新 revision，不能回改已经门禁的 `0001`～`0003`。离线 `0003:0002` downgrade 只删除 `agent_steps`，不触碰前四张表；metadata/revision parity 覆盖三段 graph 的全部列、约束与索引，在线 migration 继续在 engine 创建前 fail closed。最终文档闭环 HEAD `4e5cd600b1efa430bb785bdc5cb7f6a49988be9a` 的 push run `32467140779` / PR run `32467144569` 均为 11/11 green、各恰好一个成功 `release-gate`，F-07 依赖已解除。

F-07 实现提交 `83a571fbc79e13ce68f237ba7ed9c653607fbb66` 以不可变 `0004_tool_calls` 将 graph 推进为 `revisions=4 / bases=1 / heads=1`，唯一 head 为该 revision。新 revision 创建 `tool_calls`，并只为复合父键向 `agent_steps` 追加 `uq_agent_steps_run_id_id`，不回改 `0001`～`0003`。离线 `0004:0003` downgrade 先删除 `tool_calls` 再删除该支持约束，不触碰前五张表；metadata/revision parity 覆盖四段 graph 的全部列、约束与索引，在线 migration 继续在 engine 创建前 fail closed。最终文档闭环 HEAD `dcff410498a862bed302687e1383cab0f554da6c` 的 push run `32469057942` / PR run `32469061094` 均为 11/11 green、各恰好一个成功 `release-gate`，F-08 依赖已解除。

F-08 实现提交 `7afa3c81a6604a09533b0b1b487d3c484f9f1909` 以不可变 `0005_tool_bundle_metadata` 将 graph 推进为 `revisions=5 / bases=1 / heads=1`，唯一 head 为该 revision。新 revision 创建 `tool_bundles / tool_bundle_versions`，并在两表均存在后追加同 bundle active pointer 外键，不回改 `0001`～`0004`；F-09 必须继续追加新 revision。离线 `0005:0004` downgrade 只删除这两张新表与其循环支持外键，不触碰 `tool_calls` 及前六张表；metadata/revision parity 覆盖五段 graph 的全部列、约束与索引，在线 migration 继续在 engine 创建前 fail closed。当前仅 F-08 本地门禁完成，精确 HEAD 双 run gate 待完成；未读取 DSN，未运行 migration，未连接或修改任何数据库。

---

# 29. 数据库配置

建议配置：

```json
{
  "database_enabled": true,
  "database_url": "...",

  "redis_enabled": true,
  "redis_url": "...",

  "db_pool_size": 10,
  "db_max_overflow": 20
}
```

Secret 不建议长期放普通 JSON。

更推荐：

```text
environment
secret file
external secret manager
```

---

# 30. 性能指标

上线 DB 后至少监控：

```text
db_query_duration
db_pool_active
db_pool_wait

redis_latency

context_load_duration
context_message_count

summary_duration
summary_token_saved

tool_parallelism
runner_start_duration
```

---

# 31. Plan 3 验收标准

- [ ] Repository Layer
- [ ] PostgreSQL 基础 Schema
- [ ] Alembic Migration
- [ ] Redis Client
- [ ] cooldown Redis
- [ ] PendingAction Redis
- [ ] AgentRun 持久化
- [ ] AgentStep 持久化
- [ ] ToolCall 持久化
- [ ] Token Usage 持久化
- [ ] Chat History 持久化
- [ ] History Hot Cache
- [ ] Session Summary
- [ ] Batch Insert
- [ ] DB Failure Spool
- [ ] Redis Failure Policy
- [ ] Tool Catalog Cache
- [ ] read_only tool parallelism
- [ ] database metrics

---

# 32. 推荐版本拆分

## 0.28

- PostgreSQL
- Redis
- Repository
- AgentRun persistence
- ToolCall audit

## 0.29

- Chat History
- Summary
- Cache
- Parallel Tools
- Usage Analytics

## 0.30

- Long-Term Memory
- pgvector（可选）
- Full Runtime Analytics

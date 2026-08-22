---
title: 03-plan-performance-database
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-22T21:09:27+00:00
---

# 03-plan-performance-database

# Plan 3：处理效率与数据库接入优化

> 推荐目标版本：`0.28 → 0.30`

> 实施门禁（2026-08-22）：Plan 1、Plan 2 的 D-01a～D-08f、Milestone E、F-01～F-14 与 G-01 已完成远端门禁；D-09 在不操作生产的约束下继续锁定，G-02 依赖已解除。G-01 实现提交 `b3566d6513f142d86de91898a6c6b8f14a4e131d` 新增深度不可变 Conversation/Message records 与显式 `AsyncSession` 注入的 PostgreSQL Repository；最近历史以显式列、`conversation_id`、`id DESC`、`LIMIT+1` 查询，使用绑定会话指纹的 `before_message_id` keyset 游标并在应用层反转。Repository 不拥有 session 生命周期，不隐式 commit/rollback/retry；`RETURNING` 只确认当前事务 statement，最终 commit 仍由调用方负责。Integrity/缺失记录是冲突，后端异常、损坏结果与未知写入是 unavailable，错误脱敏且取消原样传播。四版本定向各 `36 passed`、相关联合各 `173 passed`、普通全量各 `1244 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`；Ruff/Pyright、最低数据库依赖、fresh 制品和四组包外 10 表/7 revision/DDL/reload/零数据库 execute/connect smoke 均通过。G-01 本地证据 HEAD `d086e8ee87c5e25d8b692e8a7aadb239ef42464a` 的 push run `32593099818` / PR run `32593102078` 均为 11/11 green、各恰好一个成功 `release-gate`；远端分支与 PR head 一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。未读取生产 DSN、未创建全局 engine/session、未接配置、startup/shutdown、legacy sidecar、现有内存聊天路径或生产 runtime，未运行 migration，也未 checkout 或连接真实数据库/Redis。

> G-02 本地门禁（2026-08-22）：G-01 闭环文档 HEAD `11531889583fd5d11cf0871f503c6ff037c38395` 的 push `32593312310` / PR `32593315775` 已各 11/11 green。实现提交 `e865838` 新增 committed `HistoryWindow`、`HistoryHotCacheProtocol`、Memory/Redis 两类 backend 和 128-bit reservation generation；Memory 固定 TTL、LRU、会话数/消息数/载荷上限并拒绝跨 PID/loop 复用，Redis 显式注入 client，以 SHA-256 会话 key、canonical JSON、TTL 与 WATCH/MULTI 做 CAS。缓存从不替代 PostgreSQL 真源，损坏/超限/缺 TTL/后端未知均不返回命中，晚到 load 在 durable commit 后 invalidation 发生时必须发布失败。四版本定向各 `84 passed`、联合各 `455 passed`、普通全量各 `1328 passed, 1 skipped`，Sandbox `40 passed, 0 skipped`；最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0、静态、fresh 制品及四组包外零 I/O smoke 均通过。G-02 精确 HEAD 双 run gate 待完成，G-03 锁定；未接配置、生命周期、Repository 编排、`MessagesHandler` 或生产。

> G-02 远端闭环（2026-08-22）：本地证据 HEAD `fca62e2a97fdb1b9fcccc5dd67dc604458d754c3` 对应 push run `32595899079` / PR run `32595902263`；两者各 11 个 job 全绿、各恰好一个 `completed/success release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-03 依赖已解除；未连接真实数据库/Redis，未合并、未发布、未部署。

> G-03 本地门禁（2026-08-22）：实现提交 `82ddd7ae89049fd173360ee7662e6d40387156c1` 新增 Session Summary 领域契约、显式 `AsyncSession` Repository 与线性 `0008_session_summaries`。摘要输入、源 digest、累计水位、前驱 identity、generation 和确定性 50/10 策略全部固化；超限时不截断消息、不推进未完整读取的水位。append 使用单条条件 INSERT CAS 且依赖唯一 generation/前驱、防跨会话消息复合外键；Repository 不 commit/rollback/retry。四版本定向各 `103 passed`、联合各 `551 passed`、普通全量各 `1381 passed, 1 skipped`，Sandbox `40 passed, 0 skipped`；最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 / FakeRedis 2.31.0、静态、fresh 制品与四组包外零 I/O smoke 均通过。制品 SHA256 为 wheel `db83341f418b0bcf8ae87e8aad5d3c29d1e32ff2bfc4babbc223238afcaca718`、sdist `963bc7f6513dfb3b701ba228ca6d743444bf48d50678a7193e399fe73f9ffecf`。精确 HEAD 双 run 待完成，G-04 锁定；未调用摘要模型、未接 runtime、未运行 migration、未连接真实数据库/Redis、未部署。

> G-03 远端闭环（2026-08-22）：本地证据 HEAD `3fb6792ec18566c571ab9e9628c0ea9ec1854a53` 对应 push run `32598610770` / PR run `32598613406`；两者各 11 个 job 全绿、各恰好一个 `completed/success release-gate`，远端分支与 PR head 精确一致，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-04 依赖已解除；未运行 migration，未连接真实数据库/Redis，未合并、未发布、未部署。

> G-04 本地门禁（2026-08-22）：实现提交 `aa6e7d34a8b1335c34540bb50fe93868d70bc9f1` 新增显式 `ToolCatalogRenderContext`、typed `ToolCatalogCacheKey`、digest-stamped frozen `ToolCatalogRecord`、`ToolCatalogCacheProtocol`、`resolve_tool_catalog()` 与 Memory LRU backend。key 除 generation/两级权限外还纳入 Provider cutover、Tools/Search 开关及规范化黑名单 digest，避免相同 generation 下的权限或策略串用；Memory backend 以条目/单值/总字节硬上限和 PID/event-loop ownership 约束复用。同 key 异值、错误 identity、超限、跨 owner 与不可信 backend 结果 fail closed，构建/parity 异常不会 publish。本地四版本定向各 `161 passed`、联合各 `306 passed`、普通全量各 `1433 passed, 1 skipped`，Sandbox `40 passed, 0 skipped`；最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 / FakeRedis 2.31.0、Ruff/Pyright、fresh 制品和四组包外 cache roundtrip/reload/零 I/O smoke 均通过。制品 SHA256 为 wheel `ab805c305183bddd1e49b3e417534ca09abc4d2f4970c9df3f40d477b61c06b0`、sdist `0348bc4627dfbb6a6d227842fcbda072724b9838cc67ef7d1607e87807d3bb37`。精确 HEAD 双 run 待完成，G-05 锁定；现有 Categorize 同步路径保持未接线，不创建全局 cache、不读取连接配置、不连接真实服务、不迁移、不部署。

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

远端证据：F-11 最终文档闭环精确 HEAD `13383aee25fe90e8ecd3542a3df9af748f2e11f0` 对应 push run `32583576588` 与 PR run `32583578903`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-12 依赖已解除。本阶段不创建全局 manager/client，不读取插件配置、环境 Redis URL 或 secret file，不注册 startup/shutdown，不实现 PendingAction/Cooldown/Admission Redis，不接 Repository 或 runtime，也不连接 Redis/PostgreSQL；未合并、未发布、未部署。

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
moellm:{pending-action}:action:{nonce}
```

Value：

```json
{
  "schema_version": 1,
  "action_id": "...",
  "bot_id": "...",
  "adapter_id": "...",
  "user_id": "...",
  "group_id": "...",
  "tool_name": "...",
  "arguments_json": "{...}",
  "arguments_hash": "...",
  "generation": "42",
  "bundle_digest": null,
  "created_at": 0.0,
  "expires_at": 120.0,
  "nonce": "ABC123",
  "caller_fingerprint": "...",
  "slot_fingerprint": "..."
}
```

TTL：

```text
120 sec
```

F-12 实现提交 `ca992e967af943b4d9f1067c26deef762aceee4a` 新增 backend-neutral `PendingActionStoreProtocol` 与独立 `RedisPendingActionSettings / RedisPendingActionStore`。Store 只接受调用方显式注入的 redis-py asyncio client，不读取插件配置、环境 Redis URL 或 secret file，也不创建全局 client/store；现有内存 `pending_action_store` 继续是默认，`execute_pending_action()` 仅在显式传入 store 时使用 Redis，且显式后端即使为 falsey 也不会回退内存。`fakeredis>=2.31,<3` 只属于测试依赖，不进入运行制品。

数据与 keyspace 边界：全部动态 key 使用同一 `{pending-action}` Redis Cluster hash tag；action record、caller/tool slot、action/slot expiry index、caller failure key 与 failure expiry index 均按安全前缀隔离。Settings 对 TTL、总容量、参数字节、失败窗口/次数/key 数与 WATCH 重试数设置硬上限。Record 使用精确 schema version，绑定 Bot、adapter、user、group、tool、canonical arguments hash、generation、bundle digest、创建/过期时间与 caller/slot fingerprint；严格解码类型、UTF-8、大小、record 生命周期和 fingerprint，畸形或参数篡改记录在事务内一次性移除并拒绝执行。

原子与故障边界：create 对相同 caller/tool/arguments/generation/bundle 复用 nonce，参数或版本变化原子替换旧 nonce，且永不把旧确认码复用于新参数；consume/cancel 在 WATCH/MULTI 内先检查 caller 失败预算，再执行 caller、generation、TTL 与 arguments 绑定校验，一次性删除发生在任何外部副作用之前。并发 consume 至多一个成功；只有明确 `WatchError` 使用有界重试，其他 Redis 错误及 EXEC 已提交但响应丢失一律返回脱敏的 unavailable error、不串联原异常且不返回 action。失败预算按 Bot/adapter/user/group 隔离，窗口与 key table 均由 Redis TTL/ZSET 有界维护；clear 只清理当前 namespace。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 F-12 定向各 `50 passed`，其中 Python 3.10 额外固定 Redis 5.2.0 / FakeRedis 2.31.0；与 PendingAction/LLM Tools、Redis Client、Database Engine/Migrations/Schema、Repository、Agent、Graph、Scheduler、Conflict 联合 `582 passed`。四版本严格串行普通全量各 `1096 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0。Ruff 0.16.2 全量、新文件 format、diff check，以及 Pyright 1.1.407 在 Redis 5.2 / 6.4 两套依赖环境的目标/测试文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `48bdf9419f7edebea4489b71d963364d7ed89fff0e618344e9197c99dd0e1af5`、sdist SHA256 `7581d94865be2fa6f58c1a191bb38b11495f1af574b13398d37c5ed0780c93ec`；两种制品各 70 个文件，均包含 Redis PendingAction module、精确 Redis runtime dependency 与七个 revision，且不包含 fakeredis runtime dependency、`uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-smoke")`、显式 manager→store 构造以及模块无全局 Redis client/store，真实 Redis connect 计数始终为 0。

远端证据：F-12 最终本地证据 HEAD `23e548e76aa742686668c62405f053c363372e93` 对应 push run `32587036476` 与 PR run `32587039022`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-13 依赖已解除，F-14 继续锁定。本阶段不接 runtime/config/startup/shutdown，不实现 Cooldown/Admission Redis，不接 legacy sidecar 或 Repository，不读取生产 DSN/Redis URL，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

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

F-13 实现提交 `04cf4e3a4d6cecacafc4609ec7bda54443cb0b9c` 新增 backend-neutral `CooldownStoreProtocol / CooldownClaim / CooldownLease`、默认 `MemoryCooldownStore` 与独立 `RedisCooldownSettings / RedisCooldownStore`。`handle_llm()` 只有在调用方显式传入 store 时才使用 Redis，显式 falsey backend 也不会回退内存；默认 `BoundedValueStore` 映射、user_id 单一作用域、admission 前 claim，以及 AdmissionRejected/timeout/cancel/falsey/string 结果释放语义均保留。未读取插件配置、环境 Redis URL 或 secret file，未创建全局 Redis client/store，也未注册 startup/shutdown。

原子与 keyspace 边界：内存路径用异步锁完成单用户原子 claim，并用 128-bit lease token 与原始 claim 时间绑定释放，避免过期请求清除新请求。Redis 路径使用 `<prefix>:cd:{<sha256(user_id)>}` 的固定长度安全 key，`SET NX PX` 原子占用并依赖 Redis TTL 自动回收；重复 claim 只依据有 TTL 且不超过配置硬上限的 `PTTL` 返回向上取整等待时间。释放在 WATCH/MULTI 中比较 128-bit token 后删除，同一用户并发 claim 只有一个成功，旧 lease、错误 token 或已过期 key 均不会删除替代 claim。key prefix、最大 cooldown 与重试次数均有硬上限，0 秒 cooldown 在不访问 Redis 的情况下直接放行。

故障边界：claim 的 SET 已提交但响应丢失时不返回 lease，release 的 EXEC 已提交但响应丢失时不声称已释放；两者均抛出不含 endpoint/credential 且无 exception cause 的 unavailable error。只有显式 key 过期竞态或 `WatchError` 做有界重试，缺失 TTL、超上限 TTL、损坏 token、异常响应和 Redis 不可用均 fail closed；`CancelledError` 原样传播。Redis 失败绝不自动降级到 Memory，admission 在 claim 成功前不会进入。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 F-13 定向各 `49 passed`；Python 3.10 固定 Redis 5.2.0 / FakeRedis 2.31.0，其他版本使用 Redis 6.4.0 / FakeRedis 2.37.1。四版本严格串行普通全量各 `1142 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0。Ruff 0.16.2 全量、新文件 format、diff check，以及 Pyright 1.1.407 在 Redis 5.2 / 6.4 两套依赖环境的目标/测试文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `f76e14e296309723a9bf2a9524361f52f259f4ddd784d2c7d8101894f72677ec`、sdist SHA256 `d476570b9f58a1a19cfc451fb99112e0b4ab7141cb58dde2aac1e9fa223e65c9`；两种制品各 72 个文件，均包含 Memory/Redis Cooldown module、精确 Redis runtime dependency 与七个 revision，且不包含 fakeredis runtime dependency、`uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-smoke")`、显式 manager→PendingAction/Cooldown store 构造、模块无全局 Redis client/store，真实 Redis connect 计数始终为 0。

远端证据：F-13 最终本地证据 HEAD `12f0006784d654037cfeaca36356be481d9ec8a1` 对应 push run `32588890993` 与 PR run `32588892906`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。F-14 依赖已解除。本阶段不接配置、startup/shutdown、Admission Redis、legacy sidecar 或 Repository，不读取生产 DSN/Redis URL，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

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

F-14 实现提交 `9b095cceca5fee997d6884677579446127104499` 新增 backend-neutral `AdmissionGateProtocol / AdmissionStoreProtocol` 与强类型 reservation/activation/renewal/release/snapshot lease 契约，并新增独立 `RedisAdmissionSettings / RedisAdmissionStore / RedisAdmissionController`。现有 `AdmissionController`、`get_llm_controller()` 配置解析和默认单进程路径保持不变；只有调用方显式传入 `handle_llm(..., admission_controller=...)` 才使用其他 gate，显式 falsey controller 也不会回退默认内存。Redis store 只接受显式 redis-py asyncio client，不读取插件配置、环境 Redis URL 或 secret file，不创建全局 client/store/controller，也不注册 startup/shutdown。

原子与公平边界：每个安全 `<prefix>:<admission name>` namespace 只使用一个带 Cluster hash tag 的有界 JSON state key，key identity 仅接受 `int | str | None` 并以带类型的 SHA-256 fingerprint 保存，原始用户标识不进入 Redis key/state。reserve/try_activate/renew/release/snapshot 都在 Redis server time + WATCH/MULTI 中完成；全局 active/pending、per-key active+pending 总量和同 key 至多一个 active 由同一事务状态校验。激活选择最早的 eligible pending，因此已 active 用户的更早 pending 不会占住其他用户可用的全局 slot；pending 在安全阈值内由轮询续租，active 由独立 heartbeat 续租，取消、进程退出或失联后依靠 record/key TTL 自动回收。state schema、序号、lease identity、时间、TTL、容量、记录数和总字节均有硬上限；旧 lease、foreign namespace lease 或已过期 owner 不能释放当前记录。

故障边界：只有明确 `WatchError` 使用有界重试；Redis TIME/PTTL/SET/DELETE/EXEC 异常响应、状态损坏、重试耗尽、active/pending lease 丢失，以及 EXEC 已提交但响应丢失均 fail closed，不返回未确认 lease 或成功状态，也不自动 fallback 到 Memory。错误只公开安全操作名与异常类型，不串联可能含 endpoint/credential 的原异常；`CancelledError` 原样传播。Python 3.10 heartbeat 显式捕获 `asyncio.TimeoutError`，避免把正常续租计时误判为 backend failure。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 F-14 定向各 `66 passed`；Python 3.10 固定 Redis 5.2.0 / FakeRedis 2.31.0，其他版本使用 Redis 6.4.0 / FakeRedis 2.37.1。四版本 admission/chat/cooldown/tool-authoring/event-simulator 联合回归各 `125 passed`，严格串行普通全量各 `1208 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0。Ruff 0.16.2 全量、新文件 format、diff check，以及 Pyright 1.1.407 在 Redis 5.2 / 6.4 两套依赖环境的目标/测试文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `3ca59cca2f54320466184dd162ce57ded1b2c4721ef1fe2d8d99da9f13add2e4`、sdist SHA256 `1a5698dd2ed01795c824c946f394050c274fa7a0646bc15a4ee36436f9b9c640`；两种制品各 74 个文件，均包含 Admission Protocol/Redis module、精确 Redis runtime dependency 与七个 revision，且不包含 fakeredis runtime dependency、`uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-smoke")`、显式 manager→PendingAction/Cooldown/Admission store 与 controller 构造、模块无全局 Redis client/store/controller，真实 Redis command/connect 计数始终为 0。

远端证据：F-14 最终本地证据 HEAD `7f0e2988db896feaf4ae8dd279b02152b8ff3a2f` 对应 push run `32591089687` 与 PR run `32591092104`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-01 依赖已解除。本阶段不接配置、startup/shutdown、legacy sidecar、Repository 或生产 runtime，不读取生产 DSN/Redis URL，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

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

G-01 实现提交 `b3566d6513f142d86de91898a6c6b8f14a4e131d` 新增 `chat_history.py` 与 `postgres_history_repository.py`。`ConversationRecord / MessageRecord` 为 frozen、UTC 规范化的脱离态值对象；structured content 会校验有限浮点、NUL、循环、深度与节点上限，并递归复制为只读 mapping/tuple，数据库绑定前再生成新鲜 mutable JSON。标识、role、platform、scope、payload 与时间顺序均在 I/O 前按既有 PostgreSQL Schema 上限验证；`message_id=None` 只表示尚未 append 的草稿，持久化读取必须携带正 BIGINT identity。

查询与事务边界：`PostgresConversationRepository / PostgresMessageRepository` 只接受调用方显式提供的 `AsyncSession`，模块不创建 engine/session/factory 或全局实例。Conversation create/replace 与 Message append 使用 PostgreSQL `RETURNING` 验证本次 statement 响应；Repository 不 commit、rollback、flush、close 或自动重试，因此最终 durable commit、rollback 与未知 commit 结果仍由调用方处理。recent history 仅选择八个消息列，以 `conversation_id` 过滤、`id DESC` 排序并取 `limit + 1`；稳定 opaque cursor 包含版本、会话 ID 的 SHA-256 指纹和最旧可见 message ID，下一页使用 `id < before_message_id`，拒绝跨会话、非规范、超长、乱序、重复或错会话结果，最后在应用层恢复由旧到新的顺序。

故障边界：写入 IntegrityError 与 replace 缺失记录明确归类为 conflict，不会被误报为可安全重试；数据库异常、命令结果未知、缺失/异常 `RETURNING`、损坏 row 或违反排序契约均归类为 unavailable。每次操作最多调用一次 `session.execute()`；错误只公开安全操作名和异常类型，不串联可能包含 endpoint、credential 或消息正文的原异常，`CancelledError` 原样传播。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 G-01 定向各 `36 passed`，与 Repository/Engine/Migration/Schema/Message Context/Context Budget/Chat Runtime 联合各 `173 passed`；四版本严格串行普通全量各 `1244 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failures/errors/skipped 均为 0。Python 3.10 最低 SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 数据库联合为 `167 passed`；Ruff 0.16.2 全量、目标 format、diff check 与 Pyright 1.1.407 目标文件均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `d300006def5f17f853430513d91c5b973d078aa043ad7617b32a4f85687b159b`、sdist SHA256 `d89af40a1268f341448a142f7489471dcfc9a84e6816846962af2c22c9810061`；两种制品各 76 个文件，均包含两个 G-01 module、精确 SQLAlchemy/asyncpg/Alembic runtime dependency 与七个 revision，且不包含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 10 张表、七段 graph、离线 DDL、`reload("package-g01-smoke")`、不可变 records 与显式 session→两类 Repository 构造，数据库 execute/connect 计数始终为 0。

远端证据：G-01 最终本地证据 HEAD `d086e8ee87c5e25d8b692e8a7aadb239ef42464a` 对应 push run `32593099818` 与 PR run `32593102078`；两者各 11 个 job 全绿、各恰好一个 `completed/success` 的 `release-gate`，远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`。G-02 依赖已解除。本阶段不读取生产 DSN 或 secret file，不创建全局 engine/session，不接配置、startup/shutdown、legacy sidecar、现有 `MessagesHandler`/内存历史或生产 runtime，不运行 migration，不连接真实 PostgreSQL/Redis；D-09 保持锁定。未合并、未发布、未部署。

## 13.2 History Hot Cache（G-02）

实现落点：实现提交 `e865838` 新增 `history_hot_cache.py` 与 `redis_history_hot_cache.py`。`HistoryWindow` 是脱离 backend 的 frozen hot window，只接受同一 `conversation_id`、携带正 PostgreSQL BIGINT identity 且按 identity 严格递增的 `MessageRecord`；空窗口不得伪造 `has_older`，裁剪只保留最新后缀并正确提升更早历史标记。`HistoryCacheLookup` 必须且只能返回命中窗口或短期 `HistoryCacheLoadToken`，token 的 repr 不暴露会话指纹/代际。

一致性与事务边界：miss 先用 128-bit 随机 generation 保留一个有界 loading state；publish 必须同时匹配会话 SHA-256 指纹、generation、未过期 reservation 与 loading 状态，成功一次后重复 publish 失败。durable source write commit 后的 invalidate 会原子替换 generation，因此 commit 前已启动、commit 后才返回的旧 PostgreSQL load 不得覆盖新状态。协议明确要求只有已确认 committed source view 才可 publish、只有 durable commit 成功后才可 invalidate；cache 不写 Repository、不创建 transaction，也不把 statement-level `RETURNING` 当成 durable proof。

backend 边界：`MemoryHistoryHotCache` 使用 monotonic 固定 TTL、LRU、最大会话/消息/载荷上限与单 PID/event-loop ownership；过期、淘汰或 clear 后的 token 不能复活状态。`RedisHistoryHotCache` 只接受调用方显式注入的 redis-py asyncio client，构造时不发命令；key 仅含可配置安全前缀和 conversation SHA-256，value 是版本化、canonical ASCII JSON，消息会在读取时重新经过 G-01 records 校验。loading/ready key 均必须携带有界 TTL；publish 通过 WATCH/MULTI CAS，损坏、非 canonical、超限、跨会话、乱序、缺 TTL、异常响应或 retry budget 耗尽均不作为命中或成功。异常只含安全操作名/类型，不泄漏 key 原文、消息或 endpoint，`CancelledError` 原样传播。缓存 unavailable 后是否旁路 PostgreSQL 属于未来 runtime 编排策略，本阶段不静默吞错。

本地门禁：Python 3.10.20、3.11.15、3.12.13 与 3.13.13 G-02 定向各 `84 passed`；与 Repository、Engine、Migration、Schema、G-01 PostgreSQL History、Redis Client/PendingAction/Cooldown/Admission、Message Context、Context Budget 与 Chat Runtime 联合各 `455 passed`。四版本严格串行普通全量各 `1328 passed, 1 skipped`；mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit tests=40、failure/error/skip 均为 0。Python 3.10 最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 / fakeredis 2.31.0 使用同一联合门禁；Ruff 0.16.2 全量、目标 format 与 Pyright 1.1.407 目标源码/测试均通过。

制品门禁：实现提交 `e865838` 的 fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `afc4fdf0a95b476fba195adabac75e142ab323e4f2b20be4505e84a707163246`、sdist SHA256 `1fc9fec196ef41559c85264254fe94b55a1aa4bd04d77b5d192dd26f66488ba4`；两者各 78 个文件，均包含两个 G-02 module、精确 Redis/数据库依赖与七个 revision，且不含 `uv.lock`、`__pycache__` 或 `.pyc`。Python 3.10/3.12 × wheel/sdist 四组 fresh 仓库外安装均确认 10 表、7 revision、离线 DDL、plugin reload、Memory hit/publish、显式 client→Redis cache 构造及模块无全局 Redis client；Redis command、数据库 connect/execute 计数始终为 0。制品目录 `/tmp/moellm-g02-dist.avoM6m`，smoke 根目录 `/tmp/moellm-g02-smoke.H6N3D5`。

远端证据：G-02 本地证据 HEAD `fca62e2a97fdb1b9fcccc5dd67dc604458d754c3` 对应 push run `32595899079` 与 PR run `32595902263`；两者均为目标 SHA、各 11 个 job 全绿、无非 success job，各恰好一个 `completed/success release-gate`。远端分支与 PR head 均精确指向该 SHA，PR #2 为 `OPEN / MERGEABLE / CLEAN`，G-03 依赖已解除。未读取 Redis URL、生产 DSN 或 secret，未创建全局 client/cache/engine/session，未接配置、startup/shutdown、legacy sidecar、现有内存聊天路径、PostgreSQL Repository 编排或生产 runtime；未运行 migration，未连接真实 PostgreSQL/Redis，未合并、未 promotion、未发布、未部署。

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

G-03 实现落点：实现提交 `82ddd7ae89049fd173360ee7662e6d40387156c1` 新增 `session_summary.py` 与 `postgres_session_summary_repository.py`。`SessionSummaryPolicy` 只接受同一会话、正 BIGINT identity、oldest-first 严格递增的 committed `MessageRecord`；默认候选窗口为 50 条，压缩最老 40 条并保留最近 10 条。若完整 canonical 输入超过 64,000 字符，则以确定性二分缩小本次完整源前缀并留下更多未覆盖消息；若连下一条完整消息都无法容纳，则抛出明确错误且水位不前移。

一致性与持久化边界：model input 以 canonical JSON 绑定会话 SHA-256、前一摘要 identity/content/digest、策略和完整源消息，`source_digest` 再绑定精确 UTF-8 bytes。`SessionSummaryRecord` 记录 generation、前驱、覆盖起止水位、累计/本次消息数、策略阈值、输入上限/实际字符数、provider/model 与摘要正文。append-only `0008_session_summaries` 为 `messages(conversation_id,id)` 增加复合引用键，并用复合 `RESTRICT` 外键拒绝跨会话前驱或水位；会话内 generation、水位和非空前驱均唯一。Repository 的 append 是单条 `INSERT ... SELECT ... WHERE` head CAS，stale/fork 映射 conflict；后端未知结果映射 unavailable 且不自动重放。Repository 只接受调用方显式 `AsyncSession`，不创建 engine/session，不 commit、rollback、flush、close 或调用模型。

本地门禁：Python 3.10.20、3.11.15、3.12.13（NoneBot 2.4.4 / OneBot 2.4.6）与 3.13.13 定向各 `103 passed`；与 G-01/G-02、Database/Redis Stores、Context/Chat Runtime 联合各 `551 passed`；严格串行普通全量各 `1381 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 均为 0；Python 3.10 最低依赖联合、Ruff 0.16.2、目标 format/diff 与 Pyright 1.1.407 `0 errors, 0 warnings` 均通过，PostgreSQL identifier 最长不超过 63。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `db83341f418b0bcf8ae87e8aad5d3c29d1e32ff2bfc4babbc223238afcaca718`、sdist SHA256 `963bc7f6513dfb3b701ba228ca6d743444bf48d50678a7193e399fe73f9ffecf`；两者各 81 个文件，包含 G-03 modules 与 `0008_session_summaries`，不含 `uv.lock`、cache/bytecode。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认 11 表、8 revision、离线 upgrade/定向 downgrade、plugin reload、50→40+10 compaction、显式 session→Repository 构造；engine create、SQL execute、asyncpg connect、Redis command/connect 始终为 0。制品目录 `/tmp/moellm-g03-dist.DT96Yd`，smoke 根目录 `/tmp/moellm-g03-smoke.CH0RMT`。精确 HEAD 双 run 远端 gate 仍是 G-04 前置条件；本阶段未读取 DSN/Redis URL/secret，未运行 migration，未连接服务，未调用摘要模型，未接配置、生命周期、G-01/G-02 编排、`MessagesHandler` 或生产 runtime，未合并、未发布、未部署。

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
catalog:user:42:{policy_digest}
catalog:superuser:42:{policy_digest}
```

G-04 实现落点：实现提交 `aa6e7d34a8b1335c34540bb50fe93868d70bc9f1` 新增 `tool_catalog_cache.py`，以 `ToolCatalogRenderContext` 一次性固定 generation、`user / superuser` 权限、Provider categorize cutover、Tools/Search 开关与规范化黑名单。黑名单只接受有界字符串 tuple，去空白、去重、稳定排序后计算 SHA-256；原始 pattern 不进入 `ToolCatalogCacheKey.safe_cache_key` 或 cache value。`ToolCatalogRecord` 为 frozen、UTF-8 字节数与内容 digest 固化的非空目录值，key/value 均拒绝 bool 冒充整数、任意权限字符串、NUL、错误 digest 与绝对超限载荷。

渲染与一致性边界：`ToolSnapshot.capture_brief_catalog_context()` 只负责显式捕获当前 pinned policy，`build_brief_catalog_record(context)` 使用同一 context 分别构建 legacy 与 Provider 目录并维持既有 parity gate；只有 fallback 路径明确选定或两者精确相等后才返回 record。`resolve_tool_catalog()` 先按完整 key lookup，miss 后只接受 exact-key `ToolCatalogRecord` 并要求 backend 精确确认发布；builder/parity 异常不 publish，同 key 异值为 conflict，错误 lookup/publish identity 为 unavailable，cache failure 是否旁路仍由未来 runtime 编排显式决定。

Memory backend：`MemoryToolCatalogCache` 以 `OrderedDict` 实现 LRU，默认最多 256 项、单目录 256 KiB、总目录 8 MiB，并允许调用方在硬上限内显式收紧。实例首次使用时绑定 PID 与 `asyncio` event loop，跨进程/loop 复用 fail closed；clear 只清条目、不转移 owner。generation 或任一策略输入变化会产生新 key，因此不需要 TTL 或主动删除旧 generation；这也允许已经 pin 到旧 generation 的在途请求继续命中，旧值最终由容量 LRU 回收。当前仅提供 backend-neutral 协议和 Memory primitive，不实现 Redis 共享缓存，避免在没有跨进程运行态编排时扩大一致性面。

本地门禁：Python 3.10.20、3.11.15、3.12.13 与 3.13.13 定向各 `161 passed`，ToolManager/Provider/RuntimeSnapshot/Reload/ModelSelector/LLM Payload/Chat/Search/PendingAction 联合各 `306 passed`；严格串行普通全量各 `1433 passed, 1 skipped`。mandatory root Sandbox `40 passed, 0 skipped` 且 JUnit failure/error/skip 为 0；Python 3.10 最低 Redis 5.2.0 / SQLAlchemy 2.0.0 / Alembic 1.13.0 / asyncpg 0.30.0 / FakeRedis 2.31.0 全量通过。Ruff 0.16.2 全量、G-04 新文件 format、diff check，以及 Pyright 1.1.407 新模块/测试均为 `0 errors, 0 warnings`。

制品门禁：fresh wheel/sdist 与 Twine/checksum 通过，wheel SHA256 `ab805c305183bddd1e49b3e417534ca09abc4d2f4970c9df3f40d477b61c06b0`、sdist SHA256 `0348bc4627dfbb6a6d227842fcbda072724b9838cc67ef7d1607e87807d3bb37`；两者各 82 个文件，包含 G-04 module，不含 `uv.lock`、cache 或 bytecode。Python 3.10/3.12 × wheel/sdist 四组仓库外安装均确认从 site-packages 加载、11 表、8 revision、离线 DDL、plugin reload、显式 ToolSnapshot render、Memory miss/publish/hit 与无模块级 cache；engine create、SQL execute、asyncpg connect、Redis command/connect 始终为 0。制品目录 `/tmp/moellm-g04-dist.FERb6W`，smoke 根目录 `/tmp/moellm-g04-smoke.Dn1C0A`。精确 HEAD 双 run 是 G-05 前置门禁；本阶段未读取 DSN/Redis URL/secret，未运行 migration，未连接服务，未接配置、startup/shutdown、现有 Categorize 或生产 runtime，未合并、未发布、未部署。

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
- [x] Chat History 持久化（G-01 Repository 与远端门禁；尚未接生产 runtime）
- [x] History Hot Cache（G-02 本地与精确 HEAD 双 run 远端门禁完成；尚未接生产 runtime）
- [x] Session Summary（G-03 本地与精确 HEAD 双 run 远端门禁完成；尚未接生产 runtime）
- [ ] Batch Insert
- [ ] DB Failure Spool
- [ ] Redis Failure Policy
- [ ] Tool Catalog Cache（G-04 本地门禁完成；精确 HEAD 双 run 待完成，尚未接生产 runtime）
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

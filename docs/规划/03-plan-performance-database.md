---
title: 03-plan-performance-database
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-21T08:30:34+00:00
---

# 03-plan-performance-database

# Plan 3：处理效率与数据库接入优化

> 推荐目标版本：`0.28 → 0.30`

> 实施门禁（2026-08-21）：Plan 1 远端发布门禁与 required `release-gate` 已完成；Plan 2 的 D-01a～D-08f 已完成各自精确 HEAD 远端 gate，D-09 在不操作生产的约束下继续锁定。Milestone E 的 E-01～E-08 已闭环；F-01 / F-02 已完成精确 HEAD 双 run gate。F-03 最终 HEAD `a4eb771678587e6bfd32f793c8a6f7eda88f29ab` 的 push run `32461256977` / PR run `32461262286` 均为 11/11 green、各恰好一个成功 `release-gate`，F-04 依赖已解除。F-04 实现提交 `21810cf836d89d07c268076d6e3d96b34cdfd04b` 已加入 `users / conversations / messages` 共享 metadata 与首个线性 revision `0001_users_conversations`；本地全门禁、fresh 制品及四组包外 Schema/graph/DDL smoke 已通过，包含规划的精确 HEAD 双 run gate 待完成，F-05 继续锁定。在线 migration 仍无条件拒绝；未读取生产 DSN，未创建 engine/session、Repository 实现或 Redis client，未运行 migration，也未 checkout 或连接数据库。

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

metadata/revision parity 测试会从 revision 操作重建独立 metadata，并比较全部列、类型、nullability、server default、Identity、PK/FK/unique/check/index 及 PostgreSQL 条件。F-04 四版本定向各 `32 passed`，联合 Engine/Repository/Agent/Graph/Scheduler/Conflict `423 passed`；四版本普通全量最终各 `977 passed, 1 skipped`，mandatory root Sandbox `40 passed, 0 skipped`。fresh wheel/sdist SHA256 为 `7c22c093605dd4213e8c5bb8751fa81f26925cc45e56d391366016762d679739` / `b5b52c0125fea1a9f565e531241fb19fd20bfb2a89e00d325a28a47d9b88c262`，Twine、制品内容检查与 Python 3.10/3.12 × wheel/sdist 四组包外 Schema/graph/DDL smoke 均通过。当前仅本地门禁完成，F-05 等待包含规划的 F-04 精确 HEAD 双 run gate；未运行 migration，未连接或修改数据库。

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
```

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
0003_tool_bundles
0004_usage_audit
0005_memory
```

F-03 已完成离线迁移地基；实现提交 `f9598561247e40a5ce8327a0ccd8d9f21f3fe04e` 当时保持空 graph，并由入口和 env 双重短路 Alembic 1.13 空图可能生成的误导性 `DROP TABLE alembic_version`。最终文档闭环 HEAD `a4eb771678587e6bfd32f793c8a6f7eda88f29ab` 对应 push run `32461256977` / PR run `32461262286`；两者均 11/11 green、各恰好一个成功 `release-gate`，F-04 依赖已解除。

F-04 实现提交 `21810cf836d89d07c268076d6e3d96b34cdfd04b` 将 graph 推进为 `revisions=1 / bases=1 / heads=1`，三者唯一标识均为 `0001_users_conversations`。离线 upgrade 只生成 version table、`users / conversations / messages` 及其约束/索引，明确不含 `DROP`；离线 downgrade 按 `messages → conversations → users` 逆依赖顺序删除。临时空图回归仍确认零 SQL，在线 migration 继续在 engine 创建前 fail closed。当前仅 F-04 本地门禁完成，精确 HEAD 双 run gate 待完成；未读取 DSN，未运行 migration，未连接或修改任何数据库。

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

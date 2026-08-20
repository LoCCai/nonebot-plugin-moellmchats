---
title: 03-plan-performance-database
date: 2026-08-19T14:55:10+08:00
lastmod: 2026-08-19T14:55:10+08:00
---

# 03-plan-performance-database

# Plan 3：处理效率与数据库接入优化

> 推荐目标版本：`0.28 → 0.30`

> 实施门禁（2026-08-20）：Plan 1 远端发布门禁与 required `release-gate` 已完成；Plan 2 仅推进到 D-01a 本地实现提交 `67638980b8abe3d515ca8146ab68381692f6ac74`，AgentRun、AgentStep、ToolCall 与 DeadlineContext 契约仍未完成。Plan 3 因此继续保持设计/Backlog 状态，不提前引入数据库、Redis、迁移或生产配置。

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

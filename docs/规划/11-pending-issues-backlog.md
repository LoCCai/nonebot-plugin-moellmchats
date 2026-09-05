---
title: 11-pending-issues-backlog
date: 2026-08-29T00:00:00+00:00
lastmod: 2026-08-29T00:00:00+00:00
---

# K-09 后续待修复与设计清单

> 来源：`fix/analysis-fixes` 的全库审查、当前 K-08/K-09 架构复核及 0.26.3 实施。本页以符号名和契约定位，不依赖会漂移的旧行号。已在 0.26.3 处理的项目不再冒充待办。

## 0.26.3 已收口

| 旧发现 | 当前状态 |
| --- | --- |
| PostgreSQL 二次取消可跳过 rollback/close | K-09B 以 shield/settle 同步修复 transaction factory 和 spool writer |
| URL 预检与实际 aiohttp 连接分离，可被 DNS rebinding/重定向绕过 | K-09D 改为 IP 固定、每跳重验的 `safe_request` |
| 分类缓存同键重复构建且发布冲突中断聊天 | K-09C 增加 generation-local `resolve_exact` single-flight 与精确冲突回读 |
| AST 不跟踪 walrus/`NamedExpr` | K-09E 已跟踪 process/network/mutating 别名，并覆盖 `safe_request` method 的变更判定 |
| 400 对整段文本搜 `audit`/`safety` 造成误报 | K-09E 只匹配结构化 `code/type` 的标准化精确值 |

## P1：下一个设计批次

### P1-1 spool 诊断 O(N) 全量重扫

`local_spool.py` 在 append/lease/ack 等背压路径反复扫描并解码 ready 文件。积压越重，单次写入越慢。应设计耐崩溃的增量计数/快照契约，启动或显式诊断时才全量核对；不能只删掉扫描而失去损坏检测。

### P1-2 usage 的幂等与不确定结果

commit 响应丢失时，usage 无法判断已写入还是未写入。如果盲目写 spool 会重复计费，直接丢弃则永久缺数。应先为 usage 引入稳定幂等键和数据库 `ON CONFLICT` 契约，再把 unknown/cancel 记录安全进入 spool；该项可能需要 migration，不在 0.26.3 无 migration 范围内强行实施。

### P1-3 损坏 Custom File 的启动可用性

当前坚持“候选 generation 中任一工具损坏则整代拒绝”，这防止管理员误以为只有某个文件未生效。但首次导入时尚无上一有效代，损坏文件可拖垮整个插件加载。后续应设计可审计的 last-known-good/quarantine 启动契约，而不是“坏文件直接跳过”。

## P2：并发、排空与资源生命周期

### P2-1 热历史缓存良性竞态

同会话并发 miss 时，后发布者可把“另一个请求已发布”误判为缓存不可信，使整个 generation 永久 bypass。需区分已有精确胜出窗口的良性冲突，与 token/generation 错位的真失效。

### P2-2 generation 租约获取时机

请求在 cooldown/admission 排队前获取旧代租约，可使 reload 排空被尚未开始执行的请求拖住。需结合准入公平性、请求 Deadline 与快照一致性，评估把租约下移到获得 slot 之后。

### P2-3 排空/close 无全局上限

runtime resource、trusted runner pool 或 lifecycle port 的 close 卡死时，reload/shutdown 可无限等待。后续需明确 drain timeout、FAILED 状态、资源是否安全遗弃以及运维恢复手册，不能只在外层加 `wait_for` 然后丢下清理任务。

### P2-4 Redis admission 轮询与 release 语义

0.25 秒固定轮询和 WATCH 全状态更新会在积压时产生事务风暴。应评估 pub/sub、BLPOP 或指数退避，并降低单 key 冲突范围。`slot()` finally 的 release 故障也不能简单吞掉：需保留原业务结果，同时以类型化审计/指标标记 lease 只能等 TTL 回收。

### P2-5 MCP 依赖的必选/可选语义

不能把所有缺失的 `mcp__*` 一律视为可选，否则会让本应 fail closed 的工具在缺依赖时暴露给模型。应扩展依赖契约，让插件显式声明 required/optional，再决定候选 generation 拒绝还是降级。

## P3：性能和可维护性

- ~~`UsageBatchQueue` / `AuditBatchQueue` 与旧 `is_repeat_ask_dict`~~ 2026-09-06 已核实 `is_repeat_ask_dict`（chat_runtime.py）全部 5 处引用均为写入、无任何读取方与测试引用，属只写死状态，已删除；`UsageBatchQueue`/`AuditBatchQueue` 仍待确认生产入口。
- `tool_manager.py` 同时承担模板、目录、Provider、快照和重载职责，应在独立分支分解；双视图 parity 应尽量在 reload 时证明，不要在聊天请求中反复计算。
- 同步可信 `ToolSpec` 与 runner workspace 扫描共用默认线程池，应评估独立有界 executor 或事件式监测。
- ~~`get_emotion` 的同步 glob/读图可在分段发送路径阻塞 event loop~~ 2026-09-06 已在 `send_emotion_message` 中移入 `asyncio.to_thread`。
- 非 root/Linux 的 Generated/Custom runner 仍 fail closed；如要支持无 root 或其他平台，必须重新证明 user namespace、UID 映射和 syscall 边界，不能降级为主进程执行。
- `license = "GPL"` 本轮保持不变。发布前需由维护者明确选择 `GPL-3.0-only` 或 `GPL-3.0-or-later`，再修改 SPDX 表达式与发布元数据。

## 建议顺序

1. 先设计 usage 幂等键与 spool 增量诊断，因为两者都涉及持久化可恢复性；
2. 再关闭热缓存竞态、租约时机和全局排空上限，每项使用可控事件测试双重取消与不确定结果；
3. Redis admission 和 MCP 依赖契约分开设计，不把“降低报错”当成安全语义；
4. 最后处理结构拆分、线程池、表情 I/O 和跨平台 runner，各自保留独立门禁。

---
title: 10-pending-issues-backlog
date: 2026-08-29T00:00:00+00:00
lastmod: 2026-08-29T00:00:00+00:00
---

# 待修复问题清单（2026-08-29 全库审查遗留）

> 来源：2026-08-29 对 `feat/llm-runtime-backpressure`（基线 `1043b0a…`）的三路全库审查。其中 11 项高价值/低风险问题已在 `fix/analysis-fixes` 分支修复，见 [09-code-review-fixes](./09-code-review-fixes-20260829.md)；本页是**其余未修复项**，全部状态为「待修复」。
>
> 文中行号为分析时快照（基线 `1043b0a…`），后续提交可能使行号漂移，定位时以符号名为准。每项标注了建议批次：**P1** 高价值（建议下一批处理）、**P2** 中优先、**P3** 低优先/清理。

## P1：高价值，建议下一批处理

### P1-1 spool 诊断在 append/lease/ack 路径做 O(N) 全量重扫，背压路径随积压二次方劣化

- 位置：`local_spool.py`（`_refresh_diagnostics_sync` 对每个 ready 文件调用 `_read_records_sync` 完整 JSON 解码；`_append_sync` 前后各一次、`_lease_sync`/ack 各一次）
- 影响：`max_ready_files` 上限 10,000。spool 越满（正是 DB 故障、最需要背压的时刻），每次 append/flush 代价越大；全部在持有 `asyncio.Lock` 的 `to_thread` 里串行执行，flush 吞吐崩塌。
- 方向：增量维护计数器（append +记录数、ack -记录数），或仅 stat 文件大小不解码内容；`ready_records` 只在启动/诊断 API 时全量算一次。

### P1-2 `PostgresTransactionFactory` 二次取消时跳过 `session.close()`，连接泄漏可耗尽连接池

- 位置：`agent_context_runtime.py`（`_rollback` 只捕获 `Exception`；`except asyncio.CancelledError: await self._rollback(session); await self._close(session)`）
- 影响：管理端取消或超时+管理取消叠加时，rollback 内的第二次 `CancelledError` 逃出，`close()` 永不执行；AsyncSession 连接直到 GC 才归还。`pool_size=10+overflow=20`，反复触发可打满池，后续事务在 `pool_timeout=30s` 上排队失败。`spool_worker.py` 的同类实现捕获 `BaseException`，两处不一致。
- 方向：`_rollback`/`_close` 改为捕获 `BaseException`（对齐 `PostgresSpoolRecordWriter`），或用 `asyncio.shield` + settle-task 模式。

### P1-3 网络工具 URL 校验存在 DNS rebinding / 重定向绕过

- 位置：`tool_execution.py`（分发前仅校验一次）、`network_safety.py`（校验时只解析一次 DNS）；随后 aiohttp 再次解析并默认跟随重定向。
- 影响：302 跳转到 `http://169.254.169.254/` 或内网主机可完全绕过 `validate_public_url`；DNS rebinding（校验解析公网 IP、fetch 时解析私网）同理。`tests/test_network_safety.py` 仅 6 个负例，无 rebinding/重定向用例。
- 方向：aiohttp 层强制自定义 connector（TTL=0 + 逐连接 IP 校验）、`allow_redirects=False` 并对每跳重验；补 rebinding/重定向/十六进制 IP 测试。

### P1-4 一个损坏的自定义工具文件会让整个插件导入失败

- 位置：`tool_manager.py`（模块级 `tool_manager = ToolManager()`；构造器内 `load_custom_tools()`）、`custom_tool_loader.py`（`load_file_tools` 对 AST/策略/Schema 错误直接 raise）。
- 影响：`custom_tools/*.py` 或 lifecycle 状态损坏 → NoneBot 插件加载崩溃，机器人完全不可用（而非跳过坏文件）。
- 方向：导入路径收集 per-file 错误并降级为跳过 + 告警；严格校验留给 `/刷新工具` 事务路径。

### P1-5 分类缓存无 single-flight，结果冲突会炸掉整个聊天请求而非降级

- 位置：`classification_cache.py`（publish 冲突抛 `ClassificationCacheConflictError`）；传播路径 `categorize.py` → `llm_payload.py` → 聊主流程 re-raise。
- 影响：两个并发相同 prompt 各自调 builder（双倍分类调用）；即便 temperature=0，返回不同 difficulty 时第二个请求直接无回复。分类失败本应降级为「无工具中等难度」（`categorize.py` 只处理了超时）。
- 方向：`resolve_classification` 增加 per-key 的 `asyncio.Future` single-flight；冲突时返回已有记录或按不可缓存结果降级。

### P1-6 usage 统计在 commit 结果未知时静默丢失

- 位置：`agent_context_runtime.py`（`except AgentContextCommitUnknownError: return False`；普通异常才落 spool）。
- 影响：连接在 COMMIT 时断开时，记录既不重试也不进 durable spool，token/成本记录永久丢失。
- 方向：给 `model_usage` 引入幂等键（如 run_id+序号），未知时同样写 spool，flush 端用 `ON CONFLICT` 去重。

## P2：中优先

### P2-1 同会话并发 cache miss 的良性竞态导致整个 generation 永久禁用热缓存

- 位置：`agent_context_runtime.py`（publish False ⇒ `_cache_trusted=False`）；根因 `history_hot_cache.py`（第二个并发 publish 必然返回 False）。
- 影响：`max_per_user=2` 下同会话两个请求同时 miss，后完成者把良性双载误判为失效，一次性波及该 generation 所有后续请求。
- 方向：publish 返回 False 且对端窗口已存在时视为「竞态失败但缓存健康」；只有 token 过期/generation 不匹配才 bypass。

### P2-2 generation 资源租约在 cooldown/admission 排队之前获取，reload 排空被排队请求拖住

- 位置：`chat_runtime.py`（先 `host.lease` 再 claim cooldown、再进 `admission_gate.slot`，最长 `request_timeout=180s`）；排空等待 `runtime_resources.py`。
- 影响：还在队列里等待准入的请求就持有旧代租约；reload 必须等它们全部出队才能切代，期间 `synchronize` 持锁，新消息静默阻塞。
- 方向：把 `host.lease` 下移到 `admission_gate.slot` 成功之后；reload 期间让 `synchronize` 快速失败或设排队上限。

### P2-3 关闭/重载路径无限等待：`_settle_task` 与排空循环都没有超时

- 位置：`runtime_resources.py`（`_settle_task` 的 shield 循环、reload 排空、close 排空）；`trusted_runner_pool.py` 的 gather 同样无超时。
- 影响：任一 lifecycle port 的 `close()` 挂起或某租约永不释放时，reload/close 永久悬挂。
- 方向：增加全局 drain timeout 配置，超时进入 FAILED 并保留可诊断状态。

### P2-4 usage 记录在取消时彻底丢失、spool 中毒后静默丢弃

- 位置：`agent_context_runtime.py`（commit 前先清空 `_pending_usage_records`；`CancelledError` 直接 re-raise 绕过 spool 兜底；`enqueue_usage` 抛 `SpoolWorkerResultUnknownError` 被 `except Exception` 吞掉）。
- 影响：commit 被取消时记录既没入库也没进 spool；spool 进入 RESULT_UNKNOWN 后所有 usage 静默丢弃。
- 方向：spool 兜底提到 CancelledError 分支之前（先 spool 后清除）；`flush_usage` 记录丢弃计数/结构化日志。

### P2-5 Redis admission 等待者的轮询风暴与无限续期

- 位置：`redis_admission.py`（0.25s 轮询，每次轮询 = TIME + WATCH + GET + PTTL + EXEC 全状态读改写；pending lease 过半即续期）。
- 影响：`max_pending=32` 时单 key 约 128+ 事务/秒，WATCH 冲突消耗 retry 预算；单 JSON key 成为串行化瓶颈。
- 方向：pub/sub 或 BLPOP 通知代替固定轮询，至少指数退避；考虑 per-key 子键降低 WATCH 粒度。

### P2-6 Redis admission `slot()` 的 finally 中未 shield 的 release 会掩盖真实结果

- 位置：`redis_admission.py`（body 成功后 release 遇 Redis 故障从 finally 抛错，替换成功结果；body 取消时同理）。
- 方向：release 失败仅记日志/指标，不覆盖原结果；TTL 兜底已存在（30s）。

### P2-7 MCP 前缀依赖在 server 离线时阻断整个工具重载

- 位置：`tool_manager.py`（`validate_dependencies` 对 missing 直接 raise；runtime_reload 的 known 集合不含离线 MCP 工具名）。
- 影响：默认模板鼓励声明 `mcp__*` 依赖，MCP server 未连上时 reload 整体失败。
- 方向：MCP 前缀依赖按可选处理（离线时告警跳过），与未安装插件触发器的宽容语义对齐。

### P2-8 AST 策略不追踪 walrus/NamedExpr 别名，进程/可变检测可被静态绕过

- 位置：`ast_policy.py`（`_ScopeVisitor` 无 `visit_NamedExpr`；别名绑定只发生在 Assign/AnnAssign）。
- 影响：`(f := os.system)("id")` 中 `f` 不会被解析为 `os.system`，`process.alias`/`effect.mutating` 漏报，`read_only` 声明绕过二阶段确认（运行时 seccomp 仍兜底，非完整逃逸，但破坏检测→确认契约）。
- 方向：增加 `visit_NamedExpr` 调用 `_bind_alias`；同理覆盖 `match` 捕获；补测试。

## P3：低优先 / 清理项

### P3-1 死代码与重复实现

- `UsageBatchQueue`/`AuditBatchQueue` 在生产路径是死代码（无任何 `queue.put()` 调用），且两份实现几乎逐行重复。二选一：真正接通内存队列，或删除并合并。
- `is_repeat_ask_dict` 只写不读（全部引用点为赋值/清空），且写入键混用 int/str。删除或接入真正的重复追问逻辑。

### P3-2 `tool_manager.py` 结构性问题（约 2800 行五类职责）

- 视图 dataclass、`ToolSnapshot`（每个 resolve 方法做 legacy+Provider 双计算再 parity 比对，请求期双倍 CPU，漂移即抛错中断聊天）、`ToolManager`（含 90 行内嵌示例工具源码字符串、目录构建、黑名单、MCP 合并）混在一个文件。
- 具体 bug：`commit=False` 事务预览时 `logger.debug` 记录的是旧状态而非候选；`load_custom_tools` 返回类型二义（`0` vs `(tools, deps)`）；parity 策略把目录漂移变成运行期聊天失败而非重载期失败。
- 方向：按 provider 拆分文件，示例模板移到资源文件，parity 校验前移到 reload。

### P3-3 测试覆盖空白

- 零覆盖模块：`config.py`（本轮已补）、`custom_tool_loader.py`、`mcp_manager.py`、`compat.py`（本轮已补）、`request_manager.py`、`admission_store.py`、`state_store.py`（本轮已补）、`categorize.py`、`chat_history.py` 等。
- 过薄：`test_network_safety.py`（6 个负例，无 rebinding/重定向用例，对应 P1-3）、`test_tool_snapshot.py`（1 个用例）。

### P3-4 sync 自定义工具共享默认线程池且无独立上限

- 位置：`tool_execution.py`（`asyncio.to_thread` 与 runner 的 `_scan_workspace`、spool 文件 IO 共享默认 executor）；`generated_tool_runner.py` 的 `_watch_workspace` 0.1s 轮询 ≈ 每次工具执行约 300 次 FS 扫描。
- 方向：sync 工具用独立有界 executor；workspace 监视改 inotify 或放宽到 1s。

### P3-5 杂项

- `get_emotion` 在异步发送路径做同步 glob + 全量读图，分段流式时阻塞事件循环。改 `asyncio.to_thread` 或启动时预载字节缓存。`utils.py`
- 400 敏感词判断对整个响应正文做子串匹配（`"audit"`、`"safety"` 等），普通配置错误被误报为敏感拦截。改为匹配已解析的错误码/类型字段。`llm_api.py`
- poke 冷却预检查是无锁 check-then-act，连续快速触发时其中一个走「冷却中」而非预期随机回复（代码注释已承认）。可注明接受，或改 tryClaim-and-release。`__init__.py`
- `get_llm_controller()` 配置热更替换实例存在短暂分裂限流额度的竞态（`(0,0)` 守卫只降低概率）。把 controller 放入稳定 holder，替换内部 limits 而非实例。`admission.py`
- `spool_worker.close()` 的二次 shield 未做取消加固，与 `_settle_task` 模式不一致，重复取消会绕过排空完成性检查。改用 `_settle_task`。`spool_worker.py`
- `wait_for_ready` 绕过 `_diagnostics_lock` 读字典（理论撕裂窗口）；`ProtocolBroker` 跨对象访问 `pending._clock()` 私有成员。`local_spool.py` `protocol_broker.py`
- 生成工具执行整体要求 Linux + root（fail-closed 但非 root/Windows/macOS 全不可用）。文档明示 Docker root 部署为唯一支持形态，或提供 `unshare --user --map-root-user` 路径。`generated_tool_runner.py` `generated_tool_worker.py`
- `pyproject.toml`：`license = "GPL"` 不是合法 SPDX 表达式（poetry-core 2.x 按 PEP 639 会拒绝构建）；未配置 `[tool.pytest.ini_options]`；`ruff = "0.16.2"` 精确锁死 dev 版本。

## 建议处理顺序

1. **批次一（P1）**：P1-1、P1-2 可独立小改；P1-3、P1-4 需要设计（connector 层 / loader 降级策略）；P1-5、P1-6 涉及数据契约（幂等键）。
2. **批次二（P2）**：多数是并发语义修正，建议每个独立提交并补并发回归测试。
3. **批次三（P3）**：清理类可合并为一个 housekeeping 提交；P3-2 拆分建议单独开分支。

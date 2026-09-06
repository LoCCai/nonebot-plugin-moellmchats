---
title: 16-platform-api-and-catalog-optimization-plan
date: 2026-09-06T00:00:00+00:00
lastmod: 2026-09-06T00:00:00+00:00
---

# 平台 API 与插件接入链路优化计划书

> 来源：2026-09-06 对「Bot 平台 API 处理 / 原生功能 / 插件中间件 / 调用接口」四类机制的专项梳理。基线 `e1b2192…`（分支 `fix/generated-bundles-review`）。本页只列**尚未实施**的项；零/低风险批次（P0）已随本轮实施，见文末记录。所有行号以基线为准，实施时以符号定位。

## 机制链路速览（实施前必读）

外部插件功能的一生：加载期（reload generation）经 PicMenu 投影 → 菜单规范化（每插件 128 功能/48,000 字符）→ `ToolSpec` 注册（兼容描述截 28,000 字符）→ `ToolSnapshot` 冻结（intent 索引 + `directory_digest` + 6 provider parity）；每请求经业务意图 O(1) 直达（命中则免目录免分类模型）→ 目录缓存解析（键含 `protocol_scope_digest`）→ 分类（缓存 TTL 60s，single-flight）→ Schema 组装（同键结构）→ 合成事件投递（targeted/full-bus）→ API 证据状态机回传。协议动作表（244 项）为模块级单次构建；成员名/协议探测分别为 600s 单飞缓存与每消息探测。

## P1（最高价值）：协议缓存键与目录内容因子同步重构

### 现状与问题

`protocol_context.py` 的 `snapshot.cache_digest` 入参包含 `message_id`/`reply_message_id`（`:181-182`、`:258-270`）。`ToolCatalogCacheKey.protocol_scope_digest`（`tool_catalog_cache.py:110`）与 schema 缓存键继承该 digest（`tool_manager.py:788`、`:1578`）。**每条消息键必不同 → 目录与 Schema 缓存零命中**：每消息触发 legacy+provider 目录双算（至多 96K 字符）+ 两次全量 sha256 + LRU 写放大（每消息新增一条、持续逐出）。

### ⚠️ 正确性陷阱（实施时必须同步骤）

目录内容**确实依赖消息文本**：`business_conflicting_protocol_tools`（`protocol_context.py:458-500`）按消息命中的业务触发词摘除协议工具，而 `plain_text` 目前**不在** digest 里。当前不发生正确性事故仅仅因为「永不命中、每次重算」。**只剔除 message_id 会引入「应摘除的协议工具未摘除」漏洞**。

### 实施方案（两步必须同一提交）

1. digest 入参剔除 `message_id`/`reply_message_id`。
2. 键加入内容因子：`business_conflict_digest = sha256(sorted(suppressed_tools))`。计算入口在目录/Schema 渲染前（`plain_text` 为空或 `protocol_tools_business_first` 关闭时恒为空集，可快路径短路）；空集与空集共享键（绝大多数消息）。
   - 备选（更简单但命中率低）：以 `plain_text.casefold()` 的 digest 作键因子——不同文本即使冲突集相同也各自成键，仅推荐作过渡。
3. `probe_protocol_capabilities` 的快照字段保留 message_id（运行时寻址仍需要），仅摘要不再继承。

### 验收

- 新增判例：同会话两条不同 message_id、文本均无业务冲突 → 第二次目录缓存命中（`lookup_calls` 不增）；一条命中业务触发词 → 键变化且目录中对应协议工具被摘除；`protocol_tools_business_first=false` → 全部共享键。
- 回归：现有 `test_tool_catalog_cache.py`、`test_cache_runtime_wiring.py`、`test_runtime_reload.py` 全绿。
- 效果度量：`observe_cache`/目录缓存命中指标在群聊常驻流量下从 0% 升至 >90%。

### 风险

中。改动横跨 protocol_context/tool_catalog_cache/tool_manager 三文件键语义；需证明目录渲染对 message_id 无隐式依赖（已核对渲染输入清单，无）。

## P2：缓存 miss 构建的 legacy+provider 双算改为代级抽验

- 位置：`tool_manager.py:1592-1616`（catalog）、`:802-853`（schema）、`:1513-1550`（legacy 直连路径）；parity 比较 `tool_providers.py:1873-1875`。
- 现状：provider cutover 开启时仍完整渲染 legacy 视图并做全字符串比对，构建成本约翻倍。
- 方案：provider 权威代跳过 legacy 全量渲染；parity 降级为「每 generation 首次构建时校验一次 + 按天抽验」，结果缓存在 snapshot。禁止完全删除（防回退闸门）。
- 风险：中。验收：代级校验命中漂移时仍 fail-closed（reload 期失败而非聊天期）。

## P3：兼容插件描述注册期缓存

- 位置：`tool_manager.py:2875-2879`（legacy `build_tool_schema`）、`:2804-2811`（provider 版）。
- 现状：选中+resident 插件的完整菜单描述（每插件 ≤28,000 字符）每请求重新渲染，注册期明明算过。
- 方案：`build_nonebot_plugin_candidate`（`nonebot_plugin_tools.py:209-264`）把渲染结果按 `is_superuser` 二态缓存进 `info`（多数插件无 hidden 功能可共享一份）；代内输入（info + 冻结 command_start 前缀）不变，天然安全。
- 风险：低。

## P4：协议能力探测会话级缓存

- 位置：`protocol_context.py:220`（v11 `get_version_info`）、`:238`（v12 `get_supported_actions`）。
- 现状：每条消息一次探测 Bot API 网络调用；实现名/版本与支持动作在 bot 会话内几乎不变。
- 方案：按 `(bot_id, implementation)` 缓存探测结果 TTL 300s，模式对齐 `member_cache`（单飞 + 失败不缓存）；`supported_actions_digest` 不变时直接复用。
- 风险：低。验收：连续两条消息第二次探测不再发起 API 调用；适配器重连/版本变化时失效（含 `bot_id` 键 + TTL 兜底已覆盖）。

## P5：v12 合成事件字段白名单化

- 位置：`event_simulator.py:387`（`model_dump(original)` 全量序列化重建）。
- 现状：每次兼容投递对原事件做完整 pydantic dump + 二次校验。
- 方案：按 `(protocol, event_type)` 维护保守字段白名单（sender/group/self_id/time/raw_message 等身份字段），仅覆写 message/message_id/id；白名单必须与 v11 手拼路径（`:402-421`）对齐并保证 `is_synthetic_event` 守卫依赖字段完整。
- 风险：中。仅在 full-bus 高频投递被证实为瓶颈后实施。

## 明确不做

- **full-bus 遍历全部 matchers**：设计使然的兼容成本，已有 `matcher_checked` 指标可观测。
- **协议动作表构建**：`protocol_registry.py:543` 模块级单次构建，无每请求成本。
- **pending nonce 清理**：O(256) 全表扫描，容量有界，可忽略。
- **分段发送节流**（`llm_api.py` 的 `sleep(2 + len/3)`）：防风控设计，不可削减。

## 实施顺序与批次

| 批次 | 内容 | 风险 | 前置 |
| --- | --- | --- | --- |
| P0（已完成） | 分类 prompt 延迟构建；目录缓存单条上限 256KB→1MiB；file:// 图片读盘移入 `to_thread` | 低 | 无 |
| 批次一 | P4 协议探测会话级缓存 | 低 | 无 |
| 批次二 | P1 缓存键重构（陷阱警示见上） | 中 | 批次一先行可减少键中探测字段 |
| 批次三 | P3 描述注册期缓存 → P2 双算抽验 | 低→中 | P1 落地后收益叠加 |
| 观察项 | P5 | 中 | 仅在指标证明瓶颈后 |

每批次沿用既定流程：判例复现/验证 → 独立提交 → 判例回归测试 → 简单 py 测试（py_compile + AST 结构断言）→ 有依赖环境跑定向 pytest 后合并。

## P0 已收口记录（本轮）

| 项 | 修复 | 文件 |
| --- | --- | --- |
| 分类 prompt 命中路径白拼 | `_build_prompt` 移入无缓存分支与 `build_record` 回调，命中路径（含 single-flight waiter）零拼接 | `categorize.py` |
| 目录缓存单条上限 256KB | 默认 `max_catalog_bytes` 262,144 → 1,048,576（覆盖 96K 全 CJK 目录 ≈288KB 的 3.6 倍余量；测试均用显式值不受影响） | `tool_catalog_cache.py` |
| `file://` 图片同步读盘阻塞事件循环 | 新增 `_read_local_image_base64`（`asyncio.to_thread` 读盘+编码），`_extract_send_data` 转 async（唯一调用方已 await） | `event_simulator.py` |

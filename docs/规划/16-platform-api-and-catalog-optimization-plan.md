---
title: 16-platform-api-and-catalog-optimization-plan
date: 2026-09-06T00:00:00+00:00
lastmod: 2026-09-07T00:00:00+00:00
---

# 平台 API 与插件接入链路优化计划书

> 来源：2026-09-06 对「Bot 平台 API 处理 / 原生功能 / 插件中间件 / 调用接口」四类机制的专项梳理。审查基线 `e1b2192…`，整合实施基线 `ee99c7d…`（唯一分支 `feat/generated-tool-bundles`）。本页同时维护待实施项与已收口记录；所有旧行号仅作定位提示，实施时以符号为准。

## 机制链路速览（实施前必读）

外部插件功能的一生：加载期（reload generation）经 PicMenu 投影 → 菜单规范化（每插件 128 功能/48,000 字符）→ `ToolSpec` 注册（兼容描述截 28,000 字符）→ `ToolSnapshot` 冻结（intent 索引 + `directory_digest` + 6 provider parity）；每请求经业务意图 O(1) 直达（命中则免目录免分类模型）→ 捕获协议作用域与业务冲突摘要 → 目录缓存解析 → 分类（缓存 TTL 60s，single-flight）→ Schema 组装（同一冲突身份）→ 合成事件投递（targeted/full-bus）→ API 证据状态机回传。协议动作表（244 项）为模块级单次构建；成员名/协议探测分别使用 600 秒与 300 秒单飞缓存。

## P1（已完成）：协议缓存键与目录内容因子同步重构

### 现状与问题

`protocol_context.py` 的 `snapshot.cache_digest` 入参包含 `message_id`/`reply_message_id`（`:181-182`、`:258-270`）。`ToolCatalogCacheKey.protocol_scope_digest`（`tool_catalog_cache.py:110`）与 schema 缓存键继承该 digest（`tool_manager.py:788`、`:1578`）。**每条消息键必不同 → 目录与 Schema 缓存零命中**：每消息触发 legacy+provider 目录双算（至多 96K 字符）+ 两次全量 sha256 + LRU 写放大（每消息新增一条、持续逐出）。

### ⚠️ 正确性陷阱（实施时必须同步骤）

目录内容**确实依赖消息文本**：`business_conflicting_protocol_tools`（`protocol_context.py:458-500`）按消息命中的业务触发词摘除协议工具，而 `plain_text` 目前**不在** digest 里。当前不发生正确性事故仅仅因为「永不命中、每次重算」。**只剔除 message_id 会引入「应摘除的协议工具未摘除」漏洞**。

### 已实施方案（两步同批落地）

1. `ProtocolCapabilitySnapshot` 继续保留具体 `message_id` / `reply_message_id` 供参数注入和执行寻址，但 `cache_digest` 只记录两者是否存在，不再记录具体值。存在性仍会改变协议工具可见范围，因此不能一并删除。
2. `ToolCatalogCacheKey`、`ToolSchemaCacheKey` 及两类 render context 同步加入 `business_conflict_digest`。摘要输入是排序去重后的 `suppressed_protocol_tools`；空集合使用 canonical `[]` 的稳定 SHA-256。
3. `ToolSnapshot` 在查缓存前从同一个不可变协议快照计算一次冲突集合，并把集合与摘要冻结进 render context。legacy/provider 的短目录及完整 Schema 四条渲染路径只消费冻结集合，不再读取实时正文或 `protocol_tools_business_first`。
4. `safe_cache_key` 只包含摘要，不包含用户原文、具体消息 ID 或工具名；不同 Bot、协议、支持动作、调用者、权限、场景、会话、消息/回复可用性与 generation 的原隔离均保留。

### 验收

- 判例已覆盖：同一 Bot/用户/群内两条不同消息且均无冲突时，目录与 Schema 各只构建一次；命中“给我点赞”后键变化且 `qq__like_me` 被摘除；关闭 `protocol_tools_business_first` 后同一正文重新复用空冲突键。
- 缓存键的每个新增动态字段均有隔离测试；冻结 context 后四条 renderer 不再读取实时业务冲突状态；具体正文、工具名和消息 ID 不进入安全缓存键。
- `test_protocol_context.py`、目录/Schema cache、runtime wiring、runtime reload 及协议 Broker/Registry 联合回归已通过。生产命中率仍应通过 `observe_cache` 观察，本批遵守“不操作生产”边界，不以合成测试冒充线上 >90% 指标。

### 风险

中。改动横跨 protocol_context、tool_catalog_cache、tool_schema_cache 与 tool_manager 四层键语义；需证明目录渲染对具体 message_id 无隐式依赖（已核对渲染输入清单，无）。

## P2：缓存 miss 构建的 legacy+provider 双算改为代级抽验

- 位置：`tool_manager.py:1592-1616`（catalog）、`:802-853`（schema）、`:1513-1550`（legacy 直连路径）；parity 比较 `tool_providers.py:1873-1875`。
- 现状：provider cutover 开启时仍完整渲染 legacy 视图并做全字符串比对，构建成本约翻倍。
- 方案：provider 权威代跳过 legacy 全量渲染；parity 降级为「每 generation 首次构建时校验一次 + 按天抽验」，结果缓存在 snapshot。禁止完全删除（防回退闸门）。
- 风险：中。验收：代级校验命中漂移时仍 fail-closed（reload 期失败而非聊天期）。

## P3（已完成）：兼容插件描述注册期缓存

- 位置：`tool_manager.py:2875-2879`（legacy `build_tool_schema`）、`:2804-2811`（provider 版）。
- 现状：选中+resident 插件的完整菜单描述（每插件 ≤28,000 字符）每请求重新渲染，注册期明明算过。
- 实现：`build_nonebot_plugin_candidate` 在 generation 注册期生成 frozen `CompatibilityDescriptionViews(user, superuser)` 并存入保留字段；无 hidden 功能时 `superuser` 直接复用 `user` 字符串，有 hidden 功能时才生成第二份。`ToolSpec.description` 固定为普通用户视图，legacy/provider 两条请求期 Schema 路径只按调用者选择缓存，不再重建完整描述。
- 安全边界：调用方不能在原始 `plugin_info` 伪造缓存字段；Provider parity 在 generation 发布期按冻结 `command_start` 重新生成期望双视图，并同时校验缓存类型、内容、共享关系与 `ToolSpec`。缓存缺失、类型不符、说明或前缀漂移均 fail closed。无菜单插件也统一执行 28,000 字符上限。
- 本地验收：描述/Provider/Schema/缓存定向为 `240 passed`，runtime/provider/cache/LLM payload 联合为 `428 passed`；Python 3.10/3.11/3.12/3.13 普通全量各 `3186 passed, 1 skipped`，mandatory root sandbox 为 `41 passed` 且 JUnit `failures=0 / errors=0 / skipped=0`。Ruff、CI 指定格式、Pyright、文档链接/示例、依赖、协议资源和环境依赖检查均通过。fresh wheel/sdist、Twine 与制品内容检查通过；Python 3.10/3.12 × wheel/sdist 四组均在 checkout 外加载 0.26.6，验证 v11/v12、38/31/175 动作和 runtime generation 1。
- 远端验收：实现提交 `915fc343289be35518dc0f5009fd4c3bc6258bab` 的 push run [`34084950241`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/34084950241) 精确命中该 SHA，12 个 job 全部 `completed/success`，且恰好一个 `release-gate`（job `101627235176`）成功。GitHub wheel/sdist SHA-256 分别为 `a6f3e55be84723279841053a653a292adcaaf0723eb6c6b4b72b03aef1626901` / `993b757d0174088cb11323d22d59d0aa123a46a10b6d892124a9e7d833807303`，与最终本地候选逐字一致；`BUILD-METADATA.json` 绑定同一 repository、SHA、run 和 0.26.6。远端当前只有唯一/default `feat/generated-tool-bundles` 且无活动 PR，因此本批没有可触发的 `pull_request` run，未借用历史 PR 结果。本文所在证据提交继续以自身 push run 的唯一成功 `release-gate` 作为最终闭环判据，不为记录自指 run 再追加第三个提交。
- 风险：低。

## P4（已完成）：协议能力探测会话级缓存

- 位置：`protocol_context.py:220`（v11 `get_version_info`）、`:238`（v12 `get_supported_actions`）。
- 现状：每条消息一次探测 Bot API 网络调用；实现名/版本与支持动作在 bot 会话内几乎不变。
- 实现：按 Bot 对象会话身份、协议、Adapter、`bot_id` 和可见实现/版本提示缓存探测结果，TTL 300 秒、LRU 上限 256；模式对齐 `member_cache`（单飞 + 失败不缓存），`supported_actions_digest` 不变时直接复用。新 Bot 对象会话立即隔离，无法从本地提前看到的服务端版本变化由 TTL 收敛。
- 安全加固：缓存不保存事件、用户、群、消息、权限或 generation；这些仍按请求重新冻结。二阶段确认使用 `force_refresh` 丢弃普通命中并重新探测，刷新失败后旧成功记录不可继续使用。
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
| 批次一（已完成） | P4 协议探测会话级缓存 | 低 | 无 |
| 批次二（已完成） | P1 缓存键重构（陷阱已同步收口） | 中 | P4 已完成 |
| 批次三（进行中） | P3 描述注册期缓存（已完成）→ P2 双算抽验（下一批） | 低→中 | P1 已完成 |
| 观察项 | P5 | 中 | 仅在指标证明瓶颈后 |

每批次沿用既定流程：判例复现/验证 → 独立提交 → 判例回归测试 → 简单 py 测试（py_compile + AST 结构断言）→ 有依赖环境跑定向 pytest 后合并。

## P0 已收口记录（本轮）

| 项 | 修复 | 文件 |
| --- | --- | --- |
| 分类 prompt 命中路径白拼 | `_build_prompt` 移入无缓存分支与 `build_record` 回调，命中路径（含 single-flight waiter）零拼接 | `categorize.py` |
| 目录缓存单条上限 256KB | 默认 `max_catalog_bytes` 262,144 → 1,048,576（覆盖 96K 全 CJK 目录 ≈288KB 的 3.6 倍余量；测试均用显式值不受影响） | `tool_catalog_cache.py` |
| `file://` 图片同步读盘阻塞事件循环 | 新增 `_read_local_image_base64`（`asyncio.to_thread` 读盘+编码），`_extract_send_data` 转 async（唯一调用方已 await） | `event_simulator.py` |

## P4 已收口记录（2026-09-07）

| 边界 | 结果 |
| --- | --- |
| 连续与并发请求 | 同一 Bot 会话、协议、实现提示下只构建一个探测；后续消息只重建自己的 `ProtocolCapabilitySnapshot` |
| 取消与失败 | 等待者取消由 `asyncio.shield` 隔离；探测异常、超时、非法响应和被取消 task 均不发布缓存 |
| 身份与失效 | key 绑定对象会话、协议、Adapter、Bot ID、实现/版本提示；TTL 300 秒，最多 256 条，弱引用防止缓存延长 Bot 生命周期 |
| 确认安全 | `ProtocolBroker.confirm()` 强制 refresh；失败先淘汰旧成功，保持危险动作执行前能力复核 |
| 兼容性 | 不增加配置、运行依赖、数据库 migration、Redis key 或后台任务；v11/NapCat/v12 对外快照结构不变 |

实现文件为 `protocol_context.py` 与 `protocol_broker.py`，判例位于 `test_protocol_context.py` 并复用 `test_protocol_broker.py` 的确认重探测断言。协议定向为 25 passed，协议/目录缓存/runtime reload 联合回归为 136 passed；Python 3.10/3.11/3.12/3.13 普通全量各 `3172 passed, 1 skipped`，mandatory root sandbox 为 `41 passed` 且 JUnit `failures=0 / errors=0 / skipped=0`。Ruff、CI 指定格式、Pyright、文档 11 JSON/8 TOML/10 Python 片段、162 个本地 Markdown 链接、13 项运行依赖/10 项开发依赖、244 动作/244 策略/3 wrapper 和环境依赖检查均通过。

fresh wheel/sdist 的 SHA-256 分别为 `75fb0887eaa263fcea7d4e70d462c39a8215e083c3eb3fe55840a841bb49239b` / `03ad263f47f4698a45882443ceb87cbc436ca978f8ef75d110ea8972ae85410c`，Twine 与制品内容检查通过；Python 3.10/3.12 × wheel/sdist 四组均在仓库外从 site-packages 加载 0.26.6，并验证 v11/v12、38/31/175 动作清单和 runtime generation 1。未安装或重启七七、未连接真实 Bot/模型/数据库/Redis、未发送 QQ 动作、未发布 PyPI。

远端实现门禁已关闭：实现提交 `ecfb32cac1da0fb616a344744949a86bb3ce5de7` 的 push run [`34079965087`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/34079965087) 精确命中该 SHA，12 个 job 全部 `completed/success`，且恰好一个 `release-gate`（job `101613393303`）成功；本地 HEAD、tracking 与 `ls-remote` 当时三方一致。此前分支整合已按用户要求把 `feat/generated-tool-bundles` 设为唯一远端及默认分支，历史 PR #2～#5 均已合并且其 base 分支已删除，因此本批不存在可触发的活动 pull_request run；这不是用旧 PR 结果替代当前门禁。本文所在证据提交以其自身 push run 的唯一成功 `release-gate` 作为最终闭环判据，不再为记录该自指 run 追加第三个提交。

## P1 已收口记录（2026-09-07）

| 边界 | 结果 |
| --- | --- |
| 缓存复用 | 同一 Bot/用户/群、权限、generation 和消息/回复可用性下，具体消息 ID 与无冲突正文变化不再改变目录或 Schema 键 |
| 正确性 | canonical 业务冲突集合摘要同步进入两层键；命中业务菜单时对应协议工具被摘除，关闭业务优先时复用空集合键 |
| 冻结输入 | legacy/provider 的短目录与完整 Schema 四条路径只读取 context 内冻结的抑制集合，不回读实时正文或开关 |
| 安全键 | `safe_cache_key` 不包含用户原文、具体消息 ID 或工具名；Bot、协议、权限、场景、消息/回复存在性及 generation 隔离保留 |
| 兼容性 | 不增加配置、运行依赖、数据库 migration、Redis key 或后台任务；P4 的确认强制能力重探测语义不变 |

实现提交为 `8521ad1b0de635ae47e70cc174fc3d4b5409927f`。协议/目录/Schema/runtime 定向联合回归为 `507 passed`；Python 3.10/3.11/3.12/3.13 最终源码树普通全量各 `3184 passed, 1 skipped`，mandatory root sandbox 为 `41 passed` 且 JUnit `failures=0 / errors=0 / skipped=0`。Ruff、CI 指定格式、Pyright、文档 11 JSON/8 TOML/10 Python 片段、162 个本地 Markdown 链接、13 项运行依赖/10 项开发依赖、244 动作/244 策略/3 wrapper、环境依赖及 diff 检查均通过。

fresh wheel/sdist 的 SHA-256 分别为 `93756ca6364ed8ae3ccaf86e330c8966c367fc7635dc217d5a6495d3b64e031d` / `341f296dd58d2d3da1a91fb6939143a36ff31e628eaab2f1c452508516852c86`；本地与 GitHub artifact 的 `SHA256SUMS` 完全一致，Twine 与制品内容检查通过。Python 3.10/3.12 × wheel/sdist 四组均从 checkout 外加载 0.26.6，并验证 v11/v12、38/31/175 动作清单和 runtime generation 1。

实现提交的 push run [`34082463497`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/34082463497) 精确命中该 SHA，12 个 job 全部 `completed/success`，且恰好一个 `release-gate`（job `101620379977`）成功；当时本地 HEAD、remote-tracking 与 `ls-remote` 三方一致。远端唯一/default 分支均为 `feat/generated-tool-bundles`，当前无活动 PR，因此没有可触发的 `pull_request` run；未借用历史 PR 结果。本文所在证据提交继续以自身 push run 的唯一成功 `release-gate` 作为最终闭环，不为记录自指 run 再追加提交。

本批未安装或重启七七，未修改 `/app/qi-dev`，未连接真实 Bot、模型、PostgreSQL 或 Redis，未发送 QQ 动作，未发布 PyPI。生产目录缓存命中率仍留待后续获准观测，未以合成判例冒充线上指标。

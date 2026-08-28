---
title: 09-business-routing-execution-truth
date: 2026-08-28T14:20:00+00:00
lastmod: 2026-08-28T14:20:00+00:00
---

# K-08 业务路由与执行状态真实性（0.26.2）

## 目标与边界

本阶段修复现场“今天谁发言最多”被错误选择为其他统计插件、调用失败却产生模糊拟人回复的问题。依赖顺序固定为：PicMenu 快照竞态 → 业务意图所有者 → 命令构造 → Matcher/API 真实状态 → 重试与安全日志 → 进度显示开关。

插件开发基线是 `2fa8b597e75f137a86bbb279aaf6288503f576db`，分支为 `feat/generated-tool-bundles`，远端只使用 `https://github.com/LoCCai/nonebot-plugin-moellmchats.git` 的 `origin`。不修改七七依赖、锁文件、配置、已安装包或进程，不发送真实 QQ 动作，不连接真实模型/数据库/Redis，不合并 PR，不发布 PyPI。仓库根未跟踪 `uv.lock` 保留且不提交。

七七工作区 `/app/qi-dev` 没有 Git 元数据。本阶段只更新 QWeb 功能目录与 PicMenu 桥接源码/测试，不能为这些文件声称 Git 提交或远端交付；它们也不会打入 MoEllmChats wheel。

## 依赖顺序与当前状态

| 节点 | 前置 | 当前状态 | 验收定义 |
| --- | --- | --- | --- |
| K-08A PicMenu 快照身份 | 0.26.1 | 已实现，本地全量通过 | 一次性分离投影、插件/功能计数、SHA-256；watcher 覆盖初始空目录竞态；读取异常保留上一有效快照 |
| K-08B 唯一业务所有者 | K-08A | 已实现，本地全量通过 | `llm_intents` NFKC/casefold/空白标点规范化精确匹配；唯一所有者纠正分类；重复/隐藏/黑名单/未加载 fail closed |
| K-08C generation 命令契约 | K-08B | 已实现，定向回归已通过 | 冻结真实 `command_start`；`/` 优先，否则最短字典序；空前缀移除占位；Schema 只允许 1～1024 字 `command` 且无额外字段 |
| K-08D Matcher/API 真实状态 | K-08C | 已实现，v11/v12 定向回归已通过 | 九种 `PluginDispatchResult`；Adapter 成功后才捕获；`send_like` 副作用、空命中、部分成功、结果不确定均不伪造成文本成功 |
| K-08E 重试与安全审计 | K-08D | 已实现，定向回归已通过 | generation/工具/参数摘要指纹；相同失败不重放；部分/不确定封锁工具；日志只有摘要和计数 |
| K-08F 显示开关、文档与版本 | K-08E | 已实现，本地全量通过 | 0.26.2；`tool_progress_messages_enabled=true`；确认/结果/最终反馈/日志不受开关影响；安装、配置、架构、接入和排错文档同步 |
| K-08G 本地与远端门禁 | K-08F | 本地已关闭，远端待提交 | 四版本、sandbox、制品与包外 smoke 已全绿；新 PR 以 `feat/llm-runtime-backpressure` 为 base；push/PR 各唯一成功 `release-gate` 后关闭 |

## 目录与所有者契约

PicMenu 当前内存投影先复制、规范化并深度冻结，再计算 canonical JSON SHA-256。该身份与目录条目摘要进入 runtime watcher、分类缓存、Tool Catalog/Schema 缓存和审计上下文。一次候选只消费同一份分离投影，避免构建途中 PicMenu 内存变化导致空目录与完整目录混合。

QWeb/PicMenu 目录契约版本升为 `2026.08.28.1`：

- QWeb 功能字段为 `llm_intents`；
- PicMenu 投影字段为 `pmn_llm_intents`；
- 每项 4～80 字，每功能最多 16 项；
- 只参与发现，不改变菜单可见性、插件加载或 Matcher 权限。

七七的 `qi_post / B 话榜与即时排行` 已声明十个现场/常见变体：`今天谁发言最多`、`今日谁发言最多`、`看一下今日的群发言排行`、`查看今日群发言排行`、`今日发言榜`、`今日发言排行`、`今天发言榜`、`今天发言排行`、`今天谁说话最多`、`今天谁话最多`。

唯一所有者选择来源记为 `business_intent_owner`。重复所有者返回 ambiguous；所有者隐藏、拉黑、权限不足或未加载返回 unavailable。两者都不会回退到分类模型选择另一个插件。没有精确别名命中时才保留分类模型结果；分类缓存策略升为 v3，并绑定目录摘要、generation、场景和超级用户身份。

## 真实执行状态与重试

兼容工具固定返回以下状态：

| 状态 | ToolCall 映射 | 重试规则 |
| --- | --- | --- |
| `matched_with_output` / `matched_side_effect` | `COMPLETED` | 成功 observation，不要求重复 |
| `timed_out` | `TIMED_OUT` | 相同工具和参数禁止重放 |
| `admission_rejected` | `REJECTED` | 本次未执行，可在新任务/压力恢复后再试 |
| `not_matched` / `matched_empty` / `failed` | `FAILED` | 相同工具和参数禁止重放，允许不同工具或实质不同参数 |
| `partial_success` / `result_unknown` | `FAILED` | 本任务内整个同名工具禁止再次调用 |

`on_calling_api` 只暂存消息和动作证据，`on_called_api` 成功才提交。发送失败不能把待发送文本计入结果；网络、断连或超时造成外部结果不确定时不自动重试。读取 API 成功但无文本/图片仍是 `matched_empty`；包内协议策略认定的 mutating API（例如 `send_like`）成功且无文本时才是 `matched_side_effect`。已有已确认输出后又发生 API/handler 失败则是 `partial_success`。

安全日志包含 request/tool-call 摘要、generation、目录摘要、选择来源、插件、意图摘要、脱敏 command 形状及摘要、Matcher/API 计数、最终状态、耗时和重试决策。不得记录完整工具/API 参数、Token、Cookie、Authorization、URL 查询或本地路径。

`tool_progress_messages_enabled=false` 仅隐藏模型前置话术及“正在搜索/调用/执行”。二阶段确认、插件结果、工具 observation、最终总结、最终失败反馈和后台日志始终保留；进度消息本身绝不是执行状态。

## 当前本地证据

实现提交前的最终本地复核：

- 修改相关核心测试统一取得 `382 passed`，其中包含 v12 完整 Matcher→`send_message`、真实 `partial_success`、读取 API 空结果、关闭进度时两类确认及并行失败重放拦截；
- Python 3.10、3.11、3.12、3.13 串行普通全量均为 `2995 passed, 1 skipped`；3.12 固定 NoneBot 2.4.4 / Adapter OneBot 2.4.6，3.13 的 10 条 warning 均来自 NoneBot `ForwardRef` 的上游弃用提示；
- mandatory root sandbox 为 `41 passed, 0 skipped`，JUnit 二次校验输出 `SANDBOX_JUNIT_OK tests=41 skipped=0`；
- Ruff 0.16.2 全源码/测试/脚本通过，CI 指定 14 个格式目标通过；Pyright 1.1.411 的 CI 指定协议边界为 `0 errors, 0 warnings`；
- 仓库检查通过 JSON 11、TOML 8、Python 8 个文档片段，20 个 Markdown 文件的 129 个本地链接、12 项运行依赖、10 项开发依赖及 244/244/3 项协议资源均通过，`pip check` 无破损依赖；
- fresh wheel/sdist 的 Twine 与包内容检查通过；wheel SHA-256 为 `5e46939dea5f7f1c326f0d207041a343a6e4ab90a6f2aa4fe7b8a7b971637de7`，sdist SHA-256 为 `e243fb9946e5642e17d7ab8f2df5d17b7e09dcf50ac107ad5cf7cdc30517ee37`；Python 3.10/3.12 × wheel/sdist 四组 checkout 外 v11/v12 加载、244 项协议清单和 generation reload 均通过；
- 七七 QWeb/PicMenu 四个定向文件为 `54 passed, 1 failed`，唯一失败是 `.venv` 中已安装旧 MoEllmChats 源码行号导致历史事件审计豁免指纹漂移；本阶段目录桥接断言没有失败。按边界没有修改七七已安装包或 waiver 来掩盖该失败。

以上是本地隔离证据，不证明七七已安装 0.26.2、重载或完成真实 QQ 验收。

## 恢复点与待关闭项

1. 保留未跟踪 `uv.lock`，核对分支和工作树；
2. 完成 K-08F 文档和 0.26.2 版本一致性；
3. 串行运行 Python 3.10～3.13 普通全量、NoneBot 2.4.4 兼容、mandatory root sandbox、Ruff、格式、Pyright、仓库/依赖/文档检查、fresh build、Twine 与包外 wheel/sdist smoke；
4. 形成实现提交并推送自有 `origin`；不得推送上游、安装七七或触发真实 QQ；
5. PR #3 已合并，创建新 PR，head 为 `feat/generated-tool-bundles`，base 为 `feat/llm-runtime-backpressure`；
6. 核对本地 HEAD、remote-tracking、`ls-remote`、PR head，以及 push/PR 两类 Actions；每类必须恰好一个 `release-gate` 且全部 job 成功；
7. 最终证据提交只记录真实 SHA/run/PR 状态，不合并、不 promotion、不发布 PyPI。

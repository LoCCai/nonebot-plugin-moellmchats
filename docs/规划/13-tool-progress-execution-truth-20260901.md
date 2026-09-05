---
title: 13-tool-progress-execution-truth-20260901
date: 2026-09-01T00:00:00+00:00
lastmod: 2026-09-01T00:00:00+00:00
---

# K-11 工具进度与恢复状态真实性（0.26.5）

> 当前状态：以 `feat/generated-tool-bundles` 精确基线 `7693b4de1c9240dccde8773557623056a51fa3b4` 实施。0.26.5 实现提交 `e704092a1e8d9ad215e4e9de35a9fe403483d56f` 已通过完整本地门禁和精确 push/pull_request 双 Actions，可作为隔离测试安装恢复点。本文件所在证据提交仍须通过自身双 Actions 才最终闭环。

## 问题与边界

现场需要的是“每个真正准备调用的函数或插件都有一条明确提示”，而旧行为依赖模型是否输出前置话术：空白话术可能吞掉固定提示，协议路径没有一致兜底，并行批次也只给一个笼统标题。与此同时，业务插件可能先调用成员资料查询，失败后改用另一只读查询并成功发送正文；旧聚合逻辑仍把前面的只读失败判成 `partial_success`，导致模型把已经成功的“抢面包”总结成失败，并封锁同一个 `bread_shop` 后续的“赌面包”。

K-11 同时修复显示层和执行真实性，但不放宽副作用重试：真正的 `partial_success` / `result_unknown` 仍封锁整个同名工具。本阶段只修改本插件开发仓库，不安装或重启七七，不修改七七依赖、锁文件、配置、`.venv` 或进程，不调用真实模型、PostgreSQL、Redis 或 QQ API，不合并 PR，不发布 PyPI。

## 依赖顺序与状态

| 节点 | 依赖 | 当前状态 | 交付语义 |
| --- | --- | --- | --- |
| K-11A 确定性逐调用进度 | K-10 | 已实现并通过首轮定向验证 | 只有通过 Schema、权限、信任和重复检查的调用才提示；搜索、协议、NoneBot、Registered、Custom File、Generated、MCP 按可信来源显示；并行逐项提示 |
| K-11B 可选自然话术与固定命令 | K-11A | 已实现并通过首轮定向验证 | `tool_progress_model_preface_enabled=false`；复用同一次工具决策响应、脱敏后合并，不增加请求；固定 SUPERUSER 命令原子写入并热更新 |
| K-11C 只读恢复与部分成功反馈 | K-11B | 已实现并通过首轮定向验证 | 已知只读失败可被随后已确认输出/副作用恢复；未知/变更失败和不确定副作用继续 fail closed；真正部分成功明确保留已可见结果 |
| K-11D 版本、配置、文档与规划 | K-11C | 本地已完成 | 版本 0.26.5；同步配置、命令、架构、排错、安装、依赖、README、CHANGELOG 和 K-11；无新依赖或 migration |
| K-11E 本地与远程门禁 | K-11D | 实现已通过，证据提交待门禁 | 串行四版本、静态、仓库/文档/资源、sandbox、fresh 制品、Twine、四组包外加载及实现提交双 Actions 已通过；证据提交自身双 Actions 待关闭 |

## 进度消息契约

`tool_progress_messages_enabled=true` 是总开关。每个获准调用恰好发送一条运行时标题，例如：

```text
正在投递插件：bread_shop｜功能：抢面包
正在调用搜索工具：web_search
正在调用协议接口：napcat_v11__send_like｜功能：发送点赞
```

Registered、Custom File、Generated 和 MCP 分别显示注册工具、自定义文件工具、生成工具和 MCP 工具。功能名只能来自可信协议摘要或脱敏的 NoneBot 指令首词；不显示完整参数、QQ 号、URL、路径、Token、Cookie 或 Base64。二阶段动作显示“正在准备工具确认”，不会声称已执行。未知、参数错误、越权、重复受限或策略拒绝的调用没有假进度。

`tool_progress_model_preface_enabled=false` 默认关闭。开启后，主模型可以在同一次工具决策响应里给一句说明；运行时折叠空白、截断、脱敏并合入固定标题，仍只发一条消息。并行批次只把说明附在第一项。空白说明按空处理，不能压掉固定标题，也不会产生额外模型请求。

进度发送有 1 秒预算。失败或超时只记录 `progress_status` 和安全异常类型，工具照常执行，不因为提示失败重试副作用；`CancelledError` 原样传播。确认、工具结果、最终总结和审计不受总开关影响。

## 执行状态真实性

兼容调度保留 `api_failed` / `api_unknown`，并增加 `api_read_failed`、`api_read_recovered`、`api_unresolved_failed` 和 `api_unresolved_unknown`。已知只读 API 失败后，如果 Matcher 正常完成并产生 Adapter 已确认的文本、图片或副作用，则只读降级已恢复，最终仍是 `matched_with_output` 或 `matched_side_effect`。只读失败且没有任何可验证结果仍是 `failed`。

未知 API、变更 API 失败、Matcher 异常和不确定副作用仍产生 `partial_success` 或 `result_unknown`。真正的 `partial_success` observation 会包含最多 2000 字已确认文本和图片数量，明确这部分已经成功且已对用户可见；模型只能描述剩余步骤失败或不确定，不能把整个动作总结成失败。两种不确定状态仍封锁本任务内整个同名工具，副作用不自动重试。

## 验证与交付状态

本地证据只证明当前开发工作树：进度/配置/命令/兼容调度联合定向为 `148 passed`；Python 3.10、3.11、3.12（NoneBot 2.4.4 / OneBot Adapter 2.4.6）和 3.13 串行普通全量均为 `3129 passed, 1 skipped`，唯一 skip 是单独执行的 mandatory root sandbox 文件。mandatory root sandbox 为 `41 passed`，JUnit 二次复核为 `tests=41, skipped=0`。Ruff 全仓、CI 指定 14 个格式目标、协议边界 Pyright、diff、仓库、文档链接、依赖、244 项协议资源和环境依赖一致性检查均通过。

Python 3.13 首轮整套运行曾有两个未改动的 runtime watcher / spool worker 用例等待超时；同一环境隔离复跑为 `2 passed`，随后整套重跑为 `3129 passed, 1 skipped`，因此只把后一次完整绿灯计入门禁，不隐藏首轮抖动。定向回归覆盖 v11/v12 只读失败恢复、只读失败无输出、协议和六类工具来源、并行逐项提示、自然话术脱敏、发送失败/超时/取消，以及同一 `bread_shop` 的吃、买、抢、赌连续四步。

fresh wheel/sdist 的 SHA-256 分别为 `c2a6f576c81c89ef49ffb29084da02652f516a5294354c6da47df36d1bdd4580` / `e2be72eec03453ca533d648d817ad0dc89905c466ab7305a04ef323f1da07b5f`；Twine 与包内容检查通过。Python 3.10/3.12 × wheel/sdist 四组均从 checkout 外的独立 `site-packages` 加载，验证 v11/v12 消息门面、v11 38 / v12 31 / NapCat 175 项清单与 generation 1 发布。

实现提交 `e704092a1e8d9ad215e4e9de35a9fe403483d56f` 已只推送到本仓库 `origin/feat/generated-tool-bundles`。push run [`33495001417`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33495001417) 与 pull_request run [`33495005164`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33495005164) 均精确命中该 SHA、12/12 jobs 成功、`non_success=[]`，且各恰好一个成功 `release-gate`。本地 HEAD、remote-tracking、`git ls-remote` 与 PR #5 head 核验时一致，PR 为 `OPEN / MERGEABLE / CLEAN`；未合并、未发布、未部署。

本文件所在纯证据提交也必须通过自身 push/pull_request 双 Actions。为避免无限自指，不再创建第三个提交记录证据提交自身 run；最终交付直接报告其核验结果、实现 SHA、证据 SHA、CI run、PR 状态和用户自行执行的精确 `.venv` 安装命令，不以 CI 冒充七七线上验收。

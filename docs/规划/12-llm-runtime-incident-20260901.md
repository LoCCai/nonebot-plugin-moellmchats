---
title: 12-llm-runtime-incident-20260901
date: 2026-09-01T00:00:00+00:00
lastmod: 2026-09-01T00:00:00+00:00
---

# K-10 LLM 运行事故修复（0.26.4）

> 当前状态：以 `feat/generated-tool-bundles` 的精确基线 `0e91477c8655aff6734314a2c10624c4b2032b9e` 实施。0.26.4 实现提交 `2b87cdf410b3c77792b5d8c9d37ab11b379d72c8` 已通过完整本地门禁和精确 push/pull_request 双 Actions，可作为隔离测试安装恢复点。本文件所在证据提交仍须通过自身双 Actions 才最终闭环。

## 问题与证据边界

近期运行日志暴露了三类彼此独立的问题：分类传输超时被通用解析重试捕获，可能再等待一次完整超时；`max_repeated_tool_calls` 只按工具名累计，使同一工具的不同参数被误判为原样循环；进度显示虽然已有配置，但缺少不经过 LLM 的现场管理入口。服务商错误正文也不应为了诊断 400 或解析失败而进入应用日志。

本阶段只修改插件仓库。七七工作区已经存在的面包批量排行实现只做定向测试，不继续修改，也不把宿主源码冒充成本仓库提交。未连接真实模型、PostgreSQL、Redis 或 OneBot，没有发送 QQ 动作。

## 依赖顺序与状态

| 节点 | 依赖 | 当前状态 | 交付语义 |
| --- | --- | --- | --- |
| K-10A 分类超时与日志边界 | K-09 | 已实现并通过本地门禁 | 传输超时立即统一抛给外层降级；任一 400 都不读正文；解析异常只记次数和异常类型 |
| K-10B 参数级重复限次 | K-10A | 已实现并通过本地门禁 | 串行和受信只读并行入口均按 `(generation, tool_name, canonical_arguments_digest)` 计数；不同参数仍受总轮次/步骤限制 |
| K-10C 固定进度指令 | K-10B | 已实现并通过本地门禁 | `设置工具进度 开/关` 及固定别名只允许 `SUPERUSER`，不经过分类、LLM 或工具链，写入既有配置并热更新当前快照 |
| K-10D 版本、文档与依赖 | K-10C | 已实现并通过本地门禁 | 版本 0.26.4；配置、命令、架构、排错、安装、CHANGELOG 和规划同步；无新依赖或 migration |
| K-10E 本地与远程门禁 | K-10D | 实现已通过，证据提交待门禁 | 四版本普通全量、静态、sandbox、制品和包外加载已通过；实现提交双 Actions 已关闭，证据提交自身双 Actions 待关闭 |

## 行为契约

分类请求的“有限重试”只用于响应兼容问题。首次 400 可在移除 `response_format=json_object` 后重试一次，非超时解析错误也最多再试一次；连接、读取或总请求超时不属于解析错误，必须立即结束分类并由外层返回中等难度、无工具的保守结果。400 正文不读取，其他异常不记录服务商正文或请求参数。

重复调用预算绑定当前 generation、工具名和规范化 JSON 参数摘要。JSON 对象键顺序不改变摘要；参数实质不同则不是同一指纹。K-08 的失败/不确定结果重放规则仍优先：相同失败指纹不得重放，`result_unknown` 或 `partial_success` 会阻止本任务继续调用整个同名工具。`max_tool_rounds` 和 `max_agent_steps` 仍限制总工作量。

固定管理指令接受可选 `/`、`!`、`！` 前缀，名称为 `设置工具进度`、`设置调用进度`、`设置工具提示`，值只接受 `开/关/1/0`。它只改变用户可见的前置话术和“正在执行”提示；确认消息、工具结果、最终回复、失败反馈和后台审计不受影响。

## 验证计划与恢复点

定向测试覆盖分类超时只发一次请求、400 正文不可读且不泄漏、解析错误有限重试、相同参数限次、不同参数继续、并行入口和管理命令权限；七七侧只运行面包批量更新与排序测试。完整门禁将串行执行 Python 3.10～3.13 普通全量、Ruff、CI 格式目标、Pyright、仓库/文档/依赖/协议检查、mandatory root sandbox、fresh wheel/sdist、Twine 及 Python 3.10/3.12 的 wheel/sdist 包外加载。

实现提交 `2b87cdf410b3c77792b5d8c9d37ab11b379d72c8` 已通过精确 push 与 pull_request 双 Actions，并已写入安装页作为用户自行安装的恢复点。本文件所在证据提交也必须重新通过两类 Actions；为避免无限自指，不再创建第三个提交记录该证据提交自身的 run ID，最终交付直接报告核验结果。两阶段均不合并 PR、不发布 PyPI、不修改七七 `pyproject.toml`、`uv.lock`、`.venv` 或进程，不执行最终安装命令。

## 2026-09-01 本地门禁证据

- 分类、工具循环和固定管理命令定向回归为 `92 passed`；其中连续两次 HTTP 400 的假响应同时拒绝 `json()`/`text()`，证明两次正文均未读取；
- 七七既有面包批量排行测试为 `6 passed`，`bread.py`、`bread_repository.py` 和测试文件的 SHA-256 在执行前后不变，本阶段没有继续修改宿主源码；
- Python 3.10.20、3.11.15、3.12.13、3.13.13 串行普通全量均为 `3102 passed, 1 skipped`；唯一 skip 是单独执行的 root sandbox 文件；
- mandatory root sandbox 为 `41 passed`，JUnit 复核 `tests=41, skipped=0`；
- Ruff 全仓、CI 格式目标、CI 固定 Pyright 边界、`pip check`、diff whitespace、仓库/文档/依赖/协议资源检查均通过；证据文档树复核为 JSON/TOML/Python 示例 `11/8/9`、21 个文档中的本地链接 `141`、运行/开发依赖 `12/10`、协议动作/策略/封装 `244/244/3`；
- fresh wheel/sdist、Twine 和包内容检查通过；临时 wheel/sdist SHA-256 分别为 `fbba65316c627c17dc830b8dca3acbd427ee633c9d05bf19286a5ec0c51f77e0` / `bdbdb8c6993461f81ba36c775559b0fed5043a7d7e02949314b114baed477ae2`，只作本地树证据，不冒充 GitHub artifact 或发布制品；
- Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载均从独立 site-packages 导入 0.26.4，验证 v11/v12 消息门面、38/31/175 协议清单和 generation 1 reload。

以上本地证据本身不证明 GitHub Actions、七七安装或线上模型/QQ 行为；可恢复安装身份必须继续使用下节已经过远端门禁的实现提交，不能使用脏工作树或临时制品。

## 实现提交远程证据与恢复点

实现提交 `2b87cdf410b3c77792b5d8c9d37ab11b379d72c8` 只推送到本仓库 `origin/feat/generated-tool-bundles`：

- push run [`33485504350`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33485504350) 为 12/12 `completed/success`，`non_success=[]`，恰好一个成功 `release-gate`；
- pull_request run [`33485508930`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33485508930) 为 12/12 `completed/success`，`non_success=[]`，恰好一个成功 `release-gate`；
- 核验时本地 HEAD、remote-tracking、`git ls-remote` 与 PR #5 head 四方一致；PR 为 `OPEN / MERGEABLE / CLEAN`，没有合并。

因此 0.26.4 的源码安装恢复点固定为上述完整实现 SHA，不是本文件后续的纯文档证据 SHA，也不是移动分支。GitHub 结果仍不证明七七已安装或已完成真实模型/QQ 验收。

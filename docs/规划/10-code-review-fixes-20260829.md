---
title: 10-code-review-fixes-20260829
date: 2026-08-29T00:00:00+00:00
lastmod: 2026-08-29T00:00:00+00:00
---

# K-09 当前基线审查修复（0.26.3）

> 当前状态：核心语义重放已通过第一道远程双门禁；高风险并发与网络安全批次已经完成 0.26.3 本地全门禁，尚待提交并取得精确实现 SHA 的 push/PR 双绿灯。本阶段不合并 PR、不发布 PyPI、不修改或重启七七，也不发送真实 QQ 动作。

## 基线与旧分支事实更正

K-09 只以 `feat/generated-tool-bundles` 的精确基线 `74f3c638adf47dc8ca5567d209cc08e65fda2b04` 开始实施。旧 `fix/analysis-fixes` 没有合并或 cherry-pick 到当前分支，因为它的基线早于 K-08 业务路由和真实执行状态；本轮是按当前架构逐项重放语义。

旧分支文档中“未推送远程”和“本机无 pytest 环境”的表述已经与事实不符：

- 远程 `origin/fix/analysis-fixes` 精确为 `6a25cf8f2da7d412c512ec445747040bb61a8062`；
- 它的 push run [`33198457610`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33198457610) 为 `completed/failure`；制品、包外 smoke 和 mandatory root sandbox 成功，但 Repository quality gates、Python 3.10～3.13 四个 test job 及 `release-gate` 失败；
- 直接原因是 Ruff 0.16.2 报出 5 项错误（3 项 import 问题、1 项未使用导入、1 项复合 assert）。四个 Python job 均在 Ruff 步骤停止，因此不能写成“CI pytest 失败”，实际是 pytest 没有在该 run 中执行；
- 2026-08-29 使用现有 Python 3.12 测试环境对旧分支新增的 8 组定向回归补测为 `57 passed`，但这不替代它没有通过远程全门禁的事实。

## 依赖顺序与状态

| 节点 | 依赖 | 状态 | 交付语义 |
| --- | --- | --- | --- |
| K-09A 核心 11 项语义重放 | K-08 | 已实现并通过双门禁 | 个人上下文键、有界 Store 成员检查、SSE、总结兜底、取消、重试通知、超时、成员缓存、`ai` 边界、Tavily TLS/占位密钥、布尔配置与未知键 |
| K-09B PostgreSQL 取消 cleanup | K-09A | 已实现，本地定向通过 | transaction factory 与 spool writer 均使用 shield/settle；连续取消不能跳过 rollback/close，cleanup 异常只作类型化附加证据 |
| K-09C 分类 single-flight | K-09B | 已实现，本地定向通过 | 完整 key 同键只构建一次，异键并发；等待者取消不取消共享任务；失败可重试，发布冲突回读精确胜出记录 |
| K-09D 安全 HTTP 门面 | K-09C | 已实现，本地定向通过 | Custom File 仅能使用注入的 `safe_request`；每跳 DNS/公网/allowlist 重验、IP 固定连接、手动重定向、TLS 降级和跨域敏感头防护，共享总时间与大小预算 |
| K-09E AST 与 400 判定 | K-09D | 已实现，本地定向通过 | `NamedExpr`/walrus 不再绕过 process/network/mutating 证据；`safe_request` 的非 GET/HEAD 或动态 method 保守提升为 mutating；400 只匹配结构化 `code/type` 精确值 |
| K-09F 版本、文档和制品 | K-09E | 已实现，本地制品通过 | 版本 0.26.3；Custom File 迁移、架构、排错、依赖、安装、CHANGELOG 与规划状态同步；不增加 migration 或配置项 |
| K-09G 完整本地与远程门禁 | K-09F | 本地已通过，远程待关闭 | Python 3.10～3.13、静态、仓库/文档/依赖、sandbox、制品、包外 v11/v12 加载均通过；实现 SHA 的 push/PR 双 Actions 尚待取得 |

K-09A 实现提交为 `0a9b7c4cf7858926ef0801369d91641cffaadc1c`，只推送到本仓库 `origin/feat/generated-tool-bundles`。它的 push run [`33240925332`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33240925332) 与 PR run [`33240926968`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33240926968) 均全部成功且各有唯一成功 `release-gate`。这两个 run 只放行 K-09A，不得冒充尚未推送的 K-09B～K-09G。

## 安全 HTTP 契约

`safe_request(...) -> SafeHttpResponse` 是隔离 worker 注入的包内可信门面，不是模型可控的通用 connector。工具只能传 URL、有界 method/headers/body，不能传或扩大 `_network_allow`。运行时契约包括：

- 最多 5 次重定向，所有跳共享 15 秒总预算；请求体最多 256 KiB，响应体最多 1 MiB；
- 禁止 URL 凭据、私网/环回/保留/元数据地址、HTTPS 降级和超出 allowlist 的跳转；
- 每一跳重新解析全部地址，任一私网答案即拒绝，并把已验证 IP 直接用于连接，TLS SNI/证书仍绑定原主机名；
- 关闭客户端自动重定向；跨 origin 时移除 Authorization、Cookie、API key 等敏感头；
- 不接受压缩响应，HTTP 行、header 数量/字节、chunk framing 和 Content-Length 全部有界并严格解析。

## 验证与证据边界

定向回归覆盖连续取消、single-flight 同键/异键/等待者取消/失败恢复/冲突发布、DNS 混合答案、IP 固定、301/302/303/307/308、私网重定向、敏感头、HTTPS 降级、重定向环、响应超限、close 等待上限、非法状态/header、walrus 绕过和结构化 400 判定。2026-08-29 的本地证据为：

- Python 3.10、3.11、3.12、3.13 串行普通全量各 `3086 passed, 1 skipped`；skip 是单独执行的 root sandbox 文件；
- mandatory root sandbox 为 `41 passed`，JUnit 复核 `tests=41, skipped=0`；
- Ruff 全仓、CI 格式目标、扩展安全关键模块 Pyright 均通过；仓库检查为 JSON/TOML/Python 示例 `11/8/9`、本地链接 `139`、运行/开发依赖 `12/10`、协议动作/策略/封装 `244/244/3`；
- fresh `nonebot_plugin_moellmchats-0.26.3` wheel/sdist、Twine 和包内容检查通过；本地临时制品不作为发布哈希或远程 artifact 身份；
- Python 3.10/3.12 × wheel/sdist 四组 checkout 外加载均验证 v11/v12 消息门面、38/31/175 协议清单和 generation 1 reload；
- 内置 `_example.py` 从源码 AST 提取后无控制字符或语法 warning，假 `safe_request` 验证 script/style 内容不会泄漏到提取结果。

这些制品只存在于独立临时目录，没有提交本地 `dist/`。最终远程证据只能在 K-09G 的精确实现 SHA 推送后补录。

任何本地 Fake Bot、隔离 worker 或 CI 结果都不证明七七已安装、重载或完成真实 QQ/模型/PostgreSQL/Redis 验收。

## 恢复点

继续时先核对当前工作树、`uv.lock` 仍未跟踪且未改动，再提交 K-09B～K-09G 的实现与候选文档，推送本仓库分支并核对精确 SHA 的 push/pull_request 双门禁。双绿灯前，0.26.3 不得写成可安装候选。剩余设计问题见[11-pending-issues-backlog](./11-pending-issues-backlog.md)。

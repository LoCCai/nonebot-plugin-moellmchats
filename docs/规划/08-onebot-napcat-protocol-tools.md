---
title: 08-onebot-napcat-protocol-tools
date: 2026-08-28T00:00:00+00:00
lastmod: 2026-08-28T09:17:11+00:00
---

# 全量 OneBot / NapCat 协议工具实施状态

## 目标与不变边界

本阶段把 OneBot v11、OneBot v12 和 NapCat 4.18.19 的动作完整收入离线、可审计清单，并只把人工策略允许、当前 Bot 支持、当前调用者有权使用的固定动作交给 LLM。完整收录不等于允许执行；不存在模型可控的任意 `call_api(action, params)`。

实施只修改 `/app/github_git/nonebot-plugin-moellmchats` 的 `feat/generated-tool-bundles` 分支并推送自有 `origin`。不修改 `/app/qi-dev` 的依赖、配置、锁文件、`.venv` 或进程，不发送真实 QQ 动作，不连接真实模型/数据库/Redis，不运行 migration，不合并 PR，不发布 PyPI。用户未跟踪的仓库根 `uv.lock` 必须保留且不提交。

进入本阶段前的基线为 `79d2268930251773cb4e91cdd9b13a9ec36a7d14`。该 0.25 基线的 push run `33134760223` 和 PR run `33134761967` 已完成双门禁；它们是本阶段前置证据，不包含 0.26.0 协议实现。更早文档把 `bbc3963…` 写成“当前候选”的表述已经过期，`bbc3963…` 只保留为 J 阶段历史实现点。

## 依赖顺序与状态

| 节点 | 前置 | 当前状态 | 本阶段验收定义 |
| --- | --- | --- | --- |
| K-01 固定来源与包内清单 | `79d2268…` | 已实现并本地验证 | 38 + 31 + 175 项；来源、数量和摘要固定；新增/缺策略/碰撞 fail closed；普通运行/构建离线 |
| K-02 正式协议工具契约 | K-01 | 已实现并本地验证 | 三类稳定前缀；Builtin 不建 `ToolSource`/migration；`ToolConfirmationMode` 仅允许 canonical 协议封装免确认；Bot Capability 由 Broker 消费 |
| K-03 v11/v12 中立门面 | K-02 | 已实现并本地验证 | 数字/字符串 ID、嵌套 self、datetime、mention/reply/file_id；v11/NapCat/v12 独立 3 秒探测上限、能力快照和缓存隔离 |
| K-04 完整发现、按需展开 | K-03 | 已实现并本地验证 | 当前 Bot/权限短目录；只展开选中动作 Schema；事件目标不可改；业务菜单冲突优先 |
| K-05 权限、确认和 Broker | K-04 | 已实现并本地验证 | 永久拒绝、SUPERUSER、二阶段确认、单次副作用、`result_unknown`、限额、截断、脱敏和审计 |
| K-06 点赞、消息与表情 | K-05 | 已实现并本地验证 | 真 NoneBot Matcher 隔离点赞一次；v11/v12 消息捕获；正文/可选表情分离；v12 无 file_id 跳过 |
| K-07 配置、文档与包 | K-06 | 已实现并完成本地发布门禁 | 0.26.0、四开关、支持/权限/动作总表、模型/调度/插件说明、wheel/sdist 资源与 MIT 归属 |
| K-08 GitHub 交付 | K-07 全部本地门禁 | 原交付已完成；PR #3 后续由外部操作合并 | 原实现 SHA 的 push/PR 各唯一成功 release-gate；本阶段没有执行合并、发布或部署 |

## 固定来源与确定性身份

| 来源 | 固定版本 | 用途 |
| --- | --- | --- |
| OneBot v11 | `d4456ee706f9ada9c2dfde56a2bcfc69752600e4` | `api/public.md`，38 个公开动作 |
| OneBot v12 | `d533f0fca3bd14781d4461776dba8d907d9de253` | `specs/interface/*/actions.md`，31 个标准动作 |
| NoneBot Adapter OneBot | `3ac943fc4470d851219f368cacadf3dcdd649ee7`，2.4.6 | v11/v12 兼容参考 |
| NapCatDocs | `14ad6896579abf17c761cdf8d9dfb7c3ea396305`，4.18.19 | `src/api/4.18.19/openapi.json`，175 个动作 |

NapCat OpenAPI SHA-256 为 `905ff1faa265cdfa6401a91e8ed832ab15c9e32a7683c42dc11bb6752682ae39`。规范化动作、人工动作策略和三个安全封装摘要分别为：

- `674fe5b28be905192dfda36cf0952f27c548561b6aa8981d840e5ef52826bf7e`
- `e16b43d3a5ab2abe63248ebbc2245b04e43121403b66e46ece96043fb8901526`
- `0d4cb894677c5b03f6c88a9f3bfe8421474a529f2a654c4087a39fc8f55fc475`

离线生成器同时维护[244 项动作总表](../protocol-actions.md)。技术清单与人工策略分开；生成器不会为新动作自动创建 `reviewed=true` 策略。

## 已实现安全语义

- 工具名固定为 `onebot_v11__*`、`onebot_v12__*`、`napcat_v11__*`；特殊字符确定性转义，生成期和运行时检查碰撞。
- 244 项策略中，129 项为 SUPERUSER、20 项为普通用户、95 项永久拒绝。NapCat 与标准 v11 同名动作按当前实现去重后，构造 121 个开放协议动作和 3 个固定目标安全封装；连同 `web_search` 共 125 个 Builtin。
- `qq__like_me`、当前上下文戳一戳和当前消息表情回应只接受模型公开参数；用户、群、消息目标来自当前事件。低风险直执可配置关闭。
- 全局读取、列表和历史仅 `SUPERUSER` 可见；消息、请求、群管理和账号修改均为 `SUPERUSER + 二阶段确认`。v12 不使用群管理员角色授权。
- Cookies/CSRF/credentials/clientkey/rkey、原始发包、隐藏 quick operation、事件拉取、退出/重启、任意路径/URL/Base64/文件动作，以及没有可靠单目标上限的 NapCat `set_group_kick_members` 永久拒绝；批量踢人改用单人 `set_group_kick`。
- 确认绑定 Bot、Adapter、协议、实现/版本、支持动作摘要、调用者、会话、generation、动作、参数和策略摘要；执行前重新探测并校验。
- 所有开放动作均有原子有界限额，规范化参数 JSON 总量硬限制为 64 KiB。副作用只调用一次；超时、断连或取消导致响应不确定时返回 `result_unknown`、保留额度并禁止自动重试。
- 结果递归截断并脱敏凭证、Authorization、Cookie、路径和大块二进制；审计只保存身份、状态和摘要。

## 本地证据

提交前工作树已取得以下本地证据：

- 固定 checkout 生成和 `--check` 均通过，数量为 38 / 31 / 175；
- 协议核心 43 项、K 阶段关联联合 200 项通过，覆盖来源/摘要/拒绝项、v11/NapCat/v12、普通用户/超管、场景/回复、缓存身份、越权参数、64 KiB 参数总量、确认过期/漂移、并发限额、`result_unknown`、脱敏、真实 Matcher 点赞、v12 捕获和可选表情跳过；
- Python 3.10.20、3.11.15、3.12.13 和 3.13.13 严格串行普通全量均为 `2935 passed, 1 skipped`；唯一 skip 是 root 环境下由 mandatory sandbox 套件另行覆盖的既有条件分支，不属于新增协议测试；
- mandatory root sandbox 为 `41 passed, 0 skipped, 0 failures, 0 errors`，JUnit 已验证确实执行；
- Ruff 0.16.2 全源码、脚本和测试通过；14 个协议门禁目标文件格式检查通过；Pyright 1.1.411 协议/门面/生成及检查目标为 `0 errors, 0 warnings`；
- 文档检查解析 11 个 JSON、8 个 TOML、8 个面向用户的 Python 示例，并验证 19 个文件中的 116 个本地链接/锚点；依赖清单核对 12 个运行依赖和 10 个开发依赖；
- fresh wheel/sdist 通过 Twine、校验和和包内容门禁；wheel SHA-256 为 `a085a5929fef95ed3eba8d7f76cb49eb78fab5c3ea51f24e6f5e975f94263938`，sdist 为 `70dcd47113b0b6ee32855adeeb1798f3d1f3d82742729ead1cb0f31cca891097`；两者均含 244 项动作、244 项策略、3 个安全封装、固定来源和完整 MIT NOTICE，且不含 `uv.lock`、cache 或 bytecode；
- Python 3.10 × wheel/sdist 使用 NoneBot 2.5.0、Python 3.12 × wheel/sdist 使用最低 NoneBot 2.4.4，四组均在仓库外同时加载 v11/v12、验证 38 / 31 / 175 清单并发布 runtime generation 1。

这些只证明本地隔离实现和制品可加载，不证明七七已经安装或发送过真实 QQ 动作。实现提交和 CI 启动方式修复均已推送自有 `origin`，实现 SHA 的 push/PR 双门禁已经关闭；本文件所在提交只负责固化证据，不改变运行时实现。

## GitHub 交付与恢复点

0.26.0 协议实现提交为 `1bc42a0ac7ff6e006c9ba71367c6fe4fc9f0c84f`。它的 push run `33147881907` 和 PR run `33147883585` 均因普通矩阵使用裸 `pytest`、无法从仓库根导入 `scripts.generate_protocol_manifests` 而失败；其余质量、构建、sandbox 和四组包外安装 job 已成功，这两条失败 run 不作为放行证据。

最小 CI 修复提交为 `cf6946881de79cd6501792d9d70c62f253d8499f`，只把普通矩阵入口从 `pytest` 改为 `python -m pytest`。同一干净 Python 3.12 环境已复现前者因 `ModuleNotFoundError` 退出、后者取得 `2935 passed, 1 skipped`。推送后已核对本地 HEAD、`origin/feat/generated-tool-bundles`、`git ls-remote` 和 PR #3 head 均为该 SHA。

该实现 SHA 的合格远端证据为：

- push run [`33148454431`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33148454431)：12/12 job 为 `completed/success`，唯一 `release-gate` job `98774828064` 成功；
- pull_request run [`33148456661`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33148456661)：12/12 job 为 `completed/success`，唯一 `release-gate` job `98774850043` 成功；
- PR [#3](https://github.com/LoCCai/nonebot-plugin-moellmchats/pull/3)：完成上述门禁核验时为 `OPEN`、非 Draft、head 为该实现 SHA 且 `mergeStateStatus=CLEAN`；随后已由本任务之外的操作合并。

### 0.26.0 后续修复与当前安装点

后续实现提交 `20cfe44576a3f6f8dbf1bd5a330407a936fe481a` 在 0.26.0 上增加固定 `SUPERUSER` 冷却命令（允许 `0`～`86400`，`0` 关闭冷却），并为工具生成模型的非内容安全 HTTP 400 增加一次最小参数兼容重试和有界脱敏错误摘要。该提交没有改变 244 项协议清单或权限策略。

- Python 3.10.20、3.11.15、3.12.13（含 NoneBot 2.4.4）和 3.13.13 普通全量均为 `2962 passed, 1 skipped`；mandatory root sandbox 为 `41 passed, 0 skipped`；
- Ruff、触及模块 Pyright、仓库文档/依赖/协议资源检查、Twine 和包内容检查通过；final wheel/sdist SHA-256 分别为 `d812b13eeba572daa50d2199977f2e3ae41e6e591765f9c8e34a47f05c08836f` / `72ae560dbd2b66735107cb8008a3f502f378198f51b6d9ba14e1d548325147ca`；
- Python 3.10/3.12 × wheel/sdist 四组包外加载均成功，验证版本 0.26.0、冷却参数解析、v11/v12 消息门面、38 / 31 / 175 清单和 runtime generation 1；
- push run [`33155319608`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33155319608) 为 12/12 `completed/success`，唯一 `release-gate` job `98796777215` 成功；本地 HEAD、`origin` 与 `ls-remote` 已核对为该 SHA。

PR #3 在 `20cfe44…` 推送前已经合并并固定在旧 head `348293c…`，因此该后续提交没有 pull_request run；不得把 #3 的历史 PR run 误绑到它。当前 Git 隔离测试安装应固定 `20cfe44576a3f6f8dbf1bd5a330407a936fe481a`；`79d2268…` 只保留为 0.25.0 回退点。若发布规则要求新提交具备 PR 门禁，必须另开 PR 并等待新的 pull_request CI。

文档提交无法在自身内容中写入自己的 SHA 或由自身触发的 run ID，否则会形成无穷自引用；其身份应由 Git 历史、远端引用和对应 Actions 共同绑定。PR #3 已合并后产生的文档修订只会触发 push run，不得被描述成新的 PR 双门禁。每个被验收的 run 仍必须全部 job 成功，且恰好一个 `release-gate` 为 `completed/success`；不 promotion、不发布 PyPI。

恢复时先执行只读核查：

1. `git status --short --branch`，确认仍在 `feat/generated-tool-bundles`；
2. 保留未跟踪 `uv.lock`，不得提交、修改或删除；
3. 核对本地 HEAD、`origin/feat/generated-tool-bundles` 和 `git ls-remote`；PR #3 已合并，不能再作为当前分支 head 依据；
4. K-07 协议实现 SHA `cf69468…` 的历史远端双门禁已完成；当前 0.26.0 安装点为后续修复 `20cfe44…`；
5. `20cfe44…` 的 push 门禁已完成，但没有 PR run；若发布规则要求双门禁，应另开 PR，不得复用 #3 的结果；
6. 不检查或修改七七依赖/进程，除非用户另行明确授权新的部署阶段。

---
title: 07-intent-discovery-onebot-reliability
date: 2026-08-27T00:00:00+00:00
lastmod: 2026-08-28T00:00:00+00:00
---

# 功能级意图发现与 OneBot 投递可靠性

> 状态更正（2026-08-28）：`bbc3963…` 是本阶段最初实现点，不再是当前候选。后续表情降级修复已收口到 `79d2268930251773cb4e91cdd9b13a9ec36a7d14`，其 push/PR run `33134760223` / `33134761967` 已完成双门禁。0.26.0 的全量协议工作另见 [K 阶段实施状态](./08-onebot-napcat-protocol-tools.md)；以下 `bbc3963…` 记录保留为历史证据，不应用作新协议功能的安装 SHA。

## 目标与边界

本阶段解决两个隔离测试暴露的问题：分类目录只有插件级泛化描述，导致“给我点个赞”未召回 `qi_group_admin`；正文成功后附加表情遇到 NapCat `ActionFailed(retcode=1200)`，导致整轮 Matcher 被判失败。

目标链路是：

```text
完整但有界的功能目录
  → 分类模型选择插件标识
  → 只展开命中插件的详细 Schema/菜单用法
  → ToolSpec handler 或目标插件 Matcher
  → OneBot V11 / NapCat
```

菜单只作 discovery hint，不获得 handler、凭据、权限或通用 OneBot API。点赞必须继续进入 `qi_group_admin` 的现有 Matcher，由该插件复核每日次数、黑名单和业务状态；不得向模型开放任意 `bot.call_api`。

本阶段只修改 `/app/github_git/nonebot-plugin-moellmchats` 开发仓库并执行离线/模拟测试。不修改 `/app/qi-dev` 的依赖、锁文件、配置、数据或源码，不重载七七，不发真实 QQ 消息，不连接真实模型、PostgreSQL 或 Redis，不运行 migration，不发布或部署。

## 依赖顺序与状态

| 节点 | 前置 | 状态 | 验收定义 |
| --- | --- | --- | --- |
| J-01 菜单规范化契约 | `7705cdd…` 基线 | 已完成（`bbc3963…`） | 清理 `<ft>`/控制字符，拒绝错误类型；功能、触发、字段和总字符有界；固化进 `ToolSnapshot` |
| J-02 权威目录桥接 | J-01 | 已完成（`bbc3963…`） | 显式覆写 > PicMenu 内存目录 > Metadata menu > 普通 Metadata；未加载插件不能因菜单变成工具 |
| J-03 两阶段展开 | J-02 | 已完成（`bbc3963…`） | 分类目录按功能展开且返回插件 ID；命中后兼容 Schema 包含精确消息入口；Provider/legacy parity 保持一致 |
| J-04 OneBot 表情降级 | J-01 | `bbc3963…` 已完成；三类适配器失败本地补齐 | 正文失败传播；正文已成功后单个表情的 `ActionFailed`/`NetworkError`/`ApiNotAvailable` 隔离且不重试正文；无成功投递时仍传播 |
| J-05 文档、依赖与本地矩阵 | J-01～J-04 | 已完成（`bbc3963…` 双门禁） | 用户文档、架构、依赖、定向/相关/普通全量、Ruff、mandatory root sandbox 与包制品均已核验 |
| J-06 七七隔离升级与真实行为 | J-05 + 精确 SHA/制品 | 只读前置完成；升级/重载未执行 | 依赖固定到 `bbc3963…`，人工重载测试实例后验证分类、Matcher、OneBot |
| J-07 发布/生产 | J-06 + 独立发布计划 | 锁定 | 合并、正式制品、发布观察与回退全部另行授权；本阶段不得推进 |

## 已实现契约

### 目录来源

- `custom_plugin_info.json` 是完整显式覆写；存在条目时不再拼接自动菜单。
- PicMenu Next 已加载时，只读取它已经安装到内存的公开目录。七七的该目录由 QWeb Feature Catalog 校验和物化；通用插件不读取七七路径，也不新增 PicMenu/QWeb 依赖。
- PicMenu 不存在或内存尚未就绪时，读取 `PluginMetadata.extra["menu_data"]`。
- 两者都没有时保持原有 Metadata 名称、描述和用法。

### 有界发现与执行分离

- 每插件最多 128 个功能、每功能最多 16 个触发入口、每插件最多 48,000 字符；最终分类目录最多 96,000 字符，超限会明确截断而不是继续膨胀 prompt。
- 分类目录一功能一行，但第一列始终是可执行插件标识；同一插件被选中一次即可。
- 被动事件/定时功能只帮助理解范围，详细 Schema 明确禁止通过合成 `command` 伪造。
- 兼容插件仍保守标为 `mutating`，仍通过有界目标 Matcher 投递；强类型参数、权限、副作用和二阶段确认必须使用 `ToolSpec`。
- 分类 prompt 改动使用新的 `categorize-json-v2-menu-discovery` 策略版本，避免旧成功缓存跨策略复用。

### OneBot 不确定发送

- 正文发送失败不吞异常，调用方可以正确标记失败。
- 正文或前一个附件已成功后，附加表情的 `ActionFailed`、`NetworkError` 或 `ApiNotAvailable` 只跳过当前表情并记录无正文内容的 warning。
- 不自动重试 NapCat 超时，因为 `retcode=1200` 不能证明 QQ 端一定未投递，重试可能产生重复消息。
- 返回并保存的是去掉表情标记后的正文；工具总结重试分支也使用该返回值。

## 全部相关接口验收范围

| 接口 | 本链路职责 | 验收边界 |
| --- | --- | --- |
| `Bot.send(event, message)` | MoEllmChats 投递正文和可选表情，由适配器按事件选择消息动作 | 正文是必需投递；可选表情只在已有成功投递后降级 |
| `send_msg` | NoneBot OneBot V11 通用消息动作 | 兼容 Matcher 输出可被有界捕获 |
| `send_group_msg` | 群消息动作 | 兼容 Matcher 输出可被有界捕获，reply ID 仍绑定原事件 |
| `send_private_msg` | 私聊消息动作 | 兼容 Matcher 输出可被有界捕获 |
| `send_like` | `qi_group_admin` 点赞副作用 | 不作为消息输出捕获，必须真实进入原 Matcher 的次数、黑名单和业务检查 |
| `ActionFailed` | OneBot/NapCat 返回动作失败，例如 `retcode=1200` | 正文阶段原样传播；已有正文后仅降级可选表情 |
| `NetworkError` | WebSocket/HTTP 超时、断连或异常响应 | 与动作失败使用相同的不重复正文规则 |
| `ApiNotAvailable` | API 连接在两次发送之间失效 | 与动作失败使用相同的不重复正文规则 |

不在本次范围内的踢人、禁言、撤回、群管理和其他 NapCat 扩展 API 不会因“全部相关接口”而开放给模型；新增副作用仍应使用明确的业务 Matcher 或 `ToolSpec`，禁止通用 `bot.call_api`。

## 当前证据与恢复点

- 开发分支：`feat/generated-tool-bundles`。
- 本阶段开始时基线 HEAD：`7705cdd46e8dffd29ee50440fcf8ede94e76dd7d`。
- 本阶段实现提交：`bbc3963a361259f4d98c29003937afb1cbe976f9`。
- 新增实现：`tool_discovery.py`、`test_tool_discovery.py`、`test_moe_llm.py`；并修改分类、工具管理、Provider parity、兼容 ToolSpec 和发送路径。
- 文档机器核验通过：98 个本地链接/标题身份有效，5 个严格 JSON、8 个 TOML、8 个 Python 示例可解析或编译，58/58 个 `DEFAULT_CONFIG` 字段和 12/12 条直接运行依赖均有说明。源码顶层 import 与 `pyproject.toml` 交叉审计未发现漏声明包；PicMenu/QWeb 仍是可选内存发现源，不新增安装依赖。
- 最后一次格式化后的受影响回归在 Python 3.10、3.11、3.12、3.13 各为 `124 + 40 + 9 = 173 passed`；其中仅 `ModelSelector` 9 项因当前 Codex 沙箱无法唤醒跨线程 selector，显式加载 checkout 外的临时 polling harness。Python 3.12 的 Classification/Tool Catalog/Tool Schema cache 另有 `226 passed`；权限过滤、Provider/legacy parity、点赞目录与 OneBot 表情降级均包含在上述集合中。
- 实现完成后的普通全量在同一临时 harness 下为 `2870 passed, 1 skipped, 15 deselected`。15 个被排除用例及另行定义的 41 个 mandatory sandbox 用例需要真实 `setuid`/`chown`/namespace/socket 等 root 能力；当前外层沙箱虽显示 euid 0，但禁止这些能力，所以新工作树的强隔离门禁必须由具备真实能力的 CI/主机关闭，不能用 harness 冒充。
- 精确实现 SHA 的 GitHub push run `33066587717` 与 PR run `33080256433` 各有 11 个 job、全部 `completed/success`、`non_success=[]`，并各恰好一个成功 `release-gate`。两者均覆盖 Mandatory root sandbox、Python 3.10～3.13、单次 wheel/sdist 构建，以及 Python 3.10/3.12 × wheel/sdist 四组包外 smoke；因此本地受限沙箱留下的强隔离远端门禁已经关闭。
- Ruff 0.16.2 目标 lint 与 `git diff --check` 通过。fresh wheel/sdist 构建和 Twine 检查通过；两种制品都在 checkout 外安装、加载并完成 `generation=1` 原子重载 smoke。临时目录 `/tmp/moellm-j05-dist.q7PiJq` 中 wheel/sdist SHA-256 分别为 `d768e3d6f9c3495fc35c862a5f01cc8e790f680d21bf3bb9a705f71ad794e35a` / `80a75157db7c162fd766d66d1776809571b18799e5f886f67cc38b66c8a301e9`；它们来自脏工作树，不绑定提交，不能作为安装或发布制品。
- 用户原有未跟踪 `uv.lock` 必须继续保留且不得修改、删除或纳入提交。
- PR #2 已于 2026-08-26 合并到本仓库自己的 `feat/llm-runtime-backpressure` 集成分支，merge commit 为 `c78ef06190d2df1d77c2ada6d9f06020ef6b37ca`；本轮 PR #3 以该分支为 base，head 精确为 `bbc3963…`，当前 `OPEN / CLEAN`。上游和默认 `master` 都不是本轮集成 base。
- `bbc3963…` 已可作为七七隔离升级的固定源码 SHA，但这不证明七七已经安装、重载或完成真实 QQ 验收。
- J-05 证据提交 `45f7a6e6d5d1017fd8f3d9dc4a65ed497a2862b9` 已推到本仓库的 `feat/generated-tool-bundles`。其 push run `33081113984` 与 PR run `33081119792` 也各为 11/11 success、`non_success=[]`，并各恰好一个成功 `release-gate`；因此证据回填自身的远端门禁已经关闭。
- J-06 只读核查显示：七七工作区的声明仍引用本仓库可移动分支 `feat/generated-tool-bundles`，`uv.lock` 与已安装 `direct_url.json` 则同为 `0.25.0 @ 8e7f0547e72bb67bbdfbda937c7873b235e971e7`。`8e7f054…` 是 `bbc3963…` 的祖先，中间相隔 180 个提交；这不是上游来源，也不能把可移动声明等同于已安装分支头。
- 临时目录 `/tmp/moellm-j06-lock.ULPyaQ` 中的独立演练把依赖声明固定到 `bbc3963…`，`uv lock --check` 通过且仍解析 186 个包；锁差异将插件从 `8e7f054…` 前移到 `bbc3963…`，并补齐该版本声明的 Alembic、asyncpg、Redis、SQLAlchemy 依赖边，这些包已经是七七项目的直接依赖。使用七七现有 Python 3.12 环境、临时 LocalStore、禁网钩子和 `DRIVER=~none` 加载候选源码成功，“给我点个赞”菜单样例被规范化为可调用功能。
- 上述演练没有修改 `/app/qi-dev/pyproject.toml`、`uv.lock`、现有 `.venv`、配置或进程，也没有启动 driver、连接模型/数据库/Redis、发送 QQ 请求或操作生产。

恢复时先只读检查插件仓库 HEAD、PR #3、远端 run，以及七七依赖声明、锁 SHA、已安装 `direct_url.json` 三者是否仍一致；保留插件仓库中用户未跟踪的 `uv.lock`。J-06 下一步是把七七隔离测试依赖固定到 `bbc3963…`、重新锁定并核对实际安装元数据；随后由人工控制只重载测试实例，再验证分类、Matcher 与 OneBot。未取得具体运行时授权前不得修改七七工作树或进程，也不得跳到 J-07 发布/生产。

---
title: 07-intent-discovery-onebot-reliability
date: 2026-08-27T00:00:00+00:00
lastmod: 2026-08-27T00:00:00+00:00
---

# 功能级意图发现与 OneBot 投递可靠性

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
| J-01 菜单规范化契约 | `7705cdd…` 基线 | 已完成（本地工作树） | 清理 `<ft>`/控制字符，拒绝错误类型；功能、触发、字段和总字符有界；固化进 `ToolSnapshot` |
| J-02 权威目录桥接 | J-01 | 已完成（本地工作树） | 显式覆写 > PicMenu 内存目录 > Metadata menu > 普通 Metadata；未加载插件不能因菜单变成工具 |
| J-03 两阶段展开 | J-02 | 已完成（本地工作树） | 分类目录按功能展开且返回插件 ID；命中后兼容 Schema 包含精确消息入口；Provider/legacy parity 保持一致 |
| J-04 OneBot 表情降级 | J-01 | 已完成（本地工作树） | 正文失败传播；正文已成功后单个表情 `ActionFailed` 隔离且不重试正文；无成功投递时仍传播 |
| J-05 文档、依赖与本地矩阵 | J-01～J-04 | 本地完成；强隔离远端门禁待关闭 | 用户文档、架构、依赖、定向/相关/普通全量、Ruff 与包制品已核验；当前外层沙箱不能替代 mandatory root sandbox |
| J-06 七七隔离升级与真实行为 | J-05 强隔离门禁 + 新精确 SHA/制品 | 锁定 | 依赖固定到通过门禁的新 SHA，人工重载测试实例后验证分类、Matcher、OneBot；当前未授权执行 |
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
- 正文或前一个附件已成功后，附加表情的 `ActionFailed` 只跳过当前表情并记录无正文内容的 warning。
- 不自动重试 NapCat 超时，因为 `retcode=1200` 不能证明 QQ 端一定未投递，重试可能产生重复消息。
- 返回并保存的是去掉表情标记后的正文；工具总结重试分支也使用该返回值。

## 当前证据与恢复点

- 开发分支：`feat/generated-tool-bundles`。
- 本阶段开始时基线 HEAD：`7705cdd46e8dffd29ee50440fcf8ede94e76dd7d`。
- 新增实现：`tool_discovery.py`、`test_tool_discovery.py`、`test_moe_llm.py`；并修改分类、工具管理、Provider parity、兼容 ToolSpec 和发送路径。
- 文档机器核验通过：98 个本地链接/标题身份有效，5 个严格 JSON、8 个 TOML、8 个 Python 示例可解析或编译，58/58 个 `DEFAULT_CONFIG` 字段和 12/12 条直接运行依赖均有说明。源码顶层 import 与 `pyproject.toml` 交叉审计未发现漏声明包；PicMenu/QWeb 仍是可选内存发现源，不新增安装依赖。
- 最后一次格式化后的受影响回归在 Python 3.10、3.11、3.12、3.13 各为 `124 + 40 + 9 = 173 passed`；其中仅 `ModelSelector` 9 项因当前 Codex 沙箱无法唤醒跨线程 selector，显式加载 checkout 外的临时 polling harness。Python 3.12 的 Classification/Tool Catalog/Tool Schema cache 另有 `226 passed`；权限过滤、Provider/legacy parity、点赞目录与 OneBot 表情降级均包含在上述集合中。
- 实现完成后的普通全量在同一临时 harness 下为 `2870 passed, 1 skipped, 15 deselected`。15 个被排除用例及另行定义的 41 个 mandatory sandbox 用例需要真实 `setuid`/`chown`/namespace/socket 等 root 能力；当前外层沙箱虽显示 euid 0，但禁止这些能力，所以新工作树的强隔离门禁必须由具备真实能力的 CI/主机关闭，不能用 harness 冒充。
- Ruff 0.16.2 目标 lint 与 `git diff --check` 通过。fresh wheel/sdist 构建和 Twine 检查通过；两种制品都在 checkout 外安装、加载并完成 `generation=1` 原子重载 smoke。临时目录 `/tmp/moellm-j05-dist.q7PiJq` 中 wheel/sdist SHA-256 分别为 `d768e3d6f9c3495fc35c862a5f01cc8e790f680d21bf3bb9a705f71ad794e35a` / `80a75157db7c162fd766d66d1776809571b18799e5f886f67cc38b66c8a301e9`；它们来自脏工作树，不绑定提交，不能作为安装或发布制品。
- 用户原有未跟踪 `uv.lock` 必须继续保留且不得修改、删除或纳入提交。
- 当前增量尚无不可变提交 SHA、远端 CI 或发布制品；`7705cdd…` 不包含本阶段实现，七七当前安装版本也不能据此宣称已经修复。

恢复时先只读检查 `git status --short` 和 HEAD，保留既有文档改动与 `uv.lock`；从具备真实 root 能力的 mandatory sandbox 门禁、新精确提交与远端双 `release-gate` 继续。三者未关闭前不得推进七七依赖变更或真实 QQ 验收。

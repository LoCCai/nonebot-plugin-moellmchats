---
title: 15-tool-schema-command-recovery-20260901
date: 2026-09-01T00:00:00+00:00
lastmod: 2026-09-01T00:00:00+00:00
---

# K-13 指令投递与工具作用域真实性（0.26.6）

> 当前状态：以 `feat/generated-tool-bundles` 的未提交 0.26.6/K-12 工作树为依赖基线，K-13 源码、PicStatus 双副本、QWeb Feature Catalog、四版本、静态、sandbox 和制品本地门禁均已通过。尚未提交、推送、安装、重启或做真实模型/QQ 验收。最后一个远端已验证恢复点仍是 0.26.5 `e704092a1e8d9ad215e4e9de35a9fe403483d56f`。

## 现场证据与根因

2026-09-01 22:52 的请求“用状态模块看一下我们的宽带近一天的状态图”已经被分类为 `nonebot_plugin_picstatus_ng`，但难度为简单。主模型连续构造三条以 `/状态` 开头的不同命令，兼容调度均为 `not_matched`；22:55 改用 `/zt` 后，同一 Matcher 立即得到 `matched_with_output`。因此第一层问题不是 PicStatus 未加载，而是菜单入口、命令合成模型与真实 Matcher 别名不一致。

22:56 的另一个请求只分类开放了 `qi_toolbox` 和 `web_search`，主模型却连续生成 `qi_db_analytics` 调用。旧执行器从 generation 全局目录解析工具，只要插件在全局快照中存在就继续信任、进度和 Matcher，没有核对它是否真的出现在本轮模型收到的 `tools` 字段。三次错调因此进入数据库插件并得到 `matched_empty`。两份现存 `model_config.json` 的 `resident_plugins` 都为空，所以这不是“常驻插件太多”，而是执行作用域缺口。

旧进度标题只提取 NoneBot command 首词，`/zt 拓扑 全部`、`/zt 节点 ...` 等不同操作都会显示成相同的 `zt`。用户能看到插件被重复投递，却无法区分每次究竟查询什么。

最后，旧 observation 虽然告诉模型“未命中”，却没有重新给出同一插件的完整真实菜单，也没有明确要求先查帮助/列表/拓扑。因此模型容易继续猜参数，或凭上下文记忆转去另一个全局插件。

## 依赖顺序与状态

| 节点 | 依赖 | 当前状态 | 契约 |
| --- | --- | --- | --- |
| K-13A 完整可显示指令 | K-11 | 本地已实现并通过定向测试 | NoneBot 进度显示规范化、截断和脱敏后的完整 command；不显示原始 JSON、凭据、URL、路径、长数字 ID 或 Base64 |
| K-13B 请求级 Schema 强制许可 | K-13A、K-12 generation | 本地已实现并通过定向测试 | 工具名、说明与当前 generation 绑定；串行、并行和超额调用均在进度/Matcher 前复核；Schema 外调用记录 `schema_scope_rejected` |
| K-13C 同插件菜单恢复 | K-13B、K-08 真实状态 | 本地已实现并通过定向测试 | `not_matched` / `matched_empty` 重新附上本轮授权插件的真实菜单，先发现候选再精确查询，不开放其他插件 |
| K-13D 难度与 PicStatus 所有者 | K-13C | 本地已实现并通过定向测试 | NoneBot 自由 command 合成最低中等难度；PicStatus 增加拓扑/节点/宽带意图，规范入口只保留 `运行状态`、`zt`、`yxzt`、`status` |
| K-13E QWeb 菜单与门禁 | K-13D | 本地已完成，待提交/远端门禁 | QWeb 目录版本 `2026.09.01.2`；未导入 CMS，不修改数据库；完整本地门禁已通过 |

## 运行契约

每次构造主模型 payload 时，运行时从最终 `tools` 数组提取精确工具名和说明，并绑定该请求所持的 immutable generation。任何工具调用依次经过：

```text
模型 tool_call
  → 当前 generation 与本轮 Tool Schema 校验
  → 参数 Schema、权限、信任、确认和重复检查
  → 有界进度提示
  → 对应 handler / Matcher
  → 类型化结果和审计
```

Schema 校验失败时不会发送“正在调用”，不会进入 `resolve_llm_tool_execution` 后续执行，也不会让模型通过猜测名称访问全局目录。拒绝 observation 只允许模型使用本轮 `tools` 字段列出的工具。

NoneBot 插件返回未命中或空命中时，运行时从本轮 Schema 中取回同插件说明；如果含“菜单功能提示”，只保留真实菜单段，并做 4000 字有界截断。恢复提示要求先使用帮助、列表、拓扑、全部等发现入口，再根据候选构造实质不同 command。真正的失败、超时、部分成功和结果不确定仍遵守 K-08/K-11 的重试边界，不会因菜单恢复而放宽副作用重试。

## PicStatus 与 QWeb 同步

PicStatus 仓库 `/app/github_git/nonebot-plugin-picstatus-ng` 和七七源码 `/app/qi-dev/src/plugins/nonebot_plugin_picstatus_ng` 的 `commands.py` 保持字节一致。默认 `ps_command` 删除过于宽泛的中文别名“状态”，只保留：

```text
运行状态 / zt / yxzt / status
```

帮助、README、`PluginMetadata.extra.menu_data`、`pmn_triggers` 和 `pmn_llm_intents` 统一以 `zt` 为规范示例。七七 QWeb `plugin_documents.json` 同步四项功能及其拓扑、节点、链路、资源、消息和事故入口，并把目录版本提升为 `2026.09.01.2`。

宿主显式 `PS_COMMAND` 会覆盖插件默认值。七七当前 `.env.prod` 仍检测到旧覆盖中包含“状态”；本阶段不修改运行配置或进程，因此源码就绪不等于该别名已经从七七运行态消失。后续维护窗口必须先移除该非敏感配置值中的旧别名，再由用户按宿主流程重载。

## 本地证据与边界

本地证据（2026-09-01）：

- MoEllmChats 路由、payload、执行、Schema 和菜单联合定向测试 `81 passed`；其中 PicStatus 两条现场话语的唯一意图所有者回归为 `1 passed`；
- PicStatus 命令/菜单定向测试 `22 passed`，QWeb Feature Catalog 定向测试 `22 passed`；
- MoEllmChats Python 3.10、3.11、3.12、3.13 串行普通全量各 `3138 passed, 1 skipped`；3.12 另固定 NoneBot 2.4.4，结果相同；
- PicStatus 全量 `65 passed`；QWeb Feature Catalog 与内置清单联合 `28 passed`；
- 上述修改的 Ruff 与格式检查通过；PicStatus 仓库和七七源码的 `commands.py` SHA-256 均为 `4795745236c1e55b1f58a22138b1f6a9c8190ff6e7ca4f9faa8f2d3362475e73`；
- 回归覆盖完整 `/zt 拓扑 全部` 进度、Schema 外 `qi_db_analytics` 零 dispatch、同插件菜单恢复、难度下限、实际话语所有者和删除默认“状态”别名；
- CI 范围 Ruff/format、Pyright 类型边界、153 个本地文档链接、13 项运行依赖、10 项开发依赖与 244 项协议资源检查通过；mandatory root sandbox `41 passed, 0 skipped`；
- fresh wheel/sdist、Twine 和制品内容检查通过，临时 wheel/sdist SHA-256 分别为 `a8d85a1c2c8024d1fe65ed171abb997ae75fa3468cced80ffb17ce57982026c7` / `fb72ef8c9964a3ce05c41161484335b13849d7c3bdcc322e8d2fe82ef7990542`；Python 3.10/3.12 × wheel/sdist 四组包外加载均发布 generation 1，并验证 v11/v12 门面与 `38/31/175` 协议数量。

这些是隔离源码测试，不代表七七已经重载，不代表真实模型会一次选择理想的发现路径，也不代表 PicStatus/QWeb 线上卡片已验收。本阶段没有安装依赖、修改 `.venv`、重启 Qiqi、导入 CMS、连接生产数据库、调用真实模型或发送 QQ 动作；MoEllmChats 与 PicStatus 均尚未为本轮创建提交或推送。

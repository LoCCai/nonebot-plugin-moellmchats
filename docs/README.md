# nonebot-plugin-moellmchats Wiki

欢迎使用 **nonebot-plugin-moellmchats** 文档中心。这里汇集了插件所有配置项、高级功能与使用技巧的完整说明。

> 当前文档面向 0.26.0 开发候选。进入本阶段前最后一个已完成双远端门禁的 0.25 基线是 `79d2268930251773cb4e91cdd9b13a9ec36a7d14`，push/PR run 为 `33134760223` / `33134761967`；它不包含本页新增的全量协议工具。0.26.0 的精确实现 SHA 和门禁证据记录在[实施状态](./规划/08-onebot-napcat-protocol-tools.md)，未完成前不得把分支名当成可复现版本。PyPI 与七七安装状态是独立结论。

---

## 目录

| 页面 | 内容 |
|------|------|
| [安装、升级与测试验收](./installation.md) | 精确 SHA 安装、NoneBot 加载、零外部 I/O smoke、隔离验收与回退 |
| [依赖与运行前提](./dependencies.md) | 全部 Python 依赖用途、MCP/系统前提，以及哪些后端默认不会连接 |
| [配置参考](./configuration.md) | 所有配置文件的完整字段说明（`config.json`、`providers.toml`、`model_config.json` 等） |
| [调度链路与运行时架构](./runtime-architecture.md) | 从消息准入到 Agent、选模、工具、确认、缓存和默认资源的完整链路 |
| [OneBot / NapCat 协议工具](./protocol-tools.md) | v11/v12/NapCat 支持矩阵、四个开关、权限确认、限额、回退和排错 |
| [244 项协议动作总表](./protocol-actions.md) | 固定版本离线清单、逐动作暴露策略、请求字段与永久拒绝原因 |
| [自定义工具开发](./custom-tools.md) | 隔离文件工具 capability、runner 边界与 AI 工具包热插拔 |
| [NoneBot 插件与 ToolSpec 接入](./plugin-integration.md) | 兼容已有 Matcher，或编写强类型函数供 AI 调用 |
| [性格系统](./personality.md) | `temperaments.json` 性格预设配置与用户切换管理 |
| [完整指令表](./commands.md) | 所有 Bot 指令的参数与权限说明，包括二阶段确认、生成工具权限与请求管理 |
| [Plan 1 完成审计](./规划/05-plan1-completion-audit.md) | A-01～C-07 的源码、测试 node、门禁状态与最终关闭条件 |
| [Plan 2 / Plan 3 完成度审计](./规划/06-plan2-plan3-completion-audit.md) | H-08 后的运行态缺口、Milestone I 依赖顺序与非生产门禁 |
| [功能级意图发现与 OneBot 可靠性](./规划/07-intent-discovery-onebot-reliability.md) | 菜单/QWeb 发现链路、两阶段展开、表情降级和七七隔离验收门禁 |
| [全量 OneBot / NapCat 协议工具实施状态](./规划/08-onebot-napcat-protocol-tools.md) | K-01～K-07 依赖顺序、实现边界、本地/远端证据和恢复点 |
| [核心修复包：全库审查修复记录](./规划/09-code-review-fixes-20260829.md) | 2026-08-29 全库审查的 11 项修复、根因、回归测试与验证状态 |
| [待修复问题清单](./规划/10-pending-issues-backlog.md) | 审查遗留的中低优先问题、建议批次与处理顺序 |

---

## 管理员常用功能

- `查看请求`：查看当前正在处理的 LLM 请求、来源、用户、运行时长与消息预览。
- `停止请求 [编号|all]`：终止卡住或不想继续的 LLM 请求，停止后会清理对应用户 CD。
- `查看消耗 [数量或范围]`：查看 API Token 消耗记录；新记录会显示本轮请求累计耗时。
- `添加LLM功能 <需求>`：生成并复核工具草稿；核对 diff 与哈希后再用 `批准LLM功能` 启用。
- `设置LLM功能权限 <包> <至少 8 位哈希前缀> <工具> user|superuser`：对精确的已激活生成工具版本授予或撤销普通用户权限；只有 manifest 已请求 `user` 的工具才能放宽，manifest 声明为 `superuser` 时不能降级。

## Milestone A / B / C 工具安全与一致性边界

- Capability 契约严格包含 `network`、`process`、`workspace`、`host_filesystem`、`secrets` 五个布尔字段，effective 值是申请与管理上限的交集；省略时默认仅启用私有 workspace。`secrets` 是预留能力，当前不会注入宿主环境或密钥。
- Generated Tool 的管理上限固定为仅私有 workspace；Custom File Tool 属于管理员维护的信任域，只有静态 `TOOLS_REGISTRY.capabilities` 字面量声明才能放宽能力。禁网、禁进程、禁止宿主文件读取等隔离前提不可用时 fail closed。
- `mutating` 工具不再接受同一条消息中的文字或模型 `confirm=true` 作为授权。首次调用只创建默认 120 秒有效的 6 位大写十六进制确认码；原用户必须在原 Bot 和原会话另发 `确认执行 <码>`，或发 `取消执行 <码>`。过期、已使用或 generation 改变的确认码都会失效。
- LocalStore 配置目录按 `0700` 收紧，凭据、配置、草稿和授权策略文件按 `0600` 保护；已批准版本保持只读。这依赖 POSIX UID/mode/chmod 语义，当前不支持 Windows；文件/生成工具的完整隔离路径进一步要求 Linux。
- 正式 Custom File / Generated loader 会把源码、Schema 与安全契约固化为当前 generation 的不可变 `ToolArtifact`。两类制品执行前都复核 artifact digest；Generated Tool 还复核完整 bundle digest，活动请求不会回读后来被修改的活动源文件。
- runner 使用独立 FD3 返回版本化结果，stdout/stderr 仅作为有界日志读取；workspace 在运行中异步扫描，并在进程结束后强制最终扫描，总字节、单文件、条目数和深度四类上限均会 fail closed。
- 加载器的结构化 AST Policy 会给出 `ALLOW`、`DENY`、`CAPABILITY_REQUIRED` 或 `RISK`，并按 handler 的可达 helper 图提升实际变更属性。它是静态预检，不替代操作系统隔离或人工审查。
- runner 使用独立 PID/mount/IPC/UTS namespace 与固定 hostname，将 namespace 根挂载递归设为只读，只在 `workspace=true` 时恢复私有 workspace bind mount 的写权限；`host_filesystem=false` 时以 Landlock 限制可读路径并拒绝 xattr 读取/枚举。`network=false` 会拒绝全部新建 socket；只开放 IP 网络但未开放宿主文件时仍拒绝 AF_UNIX/AF_VSOCK，受限 `socketpair` 仅保留 AF_UNIX/STREAM；Linux keyring syscall 始终拒绝。它没有 cgroup，也不是容器或完整 syscall allowlist，`stat`/`readlink` 路径元数据仍可能可见；Custom File 显式取得宿主文件能力后仍可读取 DAC 允许的文件。FD3 只避免普通 stdout 污染，不是恶意代码认证边界。
- Generated Tool 的 `lifecycle_state.json` schema v3 是唯一决策源，使用固定 `.lifecycle.lock` 与 revision/state digest CAS；schema v2 可读取并在内存中转换，旧草稿会获得标明 `schema-v2-migration` 和 `legacy_unverified` 的 canonical evidence，下一次 canonical 写入持久化为 v3。
- schema v3 的 `DraftEvidence` 是 canonical、绑定草稿 digest 并纳入 state digest；metadata 中的 `lifecycle_evidence` 只是该证据的兼容投影，原始 `metadata.review` 仍是 best-effort 摘要。`active.json`、`permission_policy.json` 和 metadata 状态都不参与决策。
- 审阅页给出的 64 位 review stamp 绑定草稿 ID/digest、lifecycle revision/state digest 和同 bundle 当前 active digest；批准命令必须为 `批准LLM功能 <ID> <至少8位哈希> <完整review stamp>`。任何审阅后的 lifecycle 变化都会使旧 stamp 失效。
- 批准、拒绝、权限、停用和回滚只通过 `RuntimeReloader` 的候选预构建、canonical durable CAS、当前进程 RuntimeSnapshot 发布三个阶段执行；Store 的内部 commit 方法不是生产 API。目录 fsync 重试 3 次仍失败时保持 uncertain，即使 after-state 可见也不推断 durable success；只有 durability 已确认后的回读不确定才按完整 before/after identity 调和。
- 回滚要求版本前缀唯一、记录未 Archived，并在同一 canonical snapshot 下通过 owner/no-follow、目录 `0500`、仅三个 `0400` 普通文件及完整 bundle digest 校验。三阶段事务不承诺跨进程内存 ACID；其他新版进程由 watcher 最终收敛。
- 共享 LocalStore 升级时，应先退出所有仍使用 legacy 或 schema v2 的旧插件进程，再统一启动新版；旧进程不能读取 v3，且可能保留旧 RuntimeSnapshot 或竞争兼容投影。
- promotion 在下载原 CI artifact 前必须确认 job 列表完整，且名称精确为 `release-gate` 的 job 恰好一个、`status=completed`、`conclusion=success`；不会重新构建或发布 PyPI。

---

## 快速跳转

- [安装、升级与验收](./installation.md)
- [配置 AI 模型](./configuration.md#五分钟最小配置)
- [调度链路](./runtime-architecture.md)
- [OneBot / NapCat 协议工具](./protocol-tools.md)
- [编写 ToolSpec 插件](./plugin-integration.md#方式二注册强类型-toolspec推荐)
- [更新日志](../CHANGELOG.md)

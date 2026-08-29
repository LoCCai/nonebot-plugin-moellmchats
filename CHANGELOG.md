# Changelog

本项目的所有显著更改都将记录在此文件中。

## [Unreleased]

## [0.26.3] - 2026-08-29

- 统一 Python 3.10～3.13 的聊天超时异常；SSE 只接受 JSON object，摘要失败、重试通知和管理员取消通知均使用固定安全兜底，`CancelledError` 不再被通知异常替换。
- PostgreSQL transaction factory 与 local spool writer 使用 shield/settle 清理，连续取消也必须等待 rollback/close 完成或返回类型化清理错误；原业务异常或取消保持主结果。
- 分类缓存增加 generation-local `resolve_exact` single-flight：完整 key 相同只构建一次，异键仍并行，等待者取消不取消共享构建，失败可重试，发布冲突回读精确胜出记录。
- Custom File 联网改为 worker 注入的 `safe_request`：逐跳重验公网 DNS 与 allowlist、固定验证 IP、手动有界重定向、拒绝 URL 凭据/HTTPS 降级/跨域敏感头，并统一总时间与请求/响应大小预算。
- AST Policy 拒绝 Custom File 直接使用 `aiohttp`、`httpx`、`requests`、`urllib` 或 `socket`，跟踪 walrus/`NamedExpr` 别名；`safe_request` 的非 GET/HEAD 或动态 method 保守判定为变更型。
- 工具生成的 HTTP 400 内容安全判断只匹配结构化 `code`/`type` 精确值；普通消息中的 `audit`、`safety` 等单词不再误触发内容安全拦截。
- 修复个人上下文字符串键、有界 Store 只读成员检查、成员名失败缓存、`ai` 唤醒边界、Tavily TLS/占位密钥，以及布尔配置与未知键兼容告警。

## [0.26.2] - 2026-08-28

- PicMenu 内存目录增加确定性计数和 SHA-256 身份，并纳入 runtime watcher；即使首次 generation 先看到空目录，完整目录稍后安装也会在一个监控周期内自动发布新 generation，读取异常继续保留上一有效快照。
- 功能目录支持 `llm_intents` 精确意图别名。唯一、可见且已加载的业务所有者可覆盖分类模型；重复所有者、黑名单、权限不足或未加载均 fail closed，不再猜测另一个插件。
- NoneBot 兼容命令 Schema 改为严格对象、只接受 1～1024 字 `command`，并把当前 generation 的真实 `command_start` 冻结进说明；`/` 优先，否则选择最短且字典序最前的前缀，空前缀会移除占位符。
- 新增九种 `PluginDispatchResult` 真实状态。消息发送只在 Adapter 成功回调后计入结果；已确认副作用、空命中、未命中、处理失败、超时、队列拒绝、部分成功和结果不确定不再被任意 metadata 或发送前内容伪装成成功。
- 工具循环按 generation、工具名和规范化参数摘要阻止失败原样重放；部分成功或结果不确定会封锁本任务内的同一工具，同时仍允许选择不同工具或实质不同参数继续。
- 新增 `tool_progress_messages_enabled=true`。关闭时只隐藏模型前置话术和“正在执行”提示；确认消息、插件结果、最终总结和后台安全审计始终保留。

## [0.26.1] - 2026-08-28

- 将固定冷却管理入口改为优先级 0 的全文锚定 Matcher，直接识别 `/设置LLM冷却 0`、`!设置LLM冷却 0`、`！设置LLM冷却 0` 及无前缀形式；它不再依赖标准命令前缀预处理，仍只允许 `SUPERUSER` 且不会进入 LLM 链路。
- 新增仅 `SUPERUSER` 可用的固定命令 `设置LLM冷却`（别名 `设置LLMCD`、`设置对话冷却`），允许在 `0`～`86400` 秒内热修改每用户对话冷却；`0` 明确关闭冷却，命令不经过 LLM 或 Generated Tool。
- 工具草稿模型请求现在对非内容安全类 HTTP 400 执行一次最小参数兼容重试，并只回显结构化、定长且经过凭据、URL 与本地路径脱敏的错误摘要；非 JSON 错误正文继续隐藏。

## [0.26.0] - 2026-08-28

- 新增默认关闭的固定 OneBot v11/v12 与 NapCat 4.18.19 协议工具：包内离线清单完整收录 38/31/175 个动作，运行时只向模型暴露当前 Bot、权限与场景允许的严格动作 Schema，不提供任意 API 名入口。
- 新增 v11/v12 中立事件门面、独立 3 秒上限的请求级能力探测与隔离缓存；只有精确识别为 `NapCat.Onebot` 的 v11 Bot 才能发现 NapCat 扩展，探测失败只关闭本次协议工具。
- 新增协议执行 Broker、`bot_read` / `bot_send` / `bot_manage` 能力、二阶段确认、64 KiB 参数总量上限、限额、单次副作用、`result_unknown`、结果脱敏和包内低风险当前目标封装；凭证、原始发包、生命周期、任意文件接口及无可靠单目标上限的批量群管理永久拒绝。
- 业务插件菜单触发词默认优先于协议动作；点赞、当前会话戳一戳和当前消息表情回应固定由事件注入目标，模型不能改为其他用户、群或消息。
- 补充完整协议动作表、权限矩阵、配置、调度链路、模型和 ToolSpec/插件接入文档；固定来源、人工策略和 MIT 归属纳入 wheel/sdist，离线生成器纳入仓库与 sdist。

- Milestone A 将 `mutating` 工具改为真正的二阶段确认：首次调用只保存有界参数并生成 6 位大写十六进制 nonce，用户需在原 Bot/会话另发 `确认执行 <确认码>`；新增 `取消执行`，确认码受用户、会话、generation、工具版本、TTL 与一次性消费约束。
- 新增 deny-by-default `ToolCapability` / `ToolPolicy`，严格限定 `network`、`process`、`workspace`、`host_filesystem`、`secrets` 五个布尔字段并按申请与管理上限取交集；Generated Tool 的有效上限仅允许私有 workspace，`secrets` 目前不注入宿主密钥。
- Custom File Tool 支持静态 `TOOLS_REGISTRY.capabilities`；省略时默认仅启用私有 workspace，任何放宽都必须使用显式布尔字面量，未知 capability 会拒绝整个候选 generation。
- Generated Tool 即使请求 `permission=user` 也默认以 `superuser` 生效；新增 `设置LLM功能权限 <包> <至少 8 位版本哈希前缀> <工具> user|superuser`，人工授权绑定精确 digest，且不能将 manifest 声明为 `superuser` 的工具降级；策略损坏时全部降级为超管专用。
- LocalStore 配置树增加所有权和符号链接检查：配置目录收紧为 `0700`，凭据、配置、草稿和授权策略为 `0600`，已批准的不可变版本保持 `0500` / `0400`。
- Milestone B 将正式 Custom File / Generated loader 接入 generation 固定的不可变 `ToolArtifact`；执行前复核 artifact digest，Generated Tool 额外复核由 manifest、源码和测试形成的 bundle digest，不再回读活动源文件。
- runner 结果协议迁移到独立版本化 FD3，stdout/stderr 仅作为有界日志读取；该隔离避免普通输出意外污染结果，但不作为恶意子进程认证边界。
- workspace 增加总字节、单文件、文件和目录条目数、目录深度四类预算，以及符号链接/特殊文件拒绝；扫描移到 event loop 外，并在进程组结束后强制最终扫描。
- 新增结构化 AST Policy，按模块、handler 及可达 helper 输出 `ALLOW` / `DENY` / `CAPABILITY_REQUIRED` / `RISK`，并将文件/数据库/HTTP 写入和系统命令提升为 `mutating`；即使 Custom File 获准 process capability，也不能绕过二阶段确认。
- 修复 Python 3.10 在 runner 输出洪泛或失败清理后延迟析构 subprocess transport 的问题，确定性关闭三路 pipe 并等待 connection-lost callback 收敛。
- 修复部分 CPython 3.10/早期 3.11 把只读代理作为 frame builtins 执行 import 时触发内部 `SystemError` 的兼容问题；worker 在执行不可信源码前探测解释器能力，受影响版本使用拒绝公开变更入口的冻结映射，其余版本保持 `MappingProxyType`，Generated AST 与 OS 隔离边界不变。
- runner 增加独立 PID/mount/IPC/UTS namespace、固定 hostname、递归只读根挂载和条件可写 workspace bind；`host_filesystem=false` 时使用 Landlock 读 allowlist，并拒绝 xattr 读取/枚举。禁网时拒绝全部 socket，只开放 IP 网络但无宿主文件权时仍拒绝 AF_UNIX/AF_VSOCK，受限 socketpair 仅保留 AF_UNIX/STREAM；keyring syscall 无条件拒绝。它不使用 cgroup，也不是容器或完整 syscall allowlist，`stat`/`readlink` 路径元数据仍可能可见，显式 `host_filesystem=true` 仍允许 DAC 范围内的宿主读取。
- 文档安装地址改为当前 0.25 维护分支，避免误装尚未合并维护实现的远端 `master`。
- 文档同步当前冷却、重试、分类、命令别名、总结模型与生成工具开关语义。
- 补充文件工具权限声明、AI 工具包格式，以及 runner 的 Linux 前提和网络/文件系统隔离边界。
- Milestone C 将 `lifecycle_state.json` canonical schema 升至 v3，以固定 `.lifecycle.lock`、revision/state digest CAS、durable replace 和严格 Draft/Version 状态机统一管理草稿、活动版本与精确权限；兼容读取 schema v2，并把旧状态转换为带 `schema-v2-migration` evidence 的 v3 内存状态，后续 canonical 写入持久化 v3。
- 新增 canonical、draft-digest-bound `DraftEvidence`，严格校验 producer/outcome/summary/risks/时间/顺序；metadata 的 `lifecycle_evidence` 改为 canonical evidence 投影，原始 `metadata.review` 仍只是 best-effort 摘要。`create_draft()` 只创建 `Draft`，ToolAuthoring 使用专用 transition 入口逐步推进。
- 草稿审阅扩展为 summary、manifest、source、tests、risks、capabilities、diff 七个无损分页区段；页头除 lifecycle revision/state digest 与区段 SHA-256 外，还包含绑定草稿 ID/digest、revision/state digest 和同 bundle active digest 的 64 位 review stamp 及完整三参数批准命令。审阅后的任一 lifecycle 变化都会要求重新查看。
- 批准、拒绝、权限、停用和回滚统一通过 `RuntimeReloader` 的 typed prepare/commit transaction：先预构建 after-state 候选，再持久化 canonical CAS，最后发布当前进程 RuntimeSnapshot；Store 的 commit 方法降为内部实现，不再是生产 API。发布失败保留 canonical 新 revision 并交由 watcher 收敛，不盲写旧映射。
- lifecycle directory fsync 增加 3 次有界重试；重试耗尽时即使 after-state 可见也保持 uncertain，只有 durability 已确认后的回读不确定才允许按完整 before/after identity 调和。回滚增加唯一/非 Archived、owner/no-follow、`0500` 目录、仅三个 `0400` 普通文件和完整内容 digest 校验。
- RuntimeSnapshot 与 ToolSnapshot 增加 Generated lifecycle revision/state digest/active stamp；watcher 增加外层异常恢复、0.5～30 秒有界退避、意外退出日志，并把文件指纹 I/O 移出事件循环。
- 修复 `刷新工具` 在原子重载失败时仍误报成功的问题；成功回显实际发布的 generation 与 Custom/MCP 数量，失败明确保留旧 generation。黑名单前置校验重载失败不写配置，写入后的同步失败则明确提示配置与运行快照暂时不一致。
- `查看LLM状态` 增加 desired/applied lifecycle、converged 与 legacy projection stale/error；完整事件总线、跨进程 RuntimeSnapshot 与 legacy 审计投影仍遵守文档中的有限一致性边界。
- CI 增加 Python 3.10～3.13 普通矩阵、mandatory root Sandbox 零 skip、单次 sdist/wheel 构建、四组 checkout 外 package smoke 与 fail-closed 聚合 `release-gate`，官方 Actions 均固定完整 SHA；最新本地总门禁已通过，首次远端 `release-gate` green 与 required-check 配置仍待取得。
- 继承自上游的 tag 自动 PyPI 发布 workflow 改为手动提升默认分支已全绿 CI 的同一制品；promotion 会先验证 job 列表完整且恰好一个名称精确为 `release-gate` 的 job 已 `completed/success`，再下载原 CI artifact，不重新构建，也不发布 PyPI。

## [0.25.0] - 2026-08-18

- 修复非对象、缺字段及类型错误的工具参数导致对话中断的问题，并递归冻结 generation 数据。
- 工具、NoneBot 插件、MCP 与生成工具重名时整代拒绝；重载提交失败恢复全部 Manager 状态。
- Provider `/models` 临时失败时保留该 Provider 最后一次成功缓存。
- 文件型 Python 工具改为 AST 读取元数据并在降权子进程执行；不再向文件工具注入 Bot、Event 或 ToolManager。
- 增加超级管理员 AI 工具包草稿、双模型复核、哈希批准、停用、升级和回滚流程。
- 批准工具包按 SHA-256 只读保存并切换，新请求使用新 generation，进行中的请求保持旧快照。
- 生成工具 runner 增加 nobody、`no_new_privs`、资源上限、进程组清理、干净环境和有界队列；隔离不可用时 fail closed。
- 普通用户无法看到或执行 `superuser` 工具；变更型工具继续要求文字和参数双重确认。

## [0.24.0] - 2026-08-18

- LoCCai 接管维护，保留原包名、配置目录和命令兼容。
- 增加有界 LLM/兼容投递队列、成员信息缓存、整任务与工具预算。
- 默认将插件调用直接定向到目标 Matcher，完整事件总线改为显式兼容列表。
- 增加原子资源热重载、文件监听、运行状态命令与结构化指标。
- 增加 `ToolSpec`、`ToolContext`、`ToolResult` 显式工具接口和分步 Agent 限额。
- 普通群上下文不再查询成员或回查引用消息，敏感 Payload 不再完整写日志。
- 冷却请求改为排队前立即拒绝，整任务时间预算覆盖排队与执行阶段。

## [0.22.2] - 2026-06-25

### Added

- 被戳一戳时可按概率走 LLM 对话：新增 `config.json` 配置项 `poke_llm_rate`（0~1，默认 0.3，仅群聊生效）。命中概率时以 `(xx戳了一下你)` 作为对话内容复用完整 LLM 流程（cd/队列/工具/上下文/性格）；cd 中或概率外则保持原有随机默认文案回复。老用户配置文件无此字段时视为 0，行为不变。

### Changed

- 限制单轮 LLM 工具调用最多实际执行 10 个，超出的工具调用会返回跳过提示，避免一次性并发/连续调用过多工具。

## [0.22.1] - 2026-05-22

### Added

- `查看消耗` 现在会显示每次 API 调用实际执行的工具名称和次数，方便排查工具调用链路。

### Changed

- 在自定义工具执行前校验模型传入参数；若存在函数签名未声明的参数，返回可被模型读取的工具错误提示并跳过调用，避免直接 TypeError。
- 更新aiohttp版本


## [0.22.0] - 2026-05-22

### Added


- 新增运行中请求管理：超级管理员可通过 `查看请求` 查看当前正在处理的 LLM 请求，并通过 `停止请求 [编号|all]` 终止卡住或不需要继续的请求。
- `查看消耗` 现在会显示新记录的请求耗时，便于排查慢请求与 API 卡顿。

### Changed

- 重构 `__init__.py`，将聊天运行时、请求管理、工具重载与 Token 消耗格式化拆分到独立模块，入口文件仅保留 NoneBot 命令入口。
- 优化分类模型提示词：不确定时倾向于返回可能插件，降低小模型漏选工具导致主模型无法工作的概率。
- 重构 `moe_llm.py`，拆分为多个文件。
- 移除视觉模型的 `is_vision` 开关依赖：有图片时强制使用 `vision_model`，未配置则提示用户先设置视觉模型。
- 优化 API 400 错误提示：现在会尽量透出厂商返回的 `code`、`type`、`param` 和 `message`，方便直接定位请求失败原因。

## [0.21.0] - 2026-04-26

### Added

- 新增 MCP 工具接入，可通过 `mcp_servers.toml` 配置 MCP Server，并在 `刷新工具` / `重载工具` / `刷新插件` 时自动加载。
- `custom_plugin_info.json` 新增 `dependencies` 字段，可为 NoneBot 插件声明伴生工具，例如让“随机图”插件先调用 Danbooru tag 搜索 MCP。
- 自定义函数和 MCP 工具支持返回图片结果，格式为 `{"text": "...", "images": [...]}`，图片会自动交给视觉模型处理。
- 现在添加插件黑名单的时候会判断是否存在这个插件，不存在会提示

### Changed

- 工具系统统一管理 NoneBot 插件、自定义函数和 MCP 工具，黑名单、常驻工具、依赖注入均支持三类工具。
- `刷新工具` 现在会同时刷新 NoneBot 插件描述、`custom_tools/`、`custom_plugin_info.json` 依赖声明和 MCP 工具。
- `custom_plugin_info.json` 修改后可通过 `刷新工具` 生效，无需重启。


## [0.20.7] - 2026-04-24

- 工具调用中的回复对象占位符由 `[reply_user]` 改为 `[at:0]`，当前消息里额外 `@` 的人从 `[at:1]` 开始编号，减少与 `current user` 的混淆
- 更新插件事件模拟中的占位符解析逻辑，`[at:0]` 现在会正确映射到被回复的用户
- 更新工具提示与历史占位符渲染逻辑，统一使用新的 `at` 编号规则
- 更新切换模型时，有重复模型会导致直接说找不到该模型的bug
- 升级依赖安全约束：`ujson >=5.12.0`、`python-dotenv >=1.2.2`、`filelock >=3.20.3`，并将最低 Python 版本提升到 3.10 以兼容修复版本
- 适配Deepseek v4调用工具要回传思考内容

## [0.20.6] - 2026-04-21

- 现在调用其他nonebot插件能模拟@了（须对bot说话时@对应目标）
- 现在能自定义配置戳一戳和空白@消息
- 优化回复消息格式，防止llm认错人

## [0.20.5] - 2026-04-14

- 优化消息格式以减少模型幻觉
- 修复可能返回使用情况为none导致报错的bug
- 现在可以配置`extra_body`了
- 修复 `temperaments.json` 首次生成或读取失败时返回类型不正确，导致性格切换/读取异常的问题
- 增加模型配置校验与自动回退：当 `selected_model`、`category_model`、`moe_models`、`vision_model` 或 `summary_model` 未配置或不可用时，会给用户提示并在可修复的情况下自动回退
- 修复工具返回图片后自动切换视觉模型时未同步 `stream` 状态的问题，避免跨模型请求参数串用
- 现在不同供应商同名模型不会冲突了

## [0.20.4] - 2026-04-12

- 工具调用插件现在支持捕获其他插件发送的图片，并自动切换视觉模型进行分析
- 修复部分插件直接调用 `bot.send_group_msg` 等 API 时内容无法被捕获的问题
- 修复触发工具调用时表情包格式 `[名字]` 被写入历史记录的问题
- 修复部分模型只能接收一个System角色消息而出问题的bug
- 修复分类模型思考内容没去掉的bug
- 增加其他插件执行超时时间，超过时间就不等了（60秒）

## [0.20.3] - 2026-04-11

- 新增 providers.toml 模型级 `no_tools` 配置：对不支持工具调用格式的模型标记后，历史中的工具消息会自动拍平为普通文本，兼容 MoE 混合模型场景
- 优化 HTTP 连接池，全局复用 aiohttp Session，减少重复握手开销
- 优化上下文过期清理逻辑
- 修复多处指令响应时错误调用其他 matcher 的 bug
- 修复 MoE 关闭时分类请求未正确走默认模型的问题
- 优化 Token 消耗记录代码结构
- 优化 API 异常提示，现在会显示 HTTP 状态码及具体原因
- 优化首次启动拉取模型列表性能
- 重构工具调用历史记忆机制：改为将工具消息（截断后）直接存入历史记录，取代原先的文本摘要注入方案，LLM 可通过原生消息格式感知历史工具调用

### [0.20.1, 0.20.2] - 2026-04-10

- 修复ai助手也不小心带上上下文的bug
- 现在可以配置是否自动在system prompt加入当前时间
- 现在非流式也能发表情包了
- 新增常驻插件
- 新增重置对话记录命令
- 新增查看token消耗
- 优化System prompt，增加缓存命中率
- 现在只要有custom_tools文件夹就不会重复生成`example.py`了
- 优化调用工具时上下文长度
- 优化刷新插件信息提示
- 优化错误提示消息

## [0.20.0] - 2026-04-08
- 同一个供应商可以填多个key，随机选择
- 优化工具调用逻辑
- 现在可以设置允许超级管理员私聊bot（方便调用如执行shell命令之类的工具）
- 现在可以在模型设置中开启结构化输出，方便分类模型使用
- 现在第一次安装后，不再生成旧版`models.json`文件。
- 现在没有开启moe的情况下开启了工具调用，会使用默认模型进行选择工具。有图片的情况下会直接选择视觉模型
- 现在可以按全局默认 < 供应商默认 < 分组配置 < 独立配置进行模型参数设置了
- 现在可以模糊查找供应商和模型

## [0.19.0] - 2026-04-07
- **重构配置系统**：全面引入 `providers.toml`。现在只需提供服务商的 `base_url` 和 `api_key`，程序将自动补全请求路径与 `Bearer` 鉴权头，彻底告别繁杂的 JSON 手动配置。
- **全自动模型发现**：启动时自动请求提供商接口抓取可用模型并建立本地缓存。
- **指令与交互升级**：
  - 所有模型与系统设置相关的管理员指令全面放宽权限，现在支持**私聊**无痕管理。
  - 大幅美化 `查看模型` 的控制台输出，支持按供应商分类展示与过滤查询，告别刷屏。
  - 新增 `查看当前配置` 指令，提供可视化的大模型运行状态仪表盘。
  - 新增 `刷新模型` 指令，新增 API Key 或节点后无需重启，一键热重载并抓取新模型。
  - 现在可以用编号来快速切换模型
- **向下兼容**：完美兼容原有的 `models.json` 配置，老用户平滑过渡。

## [0.18.6, 0.18.7] - 2026-04-06
- 简化工具函数写作方式，现在只需要用`docstring` 和 `Annotated` 写明介绍和参数即可，不再需要写一大堆json
- 解耦联网搜索和网页提取。现在可以通过在工具中添加`TOOL_DEPENDENCIES`注入其他工具
- 修复一些bug

## [0.18.3, 0.18.4, 0.18.5] - 2026-04-06
- 优化提示词，优化连续调用工具
- 现在llm能获得其他插件的执行结果了（纯文本）
- 优化多轮调用工具，同时修复一些bug
- 兼容Gemma4等用thought而不是think标签

## [0.18.0, 0.18.1, 0.18.2] - 2026-04-04
- **重磅功能**：新增大模型函数调用（Function Calling / Tools）能力。支持大模型理解意图并代为调用系统内的其他本地插件（如点歌、查天气等），自动将指令伪造并派发。
- **创新架构**：引入了“轻量模型预分类 - 按需注入 Schema”的两阶段机制。彻底解决全量工具说明塞入上下文导致 Token 剧增和触发模型幻觉的问题。
- **配置与指令**：新增 `/设置工具调用 开/关` 以及动态的插件调用黑白名单管理 `/添加/移除黑名单 [插件标识]`，全面保障系统的安全性。
- **自定义原生函数 (Custom Tools)** ：新增 `custom_tools` 目录支持。用户可以零门槛编写原生 Python 函数（如高精度计算器、获取系统时间等）供大模型直接调用执行，并支持自动生成包含规范注释的代码模板。
- **插件描述覆写机制**：新增 `custom_plugin_info.json` 配置。允许用户重写现有 NoneBot 插件的调用描述与指令用法规范（如指导模型正确生成特定指令的格式），有效解决部分插件原生注释对大模型不友好的问题。
- **新增**：新增快速切换分类模型功能，支持通过指令（如 `/切换分类模型 [模型名字]`）实时热切换。

## [0.16, 0.17, 0.17.1, 0.17.2] - 2025-11-24
- **重磅更新**：新增多模态视觉支持与智能路由
- 支持识别当前消息或引用消息中的图片
- 升级分类器：支持判断“视觉需求”，实现文本任务与视觉任务的自动分流
- 新增配置：`model_config.json` 增加 `vision_model` 字段
- 视觉任务通过专用视觉模型处理
- 优化：重构消息预处理流程，实现多模态与纯文本历史记录的无缝兼容
- 修复一些bug。同时图片被拒绝报400时也会提示了

## [0.15.11] - 2025-06-22
- 重试间隔从 `2**retry_times` 改为 `2**(retry_times+1)`

## [0.15.10] - 2025-05-20
- 去掉了谷歌单独处理，可以免得其他模型出bug
- 优化了sse处理，鲁棒性更强

## [0.15.9] - 2025-05-13
- 修复获取不到昵称时再次产生bug的bug
- 优化搜索的提示，现在更可爱了

## [0.15.8] - 2025-04-23
- 修复未开启分段和流式发送时也发表情包的bug
- 修复读性格失败时，再产生新bug的bug

## [0.15.7] - 2025-04-22
- 对话的优先级降低
- 修复非流式模式忘了加await的bug

## [0.15.6] - 2025-04-21
- 优化发送表情包逻辑：为防止上下文干扰，现在发送的表情包不会进入上下文了
- 优化重试逻辑

## [0.15.5] - 2025-04-16
- 修复错误提示bug

## [0.15.4] - 2025-04-16
- 优化错误提示
- 优化description

## [0.15.2] - 2025-04-13
- 修复有些模型没有top_k的bug（说的就是你，Gemini）
- 优化表情包发送逻辑

## [0.15.0] - 2025-04-12
- **新增**：支持分段发送与表情包
- 修复一些bug，优化性能和提升容错

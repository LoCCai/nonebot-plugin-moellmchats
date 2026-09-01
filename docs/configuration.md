# 配置参考

配置分为两层：NoneBot 自身的 `.env`，以及插件 LocalStore 中的 JSON/TOML 文件。模型 API Key 应写入 `providers.toml`，不要混进 `.env` 或提交到 Git。

插件配置目录由 `nonebot_plugin_localstore.get_plugin_config_dir()` 决定。`LOCALSTORE_USE_CWD=true` 时通常是：

```text
<Bot 工作目录>/config/nonebot_plugin_moellmchats/
```

未启用该选项时使用操作系统的 NoneBot 用户配置目录，具体规则参照 [NoneBot Plugin LocalStore](https://github.com/nonebot/plugin-localstore)。

插件启动和重载时会把其 LocalStore 配置目录收紧为 owner-only `0700`，普通配置、密钥、草稿和策略文件收紧为 `0600`；已批准的不可变版本使用 owner-only 只读权限。这依赖 POSIX 的 UID、mode bit 与安全 `chmod(..., follow_symlinks=False)` 语义；当前不支持 Windows，平台无法提供这些保证时会 fail closed。不是当前进程用户拥有的路径或符号链接也会拒绝处理，不会盲目 `chmod`。文件/生成工具的完整隔离路径进一步要求 Linux。

配置模板会在插件首次加载时自动生成。建议在隔离测试目录中完成首次加载，然后修改模板并执行 `重载LLM`。自动生成的 `providers.toml` 含有非空的 `sk-xxxxxx` 占位值；真实 driver 启动时，后台模型刷新会把它视为已配置 Key 并尝试访问示例 Provider 的 `/models`。若要求首次检查也完全不联网，请先按[零模型、零数据库的加载 smoke](./installation.md#零模型零数据库的加载-smoke)生成并修改模板，再启动测试 Bot。

> **注意**：JSON 文件不支持注释，手动复制示例后记得删除 `//` 注释及末尾多余逗号。

---

## NoneBot `.env` 变量

这些变量由 NoneBot 或 LocalStore 读取，不属于 `config.json`：

| 变量 | 是否建议配置 | 通俗含义 | 示例 |
| --- | --- | --- | --- |
| `SUPERUSERS` | 必须 | 哪些 QQ 号可以执行模型、工具和生命周期管理指令 | `["123456789"]` |
| `NICKNAME` | 必须 | 用户可以用哪些名字称呼并触发 Bot | `["七七", "机器人"]` |
| `COMMAND_START` | 可选 | NoneBot 命令前缀；空字符串表示允许无前缀命令 | `["/", ""]` |
| `LOCALSTORE_USE_CWD` | 测试推荐 | 为 true 时把 LocalStore 放在 Bot 当前工作目录，便于隔离和备份 | `true` |
| `LOCALSTORE_CONFIG_DIR` | 高级 | 显式指定 LocalStore 配置根目录；不要让测试和生产指向同一路径 | `"/srv/test-bot/config"` |

示例：

```dotenv
SUPERUSERS=["123456789"]
NICKNAME=["七七", "机器人"]
COMMAND_START=["/", ""]
LOCALSTORE_USE_CWD=true
```

`SUPERUSERS`、`NICKNAME` 等数组应使用当前 NoneBot 能解析的 JSON 风格格式。不要把 PostgreSQL DSN、Redis URL 或模型 Key 填到猜测的变量名中：标准安装不会从环境变量自动组合这些高级后端。

## 五分钟最小配置

1. 按上节配置 `.env`，用独立工作目录加载插件一次。
2. 在生成的 `providers.toml` 中只保留实际使用的服务商，填写 `base_url`、`api_key` 和必要的 `models`。
3. 启动后发送 `刷新模型`，再发送 `查看模型`；复制列表里的精确模型 ID。
4. 在 `model_config.json` 中设置 `selected_model`。图片需要 `vision_model`；MoE 才需要单独关注 `category_model` 与 `moe_models`。
5. 执行 `重载LLM` 和 `查看配置`，先验证纯文本；随后再逐项开启工具、联网、MCP 或生成工具。

一个最低风险的开始方式是：

```json
{
  "use_moe": false,
  "moe_models": {
    "0": "your-model (your-provider)",
    "1": "your-model (your-provider)",
    "2": "your-model (your-provider)"
  },
  "selected_model": "your-model (your-provider)",
  "category_model": "your-model (your-provider)",
  "summary_model": "your-model (your-provider)",
  "vision_model": "",
  "use_web_search": false,
  "use_tools": false,
  "tool_blacklist": [],
  "resident_plugins": []
}
```

确认普通聊天稳定后，再把 `use_tools` 改为 true。JSON 文件不允许 `//` 注释；本页的 `json5` 代码块只是为了讲解字段，不能原样带注释复制。

---

## 配置文件一览

| 文件 | 维护方式 | 说明 | 修改后是否需重启 |
|------|----------|------|------------------|
| `config.json` | 手动 | 基础行为、队列、预算与重载配置 | 自动或 `重载LLM` |
| `providers.toml` | 手动 | 服务商/API 密钥/模型参数，核心配置 | 自动或 `重载LLM`；远端列表用 `刷新模型` |
| `model_config.json` | 指令/手动 | MoE 调度、视觉/工具开关 | 指令实时，手动自动重载 |
| `model_cache.json` | 系统自动 | 模型列表缓存，无需手动修改 | — |
| `models.json` | 手动（遗留） | 旧版手动模型配置，不推荐新用户使用 | 自动或 `重载LLM` |
| `temperaments.json` | 手动 | 性格预设 system prompt | 自动或 `重载LLM` |
| `temperament_config.json` | 指令自动 | 用户↔性格绑定关系 | 指令实时生效 |
| `replies.toml` | 手动 | 空白艾特与戳一戳回复 | 自动或 `重载LLM` |
| `mcp_servers.toml` | 手动 | MCP Server 配置，启用后会作为 Function Calling 工具注入 | 自动、`刷新工具` 或 `重载LLM` |
| `custom_plugin_info.json` | 手动 | 仅在自动 PicMenu/Metadata 菜单不足时，完整覆写插件发现说明并可声明依赖 | 自动、`刷新工具` 或 `重载LLM` |
| `custom_tools/` | 手动 | 原生 Python 工具函数 | 自动、`刷新工具` 或 `重载LLM` |
| `generated_tools/lifecycle_state.json` | 系统/超管指令 | schema v3 canonical 生命周期、digest-bound evidence、活动版本、授权、revision 与 state digest；兼容读取 v2 | 生命周期指令提交后生效 |
| `generated_tools/drafts/`、`versions/` | 系统/超管指令 | 草稿与内容寻址的只读版本 | 由生命周期指令管理 |
| `generated_tools/active.json`、`permission_policy.json` | 系统兼容投影 | 供旧版本读取的单向投影；当前版本不回读它们作决策 | canonical 提交后尽力更新 |

`lifecycle_state.json` schema v3 是唯一决策源。canonical 文件不存在时，会在固定 `.lifecycle.lock` 下单向迁移现有 `active.json`、草稿 metadata 和权限策略并直接写入 v3；已有 schema v2 文件则兼容读取为 v3 内存状态，为旧草稿补入明确的 `schema-v2-migration` / `legacy_unverified` evidence，并在下一次 canonical 写入时持久化 v3。canonical 文件一旦存在，损坏时会 fail closed，不会回退到旧投影。投影写入失败不撤销已经提交的 canonical revision 或当前进程 RuntimeSnapshot，`查看LLM状态` 会显示 `legacy_projection_stale/error`。修复文件系统问题后，下一次生命周期写入或完整进程重启会再次尝试投影。

首次持久化 schema v3 前，不得让仍使用 legacy 或 schema v2 的旧版插件进程与新版并行运行。旧进程无法读取 v3，且可能保留旧运行快照或与兼容投影竞争，形成 split-brain；所有共享该 LocalStore 的旧进程必须先退出，再统一启动新版。

---

## `config.json` — 基础配置

📌 修改后会在约 2 秒内自动校验并原子重载，也可执行 `重载LLM`。Tavily 搜索 API Key：[获取地址](https://tavily.com/)。

`config.json` 首次生成时已包含全部默认字段。下表与源码 `DEFAULT_CONFIG` 一一对应；一般只改确实理解的字段，不要为了“性能”盲目放大队列、结果或 runner 上限。

### 上下文与记忆

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `max_group_history` | `10` | 每个群聊保留的最近环境消息条数 | 群聊上下文不足时小幅增加；会增加 prompt |
| `max_user_history` | `8` | 每个用户保留的最近问答轮数 | 需要更长连续对话时调整 |
| `max_history_chars` | `16000` | 一次注入历史的字符硬上限 | 模型上下文较小时降低 |
| `max_history_tokens` | `4000` | 历史的 token 估算上限 | 配合模型 context window 调整 |
| `max_context_sessions` | `1000` | 群聊、用户、CD 等内存状态表各自最多保存多少个 key，超出按 LRU 淘汰 | 多群实例才需增加 |
| `user_history_expire_seconds` | `600` | 群聊/用户临时上下文和 CD 状态空闲多久过期 | 希望更快遗忘时降低 |

### 请求、模型与工具预算

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `max_retry_times` | `3` | 一次模型步骤总尝试数，包含第一次；重试前等待 4 秒、8 秒 | 不稳定 API 可保留；总预算较小时降低 |
| `max_tool_rounds` | `6` | 兼容工具闭环轮数上限 | 多步骤任务确有需要时调整 |
| `max_agent_steps` | `6` | Agent 工具步骤上限；实际与 `max_tool_rounds` 取较小值 | 通常与上项保持一致 |
| `max_repeated_tool_calls` | `2` | 同一任务内同一工具使用同一组规范化参数最多调用几次 | 防止模型原样循环；不同实质参数仍受总工具轮次与 Agent 步数限制 |
| `max_tool_result_chars` | `6000` | 没有独立 `result_limit` 时，工具文本最多交给模型多少字符 | 控制上下文与数据暴露面 |
| `max_tool_images` | `4` | 一轮工具结果最多交给视觉模型多少张图 | 视觉模型能力明确时再增加 |
| `request_timeout_seconds` | `180` | 整个任务的单一墙钟预算，包含排队、分类、模型、工具和收尾 | 慢模型可增加，但队列会占更久 |
| `classification_timeout_seconds` | `20` | 单次分类请求超时 | 分类服务慢且可靠时小幅增加 |
| `tool_timeout_seconds` | `30` | 未在 ToolSpec 中单独声明时的可信工具超时 | 只读慢查询可按需调整 |

### 冷却、准入、兼容投递与成员缓存

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `llm_max_active` | `4` | 全局同时执行的 LLM 任务数 | 按 API 配额和内存评估 |
| `llm_max_pending` | `32` | 全局最多等待多少个任务 | 队列过长会消耗总预算，不宜盲目增加 |
| `llm_max_per_user` | `2` | 单用户总槽位；默认表现为 1 个活动 + 1 个等待 | 防止单用户占满队列 |
| `cd_seconds` | `120` | 用户成功占用对话后进入的冷却时间；排队前检查；允许 `0`～`86400`，`0` 表示关闭 | 可由超管执行 `设置LLM冷却 <秒数>` 热修改 |
| `legacy_dispatch_max_pending` | `16` | 完整 NoneBot 事件总线兼容投递的等待上限 | 只有遗留插件确有需要时调整 |
| `legacy_dispatch_timeout_seconds` | `20` | 兼容投递单次超时 | 遗留 Matcher 较慢时谨慎增加 |
| `legacy_full_event_plugins` | `[]` | 必须走完整事件总线的插件包名数组；其他插件只定向执行 Matcher | 仅解决已确认的前处理器依赖 |
| `member_cache_ttl_seconds` | `600` | QQ 群成员信息缓存多久 | 群名片频繁变化时降低 |
| `member_cache_max_entries` | `4096` | 群成员缓存最大条目数 | 超大群/多群实例才需增加 |
| `member_lookup_timeout_seconds` | `2` | OneBot 成员查询超时 | adapter 明显较慢时调整 |

### 二阶段确认

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `pending_action_ttl_seconds` | `120` | 一次性确认码有效期 | 用户响应慢时小幅增加 |
| `pending_action_max_entries` | `256` | 进程内最多保存多少个待确认操作 | 队列满应先查滥用，不要先放大 |
| `pending_action_max_argument_bytes` | `16384` | 一个待确认参数的 canonical JSON 最大字节数 | 通常不改 |
| `pending_action_failure_window_seconds` | `60` | 错误确认码尝试的固定统计窗口 | 安全策略调整时修改 |
| `pending_action_max_failures` | `8` | 同一调用者在窗口内最多失败几次 | 遭枚举时降低 |
| `pending_action_max_failure_keys` | `4096` | 失败预算最多跟踪多少个调用者 key | 多实例高流量时评估 |

### 热重载与 Provider consumer 回滚开关

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `runtime_watch_enabled` | `true` | 是否监听配置文件变化并发布新 generation | 只想手动重载时设 false |
| `runtime_watch_interval_seconds` | `2` | 文件变化检测间隔 | 慢存储可增加；过小会增加 I/O |
| `provider_catalog_categorize_enabled` | `true` | 分类短目录使用 Provider Catalog | 仅诊断 Provider/legacy parity 时临时回滚 |
| `provider_catalog_llm_payload_enabled` | `true` | 主模型 Tool Schema 使用 Provider Catalog | 同上 |
| `provider_catalog_llm_tools_enabled` | `true` | 工具调用解析/执行使用 Provider Catalog | 同上 |
| `provider_catalog_pending_actions_enabled` | `true` | 确认执行使用 Provider Catalog | 同上；不要长期关闭安全 consumer |
| `provider_catalog_search_enabled` | `true` | 搜索网页提取器选择使用 Provider Catalog | 同上 |
| `provider_catalog_management_enabled` | `true` | 黑名单管理校验使用 Provider Catalog | 同上 |

六个 `provider_catalog_*` 字段是分 consumer 的兼容回滚开关，不是性能开关。默认保持 true；任何关闭都应记录原因、范围和恢复时间，且 D-09 完成前不能删除 legacy sidecar。

### OneBot / NapCat 协议工具

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `protocol_tools_enabled` | `false` | 协议工具总开关；关闭时不探测 Bot，也不向模型显示协议动作 | 只在隔离测试确认权限和限额后显式开启 |
| `protocol_tools_napcat_extensions_enabled` | `true` | 精确识别到 `NapCat.Onebot` 后，是否加入审核允许的 NapCat 扩展 | 只想保留通用 v11 动作时关闭 |
| `protocol_tools_low_risk_direct_enabled` | `true` | 固定当前目标的点赞、戳一戳和消息表情回应是否可在限额内直执 | 希望三者也必须另发确认码时关闭 |
| `protocol_tools_business_first` | `true` | 用户原话命中已加载业务插件菜单时，优先业务 Matcher 并抑制冲突协议动作 | 仅在诊断菜单冲突策略时临时关闭 |

四项都必须是 JSON 布尔值。`protocol_tools_enabled=false` 时其余三项不会单独开放任何动作；安装或升到 0.26.0 也不会自动改成 true。协议工具还要求 `model_config.json` 的 `use_tools=true` 和支持 Function Calling 的当前模型。

开启后的完整权限、确认、限额、NapCat 识别和排错说明见 [OneBot / NapCat 协议工具](./protocol-tools.md)。不要把 244 项清单理解成全部可执行：永久拒绝动作只留在离线审计总表，普通用户也看不到超级管理员动作。

### 互动、搜索与显示

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `tool_progress_messages_enabled` | `true` | 工具进度总开关；每个通过 Schema、权限、信任和重复检查的调用各发送一条可信固定提示 | 希望群里更安静时由超管发送 `/设置工具进度 关`；确认、插件结果、最终总结和后台日志仍保留 |
| `tool_progress_model_preface_enabled` | `false` | 是否把当前工具决策响应中的一句自然话术附在固定提示后；不增加模型请求，只在总开关开启时生效 | 希望提示更拟人时由超管发送 `/设置工具自然话术 开`；默认关闭可避免模型提前下结论 |
| `search_api` | `"your api"` | Tavily 的完整 `Authorization` 值，例如 `Bearer tvly-...` | 仅在开启联网前填写 |
| `fastai_enabled` | `false` | 是否开放 `ai <内容>` 快速助手 | 需要无角色、无表情快速问答时开启 |
| `emotions_enabled` | `false` | 是否让普通角色回复可使用表情包 | 配好目录后再开启 |
| `emotion_rate` | `0.1` | 每轮启用表情包提示的概率，范围 0～1 | 控制出现频率 |
| `emotions_dir` | `"absolute path"` | 表情包根目录，必须是 Bot 可读的绝对路径 | 开启表情包前填写 |
| `private_chat_enabled` | `false` | 是否允许超级管理员私聊 Bot；普通用户私聊仍不开放 | 需要管理私聊时开启 |
| `show_datetime` | `false` | 是否在 system prompt 注入当前时间 | 需要时间感知时开启，会让缓存更易变化 |
| `poke_llm_rate` | `0.3` | 群聊戳一戳走 LLM 的概率；0 表示关闭 | 控制成本和打扰程度 |

`tool_progress_messages_enabled=true` 时，运行时为每个真正获准的调用单独生成一条提示。例如 NoneBot 为 `正在投递插件：bread_shop｜功能：抢面包`，搜索为 `正在调用搜索工具：web_search`，协议为 `正在调用协议接口：napcat_v11__send_like｜功能：发送点赞`。Registered、Custom File、Generated 和 MCP 也会显示固定来源与工具名。并行批次逐项发送；二阶段动作只显示“正在准备工具确认”，不会冒充已执行。未知工具、参数错误、越权、策略拒绝和重复受限调用不会发送“正在执行”。

固定提示只使用可信工具身份、协议摘要或 NoneBot 指令首词，不显示完整参数。发送有 1 秒预算；失败或超时只写安全状态和异常类型，不阻断工具，也不会重试副作用。提示本身不是成功证据，最终仍以 Adapter 成功回调和类型化工具结果为准。

`tool_progress_model_preface_enabled=true` 时，当前工具决策响应可以同时给出一句自然话术；运行时会折叠空白、截断并脱敏 QQ 号、URL、本地路径、Token、Cookie、Authorization 和 Base64，再把它合并到同一条固定提示中。它不会额外调用模型；并行批次只把话术附在第一项。空白话术按空处理，绝不会压掉固定提示。

`tool_progress_messages_enabled=false` 不会跳过分类、工具调用、Matcher、权限检查、二阶段确认或结果回填，也不会把失败变成静默成功。无论自然话术开关是什么，模型工具消息、最终失败说明和安全审计照常产生；只有用户可见的进度提示被隐藏。需要确认的协议工具和自定义 `mutating` 工具仍会直接向原会话发送确认指令。

超级管理员可以使用 `/设置工具进度 关`（别名 `设置调用进度`、`设置工具提示`）即时关闭这些提示，使用 `/设置工具进度 开` 恢复。该固定指令接受可选 `/`、`!`、`！` 前缀及 `开/关/1/0`，不经过 LLM 或工具调用链，写入后立即更新当前运行快照。

自然话术使用 `/设置工具自然话术 开|关|1|0`，别名为 `设置工具话术`、`设置调用话术`。它同样是优先级 0 的全文固定 `SUPERUSER` 命令，不经过 LLM；总开关关闭时，即使自然话术设置为开也不会发送进度。

### 文件/生成工具 runner

| 字段 | 默认值 | 通俗含义 | 何时调整 |
| --- | ---: | --- | --- |
| `generated_tools_enabled` | `true` | 是否允许创建新的 AI 工具草稿；不会停用已批准工具 | 不允许生成新功能时关闭 |
| `generated_tool_max_pending` | `4` | Custom File 与 Generated Tool 共用的等待数；另有 1 个活动任务 | 队列压力确认后再调整 |
| `generated_tool_timeout_seconds` | `30` | runner 单次墙钟上限 | 工具确需更久时评估 |
| `generated_tool_cpu_seconds` | `10` | 子进程 CPU 时间上限 | CPU 密集工具需专门审查 |
| `generated_tool_memory_mb` | `256` | 子进程地址空间上限，MiB | 发生明确内存不足且代码可信时调整 |
| `generated_tool_output_bytes` | `65536` | stdout、stderr、FD3 各自最多读取的字节数 | 防日志/协议输出失控，通常不改 |
| `generated_tool_workspace_mb` | `64` | 私有 workspace 总容量，MiB | 明确需要临时大文件时调整 |
| `generated_tool_workspace_max_files` | `256` | 文件和目录条目总数上限 | 防止小文件洪泛 |
| `generated_tool_workspace_max_depth` | `8` | workspace 最大目录深度 | 通常不改 |
| `generated_tool_workspace_max_file_bytes` | `8388608` | 单个 workspace 文件最大字节数（8 MiB） | 与总容量一起评估 |
| `generated_tool_max_processes` | `16` | 仅显式 `process=true` 的 Custom File 使用；默认 `process=false` 时进程额度为 1 | 高权限工具经审查后才调整 |

所有整数预算字段必须是正整数；`emotion_rate` 和 `poke_llm_rate` 必须在 0～1 之间。配置文件中缺少已知字段时会补入默认值；字段类型或安全约束不合法时，候选重载失败并保留上一代。

`pending_action_*` 控制变更型工具的二阶段确认。首次工具调用只在内存中保存规范化参数及 SHA-256，并生成绑定 Bot/Adapter、用户、群组或私聊会话、工具、runtime generation 和 Generated bundle digest 的 6 位确认码；只有同一用户在同一会话内另发 `确认执行 <确认码>` 才会消费并执行。确认码一次性、默认 120 秒过期，重载后失效；待确认队列满或参数过大时直接拒绝，不会执行。

格式错误、不存在、过期或会话不匹配的确认/取消尝试，按 `(Bot, Adapter, 用户, 群组或私聊)` 独立计入固定时间窗口；默认 60 秒内失败 8 次后，该调用者会被拒绝到窗口结束。成功确认或取消会清除自己的失败状态，其他用户的失败不会影响正确调用者。失败状态表最多保留 4096 个调用者键。PendingAction 与失败状态均只保存在进程内存中，进程重启后自然失效。

主进程内注册的可信 `ToolSpec` 由统一执行入口严格校验和裁剪结构化结果：`text` 必须是字符串，`images` 必须是只含非空字符串的 list/tuple，`metadata` 必须是字符串键的 Mapping，并会复制为普通字典。Custom File / Generated Tool 当前使用 Runner v1：worker 只规范化并通过 FD3 返回 `text` 与 `images`，不会透传工具返回值中的 `metadata`。两条路径的文本都优先使用工具自身 `result_limit`，未声明时使用 `max_tool_result_chars`；图片使用 `max_tool_images`，普通调用与二阶段确认调用使用相同裁剪上限。

Custom File 与 Generated Tool 在候选 generation 加载时都会经过结构化 AST Policy。Policy 对模块级语句、handler 及其可达 helper 生成 `ALLOW`、`DENY`、`CAPABILITY_REQUIRED` 或 `RISK` finding；阻断项会拒绝整代加载，检测到的文件/数据库/HTTP 写入或系统命令会把 `read_only` 提升为 `mutating`。Custom File 即使获准 `process=true` 也不能绕过 PendingAction 二阶段确认。该检查是保守的静态预检，不是完整代码证明，也不替代运行时隔离和人工审查。

文件工具和生成工具共用全局单并发 runner，等待队列默认 4。每次调用都启动 nobody 子进程并使用环境变量白名单；墙钟、CPU、内存、进程、输出和工作目录任一越限都会终止整个进程组。无法进入 nobody 或启用硬限制时，这两类工具 fail closed，普通聊天、可信 `ToolSpec` 与 MCP 不受影响。

文件/生成工具的完整 runner 隔离路径要求 Linux 资源限制，并要求主进程有能力切换到 UID/GID 65534；Windows 当前不支持。Capability 严格限定为 `network`、`process`、`workspace`、`host_filesystem`、`secrets` 五个布尔字段，默认仅 private workspace；`secrets` 当前不注入宿主密钥。所有文件/生成工具都必须成功进入 PID/mount/IPC/UTS namespace 并设置固定 hostname，`network=false` 时再进入 network namespace；Generated Tool 的管理上限不允许放宽 network/process/host_filesystem/secrets，Custom File 只有静态 `TOOLS_REGISTRY.capabilities` 字面量才能放宽。不满足 namespace、hostname、mount、Landlock 或 seccomp 要求时 fail closed。

正式 Custom File / Generated loader 会把源码、Schema 与安全契约固化为请求 generation 的不可变 `ToolArtifact`；执行前复核 generation 与 artifact digest，Generated Tool 还复核 bundle manifest、源码、测试共同形成的 bundle digest。worker 只执行制品中的源码，不回读活动工具文件。结果只通过版本化 FD3 协议返回，stdout/stderr 分别作为有界日志读取，普通 `print` 或直接写 stdout 不会意外污染结果协议；FD3 的作用是通道分离，并不是对恶意工具代码的认证机制或额外信任边界。旧 Path API 仅为隔离测试和兼容保留，不是正式 loader 的执行路径。

工作目录在运行中异步扫描，并在进程组结束后强制最终扫描；总容量、单文件大小、文件和目录条目数、目录深度四类预算，以及符号链接和特殊文件检查任一违规都会拒绝结果。runner 把 namespace 根挂载递归设为只读，只在 `workspace=true` 时恢复私有 workspace bind mount 的写权限；`workspace=false` 不注入 `_workspace`，私有 cwd 也保持只读。`host_filesystem=false` 时 Landlock 限制可读路径并由 seccomp 拒绝 xattr 读取/枚举；禁网时拒绝全部新建 socket，只开放 IP 网络但无宿主文件权时仍拒绝 AF_UNIX/AF_VSOCK，受限 `socketpair` 只保留 AF_UNIX/STREAM；Linux keyring syscall 始终拒绝。它没有 cgroup，也不是容器或完整 syscall allowlist，`stat`/`readlink` 等路径元数据仍可能可见；显式 `host_filesystem=true` 的 Custom File 可读取 DAC 允许的宿主文件。只应批准已审查代码，详细边界见[自定义工具开发](./custom-tools.md#runner-隔离边界与运行要求)。

`runtime_watch_enabled=false` 时，所有文件变更（包括重新开启该开关）都需要手动执行 `重载LLM` 才会进入运行快照。

### 表情包目录结构

`emotions_dir` 下每个子文件夹的名称即为表情包名（中英皆可），LLM 会自动识别文件夹名，无需手动在 prompt 中添加说明。

```plaintext
your_absolute_path/
├── smile/
│   ├── smile1.jpg
│   └── smile2.png
├── 滑稽/
│   ├── huaji001.png
│   └── huaji002.jpg
└── 阴险/
    ├── yinxian_a.jpg
    └── yinxian_b.png
```

---

## `providers.toml` — 服务商配置（核心）

📌 首次运行后自动生成模板。程序会自动补全 API 路径（`/chat/completions`、`/models`）和 Bearer 鉴权头，并在启动时**自动抓取**可用模型列表。支持全局代理与四级参数继承。

> 模板中的 `sk-xxxxxx` 只是示例，但因为它非空，真实 driver 首次启动仍会尝试请求示例地址。启动前应删除不使用的 Provider，并替换保留项的 `base_url`、`api_key` 和 `models`；隔离加载 smoke 不应运行 driver。

### 基本结构

```toml
# base_url: 基础API地址（直接写Base URL即可，程序会自动补全 /chat/completions 及 /models）
# api_key: 你的API密钥（无需手动写 Bearer ，程序会自动补全）。支持填入单个字符串，或字符串列表实现随机轮询，如 ["sk-key1", "sk-key2"]
# proxy: [可选] 该服务商的全局代理
# models: [可选] 手动补充的模型列表。若API不支持 /models 自动获取，或获取不全时可在这里手动指定作为补充。
# extra_payload 不是 provider 根字段；必须放在下文四个模型参数继承层之一。
# 【全局默认配置】（所有供应商的所有模型均默认继承此设置，垫底优先级）
[global_default]
stream = true
is_segment = true
max_segments = 5

[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key = "sk-xxxxxx"
models = ["deepseek-chat", "deepseek-reasoner"]

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key = "sk-xxxxxx"
proxy = "http://127.0.0.1:7890"
```

### Provider 根字段

| 字段 | 是否必需 | 通俗含义 |
| --- | --- | --- |
| `base_url` | 是 | OpenAI-compatible API 的 Base URL；插件补全 `/chat/completions` 和 `/models` |
| `api_key` | 是 | 单个 Key，或用于随机轮询的字符串数组；插件自动补 `Bearer` |
| `models` | 建议 | 手工补充的真实模型名；服务商不支持 `/models` 时必须填写 |
| `proxy` | 可选 | 该 Provider 的 HTTP 代理 |
| `default_config` | 可选 | 该 Provider 下所有模型共同继承的模型参数表 |
| `config_groups` | 可选 | 按 `models` 数组批量套用参数的表数组 |
| `model_configs` | 可选 | 按真实模型名配置的最高优先级参数表 |

`api_key`、`base_url`、`proxy` 和 `models` 是连接/发现字段；它们不应放进 `extra_payload`。反过来，`temperature`、`stream`、`no_tools`、`extra_payload` 和 `capability_routing` 等是模型参数，应放进四级继承层，不要直接写在 `[providers.<名称>]` 根表中。

### 四级参数继承

优先级：**全局默认 < 供应商默认 < 分组配置 < 单模型独立配置**

```toml
# 【一】供应商默认配置（覆盖 global_default，对该供应商所有模型生效）
[providers.openai.default_config]
temperature = 1.0

# 【二】批量分组配置（覆盖前两项，对 models 数组中的模型生效）
[[providers.openai.config_groups]]
models = ["gpt-4o", "gpt-4-turbo"]
temperature = 1.2

# 【三】单模型独立配置（最高优先级，覆盖一切）
[providers.openai.model_configs."o1-preview"]
stream = false      # 该模型不支持流式，单独关闭
json_mode = true    # 开启 JSON 结构化输出（适用于分类模型）

# 厂商私有字段必须放在某个模型参数继承层中；这里放在单模型层。
extra_payload = { extra_body = { thinking = { level = "low" } } }

# 【no_tools】标记该模型不支持工具调用格式
# 设置后：本次请求不注入 tool schema，历史中的工具消息也自动转为普通文本
[providers.some-provider.model_configs."some-cheap-model"]
no_tools = true
```

这四层使用**浅合并**：后一级的同名键整体覆盖前一级，不会递归合并嵌套对象。例如单模型层重新声明 `extra_payload` 时，会替换较低层的整个 `extra_payload`。

常用模型参数如下：

| 字段 | 含义 | 注意事项 |
| --- | --- | --- |
| `stream` | 是否请求流式响应 | 工具调用时会临时切成非流式；能力路由也会关闭不支持 streaming 的模型 |
| `is_segment` | 流式文本是否分段发送 | 只影响消息呈现，不改变模型能力 |
| `max_segments` | 最多发送多少段 | 防止过度刷屏 |
| `max_tokens` | 请求的最大输出 token | 必须符合服务商接口约束 |
| `temperature`、`top_p`、`top_k` | 采样参数 | 不是所有服务商都同时支持 |
| `json_mode` | 分类请求是否附加 JSON object 响应格式 | 主要给分类角色使用；服务商不兼容时关闭 |
| `no_tools` | 明确标记模型不支持 Function Calling | 为 true 时不注入 Tool Schema，并把历史工具消息转成普通文本 |
| `extra_payload` | 浅合并到请求根 JSON 的厂商私有参数 | 只放厂商扩展字段；不要覆盖 `model`、`messages`、`tools` 或 `stream` |
| `capability_routing` | 该模型的受信能力目录元数据 | 只有同时开启 `model_config.json` 的高级路由才会消费 |

普通未知字段不会自动透传给 API；需要透传的厂商字段必须放入 `extra_payload`。API Key 不应出现在截图、日志或版本库中。

### 多 API Key 轮询

```toml
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key = ["sk-key1", "sk-key2", "sk-key3"]  # 随机轮询
```

---

## `model_config.json` — 智能调度配置

📌 支持 QQ 指令实时切换；手动修改会自动原子重载。最稳妥的模型标识是 `查看模型` 显示的完整 `模型名 (provider)`。只写裸模型名时，插件仅在名称唯一时自动补全 Provider；同名冲突会拒绝或回退。

```json
{
  "use_moe": false,
  "moe_models": {
    "0": "deepseek-chat (deepseek)",
    "1": "deepseek-chat (deepseek)",
    "2": "deepseek-reasoner (deepseek)"
  },
  "vision_model": "gpt-4o (openai)",
  "selected_model": "deepseek-reasoner (deepseek)",
  "category_model": "glm-4-flash (zhipu)",
  "summary_model": "deepseek-chat (deepseek)",
  "use_web_search": false,
  "use_tools": true,
  "tool_blacklist": [
    "nonebot_plugin_orm",
    "nonebot_plugin_some_dangerous_plugin",
    "mcp__filesystem",
    "mcp__filesystem__read_file",
    "mcp__filesystem__*"
  ],
  "resident_plugins": [],
  "capability_routing": {
    "enabled": false
  }
}
```

### 全部字段

| 字段 | 默认/初始行为 | 通俗含义 |
| --- | --- | --- |
| `use_moe` | `false` | 是否按分类难度从 `moe_models` 选择纯文本聊天模型 |
| `moe_models.0/1/2` | 首次校验后回退到可用模型 | 简单、中等、复杂任务各自绑定的模型；仅 `use_moe=true` 时用于聊天 |
| `selected_model` | 首次校验后选择可用模型 | 未开启 MoE 时的聊天模型；未开启 MoE 但需要分类时也由它做分类 |
| `category_model` | 缺失时回退到 `selected_model` | 只有 `use_moe=true` 时负责难度、视觉和工具分类 |
| `summary_model` | 缺失时自动补为 `selected_model` | AI 工具包独立复核等总结任务使用的模型 |
| `vision_model` | 空字符串 | 有真实图片或分类判定需要视觉时优先使用；未配置会明确提示，不悄悄交给文本模型 |
| `use_web_search` | `false` | 把内置 `web_search` 加入候选；还要求 `use_tools=true` 且最终模型支持工具调用 |
| `use_tools` | `true` | Function Calling 总开关；关闭后不向聊天模型注入任何工具 Schema |
| `tool_blacklist` | 内置一组框架/基础插件 | 禁止目录展示、Schema 注入和管理释放的工具标识；支持精确名、MCP 服务名和尾部 `*` |
| `resident_plugins` | `[]` | 管理员强制注入/诊断兜底；无视分类结果、每轮都尝试注入，仍受总开关、黑名单和权限约束 |
| `capability_routing` | 缺失，等价于关闭 | 高级受信能力路由；关闭对象必须精确为 `{"enabled": false}` |

只要 `use_moe`、`use_tools`、`use_web_search` 任一开启，就会先做一次分类。真实选模顺序和默认固定模式见[调度链路](./runtime-architecture.md#4-分类与模型选择)。
`tool_blacklist` 现在同时支持：

- NoneBot 插件包名
- `custom_tools/` 自定义函数名
- MCP 工具名
- MCP 服务级禁用，如 `mcp__filesystem`
- MCP 通配禁用，如 `mcp__filesystem__*`

`resident_plugins` 也可以填写以上任意工具标识。正常业务不需要把插件常驻：系统会优先读取 PicMenu Next 已安装的内存目录（可来自 QWeb Feature Catalog），否则读取 `PluginMetadata.extra.menu_data`，先按紧凑功能目录判断意图，命中后再展开详细 Schema。只有隔离验收、诊断漏选或确实要求每轮强制提供时才加入常驻。

`custom_plugin_info.json` 是最高优先级的完整覆写，不会和自动菜单拼接。某插件一旦出现在该文件中，名称、描述、用法和可选 `menu_data` 都以覆写为准；如果只想使用插件原有 PicMenu/Metadata 菜单，就不要为它保留示例或空覆写条目。详细字段和安全边界见[插件集成](./plugin-integration.md#自动复用-picmenuqweb-菜单)。

### 可选能力路由 `capability_routing`

这是高级、显式 opt-in 功能。不开启时继续使用上面的固定角色绑定；不要为了普通聊天先启用它。

启用分两步。第一步，为每个要参与路由的模型在 `providers.toml` 的模型参数层写入**完整且精确**的能力元数据：

```toml
[providers.openai.model_configs."gpt-4o-mini"]
stream = true

[providers.openai.model_configs."gpt-4o-mini".capability_routing]
availability = "available"
latency_ms = 800
quality_score = 700

[providers.openai.model_configs."gpt-4o-mini".capability_routing.capabilities]
text = true
vision = true
tools = true
json_schema = true
reasoning = false
streaming = true

[providers.openai.model_configs."gpt-4o-mini".capability_routing.cost]
input_per_million = "0.15"
output_per_million = "0.60"

[providers.openai.model_configs."gpt-4o-mini".capability_routing.limits]
context_window = 128000
max_output = 16384
```

第二步，在 `model_config.json` 顶层加入完整策略。以下对象是可直接解析的结构；模型绑定仍使用本页前面的字段：

```json
{
  "enabled": true,
  "policy": {
    "allow_degraded": false,
    "mode": "fixed_preferred",
    "version": "operator-policy-v1"
  },
  "requirements": {
    "input_tokens": 2048,
    "maximum_latency_ms": 10000,
    "maximum_unit_cost": null,
    "minimum_context_window": 4096,
    "minimum_quality": 0,
    "output_tokens": 1024
  }
}
```

上面的对象应作为 `"capability_routing": {...}` 的值，不是整个 `model_config.json`。三个模式的区别：

| `mode` | 行为 |
| --- | --- |
| `fixed_preferred` | 优先使用当前角色绑定；绑定模型不满足完整要求时，按能力目录选择其他合格模型，适合渐进启用 |
| `fixed_only` | 只允许当前角色绑定；绑定不存在或能力不足时直接拒绝 |
| `capability_only` | 忽略七个固定角色绑定，完全从合格目录中确定性选择 |

字段规则必须完整满足以下契约：

- 模型元数据只能且必须包含 `availability`、`capabilities`、`cost`、`latency_ms`、`limits`、`quality_score`。
- `capabilities` 必须同时包含 `text`、`vision`、`tools`、`json_schema`、`reasoning`、`streaming` 六个布尔值。
- `availability` 只接受 `unknown`、`available`、`degraded`、`unavailable`；`unknown` 和 `unavailable` 不参与选择，`degraded` 还需 `allow_degraded=true`。
- 当前选择器要求候选具有可比较的 `cost`。免费模型也应明确写字符串 `"0"`；不要用二进制浮点数表示价格。所有候选应使用同一币种和计价口径。
- `requirements.input_tokens` 是成本排序使用的预计输入量；`output_tokens` 同时受模型 `max_output` 约束，二者之和不能超过 `minimum_context_window`。
- `maximum_unit_cost` 为 `null` 表示不设价格上限；需要限制时使用与模型 `cost` 相同的两个精确十进制字段。
- 质量分是运维方自定义的相对整数分，候选之间必须使用同一标尺；延迟也应来自同一测量口径。
- 开启对象和所有嵌套对象采用精确字段集合。缺字段、多字段、类型错误、目录漂移或没有合格模型都会 fail closed，不能回退到一个未声明能力的模型。

建议先用 `fixed_preferred`、一到两个已核实模型和测试 Key 验证；图片、工具和分类分别要求 `vision`、`tools`、`json_schema`，不要仅凭模型名称猜能力。

---

## `model_cache.json` — 系统动态缓存

📌 **系统自动维护，无需手动修改。**

存储从 `providers.toml` 中各 API 提供商自动拉取到的最新可用模型列表，避免每次启动或对话时重复请求。可通过`刷新模型`指令实时更新此缓存。

---

## `models.json` — 遗留手动配置（不推荐新用户）

📌 用于兼容旧版手动编写的复杂模型配置。系统启动时会将此文件中的模型与 `providers.toml` 获取的模型合并。**新用户建议直接使用 `providers.toml`。**

```json5
{
  "dpsk-chat": {
    "url": "https://api.deepseek.com/chat/completions",
    "key": "Bearer sk-xxx",
    "model": "deepseek-chat",
    "temperature": 1.5,
    "max_tokens": 1024,
    "stream": true,           // 是否流式响应
    "is_segment": true,       // 是否开启分段发送（仅 stream=true 时生效）
    "max_segments": 5         // 分段发送最大段数（超出后停止发送）
  },
  "dpsk-r1": {
    "url": "https://api.deepseek.com/chat/completions",
    "key": "Bearer sk-xxx",
    "model": "deepseek-reasoner",
    "stream": false,
    "top_k": 5,
    "top_p": 1.0
  },
  "gpt-4o": {
    "url": "https://api.openai.com/v1/chat/completions",
    "key": "Bearer sk-xxx",
    "model": "gpt-4o",
    "proxy": "http://127.0.0.1:7890",
    "stream": true
  }
}
```

## `replies.toml` — 互动回复配置

📌 首次触发“戳一戳”或“空白艾特”时自动生成模板。此文件用于自定义机器人的基础互动文案，修改后实时生效，无需重启。

支持在文案中使用 `{bot_name}` 占位符，程序会自动将其替换为机器人当前的真实昵称。

```toml
# 机器人回复文案配置
# 可在文案中使用 {bot_name} 作为机器人昵称的占位符

# 收到只有艾特，没有具体文字内容的回复文案
hello = [
    "你好喵~",
    "你好OvO",
    "喵呜 ~ ，叫{bot_name}做什么呢☆"
]

# 收到戳一戳时的回复文案
poke = [
    "嗯？",
    "戳我干嘛qwq",
    "请不要戳{bot_name} >_<",
    "喵 ~ ！ 戳{bot_name}干嘛喵！"
]
```

## `mcp_servers.toml` — MCP Server 配置

📌 首次运行后自动生成模板。修改后会自动重载，也可使用 `刷新工具` 或 `重载LLM`。

MCP 工具会被并入现有函数调用系统，统一受 `use_tools`、`tool_blacklist`、`resident_plugins` 控制。
任何启用的 MCP 在发现阶段不可达都会使本次重载失败，旧 generation 继续服务；状态可用 `查看LLM状态` 查询。

| 字段 | 适用范围 | 默认/要求 | 含义 |
| --- | --- | --- | --- |
| `enabled` | 全部 | 缺失时为 `true` | 是否启用；新配置务必显式写 `false`，审查完成后再打开 |
| `transport` | 全部 | 默认 `stdio` | `stdio`、`streamable_http` 或 `sse`；兼容别名会被规范化 |
| `description` | 全部 | 可选 | 服务的人类可读说明，不改变权限 |
| `command` | stdio | 必需 | 要由 Bot 宿主启动的可执行文件 |
| `args` | stdio | `[]` | 传给命令的字符串参数数组 |
| `env` | stdio | `{}` | 叠加到宿主完整环境上的变量表，不是隔离环境 |
| `cwd` | stdio | 可选 | 工作目录；只有当前 MCP SDK 支持该参数时才应用，不能把它当安全边界 |
| `url` | HTTP/SSE | 必需 | MCP endpoint；当前配置不提供自定义鉴权 header 字段 |
| `timeout` | 全部 | `30` 秒 | 连接/通用超时基准，最小 1 秒 |
| `discover_timeout` | 全部 | 回退到 `timeout` | 重载时 `list_tools` 的等待上限 |
| `tool_timeout` | 全部 | 回退到 `timeout` | 每次 `call_tool` 的等待上限 |
| `sse_read_timeout` | SSE | SDK 内部默认；可显式设置 | SSE 读取预算；外层发现/调用仍受对应超时约束 |
| `result_limit` | 全部 | `6000` 字符 | MCP 文本结果交给模型前的截断上限 |

```toml
[mcp.filesystem]
enabled = false
transport = "stdio"
command = "uvx"
args = ["mcp-server-filesystem", "/tmp"]
description = "本地文件系统 MCP"
cwd = "/path/to/workdir"
timeout = 30
discover_timeout = 30
tool_timeout = 60
result_limit = 6000

[mcp.filesystem.env]
SOME_TOKEN = "xxx"

[mcp.myapi]
enabled = false
transport = "streamable_http"
url = "http://127.0.0.1:8000/mcp"
description = "HTTP MCP 示例"
# timeout = 30
# discover_timeout = 30
# tool_timeout = 60
# result_limit = 6000
```

MCP 是外部信任边界，不走 Custom/Generated Tool 的 nobody、namespace、Landlock 或 seccomp runner。stdio 子进程会继承 Bot 的完整环境，再叠加 `env`；当前配置也不能给单个 MCP 工具声明 `permission` 或 `read_only`/`mutating`，因此不会自动获得精确的二阶段确认保护。只启用已经审查的 Server，并优先使用最小环境启动包装器；会写文件、发消息、删除数据或调用管理 API 的 MCP 应保持禁用/黑名单，或改写成能明确声明契约的可信 [`ToolSpec`](./plugin-integration.md#方式二注册强类型-toolspec推荐)。

工具名规范为 `mcp__<服务名>__<工具名>`，超长名称会截断并附加摘要。配置后先执行 `刷新工具`，用 `查看LLM状态` 确认发现数量，再按精确工具名配置黑名单或常驻项。每次发现和调用都会建立会话并在完成后关闭；不要假设远端连接常驻。

## 热重载与兼容投递

- 文件检测间隔默认 2 秒并带防抖；指纹文件 I/O、解析和候选构建均移出事件循环，所有资源校验成功后才发布到当前进程。
- 活动请求固定使用其开始时的配置、模型、工具 Schema 与不可变 `ToolArtifact` generation。Custom File 和 Generated Tool 都执行候选加载时固化的源码与安全契约，不会在子进程启动时回读后来修改的源文件；新请求只在新 generation 发布后看到修改。
- 半截 JSON/TOML、错误 Python 工具或不可达 MCP 不会污染当前运行状态。
- `legacy_full_event_plugins` 之外的 NoneBot 插件只定向执行目标插件 Matcher，不经过无关记录器/数据库链。
- 完整事件总线兼容模式统一单并发、队列 16、默认 20 秒超时。插件源码和 Matcher 注册变化仍需重启 NoneBot。
- 普通资源重载对当前进程原子发布。Generated Tool 管理另以 canonical CAS 持久化后发布当前进程 snapshot；共享配置目录的其他新版进程通过 watcher 最终收敛，不提供跨进程内存 ACID。

---

## 相关 Wiki 页面

- [自定义工具开发](./custom-tools.md)
- [NoneBot 插件集成](./plugin-integration.md)
- [性格系统](./personality.md)
- [完整指令表](./commands.md)

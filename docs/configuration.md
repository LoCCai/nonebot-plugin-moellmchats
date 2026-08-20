# 配置参考

所有配置文件均位于 `nonebot_plugin_localstore.get_plugin_config_dir()` 目录，具体路径参照 [NoneBot Plugin LocalStore](https://github.com/nonebot/plugin-localstore)。

插件启动和重载时会把其 LocalStore 配置目录收紧为 owner-only `0700`，普通配置、密钥、草稿和策略文件收紧为 `0600`；已批准的不可变版本使用 owner-only 只读权限。这依赖 POSIX 的 UID、mode bit 与安全 `chmod(..., follow_symlinks=False)` 语义；当前不支持 Windows，平台无法提供这些保证时会 fail closed。不是当前进程用户拥有的路径或符号链接也会拒绝处理，不会盲目 `chmod`。文件/生成工具的完整隔离路径进一步要求 Linux。

**首次运行时自动生成**，建议先启动一次再停止，然后根据本文档手动修改。

> **注意**：JSON 文件不支持注释，手动复制示例后记得删除 `//` 注释及末尾多余逗号。

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
| `custom_plugin_info.json` | 手动 | 覆写插件描述，并可声明插件依赖工具 | 自动、`刷新工具` 或 `重载LLM` |
| `custom_tools/` | 手动 | 原生 Python 工具函数 | 自动、`刷新工具` 或 `重载LLM` |
| `generated_tools/lifecycle_state.json` | 系统/超管指令 | schema v3 canonical 生命周期、digest-bound evidence、活动版本、授权、revision 与 state digest；兼容读取 v2 | 生命周期指令提交后生效 |
| `generated_tools/drafts/`、`versions/` | 系统/超管指令 | 草稿与内容寻址的只读版本 | 由生命周期指令管理 |
| `generated_tools/active.json`、`permission_policy.json` | 系统兼容投影 | 供旧版本读取的单向投影；当前版本不回读它们作决策 | canonical 提交后尽力更新 |

`lifecycle_state.json` schema v3 是唯一决策源。canonical 文件不存在时，会在固定 `.lifecycle.lock` 下单向迁移现有 `active.json`、草稿 metadata 和权限策略并直接写入 v3；已有 schema v2 文件则兼容读取为 v3 内存状态，为旧草稿补入明确的 `schema-v2-migration` / `legacy_unverified` evidence，并在下一次 canonical 写入时持久化 v3。canonical 文件一旦存在，损坏时会 fail closed，不会回退到旧投影。投影写入失败不撤销已经提交的 canonical revision 或当前进程 RuntimeSnapshot，`查看LLM状态` 会显示 `legacy_projection_stale/error`。修复文件系统问题后，下一次生命周期写入或完整进程重启会再次尝试投影。

首次持久化 schema v3 前，不得让仍使用 legacy 或 schema v2 的旧版插件进程与新版并行运行。旧进程无法读取 v3，且可能保留旧运行快照或与兼容投影竞争，形成 split-brain；所有共享该 LocalStore 的旧进程必须先退出，再统一启动新版。

---

## `config.json` — 基础配置

📌 修改后会在约 2 秒内自动校验并原子重载，也可执行 `重载LLM`。Tavily 搜索 API Key：[获取地址](https://tavily.com/)。

```json5
{
  "max_group_history": 10,        // 群组上下文最大保留条数
  "max_user_history": 8,          // 每个用户上下文最大保留条数
  "max_history_chars": 16000,     // 历史字符上限
  "max_history_tokens": 4000,     // 历史 token 估算上限
  "max_context_sessions": 1000,   // 每个群聊/用户/CD 状态表各自的最大键数（LRU）
  "max_retry_times": 3,           // LLM 请求总尝试次数（包含首次调用）
  "max_tool_rounds": 6,           // 兼容工具交互轮次上限；与 max_agent_steps 取较小值
  "max_agent_steps": 6,           // Agent 总步骤上限；与 max_tool_rounds 取较小值，每步一个工具
  "max_repeated_tool_calls": 2,   // 单任务同一工具最多执行次数
  "max_tool_result_chars": 6000,  // 单工具结果字符上限
  "max_tool_images": 4,           // 每轮待交给视觉模型的工具图片上限
  "request_timeout_seconds": 180, // 整个 LLM 任务总预算（包含排队等待）
  "classification_timeout_seconds": 20,
  "tool_timeout_seconds": 30,
  "pending_action_ttl_seconds": 120,       // 变更型工具一次性确认码 TTL（秒）
  "pending_action_max_entries": 256,       // 全局内存 PendingAction 数量上限
  "pending_action_max_argument_bytes": 16384, // 单个待确认参数的规范 JSON 字节上限
  "pending_action_failure_window_seconds": 60, // 确认码失败尝试固定统计窗口（秒）
  "pending_action_max_failures": 8,        // 单调用者每窗口最多失败次数
  "pending_action_max_failure_keys": 4096, // 失败预算调用者键的内存上限
  "llm_max_active": 4,
  "llm_max_pending": 32,
  "llm_max_per_user": 2,          // 1 个活动 + 1 个等待
  "legacy_dispatch_max_pending": 16,
  "legacy_dispatch_timeout_seconds": 20,
  "legacy_full_event_plugins": [], // 只有这些遗留插件走完整事件总线
  "member_cache_ttl_seconds": 600,
  "member_cache_max_entries": 4096,
  "member_lookup_timeout_seconds": 2,
  "runtime_watch_enabled": true,
  "runtime_watch_interval_seconds": 2,
  "user_history_expire_seconds": 600, // 群聊/用户上下文及 CD 临时状态的空闲 TTL（秒）
  "cd_seconds": 120,              // 每个用户的对话冷却时间（秒，排队前检查）
  "search_api": "Bearer your_tavily_key", // 联网搜索 Tavily API Key（开启搜索必填）
  "fastai_enabled": false,        // 快速 AI 助手开关（无角色扮演、无分段、无表情包）
  "emotions_enabled": false,      // 是否开启表情包功能
  "emotion_rate": 0.1,            // 触发表情包的概率（0~1）
  "emotions_dir": "/absolute/path/to/emotions", // 表情包根目录（绝对路径）
  "private_chat_enabled": false,  // 是否允许超级管理员私聊 Bot
  "show_datetime": false,         // 是否在 System Prompt 中注入当前时间
  "poke_llm_rate": 0.3,           // 被戳一戳时走LLM对话的概率（0~1，0为关闭；仅群聊生效，cd中或概率外则回随机默认文案）
  "generated_tools_enabled": true, // 是否允许“添加/创建LLM功能”；不自动停用已激活工具包
  "generated_tool_max_pending": 4, // 文件/生成工具共用 runner 的最大等待数；另有 1 个活动任务
  "generated_tool_timeout_seconds": 30, // runner 单次调用墙钟上限（秒）
  "generated_tool_cpu_seconds": 10,     // 子进程 CPU 时间上限（秒）
  "generated_tool_memory_mb": 256,      // 子进程地址空间上限（MiB）
  "generated_tool_output_bytes": 65536, // stdout、stderr、FD3 协议各自的读取上限（字节）
  "generated_tool_workspace_mb": 64,    // 私有工作目录总容量上限（MiB）
  "generated_tool_workspace_max_files": 256, // 文件和目录条目总数上限
  "generated_tool_workspace_max_depth": 8,   // 最大目录层级
  "generated_tool_workspace_max_file_bytes": 8388608, // 单文件上限（字节）
  "generated_tool_max_processes": 16    // 仅 process=true 工具使用；默认 process=false 时收紧为 1
}
```

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

### 基本结构

```toml
# base_url: 基础API地址（直接写Base URL即可，程序会自动补全 /chat/completions 及 /models）
# api_key: 你的API密钥（无需手动写 Bearer ，程序会自动补全）。支持填入单个字符串，或字符串列表实现随机轮询，如 ["sk-key1", "sk-key2"]
# proxy: [可选] 该服务商的全局代理
# models: [可选] 手动补充的模型列表。若API不支持 /models 自动获取，或获取不全时可在这里手动指定作为补充。
# extra_payload: [可选] 字典格式。用于透传厂商特有参数（如 Gemini 的 thinking_config ）。
#                该字典下的内容会直接合并到发送给 API 的请求根 JSON 中。
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

# 【no_tools】标记该模型不支持工具调用格式
# 设置后：本次请求不注入 tool schema，历史中的工具消息也自动转为普通文本
[providers.some-provider.model_configs."some-cheap-model"]
no_tools = true
```

### 多 API Key 轮询

```toml
[providers.deepseek]
base_url = "https://api.deepseek.com"
api_key = ["sk-key1", "sk-key2", "sk-key3"]  # 随机轮询
```

---

## `model_config.json` — 智能调度配置

📌 支持 QQ 指令实时切换；手动修改会自动原子重载。**模型名称必须是 `providers.toml` 中可用的模型 ID**（可用`查看模型`指令查看）。

```json5
{
  "use_moe": false,           // 是否按分类难度路由 MoE 模型；不是工具或联网搜索的前提
  "moe_models": {
    "0": "deepseek-chat",     // 简单问题对应的模型
    "1": "deepseek-chat",     // 中等问题对应的模型
    "2": "deepseek-reasoner"  // 复杂问题对应的模型
  },
  "vision_model": "gpt-4o",  // 视觉任务专用模型（有图片时强制使用；未配置则提示用户设置）
  "selected_model": "deepseek-reasoner", // 不启用 MoE 时使用的模型（难度分级失败时也回滚至此）
  "category_model": "glm-4-flash",       // MoE 开启时的分类模型；否则分类使用 selected_model
  "summary_model": "deepseek-chat",      // 工具包独立复核等总结任务使用的模型
  "use_web_search": false,    // 是否把联网搜索加入候选工具；仍需 use_tools=true 且模型支持工具调用
  "use_tools": true,          // 是否启用全部函数调用（包括联网搜索）
  "tool_blacklist": [         // 禁止 LLM 调用的插件黑名单
    "nonebot_plugin_orm",
    "nonebot_plugin_some_dangerous_plugin",
    "mcp__filesystem",
    "mcp__filesystem__read_file",
    "mcp__filesystem__*"
  ],
  "resident_plugins": []      // 常驻插件：无视分类模型，强制每次注入给 LLM
}
```
`tool_blacklist` 现在同时支持：

- NoneBot 插件包名
- `custom_tools/` 自定义函数名
- MCP 工具名
- MCP 服务级禁用，如 `mcp__filesystem`
- MCP 通配禁用，如 `mcp__filesystem__*`

`resident_plugins` 也可以填写以上任意工具标识。

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

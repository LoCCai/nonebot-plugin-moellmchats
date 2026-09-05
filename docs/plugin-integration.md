# NoneBot 插件与 `ToolSpec` 接入

让 AI 调用 Bot 能力有两条不同路线：

| 需求 | 应选择的方式 |
| --- | --- |
| 已有插件已有 PicMenu/QWeb 菜单或 `PluginMetadata.extra.menu_data` | 直接复用菜单做意图发现，让系统生成模拟消息 |
| 已有 Matcher 没有可读菜单，或菜单需要人工纠正 | 用 `custom_plugin_info.json` 显式覆写 |
| 新功能需要明确参数、权限、副作用、确认和结构化结果 | 在独立 NoneBot 插件中注册强类型 `ToolSpec`（推荐） |
| 工具不需要 Bot/Event，只做纯计算或受限外部访问 | 使用 [`custom_tools/`](./custom-tools.md) 的隔离文件工具 |
| 能力来自标准 MCP Server | 配置 [MCP](./configuration.md#mcp_serverstoml--mcp-server-配置)，并先审查其宿主权限 |

菜单、`custom_plugin_info.json` 和 `ToolSpec` 不是三份等价配置：菜单负责“用户想做什么”，覆写文件补救遗留说明，`ToolSpec` 才负责“允许模型用哪些参数调用哪个 handler”。新开发优先使用 `ToolSpec`。

## 方式一：兼容调用已有 Matcher 插件

### 它实际做了什么

目标包必须已经由 NoneBot 加载，并拥有可被消息触发的 Matcher。菜单或 `custom_plugin_info.json` 只提供给模型看的发现/用法信息，不会安装插件，也不会把菜单条目变成新的 Bot API。

一次兼容调用的链路是：

```text
规范化用户原话并检查功能目录中的唯一 llm_intents 所有者
  → 唯一所有者直接选中；无别名命中才请求分类模型
  → 只展开该插件的 usage 与可消息触发菜单
  → 主模型按当前 generation 的真实 command_start 生成一个 command 字符串
  → 系统基于原消息构造合成 OneBot 消息
  → 默认只定向执行目标插件的 Matcher
  → Adapter 成功回调后才确认文本、图片或副作用
  → 把类型化执行状态和有界结果作为工具 observation 交回模型
```

这条路线只有一个模型参数 `command`，不能为插件内部每个动作声明独立 JSON Schema、`permission` 或 `effect`。为了不把任意命令误称为只读，目录会把兼容插件保守标记为 `mutating`；但为保留既有插件行为，当前兼容边界不提供强类型 `ToolSpec` 那套逐工具二阶段确认。它也无法回滚 Matcher 已经产生的副作用。

因此兼容方式只适合已经审查、且本来就允许对应用户直接发命令触发的插件。涉及发消息、群管理、扣费、写库、删除或管理 API 时，应改成明确的 `ToolSpec(effect=ToolEffect.MUTATING, ...)`。

### 自动复用 PicMenu/QWeb 菜单

发现信息按以下优先级构建，优先级高的来源会完整覆盖低优先级来源：

1. `custom_plugin_info.json` 中该插件的显式条目；
2. 已加载 PicMenu Next 当前安装的内存目录；在七七中它是 QWeb Feature Catalog 校验、物化并合并后的快照；
3. `PluginMetadata.extra["menu_data"]`；
4. `PluginMetadata` 的 `name`、`description`、`usage`。

PicMenu 桥接是可选的 duck-typed 读取，不是 Python 包依赖，也不会自行读取宿主的 `menu_config` 路径。系统只接受 PicMenu 已经安装到内存的目录，并且只为当前确实已加载的 NoneBot 插件补充功能；目录中存在但未加载的插件不会变成可执行工具。PicMenu 未加载或首次快照尚为空时使用插件元数据。系统会为内存投影计算功能数和 SHA-256，并把它纳入 watcher 指纹；目录稍后安装完整内容时会在一个监控周期内自动发布新 generation。读取器异常时保留上一份有效投影，不把完整目录悄悄降级为空目录。

每个菜单功能会规范化为有界 discovery hint：功能名、摘要、触发类型/入口、条件提示、是否隐藏、是否可由消息触发，以及可选的 `llm_intents`。QWeb 功能目录使用 `llm_intents`，PicMenu 内存投影字段是 `pmn_llm_intents`；每项 4～80 字、每功能最多 16 项。别名仅参与发现，不授予权限，也不是模糊语义匹配：系统对用户原话做 Unicode NFKC、大小写、空白和标点规范化后进行精确匹配。

唯一别名所有者会在分类模型之前直接选中，即使分类模型缓存曾返回其他插件也不会覆盖它。相同别名被多个插件声明时不猜测；所有者隐藏、被黑名单禁用、没有权限或插件未加载时也不会降级调用“看起来相近”的另一个插件，而是要求澄清或明确告知不可用。无别名命中才继续使用分类模型。`<ft ...>` 展示标签和控制字符会清理，错误类型不会被 `str()` 强行塞入快照；每插件最多 128 个功能、每功能最多 16 个触发入口、每插件最多 48,000 字符，最终分类目录还有 96,000 字符总上限。`pmn_hidden=true` 的功能不会进入普通用户的分类目录，也不会在普通用户命中同插件后泄漏到详细 Schema；被动事件和定时功能可以帮助理解插件范围，但选中后会明确标为“不得通过 command 伪造”。这些显示规则仍不代替 Matcher 的执行端权限检查。

分类目录会形成类似：

```text
- qi_group_admin | 七七群管理 > 点赞与禅定 | 请求 Bot 点赞，或临时进入和解除禅定状态。 | 命令: 点赞 / 命令: 赞我 / 命令: 给我点赞
```

分类模型只能返回第一列 `qi_group_admin`。随后主模型才看到该插件的详细菜单和严格 `command` Schema：顶层禁止额外字段，`command` 只能是 1～1024 字字符串。说明使用候选 generation 构建时冻结的真实 NoneBot `command_start`：有 `/` 时优先 `/`，否则选最短且字典序最前的前缀；配置允许空前缀时会直接移除 `<命令前缀>` 占位符，并列出其他有效前缀。模型不需要也不允许猜测占位文本。菜单不携带 handler、OneBot API 名、凭据、`ToolEffect` 或授权位；执行时仍由目标 Matcher 复核群权限、次数、黑名单和业务状态。

因此“给我点个赞”应先召回 `qi_group_admin`，再生成插件本来支持的“点赞/赞我”消息，最终仍走该插件的 `bot.send_like` 与每日次数逻辑。不要注册一个允许模型任意填写 API 名和参数的 `bot.call_api` 工具，那会绕过业务层。

兼容 Matcher 的消息输出钩子覆盖 OneBot V11 的 `send_msg`、`send_group_msg`、`send_private_msg`，以及 OneBot V12 的 `send_message`。`on_calling_api` 只暂存候选内容，只有 `on_called_api` 明确成功才会把文本/图片计入工具结果；失败发送不会再被发送前参数伪装成成功。`send_like` 不属于消息输出，不会被钩子吞掉；协议策略确认它成功且 Matcher 没有文本时，结果记为 `matched_side_effect`。

兼容执行固定使用以下状态，不以“metadata 非空”代替真实成功：

| 状态 | 含义 | 是否作为成功结果交给模型 |
| --- | --- | --- |
| `matched_with_output` | Matcher 命中，且 Adapter 已确认文本或图片发送成功 | 是 |
| `matched_side_effect` | Matcher 命中，且至少一个包内策略认定的变更 API 已确认成功 | 是 |
| `partial_success` | 已有可验证输出/副作用，随后 Matcher 或 API 又失败 | 否；明确告知部分成功，整任务内禁止再次调用该工具 |
| `matched_empty` | Matcher 命中，但只有读取 API 或没有可验证结果 | 否；相同参数不得重放 |
| `not_matched` | 检查完成但没有 Matcher 规则命中 | 否；相同参数不得重放 |
| `failed` / `timed_out` | 处理异常或超时，且没有已验证结果 | 否；相同参数不得重放 |
| `admission_rejected` | 有界兼容队列没有接纳本次执行 | 否；映射为拒绝 |
| `result_unknown` | API 调用后断连、网络错误或响应不确定，无法证明是否生效 | 否；整任务内禁止再次调用该工具 |

工具循环记录 `(generation, tool_name, canonical_arguments_digest)`。失败的原样调用不会挤占后续“不同工具或实质不同参数”的机会；`partial_success` 和 `result_unknown` 绝不自动重试同一工具。

0.26.0 另有默认关闭的[固定 OneBot / NapCat 协议工具](./protocol-tools.md)。它把 244 项接口完整收入离线审计清单，但只给模型展示人工策略允许、当前 Bot 支持且当前调用者有权使用的固定动作；永久拒绝项不会生成 Schema。业务菜单和协议动作发生意图冲突时默认仍以本节 Matcher 为先，不会用协议 Broker 绕过已有业务检查。

PicMenu/QWeb 在 MoEllmChats 初始 generation 之后才完成内存同步时，默认 watcher 会在 `runtime_watch_interval_seconds` 的一个监控周期内检测摘要变化并自动发布新目录，无需把全部插件加入常驻，也无需重启 NoneBot。`刷新工具` 仍可用于人工立即收敛和诊断；摘要不变不会重复发布，读取异常则继续使用上一有效目录。

### 配置格式

文件首次运行时自动生成，修改后自动原子重载，也可执行 `刷新工具` 或 `重载LLM`：

```json
{
  "nonebot_plugin_whatis": {
    "name": "百科搜索与群词条记忆",
    "description": "查询百科词条；也能按用户明确要求记录或遗忘群词条。",
    "usage": "查询：`[词汇]是什么?`\n记录：`记住 [A] 是 [B]`\n遗忘：`忘记 [A]`",
    "dependencies": [],
    "menu_data": [
      {
        "func": "查询词条",
        "brief_des": "查询一个明确词条",
        "trigger_method": "消息触发",
        "trigger_condition": "[词汇]是什么?",
        "llm_intents": ["帮我查询这个词条", "这个词条是什么意思"]
      }
    ]
  }
}
```

| 字段 | 是否必需 | 说明 |
| --- | --- | --- |
| 顶层键 | 是 | 目标 NoneBot 插件的真实加载名，例如 `nonebot_plugin_whatis` |
| `name` | 是 | 给模型看的简短名称 |
| `description` | 是 | 说明什么时候该调用，也要写清“不适用”的情况 |
| `usage` | 是 | 目标 Matcher 能识别的精确命令格式；不要只写自然语言概述 |
| `dependencies` | 可选 | 分类选中本插件时一并注入的已存在工具标识数组 |
| `menu_data` | 可选 | 功能级目录；功能可声明 `llm_intents` 精确别名，但仍不会改变插件权限或创建 handler |

存在显式覆写时，自动 PicMenu/元数据菜单不会再合并，避免两套说明互相打架。若希望在覆写里继续提供功能目录，可增加与 `PluginMetadata` 相同格式的 `menu_data` 数组；否则分类模型只看到该覆写的名称和描述。主模型被选中后才看到完整 `usage`。`dependencies` 只是让依赖工具一并进入 Schema，不会预先执行它们，也不能绕过黑名单或权限。

### 如何找到真实插件名

优先查看 Bot 的插件加载配置或启动日志，也可运行：

```bash
nb plugin list
```

PyPI 分发名常用连字符，NoneBot 加载名通常使用下划线，两者不一定相同。配置中的顶层键必须匹配 `nonebot.plugin.get_loaded_plugins()` 中的名称；只在文件里写一个未加载的名字不会创建工具。

### 定向投递与完整事件总线

默认仅执行目标插件 Matcher，不经过无关插件，也不运行全局事件前/后处理器。若一个已确认的遗留插件确实依赖这些处理器，可把其包名加入 `config.json` 的 `legacy_full_event_plugins`。

完整总线模式会执行更多全局代码，统一受单并发、等待队列和超时保护，但风险面更大。只为有证据的兼容需求逐个加入，不要把所有插件都放进去。Matcher 源码或注册方式变化仍需重启 NoneBot；只修改 `custom_plugin_info.json` 不需要重启。

## 方式二：注册强类型 `ToolSpec`（推荐）

`ToolSpec` 适合需要 Bot、Event、应用服务或明确安全契约的新功能。它在 NoneBot 主进程执行，是受信代码，不经过文件/生成工具的 nobody sandbox；插件作者必须自己保护凭据、连接和业务边界。

### 可复制的最小插件

在 Bot 的本地插件目录创建：

```text
src/plugins/moellm_whoami/
└── __init__.py
```

`__init__.py` 内容：

```python
from nonebot import require

require("nonebot_plugin_moellmchats")

from nonebot_plugin_moellmchats import (
    ToolContext,
    ToolEffect,
    ToolResult,
    ToolSpec,
    register_tool,
)


async def describe_current_chat(
    *,
    _tool_context: ToolContext,
) -> ToolResult:
    """返回当前调用者和会话的基本信息，不修改任何状态。"""
    event = _tool_context.event
    user_id = str(getattr(event, "user_id", ""))
    group_id = getattr(event, "group_id", None)
    group_text = "私聊" if group_id is None else f"群聊 {group_id}"
    return ToolResult(
        text=f"当前调用者是 {user_id}，会话是 {group_text}。",
        structured={
            "user_id": user_id,
            "group_id": None if group_id is None else str(group_id),
        },
        metadata={
            "request_id": _tool_context.request_id,
            "confirmed": _tool_context.confirmed,
        },
    )


CURRENT_CHAT_TOOL = ToolSpec(
    name="describe_current_chat",
    description="查询当前调用者和群聊/私聊会话标识；不读取其他成员资料。",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    handler=describe_current_chat,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=5,
    result_limit=1000,
    dependencies=(),
)

register_tool(CURRENT_CHAT_TOOL)
```

`require("nonebot_plugin_moellmchats")` 必须在导入接口之前执行，确保依赖插件先加载。再按宿主项目的既有方式加载 `moellm_whoami`；如果项目扫描 `src/plugins`，通常无需把它发布到 PyPI。

### `ToolSpec` 字段

构造签名为：

```python
ToolSpec(
    name,
    description,
    parameters,
    handler,
    effect=ToolEffect.READ_ONLY,
    permission="user",
    timeout_seconds=None,
    result_limit=None,
    dependencies=(),
    policy=None,
    confirmation_mode=ToolConfirmationMode.DEFAULT,
)
```

| 字段 | 含义 |
| --- | --- |
| `name` | 全局工具名，1～64 个字母、数字、下划线或连字符；不同来源之间也必须唯一 |
| `description` | 告诉分类模型和聊天模型何时调用；必须非空 |
| `parameters` | 顶层 `type=object` 的 JSON Schema；`required` 只能引用已声明属性 |
| `handler` | 同步或异步函数；变更型必须是可取消的 `async def` |
| `effect` | `READ_ONLY` 或 `MUTATING`；后者首次只创建待确认操作 |
| `permission` | `user` 或 `superuser`；执行端会再次核验，不只靠 Schema 隐藏 |
| `timeout_seconds` | 本工具自己的正数超时；省略时使用全局 `tool_timeout_seconds` |
| `result_limit` | 本工具文本呈现的正整数字符上限；省略时使用全局上限 |
| `dependencies` | 需要一并注入的其他工具名元组；不是调用顺序 |
| `policy` | 高级 `ToolPolicy`；普通可信插件保持 `None`，不要伪造隔离能力 |
| `confirmation_mode` | 公开确认契约；普通插件保持 `DEFAULT`，变更工具仍自动二阶段确认；`REQUIRED` 可显式记录同一要求，但不会把只读工具改成变更工具 |

模型只控制 `parameters` 声明的公开参数。若 handler 签名中显式包含 `_tool_context`，执行器会注入：

```python
ToolContext(bot, event, request_id=None, confirmed=False)
```

- `bot` / `event` 是当前请求固定的 NoneBot 对象；
- `request_id` 是当前请求编号，可能为 `None`；
- `confirmed` 只有二阶段确认通过后的变更执行才为 `True`。

不要把 `_tool_context` 写进模型 JSON Schema，也不要相信模型传来的同名字段。执行器还会校验模型提供的 URL 参数，拒绝环回、私网、保留地址和云元数据地址。

`ToolConfirmationMode.TRUSTED_LOW_RISK_DIRECT` 不是插件作者的通用开关。Provider 信任层只接受包内 canonical 协议 Builtin 的精确 `ToolSpec` 对象；Registered、Custom File、Generated、MCP 和兼容 Matcher 即使伪造同名枚举也会 fail closed。普通变更工具继续使用默认二阶段确认。

### 正确区分只读与变更

以下行为都应标为 `MUTATING`：发送/撤回消息、戳一戳、改群设置、写库/文件、创建任务、扣费、调用会改变远端状态的 API。首次调用不会执行，系统只保存规范化参数并向原用户发送一次性确认码；同一用户必须在原会话另发 `确认执行 <确认码>`。

同步变更 handler 在 asyncio 超时后无法终止后台线程，因此执行前会被拒绝。变更 handler 必须使用可取消的 `async def`；需要强制杀死进程组的代码应迁移到隔离文件工具。

### `ToolResult` 六字段

handler 推荐返回 `ToolResult`：

| 字段 | 用途 |
| --- | --- |
| `text` | 给模型和用户看的主要文本 |
| `images` | 图片 URL 或 data URL 的 list/tuple，规范化为不可变 tuple |
| `metadata` | 有界 JSON 辅助信息；也会进入结构化模型视图，禁止放密钥 |
| `files` | `ToolResultFile` 或等价映射；locator 必须是 `artifact:`、`attachment:`、`blob:`、`object:`、`result:`、`urn:` 等 opaque 引用，不能是主机路径 |
| `structured` | 有界、可 JSON 序列化的主要结构化结果 |
| `citations` | `ToolResultCitation` 或等价映射；URL 必须是安全公网 HTTPS 地址，系统不会自动抓取 |

六类数据都会脱离原对象、递归校验并冻结；文本和图片还会按工具/全局展示上限裁剪。异常、循环引用、无限浮点、私网引用、主机路径或过深/过大的结构会拒绝整个结果。

## 加载顺序、重名与重载

- 集成插件应在模块导入时先 `require` MoEllmChats，再调用 `register_tool(spec)`；默认 `replace=False`，重复注册同名工具会立即报错。
- `replace=True` 只适合插件明确替换自己先前注册的同名对象，不能用来抢占内置、文件、生成、MCP 或其他插件的名称。
- Registered、Custom File、Generated、MCP、Builtin 和兼容 NoneBot 插件共享全局名称空间。跨来源冲突会使候选 generation 拒绝发布，上一代继续服务。
- 正常启动期间完成的注册会被启动工具重载捕获。进程运行后才动态注册/替换时，再执行 `刷新工具` 或 `重载LLM` 发布新快照。
- 修改 `ToolSpec` 插件的 Python 源码不会由配置 watcher 重新 import，必须按宿主运维流程重启**测试实例**；不要把 `tool_manager.load_custom_tools()` 当公共热更新 API。

## 隔离验收

1. 在隔离 Bot 中加载 MoEllmChats 和集成插件，确认启动无重复注册或候选重载错误。
2. 执行 `设置工具调用 开`，确认工具不在黑名单；测试时可用 `添加常驻插件 describe_current_chat` 避免分类模型漏选。
3. 执行 `刷新工具`，再用 `查看LLM状态` 确认新 generation 已发布。
4. 让普通用户请求“告诉我当前会话信息”，核对工具确实被调用，且返回没有超出声明范围。
5. 将测试工具临时设计为 `MUTATING` 时，确认首次只给确认码、错误用户/会话不能确认、重放和过期均失败。
6. 删除测试常驻项，复核分类、黑名单、`user`/`superuser` 权限和结果上限。

这些步骤只验证测试实例。插件加载、模型实际调用、OneBot API 和生产部署是不同结论。

## 常驻与黑名单

管理员可用：

```text
查看常驻插件
添加常驻插件 <精确工具标识>
移除常驻插件 <精确工具标识>

插件黑名单
添加插件黑名单 <精确工具标识>
移除插件黑名单 <精确工具标识>
```

常驻只跳过分类选择，不跳过 `use_tools`、黑名单、权限、generation 或安全策略。黑名单优先于依赖和常驻。正常业务召回应依赖完整紧凑目录；`resident_plugins` 只用于临时验收、诊断分类漏选或管理员明确要求每轮强制注入，不应把所有插件常驻。

## 相关页面

- [调度链路与运行时架构](./runtime-architecture.md)
- [自定义工具开发](./custom-tools.md)
- [配置参考](./configuration.md)
- [完整指令表](./commands.md)

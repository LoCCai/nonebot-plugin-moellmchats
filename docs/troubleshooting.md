# 故障排查：插件选错、调用失败与提示消息

这一页专门处理“模型说正在做，但实际没做”“选错插件”“日志有调用却没有结果”这类问题。先看类型化状态和安全审计，不要根据模型前置话术猜执行结果，也不要反复发送可能有副作用的同一句话。

## 先看哪几项

在同一请求窗口搜索 `LLM 工具执行审计` 和 `NoneBot 插件兼容调度完成`。安全日志应能对应到以下字段：

- `request`、`tool_call`、`generation`、`directory_digest`：确认日志属于同一请求和同一目录代；
- `selection_source`、`plugin`、`intent_digest`：确认插件来自唯一业务别名还是分类模型；
- `command_preview`、`arguments_digest`：只显示脱敏命令形状和摘要，不会显示完整参数；
- `matcher_checked`、`matcher_matched`、`matcher_failed`、`matcher_blocked`：确认规则是否检查、命中或异常；
- `capture_success`、`api_success`、`api_failed`、`api_unknown`：确认 Adapter 是否真正回调成功；
- `api_read_failed`、`api_read_recovered`、`api_unresolved_failed`、`api_unresolved_unknown`：区分已恢复的只读查询降级和仍未解决的失败；
- `progress_status`、`status`、`retry_decision`、`duration_ms`：进度是否送达与执行是否成功是两件事，最终结果以后四项为准。

日志不会记录完整工具参数、原始 API 参数、Token、Cookie、Authorization、URL 查询或本地路径。如果排查必须看到业务参数，应在目标业务插件内增加经过审查的字段级日志，不要临时打开无差别 payload 日志。

## “今天谁发言最多”选错插件

先确认 QWeb/PicMenu 功能目录中的目标功能声明了对应 `llm_intents`，并且目录版本/摘要已经进入新的 runtime generation。`llm_intents` 是规范化后的精确别名，不是向量搜索：字面变体需要逐条声明。

- `selection_source=business_intent_owner` 且 `plugin=qi_post`：唯一所有者路由正常，后续问题在 command 或 Matcher；
- `selection_source=classification_model`：本次原话没有命中别名，检查别名长度、字段名和目录摘要；
- 日志提示 ambiguous：同一规范化别名被多个插件声明，删除重复所有者或让用户澄清；
- 日志提示 unavailable：所有者隐藏、被拉黑、权限不足或插件未加载。系统会 fail closed，不会退而调用另一个插件。

PicMenu 在 MoEllmChats 初始 generation 后才装入完整目录不再要求重启。默认 watcher 会在一个 `runtime_watch_interval_seconds` 周期内看到摘要变化并发布新 generation；也可以用 `刷新工具` 要求立即收敛。读取异常时上一有效目录继续服务，先修 PicMenu 读取器，不要清空缓存或把所有插件加入常驻。

## 已选对插件，但没有执行

按 `status` 处理：

| 状态 | 说明 | 下一步 |
| --- | --- | --- |
| `not_matched` | 目标插件规则没有命中生成的 command | 检查详细 Schema 中的真实前缀和插件 usage；同参数不要原样重试 |
| `matched_empty` | Matcher 命中，但没有 Adapter 已确认的输出或变更副作用 | 检查插件是否只调用读取 API、只改内存却不返回结果，或忘记发送/返回；同参数不要重试 |
| `failed` | Matcher、handler 或确定失败的 API 发生异常 | 查看目标插件自己的同窗口错误日志；修复后用实质不同参数或新任务验证 |
| `timed_out` | 兼容执行超过预算并已取消 | 查慢规则、外部 I/O 和 `legacy_dispatch_timeout_seconds`；不要先放大超时 |
| `admission_rejected` | 兼容队列没有接纳 | 查活动/等待数和持续时间，等待压力恢复后再发新任务 |
| `result_unknown` | API 可能已经到达外部系统，但响应不确定 | 绝对不要重试同一工具；先从外部状态只读核对 |
| `partial_success` | 已有文本、图片或副作用成功，随后仍有未解决的变更/未知 API、Matcher 异常或不确定副作用 | 保留 observation 中明确列出的已成功部分，不要重放该工具；只用不同工具完成剩余步骤 |

只有 `matched_with_output` 和 `matched_side_effect` 是兼容插件成功。任意非空 metadata、模型说“已执行”或 `on_calling_api` 看到发送参数，都不能把失败改成成功。

一个常见的正常降级是：`get_group_member_info` 失败，插件改用 `get_stranger_info`，随后正文发送成功。此时日志会同时出现 `api_read_failed=1`、`api_read_recovered=1` 和 `status=matched_with_output`，不应再把整步称为失败，也不会封锁同一个业务插件的下一条实质不同命令。如果只读查询失败后没有正文、图片或已确认副作用，则仍是 `failed`，不能用“查询 API 本来只是辅助”伪装成功。

## 命令前缀不对

兼容 Schema 会冻结候选 generation 构建时真实的 NoneBot `command_start`。存在 `/` 时首选 `/`；否则选长度最短、字典序最前的前缀。允许空前缀时，说明会直接移除 `<命令前缀>`，并列出其他有效前缀。

如果日志中的 `command_preview` 仍表现为错误前缀：

1. 确认修改 `COMMAND_START` 后已经发布新 generation；
2. 确认 Schema 和执行日志的 generation 一致；
3. 查看插件真实 Matcher 是否使用 `on_command`、全文匹配还是自定义规则；
4. 不要在菜单里保留字面 `<命令前缀>` 作为让模型猜测的答案。

## 指定网页请求只得到“七七在这里”

先确认原消息是否真正进入 MoEllmChats。高优先级裸链接、媒体解析或自动预览 Matcher 即使最后没有生成结果，也可能提前阻断事件。七七的 `parser_media` 修复后遵守以下规则：

- 明确 `@Bot` / `to_me` 的消息不进入自动视频探测；
- 自动裸链接 Matcher 使用 `block=False`；
- 只有已经存在持久 intake，或 QWeb 返回真实视频候选并由媒体链路接管时，handler 才调用 `stop_propagation()`；
- 普通网页、Parser Lite 路由、无视频、502 或探测异常继续传播给 LLM。

随后在当前 generation 的 Registered 工具目录确认存在 `extract_webpage`，其进度应显示“正在调用注册工具：extract_webpage”。如果工具不在目录，检查宿主 `llm_scripts` 是否被加载、注册是否发生在候选快照构建前，并执行 `刷新工具`；仅在 Python 模块源码变化后按宿主流程重启测试实例。不要把网页工具长期塞进 `resident_plugins` 来掩盖注册或分类失败。

`extract_webpage` 应先用 `safe_public_get` 获取公网 HTML/纯文本，再删除主动内容和 URL 属性，只让共享浏览器池通过 `set_content()` 处理离线文档并阻断全部网络。浏览器池不可用时回退静态正文。不要改成 `page.goto(模型提供的 URL)`：共享池的兼容启动参数本身不构成 SSRF、证书或 DNS rebinding 防护。

## 附加表情报 `retcode=1200`

先区分正文和可选表情。日志“正文已发送，附加表情发送失败并已跳过”表示正文成功，运行时不会重发正文；`retcode=1200` 也不能反推正文失败。

0.26.6 只把至少含一张合格图片的一级目录公布为表情分类。JPEG/PNG/GIF/WebP/BMP 文件必须是非符号链接普通文件，扩展名与文件头一致，大小在 1 字节到 8 MiB 之间；发送前还会使用 no-follow 重新打开并复核。`Thumbs.db`、空文件、伪装扩展名、超大文件和候选发布后损坏文件会直接跳过。如果合格图片仍收到 Adapter 拒绝，则保留该 warning 作为协议端失败证据，不自动重试可选发送，也不让模型用一段“在的”兜底覆盖已经成功的正文。

## 关闭进度消息后是不是没有执行

不是。`tool_progress_messages_enabled=false` 只隐藏固定进度与可选自然话术。以下内容不受影响：

- 二阶段确认消息；
- 目标插件已确认的结果；
- 工具 observation 与最终总结；
- 最终失败反馈；
- 后台安全审计和 Agent/ToolCall 状态。

超级管理员可直接发送 `/设置工具进度 关` 或 `/设置工具进度 开`；别名为 `设置调用进度`、`设置工具提示`，前缀可省略。开启时，每个真正获准的调用会单独显示一条固定提示，例如：

```text
正在投递插件：nonebot_plugin_picstatus_ng｜指令：/zt 拓扑 全部
正在调用搜索工具：web_search
正在调用协议接口：napcat_v11__send_like｜功能：发送点赞
```

如果总开关已经开启但群里仍没有提示，先在同请求日志找 `LLM 工具进度审计`：`status=sent` 表示 Adapter 已确认发送；`timed_out` 或 `failed` 只表示进度发送失败，工具仍会继续；没有该日志通常表示调用在 Schema、权限、信任、重复或策略检查阶段已被拒绝。不要根据模型自己的“我来看看”判断固定提示是否工作。

自然话术默认关闭。超级管理员可发送 `/设置工具自然话术 开`，别名 `设置工具话术`、`设置调用话术`；它只把同一次工具决策中的一句脱敏话术合到固定提示下面，不增加模型请求。总开关关闭时，自然话术开关不会单独发消息。两个固定指令都不经过 LLM、分类或工具链，写入后立即更新当前运行快照；普通群管理员没有权限。不要把任何进度提示当作 API 成功证据。

## PicStatus 一直显示 `zt`、误用 `/状态`，或转去数据库插件

先把三种现象分开：

- 进度消息只显示 `zt` 是旧显示逻辑只取命令首词，不代表每次参数相同。0.26.6 会显示有界、脱敏后的完整指令，例如 `/zt 拓扑 全部`。
- `nonebot_plugin_picstatus_ng` 被正确选中、但 Matcher 连续 `not_matched`，同时审计预览以 `/状态` 开头，说明主模型没有按当前真实入口合成命令。更新后的菜单统一以 `zt` 为规范入口，兼容插件命令合成最低使用中等难度模型；失败 observation 还会重新附上同插件菜单，引导先查 `zt 帮助`、`zt 拓扑 全部` 等发现入口。
- 分类日志只选择了其他工具，但执行日志却出现 `qi_db_analytics`，是旧执行端只查全局 generation、没有查本轮实际 Tool Schema 的漏洞。更新后会在任何进度或 Matcher 之前以 `schema_scope_rejected` 阻断，不会尝试数据库插件。

这类错调和 `resident_plugins` 不是同一件事。先用 `查看常驻插件` 核对；即使常驻列表为空，旧版本仍可能接受模型猜中的全局工具名。不要通过把全部插件常驻来修复，常驻会扩大每轮 Schema 和提示体积。正常恢复顺序是：唯一意图所有者选择 PicStatus → 主模型只看到 PicStatus Schema → 未命中时读取该插件完整菜单 → 先列出拓扑/节点候选 → 再精确查询。

PicStatus 源码默认入口只保留 `运行状态`、`zt`、`yxzt`、`status`。如果宿主显式设置了 `PS_COMMAND`，环境值会覆盖源码默认；升级源码后仍看到 `/状态` 可命中时，应只检查该非敏感配置键并移除旧别名，再按宿主自己的维护流程重载。不要为此改数据库或导入一份重复菜单。

## 为什么系统拒绝重复调用

系统按 generation、工具名和规范化参数摘要记录本任务内的尝试：

- 未命中、空命中、失败或超时：相同工具和相同参数禁止原样重复；
- 结果不确定或部分成功：本任务内整个同名工具禁止再次调用；
- `max_repeated_tool_calls` 也使用同一组 `(generation, 工具名, 规范化参数摘要)` 计数；相同参数达到上限才拒绝，不同实质参数仍可继续；
- 所有调用仍受 `max_tool_rounds` 与 `max_agent_steps` 的总上限约束。

这是为了避免点赞、发消息、群管理等副作用因模型循环而重复发生。需要在修复后重新验收时，应开启一个新任务，并先只读核对外部状态。

## SSE 没有输出或看到坏行日志

0.26.3 的流式解析只接受两种载荷：`data:` 后的 JSON object，或整行就是 JSON object 的 NDJSON。`event:`、`retry:`、注释、空行、数组、字符串/数字标量和无效 JSON 都会跳过，不再触发整轮重试。`data:[DONE]` 和带空格的 `data: [DONE]` 都是正常结束。

坏行日志只包含原因、字节长度和 SHA-256 摘要，不会记录原始 payload。如果供应商发的是数组或自定义控制包，应在 provider 转换层把内容归一为 OpenAI 兼容的 JSON object，不要放宽主链路去解释任意类型。

## 停止请求、连续取消与数据库清理

`停止请求` 先释放对话冷却，再用最多 1 秒尝试发送终止通知，最后仍以 `CancelledError` 结束原任务。通知发送失败或超时不表示取消失败，也不应把取消改记为普通成功。

PostgreSQL transaction 与 spool writer 会让同一个 rollback/close 子任务在连续取消下 settle 完成，然后再传播原取消或业务异常。如果 cleanup 本身失败，日志/类型化错误只记安全异常类型，不记连接字符串。commit 已发出后断连仍是 unknown result，不能自动重试；应根据审计/业务幂等键只读核对。

## 分类缓存不可用或同一请求重复分类

当前 generation 的内存分类缓存会按完整 key 做 single-flight：同键并发只有一个 builder，等待者取消不会取消共享分类，构建失败后会清理 flight 以便新请求重试。不同 key 不互相串行。

目录或分类缓存 lookup/publish/resolve 不可用时，本请求降级为“中等难度、无工具”，而不是绕过缓存直接再调一次分类模型。K-08 的唯一业务意图所有者在此前已经解析，所以精确别名仍可保持业务优先；所有者不可用时仍 fail closed，不改猜其他插件。

## 分类超时后为什么没有第二次请求

0.26.4 把“传输超时”和“响应 JSON 无法解析”分开处理：连接、读取或总请求超时会立即结束本次分类，并降级为“中等难度、无工具”；它不会消耗解析重试，也不会再等待一次完整的 `classification_timeout_seconds`。这能避免一次分类占用接近两倍超时预算。

只有两类可恢复的响应问题会进行最多一次有限重试：首次 HTTP 400 会移除 `json_object` 模式后再试一次；非超时的 JSON/结构解析错误会再试一次。400 响应正文不会读取，其他异常日志只记录尝试次数和安全异常类型，不记录服务商正文、请求参数或凭据。若要定位供应商兼容性，应结合状态码、异常类型和服务商侧 request ID 排查，不要临时把原始响应正文写入群聊或普通日志。

## Custom File 报 `safe_request` 或 network allowlist 错误

先确认工具源码没有导入 `aiohttp/httpx/requests/urllib/socket`，并且 `TOOLS_REGISTRY.capabilities.network` 使用了明确主机列表，例如 `{"allow": ["api.example"]}`。`safe_request` 由 worker 注入，源码不要从插件包导入它，也不能把 `_network_allow`、resolver 或 connector 当作模型参数。

常见拒绝原因包括：URL 主机不在 allowlist，DNS 同时返回公网和私网地址，重定向超出 allowlist，HTTPS 降级，URL 携带凭据，响应压缩/超过 1 MiB，请求体超过 256 KiB，或所有重定向合计超过 15 秒。不要把 allowlist 改成 `*` 来“修好”配置；应确认真实 API 主机和必需重定向域名后逐个声明。

## HTTP 400 明明是参数错误，却显示内容安全拦截

0.26.3 只在结构化错误对象的 `code` 或 `type` 精确归一为已知内容策略标识时，才显示敏感内容提示。普通 `message` 中出现 `audit`、`safety` 或 `content_filter` 字样不会触发；非 JSON 正文也不会用子串猜测。日志和用户错误仍不包含原响应正文、请求参数或凭据。

## 相关页面

- [调度链路与运行时架构](./runtime-architecture.md)
- [NoneBot 插件与 ToolSpec 接入](./plugin-integration.md)
- [配置参考](./configuration.md)
- [安装与隔离验收](./installation.md)
- [OneBot / NapCat 协议工具](./protocol-tools.md)
- [Custom File 与安全 HTTP](./custom-tools.md#安全联网只使用-safe_request)
- [K-09 实施状态](./规划/10-code-review-fixes-20260829.md)
- [K-10 实施状态](./规划/12-llm-runtime-incident-20260901.md)

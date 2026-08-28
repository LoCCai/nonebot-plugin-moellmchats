# 故障排查：插件选错、调用失败与提示消息

这一页专门处理“模型说正在做，但实际没做”“选错插件”“日志有调用却没有结果”这类问题。先看类型化状态和安全审计，不要根据模型前置话术猜执行结果，也不要反复发送可能有副作用的同一句话。

## 先看哪几项

在同一请求窗口搜索 `LLM 工具执行审计` 和 `NoneBot 插件兼容调度完成`。安全日志应能对应到以下字段：

- `request`、`tool_call`、`generation`、`directory_digest`：确认日志属于同一请求和同一目录代；
- `selection_source`、`plugin`、`intent_digest`：确认插件来自唯一业务别名还是分类模型；
- `command_preview`、`arguments_digest`：只显示脱敏命令形状和摘要，不会显示完整参数；
- `matcher_checked`、`matcher_matched`、`matcher_failed`、`matcher_blocked`：确认规则是否检查、命中或异常；
- `capture_success`、`api_success`、`api_failed`、`api_unknown`：确认 Adapter 是否真正回调成功；
- `status`、`retry_decision`、`duration_ms`：以这里判断最后结果和是否允许再次尝试。

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
| `partial_success` | 已有文本、图片或副作用成功，随后又失败 | 保留已成功部分，不要重放该工具；只用不同工具完成剩余步骤 |

只有 `matched_with_output` 和 `matched_side_effect` 是兼容插件成功。任意非空 metadata、模型说“已执行”或 `on_calling_api` 看到发送参数，都不能把失败改成成功。

## 命令前缀不对

兼容 Schema 会冻结候选 generation 构建时真实的 NoneBot `command_start`。存在 `/` 时首选 `/`；否则选长度最短、字典序最前的前缀。允许空前缀时，说明会直接移除 `<命令前缀>`，并列出其他有效前缀。

如果日志中的 `command_preview` 仍表现为错误前缀：

1. 确认修改 `COMMAND_START` 后已经发布新 generation；
2. 确认 Schema 和执行日志的 generation 一致；
3. 查看插件真实 Matcher 是否使用 `on_command`、全文匹配还是自定义规则；
4. 不要在菜单里保留字面 `<命令前缀>` 作为让模型猜测的答案。

## 关闭进度消息后是不是没有执行

不是。`tool_progress_messages_enabled=false` 只隐藏模型前置话术和“正在搜索/调用/执行”消息。以下内容不受影响：

- 二阶段确认消息；
- 目标插件已确认的结果；
- 工具 observation 与最终总结；
- 最终失败反馈；
- 后台安全审计和 Agent/ToolCall 状态。

如果希望恢复原来的可见过渡提示，把该项设回 `true` 并发布新 generation。不要把进度提示当作 API 成功证据。

## 为什么系统拒绝重复调用

系统按 generation、工具名和规范化参数摘要记录本任务内的尝试：

- 未命中、空命中、失败或超时：相同工具和相同参数禁止原样重复；
- 结果不确定或部分成功：本任务内整个同名工具禁止再次调用；
- 不同工具或实质不同参数仍可继续，但仍受总步骤和同工具次数上限约束。

这是为了避免点赞、发消息、群管理等副作用因模型循环而重复发生。需要在修复后重新验收时，应开启一个新任务，并先只读核对外部状态。

## 相关页面

- [调度链路与运行时架构](./runtime-architecture.md)
- [NoneBot 插件与 ToolSpec 接入](./plugin-integration.md)
- [配置参考](./configuration.md)
- [安装与隔离验收](./installation.md)
- [OneBot / NapCat 协议工具](./protocol-tools.md)

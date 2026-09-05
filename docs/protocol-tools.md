# OneBot / NapCat 协议工具

协议工具让 LLM 在当前 Bot 的真实协议能力范围内发现并调用固定 OneBot 动作。它不是一个通用 `call_api(action, params)`：每个工具在包内固定唯一 API 名、严格 JSON Schema、权限、场景、确认、限额和脱敏策略，模型只能填写该工具公开的参数。

默认总开关是关闭的。安装 0.26.0 不会自动开放任何协议动作；管理员必须在隔离测试实例中显式设置 `protocol_tools_enabled=true`，并同时开启模型工具调用。

0.26.2 的 `tool_progress_messages_enabled` 只控制“正在执行”类显示，不改变本页任何权限、确认、限额或 Broker 状态。即使把进度提示关闭，需要二阶段确认的协议动作仍会直接向原会话发送确认消息；`result_unknown` 仍禁止重试。

## 支持矩阵

| 当前 Bot | 如何识别 | 包内完整清单 | 本次请求实际出现的动作 |
| --- | --- | ---: | --- |
| OneBot v11 通用实现 | Adapter 是 OneBot v11；请求开始以独立 3 秒上限调用一次 `get_version_info` | 38 个 v11 公开动作 | 只保留人工策略允许、当前用户/场景可执行的 v11 动作 |
| NapCat v11 4.18.19 | v11 `get_version_info.app_name` 必须精确等于 `NapCat.Onebot` | 175 个 NapCat 动作，另复用 v11 标准动作 | 在 v11 动作上增加允许的 NapCat 扩展；近似名称不会误判成 NapCat |
| OneBot v12 | Adapter 是 OneBot v12；请求开始以独立 3 秒上限调用一次 `get_supported_actions` | 31 个 v12 标准动作 | 包内清单与 Bot 当次返回动作的交集 |
| 其他 Adapter 或探测失败 | 不支持或探测抛错/返回非法数据 | 清单仍随包存在 | 当前请求不提供协议工具，普通聊天和业务插件不受影响 |

“完整清单”用于离线审核和管理员查阅，不表示全部动作可执行。当前清单总计 244 项；永久拒绝项不会进入任何 LLM Schema，普通用户也看不到超级管理员动作。完整逐项表见[动作总表](./protocol-actions.md)。

插件元数据同时声明 `~onebot.v11` 和 `~onebot.v12`。v11 的数字 ID 会在协议调用时恢复成整数；v12 的 Bot、用户、群、频道和消息 ID 始终按字符串处理，并兼容嵌套 `self.user_id`、`datetime`、`mention`、`reply` 和图片 `file_id`。

## 四个配置开关

在 LocalStore 的 `config.json` 中配置：

```json
{
  "protocol_tools_enabled": false,
  "protocol_tools_napcat_extensions_enabled": true,
  "protocol_tools_low_risk_direct_enabled": true,
  "protocol_tools_business_first": true
}
```

| 字段 | 默认值 | 通俗含义 |
| --- | ---: | --- |
| `protocol_tools_enabled` | `false` | 总开关。为 false 时不探测协议，也不向模型显示任何协议工具 |
| `protocol_tools_napcat_extensions_enabled` | `true` | 识别到真正 NapCat 后是否加入其扩展动作；总开关关闭时本项没有效果 |
| `protocol_tools_low_risk_direct_enabled` | `true` | 是否允许三个固定当前目标的低风险封装在限额内直接执行；设为 false 后它们也需要二阶段确认 |
| `protocol_tools_business_first` | `true` | 用户原话命中已加载业务插件的菜单触发词时，先用业务 Matcher，并从本轮协议候选中去掉冲突动作 |

这些字段必须是 JSON 布尔值，不能写成字符串 `"true"`。修改后由 watcher 原子发布新 generation，也可在测试实例执行 `重载LLM`。完整基础配置见[配置参考](./configuration.md)。

## 调度链路

```text
收到当前 OneBot 事件
  → 异步探测实现和支持动作，生成不可变能力快照
  → 结合当前用户、SUPERUSER、群/私聊、消息与回复范围过滤短目录
  → 已加载业务插件的规范化菜单触发词优先消除冲突
  → 分类模型只选择简短工具名
  → 仅展开被选中动作的完整参数 Schema
  → 协议 Broker 复核固定 API、参数、能力、目标、权限、确认和限额
  → 最多调用一次 Bot API
  → 截断、脱敏结果并写入无 payload 审计摘要
```

目录和 Schema 缓存键包含协议、实现、实现版本、支持动作摘要、Adapter、Bot、调用者、SUPERUSER 身份、场景、群/频道、当前消息、回复消息和 runtime generation。因此不同 Bot、协议、消息或权限不会共用协议目录。

菜单解决的是“用户想做什么”，协议 Schema 解决的是“这个固定 API 可以填什么参数”。例如七七已加载的 `qi_group_admin` 菜单声明“点赞 / 赞我 / 给我点赞”后，“给我点个赞”应优先进入该业务 Matcher，继续使用它自己的次数、黑名单和业务检查；`qq__like_me` 只是没有唯一业务插件命中时的安全后备。菜单不能授予协议权限，也不会把未加载插件变成工具。

## 权限、确认和范围

| 类别 | 普通用户 | NoneBot `SUPERUSER` | 是否确认 |
| --- | --- | --- | --- |
| 当前用户、当前群、当前回复消息的低风险读取 | 只在事件能固定目标时可见 | 可见 | 不确认，但有读取限额 |
| `qq__like_me` | 目标强制为发起用户本人 | 同普通用户 | 默认限额内直执；可由配置改为确认 |
| 当前会话戳一戳、当前消息表情回应 | 仅 NapCat 且当前事件能固定用户/群/消息时可见 | 同普通用户 | 默认限额内直执；可由配置改为确认 |
| 好友/群列表、历史、全局资料等隐私读取 | 不可见、不可执行 | 只显示审核策略允许的动作 | 只读动作不确认，但有独立限额 |
| 发送消息、处理请求、群管理、改账号资料 | 不可见、不可执行 | 可见 | 必须另发一次性 `确认执行 <6位码>` |
| 凭证、原始发包、事件拉取、生命周期、任意文件/URL/路径/二进制 | 永久拒绝 | 仍永久拒绝 | 不会生成可确认操作 |

OneBot v12 事件不能稳定证明“发起用户是当前群管理员”。因此协议 Broker 不使用群角色授权 v12 管理动作；所有群管理统一要求 NoneBot `SUPERUSER`，适配器和 QQ 端仍会复核 Bot 自身是否有权限。

二阶段确认记录绑定 Bot、Adapter、协议、实现及版本、支持动作摘要、用户、会话、runtime generation、固定动作、固化参数和策略摘要。确认时会重新探测并重做全部检查；过期、重放、换 Bot、换用户、换群、generation 变化、能力变化或策略变化都会拒绝且不会执行。

## 三个普通用户安全封装

| 工具 | 模型可填参数 | 系统强制注入 | 默认限额 |
| --- | --- | --- | ---: |
| `qq__like_me` | `times`，1～10 | 当前发起用户 | 同一 Bot、发起者、目标和 UTC 日期合计 10 次 |
| `qq__poke_current` | 无 | 当前用户和当前群/私聊 | 同一当前场景 60 秒 3 次 |
| `qq__react_current_message` | `emoji_id` | 当前触发消息 ID | 同一消息 60 秒 6 次 |

模型不能传 `user_id`、`group_id` 或 `message_id` 覆盖目标；多余字段会在 API 调用前被严格 Schema 拒绝。NapCat 没有对应动作、当前消息缺少 ID 或场景不符时，封装不会出现。

所有开放协议动作都有有界限额，单次协议参数的规范化 JSON 也不得超过 64 KiB。限额检查是原子的；副作用一旦开始就不退还额度。超时、断连或取消导致响应状态无法确定时返回 `result_unknown`，保留额度并在窗口内禁止同目标重试，避免重复点赞、发消息或管理操作。

## 永久拒绝与结果脱敏

以下类别只保留在 244 项管理员总表，不进入 LLM Schema，即使调用者是 `SUPERUSER` 也不能开放：

- Cookies、CSRF、credentials、clientkey、rkey 和类似凭证；
- `send_packet`、隐藏 quick operation 和其他原始发包入口；
- `get_latest_events` 等事件拉取；
- 退出、重启和进程生命周期动作；
- 接受或返回任意本地路径、URL、Base64、上传/下载文件及大块二进制的动作；
- 无可靠单目标上限的批量群管理动作，例如 NapCat `set_group_kick_members`；应改用逐次确认、限额明确的单人 `set_group_kick`；
- 以下划线、点开头或审核为内部实现的隐藏动作。

协议结果递归限制深度、节点、数组项和总字符数，并脱敏字段名或内容中的 token、Cookie、Authorization、credentials、CSRF、clientkey、rkey、路径及大块 Base64。审计只保存有界身份、状态、策略摘要和结果摘要，不保存原始参数或响应 payload。

## 消息、点赞和可选表情

- 开放的 7 个协议消息动作只接受非空纯文本，最长 4000 字符；消息段、文件对象、多余参数和相互冲突的目标字段都会在 Broker 调用前拒绝。
- v11 / NapCat 消息调用由 Broker 强制加入 `auto_escape=true`。像 `[CQ:image,file=file:///etc/passwd]` 这样的内容只会按普通文字发送，不会被解析成图片或文件；模型不能覆盖该字段。v12 同样不接受模型构造的文件消息段。
- v11 兼容 Matcher 捕获 `send_msg`、`send_group_msg`、`send_private_msg`；v12 捕获 `send_message`。
- `send_like` 不是消息输出，捕获钩子不会吞掉它。隔离测试使用真正 NoneBot Matcher 验证“给我点赞”只调用一次 `send_like`，但这不等于七七线上已验收。
- 正文发送失败继续向上抛出。正文成功后，可选表情遇到 `ActionFailed`、`NetworkError` 或 `ApiNotAvailable` 时只跳过该表情，不重发正文。
- v12 本地表情目录里的文件不是协议实现签发的 `file_id`；没有可用 `file_id` 时直接跳过可选表情，不把正文判成失败。

## 模型配置和发现失败排查

协议工具仍走标准 Function Calling，所以至少需要：

1. 在 `providers.toml` 配置一个真实支持工具调用的模型；
2. 在 `model_config.json` 设置 `selected_model`，并把 `use_tools` 设为 true；
3. 保持目标动作不在 `tool_blacklist`；
4. 再在 `config.json` 显式开启 `protocol_tools_enabled`。

模型配置步骤和示例见[配置参考](./configuration.md#五分钟最小配置)。若聊天正常但看不到协议工具，依次检查总开关、当前 Adapter、能力探测 warning、NapCat 精确实现名、v12 `get_supported_actions`、用户是否为 `SUPERUSER`、当前场景/回复消息、业务菜单冲突、黑名单和模型 `use_tools`。

不要把全部协议工具加入常驻。完整短目录会按当前能力自动参与分类，选中后才展开 Schema；常驻只适合短期隔离诊断，不能绕过权限、确认或能力快照。

## 来源、生成和离线运行

清单固定到 OneBot v11 `d4456ee…`、OneBot v12 `d533f0f…`、NoneBot Adapter OneBot 2.4.6 `3ac943f…` 和 NapCatDocs `14ad689…`。NapCat 权威输入是 `src/api/4.18.19/openapi.json`，SHA-256 为 `905ff1faa265cdfa6401a91e8ed832ab15c9e32a7683c42dc11bb6752682ae39`。

维护者生成命令只读取这些固定 checkout；运行时和普通构建只读取包内 JSON，不联网。技术动作清单与人工安全策略分开保存，动作集合变化、重名、缺少策略、未审核策略或生成文件漂移都会失败。上游来源均按包内 [`NOTICE.md`](../nonebot_plugin_moellmchats/protocol_resources/NOTICE.md) 标注 MIT 归属。

## 相关页面

- [244 项动作总表](./protocol-actions.md)
- [配置参考](./configuration.md)
- [调度链路与运行时架构](./runtime-architecture.md)
- [NoneBot 插件、菜单与 ToolSpec 接入](./plugin-integration.md)
- [安装与隔离验收](./installation.md)

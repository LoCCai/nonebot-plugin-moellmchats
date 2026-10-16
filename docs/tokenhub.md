# 腾讯 TokenHub 模型与工具

使用广州 OpenAI 兼容入口 `https://tokenhub.tencentmaas.com/v1`。`providers.toml` 可以引用宿主 NoneBot 配置中的密钥，解析后的密钥只保留在内存中：

```toml
[providers.tencent]
base_url = "https://tokenhub.tencentmaas.com/v1"
api_key_config = "txhy_api_key"
discover_models = false
models = ["deepseek-v4.1-flash"]

[providers.tencent.model_configs."deepseek-v4.1-flash"]
model_type = "chat"
is_vision = false
no_tools = false
extra_payload = { thinking = { type = "disabled" } }
```

宿主 `.env.prod` 中的 `txhy_api_key` 是唯一密钥来源。`api_key_config` 也适用于其他服务商；原 `api_key` 字符串和列表继续支持。`discover_models=false` 让手动能力目录保持稳定；`disabled_models` 可以排除已确认不可用的模型，远程缓存及下次刷新不会将其重新加入。

`model_type` 区分 `chat`、`image`、`video`、`3d`、`speech`、`music`、`transcription`、`embedding`、`multimodal_embedding`。非对话模型不能加入分类/聊天池，也不能设置为识图或聊天固定模型。历史未声明类型的模型保持兼容。

## 管理

`!查看LLM链路` 展示各角色、模型池、备用链、思考参数和额度状态。`!查看模型 tencent` 查看腾讯模型及编号。原分类与 0/1/2 档模型池仍支持跨供应商轮转。

`!设置媒体模型 类型 模型名或编号` 支持：绘图、多模态、群聊总结、文本向量、多模态向量、语音识别、语音合成、视频生成、三维生成、音乐生成。对应角色可以填 `关闭`。固定模型的备用链使用 `model_fallbacks`。

腾讯 `401007`（无免费资源包）、`401008`（免费额度用完）、`403004`（余额不足）的 HTTP 402 响应会持久排除对应模型，随后尝试同类模型。403 权限、认证或内容拒绝不冒充额度耗尽。接口不会为账号开通后付费。独立联网搜索资源包用完时，不会误停仍可聊天的模型。

GLM 5.3 系列不能关闭思考，低延迟请求可设 `reasoning_effort=low`；Kimi Code 2.7 同样不能关闭思考。Kimi K3 使用 `max_completion_tokens` 和 `reasoning_effort`。切换模型会清理上一模型的输出预算字段，并使用下一模型自己的参数。

## AI 工具

- `tokenhub_capabilities`：查看已配置的能力、模型和离线参数文档。传 `operation` 和 `page` 分页读取具体参数。
- `call_model_capability`：按指南提交原生参数，支持绘图、视频、3D、世界模型、识别、语音、音色、配音、音乐和向量。接口路径固定来自包内目录，AI 不能任意设置端点或凭据。
- `get_model_task`：使用返回的 `task_ref` 查询结果。任务在当前 Bot、群/私聊和用户范围内保存 7 天，重启后仍可查询。超时或中断产生的提交不确定状态不会自动重提；同一条用户消息、同一能力和参数也不会重复提交。
- `semantic_search`：对提供的文字段落做向量检索，直接返回排序及原文。
- `compare_embeddings`：比较已生成的同模型向量引用，支持文本、图片、视频，不把大段向量数字发给主模型。
- `tokenhub_web_search`：调用腾讯联网搜索轻量版，返回文字及来源，需要账号已有搜索资源包。

参考图和音视频可通过 `read_message_content` 取得当前/引用消息的 `asset_ref`。在原生参数值中传 `{"asset_ref":"media_..."}` 会转换为 data URI；传 `encoding="base64"` 生成裸 Base64，适用于 ASR 等原生字段。文件路径、私网素材地址及自定义回调不接受。

图像、视频、3D 生成沿用宿主绘图手动授权，并在等待生成槽位后复查授权。常用 `generate_image` 直接发送图片；扩展原生接口返回任务结果和下载链接供 AI 继续处理。当前会话的语音附件在配置语音识别模型后先转写，再交给资料分析模型，避免把音频错传给只支持图片/视频的视觉模型。

当前能力目录依据腾讯公开指南固定 52 种调用方式，覆盖 81 个非对话模型。参数文档和目录随代码发布；存在于平台 `/models` 列表并不代表账号已领取该模型的免费资源包。

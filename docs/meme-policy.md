# 群表情包策略

宿主可调用 `register_meme_authorizer(async_callback)`，按实际 `ToolContext` 和模板 key
判断是否允许生成。检查发生在模板读取、素材下载和生成请求之前；回调失败或超过
5 秒时拒绝，任务取消仍正常传播。

需要强制接入宿主策略时设置 `MOELLM_MEME_POLICY_REQUIRED=true`。此时未注册回调也会拒绝。
独立安装默认保持原有行为。七七的回调读取 Bison 实时群模板开关，私聊沿用原有设置。

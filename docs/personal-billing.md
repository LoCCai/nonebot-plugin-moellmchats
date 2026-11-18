# 宿主个人计费适配

插件提供可选的 `nonebot_plugin_moellmchats.personal_billing.register(adapter)` 接口。
未注册时继续使用原有模型、密钥与额度配置；此接口不包含任何站点凭据、账户数据库或登录实现。

适配器由宿主实现并在启动时注册，需提供：

- `event_scope(bot, event)`：异步上下文管理器，鉴权并建立当前付款人作用域；用 `ContextVar` 隔离并发请求，退出时恢复。
- `active()`、`snapshot()`：报告当前是否使用个人计费，并返回可持久化的付款人引用；快照中不得包含密钥。
- `model(key, has_tools=False, has_images=False)`：按个人设置选择模型与分组，检查工具及图片输入要求。
- `config(key, default)`：提供个人 MoE 开关、三档思考配置等设置；未覆盖时返回默认值。
- `post(session, model, *args, **kwargs)`：异步上下文管理器，在实际请求边界验证付款权限并传输；鉴权失败须明确报错。
- `recovery_scope(saved)`：异步上下文管理器，重新鉴权快照中的付款人，恢复时不得改用其他账户。

个人计费作用域优先于公共模型池和授权聊天模型。个人模型不可用时停止该次调用，不回退公共密钥。
已有个人付款人记录但宿主未注册适配器时，恢复任务会拒绝执行。
分类共享缓存不参与个人计费请求，避免跨付款人复用分类结果。

图片生成另通过 `media_tools.register_image_generation_billing(handler)` 接入宿主结算；
异步回调接收 `reserve`、`succeed`、`failed` 或 `unknown` 操作及工具上下文，
处理预留、成功、明确失败和结果未知。授权使用 `register_image_generation_authorizer`。
未支持的个人语音及 TokenHub 调用会拒绝执行。
目录能力与价格信息、用户权限、余额以及计费倍率由宿主和计费网关负责校验。

离线回归见 `tests/test_personal_billing.py`。七七宿主投递集成测试仅在显式设置
`MOELLM_TEST_HOST_ROOT` 且宿主依赖齐全的隔离测试环境运行。

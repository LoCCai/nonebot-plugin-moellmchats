# 七七 / QWeb 专用分支

本仓库维护两条用途明确的分支：

| 分支 | 用途 |
| --- | --- |
| `main` | 通用 NoneBot2 插件，独立配置模型、人设及工具，无需 QWeb 或 New API 账户体系 |
| `qiqi` | 七七部署使用，适配 QWeb 管理台、管理员默认模型、个人模型设置与 New API 结算 |

`qiqi` 由原 `feat/meme-manager-emotions-20261118` 分支更名，保留原提交历史，
并收录已经上线的管理员默认模型修复。其他历史开发分支保留供追溯。
`main` 基于原通用维护线 `471b48b`，没有把七七专用行为混入通用版本。

## 安装与升级

七七部署依赖应固定本分支经过验收的完整提交 SHA；不要使用 `main` 或浮动分支
替换正在运行的插件。开发时可按以下来源安装：

```bash
pip install 'nonebot-plugin-moellmchats @ git+https://github.com/LoCCai/nonebot-plugin-moellmchats.git@qiqi'
```

生产部署仍须核对 QWeb 宿主与插件的接口兼容性、运行回归，再按既有流程加载。
更名和推送代码本身不会重启七七或改变线上计费。

## QWeb 适配边界

- `personal_billing.register(adapter)` 接收宿主的付款人与模型路由适配器，协议见
  [个人计费接口](personal-billing.md)。个人设置不可用时不切换付款人。
- 宿主可通过 `config("administrator_default", False)` 标识管理员默认配置。
  此配置供普通用户使用时仍遵守普通用户的模型选择与工具权限，不提升为超级管理员。
- 宿主的可选 `error_message(error)` 返回可展示的权限、余额或模型配置提示。
  未识别异常继续交由原处理链处理，禁止将原始凭据或上游异常正文直接作为提示。
- 绘图继续使用原有授权和结算回调；QWeb 负责登录、价格目录、权限、余额及模型分组。
- `emotion_source="meme_manager"` 使用表情管理器当前选中的资源包和分类词库；
  表情资源管理由 QWeb 管理员入口承载，
  不要求用户另行登录表情管理器，也不在插件仓库保存站点密钥。
- `qweb_resources.emotion_settings()` / `save_emotion_settings()` 提供严格限定的
  表情开关、概率、来源与版本号。QWeb 负责管理员鉴权、CSRF 和审计；保存采用
  原有原子配置写入并更新运行快照，旧版本提交拒绝覆盖新设置。

通用修复应单独回移到 `main` 并通过独立插件测试；七七宿主专用功能留在 `qiqi`。

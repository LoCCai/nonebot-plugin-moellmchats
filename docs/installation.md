# 安装、升级与测试验收

本文说明如何安装 0.25 候选版、让 NoneBot 正确加载插件，以及怎样在不触碰生产的前提下做隔离验收。

## 先看版本状态

截至 2026-08-27：

- 当前完成精确远端双门禁的候选提交是 `bbc3963a361259f4d98c29003937afb1cbe976f9`。
- 该提交包含 PicMenu/QWeb 功能级意图发现、普通用户隐藏项过滤和 OneBot 表情发送降级；Python 3.10～3.13、mandatory root sandbox、wheel/sdist 构建与 Python 3.10/3.12 × wheel/sdist 包外 smoke 均已通过。
- push run `33066587717` 与 PR run `33080256433` 各 11/11 success、`non_success=[]`，并各恰好一个 `completed/success release-gate`。
- PR #2 已于 2026-08-26 合并到本仓库自己的 `feat/llm-runtime-backpressure` 集成分支，merge commit 为 `c78ef06190d2df1d77c2ada6d9f06020ef6b37ca`；本轮 PR #3 以该分支为 base，当前为 Open/Clean。上游和默认 `master` 都不是本轮集成 base。
- 包内版本号是 `0.25.0`，但 PyPI 当前最新正式版仍是 `0.22.3`。因此 `pip install nonebot-plugin-moellmchats` 不会得到本候选版。
- 这些证据说明“开发制品可以进入隔离测试”，不等于它已经在七七或其他生产 Bot 中部署验证。

隔离测试应把依赖固定到上面的完整提交 SHA；不要改用移动分支，也不要把本地脏工作树或未绑定提交的临时制品当作可恢复安装来源。

## 运行前提

普通聊天至少需要：

- Python 3.10 或更高版本；当前门禁实际覆盖 3.10、3.11、3.12、3.13。
- NoneBot 2.4.4+ 与 OneBot V11 adapter 2.4.6+。
- 能访问所配置的 OpenAI Chat Completions 兼容 API。
- 支持 owner、mode bit 和 no-follow 检查的 POSIX 文件系统；当前不支持 Windows。

如果要运行 `custom_tools/` 或 AI 生成工具，还需满足严格的 [Linux runner 前提](./custom-tools.md#runner-隔离边界与运行要求)。这些前提不满足时，文件/生成工具会 fail closed；普通聊天、MCP 和可信 `ToolSpec` 不会因此自动获得更宽权限。

完整 Python 包和系统依赖见[依赖与运行前提](./dependencies.md)。

## 安装当前双门禁候选提交

以下命令会修改当前项目的依赖声明和锁文件，只应在测试分支或隔离副本中执行。

### 使用 uv（推荐）

```bash
uv add "nonebot-plugin-moellmchats @ git+https://github.com/LoCCai/nonebot-plugin-moellmchats.git@bbc3963a361259f4d98c29003937afb1cbe976f9"
```

然后确认锁文件中的 source 末尾确实是目标 SHA，而不是只有分支名：

```bash
uv tree | grep nonebot-plugin-moellmchats
grep -A3 'name = "nonebot-plugin-moellmchats"' uv.lock
```

### 使用 pip

建议先创建专用虚拟环境，再安装精确提交：

```bash
python3 -m venv .venv-test
.venv-test/bin/python -m pip install --upgrade pip
.venv-test/bin/python -m pip install \
  "nonebot-plugin-moellmchats @ git+https://github.com/LoCCai/nonebot-plugin-moellmchats.git@bbc3963a361259f4d98c29003937afb1cbe976f9"
```

## 让 NoneBot 加载插件

安装 Python 包不代表 NoneBot 已经加载插件。请按项目现有方式，把模块名加入插件列表：

```text
nonebot_plugin_moellmchats
```

若项目使用 `pyproject.toml` 的 nb-cli 插件表，形式通常如下：

```toml
[tool.nonebot.plugins]
nonebot-plugin-moellmchats = ["nonebot_plugin_moellmchats"]
```

不要同时从两个目录或两个包名重复加载同一插件。

NoneBot 的 `.env` 至少应提供超级管理员和昵称：

```dotenv
SUPERUSERS=["123456789"]
NICKNAME=["七七", "机器人"]

# 测试实例推荐把 LocalStore 固定在测试工作目录内，避免碰到正式配置。
LOCALSTORE_USE_CWD=true
```

这里的 `SUPERUSERS`、`NICKNAME` 和 `LOCALSTORE_USE_CWD` 都是 NoneBot/LocalStore 配置，不是模型 API Key。变量意义和配置目录规则见[配置参考](./configuration.md#nonebot-env-变量)。

## 首次配置顺序

1. 用独立工作目录、独立虚拟环境和独立 LocalStore 启动测试实例，禁止复用生产配置目录。
2. 插件首次加载后会生成配置模板。`LOCALSTORE_USE_CWD=true` 时，目录通常为 `<Bot 工作目录>/config/nonebot_plugin_moellmchats/`。
3. 先填写 `providers.toml`，再通过 `查看模型` 确认模型 ID。
4. 编辑 `model_config.json`，至少确认聊天模型；图片、分类、总结、MoE 和工具模型按需要设置。
5. 执行 `重载LLM`，再执行 `查看LLM状态`。候选文件校验失败时会保留上一代快照，不要反复重启掩盖错误。
6. 依次验证纯文本、上下文、图片、只读工具、变更型工具确认、重载和停止请求。

`providers.toml` 中只要存在非空 API Key，启动后台刷新就可能访问对应服务商的 `/models`。自动模板里的 `sk-xxxxxx` 也是非空字符串：如果第一次就运行真实 driver、又没有先改模板，它会带着占位值尝试访问示例中的 DeepSeek/OpenAI 地址。需要零外部 I/O 时，应先用下一节的无 driver smoke 生成模板，删除未使用的 Provider、把其余占位值换成隔离测试凭据后，再启动测试 Bot；不要为了 smoke 填入生产凭据。

## 零模型、零数据库的加载 smoke

下面的 smoke 只初始化 NoneBot、加载包并生成一套临时配置，不启动 Bot driver，不连接模型、PostgreSQL 或 Redis：

```bash
TEST_DIR="$(mktemp -d)"
cd "$TEST_DIR"
DRIVER='~none' \
LOCALSTORE_USE_CWD=true \
NICKNAME='["SmokeBot"]' \
SUPERUSERS='["1"]' \
python - <<'PY'
import nonebot

nonebot.init()
assert nonebot.load_plugin("nonebot_plugin_localstore") is not None
assert nonebot.load_plugin("nonebot_plugin_moellmchats") is not None
print("PLUGIN_LOAD_OK")
PY
```

这只证明“当前 Python 环境可以导入插件”，不证明 OneBot 连接、模型请求、Linux 隔离或真实用户行为。

## 隔离功能验收清单

建议按以下顺序推进；前一步失败就停止，不继续扩大测试范围。

| 阶段 | 要验证的内容 | 通过标准 |
| --- | --- | --- |
| 1. 锁定 | 依赖和 source SHA | 锁文件明确解析到 `bbc3963…` |
| 2. 加载 | NoneBot 插件加载 | 无 import/config 权限错误，能生成独立配置目录 |
| 3. 模型 | 测试服务商与模型 | `查看模型`、`查看配置` 正确；纯文本回复成功 |
| 4. 调度 | 分类、视觉、MoE | 各角色使用预期模型；缺能力时明确拒绝或回退 |
| 5. 发现目录 | PicMenu/QWeb、Metadata 菜单、覆写优先级 | `刷新工具` 后自然语言能召回菜单功能；未加载插件不进入工具目录 |
| 6. 工具 | Custom File、ToolSpec、MCP、Matcher 兼容 | 只暴露预期工具；黑名单、权限、结果上限生效；不提供任意 `bot.call_api` |
| 7. 确认 | `mutating` ToolSpec/文件工具 | 首次只给确认码；原用户在原会话另发确认后才执行 |
| 8. OneBot 投递 | 正文与可选表情 | 正文失败可见；正文成功后单个表情 `ActionFailed` 不重发正文、不拖垮整轮 |
| 9. 隔离 | 文件/生成工具 runner | `查看LLM状态` 显示 `isolation=ready`，隔离探针无跳过 |
| 10. 运维 | 重载、取消、退回 | 坏配置保留旧 generation；请求可停止；回退路径已演练 |

必须区分以下结论：

- CI green：代码和制品门禁通过。
- package smoke：包外环境可以安装和加载。
- 测试实例验收：测试 Bot 实际完成了功能链路。
- 生产验证：生产部署后的真实行为；本任务没有执行。

## 升级与回退

升级前应记录：

- 当前依赖 source SHA；
- `pyproject.toml` 与锁文件的版本控制状态；
- 独立测试配置的备份位置；
- 当前 `generated_tools/lifecycle_state.json` schema 和 revision。

升级时只替换一个明确的 SHA，重新生成并提交锁文件，再重复上面的验收清单。不要把旧版进程和 schema v3 新版进程同时指向同一个 LocalStore 配置目录。

回退时把依赖恢复到升级前记录的精确 SHA，并使用与该 SHA 匹配的锁文件。若新版已经持久化 schema v3，旧版进程可能无法读取；不要用旧进程直接覆盖 canonical 生命周期文件。先停止测试实例，保留配置副本，再按维护者确认的兼容路径处理。

## 立即停止的情况

出现以下任一情况时，不应继续进入生产：

- 锁文件解析到的提交不是计划中的完整 SHA；
- `查看LLM状态` 没有活动 runtime generation；
- 需要文件/生成工具，但 `isolation` 不是 `ready`；
- 启用的 MCP 无法发现、工具名冲突或候选重载失败；
- 变更型工具没有走独立确认消息；
- 测试实例意外读取了生产 LocalStore、API Key、PostgreSQL DSN 或 Redis URL；
- 只能通过放宽目录权限、跳过 sandbox 测试或复用生产进程才能继续。

## 下一步

- [五分钟模型配置](./configuration.md#五分钟最小配置)
- [调度链路与运行时边界](./runtime-architecture.md)
- [自定义工具开发](./custom-tools.md)
- [NoneBot 插件与 ToolSpec 接入](./plugin-integration.md)

# 安装、升级与测试验收

本文说明如何安装精确候选提交、让 NoneBot 正确加载插件，以及怎样在不触碰生产的前提下做隔离验收。

## 先看版本状态

截至 2026-09-01：

- 当前开发工作树版本是 0.26.6，K-12 的网页/表情安全和 K-13 的完整指令、本轮 Schema 强制许可、菜单恢复与 PicStatus 入口收口均已完成完整本地门禁。尚未创建精确实现提交或取得远端门禁，因此本页不会提供一个臆造的 0.26.6 安装 SHA。
- 0.26.5 已完成 K-11 实现，新增逐调用固定进度、可选自然话术和只读失败恢复真实性；精确实现提交的 push/pull_request 双 Actions 已全绿。
- 当前已验证、可恢复的隔离候选是 0.26.5 精确 Git 提交 `e704092a1e8d9ad215e4e9de35a9fe403483d56f`，下方命令全部固定到该 SHA，不使用移动分支。
- 0.26.5 保留此前全量 OneBot/NapCat 协议工具、固定冷却管理入口、业务路由、连续取消/single-flight/安全 HTTP、分类超时、400 正文边界和参数级重复限次。
- push run [`33495001417`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33495001417) 与 PR run [`33495005164`](https://github.com/LoCCai/nonebot-plugin-moellmchats/actions/runs/33495005164) 的 12 个 job 均全部成功，且各恰好一个 `release-gate` 成功。
- PR [#5](https://github.com/LoCCai/nonebot-plugin-moellmchats/pull/5) 核验时为 `OPEN / MERGEABLE / CLEAN`；本任务没有合并它。
- `20cfe44576a3f6f8dbf1bd5a330407a936fe481a` 是 0.26.0 历史安装点：它已经有冷却 Handler，但现场证明标准 `on_command` 没有接住 `/设置LLM冷却 0`，不应继续作为当前安装点。
- `79d2268930251773cb4e91cdd9b13a9ec36a7d14` 是 0.25.0 回退基线，不包含 0.26.0 协议工具或上述后续修复；`bbc3963…` 是更早的历史实现点。
- `5d7f7958e9535f97c7b977d5fbe0fb57d68352ba` 是 0.26.1 历史安装点，不含本轮业务路由和执行状态修复。
- `e340fb77d9c215316c9d4afd69799aedbfcf34fc` 是 0.26.2 历史安装点，不含 K-09 并发、取消和网络安全修复。
- `86ee2a6a35d57e0f8e6f14bae2e3af39b8899241` 是 0.26.3 历史安装点，不含 K-10 分类、重复限次和固定进度指令修复。
- `2b87cdf410b3c77792b5d8c9d37ab11b379d72c8` 是 0.26.4 历史安装点，不含 K-11 逐调用提示和只读恢复修复。
- 当前源码包内版本号是 `0.26.6`，但这不表示 PyPI 已发布、GitHub 已有可安装提交，也不表示它已经安装到七七。`pip install nonebot-plugin-moellmchats` 得到什么仍必须以实际索引为准。
- 这些证据说明“开发制品可以进入隔离测试”，不等于它已经在七七或其他生产 Bot 中部署验证。

下方 0.26.5 命令只用于最后一个已验证候选，不包含 K-12/K-13。0.26.6 必须等本地门禁、实现提交和远端门禁完成后再补入新的完整 SHA；不要自行把命令改成移动分支或未验证 HEAD。只有明确回退到 0.26.4 时才使用 `2b87cdf…`，回退到 0.26.3 时才使用 `86ee2a6…`，回退到 0.26.2 时才使用 `e340fb7…`，回退到 0.26.1 时才使用 `5d7f795…`，回退到 0.25.0 时才使用 `79d2268…`。

## 运行前提

普通聊天至少需要：

- Python 3.10 或更高版本；当前门禁实际覆盖 3.10、3.11、3.12、3.13。
- NoneBot 2.4.4+ 与 OneBot V11/V12 adapter 2.4.6+。
- 能访问所配置的 OpenAI Chat Completions 兼容 API。
- 支持 owner、mode bit 和 no-follow 检查的 POSIX 文件系统；当前不支持 Windows。

如果要运行 `custom_tools/` 或 AI 生成工具，还需满足严格的 [Linux runner 前提](./custom-tools.md#runner-隔离边界与运行要求)。这些前提不满足时，文件/生成工具会 fail closed；普通聊天、MCP 和可信 `ToolSpec` 不会因此自动获得更宽权限。

完整 Python 包和系统依赖见[依赖与运行前提](./dependencies.md)。

## 安装当前精确候选提交

请选择与项目现有依赖管理方式一致的命令。`uv add` 会修改项目依赖声明和锁文件；`pip` 只修改目标 Python 环境。不要在同一次升级里混用两套流程。

### 项目使用 uv 时

```bash
uv add "nonebot-plugin-moellmchats @ git+https://github.com/LoCCai/nonebot-plugin-moellmchats.git@e704092a1e8d9ad215e4e9de35a9fe403483d56f"
```

然后确认锁文件中的 source 末尾确实是目标 SHA，而不是只有分支名：

```bash
uv tree | grep nonebot-plugin-moellmchats
grep -A3 'name = "nonebot-plugin-moellmchats"' uv.lock
```

### 项目使用 pip/venv 时

建议先创建专用虚拟环境，再安装精确提交：

```bash
python3 -m venv .venv-test
.venv-test/bin/python -m pip install --upgrade pip
.venv-test/bin/python -m pip install \
  "nonebot-plugin-moellmchats @ git+https://github.com/LoCCai/nonebot-plugin-moellmchats.git@e704092a1e8d9ad215e4e9de35a9fe403483d56f"
```

已有虚拟环境且依赖已经满足时，可以只替换插件本体。例如项目虚拟环境位于 `.venv`：

```bash
.venv/bin/python -m pip install \
  --upgrade \
  --force-reinstall \
  --no-deps \
  --no-cache-dir \
  "nonebot-plugin-moellmchats @ git+https://github.com/LoCCai/nonebot-plugin-moellmchats.git@e704092a1e8d9ad215e4e9de35a9fe403483d56f"
```

`--no-deps` 不会检查或补装依赖，也不会更新项目的依赖声明或锁文件；只有确认当前环境已经满足[依赖清单](./dependencies.md)时才使用。安装完成后，在重启 Bot 前核对包版本和来源：

七七本轮交接只使用上述目标 `.venv/bin/python -m pip install --no-deps` 形式，由管理员自行停机、安装和启动；不运行 `uv add` 或 `uv sync`，也不改 `pyproject.toml` / `uv.lock`。因此这次替换只对当前虚拟环境生效，未来按旧锁文件重建环境仍可能回到旧提交，重建前必须先单独更新依赖声明与锁文件。

```bash
.venv/bin/python - <<'PY'
from importlib.metadata import distribution
import json

expected = "e704092a1e8d9ad215e4e9de35a9fe403483d56f"
dist = distribution("nonebot-plugin-moellmchats")
source = json.loads(dist.read_text("direct_url.json") or "{}")
actual = source.get("vcs_info", {}).get("commit_id")
print("version:", dist.version)
print("commit:", actual)
assert dist.version == "0.26.5", dist.version
assert actual == expected, actual
PY
```

预期输出中的版本是 `0.26.5`，提交是完整的 `e704092a1e8d9ad215e4e9de35a9fe403483d56f`。如果仍显示 `0.26.4` / `2b87cdf…`，说明尚未包含 K-11 修复；如果显示 `0.26.3` / `86ee2a6…`、`0.26.2` / `e340fb7…`、`0.26.1` / `5d7f795…`、`0.26.0` / `20cfe44…` 或 `0.25.0` / `79d2268…`，则是更旧安装点。应停止重启并重新执行上面的精确安装命令。

安装只替换磁盘文件；运行中的 Python 进程不会自动重载入口模块。核对版本和 SHA 后，应按项目原有方式只重启该 Bot 进程，再由 `SUPERUSERS` 中的账号发送：

```text
/设置LLM冷却 0
```

正确行为是立即回复“已关闭 LLM 对话冷却”，且后台不应出现该消息的难度分类、模型选择或模型请求。如果命令无回复，依次核对运行进程是否在安装后启动、发送者是否属于 NoneBot `SUPERUSERS`，以及是否实际加载了另一份 site-packages；不要通过反复发送普通对话来猜测是否生效。

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
| 1. 锁定 | 依赖和 source SHA | 0.26.5 隔离测试必须解析到完整 `e704092a1e8d9ad215e4e9de35a9fe403483d56f`；`2b87cdf…` 是 0.26.4 历史点，`79d2268…` 仅是 0.25.0 回退基线 |
| 2. 加载 | NoneBot 插件加载 | 无 import/config 权限错误，能生成独立配置目录 |
| 3. 模型 | 测试服务商与模型 | `查看模型`、`查看配置` 正确；纯文本回复成功 |
| 4. 调度 | 分类、视觉、MoE | 各角色使用预期模型；缺能力时明确拒绝或回退 |
| 5. 发现目录 | PicMenu/QWeb、Metadata 菜单、`llm_intents` 与覆写优先级 | 目录晚于初始 generation 安装时在一个 watcher 周期内自动更新；唯一别名选中正确插件；重复/隐藏/黑名单/未加载 fail closed |
| 6. 工具 | Custom File、ToolSpec、MCP、Matcher 兼容 | 只暴露预期工具；真实命令前缀、严格 command Schema、黑名单、权限、结果上限生效；不提供任意 `bot.call_api` |
| 7. 确认 | `mutating` ToolSpec/文件工具 | 首次只给确认码；原用户在原会话另发确认后才执行 |
| 8. OneBot 投递 | v11 三种发送、v12 `send_message`、正文与可选表情 | 只有 Adapter 成功回调才计入捕获；正文失败可见；正文成功后表情失败不重发；v12 无可用 `file_id` 时跳过本地表情；业务 `send_like` 真实调用一次 |
| 9. 协议工具 | v11/NapCat/v12 能力、权限、确认和限额 | 默认关闭；开启后只出现当前 Bot/权限动作；永久拒绝项无 Schema；副作用不确定时不重试 |
| 10. 隔离 | 文件/生成工具 runner | `查看LLM状态` 显示 `isolation=ready`，隔离探针无跳过 |
| 11. 运维 | 重载、取消、退回 | 坏配置保留旧 generation；请求可停止；回退路径已演练 |
| 12. 状态与显示 | 九种插件调度状态、失败重试、进度开关 | 只有输出/已确认副作用为成功；相同失败不重放；部分/不确定不重试；关闭进度仍保留确认、结果、最终反馈和日志 |
| 13. K-11 逐调用进度与恢复 | 搜索/协议/六类工具来源、并行、自然话术、只读降级和面包式连续命令 | 每个获准调用恰好一条固定提示；拒绝项无假提示；只读失败后已确认输出仍成功；真正部分/不确定继续封锁同名工具 |
| 14. K-12 网页与表情 | @Bot 网页路由、普通视频裸链接、表情素材、`extract_webpage` | 网页请求不被媒体探测吞掉；确认视频仍由媒体接管；无效素材不发送；网页只经安全 GET，浏览器零网络且可静态回退 |
| 15. K-13 指令与 Schema | PicStatus 完整指令、入口别名、本轮 Tool Schema、未命中后的菜单恢复 | 进度显示 `/zt 拓扑 全部` 而非仅 `zt`；Schema 外工具在 Matcher 前拒绝；先查同插件列表/拓扑再精确查询；宿主显式 `PS_COMMAND` 不得残留旧别名 |

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
- 日志只能看到模型“正在执行”话术，却没有类型化工具状态或 Adapter 成功证据；
- `result_unknown` / `partial_success` 后系统或人工准备原样重放同一副作用工具；
- 测试实例意外读取了生产 LocalStore、API Key、PostgreSQL DSN 或 Redis URL；
- 只能通过放宽目录权限、跳过 sandbox 测试或复用生产进程才能继续。

## 下一步

- [五分钟模型配置](./configuration.md#五分钟最小配置)
- [调度链路与运行时边界](./runtime-architecture.md)
- [OneBot / NapCat 协议工具](./protocol-tools.md)
- [自定义工具开发](./custom-tools.md)
- [NoneBot 插件与 ToolSpec 接入](./plugin-integration.md)
- [故障排查](./troubleshooting.md)

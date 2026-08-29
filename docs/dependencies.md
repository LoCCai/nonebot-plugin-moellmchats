# 依赖与运行前提

安装工具会根据 `pyproject.toml` 自动解析 Python 依赖，普通用户不需要逐个执行 `pip install`。本页解释每个依赖为什么存在，以及“包装进环境”和“默认会连接后端”之间的区别。

## 支持范围

| 项目 | 当前要求 |
| --- | --- |
| Python | `>=3.10,<4.0`；当前 CI 实测 3.10～3.13 |
| NoneBot | `>=2.4.4,<3.0.0` |
| Adapter | OneBot V11 / V12，`nonebot-adapter-onebot>=2.4.6,<3.0.0` |
| 操作系统 | POSIX 权限语义；当前不支持 Windows |
| 文件/生成工具 | 额外要求完整 Linux 隔离能力，见下文 |

Python 3.14 及更高版本虽然可能满足元数据范围，但尚未进入当前门禁矩阵，不能写成“已验证”。

## 运行依赖清单

以下内容与 `pyproject.toml` 的 `[tool.poetry.dependencies]` 一致。

| 包与约束 | 用途 | 默认启动是否连接外部服务 |
| --- | --- | --- |
| `aiohttp>=3.14.0,<4.0.0` | 调用模型 API、拉取 `/models`、Tavily 搜索和工具生成请求 | 配好非空 provider 后，启动后台刷新可能访问模型服务；否则不会凭空找到服务 |
| `alembic>=1.13.0,<2.0.0` | 内置 PostgreSQL migration graph 与离线/显式迁移接口 | 否；默认不会运行 migration |
| `asyncpg>=0.30.0,<1.0.0` | SQLAlchemy PostgreSQL async driver | 否；默认资源设置没有数据库 DSN |
| `nonebot2>=2.4.4,<3.0.0` | NoneBot 插件生命周期、Matcher、权限和事件接口 | 由宿主 Bot 使用 |
| `nonebot-adapter-onebot>=2.4.6,<3.0.0` | OneBot V11/V12 消息、Bot/Event 类型、能力探测和 QQ API | 由宿主 adapter 使用 |
| `ujson>=5.12.0,<6.0.0` | 兼容既有配置和模型 payload JSON 路径 | 否 |
| `nonebot-plugin-localstore>=0.7.0,<0.8.0` | 确定配置目录并创建插件私有文件 | 只访问本地配置目录 |
| `python-dotenv>=1.2.2,<2.0.0` | 保留 NoneBot `.env` 配置链的显式安全版本下限 | 否；插件源码不直接 import 它 |
| `redis>=5.2.0,<7.0.0` | 可选 Redis admission、冷却、PendingAction 和 history backend | 否；默认没有 Redis URL，也不会创建 client |
| `sqlalchemy>=2.0.0,<3.0.0` | Agent/history/summary/usage/audit 的可选 PostgreSQL Schema、Repository 与 engine | 否；默认使用 Memory 组合 |
| `tomli>=2.0.1,<3.0.0`（仅 Python `<3.11`） | Python 3.10 读取 TOML；3.11+ 使用标准库 `tomllib` | 否 |
| `mcp[cli]>=1.28.1,<2.0.0` | MCP client session，以及 stdio、Streamable HTTP、SSE transport | 只有启用某个 MCP server 后才连接或启动子进程 |

### 为什么数据库和 Redis 包是安装依赖

数据库、Redis、migration 和 runtime resource 模块都随同一个 Python 包发布并可被公开导入，所以驱动和类型依赖必须在安装时可用。它们是“代码依赖”，但相应后端仍是“运行时显式选择”。

标准插件启动使用 `RuntimeResourceSettings()`：

- history backend 为 Memory；
- PostgreSQL、Redis、local spool 和 platform API 都未配置；
- trusted runner pool 和并行 Tool Graph 都未配置；
- 不读取 `DATABASE_URL`、`REDIS_URL` 或其他环境变量来偷偷启用后端。

因此“环境里安装了 `asyncpg`/`redis`”不代表“插件已经连上 PostgreSQL/Redis”。真实后端组合是程序化集成接口，目前不是 `config.json` 或 `.env` 的普通用户开关。详见[默认模式与程序化资源接口](./runtime-architecture.md#默认模式与程序化资源接口)。

### `mcp[cli]` 包含什么、不包含什么

插件实际使用 MCP Python SDK 的 client API。当前仍声明 `mcp[cli]`，用于保持既有 CLI/stdio 工作流的安装兼容面；这不等于安装了任意第三方 MCP Server。

例如：

```toml
command = "uvx"
args = ["mcp-server-filesystem", "/tmp"]
```

这里的 `uvx` 和 `mcp-server-filesystem` 必须由部署环境另行提供。其他 stdio 命令、Node/npm server、浏览器和系统库也一样，不属于本插件的 pip 依赖。

### PicMenu/QWeb 是可选发现源，不是安装依赖

MoEllmChats 可以读取“已经加载的 PicMenu Next 当前内存目录”，用其中的菜单补充 NoneBot 插件意图。该桥接只按公开字段做类型受限的 duck typing：不会 import PicMenu SDK，不会读取 QWeb/PicMenu 文件路径，也不会为了发现目录发起网络或数据库请求。

因此无需把 `nonebot-plugin-picmenu-next`、QWeb 或七七项目包加入本项目 `pyproject.toml`。宿主没有 PicMenu 时直接使用 `PluginMetadata.extra.menu_data`；PicMenu 初始内存目录为空时先发布 Metadata 目录。watcher 会把 PicMenu 投影的插件数、功能数和 SHA-256 纳入指纹，目录稍后安装完整内容时自动发布新 generation；读取异常保留上一有效投影，不会为了恢复发现功能而安装或更新任何额外依赖。

0.26.2 的目录摘要、NFKC 意图规范化、参数摘要和状态对象只使用 Python 标准库 `hashlib`、`json`、`unicodedata`、`dataclasses` 与现有 NoneBot Adapter 钩子，没有增加运行依赖、数据库表或 migration。七七侧的 QWeb/PicMenu 字段桥接属于宿主源码契约，不会打进本插件 wheel，也不是本插件的 pip 依赖。

### 0.26.3 安全 HTTP 与并发修复没有新增依赖

`safe_request` 使用 Python 标准库的 `asyncio`、`socket`、`ssl`、`ipaddress` 和 `urllib.parse` 实现有界 HTTP/1.1 门面，不要求安装 `httpx`、`requests` 或额外 DNS 包。Custom File 仍不能直接导入网络客户端；工具只接收 worker 注入的门面，network allowlist 来自已审核的静态 Capability，不能由模型参数扩大。

分类 single-flight、连续取消 cleanup、SSE 解析和 400 结构化判断也只复用标准库及已有 SQLAlchemy/asyncpg 接口。0.26.3 没有新增配置项、数据库 migration、Redis key 或后台服务；安装依赖集合保持不变。

### 协议清单不是新运行依赖

OneBot v11 的 38 项、OneBot v12 的 31 项和 NapCat 4.18.19 的 175 项动作以规范化 JSON 随 wheel/sdist 安装。运行时和普通构建不访问协议文档站，也不需要安装 NapCatDocs、Git 或 OpenAPI 解析器。

维护者只有在固定上游版本变更时才运行 `scripts/generate_protocol_manifests.py`。生成器会校验四个固定 Git commit、NapCat OpenAPI SHA-256、动作数量、工具名碰撞和人工策略完整性；这些上游 checkout 不是用户依赖。来源、许可证和完整权限边界见 [OneBot / NapCat 协议工具](./protocol-tools.md)。

## 系统与能力依赖

### 基础配置存储

插件会把 LocalStore 配置目录收紧为 `0700`，配置和密钥文件收紧为 `0600`，已批准的不可变工具版本使用 `0500`/`0400`。运行环境必须正确支持：

- 文件 owner 与 Unix mode bit；
- `chmod(..., follow_symlinks=False)` 或等价 no-follow 语义；
- 普通文件/目录和符号链接的可靠区分；
- 当前进程对自己配置树的所有权。

发现符号链接、owner 不符或文件类型异常时会拒绝处理，不应靠 `chmod 777` 绕过。

### 文件和生成工具 runner

要让 `custom_tools/` 和 Generated Tool 真正执行，主机还必须具备：

- Linux `setrlimit`、进程组和 `/proc` namespace 信息；
- 能切换到 `nobody`（UID/GID 65534）；主进程通常需要 root 或等价能力；
- PID、mount、IPC、UTS namespace；禁网工具还需 network namespace；
- mount propagation、只读根挂载和私有 workspace bind mount；
- Landlock；
- 可加载且 syscall 映射完整的 `libseccomp`；
- runner 所拒绝的 socket、process、xattr、keyring 等 syscall 在当前内核上可正确探测。

缺少任一强制隔离能力时，runner 的状态会是 `unavailable:<原因>`，相关工具 fail closed。它没有 cgroup，也不是容器或完整 syscall allowlist；完整边界见[runner 隔离说明](./custom-tools.md#runner-隔离边界与运行要求)。

### 外部网络和命令

按功能还可能需要：

| 功能 | 额外前提 |
| --- | --- |
| 模型聊天/分类/总结 | 可访问所配 OpenAI-compatible endpoint；有效 API Key |
| 联网搜索 | 可访问 Tavily；`search_api` 是完整 Authorization 值 |
| 远程 MCP | 可访问所配 URL；当前 HTTP/SSE 配置不自动注入自定义鉴权头 |
| stdio MCP | `command` 可执行，依赖命令在 PATH 中；按需提供 `cwd`/`env` |
| 表情包 | `emotions_dir` 是 Bot 进程可读的绝对目录 |

## MCP 信任边界

MCP 不经过文件/生成工具的 nobody sandbox：

- stdio server 是宿主进程启动的外部程序，当前会继承宿主环境，再叠加 `[mcp.<name>.env]`；
- HTTP/SSE server 是外部服务；返回内容按外部、不可信数据处理；
- 当前 `mcp_servers.toml` 不提供逐工具 `permission`/`effect` 声明，发现的 MCP 工具按兼容契约进入目录。

所以只能启用已审查的 MCP Server。会写文件、发消息、删数据或调用管理 API 的 MCP 不应直接暴露给普通用户；应保持禁用/黑名单，或改用可明确声明 `permission` 和 `mutating` 的可信 `ToolSpec` 包装。详见[插件集成文档](./plugin-integration.md)。

## 开发依赖

这些包不会因普通安装而自动加入业务依赖：

| 包 | 用途 |
| --- | --- |
| `pytest`、`pytest-asyncio` | 单元、异步和 sandbox 测试 |
| `ruff==0.16.2` | lint 与格式检查；CI 固定版本 |
| `fakeredis` | Redis 组件的离线测试 |
| `filelock` | 开发/测试辅助锁语义 |
| `nonemoji` | 开发辅助工具 |
| `pre-commit` | 本地提交前检查 |
| `build>=1.3.0,<2.0.0` | fresh wheel/sdist 构建 |
| `pyright==1.1.411` | 新增及安全关键模块的静态类型门禁；项目遗留模块仍按规划逐步收口 |
| `twine>=6.1.0,<8.0.0` | wheel/sdist 元数据与长描述检查；不会自动上传制品 |

安装开发依赖时应使用项目现有的 Poetry/uv 流程，不要把它们手工写入生产依赖。

## 依赖审计结论

本次按以下三个层次核查：

1. 将源码直接外部 import 与 `pyproject.toml` 对照；未发现运行所需但漏声明的顶层包。
2. 确认 `asyncpg` 是 SQLAlchemy PostgreSQL dialect 的运行驱动，`tomli` 是 Python 3.10 回退，`python-dotenv` 是保留的 `.env` 安全下限，而不是误删候选。
3. 保留 `mcp[cli]` 的现有兼容声明；MCP server 可执行文件仍需独立管理，不能把 SDK extra 当成 server 清单。
4. 新增的 PicMenu/QWeb 菜单发现只消费已加载模块的内存投影，不新增 Python、系统或服务依赖。
5. 协议清单生成器、文档/依赖检查和制品检查只使用 Python 标准库；`build`、`pyright`、`ruff`、`twine` 只属于开发门禁，不会进入普通安装依赖或在运行时联网。
6. 0.26.3 的安全 HTTP、single-flight 和取消清理复用标准库及已有依赖；没有把 `httpx`、`requests`、额外 DNS 客户端或新后端加入安装集合。

依赖约束本身只说明 resolver 允许的范围。最终可安装性还要由 fresh wheel/sdist 构建和包外安装 smoke 验证，见[安装验收](./installation.md#隔离功能验收清单)。

# 自定义工具开发

`custom_tools/` 目录允许你编写原生 Python 函数，供大模型通过 Function Calling 调用，**无需模拟 NoneBot 消息事件**。0.25 起，文件工具只用 AST 提取元数据，实际调用在隔离子进程中执行，不会导入 NoneBot 主进程。

首次运行后会自动生成禁用的参考模板 `custom_tools/_example.py`。以下划线开头的文件和历史 `example.py` 都不会加载，复制并改名后才会成为工具，避免默认开放网络访问。

---

## 编写规范

### 基本示例

```python
from typing import Annotated

async def get_weather(
    city: Annotated[str, "需要查询天气的城市名称，如：北京市、上海市"]
) -> str:
    """
    获取指定城市的实时天气情况。当用户询问天气时调用此工具。
    """
    # 实际使用时替换为真实 API 调用
    return f"{city}今天天气晴朗，气温25度。"
```

**规范说明：**

- 函数名即为工具名，建议使用英文下划线命名
- 参数类型用 `Annotated[类型, "参数说明"]` 标注，说明会直接传给 LLM
- 函数 docstring 即为工具描述，告诉 LLM 何时调用此工具
- 支持 `async def`（推荐）和普通 `def`
- 文件工具返回值可为标量（会转为文本），或包含 `text`/`images` 的字典；Runner v1 不透传文件/生成工具返回值中的 `metadata`。可信的主进程 `ToolSpec` 可直接返回下文所述的 `ToolResult`，并由主进程严格校验和保留 `metadata`
- 文本由 `result_limit` 裁剪，未声明时回退到全局 `max_tool_result_chars`；图片由 `max_tool_images` 裁剪。普通调用和 `确认执行` 通道共用同一结果契约与截断提示
- 不声明 `TOOLS_REGISTRY` 时，每个公开函数都默认是 `permission=user`、`effect=read_only`；五项 Capability 默认为 `network=false`、`process=false`、`workspace=true`、`host_filesystem=false`、`secrets=false`。会修改外部状态或需要放宽任一能力的工具必须显式声明，不能依赖默认值

### 多参数示例

```python
from decimal import Decimal, InvalidOperation
from typing import Annotated

async def calculate(
    left: Annotated[str, "左操作数"],
    operator: Annotated[str, "运算符：+、-、* 或 /"],
    right: Annotated[str, "右操作数"],
    precision: Annotated[int, "结果保留小数位数，默认2"] = 2
) -> str:
    """执行两个十进制数的四则运算。"""
    try:
        lhs, rhs = Decimal(left), Decimal(right)
        if operator == "+":
            result = lhs + rhs
        elif operator == "-":
            result = lhs - rhs
        elif operator == "*":
            result = lhs * rhs
        elif operator == "/" and rhs != 0:
            result = lhs / rhs
        else:
            return "不支持的运算符，或除数为 0"
        digits = min(12, max(0, precision))
        return f"计算结果：{result:.{digits}f}"
    except InvalidOperation:
        return "计算失败：请输入有效十进制数"
```

---

## 显式权限与副作用（TOOLS_REGISTRY）

需要声明权限、副作用、超时、结果上限或依赖时，在同一文件中定义 `TOOLS_REGISTRY`。一旦存在该数组，只有数组中列出的函数会加载：

```python
from typing import Annotated

async def save_note(
    content: Annotated[str, "要保存的内容"],
) -> str:
    """保存一条外部笔记。"""
    # 在这里调用经过审查的外部存储接口
    return "已保存"

TOOLS_REGISTRY = [
    {
        "name": "save_note",
        "description": "保存一条外部笔记。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要保存的内容"}
            },
            "required": ["content"],
            "additionalProperties": False,
        },
        "func": save_note,
        "permission": "superuser",
        "effect": "mutating",
        "timeout_seconds": 10,
        "result_limit": 1000,
        "dependencies": [],
        "capabilities": {
            "network": True,
            "process": False,
            "workspace": True,
            "host_filesystem": False,
            "secrets": False,
        },
    }
]
```

`permission` 仅支持 `user`/`superuser`，`effect` 仅支持 `read_only`/`mutating`。`capabilities` 只接受布尔型 `network`、`process`、`workspace`、`host_filesystem`、`secrets`；未知字段、字符串形式的布尔值等都会使候选 generation 拒绝加载。Custom File 属于管理员维护的信任域，因此字面量声明同时作为申请值和管理员上限。`workspace` 表示本次调用的私有临时工作目录；`host_filesystem` 控制是否放宽 Landlock 宿主读取限制；`secrets` 是预留字段，当前即使显式为 true 也不会注入宿主环境或密钥。

变更型工具不会把授权交给模型，也不会在工具 Schema 中加入 `confirm`。模型第一次调用时系统只固化参数及其 SHA-256，生成 6 位一次性确认码，并绑定当前 Bot/Adapter、用户、群组或私聊会话、工具名、runtime generation 和 Generated bundle digest。用户必须在默认 120 秒 TTL 内另发一条独立消息：

```text
确认执行 A7F42C
```

也可发送 `取消执行 A7F42C`。确认码在真正执行前即被原子消费，不能重放；用户、群组或 generation 不匹配时 fail closed。因此原消息包含“确认执行”、否定句或模型自行构造参数都不能授权执行。

---

## 工具依赖拓扑（TOOL_DEPENDENCIES）

当一个工具的执行依赖另一个工具时，可以声明 `TOOL_DEPENDENCIES`，让分类模型分配主工具时自动将依赖工具也注入给 LLM。

```python
from typing import Annotated

# 声明：当 LLM 被分配了 get_weather 工具时，强制同时注入 extract_webpage 工具
TOOL_DEPENDENCIES = {
    "get_weather": ["extract_webpage"]
}

async def get_weather(
    city: Annotated[str, "城市名称"]
) -> str:
    """获取城市天气。"""
    ...

async def extract_webpage(
    url: Annotated[str, "要抓取内容的网页 URL"]
) -> str:
    """抓取指定网页的文本内容。"""
    ...
```

**格式**：`{ "触发工具名": ["要同时注入的工具名1", "工具名2"] }`

---

## 与 NoneBot / QQ 集成

文件工具不再支持 `_bot`、`_event` 或 `_tool_manager`。发现这些隐藏参数时会拒绝整个候选 generation，并提示迁移。需要 Bot、Event 或数据库对象的可信集成必须放在独立 NoneBot 插件中，通过 `register_tool(ToolSpec(...))` 注册；权限和变更确认仍会在执行端复核。

文件工具可以按需声明 `_tool_context` 和 `_workspace`：前者收到只含 `request_id`、`confirmed`、`user_id`、`group_id`、`message_type` 的字典，其中 `confirmed` 仅在一次性确认码校验通过后的变更执行中为真；后者收到本次调用专用的临时可写目录。目录会在调用结束后删除，不能用来持久化数据。

---

## 热重载

编写完成后会自动原子重载，也可使用 `刷新工具` 或 `重载LLM`，**无需重启 NoneBot**。语法、契约或 Policy 错误时旧 generation 保持可用。正式 Custom File / Generated loader 会把源码、Schema 与安全契约固化为当前 generation 的不可变 `ToolArtifact`；两类制品执行前都复核 generation 和 artifact digest，Generated Tool 还复核由 manifest、源码与测试形成的 bundle digest。已经开始的请求继续执行自己固定的旧制品，新请求才使用新 generation，不会在启动子进程时重新读取后来被修改的活动源文件。任何待确认操作在 generation 改变后都会失效，必须由新请求重新生成确认码。

文件工具和 AI 生成工具均在 nobody 子进程执行，默认全局单并发、等待 4、墙钟 30 秒、CPU 10 秒、内存 256 MiB、64 KiB 输出和 64 MiB 工作目录。工作目录同时限制单文件大小、文件和目录条目数及目录深度；runner 在执行期间异步扫描，并在进程组结束后强制最终扫描。所有文件/生成工具都要求独立 PID/mount/IPC/UTS namespace，并把 hostname 固定为 `moellm-sandbox`；`network=false` 时再进入独立 network namespace。Generated Tool 的 effective 上限仅允许私有 workspace；Custom File 只有 `TOOLS_REGISTRY.capabilities` 的显式字面量声明才能放宽。允许 `process=true` 时才使用 `generated_tool_max_processes`（默认 16）的上限。它们不会收到生产环境变量、Bot、Event 或数据库对象；超时、取消和越限会杀死整个进程组。

两类 loader 还会运行结构化 AST Policy，对模块级语句、handler 及其可达 helper 生成 `ALLOW`、`DENY`、`CAPABILITY_REQUIRED` 或 `RISK` finding。阻断项会拒绝候选 generation；检测到的文件/数据库/HTTP 写入或系统命令会把工具的实际 effect 提升为 `mutating`。因此 Custom File 即使显式取得 `process=true`，也仍需经过 PendingAction 二阶段确认。Policy 只能做保守静态预检，不是完整代码证明。

## runner 隔离边界与运行要求

> 下列内容描述当前开发工作树的实现语义；最新 OS 隔离增量（UTS/socket/keyring/xattr）尚待完整门禁复跑，不能据此声称当前分支已经发布或生产验证。

- 运行环境必须支持 Linux `setrlimit`、进程组和 UID/GID 切换；主进程通常需要以 root 启动，worker 随后降权到 nobody（65534）。不能安全降权时，文件/生成工具拒绝执行，聊天、MCP 和可信 `ToolSpec` 仍可使用。
- worker 使用 `python -I`、`no_new_privs` 和环境变量白名单，不会注入生产密钥或 Bot/数据库对象；启动预检结果可在 `查看LLM状态` 的 `isolation` 字段查看。
- runner 先建立独立 PID/mount/IPC/UTS namespace，将 hostname 固定为 `moellm-sandbox`，把 namespace 根挂载递归设为只读，再把私有 workspace 做成独立 bind mount；只有 `workspace=true` 时该 bind mount 可写。`workspace=false` 时不注入 `_workspace`，私有 cwd 也保持只读。任一必要 namespace、hostname 或 mount 操作不可用时 fail closed。
- Generated Tool 的草稿自测试和每次运行期调用都禁网；Custom File 在 `network=false` 时进入独立 network namespace，只有显式声明 `network=true` 才使用主机网络。系统没有 `unshare`、创建 namespace 失败或探针不通过时，对应工具 fail closed。
- `process=false` 同时把 worker 的进程额度收紧为 1，并用 libseccomp 拒绝 `execve`/`execveat`/`fork`/`vfork`/`clone`/`clone3`，防止工具用 `os.exec*` 原地替换 worker。libseccomp、任一 syscall 映射或规则加载不可用时，工具在编译不可信源码前 fail closed。Custom File 显式声明 `process=true` 后才移除该 deny filter 并恢复到配置的进程数上限；Generated Tool 当前不能申请放宽。
- `host_filesystem=false` 时，Landlock 只开放运行 Python 所需的固定只读路径和私有 workspace；即使 `process=true`，也只额外开放固定 `/usr/local/bin`、`/usr/bin`、`/bin` 的读取/执行，PATH 同样固定。由于 Landlock ABI 1 不覆盖扩展属性，seccomp 还拒绝 `getxattr`、`lgetxattr`、`fgetxattr`、`listxattr`、`llistxattr`、`flistxattr`。Custom File 显式声明 `host_filesystem=true` 后可读取 nobody 按 DAC 本就能读取的宿主文件，因此必须视为高权限。
- `network=false` 时，seccomp 拒绝所有 `socket(2)` family，而不只依赖 network namespace。`network=true` 但 `host_filesystem=false` 时继续拒绝 AF_UNIX 与 AF_VSOCK；所有受限组合的 `socketpair` 只保留 Python/asyncio 所需的 AF_UNIX/`SOCK_STREAM`，并拒绝其他 domain/base type 与 `io_uring_setup`。只有同时取得 network 与 host-filesystem 能力时，Custom File 才可能连接宿主 Unix socket。
- `add_key`、`request_key`、`keyctl` 三个 Linux keyring syscall 在所有 capability 组合下无条件拒绝，避免读取继承的 session keyring。
- `secrets=true` 目前只保留契约位置，不会将生产环境变量、凭据文件或其他宿主密钥注入 worker。
- 正式执行只接受 generation 固定的 `ToolArtifact`。worker 从 stdin 接收固化源码，结果通过独立的版本化 FD3 协议返回；stdout/stderr 仅作为有界日志读取，因此普通 `print` 或直接写 stdout 不会意外污染结果。不过 FD3 只是协议通道分离，不是对恶意工具代码的认证机制，也不会把已批准代码变成可信代码。
- workspace 的总字节、单文件大小、文件和目录条目数、目录深度均有上限，运行中扫描在 event loop 外执行，进程组结束后还会强制最终扫描；符号链接或特殊文件同样会拒绝结果。`workspace=false` 不向 handler 注入 `_workspace`，并让私有 cwd 的 bind mount 保持只读。
- 这仍不是容器或完整 syscall allowlist，也没有 cgroup；CPU、内存和进程等预算依赖 namespace、RLIMIT 与现有进程清理。默认路径通过只读根挂载、Landlock、seccomp 和固定 hostname 收紧能力，但 `stat`、`lstat`、`readlink` 等仍可能暴露宿主路径是否存在、类型或链接目标等元数据；`host_filesystem=true` 会恢复 DAC 范围内的宿主读取，`network=true` 与 `host_filesystem=true` 的组合还会恢复宿主 AF_UNIX 连接。Source Snapshot 和 workspace 限额解决 TOCTOU 与资源滥用，不替代最小权限和人工审阅。
- 每个 `.py`、`manifest.json` 和 `tests.py` 上限 64 KiB。静态检查、模型复核与资源限制只能降低风险，不能证明生成代码安全。

## AI 工具包

超级管理员可发送 `添加LLM功能 <需求>`。系统使用当前聊天模型生成 `manifest.json`、`tool.py` 和 `tests.py`，完成 AST/Schema/敏感字面量检查与隔离测试，再由总结模型独立复核。复核通过仍只是草稿，必须查看完整审阅页，并复制其中的 `批准LLM功能 <ID> <至少8位哈希> <完整64位review stamp>`。

功能需求长度为 1 到 4000 字符，同时只允许一个生成任务；生成和复核共用 LLM 准入与整任务预算。`manifest.json` 顶层可用 `capabilities` 申请 `network`、`process`、`workspace`、`host_filesystem`、`secrets`，每个值都必须是布尔型；每个工具必须声明 `name`、`description`、对象型 `parameters`、`handler`、`permission` 和 `effect`，可选 `timeout_seconds`（最多 30）与 `result_limit`（最多 6000）及 `dependencies`。Capability 和 permission 都只是申请值：当前 Generated Tool 的管理上限仅允许私有 workspace，其余四项不会因 manifest 声明为 true 而放开。`tests.py` 必须定义 `run_tests(tool_module)`。

批准命令的第二个参数要求至少 8 位且与当前草稿内容哈希前缀匹配，第三个参数必须是完整 64 位 review stamp。stamp 绑定草稿 ID/digest、lifecycle revision/state digest 和同 bundle 当前 active digest；审阅后任何 lifecycle 变化都会拒绝旧 stamp，必须重新查看草稿。批准版本按完整 SHA-256 保存为只读目录；同一 `bundle_id` 的新草稿获批即为升级。批准、拒绝、权限、停用和回滚统一由 `RuntimeReloader` 先构建 after-state 候选，再以 revision/state digest CAS 提交 canonical lifecycle revision，最后发布当前进程 generation；Store 的内部 commit 入口不是生产 API。共享配置目录的其他新版进程由 watcher 最终收敛。已开始的请求继续固定旧的只读版本。

schema v3 将 `DraftEvidence` 作为 canonical 生命周期数据：每条证据绑定草稿 digest，并校验 producer、outcome、summary、risks、时间与顺序。schema v2 可兼容读取，旧状态会在内存中转换为带 `schema-v2-migration` / `legacy_unverified` 标记的 v3 evidence，并在下一次 canonical 写入时持久化 v3。metadata 中的 `lifecycle_evidence` 是 canonical evidence 的兼容投影；投影失败会标记 stale，但不会改变决策源。只有原始 `metadata.review` 仍是 best-effort 摘要。

目录 fsync 会有界重试 3 次；若重试耗尽，即使新 state 当前可见也必须保持 uncertain，不能推断 durable success。只有目录 durability 已确认而后续回读不确定时，才可按完整 revision/state digest 精确区分 before/after。回滚还会在同一 canonical snapshot 下要求版本前缀唯一、记录未 Archived、owner/no-follow 检查通过、目录精确为 `0500`、只含 `manifest.json`/`tool.py`/`tests.py` 三个 `0400` 普通文件，且完整 bundle digest 匹配。

Generated manifest 的 `permission` 同样只是 `requested_permission`。无论请求 `user` 还是 `superuser`，初始 `effective_permission` 都是 `superuser`；只有超级管理员对当前 bundle digest 和工具名执行 `设置LLM功能权限 <工具包> <版本哈希前缀> <工具名> user`，才会写入人工 grant 并发布新 generation。设置为 `superuser` 会撤销 grant；版本变化不会继承旧 grant。普通用户看不到 effective `superuser` 工具的目录或 Schema，即使伪造调用也不会执行。

`generated_tools_enabled=false` 只关闭新的“添加/创建LLM功能”任务，不会自动停用已经激活的工具包。需要停用时使用 `停用LLM功能 <工具包>`；需要一次关闭所有函数调用时使用 `设置工具调用 关`。

## 显式 ToolSpec 接口（推荐）

其他 Python 插件可调用 `register_tool(ToolSpec(...))` 注册结构化工具。`ToolSpec` 描述参数 JSON Schema、`read_only`/`mutating` 属性、`user`/`superuser` 权限、超时、结果上限和依赖；处理函数可接收 `_tool_context: ToolContext` 并返回 `ToolResult`。`ToolResult.images` 接受 list/tuple 并规范化为 tuple，`metadata` 接受任意 Mapping 但会复制为普通字典；两者仍须满足上面的严格元素与键类型约束。这类可信处理函数在 NoneBot 主进程执行，可以访问 Bot/Event。插件源码变化仍需重启 NoneBot；若插件在运行时动态注册或替换 `ToolSpec`，还需执行 `重载LLM` 才会发布新的工具快照。

变更型工具统一进入上述一次性 PendingAction 流程；独立 `确认执行 <确认码>` 消息通过 TTL、用户/会话、generation、工具版本和固化参数校验后才运行。主进程中的同步 `mutating` handler 无法在 `asyncio` 超时后终止后台线程，因此会在启动前明确拒绝；应改为可取消的 `async` handler，或迁移到可杀死进程组的 Custom File。URL 参数仍统一拒绝私网、环回、保留地址和云元数据地址。

---

## 注意事项

- 文件名任意（`.py` 后缀），但函数名不能与系统内置工具冲突
- 同一文件中可定义多个工具函数
- 文件工具不得声明 `_bot` / `_event` / `_tool_manager`；可信 Bot 集成请使用独立插件注册 `ToolSpec`
- 文件工具代码会实际执行；不要把未经审查的第三方或模型输出直接放入 `custom_tools/`
- 工具执行异常时建议 `try/except` 后返回错误描述字符串，而非抛出异常

---

## 相关页面

- [NoneBot 插件集成](./plugin-integration.md) — 让 LLM 调用现有 Bot 插件
- [配置参考 → model_config.json](./configuration.md#model_configjson--智能调度配置) — `use_tools`、`tool_blacklist`、`resident_plugins` 配置项

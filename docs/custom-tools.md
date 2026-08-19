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
- 返回值可为 `str`，或包含 `text`/`images` 的字典；结果会按工具预算裁剪后交给 LLM
- 不声明 `TOOLS_REGISTRY` 时，每个公开函数都默认是 `permission=user`、`effect=read_only`；会修改外部状态的工具必须显式声明，不能依赖这个默认值

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
    }
]
```

`permission` 仅支持 `user`/`superuser`，`effect` 仅支持 `read_only`/`mutating`。变更型工具的 Schema 会自动加入 `confirm` 参数，只有用户原文包含“确认执行”且模型传入 `confirm=true` 才会调用函数。

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

文件工具可以按需声明 `_tool_context` 和 `_workspace`：前者收到只含 `request_id`、`confirmed`、`user_id`、`group_id`、`message_type` 的字典，后者收到本次调用专用的临时可写目录。目录会在调用结束后删除，不能用来持久化数据。

---

## 热重载

编写完成后会自动原子重载，也可使用 `刷新工具` 或 `重载LLM`，**无需重启 NoneBot**。语法错误时旧 generation 保持可用。工具的元数据和 Schema 会按 generation 固定，但文件源码在真正启动子进程时从原路径读取；若必须保证活动请求继续执行旧源码，请在没有活动工具调用时再编辑文件。

文件工具和 AI 生成工具均在 nobody 子进程执行，默认全局单并发、等待 4、墙钟 30 秒、CPU 10 秒、内存 256 MiB、16 个进程、64 KiB 输出和 64 MiB 工作目录。它们不会收到生产环境变量、Bot、Event 或数据库对象；超时、取消和越限会杀死整个进程组。

## runner 隔离边界与运行要求

- 运行环境必须支持 Linux `setrlimit`、进程组和 UID/GID 切换；主进程通常需要以 root 启动，worker 随后降权到 nobody（65534）。不能安全降权时，文件/生成工具拒绝执行，聊天、MCP 和可信 `ToolSpec` 仍可使用。
- worker 使用 `python -I`、`no_new_privs` 和环境变量白名单，不会注入生产密钥或 Bot/数据库对象；启动预检结果可在 `查看LLM状态` 的 `isolation` 字段查看。
- 这不是容器、seccomp 或完整文件系统沙箱。运行期工具可以访问外网，也能读取 nobody 有权读取的主机文件。父进程会递归拒绝参数中显式出现的私网、环回、保留地址和云元数据 HTTP(S) URL，但工具自行拼接地址或直接使用 socket 不受这项参数检查保护。
- AI 草稿自测试使用 `unshare --net` 禁网；系统没有 `unshare` 或缺少创建网络 namespace 的权限时，`添加LLM功能` 会失败且不保存草稿。运行期调用不使用该网络 namespace。
- 每个 `.py`、`manifest.json` 和 `tests.py` 上限 64 KiB。静态检查、模型复核与资源限制只能降低风险，不能证明生成代码安全。

## AI 工具包

超级管理员可发送 `添加LLM功能 <需求>`。系统使用当前聊天模型生成 `manifest.json`、`tool.py` 和 `tests.py`，完成 AST/Schema/敏感字面量检查与隔离测试，再由总结模型独立复核。复核通过仍只是草稿，必须查看风险、diff 和 SHA-256 后发送 `批准LLM功能 <ID> <短哈希>`。

功能需求长度为 1 到 4000 字符，同时只允许一个生成任务；生成和复核共用 LLM 准入与整任务预算。`manifest.json` 中每个工具必须声明 `name`、`description`、对象型 `parameters`、`handler`、`permission` 和 `effect`，可选 `timeout_seconds`（最多 30）与 `result_limit`（最多 6000）及 `dependencies`。`tests.py` 必须定义 `run_tests(tool_module)`。

批准命令要求至少 8 位且与当前草稿内容的哈希前缀匹配。批准版本按完整 SHA-256 保存为只读目录；同一 `bundle_id` 的新草稿获批即为升级。停用和回滚只发布新 generation，已开始的请求继续固定旧的只读版本。复核失败或内容变化的草稿不能批准，拒绝草稿仍保留源码供审计。普通用户看不到 `superuser` 工具的目录或 Schema，即使伪造调用也不会执行。

`generated_tools_enabled=false` 只关闭新的“添加/创建LLM功能”任务，不会自动停用已经激活的工具包。需要停用时使用 `停用LLM功能 <工具包>`；需要一次关闭所有函数调用时使用 `设置工具调用 关`。

## 显式 ToolSpec 接口（推荐）

其他 Python 插件可调用 `register_tool(ToolSpec(...))` 注册结构化工具。`ToolSpec` 描述参数 JSON Schema、`read_only`/`mutating` 属性、`user`/`superuser` 权限、超时、结果上限和依赖；处理函数可接收 `_tool_context: ToolContext` 并返回 `ToolResult`。这类可信处理函数在 NoneBot 主进程执行，可以访问 Bot/Event。插件源码变化仍需重启 NoneBot；若插件在运行时动态注册或替换 `ToolSpec`，还需执行 `重载LLM` 才会发布新的工具快照。

变更型工具只有在用户文字包含 `确认执行` 且模型调用参数带 `confirm=true` 时才运行。URL 参数统一拒绝私网、环回、保留地址和云元数据地址。

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

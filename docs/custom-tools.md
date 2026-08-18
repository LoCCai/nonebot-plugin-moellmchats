# 自定义工具开发

`custom_tools/` 目录允许你编写原生 Python 函数，供大模型通过 Function Calling 调用，**无需模拟 NoneBot 消息事件**。0.25 起，文件工具只用 AST 提取元数据，实际调用在隔离子进程中执行，不会导入 Qiqi 主进程。

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
- 返回值为 `str`，直接作为工具执行结果返回给 LLM

### 多参数示例

```python
from typing import Annotated

async def calculate(
    expression: Annotated[str, "数学表达式，如：(1+2)*3/4"],
    precision: Annotated[int, "结果保留小数位数，默认2"] = 2
) -> str:
    """
    计算数学表达式的结果。当用户需要精确计算时调用。
    """
    try:
        result = eval(expression)
        return f"计算结果：{round(result, precision)}"
    except Exception as e:
        return f"计算失败：{e}"
```

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

---

## 热重载

编写完成后会自动原子重载，也可使用 `刷新工具` 或 `重载LLM`，**无需重启 NoneBot**。语法错误时旧 generation 保持可用。

文件工具和 AI 生成工具均在 nobody 子进程执行，默认全局单并发、等待 4、墙钟 30 秒、CPU 10 秒、内存 256 MiB、16 个进程、64 KiB 输出和 64 MiB 工作目录。它们不会收到生产环境变量、Bot、Event 或数据库对象；超时、取消和越限会杀死整个进程组。

## AI 工具包

超级管理员可发送 `添加LLM功能 <需求>`。系统使用当前聊天模型生成 `manifest.json`、`tool.py` 和 `tests.py`，完成 AST/Schema/敏感字面量检查与隔离测试，再由总结模型独立复核。复核通过仍只是草稿，必须查看风险、diff 和 SHA-256 后发送 `批准LLM功能 <ID> <短哈希>`。

批准版本按完整 SHA-256 保存为只读目录。升级、停用和回滚只发布新 generation；已开始的请求继续固定旧快照。普通用户看不到 `superuser` 工具的目录或 Schema，即使伪造调用也不会执行。

## 显式 ToolSpec 接口（推荐）

其他 Python 插件可调用 `register_tool(ToolSpec(...))` 注册结构化工具。`ToolSpec` 描述参数 JSON Schema、`read_only`/`mutating` 属性、`user`/`superuser` 权限、超时、结果上限和依赖；处理函数可接收 `_tool_context: ToolContext` 并返回 `ToolResult`。

变更型工具只有在用户文字包含 `确认执行` 且模型调用参数带 `confirm=true` 时才运行。URL 参数统一拒绝私网、环回、保留地址和云元数据地址。

---

## 注意事项

- 文件名任意（`.py` 后缀），但函数名不能与系统内置工具冲突
- 同一文件中可定义多个工具函数
- 文件工具不得声明 `_bot` / `_event` / `_tool_manager`；可信 Bot 集成请使用独立插件注册 `ToolSpec`
- 工具执行异常时建议 `try/except` 后返回错误描述字符串，而非抛出异常

---

## 相关页面

- [NoneBot 插件集成](./plugin-integration.md) — 让 LLM 调用现有 Bot 插件
- [配置参考 → model_config.json](./configuration.md#model_configjson--智能调度配置) — `use_tools`、`tool_blacklist`、`resident_plugins` 配置项

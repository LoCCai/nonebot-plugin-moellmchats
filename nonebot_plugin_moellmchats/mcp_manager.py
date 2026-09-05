import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from nonebot.log import logger

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from .model_selector import config_path
from .private_files import atomic_write_private_text, ensure_private_file

# list_tools 分页跟随上限：防异常 server 返回永续 nextCursor 导致死循环
_LIST_TOOLS_MAX_PAGES = 32


class McpManager:
    """
    MCP 管理器。

    设计目标：
    1. 从 localstore/mcp_servers.toml 读取 MCP Server 配置。
    2. 刷新工具时发现 MCP tools。
    3. 将 MCP tools 包装成 tool_manager.custom_tools 可直接使用的 schema。
    4. 调用时临时连接对应 MCP Server，执行 tool，再关闭连接。
    """

    def __init__(self):
        self.config_file = Path(config_path / "mcp_servers.toml")
        self.servers: dict[str, dict[str, Any]] = {}

        # full_tool_name -> {"server": server_name, "tool": raw_tool_name}
        self.tool_to_server: dict[str, dict[str, str]] = {}

        self._init_file()
        self.load_config()

    def _init_file(self):
        if self.config_file.exists():
            ensure_private_file(self.config_file)
            return

        template = """# MCP Server 配置文件
# 改完后发送：刷新工具 / 重载工具 / 刷新插件
#
# transport 支持：
#   - stdio
#   - streamable_http
#   - sse
#
# 黑名单示例：
#   添加插件黑名单 mcp__filesystem
#   添加插件黑名单 mcp__filesystem__read_file
#   添加插件黑名单 mcp__filesystem__*
#
# 常驻插件示例：
#   添加常驻插件 mcp__filesystem__read_file


[mcp.filesystem]
enabled = false
transport = "stdio"
command = "uvx"
args = ["mcp-server-filesystem", "/tmp"]
description = "本地文件系统 MCP"
# cwd = "/path/to/workdir"
# timeout = 30
# discover_timeout = 30
# tool_timeout = 60
# result_limit = 6000

[mcp.filesystem.env]
# SOME_TOKEN = "xxx"

[mcp.myapi]
enabled = false
transport = "streamable_http"
url = "http://127.0.0.1:8000/mcp"
description = "HTTP MCP 示例"
# timeout = 30
# discover_timeout = 30
# tool_timeout = 60
# result_limit = 6000

[mcp.legacy_sse]
enabled = false
transport = "sse"
url = "http://127.0.0.1:8000/sse"
description = "旧版 SSE MCP 示例"
# timeout = 30
# discover_timeout = 30
# tool_timeout = 60
# result_limit = 6000

"""
        atomic_write_private_text(self.config_file, template)

    def load_config_candidate(self) -> dict[str, dict[str, Any]]:
        ensure_private_file(self.config_file)
        with self.config_file.open("rb") as file:
            data = tomllib.load(file)
        servers = data.get("mcp", {})
        if not isinstance(servers, dict) or not all(
            isinstance(name, str) and isinstance(conf, dict)
            for name, conf in servers.items()
        ):
            raise ValueError("mcp_servers.toml: 顶层 [mcp.xxx] 必须是配置表")
        return deepcopy(servers)

    def load_config(self):
        try:
            self.servers = self.load_config_candidate()
        except Exception as error:
            logger.error(f"读取 mcp_servers.toml 失败: {error}")
            self.servers = {}

    def configured_server_identifiers(
        self,
        servers: Mapping[str, object] | None = None,
    ) -> frozenset[str]:
        """Freeze the legacy service-level selectors for one MCP candidate."""

        candidate = self.servers if servers is None else servers
        if not isinstance(candidate, Mapping):
            raise TypeError("MCP server candidate 必须是映射")
        return frozenset(
            self._safe_identifier(server_name)
            for server_name, conf in candidate.items()
            if isinstance(server_name, str) and isinstance(conf, dict)
        )

    async def discover_tools(
        self,
        *,
        commit: bool = True,
        servers: dict[str, dict[str, Any]] | None = None,
        strict: bool = False,
    ):
        """
        返回可直接并入 tool_manager.custom_tools 的 schema：

        {
            "mcp__filesystem__read_file": {
                "name": "mcp__filesystem__read_file",
                "description": "[MCP:filesystem] ...",
                "parameters": {...},
                "func": wrapper,
            }
        }
        """
        candidate_servers = (
            deepcopy(servers)
            if servers is not None
            else self.load_config_candidate()
        )
        new_mapping: dict[str, dict[str, str]] = {}

        result: dict[str, dict[str, Any]] = {}

        for server_name, conf in candidate_servers.items():
            if not isinstance(conf, dict):
                logger.warning(f"MCP Server {server_name} 配置不是字典，已跳过")
                continue

            if not conf.get("enabled", True):
                continue

            try:
                tools = await self._list_tools_from_server(server_name, conf)

                for tool in tools:
                    raw_tool_name = getattr(tool, "name", None)
                    if not raw_tool_name:
                        continue

                    full_name = self._build_tool_name(server_name, raw_tool_name)
                    if full_name in result or full_name in new_mapping:
                        # 不同 (server, tool) 归一化后可能碰撞（如 "my.server"
                        # 与 "my_server"），静默覆盖会让其中一个工具凭空消失
                        logger.warning(
                            "MCP 工具名归一化碰撞: {} 已被占用，后发现的 {} 被跳过"
                            "（请调整 server/tool 命名）",
                            full_name,
                            f"{server_name}.{raw_tool_name}",
                        )
                        continue

                    new_mapping[full_name] = {
                        "server": server_name,
                        "tool": raw_tool_name,
                    }

                    result[full_name] = {
                        "name": full_name,
                        "description": self._build_tool_description(
                            server_name,
                            raw_tool_name,
                            tool,
                        ),
                        "parameters": self._get_tool_input_schema(tool),
                        "func": self._make_mcp_wrapper(deepcopy(conf), raw_tool_name),
                    }

                logger.info(
                    f"MCP Server {server_name} 已发现 {len(tools)} 个工具: "
                    f"{[getattr(t, 'name', '') for t in tools]}"
                )

            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"MCP Server {server_name} 发现工具失败: {e}"
                    ) from e
                logger.warning(f"MCP Server {server_name} 加载失败: {e}")

        if commit:
            self.servers = candidate_servers
            self.tool_to_server = new_mapping
            return result
        return result, new_mapping

    def commit_discovery(
        self,
        servers: dict[str, dict[str, Any]],
        mapping: dict[str, dict[str, str]],
    ) -> None:
        """供调用方完成全部校验后原子提交发现结果。

        ``discover_tools(commit=True)`` 是先替换状态、调用方后校验，
        撞名 raise 时会留下"旧工具已弹、新工具未装、mapping 已换"的
        半提交状态；两段式流程应以本方法收尾。
        """

        self.servers = deepcopy(servers)
        self.tool_to_server = mapping

    def _make_mcp_wrapper(
        self,
        bound_conf: dict[str, Any],
        bound_tool_name: str,
    ):
        """为单个 MCP 工具生成绑定配置的执行闭包。

        必须用闭包绑定 conf/工具名：默认参数哨兵（``_mcp_conf=deepcopy(conf)``）
        是具名参数，模型在工具参数里输出 ``{"_mcp_conf": {...stdio command...}}``
        即可覆盖它并经 _run_stdio 执行任意本地命令。wrapper 额外过滤
        ``_`` 前缀键，防止模型参数借 kwargs 透传给远端时夹带私有字段。
        """

        manager = self

        async def wrapper(**kwargs):
            payload = {
                key: value
                for key, value in kwargs.items()
                if not key.startswith("_")
            }
            return await manager.call_tool_with_config(
                bound_conf, bound_tool_name, payload
            )

        return wrapper

    async def call_tool_with_config(
        self, conf: dict[str, Any], tool_name: str, arguments: dict | None
    ):
        try:
            result = await self._call_tool_on_server(
                conf=conf,
                tool_name=tool_name,
                arguments=arguments or {},
            )
            return self._format_result(
                result,
                limit=self._get_int_config(conf, "result_limit", 6000),
            )
        except Exception as error:
            logger.exception(f"MCP 工具调用失败: {tool_name}")
            return f"MCP 工具调用失败: {error}"

    async def call_tool(self, full_tool_name: str, arguments: dict | None):
        mapping = self.tool_to_server.get(full_tool_name)
        if not mapping:
            return f"MCP 工具不存在或尚未刷新: {full_tool_name}"

        server_name = mapping["server"]
        tool_name = mapping["tool"]

        self.load_config()
        conf = self.servers.get(server_name)

        if not conf:
            return f"MCP Server 不存在: {server_name}"

        return await self.call_tool_with_config(conf, tool_name, arguments)

    async def _list_tools_from_server(self, server_name: str, conf: dict) -> list:
        timeout = self._get_int_config(
            conf, "discover_timeout", self._get_timeout(conf)
        )
        timeout = max(timeout, self._get_int_config(conf, "sse_read_timeout", timeout))

        async def op(session):
            # MCP ListToolsResult 带 nextCursor 分页：不跟进会静默丢失
            # 超过分页大小的后半部分工具
            collected: list = []
            cursor = None
            for _ in range(_LIST_TOOLS_MAX_PAGES):
                response = (
                    await session.list_tools(cursor=cursor)
                    if cursor is not None
                    else await session.list_tools()
                )
                collected.extend(list(getattr(response, "tools", []) or []))
                cursor = getattr(response, "nextCursor", None)
                if not cursor:
                    break
            else:
                logger.warning(
                    "MCP Server {} list_tools 分页超过 {} 页上限，结果可能被截断",
                    server_name,
                    _LIST_TOOLS_MAX_PAGES,
                )
            return collected

        return await asyncio.wait_for(
            self._run_with_session(conf, op),
            timeout=timeout,
        )

    async def _call_tool_on_server(
        self,
        conf: dict,
        tool_name: str,
        arguments: dict,
    ):
        timeout = self._get_int_config(conf, "tool_timeout", self._get_timeout(conf))
        timeout = max(timeout, self._get_int_config(conf, "sse_read_timeout", timeout))

        async def op(session):
            return await session.call_tool(tool_name, arguments=arguments)

        return await asyncio.wait_for(
            self._run_with_session(conf, op),
            timeout=timeout,
        )

    async def _run_with_session(
        self,
        conf: dict,
        op: Callable[[Any], Awaitable[Any]],
    ):
        transport = self._normalize_transport(conf.get("transport", "stdio"))

        if transport == "stdio":
            return await self._run_stdio(conf, op)

        if transport == "streamable_http":
            return await self._run_streamable_http(conf, op)

        if transport == "sse":
            return await self._run_sse(conf, op)

        raise ValueError(
            f"不支持的 MCP transport: {transport}，"
            "可选值: stdio / streamable_http / sse"
        )

    async def _run_stdio(
        self,
        conf: dict,
        op: Callable[[Any], Awaitable[Any]],
    ):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = str(conf.get("command") or "").strip()
        if not command:
            raise ValueError("stdio MCP 配置缺少 command")

        args = conf.get("args", [])
        if args is None:
            args = []
        if not isinstance(args, list):
            raise ValueError("stdio MCP 配置中的 args 必须是数组")

        env = conf.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError("stdio MCP 配置中的 env 必须是字典")

        # 只传 SDK 安全白名单（PATH/HOME/SYSTEMROOT 等已含其中）加常用
        # 代理变量，不把宿主全量环境变量（LLM key、DB 密码等机密）泄露
        # 给第三方 MCP server 进程；显式配置的 conf.env 仍然生效并优先。
        from mcp.client.stdio import get_default_environment

        try:
            merged_env = dict(get_default_environment())
        except Exception:
            merged_env = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "TEMP", "TMP", "SYSTEMROOT")
                if key in os.environ
            }
        for proxy_key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ):
            if proxy_key in os.environ:
                merged_env.setdefault(proxy_key, os.environ[proxy_key])
        merged_env.update({str(k): str(v) for k, v in env.items()})

        params_kwargs = {
            "command": command,
            "args": [str(x) for x in args],
            "env": merged_env,
        }

        # 兼容不同版本 SDK：有些版本 StdioServerParameters 支持 cwd，有些不支持。
        cwd = conf.get("cwd")
        if cwd:
            try:
                sig = inspect.signature(StdioServerParameters)
                if "cwd" in sig.parameters:
                    params_kwargs["cwd"] = str(cwd)
            except Exception:
                pass

        server_params = StdioServerParameters(**params_kwargs)

        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)

    async def _run_streamable_http(
        self,
        conf: dict,
        op: Callable[[Any], Awaitable[Any]],
    ):
        url = str(conf.get("url") or "").strip()
        if not url:
            raise ValueError("streamable_http MCP 配置缺少 url")

        async with streamable_http_client(url) as streams:
            read_stream, write_stream = streams[0], streams[1]

            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)

    async def _run_sse(
        self,
        conf: dict,
        op: Callable[[Any], Awaitable[Any]],
    ):
        url = str(conf.get("url") or "").strip()
        if not url:
            raise ValueError("sse MCP 配置缺少 url")


        timeout = self._get_timeout(conf)
        sse_read_timeout = self._get_int_config(
            conf,
            "sse_read_timeout",
            max(timeout, 300),
        )

        kwargs = {}
        try:
            sig = inspect.signature(sse_client)
            if "timeout" in sig.parameters:
                kwargs["timeout"] = timeout
            if "sse_read_timeout" in sig.parameters:
                kwargs["sse_read_timeout"] = sse_read_timeout
        except Exception:
            pass

        async with sse_client(url, **kwargs) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)

    def _normalize_transport(self, transport: str) -> str:
        t = str(transport or "stdio").strip().lower().replace("-", "_")

        if t in {"stdio", "local"}:
            return "stdio"

        if t in {"streamable_http", "http", "streamable"}:
            return "streamable_http"

        if t in {"sse", "legacy_sse"}:
            return "sse"

        return t

    def _build_tool_name(self, server_name: str, tool_name: str) -> str:
        """
        生成 OpenAI function name 兼容的工具名。

        常规：
            mcp__filesystem__read_file

        如果超过 64 字符，自动截断并附带 hash，避免部分模型或 API 拒绝。
        """
        safe_server = self._safe_identifier(server_name)
        safe_tool = self._safe_identifier(tool_name)

        base = f"mcp__{safe_server}__{safe_tool}"

        if len(base) <= 64:
            return base

        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        return f"mcp__{safe_server[:16]}__{safe_tool[:30]}__{digest}"

    def _safe_identifier(self, value: str) -> str:
        value = str(value or "").strip()
        value = re.sub(r"[^a-zA-Z0-9_-]+", "_", value)
        value = value.strip("_-")
        return value or "unnamed"

    def _build_tool_description(
        self,
        server_name: str,
        raw_tool_name: str,
        tool: Any,
    ) -> str:
        description = getattr(tool, "description", "") or ""
        description = str(description).strip()

        title = getattr(tool, "title", "") or ""
        title = str(title).strip()

        if title and description:
            return f"[MCP:{server_name}] {title}。{description}"

        if description:
            return f"[MCP:{server_name}] {description}"

        if title:
            return f"[MCP:{server_name}] {title}"

        return f"[MCP:{server_name}] {raw_tool_name}"

    def _get_tool_input_schema(self, tool: Any) -> dict:
        schema = getattr(tool, "inputSchema", None)

        if not schema:
            return {
                "type": "object",
                "properties": {},
            }

        if isinstance(schema, dict):
            return self._normalize_json_schema(schema)

        # pydantic v2
        if hasattr(schema, "model_dump"):
            try:
                return self._normalize_json_schema(schema.model_dump())
            except Exception:
                pass

        # pydantic v1
        if hasattr(schema, "dict"):
            try:
                return self._normalize_json_schema(schema.dict())
            except Exception:
                pass

        return {
            "type": "object",
            "properties": {},
        }

    def _normalize_json_schema(self, schema: dict) -> dict:
        """
        确保传给 OpenAI-compatible tools 的 parameters 至少是 object schema。
        """
        if not isinstance(schema, dict):
            return {
                "type": "object",
                "properties": {},
            }

        schema = dict(schema)

        if not schema.get("type"):
            schema["type"] = "object"

        if schema.get("type") == "object" and "properties" not in schema:
            schema["properties"] = {}

        return schema

    def _format_result(self, result: Any, limit: int = 6000) -> dict:
        """
        将 MCP CallToolResult 转成自定义函数可识别的结构：
        {
            "text": "...",
            "images": ["https://...", "data:image/png;base64,..."]
        }

        注意：
        - 图片会返回到 images，供 MoeLlm 注入 _pending_vision_images 后切换视觉模型。
        - 音频暂不兼容，只写后台 warning，并返回文本提示。
        """
        text_parts: list[str] = []
        image_urls: list[str] = []

        is_error = getattr(result, "isError", None)
        if is_error:
            text_parts.append("[MCP工具返回错误]")

        structured = getattr(result, "structuredContent", None)
        if structured:
            try:
                text_parts.append(
                    "结构化结果：\n"
                    + json.dumps(structured, ensure_ascii=False, indent=2)
                )
            except Exception:
                text_parts.append(f"结构化结果：\n{structured}")

        content = getattr(result, "content", None)
        if content:
            for item in content:
                text, images = self._format_content_item(item)
                if text:
                    text_parts.append(text)
                if images:
                    image_urls.extend(images)

        if not text_parts and not image_urls:
            text_parts.append(str(result))

        final_text = "\n".join(text_parts).strip()

        if limit and len(final_text) > limit:
            final_text = final_text[:limit] + "\n\n...[MCP返回文本内容过长，已自动截断]"

        return {
            "text": final_text,
            "images": image_urls,
        }

    def _format_content_item(self, item: Any) -> tuple[str, list[str]]:
        """
        兼容 MCP TextContent / ImageContent / AudioContent / EmbeddedResource。

        返回:
            (text, images)

        images 内的元素必须是视觉模型能吃的 URL：
        - 远程图片：直接 URL
        - base64 图片：data:image/png;base64,...
        """
        images: list[str] = []

        item_type = getattr(item, "type", None)
        item_type = str(item_type or "").lower()

        mime_type = (
            getattr(item, "mimeType", None) or getattr(item, "mime_type", None) or ""
        )
        mime_type = str(mime_type or "")

        # 1. TextContent
        text = getattr(item, "text", None)
        if text:
            return str(text), images

        # 2. 有些 MCP Server 可能直接给 url / uri
        direct_url = (
            getattr(item, "url", None)
            or getattr(item, "uri", None)
            or getattr(item, "href", None)
        )
        if direct_url:
            direct_url = str(direct_url)

            if item_type == "image" or mime_type.startswith("image/"):
                images.append(direct_url)
                return f"[MCP返回图片: {direct_url}]", images

            if item_type == "audio" or mime_type.startswith("audio/"):
                logger.warning(
                    f"MCP 返回了音频内容，但当前暂不兼容音频处理: {direct_url}"
                )
                return "[MCP返回了音频内容，但当前暂不兼容。请查看后台日志。]", images

            return f"[MCP返回资源: {direct_url}]", images

        # 3. ImageContent / AudioContent：通常是 base64 data
        data = getattr(item, "data", None)
        if data:
            data = str(data)

            if item_type == "image" or mime_type.startswith("image/"):
                if not mime_type:
                    mime_type = "image/png"

                # 如果 MCP 已经返回 data URL，直接用
                if data.startswith("data:image/"):
                    image_url = data
                else:
                    image_url = f"data:{mime_type};base64,{data}"

                images.append(image_url)
                return "[MCP返回图片，已交给视觉模型处理]", images

            if item_type == "audio" or mime_type.startswith("audio/"):
                logger.warning(
                    "MCP 返回了音频内容，但当前暂不兼容音频处理。"
                    f"mime={mime_type or 'unknown'}，数据长度={len(data)}"
                )
                return "[MCP返回了音频内容，暂不兼容。]", images

            logger.warning(
                "MCP 返回了暂不支持的二进制内容。"
                f"type={item_type or 'unknown'}，mime={mime_type or 'unknown'}"
            )
            return (
                f"[MCP返回了暂不支持的二进制内容: "
                f"type={item_type or 'unknown'}, mime={mime_type or 'unknown'}]",
                images,
            )

        # 4. EmbeddedResource
        resource = getattr(item, "resource", None)
        if resource is not None:
            resource_text = getattr(resource, "text", None)
            resource_uri = getattr(resource, "uri", None)
            resource_mime = (
                getattr(resource, "mimeType", None)
                or getattr(resource, "mime_type", None)
                or mime_type
            )
            resource_mime = str(resource_mime or "")

            if resource_uri:
                resource_uri = str(resource_uri)

                if resource_mime.startswith("image/"):
                    images.append(resource_uri)
                    return f"[MCP返回图片资源: {resource_uri}]", images

                if resource_mime.startswith("audio/"):
                    logger.warning(
                        f"MCP 返回了音频资源，但当前暂不兼容音频处理: {resource_uri}"
                    )
                    return (
                        "[MCP返回了音频资源，但当前暂不兼容。请查看后台日志。]",
                        images,
                    )

            if resource_text:
                if resource_uri:
                    return f"[MCP资源: {resource_uri}]\n{resource_text}", images
                return str(resource_text), images

            return f"[MCP返回资源: {resource_uri or resource}]", images

        return str(item), images

    def _get_timeout(self, conf: dict) -> int:
        return self._get_int_config(conf, "timeout", 30)

    def _get_int_config(self, conf: dict, key: str, default: int) -> int:
        try:
            value = int(conf.get(key, default))
            return max(1, value)
        except Exception:
            return default


mcp_manager = McpManager()

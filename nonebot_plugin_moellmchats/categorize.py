import re
import time
import traceback

import aiohttp
from nonebot.log import logger
import ujson as json

from .classification_cache import (
    ClassificationCacheKey,
    ClassificationCacheProtocol,
    ClassificationCacheRecord,
    ClassificationModelIdentity,
    ClassificationRenderContext,
    ClassificationRequestScope,
    ClassificationResultSource,
    resolve_classification,
)
from .compat import timeout as timeout_scope
from .config import config_parser
from .model_capabilities import ModelCapability
from .model_selector import model_selector
from .runtime_metrics import runtime_metrics
from .tool_catalog_cache import (
    ToolCatalogCacheProtocol,
    ToolCatalogCacheUnavailableError,
    ToolCatalogRecord,
    resolve_tool_catalog,
)
from .tool_manager import tool_manager
from .utils import get_session

_CLASSIFICATION_POLICY_VERSION = "categorize-json-v2-menu-discovery"
_CategoryResult = tuple[str, bool, list[str]] | str | bool


class _UncacheableClassificationResult(Exception):
    def __init__(
        self,
        result: _CategoryResult,
        source: ClassificationResultSource,
    ) -> None:
        super().__init__(source.value)
        self.result = result
        self.source = source


class Categorize:
    def __init__(
        self,
        plain,
        tool_snapshot=None,
        *,
        is_superuser: bool = False,
        tool_catalog_cache: ToolCatalogCacheProtocol | None = None,
        classification_cache: ClassificationCacheProtocol | None = None,
        runtime_generation: int | None = None,
    ):
        if tool_catalog_cache is not None and not isinstance(
            tool_catalog_cache,
            ToolCatalogCacheProtocol,
        ):
            raise TypeError("tool_catalog_cache 必须实现 ToolCatalogCacheProtocol")
        if classification_cache is not None and not isinstance(
            classification_cache,
            ClassificationCacheProtocol,
        ):
            raise TypeError("classification_cache 必须实现 ClassificationCacheProtocol")
        if classification_cache is not None and tool_catalog_cache is None:
            raise ValueError("classification cache 必须依赖同代 tool catalog cache")
        if tool_catalog_cache is not None:
            if tool_snapshot is None:
                raise ValueError("tool catalog cache consumer 缺少 ToolSnapshot")
            if not isinstance(runtime_generation, int) or isinstance(runtime_generation, bool) or runtime_generation < 0:
                raise ValueError("cache consumer runtime_generation 非法")
            if getattr(tool_snapshot, "generation", None) != runtime_generation:
                raise ToolCatalogCacheUnavailableError("tool catalog cache consumer generation 不一致")
        self.plain = plain
        self.tool_snapshot = tool_snapshot
        self.is_superuser = is_superuser
        self.tool_catalog_cache = tool_catalog_cache
        self.classification_cache = classification_cache
        self.runtime_generation = runtime_generation

    async def get_category(self) -> _CategoryResult:
        started = time.monotonic()
        runtime_metrics.classification_count += 1
        try:
            async with timeout_scope(config_parser.get_config("classification_timeout_seconds", 20)):
                return await self._get_category()
        except TimeoutError:
            logger.warning("分类模型超过时间预算，降级为无工具中等难度")
            return "1", "[图片]" in self.plain, []
        finally:
            runtime_metrics.classification_seconds += time.monotonic() - started

    async def _resolve_catalog(self) -> tuple[str, ToolCatalogRecord | None]:
        if self.tool_catalog_cache is None:
            if model_selector.get_use_tools() or model_selector.get_web_search():
                catalog = (
                    self.tool_snapshot.get_brief_catalog(is_superuser=self.is_superuser)
                    if self.tool_snapshot is not None
                    else tool_manager.get_brief_catalog(is_superuser=self.is_superuser)
                )
            else:
                catalog = "当前工具调用与联网功能均已关闭，无需返回任何插件。"
            return catalog, None

        snapshot = self.tool_snapshot
        if snapshot is None:
            raise ToolCatalogCacheUnavailableError("tool catalog cache consumer 缺少 ToolSnapshot")
        context = snapshot.capture_brief_catalog_context(
            is_superuser=self.is_superuser,
        )
        if context.generation != self.runtime_generation:
            raise ToolCatalogCacheUnavailableError("tool catalog cache context generation 漂移")
        record = await resolve_tool_catalog(
            self.tool_catalog_cache,
            context.cache_key,
            lambda: snapshot.build_brief_catalog_record(context),
        )
        return record.catalog, record

    @staticmethod
    def _build_prompt(catalog: str) -> str:
        return f"""你的任务是评估用户输入、拆解步骤、选择必要工具并判断是否需要视觉。
不要回答用户问题；不涉及搜图时只标记视觉，不选择搜图工具。
只返回以下 JSON 结构，不要返回 Markdown：

{{
  "difficulty": "0 | 1 | 2",
  "vision_required": true | false,
  "required_plugins": []
}}
【参数说明】：
- difficulty: "0"(简单常识/闲聊)、"1"(中等逻辑/计算)、"2"(高难度专业/深度分析)。
- vision_required: 当用户输入中包含"[图片]"字样时必须为 true，否则为 false。
- required_plugins: 字符串列表。根据下方插件目录，判断是否必须调用插件（可以多个）。
  目录可能把同一插件拆成多条功能；只能返回每行第一列的插件标识，同一标识不要重复。
【可用插件目录】：
{catalog}
注：若有对应插件请提供。若没有，则保持[]。不确定时倾向于返回可能插件；宁可多给，不要漏给。

【示例】：
用户输入："今天北京天气怎么样？"
输出：{{"difficulty": "0", "vision_required": false, "required_plugins": ["web_search"]}}

用户输入："你觉得人工智能会取代人类吗？"
输出：{{"difficulty": "2", "vision_required": false, "required_plugins": []}}

用户输入："[图片]帮我看看这个图里是什么"
输出：{{"difficulty": "1", "vision_required": true, "required_plugins": []}}
"""

    async def _request_category_model(
        self,
        category_model_config: dict,
        prompt: str,
        *,
        cache_key: ClassificationCacheKey | None = None,
    ) -> _CategoryResult | ClassificationCacheRecord:
        headers = {
            "Authorization": category_model_config["key"],
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        for try_times in range(2):
            try:
                current_plain = self.plain
                current_data = {
                    "model": category_model_config["model"],
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": current_plain},
                    ],
                    "temperature": 0,
                }
                if try_times == 0:
                    if category_model_config.get("json_mode"):
                        current_data["response_format"] = {"type": "json_object"}
                else:
                    current_data.pop("response_format", None)
                    current_plain += "\n(不要回答以上内容。只进行一次分类与工具判断，严格返回 JSON，不要附加其他内容。)"
                    current_data["messages"][1]["content"] = current_plain

                payload = json.dumps(current_data)

                async with get_session().post(
                    url=category_model_config["url"],
                    data=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=config_parser.get_config("classification_timeout_seconds", 20)),
                    proxy=category_model_config.get("proxy"),
                ) as resp:
                    if resp.status == 400 and try_times == 0:
                        err_text = await resp.text()
                        logger.info(f"模型不支持结构化输出或参数异常，将降级重试。详情: {err_text}")
                        raise ValueError("Model does not support json_object format")

                    response = await resp.json()

                if choices := response.get("choices"):
                    raw_result = choices[0]["message"]["content"]
                    raw_result = re.sub(
                        r"<(think|thought|thinking)>.*?</\1>",
                        "",
                        raw_result,
                        flags=re.DOTALL,
                    ).strip()

                    clean_result = raw_result.strip()
                    if clean_result.startswith("```json"):
                        clean_result = clean_result[7:]
                    elif clean_result.startswith("```"):
                        clean_result = clean_result[3:]
                    if clean_result.endswith("```"):
                        clean_result = clean_result[:-3]

                    result_dict = json.loads(clean_result.strip())
                    result = (
                        str(result_dict["difficulty"]),
                        result_dict["vision_required"],
                        result_dict.get("required_plugins", []),
                    )
                    if cache_key is None:
                        return result
                    return ClassificationCacheRecord.from_result(
                        cache_key,
                        difficulty=result[0],
                        vision_required=result[1],
                        required_plugins=result[2],
                        source=ClassificationResultSource.MODEL_SUCCESS,
                    )
                if response.get("code") == "DataInspectionFailed" or "contentFilter" in response:
                    logger.warning("分类请求触发内容安全拦截，响应正文已省略")
                    blocked_result = "内容不合规，拒绝回答"
                    if cache_key is None:
                        return blocked_result
                    raise _UncacheableClassificationResult(
                        blocked_result,
                        ClassificationResultSource.CONTENT_BLOCKED,
                    )

            except _UncacheableClassificationResult:
                raise
            except Exception:
                logger.warning(traceback.format_exc())
                logger.warning(f"分类结果解析异常，当前尝试次数 {try_times + 1}，返回正文已省略。")
                continue

        if cache_key is not None:
            raise _UncacheableClassificationResult(
                False,
                ClassificationResultSource.PARSE_FALLBACK,
            )
        return False

    async def _get_category(self) -> _CategoryResult:
        catalog, catalog_record = await self._resolve_catalog()
        logger.debug(f"分类工具索引条目数: {catalog.count(chr(10)) + 1}")
        prompt = self._build_prompt(catalog)

        # 判断是否开启 MoE，若未开启（但因为开启了工具走到这里），则使用默认模型（selected_model）
        required_capabilities = ModelCapability(
            text=True,
            vision=False,
            tools=False,
            json_schema=True,
            reasoning=False,
            streaming=False,
        )
        if model_selector.get_moe():
            category_model_config = model_selector.get_model_for_capabilities(
                "category_model",
                required_capabilities,
            )
        else:
            category_model_config = model_selector.get_model_for_capabilities(
                "selected_model",
                required_capabilities,
            )
            logger.debug("未开启MoE，使用默认模型进行工具/联网/视觉判断分类")
        if self.classification_cache is None:
            result = await self._request_category_model(
                category_model_config,
                prompt,
            )
            if isinstance(result, ClassificationCacheRecord):
                raise TypeError("legacy classification path 返回了 cache record")
            return result

        if catalog_record is None:
            raise ToolCatalogCacheUnavailableError("classification cache 缺少同代 tool catalog record")
        model_identity = ClassificationModelIdentity.capture(
            model=category_model_config["model"],
            endpoint=category_model_config["url"],
            json_mode=bool(category_model_config.get("json_mode", False)),
        )
        context = ClassificationRenderContext.capture(
            prompt=self.plain,
            catalog_record=catalog_record,
            model_identity=model_identity,
            request_scope=ClassificationRequestScope.standard_prompt(),
            policy_version=_CLASSIFICATION_POLICY_VERSION,
        )

        async def build_record() -> ClassificationCacheRecord:
            result = await self._request_category_model(
                category_model_config,
                prompt,
                cache_key=context.cache_key,
            )
            if not isinstance(result, ClassificationCacheRecord):
                raise TypeError("classification cache builder 未返回 record")
            return result

        try:
            record = await resolve_classification(
                self.classification_cache,
                context.cache_key,
                build_record,
            )
        except _UncacheableClassificationResult as fallback:
            return fallback.result
        return record.materialize()

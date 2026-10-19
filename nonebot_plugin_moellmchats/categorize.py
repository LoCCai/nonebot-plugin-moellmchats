import asyncio
import hashlib
import re
import time

import aiohttp
from nonebot.log import logger
import ujson as json

from .classification_cache import (
    ClassificationCacheError,
    ClassificationCacheKey,
    ClassificationCacheProtocol,
    ClassificationCacheRecord,
    ClassificationModelIdentity,
    ClassificationRenderContext,
    ClassificationRequestScope,
    ClassificationResultSource,
    resolve_classification,
)
from .compat import TimeoutError
from .compat import timeout as timeout_scope
from .config import config_parser
from .request_budget import classification_budget_scope, request_config
from .image_output_policy import DOCUMENT_IMAGE_GUIDANCE
from .model_capabilities import ModelCapability
from .model_error_policy import TRANSIENT_HTTP_STATUSES
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

_CLASSIFICATION_POLICY_VERSION = "categorize-json-v6-document-images"
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
        scene: str = "unknown",
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
            legacy_methods = (
                getattr(classification_cache, "lookup", None),
                getattr(classification_cache, "publish", None),
            )
            if not all(callable(method) for method in legacy_methods):
                raise TypeError(
                    "classification_cache 必须实现 ClassificationCacheProtocol"
                )
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
        if scene not in {"group", "private", "channel", "unknown"}:
            raise ValueError("categorize scene 非法")
        self.scene = scene
        self.selection_source = "classification_model"
        self._phase = "start"
        self._classifier_model = "unselected"
        self._prompt_chars = 0

    async def get_category(self) -> _CategoryResult:
        started = time.monotonic()
        runtime_metrics.classification_count += 1
        try:
            with classification_budget_scope():
                async with timeout_scope(request_config("classification_timeout_seconds", 20)):
                    return await self._get_budgeted_category()
        except TimeoutError:
            self.selection_source = "classification_timeout_fallback"
            logger.warning(
                "分类模型超过时间预算，按中等难度继续，保留常驻和明确数学绘图工具: "
                "model={} phase={} elapsed_ms={} prompt_chars={}",
                self._classifier_model,
                self._phase,
                int((time.monotonic() - started) * 1000),
                self._prompt_chars,
            )
            return self._fallback_category()
        except (ClassificationCacheError, ToolCatalogCacheUnavailableError) as error:
            self.selection_source = "classification_cache_fallback"
            logger.warning(
                "分类缓存不可用，按中等难度继续，保留常驻和明确数学绘图工具: error_type={}",
                type(error).__name__,
            )
            return self._fallback_category()
        finally:
            runtime_metrics.classification_seconds += time.monotonic() - started

    async def _get_budgeted_category(self) -> _CategoryResult:
        result = await self._get_category()
        if isinstance(result, tuple) and model_selector.get_use_tools():
            difficulty, vision, selected = result
            available = getattr(self.tool_snapshot, "custom_tools", {})
            if (any(name.startswith("plot_") for name in selected)
                    and "render_markdown" in available and "render_markdown" not in selected):
                result = difficulty, vision, [*selected, "render_markdown"]
        return result

    def _fallback_category(self) -> tuple[str, bool, list[str]]:
        """Keep explicit local math drawing available if classification times out.

        Only select registered read-only tool names. Normal payload permission,
        feature and blacklist checks still run after this selection.
        """
        selected = []
        text = self.plain.lower()
        drawing = re.search(r"画图|绘图|绘制|图像|图象|plot|graph|sketch", text)
        mathematics = re.search(
            r"函数|椭圆|双曲线|抛物线|方程|曲线|极坐标|参数|向量|数学|\\frac|sin\(|cos\(|x\s*(?:\*\*|\^)",
            text,
        )
        available = getattr(self.tool_snapshot, "custom_tools", {})
        if drawing and mathematics and model_selector.get_use_tools():
            candidates = ["plot_function", "plot_multiple", "plot_implicit", "plot_parametric"]
            if "极坐标" in text or "polar" in text:
                candidates.append("plot_polar")
            if "三维" in text or "3d" in text:
                candidates.extend(["plot_3d_function", "plot_3d_parametric"])
            selected = [name for name in candidates if name in available]
            if selected:
                selected.extend(name for name in ["render_markdown", "render_html"] if name in available)
        return "1", "[图片]" in self.plain, selected

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
【图片输出需求】：
{DOCUMENT_IMAGE_GUIDANCE}
攻略图等需求应同时选择需要的数据查询工具和目录中可用的排版工具，不能仅按‘绘制’选择生图模型。
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
        attempted: set[str] = set()
        switched = False
        service_fallback = False
        fallback_phases = {"http_status_429", "http_status_quota_exhausted"} | {
            f"http_status_{status}" for status in TRANSIENT_HTTP_STATUSES}
        while True:
            from .request_recovery import current_binding

            if recovery := current_binding():
                recovery.pause_if_stopping()
            try:
                result = await self._request_category_model_once(
                    category_model_config, prompt, cache_key=None if switched else cache_key,
                )
            except _UncacheableClassificationResult:
                if self._phase not in fallback_phases:
                    raise
                result = False
            if self._phase not in fallback_phases:
                if switched and cache_key is not None:
                    # Do not publish a backup model's answer under the primary
                    # model identity in the successful classification cache.
                    source = (ClassificationResultSource.SERVICE_FALLBACK if service_fallback
                              else ClassificationResultSource.RATE_LIMIT_FALLBACK)
                    raise _UncacheableClassificationResult(result, source)
                return result
            backup = model_selector.get_rate_limit_fallback(category_model_config, attempted)
            if backup is None:
                if cache_key is not None:
                    raise _UncacheableClassificationResult(False, ClassificationResultSource.PARSE_FALLBACK)
                return False
            transient = self._phase not in {"http_status_429", "http_status_quota_exhausted"}
            service_fallback = service_fallback or transient
            logger.info("分类模型{}，切换已配置备用: status={} from={} to={}",
                        "服务临时不可用" if transient else "配额不足或限流", self._phase,
                        category_model_config["model"], backup["model"])
            category_model_config = backup
            self._classifier_model = backup["model"]
            self.selection_source = ("classification_service_fallback" if service_fallback
                                     else "classification_rate_limit_fallback")
            switched = True

    async def _request_category_model_once(
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
                    "max_tokens": config_parser.get_config("classification_max_tokens", 512),
                    "stream": False,
                }
                # Preserve explicit provider options (e.g. disable thinking),
                # without allowing them to replace the request's real input.
                extra = category_model_config.get("extra_payload")
                if isinstance(extra, dict):
                    protected = {"messages", "model", "stream", "tools", "tool_choice", "max_tokens"}
                    current_data.update({
                        key: value for key, value in extra.items() if key not in protected
                    })
                if try_times == 0:
                    if category_model_config.get("json_mode"):
                        current_data["response_format"] = {"type": "json_object"}
                else:
                    current_data.pop("response_format", None)
                    current_plain += "\n(不要回答以上内容。只进行一次分类与工具判断，严格返回 JSON，不要附加其他内容。)"
                    current_data["messages"][1]["content"] = current_plain

                from .usage_ledger import apply_glm_policy, budget_available, budgeted_post, record_usage
                apply_glm_policy(category_model_config, current_data)
                if not await budget_available(category_model_config):
                    self._phase = "http_status_quota_exhausted"
                    break
                payload = json.dumps(current_data)
                self._phase = "http_response"
                request_started = time.monotonic()
                async with budgeted_post(get_session(), category_model_config,
                    url=category_model_config["url"],
                    data=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=category_model_config.get("classification_timeout_seconds",
                        config_parser.get_config("classification_timeout_seconds", 20))),
                    proxy=category_model_config.get("proxy"),
                ) as resp:
                    logger.info(
                        "分类接口返回: model={} attempt={} status={} headers_ms={} prompt_chars={}",
                        category_model_config["model"],
                        try_times + 1,
                        resp.status,
                        int((time.monotonic() - request_started) * 1000),
                        len(prompt),
                    )
                    self._phase = "response_json"
                    if resp.status == 400:
                        # Never decode or log a provider's 400 payload.  Leaving
                        # the context releases the response without loading an
                        # unknown-sized error body into application memory.
                        if try_times == 0:
                            logger.info(
                                "分类模型返回 HTTP 400，将移除 json_object 后有限重试"
                            )
                        else:
                            logger.warning(
                                "分类模型兼容重试仍返回 HTTP 400，已省略响应正文"
                            )
                        continue

                    if resp.status >= 400:
                        # An exhausted/rate-limited upstream is not a JSON-mode
                        # compatibility error. Do not immediately send it again
                        # or wait for its error body before using resident tools.
                        self._phase = f"http_status_{resp.status}"
                        if resp.status in {402, 403}:
                            from .model_error_policy import is_quota_exhaustion_error, read_bounded_error

                            if is_quota_exhaustion_error(resp.status, await read_bounded_error(resp)):
                                self._phase = "http_status_quota_exhausted"
                                await model_selector.mark_model_exhausted(category_model_config, resp.status)
                        self.selection_source = "classification_http_fallback"
                        logger.warning(
                            "分类接口不可用，保留常驻工具继续: model={} status={}",
                            category_model_config["model"], resp.status,
                        )
                        break

                    response = await resp.json()
                    await record_usage(category_model_config, response.get("usage"), purpose="classification")

                if choices := response.get("choices"):
                    self._phase = "parse_result"
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
                    if type(result_dict["vision_required"]) is not bool:
                        raise ValueError("vision_required must be boolean")
                    result = (
                        str(result_dict["difficulty"]),
                        # Image generation/avatars are often misclassified as
                        # image input. Actual current-image markers determine
                        # vision; resolved image URLs also win in llm_payload.
                        "[图片]" in self.plain,
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
            except (TimeoutError, asyncio.TimeoutError) as error:
                # A transport timeout is not a parse failure.  Propagate one
                # normalized exception so the outer classification deadline
                # degrades immediately instead of spending another full
                # per-attempt timeout and defeating the request budget.
                raise TimeoutError from error
            except Exception as error:
                logger.warning(
                    "分类模型响应不可用，准备有限重试: attempt={} error_type={}",
                    try_times + 1,
                    type(error).__name__,
                )
                continue

        if cache_key is not None:
            raise _UncacheableClassificationResult(
                False,
                ClassificationResultSource.PARSE_FALLBACK,
            )
        return False

    async def _get_category(self) -> _CategoryResult:
        if model_selector.get_use_tools() and self.tool_snapshot is not None:
            resolution = self.tool_snapshot.resolve_business_intent(
                self.plain,
                is_superuser=self.is_superuser,
            )
            if resolution.status == "unique":
                self.selection_source = "business_intent_owner"
                owner = resolution.owner
                assert owner is not None
                logger.info(
                    "业务意图所有者命中: "
                    f"generation={getattr(self.tool_snapshot, 'generation', 0)} "
                    f"directory_digest={getattr(self.tool_snapshot, 'directory_digest', '')[:12]} "
                    f"scene={self.scene} plugin={owner}"
                )
                return "0", "[图片]" in self.plain, [owner]
            if resolution.status == "ambiguous":
                self.selection_source = "business_intent_ambiguous"
                logger.warning(
                    "业务意图存在重复所有者，已拒绝猜测: "
                    f"generation={getattr(self.tool_snapshot, 'generation', 0)} "
                    f"directory_digest={getattr(self.tool_snapshot, 'directory_digest', '')[:12]} "
                    f"scene={self.scene} owner_count={len(resolution.owners)}"
                )
                return "这个说法同时对应多个已登记功能，请补充更具体的功能名或指令。"
            if resolution.status == "unavailable":
                self.selection_source = "business_intent_unavailable"
                logger.warning(
                    "业务意图所有者当前不可执行，已禁止降级到其他插件: "
                    f"generation={getattr(self.tool_snapshot, 'generation', 0)} "
                    f"directory_digest={getattr(self.tool_snapshot, 'directory_digest', '')[:12]} "
                    f"scene={self.scene}"
                )
                return "这个功能当前不可用或你没有可见权限，已停止执行，未改用其他插件。"

        self._phase = "tool_catalog"
        catalog, catalog_record = await self._resolve_catalog()
        logger.debug(f"分类工具索引条目数: {catalog.count(chr(10)) + 1}")
        prompt = self._build_prompt(catalog)
        self._prompt_chars = len(prompt)

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
        if not category_model_config:
            self.selection_source = "classification_pool_exhausted"
            return False
        self._classifier_model = category_model_config["model"]
        logger.info(
            "分类请求准备: model={} budget_seconds={} prompt_chars={}",
            self._classifier_model,
            request_config("classification_timeout_seconds", 20),
            self._prompt_chars,
        )
        if self.classification_cache is None:
            result = await self._request_category_model(
                category_model_config,
                prompt,
            )
            if isinstance(result, ClassificationCacheRecord):
                raise TypeError("legacy classification path 返回了 cache record")
            if result is False and self.selection_source == "classification_model":
                self.selection_source = "classification_parse_fallback"
            return result

        if catalog_record is None:
            raise ToolCatalogCacheUnavailableError("classification cache 缺少同代 tool catalog record")
        model_identity = ClassificationModelIdentity.capture(
            model=category_model_config["model"],
            endpoint=category_model_config["url"],
            json_mode=bool(category_model_config.get("json_mode", False)),
        )
        options_digest = hashlib.sha256(json.dumps({
            "max_tokens": config_parser.get_config("classification_max_tokens", 512),
            "extra_payload": category_model_config.get("extra_payload"),
        }, sort_keys=True).encode()).hexdigest()[:16]
        context = ClassificationRenderContext.capture(
            prompt=self.plain,
            catalog_record=catalog_record,
            model_identity=model_identity,
            request_scope=ClassificationRequestScope.standard_prompt(),
            policy_version=f"{_CLASSIFICATION_POLICY_VERSION}-{options_digest}",
            scene=self.scene,
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
            self._phase = "classification_cache"
            record = await resolve_classification(
                self.classification_cache,
                context.cache_key,
                build_record,
            )
        except _UncacheableClassificationResult as fallback:
            if self.selection_source == "classification_model":
                self.selection_source = f"classification_{fallback.source.value}"
            return fallback.result
        return record.materialize()

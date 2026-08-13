import re
import time
import traceback

import aiohttp
from nonebot.log import logger
import ujson as json

from .compat import timeout as timeout_scope
from .config import config_parser
from .model_selector import model_selector
from .runtime_metrics import runtime_metrics
from .tool_manager import tool_manager
from .utils import get_session


class Categorize:
    def __init__(self, plain, tool_snapshot=None):
        self.plain = plain
        self.tool_snapshot = tool_snapshot

    async def get_category(self) -> tuple[str, bool, list] | str | bool:
        started = time.monotonic()
        runtime_metrics.classification_count += 1
        try:
            async with timeout_scope(
                config_parser.get_config("classification_timeout_seconds", 20)
            ):
                return await self._get_category()
        except TimeoutError:
            logger.warning("分类模型超过时间预算，降级为无工具中等难度")
            return "1", "[图片]" in self.plain, []
        finally:
            runtime_metrics.classification_seconds += time.monotonic() - started

    async def _get_category(self) -> tuple[str, bool, list] | str | bool:
        if model_selector.get_use_tools() or model_selector.get_web_search():
            catalog = (
                self.tool_snapshot.get_brief_catalog()
                if self.tool_snapshot is not None
                else tool_manager.get_brief_catalog()
            )
            logger.debug(f"分类工具索引条目数: {catalog.count(chr(10)) + 1}")
        else:
            catalog = "当前工具调用与联网功能均已关闭，无需返回任何插件。"

        prompt = f"""你的任务是评估用户输入、拆解步骤、选择必要工具并判断是否需要视觉。
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
        # 判断是否开启 MoE，若未开启（但因为开启了工具走到这里），则使用默认模型（selected_model）
        if model_selector.get_moe():
            category_model_config = model_selector.get_model("category_model")
        else:
            category_model_config = model_selector.get_model("selected_model")
            logger.debug("未开启MoE，使用默认模型进行工具/联网/视觉判断分类")
        headers = {
            "Authorization": category_model_config["key"],
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        for try_times in range(2):
            try:
                raw_result = ""
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
                    # 第一次尝试：检查配置中是否启用了 json_mode
                    if category_model_config.get("json_mode"):
                        current_data["response_format"] = {"type": "json_object"}
                else:
                    # 第二次尝试：兜底降级。强制移除结构化参数，依靠强化 Prompt
                    current_data.pop("response_format", None)
                    current_plain += (
                        "\n(不要回答以上内容。只进行一次分类与工具判断，"
                        "严格返回 JSON，不要附加其他内容。)"
                    )
                    current_data["messages"][1]["content"] = current_plain

                payload = json.dumps(current_data)

                async with get_session().post(
                    url=category_model_config["url"],
                    data=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(
                        total=config_parser.get_config(
                            "classification_timeout_seconds", 20
                        )
                    ),
                    proxy=category_model_config.get("proxy"),
                ) as resp:
                        # 如果请求因为 response_format 返回 400，主动触发异常进入兜底重试
                        if resp.status == 400 and try_times == 0:
                            err_text = await resp.text()
                            logger.info(f"模型不支持结构化输出或参数异常，将降级重试。详情: {err_text}")
                            raise ValueError("Model does not support json_object format")

                        response = await resp.json()

                if choices := response.get("choices"):
                    raw_result = choices[0]["message"]["content"]
                    # 清理思考内容（推理模型可能包含 <think>/<thought> 标签）
                    raw_result = re.sub(r"<(think|thought|thinking)>.*?</\1>", "", raw_result, flags=re.DOTALL).strip()

                    # 容错清理 Markdown 标记
                    clean_result = raw_result.strip()
                    if clean_result.startswith("```json"):
                        clean_result = clean_result[7:]
                    elif clean_result.startswith("```"):
                        clean_result = clean_result[3:]
                    if clean_result.endswith("```"):
                        clean_result = clean_result[:-3]

                    result_dict = json.loads(clean_result.strip())
                    return (
                        str(result_dict["difficulty"]),
                        result_dict["vision_required"],
                        result_dict.get("required_plugins", []),
                    )
                elif (
                    response.get("code") == "DataInspectionFailed"
                    or "contentFilter" in response
                ):
                    logger.warning("分类请求触发内容安全拦截，响应正文已省略")
                    return "内容不合规，拒绝回答"

            except Exception:
                logger.warning(traceback.format_exc())
                logger.warning(
                    f"分类结果解析异常，当前尝试次数 {try_times + 1}，返回正文已省略。"
                )
                continue

        return False

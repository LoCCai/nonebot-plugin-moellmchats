from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import aiohttp
from nonebot.log import logger

from .admission import get_llm_controller
from .compat import timeout as timeout_scope
from .config import config_parser
from .generated_tool_runner import generated_tool_runner
from .generated_tools import BundleValidation, generated_tool_store
from .model_selector import model_selector
from .runtime_metrics import runtime_metrics
from .utils import get_session

_AUTHOR_SYSTEM = """你是 Python 工具包工程师。根据超级管理员需求生成一个可持久化工具包。
只返回 JSON 对象，字段必须为 manifest、tool_py、tests_py，不要 Markdown。
manifest 格式：
{"bundle_id":"英文标识","description":"说明","tools":[{"name":"工具名","description":"给模型的用途说明","parameters":{"type":"object","properties":{},"required":[]},"handler":"函数名","permission":"user或superuser","effect":"read_only或mutating","timeout_seconds":30,"result_limit":6000}]}
tool.py 只允许 import、常量、函数和类定义，不得在模块顶层执行操作。
函数可以使用完整 Python 和当前已安装依赖，可以接收隐藏参数
_tool_context（脱敏字典）和 _workspace（可写目录）。不得请求 _bot、_event 或生产凭据。
tests.py 必须定义 async def run_tests(tool_module)，执行确定性测试并在成功时返回简短字符串。测试不得依赖真实外部服务。
不得写入任何真实 token、密码、连接串或私钥。权限和 effect 必须按实际能力如实声明。"""

_REVIEW_SYSTEM = """你是独立代码复核员。审查给定 Python 工具包是否准确满足需求、
权限声明是否保守、是否可能泄露数据或造成资源/命令注入。
只返回 JSON：{"approved":true或false,"summary":"简述","risks":["风险"]}。
有隐藏行为、权限低报、明显注入或需求不匹配时必须 false。"""


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, stop = text.find("{"), text.rfind("}")
        if start < 0 or stop <= start:
            raise ValueError("模型未返回 JSON 对象") from None
        value = json.loads(text[start : stop + 1])
    if not isinstance(value, dict):
        raise ValueError("模型返回值必须是 JSON 对象")
    return value


class ToolAuthoringService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def _call_model(self, model_key: str, system: str, user: str) -> str:
        model = model_selector.get_model(model_key)
        if not model:
            raise RuntimeError(f"{model_key} 未配置或不可用")
        headers = {
            "Content-Type": "application/json",
            "Authorization": model.get("key", ""),
        }
        data: dict[str, Any] = {
            "model": model["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        for key in ("max_tokens", "temperature", "top_p", "top_k"):
            if model.get(key) is not None:
                data[key] = model[key]
        if isinstance(model.get("extra_payload"), dict):
            data.update(model["extra_payload"])
        timeout = aiohttp.ClientTimeout(
            total=min(
                180,
                config_parser.get_config("request_timeout_seconds", 180),
            )
        )
        async with get_session().post(
            model["url"],
            headers=headers,
            json=data,
            proxy=model.get("proxy"),
            timeout=timeout,
        ) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"模型请求失败 HTTP {response.status}")
        payload = json.loads(body)
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("模型未返回 choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("模型未返回可用内容")
        return content

    @staticmethod
    def _write_candidate(path: Path, generated: dict[str, Any]) -> None:
        manifest = generated.get("manifest")
        source = generated.get("tool_py")
        tests_source = generated.get("tests_py")
        if not isinstance(manifest, dict):
            raise ValueError("生成结果缺少 manifest")
        if not isinstance(source, str) or not isinstance(tests_source, str):
            raise ValueError("生成结果缺少 tool_py 或 tests_py")
        (path / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (path / "tool.py").write_text(source, encoding="utf-8")
        (path / "tests.py").write_text(tests_source, encoding="utf-8")
        for item in path.iterdir():
            item.chmod(0o644)
        path.chmod(0o755)

    async def _create_admitted(
        self, requirement: str
    ) -> tuple[str, BundleValidation, dict[str, Any], str]:
        await asyncio.to_thread(generated_tool_store.ensure_initialized)
        candidate = Path(
            tempfile.mkdtemp(prefix=".candidate-", dir=generated_tool_store.root)
        )
        try:
            generated = _extract_json(
                await self._call_model(
                    "selected_model", _AUTHOR_SYSTEM, requirement
                )
            )
            await asyncio.to_thread(self._write_candidate, candidate, generated)
            validation = await asyncio.to_thread(
                generated_tool_store.validate_bundle, candidate
            )
            test_summary = await generated_tool_runner.run_tests(candidate)

            def review_payload() -> str:
                return json.dumps(
                    {
                        "requirement": requirement,
                        "manifest": validation.manifest,
                        "tool_py": (candidate / "tool.py").read_text(encoding="utf-8"),
                        "tests_py": (candidate / "tests.py").read_text(encoding="utf-8"),
                        "static_risks": validation.risks,
                        "test_summary": test_summary,
                    },
                    ensure_ascii=False,
                )

            review = _extract_json(
                await self._call_model(
                    "summary_model",
                    _REVIEW_SYSTEM,
                    await asyncio.to_thread(review_payload),
                )
            )
            if not isinstance(review.get("approved"), bool):
                raise ValueError("复核模型未返回 approved 布尔值")
            if not isinstance(review.get("summary"), str):
                raise ValueError("复核模型未返回 summary 字符串")
            review_risks = review.get("risks", [])
            if not isinstance(review_risks, list) or not all(
                isinstance(item, str) for item in review_risks
            ):
                raise ValueError("复核模型 risks 必须是字符串数组")
            status = "reviewed" if review["approved"] else "review_failed"

            def persist_draft():
                return generated_tool_store.create_draft(
                    validation.manifest,
                    (candidate / "tool.py").read_text(encoding="utf-8"),
                    (candidate / "tests.py").read_text(encoding="utf-8"),
                    request=requirement,
                    review=review,
                    status=status,
                )

            draft_id, stored_validation = await asyncio.to_thread(persist_draft)
            logger.info(
                f"AI 工具草稿已生成 draft={draft_id} approved={review['approved']} "
                f"risks={len(stored_validation.risks)}"
            )
            return draft_id, stored_validation, review, test_summary
        finally:
            await asyncio.to_thread(shutil.rmtree, candidate, True)

    async def create(
        self,
        requirement: str,
        *,
        actor_key: str = "generated-tool-authoring",
    ) -> tuple[str, BundleValidation, dict[str, Any], str]:
        if not config_parser.get_config("generated_tools_enabled", True):
            raise RuntimeError("AI 生成功能当前已关闭")
        if self._lock.locked():
            raise RuntimeError("已有一个 AI 功能正在生成或研判")
        requirement = requirement.strip()
        if not requirement or len(requirement) > 4000:
            raise ValueError("功能需求必须为 1 到 4000 个字符")
        async with self._lock:
            runtime_metrics.generated_authoring_active = 1
            try:
                async with timeout_scope(
                    config_parser.get_config("request_timeout_seconds", 180)
                ):
                    async with get_llm_controller().slot(actor_key):
                        return await self._create_admitted(requirement)
            except TimeoutError:
                raise RuntimeError("AI 功能生成超过总时间预算") from None
            finally:
                runtime_metrics.generated_authoring_active = 0


tool_authoring_service = ToolAuthoringService()

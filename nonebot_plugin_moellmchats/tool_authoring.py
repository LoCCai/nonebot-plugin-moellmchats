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
from .private_files import harden_private_tree
from .runtime_metrics import runtime_metrics
from .utils import get_session

_AUTHOR_SYSTEM = """你是 Python 工具包工程师。根据超级管理员需求生成一个可持久化工具包。
只返回 JSON 对象，字段必须为 manifest、tool_py、tests_py，不要 Markdown。
manifest 格式：
{"bundle_id":"英文标识","description":"说明","capabilities":{"network":false,"process":false,"workspace":true},"tools":[{"name":"工具名","description":"给模型的用途说明","parameters":{"type":"object","properties":{},"required":[]},"handler":"函数名","permission":"user或superuser","effect":"read_only或mutating","timeout_seconds":30,"result_limit":6000}]}
tool.py 只允许 import、常量、函数和类定义，不得在模块顶层执行操作。
函数可以使用完整 Python 和当前已安装依赖，可以接收隐藏参数
_tool_context（脱敏字典）和 _workspace（可写目录）。不得请求 _bot、_event 或生产凭据。
tests.py 必须定义 async def run_tests(tool_module)，执行确定性测试并在成功时返回简短字符串。测试不得依赖真实外部服务。
不得写入任何真实 token、密码、连接串或私钥。权限、effect 和 capability 必须按实际需求如实申请；
它们只是申请值，系统会以更严格的人工策略作为最终权限。Generated Tool 默认禁止 network/process。"""

_REVIEW_SYSTEM = """你是独立代码复核员。审查给定 Python 工具包是否准确满足需求、
权限声明是否保守、是否可能泄露数据或造成资源/命令注入。
只返回 JSON：{"approved":true或false,"summary":"简述","risks":["风险"]}。
有隐藏行为、权限低报、明显注入或需求不匹配时必须 false。"""

_MODEL_ERROR_BODY_LIMIT = 16_384
_MODEL_ERROR_MESSAGE_LIMIT = 300
_MODEL_ERROR_FIELD_LIMIT = 80
_CONTENT_POLICY_MARKERS = (
    "datainspectionfailed",
    "content_filter",
    "sensitive",
    "safety",
    "violation",
    "audit",
    "prohibited",
)
_BEARER_SECRET_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(authorization|api[_ -]?key|access[_ -]?token|token|cookie|"
    r"secret|password|clientkey|rkey)(\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_OPENAI_SECRET_RE = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s]+")
_LOCAL_PATH_RE = re.compile(r"(?<![\w.])/(?:root|home|app|etc|var|tmp)(?:/[^\s,;:]+)+")
_OPAQUE_SECRET_RE = re.compile(r"\b[a-zA-Z0-9_+/=-]{40,}\b")


def _redact_model_error_field(value: object, *, limit: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    text = _URL_RE.sub("<url>", text)
    text = _BEARER_SECRET_RE.sub("Bearer <redacted>", text)
    text = _OPENAI_SECRET_RE.sub("<redacted>", text)
    text = _NAMED_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    text = _LOCAL_PATH_RE.sub("<path>", text)
    text = _OPAQUE_SECRET_RE.sub("<redacted>", text)
    return text[:limit]


def _extract_model_error_info(body: str) -> dict[str, str]:
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    candidates: list[dict[str, Any]] = []
    nested = payload.get("error")
    if isinstance(nested, dict):
        candidates.append(nested)
    candidates.append(payload)
    values: dict[str, str] = {}
    aliases = {
        "code": ("code", "error_code"),
        "type": ("type",),
        "param": ("param",),
        "message": ("message", "msg"),
    }
    for candidate in candidates:
        for field, keys in aliases.items():
            if values.get(field):
                continue
            raw = next(
                (candidate.get(key) for key in keys if candidate.get(key) is not None),
                None,
            )
            limit = _MODEL_ERROR_MESSAGE_LIMIT if field == "message" else _MODEL_ERROR_FIELD_LIMIT
            if rendered := _redact_model_error_field(raw, limit=limit):
                values[field] = rendered
    return values


def _is_content_policy_error(body: str) -> bool:
    normalized = body.casefold()
    return any(marker in normalized for marker in _CONTENT_POLICY_MARKERS)


def _format_model_http_error(
    status: int,
    body: str,
    *,
    truncated: bool,
    compatibility_retried: bool,
) -> str:
    parts = [f"模型请求失败 HTTP {status}"]
    info = _extract_model_error_info(body)
    if _is_content_policy_error(body):
        parts.append("模型服务拒绝了请求内容或安全策略")
    else:
        labels = {
            "code": "错误码",
            "type": "类型",
            "param": "参数",
            "message": "原因",
        }
        parts.extend(f"{labels[field]}={info[field]}" for field in ("code", "type", "param", "message") if info.get(field))
    if compatibility_retried:
        parts.append("已使用最小兼容参数重试")
    if truncated:
        parts.append("错误响应已截断")
    return "；".join(parts)


async def _read_model_error_body(response: Any) -> tuple[str, bool, int]:
    read_limit = _MODEL_ERROR_BODY_LIMIT + 1
    try:
        raw = await response.content.readexactly(read_limit)
    except asyncio.IncompleteReadError as error:
        raw = error.partial
    truncated = len(raw) > _MODEL_ERROR_BODY_LIMIT
    encoding = getattr(response, "charset", None) or "utf-8"
    try:
        body = raw[:_MODEL_ERROR_BODY_LIMIT].decode(
            encoding,
            errors="replace",
        )
    except LookupError:
        body = raw[:_MODEL_ERROR_BODY_LIMIT].decode(
            "utf-8",
            errors="replace",
        )
    return body, truncated, len(raw)


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
            "Accept-Encoding": "identity",
        }
        minimal_data: dict[str, Any] = {
            "model": model["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        }
        data = dict(minimal_data)
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
        attempt_data = data
        compatibility_retried = False
        while True:
            async with get_session().post(
                model["url"],
                headers=headers,
                json=attempt_data,
                proxy=model.get("proxy"),
                timeout=timeout,
            ) as response:
                if response.status == 200:
                    body = await response.text()
                    break
                error_body, truncated, read_bytes = await _read_model_error_body(response)
                logger.warning(
                    "AI 工具草稿模型请求失败；"
                    f"status={response.status} "
                    f"read_bytes={read_bytes} "
                    f"truncated={truncated}，响应正文已省略"
                )
                if (
                    response.status == 400
                    and not compatibility_retried
                    and data != minimal_data
                    and not _is_content_policy_error(error_body)
                ):
                    compatibility_retried = True
                    attempt_data = minimal_data
                    logger.warning("AI 工具草稿模型不接受可选参数，将使用 model/messages/stream 最小请求重试一次")
                    continue
                raise RuntimeError(
                    _format_model_http_error(
                        response.status,
                        error_body,
                        truncated=truncated,
                        compatibility_retried=compatibility_retried,
                    )
                )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError("模型响应不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise RuntimeError("模型响应 JSON 顶层不是对象")
        choices = payload.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("模型未返回 choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise RuntimeError("模型返回的 choices 格式错误")
        message = first_choice.get("message") or {}
        if not isinstance(message, dict):
            raise RuntimeError("模型返回的 message 格式错误")
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
        (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (path / "tool.py").write_text(source, encoding="utf-8")
        (path / "tests.py").write_text(tests_source, encoding="utf-8")
        harden_private_tree(path)

    async def _create_admitted(self, requirement: str) -> tuple[str, BundleValidation, dict[str, Any], str]:
        await asyncio.to_thread(generated_tool_store.ensure_initialized)
        candidate = Path(tempfile.mkdtemp(prefix=".candidate-", dir=generated_tool_store.root))
        try:
            generated = _extract_json(await self._call_model("selected_model", _AUTHOR_SYSTEM, requirement))
            await asyncio.to_thread(self._write_candidate, candidate, generated)
            validation = await asyncio.to_thread(generated_tool_store.validate_bundle, candidate)

            def persist_initial_draft():
                return generated_tool_store.create_draft(
                    validation.manifest,
                    (candidate / "tool.py").read_text(encoding="utf-8"),
                    (candidate / "tests.py").read_text(encoding="utf-8"),
                    request=requirement,
                    review={
                        "approved": None,
                        "summary": "等待独立模型复核",
                        "risks": [],
                    },
                )

            draft_id, stored_validation = await asyncio.to_thread(persist_initial_draft)
            try:
                await asyncio.to_thread(
                    generated_tool_store.mark_static_validated,
                    draft_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                try:
                    await asyncio.to_thread(
                        generated_tool_store.mark_validation_failed,
                        draft_id,
                        str(error)[:1000],
                    )
                except Exception:
                    logger.exception(f"草稿 {draft_id} 静态验证失败后无法记录 failure evidence")
                raise
            draft_path = generated_tool_store.drafts_dir / draft_id
            try:
                test_summary = await generated_tool_runner.run_tests(draft_path)
                await asyncio.to_thread(
                    generated_tool_store.mark_sandbox_tested,
                    draft_id,
                    test_summary,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await asyncio.to_thread(
                    generated_tool_store.mark_test_failed,
                    draft_id,
                    str(error)[:1000],
                )
                raise

            def review_payload() -> str:
                return json.dumps(
                    {
                        "requirement": requirement,
                        "manifest": stored_validation.manifest,
                        "tool_py": (draft_path / "tool.py").read_text(encoding="utf-8"),
                        "tests_py": (draft_path / "tests.py").read_text(encoding="utf-8"),
                        "static_risks": stored_validation.risks,
                        "test_summary": test_summary,
                    },
                    ensure_ascii=False,
                )

            try:
                review = _extract_json(
                    await self._call_model(
                        "summary_model",
                        _REVIEW_SYSTEM,
                        await asyncio.to_thread(review_payload),
                    )
                )
                if not isinstance(review.get("approved"), bool):
                    raise ValueError("复核模型未返回 approved 布尔值")
                if (
                    not isinstance(review.get("summary"), str)
                    or not review["summary"].strip()
                    or review["summary"] != review["summary"].strip()
                    or len(review["summary"]) > 4000
                ):
                    raise ValueError("复核模型 summary 必须为 1 到 4000 个字符")
                review_risks = review.get("risks", [])
                if not isinstance(review_risks, list) or not all(
                    isinstance(item, str) and bool(item.strip()) and item == item.strip() and len(item) <= 500
                    for item in review_risks
                ):
                    raise ValueError("复核模型 risks 必须是最多 500 字的非空字符串数组")
                if len(review_risks) > 64:
                    raise ValueError("复核模型 risks 最多 64 项")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await asyncio.to_thread(
                    generated_tool_store.mark_review_failed,
                    draft_id,
                    summary=str(error)[:1000],
                )
                raise

            if review["approved"]:
                await asyncio.to_thread(
                    generated_tool_store.mark_model_reviewed,
                    draft_id,
                    summary=review["summary"],
                    risks=tuple(review_risks),
                )
                await asyncio.to_thread(
                    generated_tool_store.mark_awaiting_approval,
                    draft_id,
                )
            else:
                await asyncio.to_thread(
                    generated_tool_store.mark_review_failed,
                    draft_id,
                    summary=review["summary"],
                    risks=tuple(review_risks),
                )
            logger.info(f"AI 工具草稿已生成 draft={draft_id} approved={review['approved']} risks={len(stored_validation.risks)}")
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
                async with timeout_scope(config_parser.get_config("request_timeout_seconds", 180)):
                    async with get_llm_controller().slot(actor_key):
                        return await self._create_admitted(requirement)
            except TimeoutError:
                raise RuntimeError("AI 功能生成超过总时间预算") from None
            finally:
                runtime_metrics.generated_authoring_active = 0


tool_authoring_service = ToolAuthoringService()

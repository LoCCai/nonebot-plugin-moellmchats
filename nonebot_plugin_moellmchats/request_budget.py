"""Request-local limits, selected by the host from the real caller identity."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from nonebot import get_plugin_config, logger
from pydantic import BaseModel, Field


class RequestBudgetConfig(BaseModel):
    moellm_ordinary_request_seconds: int = Field(default=180, ge=30, le=600)
    moellm_ordinary_model_seconds: int = Field(default=45, ge=5, le=180)
    moellm_ordinary_output_tokens: int = Field(default=4096, ge=2048, le=32768)
    moellm_ordinary_tool_rounds: int = Field(default=4, ge=1, le=12)
    moellm_ordinary_tools_per_round: int = Field(default=2, ge=1, le=8)
    moellm_ordinary_model_attempts: int = Field(default=2, ge=1, le=3)
    moellm_ordinary_history_tokens: int = Field(default=2000, ge=256, le=16000)
    moellm_ordinary_history_exchanges: int = Field(default=4, ge=1, le=20)
    moellm_ordinary_reasoning_effort: Literal["low", "medium"] = "low"
    moellm_queue_wait_seconds: float = Field(default=15, ge=0.1, le=60)
    moellm_authorized_request_seconds: int = Field(default=3600, ge=360, le=3600)
    moellm_authorized_model_seconds: int = Field(default=360, ge=30, le=360)
    moellm_authorized_rounds: int = Field(default=10, ge=1, le=10)


budget_config = get_plugin_config(RequestBudgetConfig)
_ordinary: ContextVar[bool] = ContextVar("moellm_ordinary_budget", default=False)
_authorized: ContextVar[bool] = ContextVar("moellm_authorized_budget", default=False)
_stage: ContextVar[str] = ContextVar("moellm_budget_stage", default="chat")
_authorizer: Callable[[Any, Any], Awaitable[bool]] | None = None


def register_budget_authorizer(authorizer: Callable[[Any, Any], Awaitable[bool]]) -> None:
    """A host callback owns grants; chat text and model arguments cannot grant them."""
    global _authorizer
    if not callable(authorizer):
        raise TypeError("预算授权检查必须可调用")
    _authorizer = authorizer


async def budget_authorized(bot: Any, event: Any) -> bool:
    if _authorizer is None:
        from .onebot_facade import event_user_id

        superusers = getattr(getattr(bot, "config", None), "superusers", ())
        return event_user_id(event) in {str(value) for value in superusers}
    try:
        return await asyncio.wait_for(_authorizer(bot, event), timeout=2) is True
    except Exception as error:
        logger.warning("LLM 预算授权检查失败，使用普通预算：error_type={}", type(error).__name__)
        return False


@contextmanager
def request_budget_scope(*, authorized: bool) -> Iterator[None]:
    token = _ordinary.set(not authorized)
    authorized_token = _authorized.set(authorized)
    try:
        yield
    finally:
        _ordinary.reset(token)
        _authorized.reset(authorized_token)


@contextmanager
def classification_budget_scope() -> Iterator[None]:
    token = _stage.set("classification")
    try:
        yield
    finally:
        _stage.reset(token)


def budget_limit(key: str, configured: int | float) -> int | float:
    if _authorized.get():
        limits = {
            "request_timeout_seconds": budget_config.moellm_authorized_request_seconds,
            "model_timeout_seconds": budget_config.moellm_authorized_model_seconds,
            "max_tool_rounds": max(0, budget_config.moellm_authorized_rounds - 1),
            "max_agent_steps": max(0, budget_config.moellm_authorized_rounds - 1),
        }
        return limits.get(key, configured)
    if not _ordinary.get():
        return configured
    limits = {
        "request_timeout_seconds": budget_config.moellm_ordinary_request_seconds,
        "model_timeout_seconds": budget_config.moellm_ordinary_model_seconds,
        "classification_timeout_seconds": 8,
        "max_tool_rounds": budget_config.moellm_ordinary_tool_rounds,
        "max_agent_steps": budget_config.moellm_ordinary_tool_rounds,
        "max_tool_calls_per_round": budget_config.moellm_ordinary_tools_per_round,
        "max_retry_times": budget_config.moellm_ordinary_model_attempts,
        "max_history_tokens": budget_config.moellm_ordinary_history_tokens,
        "max_history_chars": budget_config.moellm_ordinary_history_tokens * 3,
        "max_user_history": budget_config.moellm_ordinary_history_exchanges,
    }
    return min(configured, limits[key]) if key in limits else configured


def request_config(key: str, default: int) -> int:
    from .config import config_parser

    return budget_limit(key, config_parser.get_config(key, default))


def budget_tier() -> str:
    return "ordinary" if _ordinary.get() else "authorized"


def ordinary_request() -> bool:
    return _ordinary.get()


def authorized_request() -> bool:
    return _authorized.get()


def apply_payload_budget(model: dict, payload: dict) -> None:
    """Apply after provider extras, including backup, summary and media calls."""
    classification = _stage.get() == "classification"
    if not _ordinary.get() and not classification:
        return
    glm = model.get("provider") == "bigmodel" and model.get("model") == "glm-5.3-flash"
    output_limit = (2048 if glm else 512) if classification else budget_config.moellm_ordinary_output_tokens
    field = model.get("token_limit_field", "max_tokens")
    if field not in {"max_tokens", "max_completion_tokens"}:
        field = "max_tokens"
    present = [key for key in ("max_tokens", "max_completion_tokens") if key in payload]
    for key in present or [field]:
        value = payload.get(key)
        payload[key] = min(value, output_limit) if type(value) is int and value > 0 else output_limit
    effort = "low" if classification else budget_config.moellm_ordinary_reasoning_effort
    if glm or "reasoning_effort" in payload:
        payload["reasoning_effort"] = effort
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        payload["reasoning"] = {**reasoning, "effort": effort}
    # Only narrow fields already supported by the configured provider.
    for key in ("thinking_budget", "reasoning_budget"):
        value = payload.get(key)
        if type(value) is int and value > 0:
            payload[key] = min(value, 1024)

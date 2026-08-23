from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import re

from .database_schema import (
    MODEL_NAME_MAX_CHARS,
    MODEL_PROVIDER_MAX_CHARS,
    MODEL_USAGE_COST_PRECISION,
    MODEL_USAGE_COST_SCALE,
)

MAX_MODEL_USAGE_BATCH_SIZE = 100

_AGENT_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_MAX_COST_EXCLUSIVE = Decimal(10) ** (MODEL_USAGE_COST_PRECISION - MODEL_USAGE_COST_SCALE)


def validate_usage_run_id(value: object) -> str:
    """Validate the AgentRun identity shared by usage records and cursors."""

    if not isinstance(value, str) or not _AGENT_RUN_ID_RE.fullmatch(value):
        raise ValueError("run_id 必须是安全的 AgentRun 标识")
    return value


def _require_bounded_label(
    value: object,
    *,
    label: str,
    maximum: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _CONTROL_CHARACTER_RE.search(value)
    ):
        raise ValueError(f"{label} 必须是无首尾空白和控制字符的有界非空字符串")
    return value


def _require_nonnegative_bigint(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} 必须是非负 PostgreSQL BIGINT")
    return value


def _require_optional_positive_bigint(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} 必须是正 PostgreSQL BIGINT 或 None")
    return value


def _normalize_cost(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("ModelUsageRecord.cost 必须是非负有限 Decimal 或 None")

    if value == 0:
        return Decimal(0)
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("ModelUsageRecord.cost 必须是普通有限 Decimal")
    canonical_digits = digits
    canonical_exponent = exponent
    while canonical_digits[-1] == 0:
        canonical_digits = canonical_digits[:-1]
        canonical_exponent += 1
    normalized = Decimal((sign, canonical_digits, canonical_exponent))
    if normalized >= _MAX_COST_EXCLUSIVE:
        raise ValueError("ModelUsageRecord.cost 超出 NUMERIC(24, 12) 整数位上限")
    if canonical_exponent < -MODEL_USAGE_COST_SCALE:
        raise ValueError("ModelUsageRecord.cost 超出 NUMERIC(24, 12) 小数位上限")
    return normalized


def _normalize_datetime(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} 必须是带时区的 datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError
        return value.astimezone(timezone.utc)
    except Exception:
        raise ValueError(f"{label} 必须是有效的带时区 datetime") from None


@dataclass(frozen=True)
class ModelUsageRecord:
    """Detached immutable model-usage event aligned with ``model_usage``."""

    usage_id: int | None
    run_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    cost: Decimal | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_optional_positive_bigint(
            self.usage_id,
            label="ModelUsageRecord.usage_id",
        )
        validate_usage_run_id(self.run_id)
        _require_bounded_label(
            self.provider,
            label="ModelUsageRecord.provider",
            maximum=MODEL_PROVIDER_MAX_CHARS,
        )
        _require_bounded_label(
            self.model,
            label="ModelUsageRecord.model",
            maximum=MODEL_NAME_MAX_CHARS,
        )
        _require_nonnegative_bigint(
            self.input_tokens,
            label="ModelUsageRecord.input_tokens",
        )
        _require_nonnegative_bigint(
            self.output_tokens,
            label="ModelUsageRecord.output_tokens",
        )
        _require_nonnegative_bigint(
            self.reasoning_tokens,
            label="ModelUsageRecord.reasoning_tokens",
        )
        _require_nonnegative_bigint(
            self.cached_tokens,
            label="ModelUsageRecord.cached_tokens",
        )
        cost = _normalize_cost(self.cost)
        created_at = _normalize_datetime(
            self.created_at,
            label="ModelUsageRecord.created_at",
        )
        object.__setattr__(self, "cost", cost)
        object.__setattr__(self, "created_at", created_at)

    @property
    def persisted(self) -> bool:
        return self.usage_id is not None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, object]:
        return {
            "usage_id": self.usage_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "cost": self.cost,
            "created_at": self.created_at,
        }

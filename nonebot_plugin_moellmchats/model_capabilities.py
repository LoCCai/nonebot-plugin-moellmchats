from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from typing import Any

MODEL_CAPABILITY_SCHEMA_VERSION = 1
MODEL_DESCRIPTOR_SCHEMA_VERSION = 1
MODEL_LIMIT_MAX_TOKENS = 100_000_000
MODEL_COST_PRECISION = 24
MODEL_COST_SCALE = 12
MODEL_DESCRIPTOR_MAX_JSON_BYTES = 16_384
MODEL_DESCRIPTOR_ID_MAX_CHARS = 512
MODEL_PROVIDER_MAX_CHARS = 128
MODEL_NAME_MAX_CHARS = 255

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_MAX_COST_EXCLUSIVE = Decimal(10) ** (MODEL_COST_PRECISION - MODEL_COST_SCALE)
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")


class ModelAvailability(str, Enum):
    """A bounded availability state supplied by an explicit catalog owner."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def _require_boolean(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} 必须是 bool")
    return value


def _require_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError("ModelDescriptor.generation 必须是非负 PostgreSQL BIGINT")
    return value


def _require_bounded_identity(
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
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{label} 必须是有效 UTF-8 文本") from None
    return value


def _require_token_limit(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MODEL_LIMIT_MAX_TOKENS:
        raise ValueError(f"{label} 必须是 1 到 {MODEL_LIMIT_MAX_TOKENS} 的整数")
    return value


def _normalize_cost(value: object, *, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{label} 必须是非负有限 Decimal")
    if value == 0:
        return Decimal(0)

    sign, digits, exponent = value.as_tuple()
    if sign or not isinstance(exponent, int):
        raise ValueError(f"{label} 必须是普通非负有限 Decimal")
    canonical_digits = digits
    canonical_exponent = exponent
    while canonical_digits[-1] == 0:
        canonical_digits = canonical_digits[:-1]
        canonical_exponent += 1
    normalized = Decimal((0, canonical_digits, canonical_exponent))
    if normalized >= _MAX_COST_EXCLUSIVE:
        raise ValueError(f"{label} 超出 NUMERIC({MODEL_COST_PRECISION}, {MODEL_COST_SCALE}) 整数位上限")
    if canonical_exponent < -MODEL_COST_SCALE:
        raise ValueError(f"{label} 超出 NUMERIC({MODEL_COST_PRECISION}, {MODEL_COST_SCALE}) 小数位上限")
    return normalized


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonical_json(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("ascii")) > MODEL_DESCRIPTOR_MAX_JSON_BYTES:
        raise ValueError("model capability payload 超过安全字节上限")
    return payload


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """The fixed, credential-free set of model features understood by routing."""

    text: bool
    vision: bool
    tools: bool
    json_schema: bool
    reasoning: bool
    streaming: bool

    def __post_init__(self) -> None:
        for field_name in (
            "text",
            "vision",
            "tools",
            "json_schema",
            "reasoning",
            "streaming",
        ):
            _require_boolean(
                getattr(self, field_name),
                label=f"ModelCapability.{field_name}",
            )

    @property
    def enabled(self) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in (
                "text",
                "vision",
                "tools",
                "json_schema",
                "reasoning",
                "streaming",
            )
            if getattr(self, field_name)
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "capabilities": self.as_dict(),
                "schema_version": MODEL_CAPABILITY_SCHEMA_VERSION,
            }
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "text": self.text,
            "vision": self.vision,
            "tools": self.tools,
            "json_schema": self.json_schema,
            "reasoning": self.reasoning,
            "streaming": self.streaming,
        }


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """Hard token limits advertised for one exact model catalog entry."""

    context_window: int
    max_output: int

    def __post_init__(self) -> None:
        context_window = _require_token_limit(
            self.context_window,
            label="ModelLimits.context_window",
        )
        max_output = _require_token_limit(
            self.max_output,
            label="ModelLimits.max_output",
        )
        if max_output > context_window:
            raise ValueError("ModelLimits.max_output 不能超过 context_window")

    @property
    def digest(self) -> str:
        return _digest({"limits": self.as_dict(), "schema_version": MODEL_CAPABILITY_SCHEMA_VERSION})

    def as_dict(self) -> dict[str, int]:
        return {
            "context_window": self.context_window,
            "max_output": self.max_output,
        }


@dataclass(frozen=True, slots=True)
class ModelCost:
    """Exact public catalog prices per one million input and output tokens."""

    input_per_million: Decimal
    output_per_million: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_per_million",
            _normalize_cost(
                self.input_per_million,
                label="ModelCost.input_per_million",
            ),
        )
        object.__setattr__(
            self,
            "output_per_million",
            _normalize_cost(
                self.output_per_million,
                label="ModelCost.output_per_million",
            ),
        )

    @property
    def digest(self) -> str:
        return _digest({"cost": self.as_dict(), "schema_version": MODEL_CAPABILITY_SCHEMA_VERSION})

    def as_dict(self) -> dict[str, str]:
        return {
            "input_per_million": _decimal_text(self.input_per_million),
            "output_per_million": _decimal_text(self.output_per_million),
        }


@dataclass(frozen=True, slots=True, repr=False)
class ModelDescriptor:
    """One generation-bound model catalog record with no endpoint or credential."""

    descriptor_id: str
    provider: str
    model: str
    generation: int
    capabilities: ModelCapability
    limits: ModelLimits
    cost: ModelCost | None
    availability: ModelAvailability

    def __post_init__(self) -> None:
        _require_bounded_identity(
            self.descriptor_id,
            label="ModelDescriptor.descriptor_id",
            maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
        )
        _require_bounded_identity(
            self.provider,
            label="ModelDescriptor.provider",
            maximum=MODEL_PROVIDER_MAX_CHARS,
        )
        _require_bounded_identity(
            self.model,
            label="ModelDescriptor.model",
            maximum=MODEL_NAME_MAX_CHARS,
        )
        _require_generation(self.generation)
        if not isinstance(self.capabilities, ModelCapability):
            raise TypeError("ModelDescriptor.capabilities 必须是 ModelCapability")
        if not isinstance(self.limits, ModelLimits):
            raise TypeError("ModelDescriptor.limits 必须是 ModelLimits")
        if self.cost is not None and not isinstance(self.cost, ModelCost):
            raise TypeError("ModelDescriptor.cost 必须是 ModelCost 或 None")
        if not isinstance(self.availability, ModelAvailability):
            raise TypeError("ModelDescriptor.availability 必须是 ModelAvailability")
        _canonical_json(self._payload())

    def _identity_payload(self) -> dict[str, str]:
        return {
            "descriptor_id": self.descriptor_id,
            "model": self.model,
            "provider": self.provider,
        }

    def _capability_payload(self) -> dict[str, Any]:
        return {
            "capabilities": self.capabilities.as_dict(),
            "limits": self.limits.as_dict(),
            "schema_version": MODEL_CAPABILITY_SCHEMA_VERSION,
        }

    def _payload(self) -> dict[str, Any]:
        return {
            "availability": self.availability.value,
            "capabilities": self.capabilities.as_dict(),
            "cost": None if self.cost is None else self.cost.as_dict(),
            "descriptor_id": self.descriptor_id,
            "generation": self.generation,
            "limits": self.limits.as_dict(),
            "model": self.model,
            "provider": self.provider,
            "schema_version": MODEL_DESCRIPTOR_SCHEMA_VERSION,
        }

    @property
    def identity_digest(self) -> str:
        return _digest(self._identity_payload())

    @property
    def capability_digest(self) -> str:
        return _digest(self._capability_payload())

    @property
    def descriptor_digest(self) -> str:
        return _digest(self._payload())

    def as_dict(self) -> dict[str, Any]:
        return self._payload()

    def to_json(self) -> str:
        return _canonical_json(self._payload())

    def __repr__(self) -> str:
        return (
            "ModelDescriptor("
            f"identity_digest={self.identity_digest!r}, "
            f"generation={self.generation!r}, "
            f"availability={self.availability.value!r}, "
            f"descriptor_digest={self.descriptor_digest!r})"
        )


__all__ = [
    "MODEL_CAPABILITY_SCHEMA_VERSION",
    "MODEL_COST_PRECISION",
    "MODEL_COST_SCALE",
    "MODEL_DESCRIPTOR_ID_MAX_CHARS",
    "MODEL_DESCRIPTOR_MAX_JSON_BYTES",
    "MODEL_DESCRIPTOR_SCHEMA_VERSION",
    "MODEL_LIMIT_MAX_TOKENS",
    "MODEL_NAME_MAX_CHARS",
    "MODEL_PROVIDER_MAX_CHARS",
    "ModelAvailability",
    "ModelCapability",
    "ModelCost",
    "ModelDescriptor",
    "ModelLimits",
]

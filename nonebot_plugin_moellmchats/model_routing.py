from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from typing import Any

from .model_capabilities import (
    MODEL_COST_PRECISION,
    MODEL_COST_SCALE,
    MODEL_DESCRIPTOR_ID_MAX_CHARS,
    MODEL_LIMIT_MAX_TOKENS,
    ModelAvailability,
    ModelCapability,
    ModelCost,
    ModelDescriptor,
)

MODEL_ROUTING_SCHEMA_VERSION = 1
MODEL_ROUTING_MAX_CANDIDATES = 1_024
MODEL_ROUTING_MAX_JSON_BYTES = 2_097_152
MODEL_ROUTING_MAX_LATENCY_MS = 86_400_000
MODEL_ROUTING_MAX_POLICY_VERSION_CHARS = 128
MODEL_ROUTING_MAX_QUALITY_SCORE = 1_000_000
MODEL_ROUTING_COST_DENOMINATOR = 10 ** (MODEL_COST_SCALE + 6)
MODEL_ROUTING_MAX_COST_NUMERATOR = ((10**MODEL_COST_PRECISION) - 1) * MODEL_LIMIT_MAX_TOKENS
MODEL_ROUTING_DYNAMIC_ORDER = (
    "availability_asc",
    "quality_desc",
    "latency_asc",
    "estimated_cost_asc",
    "identity_digest_asc",
)

_POSTGRES_BIGINT_MAX = (1 << 63) - 1
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY_FIELDS = (
    "text",
    "vision",
    "tools",
    "json_schema",
    "reasoning",
    "streaming",
)


class ModelRoutingError(ValueError):
    """A model route cannot be selected without weakening its contract."""


class ModelRoutingDriftError(ModelRoutingError):
    """The request is not bound to the supplied catalog or policy snapshot."""


class ModelRoutingUnavailableError(ModelRoutingError):
    """No candidate satisfies the complete request and policy contract."""


class ModelRouteRole(str, Enum):
    SELECTED = "selected_model"
    VISION = "vision_model"
    CATEGORY = "category_model"
    SUMMARY = "summary_model"
    MOE_0 = "moe_models.0"
    MOE_1 = "moe_models.1"
    MOE_2 = "moe_models.2"


class ModelRoutingMode(str, Enum):
    CAPABILITY_ONLY = "capability_only"
    FIXED_PREFERRED = "fixed_preferred"
    FIXED_ONLY = "fixed_only"


class ModelSelectionReason(str, Enum):
    CAPABILITY = "capability"
    FIXED = "fixed"


def _require_bounded_text(
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


def _require_generation(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} 必须是非负 PostgreSQL BIGINT")
    return value


def _require_bounded_integer(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须是 {minimum} 到 {maximum} 的整数")
    return value


def _require_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是小写 SHA-256")
    return value


def _canonical_json(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("ascii")) > MODEL_ROUTING_MAX_JSON_BYTES:
        raise ValueError("model routing payload 超过安全字节上限")
    return payload


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _cost_atoms(value: Decimal) -> int:
    sign, digits, exponent = value.as_tuple()
    if sign or not isinstance(exponent, int) or exponent < -MODEL_COST_SCALE:
        raise ValueError("model routing cost 不是规范化非负 Decimal")
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return coefficient * (10 ** (exponent + MODEL_COST_SCALE))


@dataclass(frozen=True, slots=True, repr=False)
class FixedModelBindings:
    """The seven bounded model pins used by the existing selector contract."""

    selected_model: str
    vision_model: str | None
    category_model: str
    summary_model: str
    moe_0: str
    moe_1: str
    moe_2: str

    def __post_init__(self) -> None:
        for field_name in (
            "selected_model",
            "category_model",
            "summary_model",
            "moe_0",
            "moe_1",
            "moe_2",
        ):
            _require_bounded_text(
                getattr(self, field_name),
                label=f"FixedModelBindings.{field_name}",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            )
        if self.vision_model is not None:
            _require_bounded_text(
                self.vision_model,
                label="FixedModelBindings.vision_model",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            )
        _canonical_json(self.as_dict())

    @classmethod
    def from_model_config(cls, model_config: Mapping[str, object]) -> FixedModelBindings:
        """Detach only existing fixed model ids; ignore unrelated config and secrets."""

        if not isinstance(model_config, Mapping):
            raise TypeError("model_config 必须是映射")
        moe_models = model_config.get("moe_models")
        if not isinstance(moe_models, Mapping):
            raise ValueError("model_config.moe_models 必须是映射")
        vision_value = model_config.get("vision_model")
        if vision_value == "":
            vision_value = None
        if vision_value is not None and not isinstance(vision_value, str):
            raise ValueError("model_config.vision_model 必须是字符串或空值")
        return cls(
            selected_model=_require_bounded_text(
                model_config.get("selected_model"),
                label="model_config.selected_model",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            ),
            vision_model=vision_value,
            category_model=_require_bounded_text(
                model_config.get("category_model"),
                label="model_config.category_model",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            ),
            summary_model=_require_bounded_text(
                model_config.get("summary_model"),
                label="model_config.summary_model",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            ),
            moe_0=_require_bounded_text(
                moe_models.get("0"),
                label="model_config.moe_models.0",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            ),
            moe_1=_require_bounded_text(
                moe_models.get("1"),
                label="model_config.moe_models.1",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            ),
            moe_2=_require_bounded_text(
                moe_models.get("2"),
                label="model_config.moe_models.2",
                maximum=MODEL_DESCRIPTOR_ID_MAX_CHARS,
            ),
        )

    def for_role(self, role: ModelRouteRole) -> str | None:
        if not isinstance(role, ModelRouteRole):
            raise TypeError("role 必须是 ModelRouteRole")
        return {
            ModelRouteRole.SELECTED: self.selected_model,
            ModelRouteRole.VISION: self.vision_model,
            ModelRouteRole.CATEGORY: self.category_model,
            ModelRouteRole.SUMMARY: self.summary_model,
            ModelRouteRole.MOE_0: self.moe_0,
            ModelRouteRole.MOE_1: self.moe_1,
            ModelRouteRole.MOE_2: self.moe_2,
        }[role]

    @property
    def digest(self) -> str:
        return _digest({"bindings": self.as_dict(), "schema_version": MODEL_ROUTING_SCHEMA_VERSION})

    def as_dict(self) -> dict[str, str | None]:
        return {
            "category_model": self.category_model,
            "moe_0": self.moe_0,
            "moe_1": self.moe_1,
            "moe_2": self.moe_2,
            "selected_model": self.selected_model,
            "summary_model": self.summary_model,
            "vision_model": self.vision_model,
        }

    def __repr__(self) -> str:
        return f"FixedModelBindings(digest={self.digest!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ModelRoutingPolicy:
    version: str
    mode: ModelRoutingMode
    allow_degraded: bool = False

    def __post_init__(self) -> None:
        _require_bounded_text(
            self.version,
            label="ModelRoutingPolicy.version",
            maximum=MODEL_ROUTING_MAX_POLICY_VERSION_CHARS,
        )
        if not isinstance(self.mode, ModelRoutingMode):
            raise TypeError("ModelRoutingPolicy.mode 必须是 ModelRoutingMode")
        if type(self.allow_degraded) is not bool:
            raise TypeError("ModelRoutingPolicy.allow_degraded 必须是 bool")
        _canonical_json(self.as_dict())

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "allow_degraded": self.allow_degraded,
            "dynamic_order": list(MODEL_ROUTING_DYNAMIC_ORDER),
            "mode": self.mode.value,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
            "version": self.version,
        }

    def __repr__(self) -> str:
        return f"ModelRoutingPolicy(version={self.version!r}, digest={self.digest!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ModelRouteCandidate:
    descriptor: ModelDescriptor
    quality_score: int
    latency_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ModelDescriptor):
            raise TypeError("ModelRouteCandidate.descriptor 必须是 ModelDescriptor")
        _require_bounded_integer(
            self.quality_score,
            label="ModelRouteCandidate.quality_score",
            minimum=0,
            maximum=MODEL_ROUTING_MAX_QUALITY_SCORE,
        )
        _require_bounded_integer(
            self.latency_ms,
            label="ModelRouteCandidate.latency_ms",
            minimum=0,
            maximum=MODEL_ROUTING_MAX_LATENCY_MS,
        )
        _canonical_json(self.as_dict())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "descriptor_digest": self.descriptor.descriptor_digest,
            "latency_ms": self.latency_ms,
            "quality_score": self.quality_score,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    @property
    def candidate_digest(self) -> str:
        return _digest(self._identity_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor.as_dict(),
            "latency_ms": self.latency_ms,
            "quality_score": self.quality_score,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    def __repr__(self) -> str:
        return f"ModelRouteCandidate(candidate_digest={self.candidate_digest!r})"


@dataclass(frozen=True, slots=True, repr=False)
class ModelRoutingCatalog:
    generation: int
    candidates: tuple[ModelRouteCandidate, ...]

    def __post_init__(self) -> None:
        _require_generation(self.generation, label="ModelRoutingCatalog.generation")
        if (
            not isinstance(self.candidates, tuple)
            or not self.candidates
            or len(self.candidates) > MODEL_ROUTING_MAX_CANDIDATES
            or not all(isinstance(candidate, ModelRouteCandidate) for candidate in self.candidates)
        ):
            raise ValueError(
                f"ModelRoutingCatalog.candidates 必须是非空且不超过 {MODEL_ROUTING_MAX_CANDIDATES} 项的 ModelRouteCandidate 元组"
            )
        if any(candidate.descriptor.generation != self.generation for candidate in self.candidates):
            raise ValueError("ModelRoutingCatalog 中所有 descriptor 必须绑定同一 generation")
        descriptor_ids = [candidate.descriptor.descriptor_id for candidate in self.candidates]
        if len(set(descriptor_ids)) != len(descriptor_ids):
            raise ValueError("ModelRoutingCatalog descriptor_id 不得重复")
        candidate_digests = [candidate.candidate_digest for candidate in self.candidates]
        if len(set(candidate_digests)) != len(candidate_digests):
            raise ValueError("ModelRoutingCatalog candidate identity 不得重复")
        object.__setattr__(
            self,
            "candidates",
            tuple(
                sorted(
                    self.candidates,
                    key=lambda candidate: (
                        candidate.descriptor.identity_digest,
                        candidate.candidate_digest,
                    ),
                )
            ),
        )
        _canonical_json(self.as_dict())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "candidate_digests": [candidate.candidate_digest for candidate in self.candidates],
            "generation": self.generation,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    @property
    def catalog_digest(self) -> str:
        return _digest(self._identity_payload())

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "generation": self.generation,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())

    def __repr__(self) -> str:
        return (
            "ModelRoutingCatalog("
            f"generation={self.generation!r}, candidate_count={len(self.candidates)!r}, "
            f"catalog_digest={self.catalog_digest!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelRouteRequirements:
    required_capabilities: ModelCapability
    minimum_context_window: int
    input_tokens: int
    output_tokens: int
    minimum_quality: int
    maximum_latency_ms: int
    maximum_unit_cost: ModelCost | None

    def __post_init__(self) -> None:
        if not isinstance(self.required_capabilities, ModelCapability):
            raise TypeError("ModelRouteRequirements.required_capabilities 必须是 ModelCapability")
        if not self.required_capabilities.enabled:
            raise ValueError("ModelRouteRequirements 至少需要一种 capability")
        _require_bounded_integer(
            self.minimum_context_window,
            label="ModelRouteRequirements.minimum_context_window",
            minimum=1,
            maximum=MODEL_LIMIT_MAX_TOKENS,
        )
        _require_bounded_integer(
            self.input_tokens,
            label="ModelRouteRequirements.input_tokens",
            minimum=0,
            maximum=MODEL_LIMIT_MAX_TOKENS,
        )
        _require_bounded_integer(
            self.output_tokens,
            label="ModelRouteRequirements.output_tokens",
            minimum=1,
            maximum=MODEL_LIMIT_MAX_TOKENS,
        )
        if self.input_tokens + self.output_tokens > self.minimum_context_window:
            raise ValueError("ModelRouteRequirements token 预算不能超过 minimum_context_window")
        _require_bounded_integer(
            self.minimum_quality,
            label="ModelRouteRequirements.minimum_quality",
            minimum=0,
            maximum=MODEL_ROUTING_MAX_QUALITY_SCORE,
        )
        _require_bounded_integer(
            self.maximum_latency_ms,
            label="ModelRouteRequirements.maximum_latency_ms",
            minimum=0,
            maximum=MODEL_ROUTING_MAX_LATENCY_MS,
        )
        if self.maximum_unit_cost is not None and not isinstance(self.maximum_unit_cost, ModelCost):
            raise TypeError("ModelRouteRequirements.maximum_unit_cost 必须是 ModelCost 或 None")
        _canonical_json(self.as_dict())

    @property
    def capability_digest(self) -> str:
        return self.required_capabilities.digest

    @property
    def requirements_digest(self) -> str:
        return _digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_digest": self.capability_digest,
            "input_tokens": self.input_tokens,
            "maximum_latency_ms": self.maximum_latency_ms,
            "maximum_unit_cost": None if self.maximum_unit_cost is None else self.maximum_unit_cost.as_dict(),
            "minimum_context_window": self.minimum_context_window,
            "minimum_quality": self.minimum_quality,
            "output_tokens": self.output_tokens,
            "required_capabilities": self.required_capabilities.as_dict(),
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    def __repr__(self) -> str:
        return (
            "ModelRouteRequirements("
            f"capability_digest={self.capability_digest!r}, "
            f"requirements_digest={self.requirements_digest!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelRoutingRequest:
    catalog_generation: int
    catalog_digest: str
    policy_version: str
    policy_digest: str
    role: ModelRouteRole
    requirements: ModelRouteRequirements
    fixed_bindings: FixedModelBindings | None = None

    def __post_init__(self) -> None:
        _require_generation(self.catalog_generation, label="ModelRoutingRequest.catalog_generation")
        _require_digest(self.catalog_digest, label="ModelRoutingRequest.catalog_digest")
        _require_bounded_text(
            self.policy_version,
            label="ModelRoutingRequest.policy_version",
            maximum=MODEL_ROUTING_MAX_POLICY_VERSION_CHARS,
        )
        _require_digest(self.policy_digest, label="ModelRoutingRequest.policy_digest")
        if not isinstance(self.role, ModelRouteRole):
            raise TypeError("ModelRoutingRequest.role 必须是 ModelRouteRole")
        if not isinstance(self.requirements, ModelRouteRequirements):
            raise TypeError("ModelRoutingRequest.requirements 必须是 ModelRouteRequirements")
        if self.fixed_bindings is not None and not isinstance(self.fixed_bindings, FixedModelBindings):
            raise TypeError("ModelRoutingRequest.fixed_bindings 必须是 FixedModelBindings 或 None")
        _canonical_json(self.as_dict())

    @classmethod
    def bind(
        cls,
        *,
        catalog: ModelRoutingCatalog,
        policy: ModelRoutingPolicy,
        role: ModelRouteRole,
        requirements: ModelRouteRequirements,
        fixed_bindings: FixedModelBindings | None = None,
    ) -> ModelRoutingRequest:
        if not isinstance(catalog, ModelRoutingCatalog):
            raise TypeError("catalog 必须是 ModelRoutingCatalog")
        if not isinstance(policy, ModelRoutingPolicy):
            raise TypeError("policy 必须是 ModelRoutingPolicy")
        return cls(
            catalog_generation=catalog.generation,
            catalog_digest=catalog.catalog_digest,
            policy_version=policy.version,
            policy_digest=policy.digest,
            role=role,
            requirements=requirements,
            fixed_bindings=fixed_bindings,
        )

    @property
    def capability_digest(self) -> str:
        return self.requirements.capability_digest

    @property
    def request_digest(self) -> str:
        return _digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_digest": self.catalog_digest,
            "catalog_generation": self.catalog_generation,
            "fixed_bindings_digest": None if self.fixed_bindings is None else self.fixed_bindings.digest,
            "policy_digest": self.policy_digest,
            "policy_version": self.policy_version,
            "requirements": self.requirements.as_dict(),
            "role": self.role.value,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    def __repr__(self) -> str:
        return (
            "ModelRoutingRequest("
            f"catalog_generation={self.catalog_generation!r}, "
            f"capability_digest={self.capability_digest!r}, request_digest={self.request_digest!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelRoutingDecision:
    selected: ModelRouteCandidate
    reason: ModelSelectionReason
    request_digest: str
    catalog_digest: str
    policy_digest: str
    capability_digest: str
    estimated_cost_numerator: int

    def __post_init__(self) -> None:
        if not isinstance(self.selected, ModelRouteCandidate):
            raise TypeError("ModelRoutingDecision.selected 必须是 ModelRouteCandidate")
        if not isinstance(self.reason, ModelSelectionReason):
            raise TypeError("ModelRoutingDecision.reason 必须是 ModelSelectionReason")
        for field_name in (
            "request_digest",
            "catalog_digest",
            "policy_digest",
            "capability_digest",
        ):
            _require_digest(getattr(self, field_name), label=f"ModelRoutingDecision.{field_name}")
        if (
            not isinstance(self.estimated_cost_numerator, int)
            or isinstance(self.estimated_cost_numerator, bool)
            or not 0 <= self.estimated_cost_numerator <= MODEL_ROUTING_MAX_COST_NUMERATOR
        ):
            raise ValueError("ModelRoutingDecision.estimated_cost_numerator 必须是有界非负整数")
        _canonical_json(self.as_dict())

    @property
    def decision_digest(self) -> str:
        return _digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_digest": self.selected.candidate_digest,
            "capability_digest": self.capability_digest,
            "catalog_digest": self.catalog_digest,
            "cost_denominator": str(MODEL_ROUTING_COST_DENOMINATOR),
            "descriptor_digest": self.selected.descriptor.descriptor_digest,
            "estimated_cost_numerator": str(self.estimated_cost_numerator),
            "policy_digest": self.policy_digest,
            "reason": self.reason.value,
            "request_digest": self.request_digest,
            "schema_version": MODEL_ROUTING_SCHEMA_VERSION,
        }

    def __repr__(self) -> str:
        return (
            "ModelRoutingDecision("
            f"reason={self.reason.value!r}, candidate_digest={self.selected.candidate_digest!r}, "
            f"decision_digest={self.decision_digest!r})"
        )


def _estimated_cost_numerator(candidate: ModelRouteCandidate, requirements: ModelRouteRequirements) -> int | None:
    cost = candidate.descriptor.cost
    if cost is None:
        return None
    return (
        _cost_atoms(cost.input_per_million) * requirements.input_tokens
        + _cost_atoms(cost.output_per_million) * requirements.output_tokens
    )


def _candidate_is_eligible(
    candidate: ModelRouteCandidate,
    *,
    policy: ModelRoutingPolicy,
    requirements: ModelRouteRequirements,
) -> bool:
    capabilities = candidate.descriptor.capabilities
    if any(
        getattr(requirements.required_capabilities, field_name) and not getattr(capabilities, field_name)
        for field_name in _CAPABILITY_FIELDS
    ):
        return False
    if candidate.descriptor.limits.context_window < requirements.minimum_context_window:
        return False
    if candidate.descriptor.limits.max_output < requirements.output_tokens:
        return False
    availability = candidate.descriptor.availability
    if availability in (ModelAvailability.UNKNOWN, ModelAvailability.UNAVAILABLE):
        return False
    if availability is ModelAvailability.DEGRADED and not policy.allow_degraded:
        return False
    if candidate.quality_score < requirements.minimum_quality:
        return False
    if candidate.latency_ms > requirements.maximum_latency_ms:
        return False
    cost = candidate.descriptor.cost
    if cost is None:
        return False
    maximum_cost = requirements.maximum_unit_cost
    return maximum_cost is None or (
        cost.input_per_million <= maximum_cost.input_per_million and cost.output_per_million <= maximum_cost.output_per_million
    )


def _dynamic_rank(
    candidate: ModelRouteCandidate,
    requirements: ModelRouteRequirements,
) -> tuple[int, int, int, int, str, str]:
    availability_rank = 0 if candidate.descriptor.availability is ModelAvailability.AVAILABLE else 1
    cost_numerator = _estimated_cost_numerator(candidate, requirements)
    if cost_numerator is None:
        raise ModelRoutingUnavailableError("候选模型缺少可比较的成本")
    return (
        availability_rank,
        -candidate.quality_score,
        candidate.latency_ms,
        cost_numerator,
        candidate.descriptor.identity_digest,
        candidate.candidate_digest,
    )


def select_model_route(
    *,
    catalog: ModelRoutingCatalog,
    policy: ModelRoutingPolicy,
    request: ModelRoutingRequest,
) -> ModelRoutingDecision:
    """Select one descriptor without reading configuration, credentials, or I/O."""

    if not isinstance(catalog, ModelRoutingCatalog):
        raise TypeError("catalog 必须是 ModelRoutingCatalog")
    if not isinstance(policy, ModelRoutingPolicy):
        raise TypeError("policy 必须是 ModelRoutingPolicy")
    if not isinstance(request, ModelRoutingRequest):
        raise TypeError("request 必须是 ModelRoutingRequest")
    if request.catalog_generation != catalog.generation or request.catalog_digest != catalog.catalog_digest:
        raise ModelRoutingDriftError("model routing catalog generation 或 digest 已漂移")
    if request.policy_version != policy.version or request.policy_digest != policy.digest:
        raise ModelRoutingDriftError("model routing policy version 或 digest 已漂移")

    if policy.mode is ModelRoutingMode.CAPABILITY_ONLY:
        if request.fixed_bindings is not None:
            raise ModelRoutingError("capability-only request 不得携带 fixed bindings")
    elif request.fixed_bindings is None:
        raise ModelRoutingError("fixed routing mode 必须绑定 fixed model snapshot")

    eligible = tuple(
        candidate
        for candidate in catalog.candidates
        if _candidate_is_eligible(candidate, policy=policy, requirements=request.requirements)
    )
    selected: ModelRouteCandidate | None = None
    reason = ModelSelectionReason.CAPABILITY
    fixed_target = None if request.fixed_bindings is None else request.fixed_bindings.for_role(request.role)

    if policy.mode in (ModelRoutingMode.FIXED_ONLY, ModelRoutingMode.FIXED_PREFERRED) and fixed_target is not None:
        selected = next(
            (candidate for candidate in eligible if candidate.descriptor.descriptor_id == fixed_target),
            None,
        )
        if selected is not None:
            reason = ModelSelectionReason.FIXED

    if policy.mode is ModelRoutingMode.FIXED_ONLY and selected is None:
        raise ModelRoutingUnavailableError("fixed model 不存在或不满足完整路由要求")
    if selected is None:
        if not eligible:
            raise ModelRoutingUnavailableError("没有模型满足完整路由要求")
        selected = min(eligible, key=lambda candidate: _dynamic_rank(candidate, request.requirements))

    estimated_cost_numerator = _estimated_cost_numerator(selected, request.requirements)
    if estimated_cost_numerator is None:
        raise ModelRoutingUnavailableError("选中模型缺少可比较的成本")
    return ModelRoutingDecision(
        selected=selected,
        reason=reason,
        request_digest=request.request_digest,
        catalog_digest=catalog.catalog_digest,
        policy_digest=policy.digest,
        capability_digest=request.capability_digest,
        estimated_cost_numerator=estimated_cost_numerator,
    )


__all__ = [
    "MODEL_ROUTING_COST_DENOMINATOR",
    "MODEL_ROUTING_DYNAMIC_ORDER",
    "MODEL_ROUTING_MAX_CANDIDATES",
    "MODEL_ROUTING_MAX_COST_NUMERATOR",
    "MODEL_ROUTING_MAX_JSON_BYTES",
    "MODEL_ROUTING_MAX_LATENCY_MS",
    "MODEL_ROUTING_MAX_POLICY_VERSION_CHARS",
    "MODEL_ROUTING_MAX_QUALITY_SCORE",
    "MODEL_ROUTING_SCHEMA_VERSION",
    "FixedModelBindings",
    "ModelRouteCandidate",
    "ModelRouteRequirements",
    "ModelRouteRole",
    "ModelRoutingCatalog",
    "ModelRoutingDecision",
    "ModelRoutingDriftError",
    "ModelRoutingError",
    "ModelRoutingMode",
    "ModelRoutingPolicy",
    "ModelRoutingRequest",
    "ModelRoutingUnavailableError",
    "ModelSelectionReason",
    "select_model_route",
]

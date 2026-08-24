from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from .model_capabilities import (
    ModelAvailability,
    ModelCapability,
    ModelCost,
    ModelDescriptor,
    ModelLimits,
)
from .model_routing import (
    FixedModelBindings,
    ModelRouteCandidate,
    ModelRouteRequirements,
    ModelRouteRole,
    ModelRoutingCatalog,
    ModelRoutingDecision,
    ModelRoutingMode,
    ModelRoutingPolicy,
    ModelRoutingRequest,
    select_model_route,
)

MODEL_ROUTING_RUNTIME_CONFIG_KEY = "capability_routing"

_CAPABILITY_FIELDS = frozenset(
    {
        "text",
        "vision",
        "tools",
        "json_schema",
        "reasoning",
        "streaming",
    }
)
_MODEL_FIELDS = frozenset(
    {
        "availability",
        "capabilities",
        "cost",
        "latency_ms",
        "limits",
        "quality_score",
    }
)
_POLICY_FIELDS = frozenset({"allow_degraded", "mode", "version"})
_REQUIREMENT_FIELDS = frozenset(
    {
        "input_tokens",
        "maximum_latency_ms",
        "maximum_unit_cost",
        "minimum_context_window",
        "minimum_quality",
        "output_tokens",
    }
)


class ModelRoutingRuntimeError(RuntimeError):
    """Base error for the trusted catalog/runtime integration boundary."""


class ModelRoutingRuntimeConfigurationError(ModelRoutingRuntimeError):
    """Explicit capability-routing configuration is incomplete or ambiguous."""


def _require_exact_fields(
    value: object,
    expected: frozenset[str],
    *,
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ModelRoutingRuntimeConfigurationError(f"{label} 字段集合非法")
    if not all(isinstance(key, str) for key in value):
        raise ModelRoutingRuntimeConfigurationError(f"{label} 字段名非法")
    return value


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ModelRoutingRuntimeConfigurationError(f"{label} 必须是精确十进制文本或整数")
    if isinstance(value, str) and (not value or value != value.strip()):
        raise ModelRoutingRuntimeConfigurationError(f"{label} 必须是精确十进制文本或整数")
    try:
        return Decimal(value)
    except Exception:
        raise ModelRoutingRuntimeConfigurationError(f"{label} 必须是精确十进制文本或整数") from None


def _cost(value: object, *, label: str) -> ModelCost | None:
    if value is None:
        return None
    fields = _require_exact_fields(
        value,
        frozenset({"input_per_million", "output_per_million"}),
        label=label,
    )
    try:
        return ModelCost(
            input_per_million=_decimal(
                fields["input_per_million"],
                label=f"{label}.input_per_million",
            ),
            output_per_million=_decimal(
                fields["output_per_million"],
                label=f"{label}.output_per_million",
            ),
        )
    except ModelRoutingRuntimeConfigurationError:
        raise
    except (TypeError, ValueError):
        raise ModelRoutingRuntimeConfigurationError(f"{label} 超出安全边界") from None


def _policy(value: object) -> ModelRoutingPolicy:
    fields = _require_exact_fields(value, _POLICY_FIELDS, label="capability_routing.policy")
    try:
        mode = ModelRoutingMode(fields["mode"])
        return ModelRoutingPolicy(
            version=fields["version"],  # type: ignore[arg-type]
            mode=mode,
            allow_degraded=fields["allow_degraded"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError):
        raise ModelRoutingRuntimeConfigurationError("capability_routing.policy 非法") from None


@dataclass(frozen=True, slots=True, repr=False)
class ModelRoutingRuntime:
    """One generation-bound trusted catalog and its request budget policy."""

    catalog: ModelRoutingCatalog
    policy: ModelRoutingPolicy
    fixed_bindings: FixedModelBindings | None
    minimum_context_window: int
    input_tokens: int
    output_tokens: int
    minimum_quality: int
    maximum_latency_ms: int
    maximum_unit_cost: ModelCost | None

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, ModelRoutingCatalog):
            raise TypeError("catalog 必须是 ModelRoutingCatalog")
        if not isinstance(self.policy, ModelRoutingPolicy):
            raise TypeError("policy 必须是 ModelRoutingPolicy")
        if self.policy.mode is ModelRoutingMode.CAPABILITY_ONLY:
            if self.fixed_bindings is not None:
                raise ValueError("capability-only runtime 不得绑定 fixed models")
        elif not isinstance(self.fixed_bindings, FixedModelBindings):
            raise ValueError("fixed routing runtime 必须绑定 fixed models")
        self._requirements(
            ModelCapability(
                text=True,
                vision=False,
                tools=False,
                json_schema=False,
                reasoning=False,
                streaming=False,
            )
        )

    @property
    def generation(self) -> int:
        return self.catalog.generation

    def _requirements(
        self,
        capabilities: ModelCapability,
    ) -> ModelRouteRequirements:
        if not isinstance(capabilities, ModelCapability):
            raise TypeError("capabilities 必须是 ModelCapability")
        return ModelRouteRequirements(
            required_capabilities=capabilities,
            minimum_context_window=self.minimum_context_window,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            minimum_quality=self.minimum_quality,
            maximum_latency_ms=self.maximum_latency_ms,
            maximum_unit_cost=self.maximum_unit_cost,
        )

    def select(
        self,
        role: ModelRouteRole,
        *,
        capabilities: ModelCapability,
    ) -> ModelRoutingDecision:
        if not isinstance(role, ModelRouteRole):
            raise TypeError("role 必须是 ModelRouteRole")
        request = ModelRoutingRequest.bind(
            catalog=self.catalog,
            policy=self.policy,
            role=role,
            requirements=self._requirements(capabilities),
            fixed_bindings=self.fixed_bindings,
        )
        return select_model_route(
            catalog=self.catalog,
            policy=self.policy,
            request=request,
        )

    def safe_diagnostics(self) -> dict[str, int | str]:
        return {
            "generation": self.generation,
            "catalog_digest": self.catalog.catalog_digest,
            "policy_digest": self.policy.digest,
            "policy_mode": self.policy.mode.value,
            "candidate_count": len(self.catalog.candidates),
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(generation={self.generation!r}, "
            f"catalog_digest={self.catalog.catalog_digest!r}, policy_digest={self.policy.digest!r})"
        )


def _candidate(
    descriptor_id: str,
    model: Mapping[str, object],
    metadata: Mapping[str, object],
    *,
    generation: int,
) -> ModelRouteCandidate:
    capability_values = _require_exact_fields(
        metadata["capabilities"],
        _CAPABILITY_FIELDS,
        label="model capability_routing.capabilities",
    )
    limit_values = _require_exact_fields(
        metadata["limits"],
        frozenset({"context_window", "max_output"}),
        label="model capability_routing.limits",
    )
    try:
        capabilities = ModelCapability(**dict(capability_values))  # type: ignore[arg-type]
        limits = ModelLimits(
            context_window=limit_values["context_window"],  # type: ignore[arg-type]
            max_output=limit_values["max_output"],  # type: ignore[arg-type]
        )
        availability = ModelAvailability(metadata["availability"])
        descriptor = ModelDescriptor(
            descriptor_id=descriptor_id,
            provider=model.get("provider"),  # type: ignore[arg-type]
            model=model.get("model"),  # type: ignore[arg-type]
            generation=generation,
            capabilities=capabilities,
            limits=limits,
            cost=_cost(metadata["cost"], label="model capability_routing.cost"),
            availability=availability,
        )
        return ModelRouteCandidate(
            descriptor=descriptor,
            quality_score=metadata["quality_score"],  # type: ignore[arg-type]
            latency_ms=metadata["latency_ms"],  # type: ignore[arg-type]
        )
    except ModelRoutingRuntimeConfigurationError:
        raise
    except (TypeError, ValueError):
        raise ModelRoutingRuntimeConfigurationError("model capability_routing 非法") from None


def build_model_routing_runtime(
    *,
    generation: int,
    models: Mapping[str, object],
    model_config: Mapping[str, object],
) -> ModelRoutingRuntime | None:
    """Build from explicit allowlisted metadata without retaining transport data."""

    if not isinstance(models, Mapping) or not isinstance(model_config, Mapping):
        raise TypeError("models 与 model_config 必须是映射")
    raw_config = model_config.get(MODEL_ROUTING_RUNTIME_CONFIG_KEY)
    if raw_config is None:
        return None
    if not isinstance(raw_config, Mapping):
        raise ModelRoutingRuntimeConfigurationError("capability_routing 必须是对象")
    enabled = raw_config.get("enabled")
    if type(enabled) is not bool:
        raise ModelRoutingRuntimeConfigurationError("capability_routing.enabled 必须是 bool")
    if not enabled:
        if set(raw_config) != {"enabled"}:
            raise ModelRoutingRuntimeConfigurationError("关闭的 capability_routing 只能包含 enabled")
        return None
    config = _require_exact_fields(
        raw_config,
        frozenset({"enabled", "policy", "requirements"}),
        label="capability_routing",
    )
    policy = _policy(config["policy"])
    requirement_values = _require_exact_fields(
        config["requirements"],
        _REQUIREMENT_FIELDS,
        label="capability_routing.requirements",
    )
    candidates: list[ModelRouteCandidate] = []
    for descriptor_id, value in models.items():
        if not isinstance(descriptor_id, str) or not isinstance(value, Mapping):
            raise ModelRoutingRuntimeConfigurationError("model catalog identity 非法")
        metadata = value.get(MODEL_ROUTING_RUNTIME_CONFIG_KEY)
        if metadata is None:
            continue
        fields = _require_exact_fields(
            metadata,
            _MODEL_FIELDS,
            label="model capability_routing",
        )
        candidates.append(
            _candidate(
                descriptor_id,
                value,
                fields,
                generation=generation,
            )
        )
    try:
        catalog = ModelRoutingCatalog(
            generation=generation,
            candidates=tuple(candidates),
        )
        fixed_bindings = (
            None if policy.mode is ModelRoutingMode.CAPABILITY_ONLY else FixedModelBindings.from_model_config(model_config)
        )
        return ModelRoutingRuntime(
            catalog=catalog,
            policy=policy,
            fixed_bindings=fixed_bindings,
            minimum_context_window=requirement_values["minimum_context_window"],  # type: ignore[arg-type]
            input_tokens=requirement_values["input_tokens"],  # type: ignore[arg-type]
            output_tokens=requirement_values["output_tokens"],  # type: ignore[arg-type]
            minimum_quality=requirement_values["minimum_quality"],  # type: ignore[arg-type]
            maximum_latency_ms=requirement_values["maximum_latency_ms"],  # type: ignore[arg-type]
            maximum_unit_cost=_cost(
                requirement_values["maximum_unit_cost"],
                label="capability_routing.requirements.maximum_unit_cost",
            ),
        )
    except ModelRoutingRuntimeConfigurationError:
        raise
    except (TypeError, ValueError):
        raise ModelRoutingRuntimeConfigurationError("capability_routing runtime 非法") from None


__all__ = [
    "MODEL_ROUTING_RUNTIME_CONFIG_KEY",
    "ModelRoutingRuntime",
    "ModelRoutingRuntimeConfigurationError",
    "ModelRoutingRuntimeError",
    "build_model_routing_runtime",
]

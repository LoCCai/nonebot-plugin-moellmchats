from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
import json
import re
import socket

import pytest

from nonebot_plugin_moellmchats.model_capabilities import (
    MODEL_COST_SCALE,
    ModelAvailability,
    ModelCapability,
    ModelCost,
    ModelDescriptor,
    ModelLimits,
)
from nonebot_plugin_moellmchats.model_routing import (
    MODEL_ROUTING_COST_DENOMINATOR,
    MODEL_ROUTING_DYNAMIC_ORDER,
    MODEL_ROUTING_MAX_CANDIDATES,
    MODEL_ROUTING_MAX_COST_NUMERATOR,
    MODEL_ROUTING_MAX_JSON_BYTES,
    MODEL_ROUTING_MAX_LATENCY_MS,
    MODEL_ROUTING_MAX_POLICY_VERSION_CHARS,
    MODEL_ROUTING_MAX_QUALITY_SCORE,
    MODEL_ROUTING_SCHEMA_VERSION,
    FixedModelBindings,
    ModelRouteCandidate,
    ModelRouteRequirements,
    ModelRouteRole,
    ModelRoutingCatalog,
    ModelRoutingDecision,
    ModelRoutingDriftError,
    ModelRoutingError,
    ModelRoutingMode,
    ModelRoutingPolicy,
    ModelRoutingRequest,
    ModelRoutingUnavailableError,
    ModelSelectionReason,
    select_model_route,
)


def _capabilities(**overrides: bool) -> ModelCapability:
    values = {
        "text": True,
        "vision": False,
        "tools": True,
        "json_schema": True,
        "reasoning": False,
        "streaming": True,
    }
    values.update(overrides)
    return ModelCapability(**values)


def _descriptor(
    descriptor_id: str = "primary (provider-a)",
    *,
    generation: int = 9,
    availability: ModelAvailability = ModelAvailability.AVAILABLE,
    capabilities: ModelCapability | None = None,
    context_window: int = 128_000,
    max_output: int = 16_384,
    input_cost: str | None = "1",
    output_cost: str = "2",
) -> ModelDescriptor:
    provider = descriptor_id.rsplit("(", 1)[-1].rstrip(")") if "(" in descriptor_id else "provider"
    model = descriptor_id.split(" (", 1)[0]
    cost = (
        None
        if input_cost is None
        else ModelCost(
            input_per_million=Decimal(input_cost),
            output_per_million=Decimal(output_cost),
        )
    )
    return ModelDescriptor(
        descriptor_id=descriptor_id,
        provider=provider,
        model=model,
        generation=generation,
        capabilities=_capabilities() if capabilities is None else capabilities,
        limits=ModelLimits(context_window=context_window, max_output=max_output),
        cost=cost,
        availability=availability,
    )


def _candidate(
    descriptor_id: str = "primary (provider-a)",
    *,
    generation: int = 9,
    availability: ModelAvailability = ModelAvailability.AVAILABLE,
    capabilities: ModelCapability | None = None,
    context_window: int = 128_000,
    max_output: int = 16_384,
    input_cost: str | None = "1",
    output_cost: str = "2",
    quality: int = 500,
    latency_ms: int = 500,
) -> ModelRouteCandidate:
    return ModelRouteCandidate(
        descriptor=_descriptor(
            descriptor_id,
            generation=generation,
            availability=availability,
            capabilities=capabilities,
            context_window=context_window,
            max_output=max_output,
            input_cost=input_cost,
            output_cost=output_cost,
        ),
        quality_score=quality,
        latency_ms=latency_ms,
    )


def _catalog(*candidates: ModelRouteCandidate, generation: int = 9) -> ModelRoutingCatalog:
    return ModelRoutingCatalog(
        generation=generation,
        candidates=candidates or (_candidate(generation=generation),),
    )


def _requirements(**overrides: object) -> ModelRouteRequirements:
    values: dict[str, object] = {
        "required_capabilities": _capabilities(),
        "minimum_context_window": 8_192,
        "input_tokens": 4_096,
        "output_tokens": 1_024,
        "minimum_quality": 0,
        "maximum_latency_ms": 10_000,
        "maximum_unit_cost": None,
    }
    values.update(overrides)
    return ModelRouteRequirements(**values)  # type: ignore[arg-type]


def _policy(
    mode: ModelRoutingMode = ModelRoutingMode.CAPABILITY_ONLY,
    *,
    version: str = "routing-v1",
    allow_degraded: bool = False,
) -> ModelRoutingPolicy:
    return ModelRoutingPolicy(
        version=version,
        mode=mode,
        allow_degraded=allow_degraded,
    )


def _bindings(**overrides: object) -> FixedModelBindings:
    values: dict[str, object] = {
        "selected_model": "fixed-chat (provider-a)",
        "vision_model": "fixed-vision (provider-a)",
        "category_model": "fixed-category (provider-a)",
        "summary_model": "fixed-summary (provider-a)",
        "moe_0": "fixed-easy (provider-a)",
        "moe_1": "fixed-medium (provider-a)",
        "moe_2": "fixed-hard (provider-a)",
    }
    values.update(overrides)
    return FixedModelBindings(**values)  # type: ignore[arg-type]


def _request(
    catalog: ModelRoutingCatalog,
    policy: ModelRoutingPolicy,
    *,
    requirements: ModelRouteRequirements | None = None,
    role: ModelRouteRole = ModelRouteRole.SELECTED,
    fixed_bindings: FixedModelBindings | None = None,
) -> ModelRoutingRequest:
    return ModelRoutingRequest.bind(
        catalog=catalog,
        policy=policy,
        role=role,
        requirements=_requirements() if requirements is None else requirements,
        fixed_bindings=fixed_bindings,
    )


def _select(
    catalog: ModelRoutingCatalog,
    policy: ModelRoutingPolicy,
    *,
    requirements: ModelRouteRequirements | None = None,
    role: ModelRouteRole = ModelRouteRole.SELECTED,
    fixed_bindings: FixedModelBindings | None = None,
) -> ModelRoutingDecision:
    request = _request(
        catalog,
        policy,
        requirements=requirements,
        role=role,
        fixed_bindings=fixed_bindings,
    )
    return select_model_route(catalog=catalog, policy=policy, request=request)


def test_public_schema_and_routing_bounds_are_explicit() -> None:
    assert MODEL_ROUTING_SCHEMA_VERSION == 1
    assert MODEL_ROUTING_MAX_CANDIDATES == 1_024
    assert MODEL_ROUTING_MAX_JSON_BYTES == 2_097_152
    assert MODEL_ROUTING_MAX_LATENCY_MS == 86_400_000
    assert MODEL_ROUTING_MAX_POLICY_VERSION_CHARS == 128
    assert MODEL_ROUTING_MAX_QUALITY_SCORE == 1_000_000
    assert MODEL_ROUTING_COST_DENOMINATOR == 10 ** (MODEL_COST_SCALE + 6)
    assert MODEL_ROUTING_MAX_COST_NUMERATOR == ((10**24) - 1) * 100_000_000
    assert MODEL_ROUTING_DYNAMIC_ORDER == (
        "availability_asc",
        "quality_desc",
        "latency_asc",
        "estimated_cost_asc",
        "identity_digest_asc",
    )


def test_public_enums_are_closed_and_preserve_all_fixed_roles() -> None:
    assert tuple(role.value for role in ModelRouteRole) == (
        "selected_model",
        "vision_model",
        "category_model",
        "summary_model",
        "moe_models.0",
        "moe_models.1",
        "moe_models.2",
    )
    assert tuple(mode.value for mode in ModelRoutingMode) == (
        "capability_only",
        "fixed_preferred",
        "fixed_only",
    )
    assert tuple(reason.value for reason in ModelSelectionReason) == (
        "capability",
        "fixed",
    )


def test_fixed_bindings_detach_only_legacy_model_ids_from_config() -> None:
    model_config: dict[str, object] = {
        "selected_model": "chat (a)",
        "vision_model": "",
        "category_model": "category (a)",
        "summary_model": "summary (a)",
        "moe_models": {"0": "easy (a)", "1": "medium (a)", "2": "hard (a)"},
        "api_key": "must-not-be-read",
        "url": "https://must-not-be-read.invalid",
        "proxy": "must-not-be-read",
    }

    bindings = FixedModelBindings.from_model_config(model_config)
    model_config["selected_model"] = "changed (a)"
    cast_moe = model_config["moe_models"]
    assert isinstance(cast_moe, dict)
    cast_moe["0"] = "changed-easy (a)"

    assert bindings.as_dict() == {
        "category_model": "category (a)",
        "moe_0": "easy (a)",
        "moe_1": "medium (a)",
        "moe_2": "hard (a)",
        "selected_model": "chat (a)",
        "summary_model": "summary (a)",
        "vision_model": None,
    }
    serialized = json.dumps(bindings.as_dict(), sort_keys=True)
    assert "must-not-be-read" not in serialized
    assert "chat (a)" not in repr(bindings)
    assert re.fullmatch(r"[0-9a-f]{64}", bindings.digest)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (ModelRouteRole.SELECTED, "fixed-chat (provider-a)"),
        (ModelRouteRole.VISION, "fixed-vision (provider-a)"),
        (ModelRouteRole.CATEGORY, "fixed-category (provider-a)"),
        (ModelRouteRole.SUMMARY, "fixed-summary (provider-a)"),
        (ModelRouteRole.MOE_0, "fixed-easy (provider-a)"),
        (ModelRouteRole.MOE_1, "fixed-medium (provider-a)"),
        (ModelRouteRole.MOE_2, "fixed-hard (provider-a)"),
    ],
)
def test_fixed_bindings_resolve_every_bounded_role(role: ModelRouteRole, expected: str) -> None:
    assert _bindings().for_role(role) == expected


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("selected_model", ""),
        ("selected_model", " leading"),
        ("selected_model", "bad\x00id"),
        ("vision_model", ""),
        ("category_model", None),
        ("summary_model", 1),
        ("moe_0", "x" * 513),
        ("moe_1", "bad\nname"),
        ("moe_2", "trailing "),
    ],
)
def test_fixed_bindings_reject_unsafe_or_untyped_ids(field_name: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError), match=field_name):
        _bindings(**{field_name: value})


@pytest.mark.parametrize(
    "model_config",
    [
        {},
        {"moe_models": []},
        {
            "selected_model": "chat (a)",
            "vision_model": 1,
            "category_model": "category (a)",
            "summary_model": "summary (a)",
            "moe_models": {"0": "easy (a)", "1": "medium (a)", "2": "hard (a)"},
        },
    ],
)
def test_fixed_bindings_reject_malformed_legacy_config(model_config: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        FixedModelBindings.from_model_config(model_config)  # type: ignore[arg-type]


def test_fixed_bindings_are_frozen_and_digest_all_roles() -> None:
    baseline = _bindings()
    changed = replace(baseline, moe_2="other-hard (provider-a)")

    assert baseline.digest != changed.digest
    with pytest.raises((FrozenInstanceError, AttributeError)):
        baseline.selected_model = "changed"  # type: ignore[misc]


def test_routing_policy_is_frozen_canonical_and_digest_bound() -> None:
    policy = _policy(ModelRoutingMode.FIXED_PREFERRED, allow_degraded=True)
    changed_mode = replace(policy, mode=ModelRoutingMode.FIXED_ONLY)
    changed_degraded = replace(policy, allow_degraded=False)

    assert policy.as_dict() == {
        "allow_degraded": True,
        "dynamic_order": list(MODEL_ROUTING_DYNAMIC_ORDER),
        "mode": "fixed_preferred",
        "schema_version": 1,
        "version": "routing-v1",
    }
    assert policy.digest != changed_mode.digest
    assert policy.digest != changed_degraded.digest
    assert re.fullmatch(r"[0-9a-f]{64}", policy.digest)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        policy.allow_degraded = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("version", ""),
        ("version", " bad"),
        ("version", "bad\nversion"),
        ("version", "x" * (MODEL_ROUTING_MAX_POLICY_VERSION_CHARS + 1)),
        ("mode", "capability_only"),
        ("allow_degraded", 1),
    ],
)
def test_routing_policy_rejects_unbounded_or_untyped_values(field_name: str, value: object) -> None:
    values: dict[str, object] = {
        "version": "routing-v1",
        "mode": ModelRoutingMode.CAPABILITY_ONLY,
        "allow_degraded": False,
    }
    values[field_name] = value
    with pytest.raises((TypeError, ValueError), match=field_name):
        ModelRoutingPolicy(**values)  # type: ignore[arg-type]


def test_candidate_is_frozen_canonical_and_contains_no_transport_fields() -> None:
    candidate = _candidate()
    payload = candidate.as_dict()

    assert payload["quality_score"] == 500
    assert payload["latency_ms"] == 500
    assert not ({"api_key", "credential", "endpoint", "headers", "key", "proxy", "secret", "url"} & payload.keys())
    assert re.fullmatch(r"[0-9a-f]{64}", candidate.candidate_digest)
    assert candidate.descriptor.descriptor_id not in repr(candidate)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        candidate.quality_score = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("quality_score", -1),
        ("quality_score", True),
        ("quality_score", MODEL_ROUTING_MAX_QUALITY_SCORE + 1),
        ("latency_ms", -1),
        ("latency_ms", False),
        ("latency_ms", MODEL_ROUTING_MAX_LATENCY_MS + 1),
    ],
)
def test_candidate_rejects_invalid_quality_or_latency(field_name: str, value: object) -> None:
    values: dict[str, object] = {
        "descriptor": _descriptor(),
        "quality_score": 500,
        "latency_ms": 500,
    }
    values[field_name] = value
    with pytest.raises(ValueError, match=field_name):
        ModelRouteCandidate(**values)  # type: ignore[arg-type]


def test_catalog_is_order_independent_detached_and_canonical() -> None:
    first = _candidate("first (a)", quality=100)
    second = _candidate("second (b)", quality=200)
    forward = _catalog(first, second)
    reverse = _catalog(second, first)
    payload = forward.as_dict()
    payload["candidates"][0]["quality_score"] = 999

    assert forward.catalog_digest == reverse.catalog_digest
    assert forward.to_json() == reverse.to_json()
    assert forward.as_dict()["candidates"][0]["quality_score"] != 999
    assert len(forward.to_json().encode("ascii")) <= MODEL_ROUTING_MAX_JSON_BYTES
    assert re.fullmatch(r"[0-9a-f]{64}", forward.catalog_digest)


def test_catalog_digest_binds_generation_descriptor_and_routing_metrics() -> None:
    baseline_candidate = _candidate()
    baseline = _catalog(baseline_candidate)
    changed_quality = _catalog(replace(baseline_candidate, quality_score=501))
    changed_latency = _catalog(replace(baseline_candidate, latency_ms=501))
    changed_descriptor = _catalog(
        replace(
            baseline_candidate,
            descriptor=replace(baseline_candidate.descriptor, availability=ModelAvailability.DEGRADED),
        )
    )
    changed_generation_candidate = _candidate(generation=10)
    changed_generation = _catalog(changed_generation_candidate, generation=10)

    assert (
        len(
            {
                baseline.catalog_digest,
                changed_quality.catalog_digest,
                changed_latency.catalog_digest,
                changed_descriptor.catalog_digest,
                changed_generation.catalog_digest,
            }
        )
        == 5
    )


@pytest.mark.parametrize(
    "candidates",
    [
        (),
        [_candidate()],
        tuple(_candidate(f"model-{index} (a)") for index in range(MODEL_ROUTING_MAX_CANDIDATES + 1)),
    ],
)
def test_catalog_rejects_empty_untyped_or_oversized_candidate_collections(candidates: object) -> None:
    with pytest.raises(ValueError, match="candidates"):
        ModelRoutingCatalog(generation=9, candidates=candidates)  # type: ignore[arg-type]


def test_catalog_rejects_duplicates_and_mixed_generations() -> None:
    candidate = _candidate()
    with pytest.raises(ValueError, match="descriptor_id"):
        _catalog(candidate, replace(candidate, quality_score=501))
    with pytest.raises(ValueError, match="generation"):
        _catalog(candidate, _candidate("other (b)", generation=10))


def test_requirements_are_frozen_capability_bound_and_canonical() -> None:
    requirements = _requirements(
        maximum_unit_cost=ModelCost(
            input_per_million=Decimal("3"),
            output_per_million=Decimal("6"),
        )
    )
    payload = requirements.as_dict()
    payload["required_capabilities"]["tools"] = False

    assert requirements.capability_digest == requirements.required_capabilities.digest
    assert requirements.as_dict()["required_capabilities"]["tools"] is True
    assert requirements.as_dict()["maximum_unit_cost"] == {
        "input_per_million": "3",
        "output_per_million": "6",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", requirements.requirements_digest)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        requirements.input_tokens = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("minimum_context_window", 0, "minimum_context_window"),
        ("minimum_context_window", True, "minimum_context_window"),
        ("input_tokens", -1, "input_tokens"),
        ("input_tokens", False, "input_tokens"),
        ("output_tokens", 0, "output_tokens"),
        ("output_tokens", "1", "output_tokens"),
        ("minimum_quality", -1, "minimum_quality"),
        ("minimum_quality", MODEL_ROUTING_MAX_QUALITY_SCORE + 1, "minimum_quality"),
        ("maximum_latency_ms", -1, "maximum_latency_ms"),
        ("maximum_latency_ms", MODEL_ROUTING_MAX_LATENCY_MS + 1, "maximum_latency_ms"),
        ("maximum_unit_cost", Decimal("1"), "maximum_unit_cost"),
    ],
)
def test_requirements_reject_invalid_or_unbounded_values(
    field_name: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _requirements(**{field_name: value})


def test_requirements_reject_empty_capability_and_token_budget_over_context() -> None:
    empty = ModelCapability(
        text=False,
        vision=False,
        tools=False,
        json_schema=False,
        reasoning=False,
        streaming=False,
    )
    with pytest.raises(ValueError, match="capability"):
        _requirements(required_capabilities=empty)
    with pytest.raises(ValueError, match="token"):
        _requirements(minimum_context_window=100, input_tokens=90, output_tokens=11)


def test_request_bind_captures_catalog_policy_capability_and_fixed_digests() -> None:
    catalog = _catalog()
    policy = _policy(ModelRoutingMode.FIXED_PREFERRED)
    requirements = _requirements()
    bindings = _bindings()
    request = _request(
        catalog,
        policy,
        requirements=requirements,
        fixed_bindings=bindings,
    )

    assert request.catalog_generation == catalog.generation
    assert request.catalog_digest == catalog.catalog_digest
    assert request.policy_version == policy.version
    assert request.policy_digest == policy.digest
    assert request.capability_digest == requirements.capability_digest
    assert request.as_dict()["fixed_bindings_digest"] == bindings.digest
    assert re.fullmatch(r"[0-9a-f]{64}", request.request_digest)
    assert bindings.selected_model not in repr(request)


@pytest.mark.parametrize(
    "capability_name",
    ["text", "vision", "tools", "json_schema", "reasoning", "streaming"],
)
def test_selector_requires_every_explicit_capability(capability_name: str) -> None:
    required_values = {name: name == capability_name for name in _capabilities().as_dict()}
    supported_values = dict(required_values)
    unsupported_values = dict(required_values)
    unsupported_values[capability_name] = False
    required = ModelCapability(**required_values)
    supported = ModelCapability(**supported_values)
    unsupported = ModelCapability(**unsupported_values)
    catalog = _catalog(
        _candidate("unsupported (a)", capabilities=unsupported, quality=999),
        _candidate("supported (b)", capabilities=supported, quality=1),
    )

    decision = _select(catalog, _policy(), requirements=_requirements(required_capabilities=required))

    assert decision.selected.descriptor.descriptor_id == "supported (b)"


def test_selector_requires_context_and_output_limits() -> None:
    catalog = _catalog(
        _candidate("small-context (a)", context_window=8_191, max_output=2_048, quality=999),
        _candidate("small-output (b)", context_window=128_000, max_output=1_023, quality=999),
        _candidate("sufficient (c)", context_window=8_192, max_output=1_024, quality=1),
    )

    decision = _select(catalog, _policy())

    assert decision.selected.descriptor.descriptor_id == "sufficient (c)"


def test_unknown_and_unavailable_candidates_are_never_selected() -> None:
    catalog = _catalog(
        _candidate("unknown (a)", availability=ModelAvailability.UNKNOWN, quality=999, latency_ms=1),
        _candidate("unavailable (b)", availability=ModelAvailability.UNAVAILABLE, quality=999, latency_ms=1),
        _candidate("available (c)", quality=1, latency_ms=9_000),
    )

    decision = _select(catalog, _policy(allow_degraded=True))

    assert decision.selected.descriptor.descriptor_id == "available (c)"


def test_degraded_candidate_requires_explicit_policy_and_ranks_after_available() -> None:
    degraded = _candidate(
        "degraded (a)",
        availability=ModelAvailability.DEGRADED,
        quality=999,
        latency_ms=1,
        input_cost="0",
        output_cost="0",
    )
    available = _candidate("available (b)", quality=1, latency_ms=9_000, input_cost="9", output_cost="9")
    catalog = _catalog(degraded, available)

    strict = _select(catalog, _policy(allow_degraded=False))
    permissive = _select(catalog, _policy(allow_degraded=True))

    assert strict.selected.descriptor.descriptor_id == "available (b)"
    assert permissive.selected.descriptor.descriptor_id == "available (b)"


def test_degraded_candidate_can_be_selected_only_when_no_available_candidate_remains() -> None:
    catalog = _catalog(_candidate("degraded (a)", availability=ModelAvailability.DEGRADED))
    with pytest.raises(ModelRoutingUnavailableError):
        _select(catalog, _policy(allow_degraded=False))

    decision = _select(catalog, _policy(allow_degraded=True))

    assert decision.selected.descriptor.descriptor_id == "degraded (a)"


def test_quality_threshold_and_order_precede_latency_and_cost() -> None:
    high_quality = _candidate(
        "high-quality (a)",
        quality=900,
        latency_ms=9_000,
        input_cost="9",
        output_cost="9",
    )
    low_quality = _candidate(
        "low-quality (b)",
        quality=899,
        latency_ms=1,
        input_cost="0",
        output_cost="0",
    )
    catalog = _catalog(high_quality, low_quality)

    decision = _select(catalog, _policy(), requirements=_requirements(minimum_quality=900))

    assert decision.selected is high_quality


def test_latency_threshold_and_order_precede_cost() -> None:
    low_latency = _candidate(
        "low-latency (a)",
        quality=500,
        latency_ms=100,
        input_cost="9",
        output_cost="9",
    )
    low_cost = _candidate(
        "low-cost (b)",
        quality=500,
        latency_ms=101,
        input_cost="0",
        output_cost="0",
    )
    catalog = _catalog(low_latency, low_cost)

    decision = _select(catalog, _policy(), requirements=_requirements(maximum_latency_ms=100))

    assert decision.selected is low_latency


def test_exact_estimated_cost_is_the_final_business_rank() -> None:
    input_cheap = _candidate(
        "input-cheap (a)",
        input_cost="0.1",
        output_cost="2",
    )
    output_cheap = _candidate(
        "output-cheap (b)",
        input_cost="1",
        output_cost="0.1",
    )
    catalog = _catalog(input_cheap, output_cheap)
    requirements = _requirements(input_tokens=7_000, output_tokens=1_000, minimum_context_window=8_000)

    decision = _select(catalog, _policy(), requirements=requirements)

    assert decision.selected is input_cheap
    expected = 100_000_000_000 * 7_000 + 2_000_000_000_000 * 1_000
    assert decision.estimated_cost_numerator == expected


def test_unit_cost_ceiling_rejects_either_input_or_output_overage() -> None:
    maximum = ModelCost(input_per_million=Decimal("2"), output_per_million=Decimal("4"))
    catalog = _catalog(
        _candidate("input-over (a)", input_cost="2.1", output_cost="1", quality=999),
        _candidate("output-over (b)", input_cost="1", output_cost="4.1", quality=999),
        _candidate("bounded (c)", input_cost="2", output_cost="4", quality=1),
    )

    decision = _select(catalog, _policy(), requirements=_requirements(maximum_unit_cost=maximum))

    assert decision.selected.descriptor.descriptor_id == "bounded (c)"


def test_unknown_cost_is_never_selected_or_ranked_as_free() -> None:
    unknown = _candidate("unknown-cost (a)", input_cost=None, quality=999, latency_ms=1)
    known = _candidate("known-cost (b)", input_cost="10", output_cost="10", quality=1, latency_ms=9_000)
    decision = _select(_catalog(unknown, known), _policy())

    assert decision.selected is known
    with pytest.raises(ModelRoutingUnavailableError):
        _select(_catalog(unknown), _policy())


def test_cost_ranking_is_independent_of_ambient_decimal_context() -> None:
    first = _candidate("first (a)", input_cost="0.123456789012", output_cost="0.000000000001")
    second = _candidate("second (b)", input_cost="0.123456789013", output_cost="0.000000000001")
    catalog = _catalog(first, second)
    requirements = _requirements(input_tokens=7_000, output_tokens=1_000, minimum_context_window=8_000)

    with localcontext() as context:
        context.prec = 1
        decision = _select(catalog, _policy(), requirements=requirements)

    assert decision.selected is first


def test_exact_ties_are_stable_across_catalog_input_order() -> None:
    first = _candidate("first (a)")
    second = _candidate("second (b)")
    forward = _catalog(first, second)
    reverse = _catalog(second, first)

    forward_decision = _select(forward, _policy())
    reverse_decision = _select(reverse, _policy())

    assert forward.catalog_digest == reverse.catalog_digest
    assert forward_decision.selected.descriptor.identity_digest == reverse_decision.selected.descriptor.identity_digest


def test_fixed_only_is_an_explicit_rollback_even_when_dynamic_rank_is_worse() -> None:
    fixed = _candidate("fixed-chat (provider-a)", quality=1, latency_ms=9_000, input_cost="9", output_cost="9")
    dynamic = _candidate("dynamic (provider-b)", quality=999, latency_ms=1, input_cost="0", output_cost="0")
    catalog = _catalog(fixed, dynamic)

    decision = _select(
        catalog,
        _policy(ModelRoutingMode.FIXED_ONLY),
        fixed_bindings=_bindings(),
    )

    assert decision.selected is fixed
    assert decision.reason is ModelSelectionReason.FIXED


@pytest.mark.parametrize(
    ("role", "bindings"),
    [
        (ModelRouteRole.SELECTED, _bindings(selected_model="missing-private-model (a)")),
        (ModelRouteRole.VISION, _bindings(vision_model=None)),
    ],
)
def test_fixed_only_fails_closed_without_an_eligible_pin(
    role: ModelRouteRole,
    bindings: FixedModelBindings,
) -> None:
    catalog = _catalog(_candidate("dynamic (provider-b)"))
    with pytest.raises(ModelRoutingUnavailableError) as error:
        _select(
            catalog,
            _policy(ModelRoutingMode.FIXED_ONLY),
            role=role,
            fixed_bindings=bindings,
        )

    assert "missing-private-model" not in str(error.value)


def test_fixed_only_rechecks_full_capability_and_limit_contract() -> None:
    fixed = _candidate(
        "fixed-chat (provider-a)",
        capabilities=_capabilities(tools=False),
        quality=999,
    )
    dynamic = _candidate("dynamic (provider-b)", capabilities=_capabilities(tools=True))
    catalog = _catalog(fixed, dynamic)

    with pytest.raises(ModelRoutingUnavailableError):
        _select(
            catalog,
            _policy(ModelRoutingMode.FIXED_ONLY),
            fixed_bindings=_bindings(),
        )


def test_fixed_preferred_uses_pin_when_eligible_and_falls_back_when_not() -> None:
    fixed = _candidate("fixed-chat (provider-a)", quality=1)
    dynamic = _candidate("dynamic (provider-b)", quality=999)
    catalog = _catalog(fixed, dynamic)
    policy = _policy(ModelRoutingMode.FIXED_PREFERRED)

    preferred = _select(catalog, policy, fixed_bindings=_bindings())
    fallback = _select(
        catalog,
        policy,
        fixed_bindings=_bindings(selected_model="missing (provider-a)"),
    )

    assert preferred.selected is fixed
    assert preferred.reason is ModelSelectionReason.FIXED
    assert fallback.selected is dynamic
    assert fallback.reason is ModelSelectionReason.CAPABILITY


def test_capability_only_rejects_ambiguous_fixed_bindings() -> None:
    catalog = _catalog()
    policy = _policy(ModelRoutingMode.CAPABILITY_ONLY)
    request = _request(catalog, policy, fixed_bindings=_bindings())

    with pytest.raises(ModelRoutingError, match="fixed bindings"):
        select_model_route(catalog=catalog, policy=policy, request=request)


@pytest.mark.parametrize("mode", [ModelRoutingMode.FIXED_ONLY, ModelRoutingMode.FIXED_PREFERRED])
def test_fixed_modes_require_a_bound_fixed_snapshot(mode: ModelRoutingMode) -> None:
    catalog = _catalog()
    policy = _policy(mode)
    request = _request(catalog, policy)

    with pytest.raises(ModelRoutingError, match="fixed model snapshot"):
        select_model_route(catalog=catalog, policy=policy, request=request)


def test_catalog_generation_and_digest_drift_fail_before_selection() -> None:
    baseline = _catalog(_candidate(quality=500))
    changed = _catalog(_candidate(quality=501))
    policy = _policy()
    request = _request(baseline, policy)

    with pytest.raises(ModelRoutingDriftError, match="catalog"):
        select_model_route(catalog=changed, policy=policy, request=request)
    stale_generation = replace(request, catalog_generation=8)
    with pytest.raises(ModelRoutingDriftError, match="catalog"):
        select_model_route(catalog=baseline, policy=policy, request=stale_generation)


def test_policy_version_and_digest_drift_fail_before_selection() -> None:
    catalog = _catalog()
    baseline = _policy()
    request = _request(catalog, baseline)
    changed_digest = replace(baseline, allow_degraded=True)
    changed_version = replace(baseline, version="routing-v2")

    with pytest.raises(ModelRoutingDriftError, match="policy"):
        select_model_route(catalog=catalog, policy=changed_digest, request=request)
    with pytest.raises(ModelRoutingDriftError, match="policy"):
        select_model_route(catalog=catalog, policy=changed_version, request=request)


def test_decision_is_frozen_digest_bound_detached_and_credential_free() -> None:
    catalog = _catalog()
    policy = _policy()
    request = _request(catalog, policy)
    decision = select_model_route(catalog=catalog, policy=policy, request=request)
    payload = decision.as_dict()
    payload["candidate_digest"] = "0" * 64

    assert decision.request_digest == request.request_digest
    assert decision.catalog_digest == catalog.catalog_digest
    assert decision.policy_digest == policy.digest
    assert decision.capability_digest == request.capability_digest
    assert decision.as_dict()["candidate_digest"] == decision.selected.candidate_digest
    assert decision.as_dict()["cost_denominator"] == str(MODEL_ROUTING_COST_DENOMINATOR)
    assert re.fullmatch(r"[0-9a-f]{64}", decision.decision_digest)
    serialized = json.dumps(decision.as_dict(), sort_keys=True)
    assert not any(token in serialized for token in ("api_key", "credential", "endpoint", "proxy", "secret", "url"))
    assert decision.selected.descriptor.descriptor_id not in repr(decision)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        decision.reason = ModelSelectionReason.FIXED  # type: ignore[misc]


def test_decision_rejects_unbounded_or_untyped_cost_numerator() -> None:
    decision = _select(_catalog(), _policy())

    for value in (-1, True, MODEL_ROUTING_MAX_COST_NUMERATOR + 1):
        with pytest.raises(ValueError, match="estimated_cost_numerator"):
            replace(decision, estimated_cost_numerator=value)


def test_capability_change_changes_request_and_decision_identity() -> None:
    catalog = _catalog(_candidate(capabilities=_capabilities(reasoning=True)))
    policy = _policy()
    text_request = _request(
        catalog,
        policy,
        requirements=_requirements(required_capabilities=_capabilities(reasoning=False)),
    )
    reasoning_request = _request(
        catalog,
        policy,
        requirements=_requirements(required_capabilities=_capabilities(reasoning=True)),
    )
    text_decision = select_model_route(catalog=catalog, policy=policy, request=text_request)
    reasoning_decision = select_model_route(catalog=catalog, policy=policy, request=reasoning_request)

    assert text_request.capability_digest != reasoning_request.capability_digest
    assert text_request.request_digest != reasoning_request.request_digest
    assert text_decision.decision_digest != reasoning_decision.decision_digest


def test_routing_domain_and_selection_perform_no_file_or_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog()
    policy = _policy()
    requirements = _requirements()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected I/O")

    monkeypatch.setattr(builtins, "open", fail)
    monkeypatch.setattr(socket, "socket", fail)

    request = _request(catalog, policy, requirements=requirements)
    decision = select_model_route(catalog=catalog, policy=policy, request=request)

    assert decision.selected.descriptor.descriptor_id == "primary (provider-a)"

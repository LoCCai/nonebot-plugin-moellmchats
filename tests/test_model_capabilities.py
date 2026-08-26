from __future__ import annotations

import builtins
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import json
import re
import socket

import pytest

from nonebot_plugin_moellmchats.database_schema import (
    MODEL_NAME_MAX_CHARS as DATABASE_MODEL_NAME_MAX_CHARS,
)
from nonebot_plugin_moellmchats.database_schema import (
    MODEL_PROVIDER_MAX_CHARS as DATABASE_MODEL_PROVIDER_MAX_CHARS,
)
from nonebot_plugin_moellmchats.database_schema import (
    MODEL_USAGE_COST_PRECISION,
    MODEL_USAGE_COST_SCALE,
)
from nonebot_plugin_moellmchats.model_capabilities import (
    MODEL_CAPABILITY_SCHEMA_VERSION,
    MODEL_COST_PRECISION,
    MODEL_COST_SCALE,
    MODEL_DESCRIPTOR_ID_MAX_CHARS,
    MODEL_DESCRIPTOR_MAX_JSON_BYTES,
    MODEL_DESCRIPTOR_SCHEMA_VERSION,
    MODEL_LIMIT_MAX_TOKENS,
    MODEL_NAME_MAX_CHARS,
    MODEL_PROVIDER_MAX_CHARS,
    ModelAvailability,
    ModelCapability,
    ModelCost,
    ModelDescriptor,
    ModelLimits,
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


def _descriptor(**overrides: object) -> ModelDescriptor:
    values: dict[str, object] = {
        "descriptor_id": "gpt-4.1 (openai)",
        "provider": "openai",
        "model": "gpt-4.1",
        "generation": 7,
        "capabilities": _capabilities(),
        "limits": ModelLimits(context_window=1_000_000, max_output=32_768),
        "cost": ModelCost(
            input_per_million=Decimal("2.00"),
            output_per_million=Decimal("8.000000"),
        ),
        "availability": ModelAvailability.AVAILABLE,
    }
    values.update(overrides)
    return ModelDescriptor(**values)  # type: ignore[arg-type]


def test_public_schema_and_storage_bounds_are_explicit_and_aligned() -> None:
    assert MODEL_CAPABILITY_SCHEMA_VERSION == 1
    assert MODEL_DESCRIPTOR_SCHEMA_VERSION == 1
    assert MODEL_COST_PRECISION == MODEL_USAGE_COST_PRECISION == 24
    assert MODEL_COST_SCALE == MODEL_USAGE_COST_SCALE == 12
    assert MODEL_PROVIDER_MAX_CHARS == DATABASE_MODEL_PROVIDER_MAX_CHARS == 128
    assert MODEL_NAME_MAX_CHARS == DATABASE_MODEL_NAME_MAX_CHARS == 255
    assert MODEL_DESCRIPTOR_ID_MAX_CHARS == 512
    assert MODEL_LIMIT_MAX_TOKENS == 100_000_000
    assert MODEL_DESCRIPTOR_MAX_JSON_BYTES == 16_384


def test_model_availability_is_a_closed_string_enum() -> None:
    assert tuple(item.value for item in ModelAvailability) == (
        "unknown",
        "available",
        "degraded",
        "unavailable",
    )


def test_capability_is_frozen_explicit_and_canonical() -> None:
    capability = _capabilities()

    assert capability.enabled == ("text", "tools", "json_schema", "streaming")
    assert capability.as_dict() == {
        "text": True,
        "vision": False,
        "tools": True,
        "json_schema": True,
        "reasoning": False,
        "streaming": True,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", capability.digest)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        capability.tools = False  # type: ignore[misc]


def test_capability_all_false_is_an_explicit_known_empty_set() -> None:
    capability = ModelCapability(
        text=False,
        vision=False,
        tools=False,
        json_schema=False,
        reasoning=False,
        streaming=False,
    )

    assert capability.enabled == ()
    assert set(capability.as_dict().values()) == {False}


@pytest.mark.parametrize(
    "field_name",
    ["text", "vision", "tools", "json_schema", "reasoning", "streaming"],
)
@pytest.mark.parametrize("value", [0, 1, "true", None, object()])
def test_capability_rejects_non_boolean_values(field_name: str, value: object) -> None:
    values: dict[str, object] = dict(_capabilities().as_dict())
    values[field_name] = value

    with pytest.raises(TypeError, match=field_name):
        ModelCapability(**values)  # type: ignore[arg-type]


def test_capability_digest_is_stable_and_changes_for_every_feature() -> None:
    baseline = _capabilities()
    assert _capabilities().digest == baseline.digest

    for field_name in baseline.as_dict():
        changed = _capabilities(**{field_name: not getattr(baseline, field_name)})
        assert changed.digest != baseline.digest


def test_limits_accept_safe_boundaries_and_are_frozen() -> None:
    smallest = ModelLimits(context_window=1, max_output=1)
    largest = ModelLimits(
        context_window=MODEL_LIMIT_MAX_TOKENS,
        max_output=MODEL_LIMIT_MAX_TOKENS,
    )

    assert smallest.as_dict() == {"context_window": 1, "max_output": 1}
    assert largest.context_window == MODEL_LIMIT_MAX_TOKENS
    assert re.fullmatch(r"[0-9a-f]{64}", largest.digest)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        smallest.max_output = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("context_window", "max_output", "message"),
    [
        (0, 1, "context_window"),
        (-1, 1, "context_window"),
        (True, 1, "context_window"),
        ("128000", 1, "context_window"),
        (MODEL_LIMIT_MAX_TOKENS + 1, 1, "context_window"),
        (10, 0, "max_output"),
        (10, -1, "max_output"),
        (10, False, "max_output"),
        (10, 11, "不能超过"),
    ],
)
def test_limits_reject_invalid_or_unbounded_values(
    context_window: object,
    max_output: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ModelLimits(
            context_window=context_window,  # type: ignore[arg-type]
            max_output=max_output,  # type: ignore[arg-type]
        )


def test_cost_normalizes_exact_decimals_without_binary_float() -> None:
    cost = ModelCost(
        input_per_million=Decimal("1.230000000000"),
        output_per_million=Decimal("1E+2"),
    )

    assert cost.input_per_million == Decimal("1.23")
    assert cost.output_per_million == Decimal("1E+2")
    assert cost.as_dict() == {
        "input_per_million": "1.23",
        "output_per_million": "100",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", cost.digest)


def test_cost_canonicalizes_positive_and_negative_zero() -> None:
    cost = ModelCost(
        input_per_million=Decimal("-0"),
        output_per_million=Decimal("0.000000000000"),
    )

    assert cost.input_per_million == Decimal(0)
    assert cost.output_per_million == Decimal(0)
    assert cost.as_dict() == {
        "input_per_million": "0",
        "output_per_million": "0",
    }


def test_cost_accepts_numeric_schema_maximum() -> None:
    maximum = Decimal("999999999999.999999999999")
    cost = ModelCost(input_per_million=maximum, output_per_million=maximum)

    assert cost.as_dict() == {
        "input_per_million": str(maximum),
        "output_per_million": str(maximum),
    }


@pytest.mark.parametrize(
    "value",
    [
        0,
        1.5,
        "1.5",
        None,
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("-0.01"),
        Decimal("1000000000000"),
        Decimal("0.0000000000001"),
    ],
)
@pytest.mark.parametrize("field_name", ["input_per_million", "output_per_million"])
def test_cost_rejects_inexact_unsafe_or_out_of_schema_values(
    field_name: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "input_per_million": Decimal("1"),
        "output_per_million": Decimal("2"),
    }
    values[field_name] = value

    with pytest.raises(ValueError, match=field_name):
        ModelCost(**values)  # type: ignore[arg-type]


def test_descriptor_is_generation_bound_and_contains_no_transport_or_secret_fields() -> None:
    descriptor = _descriptor()
    payload = descriptor.as_dict()

    assert payload == {
        "availability": "available",
        "capabilities": _capabilities().as_dict(),
        "cost": {"input_per_million": "2", "output_per_million": "8"},
        "descriptor_id": "gpt-4.1 (openai)",
        "generation": 7,
        "limits": {"context_window": 1_000_000, "max_output": 32_768},
        "model": "gpt-4.1",
        "provider": "openai",
        "schema_version": 1,
    }
    assert not ({"api_key", "credential", "endpoint", "headers", "key", "proxy", "secret", "url"} & payload.keys())
    assert json.loads(descriptor.to_json()) == payload
    assert len(descriptor.to_json().encode("ascii")) <= MODEL_DESCRIPTOR_MAX_JSON_BYTES


def test_descriptor_serialization_is_canonical_and_detached() -> None:
    descriptor = _descriptor()
    first = descriptor.as_dict()
    first["capabilities"]["tools"] = False
    first["limits"]["max_output"] = 1
    first["cost"]["input_per_million"] = "999"  # type: ignore[index]

    assert descriptor.as_dict()["capabilities"]["tools"] is True
    assert descriptor.as_dict()["limits"]["max_output"] == 32_768
    assert descriptor.as_dict()["cost"]["input_per_million"] == "2"  # type: ignore[index]
    assert descriptor.to_json() == json.dumps(
        descriptor.as_dict(),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_descriptor_unknown_cost_remains_distinct_from_free_cost() -> None:
    unknown = _descriptor(cost=None)
    free = _descriptor(
        cost=ModelCost(
            input_per_million=Decimal(0),
            output_per_million=Decimal(0),
        )
    )

    assert unknown.as_dict()["cost"] is None
    assert free.as_dict()["cost"] == {
        "input_per_million": "0",
        "output_per_million": "0",
    }
    assert unknown.descriptor_digest != free.descriptor_digest


def test_descriptor_has_separate_stable_identity_capability_and_full_digests() -> None:
    baseline = _descriptor()
    changed_generation = replace(baseline, generation=8)
    changed_availability = replace(baseline, availability=ModelAvailability.DEGRADED)
    changed_cost = replace(
        baseline,
        cost=ModelCost(
            input_per_million=Decimal("3"),
            output_per_million=Decimal("9"),
        ),
    )
    changed_limits = replace(
        baseline,
        limits=ModelLimits(context_window=2_000_000, max_output=32_768),
    )

    for changed in (
        changed_generation,
        changed_availability,
        changed_cost,
        changed_limits,
    ):
        assert changed.identity_digest == baseline.identity_digest
        assert changed.descriptor_digest != baseline.descriptor_digest
    assert changed_generation.capability_digest == baseline.capability_digest
    assert changed_availability.capability_digest == baseline.capability_digest
    assert changed_cost.capability_digest == baseline.capability_digest
    assert changed_limits.capability_digest != baseline.capability_digest
    for digest in (
        baseline.identity_digest,
        baseline.capability_digest,
        baseline.descriptor_digest,
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_descriptor_identity_digest_changes_for_each_identity_field() -> None:
    baseline = _descriptor()

    assert replace(baseline, descriptor_id="chat-primary").identity_digest != baseline.identity_digest
    assert replace(baseline, provider="azure-openai").identity_digest != baseline.identity_digest
    assert replace(baseline, model="gpt-4.1-mini").identity_digest != baseline.identity_digest


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("descriptor_id", ""),
        ("descriptor_id", " leading"),
        ("descriptor_id", "bad\x00id"),
        ("descriptor_id", "x" * (MODEL_DESCRIPTOR_ID_MAX_CHARS + 1)),
        ("provider", ""),
        ("provider", "openai\nother"),
        ("provider", "x" * (MODEL_PROVIDER_MAX_CHARS + 1)),
        ("model", ""),
        ("model", "gpt-4.1 "),
        ("model", "x" * (MODEL_NAME_MAX_CHARS + 1)),
        ("model", "\ud800"),
    ],
)
def test_descriptor_rejects_unsafe_or_unbounded_identity(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        _descriptor(**{field_name: value})


@pytest.mark.parametrize(
    "generation",
    [-1, True, 1.5, "7", (1 << 63)],
)
def test_descriptor_rejects_invalid_generation(generation: object) -> None:
    with pytest.raises(ValueError, match="generation"):
        _descriptor(generation=generation)


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("capabilities", {}, "ModelCapability"),
        ("limits", {"context_window": 10}, "ModelLimits"),
        ("cost", Decimal("1"), "ModelCost"),
        ("availability", "available", "ModelAvailability"),
    ],
)
def test_descriptor_rejects_untyped_nested_records(
    field_name: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(TypeError, match=message):
        _descriptor(**{field_name: value})


def test_descriptor_repr_is_digest_only_for_raw_identity() -> None:
    descriptor = _descriptor(
        descriptor_id="private-catalog-alias",
        provider="private-provider",
        model="private-model",
    )
    rendered = repr(descriptor)

    assert "private-catalog-alias" not in rendered
    assert "private-provider" not in rendered
    assert "private-model" not in rendered
    assert descriptor.identity_digest in rendered
    assert descriptor.descriptor_digest in rendered


def test_descriptor_accepts_maximum_utf8_identity_within_json_budget() -> None:
    descriptor = _descriptor(
        descriptor_id="模" * MODEL_DESCRIPTOR_ID_MAX_CHARS,
        provider="供" * MODEL_PROVIDER_MAX_CHARS,
        model="型" * MODEL_NAME_MAX_CHARS,
    )

    assert len(descriptor.to_json().encode("ascii")) <= MODEL_DESCRIPTOR_MAX_JSON_BYTES


def test_domain_construction_performs_no_file_or_network_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected I/O")

    monkeypatch.setattr(builtins, "open", fail)
    monkeypatch.setattr(socket, "socket", fail)

    descriptor = _descriptor()

    assert descriptor.availability is ModelAvailability.AVAILABLE
    assert descriptor.cost is not None

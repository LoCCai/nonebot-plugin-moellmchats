from __future__ import annotations

from decimal import Decimal
import importlib
import inspect

import pytest

from nonebot_plugin_moellmchats.llm_payload import LlmPayloadMixin
from nonebot_plugin_moellmchats.model_capabilities import ModelCapability
from nonebot_plugin_moellmchats.model_routing import (
    ModelRouteRole,
    ModelRoutingMode,
    ModelSelectionReason,
)
from nonebot_plugin_moellmchats.model_routing_runtime import (
    ModelRoutingRuntime,
    ModelRoutingRuntimeConfigurationError,
    build_model_routing_runtime,
)
from nonebot_plugin_moellmchats.model_selector import ModelRuntimeState, ModelSelector
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot, runtime_snapshots

model_selector_module = importlib.import_module("nonebot_plugin_moellmchats.model_selector")


def _capabilities(
    *,
    vision: bool = False,
    tools: bool = False,
    json_schema: bool = False,
) -> ModelCapability:
    return ModelCapability(
        text=True,
        vision=vision,
        tools=tools,
        json_schema=json_schema,
        reasoning=False,
        streaming=False,
    )


def _routing_metadata(
    *,
    vision: bool = False,
    tools: bool = False,
    json_schema: bool = False,
    quality: int = 500,
    latency_ms: int = 500,
    input_cost: object = "1",
    output_cost: object = "2",
) -> dict[str, object]:
    return {
        "availability": "available",
        "capabilities": {
            "text": True,
            "vision": vision,
            "tools": tools,
            "json_schema": json_schema,
            "reasoning": False,
            "streaming": False,
        },
        "cost": {
            "input_per_million": input_cost,
            "output_per_million": output_cost,
        },
        "latency_ms": latency_ms,
        "limits": {"context_window": 32_768, "max_output": 4_096},
        "quality_score": quality,
    }


def _models() -> dict[str, dict[str, object]]:
    return {
        "fixed (provider-a)": {
            "provider": "provider-a",
            "model": "fixed",
            "url": "https://fixed.invalid/chat",
            "keys": ["fixed-secret"],
            "stream": True,
            "capability_routing": _routing_metadata(quality=100, input_cost="3", output_cost="6"),
        },
        "dynamic (provider-b)": {
            "provider": "provider-b",
            "model": "dynamic",
            "url": "https://dynamic.invalid/chat",
            "keys": ["dynamic-secret"],
            "stream": True,
            "capability_routing": _routing_metadata(
                tools=True,
                json_schema=True,
                quality=900,
            ),
        },
        "vision (provider-b)": {
            "provider": "provider-b",
            "model": "vision",
            "url": "https://vision.invalid/chat",
            "keys": ["vision-secret"],
            "capability_routing": _routing_metadata(
                vision=True,
                tools=True,
                quality=800,
            ),
        },
    }


def _model_config(*, mode: str = "fixed_preferred") -> dict[str, object]:
    return {
        "selected_model": "fixed (provider-a)",
        "vision_model": "vision (provider-b)",
        "category_model": "dynamic (provider-b)",
        "summary_model": "fixed (provider-a)",
        "moe_models": {
            "0": "fixed (provider-a)",
            "1": "dynamic (provider-b)",
            "2": "dynamic (provider-b)",
        },
        "use_moe": False,
        "use_tools": True,
        "use_web_search": False,
        "resident_plugins": [],
        "capability_routing": {
            "enabled": True,
            "policy": {
                "allow_degraded": False,
                "mode": mode,
                "version": "runtime-v1",
            },
            "requirements": {
                "input_tokens": 2_048,
                "maximum_latency_ms": 10_000,
                "maximum_unit_cost": None,
                "minimum_context_window": 4_096,
                "minimum_quality": 0,
                "output_tokens": 1_024,
            },
        },
    }


def _snapshot(state: ModelRuntimeState) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=77,
        config={},
        model_state=state,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=77.0,
    )


def test_absent_or_explicitly_disabled_runtime_preserves_legacy_mode() -> None:
    assert (
        build_model_routing_runtime(
            generation=1,
            models=_models(),
            model_config={"selected_model": "fixed (provider-a)"},
        )
        is None
    )
    assert (
        build_model_routing_runtime(
            generation=1,
            models=_models(),
            model_config={"capability_routing": {"enabled": False}},
        )
        is None
    )


def test_trusted_runtime_routes_fixed_and_capability_requests_without_transport_data() -> None:
    runtime = build_model_routing_runtime(
        generation=9,
        models=_models(),
        model_config=_model_config(),
    )

    assert isinstance(runtime, ModelRoutingRuntime)
    fixed = runtime.select(
        ModelRouteRole.SELECTED,
        capabilities=_capabilities(),
    )
    tools = runtime.select(
        ModelRouteRole.SELECTED,
        capabilities=_capabilities(tools=True),
    )
    vision = runtime.select(
        ModelRouteRole.VISION,
        capabilities=_capabilities(vision=True, tools=True),
    )

    assert fixed.reason is ModelSelectionReason.FIXED
    assert fixed.selected.descriptor.descriptor_id == "fixed (provider-a)"
    assert tools.reason is ModelSelectionReason.CAPABILITY
    assert tools.selected.descriptor.descriptor_id == "dynamic (provider-b)"
    assert vision.selected.descriptor.descriptor_id == "vision (provider-b)"
    assert runtime.policy.mode is ModelRoutingMode.FIXED_PREFERRED
    diagnostics = runtime.safe_diagnostics()
    assert diagnostics["candidate_count"] == 3
    rendered = f"{runtime!r}{diagnostics!r}{runtime.catalog.to_json()}"
    for forbidden in ("fixed-secret", "dynamic-secret", "https://", "proxy", "Authorization"):
        assert forbidden not in rendered


@pytest.mark.parametrize("bad_cost", [1.5, True, " 1", "not-a-decimal"])
def test_runtime_rejects_inexact_or_invalid_cost_without_fallback(bad_cost: object) -> None:
    models = _models()
    dynamic = models["dynamic (provider-b)"]
    assert isinstance(dynamic, dict)
    metadata = dynamic["capability_routing"]
    assert isinstance(metadata, dict)
    cost = metadata["cost"]
    assert isinstance(cost, dict)
    cost["input_per_million"] = bad_cost

    with pytest.raises(ModelRoutingRuntimeConfigurationError):
        build_model_routing_runtime(
            generation=9,
            models=models,
            model_config=_model_config(),
        )


def test_selector_consumes_bound_trusted_catalog_and_returns_only_selected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ModelRuntimeState(
        models=_models(),
        providers={},
        global_default={},
        model_config=_model_config(),
    )
    selector = object.__new__(ModelSelector)
    selector.models = {}
    selector.providers = {}
    selector.global_default = {}
    selector.model_config = {}
    monkeypatch.setattr(model_selector_module, "model_selector", selector)

    with runtime_snapshots.bind(_snapshot(state)):
        selected = selector.get_model_for_capabilities(
            "selected_model",
            _capabilities(tools=True),
        )

    assert selected["provider"] == "provider-b"
    assert selected["model"] == "dynamic"
    assert selected["url"] == "https://dynamic.invalid/chat"
    assert selected["key"] == "Bearer dynamic-secret"
    assert selected["stream"] is False
    assert selected["_capability_routing"]["generation"] == 77
    assert selected["_capability_routing"]["capabilities"]["tools"] is True
    assert "fixed-secret" not in repr(selected)
    assert "vision-secret" not in repr(selected)


def test_runtime_exact_decimal_budget_remains_decimal() -> None:
    config = _model_config()
    routing = config["capability_routing"]
    assert isinstance(routing, dict)
    requirements = routing["requirements"]
    assert isinstance(requirements, dict)
    requirements["maximum_unit_cost"] = {
        "input_per_million": "1.000000000001",
        "output_per_million": "2.000000000001",
    }
    runtime = build_model_routing_runtime(
        generation=9,
        models=_models(),
        model_config=config,
    )
    assert runtime is not None
    assert runtime.maximum_unit_cost is not None
    assert runtime.maximum_unit_cost.input_per_million == Decimal("1.000000000001")


@pytest.mark.asyncio
async def test_real_payload_preparation_consumes_capability_route_without_network() -> None:
    config = _model_config(mode="capability_only")
    config["use_tools"] = False
    state = ModelRuntimeState(
        models=_models(),
        providers={},
        global_default={},
        model_config=config,
    )

    class Messages:
        def __init__(self) -> None:
            self.current_images = ["https://image.invalid/input"]

    class Harness(LlmPayloadMixin):
        def __init__(self) -> None:
            self.messages_handler = Messages()
            self.model_info: dict[str, object] = {}
            self.required_plugins: list[str] = []
            self.tool_snapshot = None
            self.is_superuser = False

    with runtime_snapshots.bind(_snapshot(state)):
        harness = Harness()
        assert await harness._prepare_model_info("[图片]") is None

    assert harness.model_info["model"] == "vision"
    routing = harness.model_info["_capability_routing"]
    assert isinstance(routing, dict)
    capabilities = routing["capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities["vision"] is True
    assert capabilities["text"] is True
    assert harness.model_info["key"] == "Bearer vision-secret"


def test_module_has_no_import_time_runtime_or_awaitable() -> None:
    module = importlib.reload(importlib.import_module("nonebot_plugin_moellmchats.model_routing_runtime"))

    assert not any(isinstance(value, module.ModelRoutingRuntime) for value in vars(module).values())
    assert not any(inspect.isawaitable(value) for value in vars(module).values())

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import FrozenInstanceError
import importlib
import json
from typing import Any

import pytest

from nonebot_plugin_moellmchats.metrics_api import (
    METRICS_API_READ_SCOPE,
    MODEL_API_READ_SCOPE,
    MetricsApiEndpoint,
    MetricsApiService,
    RuntimeMetricsReader,
)
from nonebot_plugin_moellmchats.model_selector import ModelRuntimeState
from nonebot_plugin_moellmchats.runtime_api import (
    RuntimeApiASGIApp,
    RuntimeApiConfigurationError,
    RuntimeApiCredential,
    RuntimeApiPrincipal,
    RuntimeApiRequest,
    RuntimeApiResponse,
    StaticBearerRuntimeApiAuthenticator,
)
from nonebot_plugin_moellmchats.runtime_metrics import RuntimeMetrics
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot

_TOKEN = "m" * 32
_WRONG_TOKEN = "w" * 32
_MAX_BIGINT = (1 << 63) - 1
_UNSET = object()

_INTEGER_FIELDS = (
    "llm_active",
    "llm_pending",
    "llm_rejected",
    "dispatch_active",
    "dispatch_pending",
    "dispatch_rejected",
    "dispatch_timeouts",
    "member_cache_hits",
    "member_cache_misses",
    "member_lookup_timeouts",
    "tool_steps",
    "tool_timeouts",
    "generated_runner_active",
    "generated_runner_pending",
    "generated_runner_rejected",
    "generated_runner_timeouts",
    "generated_runner_killed",
    "generated_runner_orphan_cleanups",
    "generated_runner_failures",
    "generated_authoring_active",
    "classification_count",
    "reload_generation",
    "reload_successes",
    "reload_failures",
)


def _models(count: int = 3) -> dict[str, dict[str, Any]]:
    records = {
        "zeta (provider-b)": {
            "model": "zeta",
            "provider": "provider-b",
            "key": ["secret-zeta"],
            "url": "https://secret.invalid",
        },
        "beta (provider-a)": {
            "model": "beta",
            "provider": "provider-a",
            "api_key": "secret-beta",
        },
        "alpha (provider-a)": {
            "model": "alpha",
            "provider": "provider-a",
            "proxy": "http://secret-proxy.invalid",
        },
    }
    if count <= len(records):
        return dict(tuple(records.items())[:count])
    return {
        f"model-{index:04d} (provider-{index % 3})": {
            "model": f"model-{index:04d}",
            "provider": f"provider-{index % 3}",
            "key": [f"secret-{index}"],
        }
        for index in range(count)
    }


def _model_state(models: object = _UNSET) -> ModelRuntimeState:
    selected = _models() if models is _UNSET else models
    return ModelRuntimeState(
        models=selected,  # type: ignore[arg-type]
        providers={
            "provider-a": {
                "api_key": "provider-secret",
                "base_url": "https://provider-secret.invalid",
            }
        },
        global_default={"secret": "global-default-secret"},
        model_config={"secret": "model-config-secret"},
    )


def _snapshot(
    *,
    generation: int = 7,
    model_state: object = _UNSET,
) -> RuntimeSnapshot:
    state = _model_state() if model_state is _UNSET else model_state
    return RuntimeSnapshot(
        generation=generation,
        config={"secret": "runtime-config-secret"},
        model_state=state,  # type: ignore[arg-type]
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=None,
        emotions=(),
        reloaded_at=20.5,
    )


def _metrics(*, generation: int = 7, **overrides: object) -> dict[str, Any]:
    values: dict[str, object] = {
        "started_at": 10.0,
        "llm_active": 1,
        "llm_pending": 2,
        "llm_rejected": 3,
        "dispatch_active": 4,
        "dispatch_pending": 5,
        "dispatch_rejected": 6,
        "dispatch_timeouts": 7,
        "member_cache_hits": 8,
        "member_cache_misses": 9,
        "member_lookup_timeouts": 10,
        "tool_steps": 11,
        "tool_timeouts": 12,
        "generated_runner_active": 13,
        "generated_runner_pending": 14,
        "generated_runner_rejected": 15,
        "generated_runner_timeouts": 16,
        "generated_runner_killed": 17,
        "generated_runner_orphan_cleanups": 18,
        "generated_runner_failures": 19,
        "generated_authoring_active": 20,
        "classification_count": 4,
        "classification_seconds": 2.0,
        "reload_generation": generation,
        "reload_successes": 21,
        "reload_failures": 22,
        "last_reload_at": 19.5,
        "last_reload_error": "secret traceback must not leak",
        "dispatch_modes": Counter({"targeted": 24, "full": 23}),
    }
    values.update(overrides)
    runtime = RuntimeMetrics(**values)  # type: ignore[arg-type]
    return runtime.snapshot()


def _credential(token: str = _TOKEN) -> RuntimeApiCredential:
    value = RuntimeApiCredential.from_authorization_header(f"Bearer {token}".encode("ascii"))
    assert value is not None
    return value


def _principal(*scopes: str) -> RuntimeApiPrincipal:
    return RuntimeApiPrincipal(
        subject="metrics-admin",
        scopes=frozenset(scopes),
    )


class _Snapshots:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0
        self.error: BaseException | None = None

    def current(self) -> RuntimeSnapshot | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]


class _Metrics:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls = 0
        self.error: BaseException | None = None

    def snapshot(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]


class _FixedAuthenticator:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0
        self.error: BaseException | None = None

    async def authenticate(
        self,
        request: RuntimeApiRequest,
    ) -> RuntimeApiPrincipal | None:
        assert isinstance(request, RuntimeApiRequest)
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


def _service(
    *,
    snapshot: object = _UNSET,
    metrics: object = _UNSET,
    principal: RuntimeApiPrincipal | None = None,
    authenticator: object | None = None,
) -> tuple[MetricsApiService, _Snapshots, _Metrics]:
    snapshots = _Snapshots(_snapshot() if snapshot is _UNSET else snapshot)
    metric_reader = _Metrics(_metrics() if metrics is _UNSET else metrics)
    auth = (
        StaticBearerRuntimeApiAuthenticator(
            token=_TOKEN,
            principal=principal
            or _principal(
                METRICS_API_READ_SCOPE,
                MODEL_API_READ_SCOPE,
            ),
        )
        if authenticator is None
        else authenticator
    )
    return (
        MetricsApiService(
            snapshots=snapshots,
            metrics=metric_reader,
            authenticator=auth,  # type: ignore[arg-type]
        ),
        snapshots,
        metric_reader,
    )


def _request(
    path: str = "/metrics",
    *,
    method: str = "GET",
    token: str | None = _TOKEN,
    query_string: bytes = b"",
    content_type: str | None = None,
    body: bytes = b"",
) -> RuntimeApiRequest:
    return RuntimeApiRequest(
        method=method,
        path=path,
        query_string=query_string,
        credential=None if token is None else _credential(token),
        content_type=content_type,
        body=body,
    )


def _payload(response: RuntimeApiResponse) -> dict[str, Any]:
    return json.loads(response.body)


async def _call_asgi(
    app: RuntimeApiASGIApp,
    *,
    path: str,
    query_string: bytes = b"",
    token: str = _TOKEN,
) -> tuple[int, dict[bytes, bytes], dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        assert not received
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": query_string,
            "headers": [
                (b"authorization", f"Bearer {token}".encode("ascii")),
            ],
        },
        receive,
        send,
    )
    assert len(sent) == 2
    start, body = sent
    assert start["type"] == "http.response.start"
    assert body["type"] == "http.response.body"
    return (
        start["status"],
        dict(start["headers"]),
        json.loads(body["body"]),
    )


def test_endpoint_contract_is_frozen_and_scope_specific() -> None:
    metrics = MetricsApiEndpoint("GET", "/metrics", METRICS_API_READ_SCOPE)
    models = MetricsApiEndpoint("GET", "/models", MODEL_API_READ_SCOPE)
    assert metrics.required_scope != models.required_scope
    with pytest.raises(FrozenInstanceError):
        metrics.path = "/models"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("POST", "/metrics", METRICS_API_READ_SCOPE),
        ("GET", "/unknown", METRICS_API_READ_SCOPE),
        ("GET", "/metrics", MODEL_API_READ_SCOPE),
        ("GET", "/models", METRICS_API_READ_SCOPE),
    ],
)
def test_endpoint_rejects_invalid_contract(
    method: str,
    path: str,
    scope: str,
) -> None:
    with pytest.raises(RuntimeApiConfigurationError):
        MetricsApiEndpoint(method, path, scope)


def test_service_exposes_only_h04_endpoints() -> None:
    service, _, _ = _service()
    assert tuple((item.method, item.path, item.required_scope) for item in service.endpoints) == (
        ("GET", "/metrics", METRICS_API_READ_SCOPE),
        ("GET", "/models", MODEL_API_READ_SCOPE),
    )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("snapshots", object()),
        ("metrics", object()),
        ("authenticator", object()),
    ],
)
def test_service_rejects_missing_protocols(argument: str, value: object) -> None:
    values: dict[str, object] = {
        "snapshots": _Snapshots(_snapshot()),
        "metrics": _Metrics(_metrics()),
        "authenticator": _FixedAuthenticator(_principal(METRICS_API_READ_SCOPE)),
    }
    values[argument] = value
    with pytest.raises(RuntimeApiConfigurationError):
        MetricsApiService(**values)  # type: ignore[arg-type]


def test_existing_runtime_metrics_satisfies_explicit_reader_protocol() -> None:
    assert isinstance(RuntimeMetrics(), RuntimeMetricsReader)


@pytest.mark.asyncio
async def test_invalid_request_is_rejected_without_authentication() -> None:
    auth = _FixedAuthenticator(_principal(METRICS_API_READ_SCOPE))
    service, snapshots, metrics = _service(authenticator=auth)
    response = await service.handle(object())  # type: ignore[arg-type]
    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_request"
    assert auth.calls == snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_missing_and_wrong_credentials_are_indistinguishable() -> None:
    service, snapshots, metrics = _service()
    missing = await service.handle(_request(token=None))
    wrong = await service.handle(_request(token=_WRONG_TOKEN))
    assert missing.status_code == wrong.status_code == 401
    assert missing.body == wrong.body
    assert dict(missing.extra_headers) == {b"www-authenticate": b'Bearer realm="moellm-runtime"'}
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_authenticator_failure_is_fixed_and_precedes_readers() -> None:
    auth = _FixedAuthenticator(_principal(METRICS_API_READ_SCOPE))
    auth.error = RuntimeError("secret auth backend")
    service, snapshots, metrics = _service(authenticator=auth)
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "authentication_unavailable"
    assert snapshots.calls == metrics.calls == 0
    assert b"secret" not in response.body


@pytest.mark.asyncio
async def test_invalid_authenticator_result_fails_closed() -> None:
    service, snapshots, metrics = _service(authenticator=_FixedAuthenticator(object()))
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "authentication_unavailable"
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_authenticator_cancellation_propagates() -> None:
    auth = _FixedAuthenticator(_principal(METRICS_API_READ_SCOPE))
    auth.error = asyncio.CancelledError()
    service, snapshots, metrics = _service(authenticator=auth)
    with pytest.raises(asyncio.CancelledError):
        await service.handle(_request())
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_unknown_path_is_hidden_until_after_authentication() -> None:
    auth = _FixedAuthenticator(_principal(METRICS_API_READ_SCOPE))
    service, snapshots, metrics = _service(authenticator=auth)
    response = await service.handle(_request("/runtime/status"))
    assert response.status_code == 404
    assert auth.calls == 1
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("/metrics", MODEL_API_READ_SCOPE),
        ("/models", METRICS_API_READ_SCOPE),
    ],
)
async def test_endpoints_require_distinct_read_scopes(path: str, scope: str) -> None:
    service, snapshots, metrics = _service(principal=_principal(scope))
    response = await service.handle(_request(path))
    assert response.status_code == 403
    assert _payload(response)["error"] == "forbidden"
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/metrics", "/models"])
async def test_method_validation_precedes_readers(path: str) -> None:
    service, snapshots, metrics = _service()
    response = await service.handle(_request(path, method="POST"))
    assert response.status_code == 405
    assert dict(response.extra_headers) == {b"allow": b"GET"}
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/metrics", "/models"])
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b""),
        (None, b"{}"),
        ("application/json", b"{}"),
    ],
)
async def test_body_is_rejected_before_readers(
    path: str,
    content_type: str | None,
    body: bytes,
) -> None:
    service, snapshots, metrics = _service()
    response = await service.handle(_request(path, content_type=content_type, body=body))
    assert response.status_code == 400
    assert _payload(response)["error"] == "body_not_supported"
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_metrics_rejects_any_query_before_readers() -> None:
    service, snapshots, metrics = _service()
    response = await service.handle(_request(query_string=b"limit=1"))
    assert response.status_code == 400
    assert _payload(response)["error"] == "query_not_supported"
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        b"limit=0",
        b"limit=21",
        b"limit=01",
        b"limit=1&limit=2",
        b"offset=1",
        b"cursor=",
        b"cursor=***",
        b"limit=1&cursor=bad&extra=1",
        "limit=一".encode(),
    ],
)
async def test_models_rejects_malformed_query_before_readers(query: bytes) -> None:
    service, snapshots, metrics = _service()
    response = await service.handle(_request("/models", query_string=query))
    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_query"
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_metrics_returns_only_bounded_low_cardinality_aggregates() -> None:
    raw = _metrics(last_reload_error="database password=must-not-leak")
    raw["unexpected_secret"] = "also-must-not-leak"
    service, snapshots, metrics = _service(metrics=raw)
    response = await service.handle(_request())
    assert response.status_code == 200
    payload = _payload(response)
    assert payload == {
        "api_version": 1,
        "classification": {
            "average_seconds": 0.5,
            "count": 4,
            "total_seconds": 2.0,
        },
        "dispatch": {
            "active": 4,
            "modes": {"full": 23, "targeted": 24},
            "pending": 5,
            "rejected": 6,
            "timeouts": 7,
        },
        "generated": {
            "authoring_active": 20,
            "runner": {
                "active": 13,
                "failures": 19,
                "killed": 17,
                "orphan_cleanups": 18,
                "pending": 14,
                "rejected": 15,
                "timeouts": 16,
            },
        },
        "generation": 7,
        "llm": {"active": 1, "pending": 2, "rejected": 3},
        "member_cache": {"hits": 8, "lookup_timeouts": 10, "misses": 9},
        "reload": {"failures": 22, "last_at": 19.5, "successes": 21},
        "started_at": 10.0,
        "tools": {"steps": 11, "timeouts": 12},
    }
    assert b"password" not in response.body
    assert b"unexpected_secret" not in response.body
    assert snapshots.calls == metrics.calls == 1


@pytest.mark.asyncio
async def test_metrics_zero_classification_count_has_zero_average() -> None:
    service, _, _ = _service(metrics=_metrics(classification_count=0, classification_seconds=0.0))
    response = await service.handle(_request())
    assert response.status_code == 200
    assert _payload(response)["classification"]["average_seconds"] == 0.0


@pytest.mark.asyncio
async def test_metrics_accepts_null_last_reload_time() -> None:
    service, _, _ = _service(metrics=_metrics(last_reload_at=None))
    response = await service.handle(_request())
    assert response.status_code == 200
    assert _payload(response)["reload"]["last_at"] is None


@pytest.mark.asyncio
async def test_metrics_generation_mismatch_fails_closed() -> None:
    service, snapshots, metrics = _service(metrics=_metrics(generation=8))
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "metrics_unavailable"
    assert snapshots.calls == metrics.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", _INTEGER_FIELDS)
async def test_each_integer_metric_rejects_negative_values(field: str) -> None:
    raw = _metrics()
    raw[field] = -1
    service, _, _ = _service(metrics=raw)
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "metrics_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, 1.0, "1", _MAX_BIGINT + 1])
async def test_integer_metric_rejects_noncanonical_values(value: object) -> None:
    raw = _metrics()
    raw["llm_active"] = value
    service, _, _ = _service(metrics=raw)
    response = await service.handle(_request())
    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["started_at", "classification_seconds", "last_reload_at"])
@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
async def test_float_metrics_reject_unsafe_values(field: str, value: object) -> None:
    raw = _metrics()
    raw[field] = value
    service, _, _ = _service(metrics=raw)
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "metrics_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "modes",
    [
        [],
        {"UPPER": 1},
        {"bad mode": 1},
        {"targeted": -1},
        {"targeted": True},
        {f"mode{index}": index for index in range(17)},
    ],
)
async def test_dispatch_modes_are_bounded_and_canonical(modes: object) -> None:
    raw = _metrics()
    raw["dispatch_modes"] = modes
    service, _, _ = _service(metrics=raw)
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "metrics_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, object(), [], "metrics"])
async def test_metrics_reader_result_must_be_mapping(value: object) -> None:
    service, _, _ = _service(metrics=value)
    response = await service.handle(_request())
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_metrics_reader_failure_is_redacted() -> None:
    service, snapshots, metrics = _service()
    metrics.error = RuntimeError("postgresql://secret")
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "metrics_unavailable"
    assert b"postgresql" not in response.body
    assert snapshots.calls == metrics.calls == 1


@pytest.mark.asyncio
async def test_metrics_reader_cancellation_propagates() -> None:
    service, snapshots, metrics = _service()
    metrics.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.handle(_request())
    assert snapshots.calls == metrics.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, object()])
async def test_runtime_snapshot_unavailable_precedes_metrics_reader(
    snapshot: object,
) -> None:
    service, snapshots, metrics = _service(snapshot=snapshot)
    response = await service.handle(_request())
    assert response.status_code == 503
    assert _payload(response)["error"] == "runtime_snapshot_unavailable"
    assert snapshots.calls == 1
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_runtime_snapshot_failure_is_redacted() -> None:
    service, snapshots, metrics = _service()
    snapshots.error = RuntimeError("snapshot secret")
    response = await service.handle(_request())
    assert response.status_code == 503
    assert b"secret" not in response.body
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_runtime_snapshot_cancellation_propagates() -> None:
    service, snapshots, metrics = _service()
    snapshots.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.handle(_request())
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_models_are_stably_sorted_and_data_minimized() -> None:
    service, snapshots, metrics = _service()
    response = await service.handle(_request("/models"))
    assert response.status_code == 200
    payload = _payload(response)
    assert payload == {
        "api_version": 1,
        "generation": 7,
        "items": [
            {"id": "alpha (provider-a)", "model": "alpha", "provider": "provider-a"},
            {"id": "beta (provider-a)", "model": "beta", "provider": "provider-a"},
            {"id": "zeta (provider-b)", "model": "zeta", "provider": "provider-b"},
        ],
        "next_cursor": None,
        "total_count": 3,
    }
    for secret in (
        b"secret-zeta",
        b"secret-beta",
        b"secret-proxy",
        b"provider-secret",
        b"runtime-config-secret",
    ):
        assert secret not in response.body
    assert snapshots.calls == 1
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_models_support_generation_bound_canonical_pagination() -> None:
    service, snapshots, metrics = _service(snapshot=_snapshot(model_state=_model_state(_models(45))))
    first = await service.handle(_request("/models", query_string=b"limit=20"))
    assert first.status_code == 200
    first_payload = _payload(first)
    assert len(first_payload["items"]) == 20
    assert isinstance(first_payload["next_cursor"], str)

    second_query = f"limit=20&cursor={first_payload['next_cursor']}".encode("ascii")
    second = await service.handle(_request("/models", query_string=second_query))
    assert second.status_code == 200
    second_payload = _payload(second)
    assert len(second_payload["items"]) == 20
    assert isinstance(second_payload["next_cursor"], str)

    third_query = f"cursor={second_payload['next_cursor']}&limit=20".encode("ascii")
    third = await service.handle(_request("/models", query_string=third_query))
    assert third.status_code == 200
    third_payload = _payload(third)
    assert len(third_payload["items"]) == 5
    assert third_payload["next_cursor"] is None

    identities = [item["id"] for payload in (first_payload, second_payload, third_payload) for item in payload["items"]]
    assert len(identities) == len(set(identities)) == 45
    assert snapshots.calls == 3
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_models_cursor_rejects_generation_change() -> None:
    models = _model_state(_models(21))
    service, snapshots, metrics = _service(snapshot=_snapshot(model_state=models))
    first = await service.handle(_request("/models", query_string=b"limit=20"))
    cursor = _payload(first)["next_cursor"]
    assert isinstance(cursor, str)
    snapshots.value = _snapshot(generation=8, model_state=models)
    response = await service.handle(_request("/models", query_string=f"cursor={cursor}".encode("ascii")))
    assert response.status_code == 409
    assert _payload(response)["error"] == "cursor_precondition_failed"
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_models_cursor_rejects_missing_anchor_in_same_generation() -> None:
    service, snapshots, metrics = _service(snapshot=_snapshot(model_state=_model_state(_models(21))))
    first = await service.handle(_request("/models", query_string=b"limit=20"))
    cursor = _payload(first)["next_cursor"]
    assert isinstance(cursor, str)
    snapshots.value = _snapshot(model_state=_model_state(_models(3)))
    response = await service.handle(_request("/models", query_string=f"cursor={cursor}".encode("ascii")))
    assert response.status_code == 409
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_models_rejects_noncanonical_cursor_before_reader() -> None:
    service, snapshots, metrics = _service(snapshot=_snapshot(model_state=_model_state(_models(21))))
    first = await service.handle(_request("/models", query_string=b"limit=20"))
    cursor = _payload(first)["next_cursor"]
    assert isinstance(cursor, str)
    snapshots.calls = 0
    tampered = f"cursor={cursor}A".encode("ascii")
    response = await service.handle(_request("/models", query_string=tampered))
    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_query"
    assert snapshots.calls == metrics.calls == 0


@pytest.mark.asyncio
async def test_empty_model_catalog_is_a_valid_page() -> None:
    service, _, metrics = _service(snapshot=_snapshot(model_state=_model_state({})))
    response = await service.handle(_request("/models"))
    assert response.status_code == 200
    assert _payload(response)["items"] == []
    assert _payload(response)["total_count"] == 0
    assert metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, object()])
async def test_missing_or_invalid_model_state_fails_closed(state: object) -> None:
    if state is None:
        snapshot: object = _snapshot(model_state=None)
    else:
        snapshot = object()
    service, _, metrics = _service(snapshot=snapshot)
    response = await service.handle(_request("/models"))
    expected = "model_catalog_unavailable" if state is None else "runtime_snapshot_unavailable"
    assert response.status_code == 503
    assert _payload(response)["error"] == expected
    assert metrics.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "models",
    [
        {"identity": "not-a-mapping"},
        {"identity": {"model": "alpha"}},
        {"identity": {"provider": "provider-a"}},
        {" identity": {"model": "alpha", "provider": "provider-a"}},
        {"identity": {"model": " alpha", "provider": "provider-a"}},
        {"identity": {"model": "alpha\x00", "provider": "provider-a"}},
        {"identity": {"model": "alpha", "provider": "provider\nsecret"}},
        {"identity": {"model": "m" * 256, "provider": "provider-a"}},
        {"identity": {"model": "alpha", "provider": "p" * 129}},
        {"i" * 200: {"model": "m" * 200, "provider": "p" * 120}},
        {"\\" * 200: {"model": "\\" * 200, "provider": "\\" * 100}},
    ],
)
async def test_model_catalog_rejects_unsafe_records(models: object) -> None:
    service, _, metrics = _service(snapshot=_snapshot(model_state=_model_state(models)))
    response = await service.handle(_request("/models"))
    assert response.status_code == 503
    assert _payload(response)["error"] == "model_catalog_unavailable"
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_model_catalog_rejects_unbounded_record_count() -> None:
    service, _, metrics = _service(snapshot=_snapshot(model_state=_model_state(_models(4_097))))
    response = await service.handle(_request("/models"))
    assert response.status_code == 503
    assert _payload(response)["error"] == "model_catalog_unavailable"
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_unicode_model_cursor_round_trips_canonical_utf8() -> None:
    models = {
        f"模型-{index:02d} (供应商)": {
            "model": f"模型-{index:02d}",
            "provider": "供应商",
        }
        for index in range(21)
    }
    service, snapshots, metrics = _service(snapshot=_snapshot(model_state=_model_state(models)))
    first = await service.handle(_request("/models", query_string=b"limit=20"))
    cursor = _payload(first)["next_cursor"]
    assert isinstance(cursor, str)
    second = await service.handle(_request("/models", query_string=f"cursor={cursor}".encode("ascii")))
    assert second.status_code == 200
    assert len(_payload(second)["items"]) == 1
    assert snapshots.calls == 2
    assert metrics.calls == 0


@pytest.mark.asyncio
async def test_asgi_adapter_serves_metrics_with_security_headers() -> None:
    service, _, _ = _service()
    status, headers, payload = await _call_asgi(
        RuntimeApiASGIApp(service=service),
        path="/metrics",
    )
    assert status == 200
    assert payload["generation"] == 7
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"content-type"] == b"application/json; charset=utf-8"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert int(headers[b"content-length"]) > 0
    assert b"access-control-allow-origin" not in headers


@pytest.mark.asyncio
async def test_asgi_adapter_serves_model_pagination() -> None:
    service, _, _ = _service(snapshot=_snapshot(model_state=_model_state(_models(21))))
    status, _, payload = await _call_asgi(
        RuntimeApiASGIApp(service=service),
        path="/models",
        query_string=b"limit=1",
    )
    assert status == 200
    assert len(payload["items"]) == 1
    assert isinstance(payload["next_cursor"], str)


@pytest.mark.asyncio
async def test_asgi_wrong_token_does_not_touch_readers() -> None:
    service, snapshots, metrics = _service()
    status, headers, payload = await _call_asgi(
        RuntimeApiASGIApp(service=service),
        path="/metrics",
        token=_WRONG_TOKEN,
    )
    assert status == 401
    assert payload["error"] == "unauthorized"
    assert headers[b"www-authenticate"] == b'Bearer realm="moellm-runtime"'
    assert snapshots.calls == metrics.calls == 0


def test_module_defines_no_service_app_or_reader_instances() -> None:
    module = importlib.reload(importlib.import_module("nonebot_plugin_moellmchats.metrics_api"))
    assert not any(isinstance(value, module.MetricsApiService) for value in vars(module).values())
    assert not any(isinstance(value, RuntimeApiASGIApp) for value in vars(module).values())
    assert not any(isinstance(value, RuntimeMetrics) for value in vars(module).values())

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
import importlib
import json
from typing import Any

import pytest

from nonebot_plugin_moellmchats.generated_tool_lifecycle import (
    DraftEvidence,
    DraftRecord,
    DraftState,
    LifecycleState,
    VersionRecord,
    VersionState,
    draft_review_stamp,
)
from nonebot_plugin_moellmchats.runtime_api import (
    RuntimeApiASGIApp,
    RuntimeApiCredential,
    RuntimeApiPrincipal,
    RuntimeApiRequest,
    RuntimeApiResponse,
    StaticBearerRuntimeApiAuthenticator,
)
from nonebot_plugin_moellmchats.runtime_snapshot import RuntimeSnapshot
from nonebot_plugin_moellmchats.tool_bundle_api import (
    TOOL_BUNDLE_API_READ_SCOPE,
    TOOL_BUNDLE_API_WRITE_SCOPE,
    ActivateToolBundleCommand,
    ApproveToolDraftCommand,
    ToolBundleApiEndpoint,
    ToolBundleApiService,
    ToolBundleMutationConflictError,
    ToolBundleMutationNotFoundError,
    ToolBundleMutationResult,
    ToolBundleMutationResultUnknownError,
    ToolBundleMutationUnavailableError,
)
from nonebot_plugin_moellmchats.tool_contracts import ToolEffect, ToolSpec
from nonebot_plugin_moellmchats.tool_manager import ToolSnapshot
from nonebot_plugin_moellmchats.tool_providers import (
    DiscoveredTool,
    ProviderCatalogSnapshot,
    ProviderRegistration,
    registered_tool_provider,
)

_TOKEN = "t" * 32
_WRONG_TOKEN = "w" * 32
_ACTIVE_DIGEST = "a" * 64
_APPROVED_DIGEST = "b" * 64
_DRAFT_DIGEST = "c" * 64
_RESULT_DIGEST = "d" * 64
_OPERATION_ID = "e" * 64


async def _handler(**_kwargs: Any) -> str:
    return "must-not-run"


def _spec(
    name: str,
    *,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    permission: str = "user",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"safe description for {name}",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_handler,
        effect=effect,
        permission=permission,
        timeout_seconds=12,
        result_limit=1024,
        dependencies=(),
    )


def _tool_snapshot(
    generation: int,
    lifecycle: LifecycleState,
    names: tuple[str, ...] = ("alpha", "beta"),
) -> ToolSnapshot:
    specs = tuple(
        _spec(
            name,
            effect=ToolEffect.MUTATING if name == "beta" else ToolEffect.READ_ONLY,
            permission="superuser" if name == "beta" else "user",
        )
        for name in names
    )
    registration = ProviderRegistration.from_provider(registered_tool_provider)
    tools = {
        spec.name: DiscoveredTool(
            provider_id=registration.provider_id,
            source=registration.source,
            trust=registration.trust,
            generation=generation,
            spec=spec,
        )
        for spec in specs
    }
    catalog = ProviderCatalogSnapshot(
        generation=generation,
        registrations={registration.provider_id: registration},
        tools=tools,
    )
    legacy_tools = {
        spec.name: {
            **spec.as_legacy_schema(),
            "source": registration.source.value,
        }
        for spec in specs
    }
    return ToolSnapshot(
        generation=generation,
        plugin_info={},
        custom_tools=legacy_tools,
        tool_dependencies={spec.name: set(spec.dependencies) for spec in specs},
        mcp_tool_names=frozenset(),
        provider_catalog=catalog,
        generated_state_revision=lifecycle.revision,
        generated_state_digest=lifecycle.state_digest,
        generated_active=lifecycle.active,
    )


def _evidence() -> tuple[DraftEvidence, ...]:
    return (
        DraftEvidence(
            state=DraftState.STATIC_VALIDATED,
            draft_digest=_DRAFT_DIGEST,
            producer="static-policy",
            outcome="passed",
            summary="static validation passed",
            recorded_at=2,
            risks=("network capability requested",),
        ),
        DraftEvidence(
            state=DraftState.SANDBOX_TESTED,
            draft_digest=_DRAFT_DIGEST,
            producer="sandbox",
            outcome="passed",
            summary="sandbox tests passed",
            recorded_at=3,
        ),
        DraftEvidence(
            state=DraftState.MODEL_REVIEWED,
            draft_digest=_DRAFT_DIGEST,
            producer="review-model-secret-name",
            outcome="passed",
            summary="model review private summary",
            recorded_at=4,
            risks=("network capability requested",),
        ),
    )


def _lifecycle() -> LifecycleState:
    return LifecycleState(
        revision=4,
        drafts={
            "draft000001": DraftRecord(
                draft_id="draft000001",
                bundle_id="DraftBundle",
                digest=_DRAFT_DIGEST,
                state=DraftState.AWAITING_APPROVAL,
                created_at=1,
                updated_at=5,
                evidence=_evidence(),
            )
        },
        versions={
            "WeatherBundle": {
                _ACTIVE_DIGEST: VersionRecord(
                    bundle_id="WeatherBundle",
                    digest=_ACTIVE_DIGEST,
                    state=VersionState.ACTIVATED,
                    source_draft_id="old-draft",
                    created_at=1,
                    approved_at=2,
                    activated_at=3,
                ),
                _APPROVED_DIGEST: VersionRecord(
                    bundle_id="WeatherBundle",
                    digest=_APPROVED_DIGEST,
                    state=VersionState.APPROVED,
                    source_draft_id="new-draft",
                    created_at=4,
                    approved_at=5,
                ),
            }
        },
        active={"WeatherBundle": _ACTIVE_DIGEST},
        permission_grants={},
    )


def _snapshot(
    lifecycle: LifecycleState | None = None,
    *,
    generation: int = 7,
    names: tuple[str, ...] = ("alpha", "beta"),
) -> RuntimeSnapshot:
    state = _lifecycle() if lifecycle is None else lifecycle
    return RuntimeSnapshot(
        generation=generation,
        config={"secret": "configuration-must-not-leak"},
        model_state=None,
        temperaments={},
        temperament_assignments={},
        replies={},
        tool_snapshot=_tool_snapshot(generation, state, names),
        emotions=(),
        reloaded_at=10,
        generated_state_revision=state.revision,
        generated_state_digest=state.state_digest,
        generated_active=state.active,
    )


def _credential(token: str = _TOKEN) -> RuntimeApiCredential:
    value = RuntimeApiCredential.from_authorization_header(f"Bearer {token}".encode("ascii"))
    assert value is not None
    return value


def _principal(*scopes: str) -> RuntimeApiPrincipal:
    return RuntimeApiPrincipal(
        subject="tool-admin",
        scopes=frozenset(scopes),
    )


class _SnapshotReader:
    def __init__(self, snapshot: RuntimeSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def current(self) -> RuntimeSnapshot | None:
        self.calls += 1
        return self.snapshot


class _LifecycleReader:
    def __init__(self, lifecycle: LifecycleState | object) -> None:
        self.lifecycle = lifecycle
        self.calls = 0
        self.error: BaseException | None = None

    async def read_current(self) -> LifecycleState:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.lifecycle  # type: ignore[return-value]


class _Mutations:
    def __init__(self) -> None:
        self.approvals: list[ApproveToolDraftCommand] = []
        self.activations: list[ActivateToolBundleCommand] = []
        self.error: BaseException | None = None
        self.invalid_result: object | None = None

    async def approve_draft(
        self,
        command: ApproveToolDraftCommand,
    ) -> ToolBundleMutationResult:
        self.approvals.append(command)
        if self.error is not None:
            raise self.error
        if self.invalid_result is not None:
            return self.invalid_result  # type: ignore[return-value]
        return ToolBundleMutationResult(
            operation="approve_draft",
            operation_id=_OPERATION_ID,
            generation=command.expected_generation + 1,
            lifecycle_revision=command.expected_lifecycle_revision + 1,
            lifecycle_state_digest=_RESULT_DIGEST,
            bundle_id=command.bundle_id,
            digest=command.digest,
            draft_id=command.draft_id,
            active_digest=None,
            audit_recorded=True,
        )

    async def activate_bundle(
        self,
        command: ActivateToolBundleCommand,
    ) -> ToolBundleMutationResult:
        self.activations.append(command)
        if self.error is not None:
            raise self.error
        if self.invalid_result is not None:
            return self.invalid_result  # type: ignore[return-value]
        return ToolBundleMutationResult(
            operation="activate_bundle",
            operation_id=_OPERATION_ID,
            generation=command.expected_generation + 1,
            lifecycle_revision=command.expected_lifecycle_revision + 1,
            lifecycle_state_digest=_RESULT_DIGEST,
            bundle_id=command.bundle_id,
            digest=command.digest,
            draft_id=None,
            active_digest=command.digest,
            audit_recorded=True,
        )


def _service(
    *,
    snapshot: RuntimeSnapshot | None = None,
    lifecycle: LifecycleState | None = None,
    principal: RuntimeApiPrincipal | None = None,
) -> tuple[ToolBundleApiService, _SnapshotReader, _LifecycleReader, _Mutations]:
    state = _lifecycle() if lifecycle is None else lifecycle
    snapshots = _SnapshotReader(_snapshot(state) if snapshot is None else snapshot)
    lifecycles = _LifecycleReader(state)
    mutations = _Mutations()
    service = ToolBundleApiService(
        snapshots=snapshots,
        lifecycle=lifecycles,
        authenticator=StaticBearerRuntimeApiAuthenticator(
            token=_TOKEN,
            principal=principal
            or _principal(
                TOOL_BUNDLE_API_READ_SCOPE,
                TOOL_BUNDLE_API_WRITE_SCOPE,
            ),
        ),
        mutations=mutations,
    )
    return service, snapshots, lifecycles, mutations


def _request(
    path: str,
    *,
    method: str = "GET",
    token: str | None = _TOKEN,
    query_string: bytes = b"",
    body: bytes = b"",
    content_type: str | None = None,
) -> RuntimeApiRequest:
    return RuntimeApiRequest(
        method=method,
        path=path,
        query_string=query_string,
        credential=None if token is None else _credential(token),
        content_type=content_type,
        body=body,
    )


def _mutation_body(
    state: LifecycleState | None = None,
    *,
    generation: int = 7,
    digest: str = _DRAFT_DIGEST,
    include_review_stamp: bool = True,
) -> bytes:
    lifecycle = _lifecycle() if state is None else state
    payload: dict[str, Any] = {
        "digest": digest,
        "expected_generation": generation,
        "expected_lifecycle_revision": lifecycle.revision,
        "expected_lifecycle_state_digest": lifecycle.state_digest,
    }
    if include_review_stamp:
        draft = lifecycle.drafts.get("draft000001")
        assert draft is not None
        payload["review_stamp"] = draft_review_stamp(
            draft_id=draft.draft_id,
            digest=draft.digest,
            lifecycle_revision=lifecycle.revision,
            lifecycle_state_digest=lifecycle.state_digest,
            active_digest=lifecycle.active.get(draft.bundle_id),
        )
    return json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _payload(response: RuntimeApiResponse) -> dict[str, Any]:
    return json.loads(response.body)


def test_endpoint_contract_is_exact_frozen_and_separates_read_write_scopes() -> None:
    service, *_ = _service()

    assert service.endpoints == (
        ToolBundleApiEndpoint("GET", "/tools", TOOL_BUNDLE_API_READ_SCOPE),
        ToolBundleApiEndpoint("GET", "/tools/{name}", TOOL_BUNDLE_API_READ_SCOPE),
        ToolBundleApiEndpoint("GET", "/tool-bundles", TOOL_BUNDLE_API_READ_SCOPE),
        ToolBundleApiEndpoint("GET", "/tool-drafts", TOOL_BUNDLE_API_READ_SCOPE),
        ToolBundleApiEndpoint(
            "POST",
            "/tool-drafts/{id}/approve",
            TOOL_BUNDLE_API_WRITE_SCOPE,
        ),
        ToolBundleApiEndpoint(
            "POST",
            "/tool-bundles/{id}/activate",
            TOOL_BUNDLE_API_WRITE_SCOPE,
        ),
    )
    with pytest.raises(FrozenInstanceError):
        service.endpoints[0].path_template = "/changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_tools_list_reads_only_current_provider_catalog_and_is_minimal() -> None:
    service, snapshots, lifecycles, mutations = _service()

    response = await service.handle(_request("/tools"))

    assert response.status_code == 200
    assert _payload(response) == {
        "api_version": 1,
        "generation": 7,
        "items": [
            {
                "effect": "read_only",
                "name": "alpha",
                "permission": "user",
                "provider_id": "registered",
                "source": "registered",
                "trust": "trusted",
            },
            {
                "effect": "mutating",
                "name": "beta",
                "permission": "superuser",
                "provider_id": "registered",
                "source": "registered",
                "trust": "trusted",
            },
        ],
        "next_cursor": None,
    }
    text = response.body.decode("utf-8")
    assert "configuration-must-not-leak" not in text
    assert _ACTIVE_DIGEST not in text
    assert "must-not-run" not in text
    assert snapshots.calls == 1
    assert lifecycles.calls == 0
    assert not mutations.approvals
    assert not mutations.activations


@pytest.mark.asyncio
async def test_tool_detail_exposes_bounded_contract_not_handler_or_parameters() -> None:
    service, *_ = _service()

    response = await service.handle(_request("/tools/beta"))

    assert response.status_code == 200
    assert _payload(response)["tool"] == {
        "dependency_count": 0,
        "dependencies": [],
        "description": "safe description for beta",
        "effect": "mutating",
        "has_capability_policy": False,
        "name": "beta",
        "parameter_count": 1,
        "permission": "superuser",
        "provider_id": "registered",
        "required_parameter_count": 1,
        "result_limit": 1024,
        "source": "registered",
        "timeout_seconds": 12.0,
        "trust": "trusted",
    }
    assert "handler" not in response.body.decode("utf-8")
    assert "additionalProperties" not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_unknown_tool_returns_not_found_without_lifecycle_read() -> None:
    service, snapshots, lifecycles, _ = _service()

    response = await service.handle(_request("/tools/missing"))

    assert response.status_code == 404
    assert _payload(response)["error"] == "not_found"
    assert snapshots.calls == 1
    assert lifecycles.calls == 0


@pytest.mark.asyncio
async def test_tool_list_uses_bounded_generation_bound_cursor() -> None:
    names = tuple(f"tool_{index:02d}" for index in range(23))
    state = _lifecycle()
    snapshot = _snapshot(state, names=names)
    service, snapshots, _, _ = _service(snapshot=snapshot, lifecycle=state)

    first = await service.handle(_request("/tools", query_string=b"limit=5"))
    first_payload = _payload(first)
    cursor = first_payload["next_cursor"]
    second = await service.handle(
        _request(
            "/tools",
            query_string=f"limit=5&cursor={cursor}".encode("ascii"),
        )
    )

    assert [item["name"] for item in first_payload["items"]] == list(names[:5])
    assert [item["name"] for item in _payload(second)["items"]] == list(names[5:10])
    snapshots.snapshot = _snapshot(state, generation=8, names=names)
    stale = await service.handle(
        _request(
            "/tools",
            query_string=f"cursor={cursor}".encode("ascii"),
        )
    )
    assert stale.status_code == 409
    assert _payload(stale)["error"] == "stale_cursor"


@pytest.mark.parametrize(
    "query",
    [
        b"limit=0",
        b"limit=21",
        b"limit=01",
        b"limit=5&limit=6",
        b"unknown=1",
        b"cursor=not-canonical",
        b"limit=5&cursor=x&extra=y",
        "limit=é".encode(),
    ],
)
@pytest.mark.asyncio
async def test_invalid_page_query_fails_before_snapshot_read(query: bytes) -> None:
    service, snapshots, lifecycles, _ = _service()

    response = await service.handle(_request("/tool-drafts", query_string=query))

    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_query"
    assert snapshots.calls == 0
    assert lifecycles.calls == 0


@pytest.mark.asyncio
async def test_bundle_versions_are_flat_bounded_and_lifecycle_stamped() -> None:
    state = _lifecycle()
    service, snapshots, lifecycles, _ = _service(lifecycle=state)

    response = await service.handle(_request("/tool-bundles", query_string=b"limit=1"))
    payload = _payload(response)

    assert response.status_code == 200
    assert payload["generation"] == 7
    assert payload["lifecycle_revision"] == state.revision
    assert payload["lifecycle_state_digest"] == state.state_digest
    assert payload["items"] == [
        {
            "activated_at": 3.0,
            "active": True,
            "approved_at": 2.0,
            "archived_at": None,
            "bundle_id": "WeatherBundle",
            "created_at": 1.0,
            "deprecated_at": None,
            "digest": _ACTIVE_DIGEST,
            "source_draft_id": "old-draft",
            "state": "activated",
        }
    ]
    assert payload["next_cursor"] is not None
    assert snapshots.calls == lifecycles.calls == 1


@pytest.mark.asyncio
async def test_draft_list_exposes_state_and_risk_counts_without_private_review_text() -> None:
    state = _lifecycle()
    service, *_ = _service(lifecycle=state)

    response = await service.handle(_request("/tool-drafts"))
    payload = _payload(response)

    assert response.status_code == 200
    assert payload["items"] == [
        {
            "approvable": True,
            "bundle_id": "DraftBundle",
            "created_at": 1.0,
            "digest": _DRAFT_DIGEST,
            "draft_id": "draft000001",
            "evidence_states": [
                "static_validated",
                "sandbox_tested",
                "model_reviewed",
            ],
            "has_risks": True,
            "risk_count": 1,
            "state": "awaiting_approval",
            "updated_at": 5.0,
        }
    ]
    text = response.body.decode("utf-8")
    assert "review-model-secret-name" not in text
    assert "model review private summary" not in text
    assert "network capability requested" not in text


@pytest.mark.parametrize("drift", ["revision", "digest", "active"])
@pytest.mark.asyncio
async def test_lifecycle_must_exactly_match_runtime_generated_stamp(drift: str) -> None:
    state = _lifecycle()
    snapshot = _snapshot(state)
    service, _, lifecycles, mutations = _service(snapshot=snapshot, lifecycle=state)
    if drift == "revision":
        lifecycles.lifecycle = LifecycleState(
            revision=state.revision + 1,
            drafts=state.drafts,
            versions=state.versions,
            active=state.active,
            permission_grants=state.permission_grants,
        )
    elif drift == "digest":
        changed_draft = replace(
            state.drafts["draft000001"],
            updated_at=6,
        )
        lifecycles.lifecycle = LifecycleState(
            revision=state.revision,
            drafts={"draft000001": changed_draft},
            versions=state.versions,
            active=state.active,
            permission_grants=state.permission_grants,
        )
    else:
        approved = state.versions["WeatherBundle"][_APPROVED_DIGEST]
        old = state.versions["WeatherBundle"][_ACTIVE_DIGEST]
        changed_versions = {
            "WeatherBundle": {
                _ACTIVE_DIGEST: replace(
                    old,
                    state=VersionState.DEPRECATED,
                    deprecated_at=6,
                ),
                _APPROVED_DIGEST: replace(
                    approved,
                    state=VersionState.ACTIVATED,
                    activated_at=6,
                ),
            }
        }
        lifecycles.lifecycle = LifecycleState(
            revision=state.revision,
            drafts=state.drafts,
            versions=changed_versions,
            active={"WeatherBundle": _APPROVED_DIGEST},
            permission_grants=state.permission_grants,
        )

    response = await service.handle(_request("/tool-bundles"))

    assert response.status_code == 503
    assert _payload(response)["error"] == "tool_lifecycle_unavailable"
    assert not mutations.approvals
    assert not mutations.activations


@pytest.mark.asyncio
async def test_missing_or_wrong_authentication_precedes_all_readers_and_mutations() -> None:
    service, snapshots, lifecycles, mutations = _service()

    missing = await service.handle(_request("/tool-drafts", token=None))
    wrong = await service.handle(_request("/tool-drafts", token=_WRONG_TOKEN))

    assert missing.status_code == wrong.status_code == 401
    assert missing.body == wrong.body
    assert snapshots.calls == lifecycles.calls == 0
    assert not mutations.approvals
    assert not mutations.activations


@pytest.mark.asyncio
async def test_read_and_write_scopes_are_independent_and_precede_state_reads() -> None:
    read_service, read_snapshots, read_lifecycles, _ = _service(principal=_principal(TOOL_BUNDLE_API_WRITE_SCOPE))
    write_service, write_snapshots, write_lifecycles, write_mutations = _service(principal=_principal(TOOL_BUNDLE_API_READ_SCOPE))

    read = await read_service.handle(_request("/tools"))
    write = await write_service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(),
        )
    )

    assert read.status_code == write.status_code == 403
    assert read_snapshots.calls == read_lifecycles.calls == 0
    assert write_snapshots.calls == write_lifecycles.calls == 0
    assert not write_mutations.approvals


@pytest.mark.asyncio
async def test_unknown_path_wrong_method_and_transport_fail_before_state_reads() -> None:
    service, snapshots, lifecycles, mutations = _service()

    unknown = await service.handle(_request("/tool-drafts/unknown"))
    method = await service.handle(_request("/tools", method="POST"))
    body = await service.handle(_request("/tools", body=b"{}", content_type="application/json"))
    detail_query = await service.handle(_request("/tools/alpha", query_string=b"limit=1"))

    assert unknown.status_code == 404
    assert method.status_code == 405
    assert dict(method.extra_headers) == {b"allow": b"GET"}
    assert body.status_code == detail_query.status_code == 400
    assert snapshots.calls == lifecycles.calls == 0
    assert not mutations.approvals
    assert not mutations.activations


@pytest.mark.asyncio
async def test_approve_requires_exact_content_type_body_and_full_cas() -> None:
    service, snapshots, lifecycles, mutations = _service()

    missing_type = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            body=_mutation_body(),
        )
    )
    malformed = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=b'{"digest":"duplicate","digest":"duplicate"}',
        )
    )
    extra = json.loads(_mutation_body())
    extra["actor"] = "forged-admin"
    forged_actor = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=json.dumps(extra).encode(),
        )
    )

    assert missing_type.status_code == 415
    assert malformed.status_code == forged_actor.status_code == 400
    assert snapshots.calls == lifecycles.calls == 0
    assert not mutations.approvals


@pytest.mark.parametrize(
    "field",
    ["expected_generation", "expected_lifecycle_revision"],
)
@pytest.mark.asyncio
async def test_mutation_rejects_out_of_range_identity_before_state_reads(field: str) -> None:
    service, snapshots, lifecycles, mutations = _service()
    body = json.loads(_mutation_body())
    body[field] = 1 << 63

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=json.dumps(body).encode(),
        )
    )

    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_request"
    assert snapshots.calls == lifecycles.calls == 0
    assert not mutations.approvals


@pytest.mark.asyncio
async def test_approve_draft_passes_authenticated_actor_and_exact_preconditions() -> None:
    state = _lifecycle()
    service, snapshots, lifecycles, mutations = _service(lifecycle=state)

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(state),
        )
    )

    assert response.status_code == 200
    assert mutations.approvals == [
        ApproveToolDraftCommand(
            actor_subject="tool-admin",
            draft_id="draft000001",
            bundle_id="DraftBundle",
            digest=_DRAFT_DIGEST,
            expected_generation=7,
            expected_lifecycle_revision=state.revision,
            expected_lifecycle_state_digest=state.state_digest,
            review_stamp=draft_review_stamp(
                draft_id="draft000001",
                digest=_DRAFT_DIGEST,
                lifecycle_revision=state.revision,
                lifecycle_state_digest=state.state_digest,
                active_digest=None,
            ),
        )
    ]
    assert _payload(response) == {
        "active_digest": None,
        "api_version": 1,
        "audit_recorded": True,
        "bundle_id": "DraftBundle",
        "digest": _DRAFT_DIGEST,
        "draft_id": "draft000001",
        "generation": 8,
        "lifecycle_revision": 5,
        "lifecycle_state_digest": _RESULT_DIGEST,
        "operation": "approve_draft",
        "operation_id": _OPERATION_ID,
    }
    assert snapshots.calls == lifecycles.calls == 1


@pytest.mark.parametrize("saturated", ["generation", "revision"])
@pytest.mark.asyncio
async def test_mutation_refuses_identity_exhaustion_before_calling_port(saturated: str) -> None:
    state = _lifecycle()
    generation = 7
    if saturated == "generation":
        generation = (1 << 63) - 1
    else:
        state = LifecycleState(
            revision=(1 << 63) - 1,
            drafts=state.drafts,
            versions=state.versions,
            active=state.active,
            permission_grants=state.permission_grants,
        )
    service, _, _, mutations = _service(
        snapshot=_snapshot(state, generation=generation),
        lifecycle=state,
    )

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(state, generation=generation),
        )
    )

    assert response.status_code == 503
    assert _payload(response)["error"] == "mutation_unavailable"
    assert not mutations.approvals


@pytest.mark.parametrize(
    "change",
    ["generation", "revision", "state_digest", "draft_digest", "review_stamp"],
)
@pytest.mark.asyncio
async def test_approve_stale_precondition_never_calls_mutation(change: str) -> None:
    state = _lifecycle()
    body = json.loads(_mutation_body(state))
    if change == "generation":
        body["expected_generation"] = 6
    elif change == "revision":
        body["expected_lifecycle_revision"] = 3
    elif change == "state_digest":
        body["expected_lifecycle_state_digest"] = "f" * 64
    elif change == "draft_digest":
        body["digest"] = "f" * 64
    else:
        body["review_stamp"] = "f" * 64
    service, _, _, mutations = _service(lifecycle=state)

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=json.dumps(body).encode(),
        )
    )

    assert response.status_code == 409
    assert _payload(response)["error"] == "mutation_precondition_failed"
    assert not mutations.approvals


@pytest.mark.asyncio
async def test_approve_missing_or_nonapprovable_draft_never_calls_mutation() -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)
    missing = await service.handle(
        _request(
            "/tool-drafts/missing-draft/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(state),
        )
    )
    rejected_draft = replace(
        state.drafts["draft000001"],
        state=DraftState.REJECTED,
        evidence=(
            DraftEvidence(
                state=DraftState.REJECTED,
                draft_digest=_DRAFT_DIGEST,
                producer="admin",
                outcome="rejected",
                summary="rejected",
                recorded_at=5,
            ),
        ),
    )
    rejected_state = LifecycleState(
        revision=state.revision,
        drafts={"draft000001": rejected_draft},
        versions=state.versions,
        active=state.active,
        permission_grants=state.permission_grants,
    )
    rejected_service, _, _, rejected_mutations = _service(lifecycle=rejected_state)
    rejected = await rejected_service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(rejected_state),
        )
    )

    assert missing.status_code == 404
    assert rejected.status_code == 409
    assert not mutations.approvals
    assert not rejected_mutations.approvals


@pytest.mark.asyncio
async def test_activate_bundle_calls_port_only_for_approved_exact_version() -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)

    response = await service.handle(
        _request(
            "/tool-bundles/WeatherBundle/activate",
            method="POST",
            content_type="application/json",
            body=_mutation_body(
                state,
                digest=_APPROVED_DIGEST,
                include_review_stamp=False,
            ),
        )
    )

    assert response.status_code == 200
    assert mutations.activations == [
        ActivateToolBundleCommand(
            actor_subject="tool-admin",
            bundle_id="WeatherBundle",
            digest=_APPROVED_DIGEST,
            expected_generation=7,
            expected_lifecycle_revision=state.revision,
            expected_lifecycle_state_digest=state.state_digest,
        )
    ]
    assert _payload(response)["active_digest"] == _APPROVED_DIGEST


@pytest.mark.parametrize(
    ("bundle_id", "digest", "status"),
    [
        ("MissingBundle", _APPROVED_DIGEST, 404),
        ("WeatherBundle", "f" * 64, 404),
        ("WeatherBundle", _ACTIVE_DIGEST, 409),
    ],
)
@pytest.mark.asyncio
async def test_activate_rejects_missing_or_already_active_target(
    bundle_id: str,
    digest: str,
    status: int,
) -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)

    response = await service.handle(
        _request(
            f"/tool-bundles/{bundle_id}/activate",
            method="POST",
            content_type="application/json",
            body=_mutation_body(
                state,
                digest=digest,
                include_review_stamp=False,
            ),
        )
    )

    assert response.status_code == status
    assert not mutations.activations


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ToolBundleMutationNotFoundError(), 404, "not_found"),
        (
            ToolBundleMutationConflictError(),
            409,
            "mutation_precondition_failed",
        ),
        (
            ToolBundleMutationUnavailableError("secret backend endpoint"),
            503,
            "mutation_unavailable",
        ),
        (RuntimeError("secret internal failure"), 503, "mutation_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_mutation_failures_are_fixed_redacted_and_called_once(
    error: BaseException,
    status: int,
    code: str,
) -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)
    mutations.error = error

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(state),
        )
    )

    assert response.status_code == status
    assert _payload(response)["error"] == code
    assert len(mutations.approvals) == 1
    assert b"secret" not in response.body


@pytest.mark.asyncio
async def test_unknown_mutation_result_explicitly_forbids_automatic_replay() -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)
    mutations.error = ToolBundleMutationResultUnknownError("do not expose")

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(state),
        )
    )

    assert response.status_code == 409
    assert _payload(response) == {
        "api_version": 1,
        "error": "mutation_result_unknown",
        "retryable": False,
    }
    assert len(mutations.approvals) == 1


@pytest.mark.asyncio
async def test_mutation_cancellation_propagates_without_replay() -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)
    mutations.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await service.handle(
            _request(
                "/tool-drafts/draft000001/approve",
                method="POST",
                content_type="application/json",
                body=_mutation_body(state),
            )
        )
    assert len(mutations.approvals) == 1


@pytest.mark.asyncio
async def test_invalid_mutation_port_result_fails_closed() -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)
    mutations.invalid_result = object()

    response = await service.handle(
        _request(
            "/tool-drafts/draft000001/approve",
            method="POST",
            content_type="application/json",
            body=_mutation_body(state),
        )
    )

    assert response.status_code == 503
    assert _payload(response)["error"] == "mutation_unavailable"


@pytest.mark.asyncio
async def test_invalid_or_failing_lifecycle_reader_is_redacted() -> None:
    service, _, lifecycles, _ = _service()
    lifecycles.lifecycle = object()
    invalid = await service.handle(_request("/tool-drafts"))
    lifecycles.error = RuntimeError("secret lifecycle path")
    failed = await service.handle(_request("/tool-drafts"))

    assert invalid.status_code == failed.status_code == 503
    assert invalid.body == failed.body
    assert b"secret" not in failed.body


@pytest.mark.asyncio
async def test_h02_service_runs_through_detached_generic_asgi_adapter() -> None:
    state = _lifecycle()
    service, _, _, mutations = _service(lifecycle=state)
    app = RuntimeApiASGIApp(service=service)
    body = _mutation_body(state)
    chunks = [body[:20], body[20:]]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": bool(chunks),
        }

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/tool-drafts/draft000001/approve",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        },
        receive,
        send,
    )

    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"])["operation"] == "approve_draft"
    assert len(mutations.approvals) == 1


@pytest.mark.asyncio
async def test_asgi_rejects_declared_oversize_or_length_mismatch_before_service() -> None:
    service, snapshots, lifecycles, mutations = _service()
    app = RuntimeApiASGIApp(service=service)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def call(headers: list[tuple[bytes, bytes]]) -> tuple[int, dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/tool-drafts/draft000001/approve",
                "query_string": b"",
                "headers": headers,
            },
            receive,
            send,
        )
        return sent[0]["status"], json.loads(sent[1]["body"])

    common = [
        (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
        (b"content-type", b"application/json"),
    ]
    oversized = await call([*common, (b"content-length", b"16385")])
    mismatch = await call([*common, (b"content-length", b"3")])

    assert oversized == (413, {"api_version": 1, "error": "request_too_large"})
    assert mismatch == (400, {"api_version": 1, "error": "invalid_request"})
    assert snapshots.calls == lifecycles.calls == 0
    assert not mutations.approvals


def test_runtime_response_recursively_detaches_nested_json() -> None:
    nested = {"items": [{"name": "alpha"}], "next_cursor": None}
    response = RuntimeApiResponse(status_code=200, payload=nested)
    nested["items"][0]["name"] = "tampered"

    assert _payload(response)["items"][0]["name"] == "alpha"
    with pytest.raises(TypeError):
        response.payload["items"][0]["name"] = "changed"  # type: ignore[index]


def test_runtime_response_rejects_cycles_and_oversized_nested_collections() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["cycle"] = cyclic
    with pytest.raises(Exception, match="循环"):
        RuntimeApiResponse(status_code=200, payload=cyclic)
    with pytest.raises(Exception, match="项数"):
        RuntimeApiResponse(
            status_code=200,
            payload={"items": list(range(513))},
        )


@pytest.mark.parametrize("value", [1 << 63, -(1 << 63)])
def test_runtime_response_rejects_out_of_range_json_integers(value: int) -> None:
    with pytest.raises(Exception, match="integer"):
        RuntimeApiResponse(status_code=200, payload={"value": value})


def test_tool_bundle_api_module_has_no_global_service_reader_mutation_or_app() -> None:
    module = importlib.import_module("nonebot_plugin_moellmchats.tool_bundle_api")

    assert not any(
        isinstance(
            value,
            (
                ToolBundleApiService,
                _SnapshotReader,
                _LifecycleReader,
                _Mutations,
                RuntimeApiASGIApp,
            ),
        )
        for value in vars(module).values()
    )

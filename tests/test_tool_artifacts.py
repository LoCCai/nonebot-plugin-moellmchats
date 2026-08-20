from __future__ import annotations

from dataclasses import replace

import pytest

from nonebot_plugin_moellmchats.runtime_snapshot import (
    immutable_mapping,
    mutable_value,
)
from nonebot_plugin_moellmchats.tool_artifacts import (
    ToolArtifact,
    ToolContractSnapshot,
    canonical_bundle_digest,
    source_sha256,
)
from nonebot_plugin_moellmchats.tool_contracts import (
    ToolEffect,
    ToolPolicy,
    ToolSpec,
)


async def _handler(value: str) -> str:
    return value


def _spec(
    *,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    permission: str = "user",
) -> ToolSpec:
    return ToolSpec(
        name="echo",
        description="echo one value",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=_handler,
        effect=effect,
        permission=permission,
        timeout_seconds=30,
        result_limit=6000,
        policy=ToolPolicy.generated(),
    )


def _schema(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.parameters,
    }


def test_custom_artifact_detaches_contract_and_verifies_pinned_generation() -> None:
    source = b"async def echo(value):\r\n    return value\r\n"
    spec = _spec()
    contract = ToolContractSnapshot.from_spec(spec)
    artifact = ToolArtifact(
        tool_name="echo",
        handler_name="echo",
        source=source,
        source_hash=source_sha256(source),
        schema=_schema(spec),
        spec=spec,
        contract=contract,
        source_type="custom_file",
        generation=4,
        filename="tools.py",
    )

    artifact.verify(
        expected_artifact_digest=artifact.artifact_digest,
        expected_bundle_digest=None,
        generation=4,
    )
    assert b"\r" not in artifact.source
    assert len(artifact.artifact_digest) == 64
    with pytest.raises(ValueError, match="generation"):
        artifact.verify(
            expected_artifact_digest=artifact.artifact_digest,
            expected_bundle_digest=None,
            generation=5,
        )


def test_generated_artifact_binds_exact_manifest_tool_and_contract() -> None:
    source = b"async def echo(value):\n    return value\n"
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    requested_spec = _spec()
    manifest = {
        "bundle_id": "echo_bundle",
        "description": "echo",
        "capabilities": {
            "network": False,
            "process": False,
            "workspace": True,
        },
        "tools": [
            {
                "name": "echo",
                "description": "echo one value",
                "parameters": requested_spec.parameters,
                "handler": "echo",
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": 30,
                "result_limit": 6000,
                "dependencies": [],
            }
        ],
    }
    spec = _spec(permission="superuser")
    contract = ToolContractSnapshot.from_spec(
        spec,
        requested_permission="user",
        declared_effect=ToolEffect.READ_ONLY,
    )
    digest = canonical_bundle_digest(manifest, source, tests_source)
    artifact = ToolArtifact(
        tool_name="echo",
        handler_name="echo",
        source=source,
        source_hash=source_sha256(source),
        schema=_schema(spec),
        spec=spec,
        contract=contract,
        source_type="generated",
        generation=7,
        filename="tool.py",
        tests_source=tests_source,
        bundle_manifest=manifest,
        bundle_id="echo_bundle",
        bundle_digest=digest,
    )

    artifact.verify(
        expected_artifact_digest=artifact.artifact_digest,
        expected_bundle_digest=digest,
        generation=7,
    )
    with pytest.raises(ValueError, match="handler_name"):
        replace(artifact, handler_name="__tests__", artifact_digest="")
    with pytest.raises(ValueError, match="bundle digest"):
        replace(artifact, bundle_digest="0" * 64, artifact_digest="")


def test_artifact_digest_binds_effect_permission_and_limits() -> None:
    source = b"async def echo(value):\n    return value\n"
    original_spec = _spec()
    artifact = ToolArtifact(
        tool_name="echo",
        handler_name="echo",
        source=source,
        source_hash=source_sha256(source),
        schema=_schema(original_spec),
        spec=original_spec,
        contract=ToolContractSnapshot.from_spec(original_spec),
        source_type="custom_file",
        generation=1,
        filename="tool.py",
    )
    changed_spec = _spec(effect=ToolEffect.MUTATING, permission="superuser")

    with pytest.raises(ValueError, match="安全契约"):
        replace(artifact, spec=changed_spec, artifact_digest="")


def test_generated_artifact_rejects_helper_not_declared_in_manifest() -> None:
    source = b"async def echo(value):\n    return value\nasync def helper():\n    return 1\n"
    tests_source = b"async def run_tests(tool_module):\n    return 'ok'\n"
    spec = _spec(permission="superuser")
    manifest = {
        "bundle_id": "echo_bundle",
        "description": "echo",
        "tools": [
            {
                "name": "echo",
                "description": spec.description,
                "parameters": spec.parameters,
                "handler": "echo",
                "permission": "user",
                "effect": "read_only",
                "timeout_seconds": 30,
                "result_limit": 6000,
            }
        ],
    }
    digest = canonical_bundle_digest(manifest, source, tests_source)
    with pytest.raises(ValueError, match="manifest 工具契约"):
        ToolArtifact(
            tool_name="echo",
            handler_name="helper",
            source=source,
            source_hash=source_sha256(source),
            schema=_schema(spec),
            spec=spec,
            contract=ToolContractSnapshot.from_spec(
                spec,
                requested_permission="user",
            ),
            source_type="generated",
            generation=1,
            filename="tool.py",
            tests_source=tests_source,
            bundle_manifest=manifest,
            bundle_id="echo_bundle",
            bundle_digest=digest,
        )


def test_bundle_digest_normalizes_crlf_like_existing_store() -> None:
    manifest = {"bundle_id": "x", "tools": []}
    lf = canonical_bundle_digest(manifest, b"a\nb\n", b"c\n")
    crlf = canonical_bundle_digest(manifest, b"a\r\nb\r\n", b"c\r\n")

    assert lf == crlf


def test_custom_artifact_rejects_paths_and_generated_metadata() -> None:
    source = b"async def echo(value):\n    return value\n"
    spec = _spec()
    kwargs = {
        "tool_name": "echo",
        "handler_name": "echo",
        "source": source,
        "source_hash": source_sha256(source),
        "schema": _schema(spec),
        "spec": spec,
        "contract": ToolContractSnapshot.from_spec(spec),
        "source_type": "custom_file",
        "generation": 1,
    }
    with pytest.raises(ValueError, match="basename"):
        ToolArtifact(**kwargs, filename="../tool.py")
    with pytest.raises(ValueError, match="不得包含"):
        ToolArtifact(**kwargs, filename="tool.py", bundle_id="fake")


def test_artifact_survives_runtime_snapshot_freeze_without_mappingproxy_copy() -> None:
    source = b"async def echo(value):\n    return value\n"
    spec = _spec()
    artifact = ToolArtifact(
        tool_name="echo",
        handler_name="echo",
        source=source,
        source_hash=source_sha256(source),
        schema=_schema(spec),
        spec=spec,
        contract=ToolContractSnapshot.from_spec(spec),
        source_type="custom_file",
        generation=3,
        filename="tool.py",
    )

    frozen = immutable_mapping({"artifact": artifact})
    mutable = mutable_value(frozen)
    assert frozen["artifact"] is artifact
    assert mutable["artifact"] is artifact
    with pytest.raises(TypeError):
        artifact.spec.parameters["properties"]["value"]["type"] = "integer"

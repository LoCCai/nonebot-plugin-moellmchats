from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePath
import re
from types import MappingProxyType
from typing import Any

from .tool_contracts import (
    CAPABILITY_DETECTOR_VERSION,
    CAPABILITY_SCHEMA_VERSION,
    ToolCapability,
    ToolCapabilityV2,
    ToolEffect,
    ToolSpec,
    parse_capability_profile,
)

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HANDLER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SOURCE_TYPES = frozenset({"custom_file", "generated"})


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            mutable_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("ToolArtifact 元数据必须是规范 JSON") from error


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("ToolArtifact JSON 字段名必须是字符串")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"ToolArtifact JSON 包含不可序列化类型: {type(value).__name__}")


def mutable_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: mutable_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mutable_json(item) for item in value]
    return value


def normalize_source_bytes(source: bytes) -> bytes:
    """Match Path.read_text(newline=None) used by existing content hashes."""

    if not isinstance(source, bytes):
        raise TypeError("工具源码快照必须是 bytes")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("工具源码必须是 UTF-8") from error
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def source_sha256(source: bytes) -> str:
    return hashlib.sha256(normalize_source_bytes(source)).hexdigest()


def canonical_bundle_digest(
    manifest: Mapping[str, Any],
    source: bytes,
    tests_source: bytes,
) -> str:
    """Canonical v1 digest shared by storage, snapshots, and execution."""

    return hashlib.sha256(
        _canonical_json(manifest)
        + b"\0"
        + normalize_source_bytes(source)
        + b"\0"
        + normalize_source_bytes(tests_source)
    ).hexdigest()


@dataclass(frozen=True)
class ToolContractSnapshot:
    """Callable-free security contract included in an artifact digest."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    requested_permission: str
    effective_permission: str
    declared_effect: ToolEffect
    effective_effect: ToolEffect
    timeout_seconds: float | None
    result_limit: int | None
    dependencies: tuple[str, ...]
    requested_capabilities: Mapping[str, bool] | None
    admin_capabilities: Mapping[str, bool] | None
    effective_capabilities: Mapping[str, bool] | None
    contract_version: int = 2
    capability_schema_version: int | None = None
    capability_detector_version: int | None = None
    detected_capabilities: Mapping[str, bool] | None = None
    requested_capabilities_v2: Mapping[str, Any] | None = None
    admin_capabilities_v2: Mapping[str, Any] | None = None
    effective_capabilities_v2: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("ToolContractSnapshot name 非法")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("ToolContractSnapshot description 不能为空")
        if self.requested_permission not in {"user", "superuser"}:
            raise ValueError("ToolContractSnapshot requested permission 非法")
        if self.effective_permission not in {"user", "superuser"}:
            raise ValueError("ToolContractSnapshot effective permission 非法")
        if (
            self.effective_permission == "user"
            and self.requested_permission != "user"
        ):
            raise ValueError("有效权限不能比工具申请权限更宽")
        if not isinstance(self.declared_effect, ToolEffect) or not isinstance(
            self.effective_effect, ToolEffect
        ):
            raise ValueError("ToolContractSnapshot effect 非法")
        if (
            self.declared_effect is ToolEffect.MUTATING
            and self.effective_effect is not ToolEffect.MUTATING
        ):
            raise ValueError("effective effect 不能弱于 manifest 声明")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("ToolContractSnapshot timeout 非法")
        if self.result_limit is not None and (
            not isinstance(self.result_limit, int)
            or isinstance(self.result_limit, bool)
            or self.result_limit <= 0
        ):
            raise ValueError("ToolContractSnapshot result_limit 非法")
        if not isinstance(self.dependencies, tuple) or not all(
            isinstance(item, str) and _TOOL_NAME_RE.fullmatch(item)
            for item in self.dependencies
        ):
            raise ValueError("ToolContractSnapshot dependencies 非法")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("ToolContractSnapshot dependencies 不得重复")
        if type(self.contract_version) is not int or self.contract_version not in {
            1,
            2,
        }:
            raise ValueError("ToolContractSnapshot contract_version 非法")

        object.__setattr__(self, "parameters", _freeze_json(self.parameters))
        for field_name in (
            "requested_capabilities",
            "admin_capabilities",
            "effective_capabilities",
        ):
            value = getattr(self, field_name)
            if value is not None:
                frozen = _freeze_json(value)
                if set(frozen) != {
                    "network",
                    "process",
                    "workspace",
                    "host_filesystem",
                    "secrets",
                } or not all(type(item) is bool for item in frozen.values()):
                    raise ValueError(
                        f"ToolContractSnapshot {field_name} capability 非法"
                    )
                object.__setattr__(self, field_name, frozen)

        legacy_values = (
            self.requested_capabilities,
            self.admin_capabilities,
            self.effective_capabilities,
        )
        if any(value is None for value in legacy_values) and any(
            value is not None for value in legacy_values
        ):
            raise ValueError("ToolContractSnapshot capability v1 字段必须同时存在")
        if self.contract_version == 1:
            if any(
                value is not None
                for value in (
                    self.capability_schema_version,
                    self.capability_detector_version,
                    self.detected_capabilities,
                    self.requested_capabilities_v2,
                    self.admin_capabilities_v2,
                    self.effective_capabilities_v2,
                )
            ):
                raise ValueError("ToolContractSnapshot v1 不得包含 capability v2 字段")
            self._verify_legacy_capability_merge()
            return

        if all(value is None for value in legacy_values):
            if any(
                value is not None
                for value in (
                    self.capability_schema_version,
                    self.capability_detector_version,
                    self.detected_capabilities,
                    self.requested_capabilities_v2,
                    self.admin_capabilities_v2,
                    self.effective_capabilities_v2,
                )
            ):
                raise ValueError("无 capability 的 ToolContractSnapshot 不得伪造 v2 policy")
            return
        if self.capability_schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ValueError("ToolContractSnapshot capability schema version 非法")
        if (
            type(self.capability_detector_version) is not int
            or self.capability_detector_version != CAPABILITY_DETECTOR_VERSION
        ):
            raise ValueError("ToolContractSnapshot capability detector version 非法")
        if self.detected_capabilities is None:
            raise ValueError("ToolContractSnapshot 缺少 detected capabilities")
        detected = _freeze_json(self.detected_capabilities)
        if set(detected) != {
            "network",
            "process",
            "workspace",
            "host_filesystem",
            "secrets",
        } or not all(type(item) is bool for item in detected.values()):
            raise ValueError("ToolContractSnapshot detected_capabilities capability 非法")
        object.__setattr__(self, "detected_capabilities", detected)

        structured_values = (
            self.requested_capabilities_v2,
            self.admin_capabilities_v2,
            self.effective_capabilities_v2,
        )
        if any(value is None for value in structured_values):
            raise ValueError("ToolContractSnapshot capability v2 字段必须同时存在")
        requested_v2 = ToolCapabilityV2.from_mapping(
            self.requested_capabilities_v2
        )
        admin_v2 = ToolCapabilityV2.from_mapping(self.admin_capabilities_v2)
        effective_v2 = ToolCapabilityV2.from_mapping(
            self.effective_capabilities_v2
        )
        if effective_v2 != requested_v2.restrict(admin_v2):
            raise ValueError("ToolContractSnapshot capability v2 merge 不一致")
        requested = ToolCapability.from_mapping(self.requested_capabilities)
        admin = ToolCapability.from_mapping(self.admin_capabilities)
        effective = ToolCapability.from_mapping(self.effective_capabilities)
        detected_value = ToolCapability.from_mapping(detected)
        if (
            requested_v2.to_legacy() != requested
            or admin_v2.to_legacy() != admin
            or effective_v2.to_legacy() != effective
        ):
            raise ValueError("ToolContractSnapshot capability v1/v2 投影不一致")
        if not detected_value.is_subset_of(effective):
            raise ValueError("ToolContractSnapshot detected capability 未获授权")
        object.__setattr__(
            self,
            "requested_capabilities_v2",
            _freeze_json(requested_v2.as_dict()),
        )
        object.__setattr__(
            self,
            "admin_capabilities_v2",
            _freeze_json(admin_v2.as_dict()),
        )
        object.__setattr__(
            self,
            "effective_capabilities_v2",
            _freeze_json(effective_v2.as_dict()),
        )

    def _verify_legacy_capability_merge(self) -> None:
        if self.requested_capabilities is None:
            return
        requested = ToolCapability.from_mapping(self.requested_capabilities)
        admin = ToolCapability.from_mapping(self.admin_capabilities)
        effective = ToolCapability.from_mapping(self.effective_capabilities)
        if requested.restrict(admin) != effective:
            raise ValueError("ToolContractSnapshot capability v1 merge 不一致")

    @classmethod
    def from_spec(
        cls,
        spec: ToolSpec,
        *,
        requested_permission: str | None = None,
        declared_effect: ToolEffect | None = None,
        contract_version: int = 2,
    ) -> ToolContractSnapshot:
        requested_capabilities = None
        admin_capabilities = None
        effective_capabilities = None
        capability_schema_version = None
        capability_detector_version = None
        detected_capabilities = None
        requested_capabilities_v2 = None
        admin_capabilities_v2 = None
        effective_capabilities_v2 = None
        if spec.policy is not None:
            policy = spec.policy
            assert policy.requested_v2 is not None
            assert policy.admin_v2 is not None
            requested_capabilities = policy.requested.as_dict()
            admin_capabilities = policy.admin.as_dict()
            effective_capabilities = policy.effective.as_dict()
            if contract_version == 1:
                if (
                    policy.detected != ToolCapability.none()
                    or policy.requested_v2
                    != ToolCapabilityV2.from_legacy(policy.requested)
                    or policy.admin_v2
                    != ToolCapabilityV2.from_legacy(policy.admin)
                ):
                    raise ValueError("ToolContractSnapshot v1 无法表示 v2 capability policy")
            elif contract_version == 2:
                capability_schema_version = policy.capability_schema_version
                capability_detector_version = policy.detector_version
                detected_capabilities = policy.detected.as_dict()
                requested_capabilities_v2 = policy.requested_v2.as_dict()
                admin_capabilities_v2 = policy.admin_v2.as_dict()
                effective_capabilities_v2 = policy.effective_v2.as_dict()
            else:
                raise ValueError("ToolContractSnapshot contract_version 非法")
        elif contract_version not in {1, 2}:
            raise ValueError("ToolContractSnapshot contract_version 非法")
        return cls(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            requested_permission=requested_permission or spec.permission,
            effective_permission=spec.permission,
            declared_effect=declared_effect or spec.effect,
            effective_effect=spec.effect,
            timeout_seconds=(
                float(spec.timeout_seconds)
                if spec.timeout_seconds is not None
                else None
            ),
            result_limit=spec.result_limit,
            dependencies=spec.dependencies,
            requested_capabilities=requested_capabilities,
            admin_capabilities=admin_capabilities,
            effective_capabilities=effective_capabilities,
            contract_version=contract_version,
            capability_schema_version=capability_schema_version,
            capability_detector_version=capability_detector_version,
            detected_capabilities=detected_capabilities,
            requested_capabilities_v2=requested_capabilities_v2,
            admin_capabilities_v2=admin_capabilities_v2,
            effective_capabilities_v2=effective_capabilities_v2,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "description": self.description,
            "parameters": mutable_json(self.parameters),
            "requested_permission": self.requested_permission,
            "effective_permission": self.effective_permission,
            "declared_effect": self.declared_effect.value,
            "effective_effect": self.effective_effect.value,
            "timeout_seconds": self.timeout_seconds,
            "result_limit": self.result_limit,
            "dependencies": list(self.dependencies),
            "requested_capabilities": mutable_json(self.requested_capabilities),
            "admin_capabilities": mutable_json(self.admin_capabilities),
            "effective_capabilities": mutable_json(self.effective_capabilities),
        }
        if self.contract_version == 1:
            return payload
        return {
            "contract_version": self.contract_version,
            **payload,
            "capability_schema_version": self.capability_schema_version,
            "capability_detector_version": self.capability_detector_version,
            "detected_capabilities": mutable_json(self.detected_capabilities),
            "requested_capabilities_v2": mutable_json(
                self.requested_capabilities_v2
            ),
            "admin_capabilities_v2": mutable_json(self.admin_capabilities_v2),
            "effective_capabilities_v2": mutable_json(
                self.effective_capabilities_v2
            ),
        }

    def verify_spec(self, spec: ToolSpec) -> None:
        current = ToolContractSnapshot.from_spec(
            spec,
            requested_permission=self.requested_permission,
            declared_effect=self.declared_effect,
            contract_version=self.contract_version,
        )
        if current.as_dict() != self.as_dict():
            raise ValueError("ToolArtifact ToolSpec 与固化安全契约不一致")

    def __deepcopy__(self, _memo: dict[int, Any]) -> ToolContractSnapshot:
        return self


@dataclass(frozen=True)
class ToolArtifact:
    """Immutable executable source bound to one runtime generation."""

    tool_name: str
    handler_name: str
    source: bytes
    source_hash: str
    schema: Mapping[str, Any]
    spec: ToolSpec
    contract: ToolContractSnapshot
    source_type: str
    generation: int
    filename: str
    tests_source: bytes = b""
    bundle_manifest: Mapping[str, Any] | None = None
    bundle_id: str | None = None
    bundle_digest: str | None = None
    artifact_digest: str = ""
    artifact_version: int | None = None

    def __post_init__(self) -> None:
        if self.source_type not in _SOURCE_TYPES:
            raise ValueError("ToolArtifact source_type 非法")
        if not isinstance(self.spec, ToolSpec):
            raise ValueError("ToolArtifact spec 必须是 ToolSpec")
        if not isinstance(self.contract, ToolContractSnapshot):
            raise ValueError("ToolArtifact contract 非法")
        artifact_version = (
            self.contract.contract_version
            if self.artifact_version is None
            else self.artifact_version
        )
        if type(artifact_version) is not int or artifact_version not in {1, 2}:
            raise ValueError("ToolArtifact artifact_version 非法")
        if artifact_version != self.contract.contract_version:
            raise ValueError("ToolArtifact 与 ToolContractSnapshot 版本不一致")
        object.__setattr__(self, "artifact_version", artifact_version)
        if self.tool_name != self.spec.name or self.tool_name != self.contract.name:
            raise ValueError("ToolArtifact 工具名与安全契约不一致")
        if (
            not _HANDLER_RE.fullmatch(self.handler_name)
            or self.handler_name.startswith("__")
        ):
            raise ValueError("ToolArtifact handler_name 非法")
        if not isinstance(self.source, bytes) or not self.source:
            raise ValueError("ToolArtifact source 必须是非空 bytes")
        if not isinstance(self.tests_source, bytes):
            raise ValueError("ToolArtifact tests_source 必须是 bytes")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("ToolArtifact generation 必须是非负整数")
        if (
            not self.filename
            or PurePath(self.filename).name != self.filename
            or self.filename in {".", ".."}
            or "/" in self.filename
            or "\\" in self.filename
            or "\0" in self.filename
        ):
            raise ValueError("ToolArtifact filename 必须是安全 basename")
        if self.source_type == "generated" and self.filename != "tool.py":
            raise ValueError("Generated ToolArtifact filename 必须是 tool.py")

        normalized_source = normalize_source_bytes(self.source)
        normalized_tests = normalize_source_bytes(self.tests_source)
        object.__setattr__(self, "source", normalized_source)
        object.__setattr__(self, "tests_source", normalized_tests)
        if not _SHA256_RE.fullmatch(self.source_hash):
            raise ValueError("ToolArtifact source_hash 非法")
        if self.source_hash != source_sha256(normalized_source):
            raise ValueError("ToolArtifact source_hash 与源码快照不匹配")

        frozen_schema = _freeze_json(self.schema)
        _canonical_json(mutable_json(frozen_schema))
        object.__setattr__(self, "schema", frozen_schema)
        self.contract.verify_spec(self.spec)
        self._verify_schema_contract()

        if self.source_type == "generated":
            self._bind_generated_manifest()
        elif self.tests_source or any(
            value is not None
            for value in (self.bundle_manifest, self.bundle_id, self.bundle_digest)
        ):
            raise ValueError("Custom ToolArtifact 不得包含 generated bundle 元数据")

        calculated = self._calculate_artifact_digest()
        if self.artifact_digest and self.artifact_digest != calculated:
            raise ValueError("ToolArtifact artifact_digest 不匹配")
        object.__setattr__(self, "artifact_digest", calculated)

    def _verify_schema_contract(self) -> None:
        schema = mutable_json(self.schema)
        if (
            schema.get("name") != self.contract.name
            or schema.get("description") != self.contract.description
            or schema.get("parameters") != mutable_json(self.contract.parameters)
        ):
            raise ValueError("ToolArtifact Schema 与固化安全契约不一致")

    def _bind_generated_manifest(self) -> None:
        if self.bundle_manifest is None or not self.bundle_id:
            raise ValueError("Generated ToolArtifact 缺少 bundle 元数据")
        if not _BUNDLE_ID_RE.fullmatch(self.bundle_id):
            raise ValueError("Generated ToolArtifact bundle_id 非法")
        if not self.bundle_digest or not _SHA256_RE.fullmatch(self.bundle_digest):
            raise ValueError("Generated ToolArtifact bundle_digest 非法")
        frozen_manifest = _freeze_json(self.bundle_manifest)
        object.__setattr__(self, "bundle_manifest", frozen_manifest)
        manifest = mutable_json(frozen_manifest)
        if manifest.get("bundle_id") != self.bundle_id:
            raise ValueError("Generated ToolArtifact bundle_id 与 manifest 不一致")
        tools = manifest.get("tools")
        if not isinstance(tools, list):
            raise ValueError("Generated ToolArtifact manifest.tools 非法")
        matches = [item for item in tools if item.get("name") == self.tool_name]
        if len(matches) != 1:
            raise ValueError("Generated ToolArtifact 未唯一绑定 manifest 工具")
        item = matches[0]
        expected_timeout = float(item.get("timeout_seconds", 30))
        expected_result_limit = int(item.get("result_limit", 6000))
        if (
            item.get("handler") != self.handler_name
            or item.get("description") != self.contract.description
            or item.get("parameters") != mutable_json(self.contract.parameters)
            or item.get("permission") != self.contract.requested_permission
            or item.get("effect") != self.contract.declared_effect.value
            or expected_timeout != self.contract.timeout_seconds
            or expected_result_limit != self.contract.result_limit
            or tuple(item.get("dependencies") or ()) != self.contract.dependencies
        ):
            raise ValueError("Generated ToolArtifact 与 manifest 工具契约不一致")
        manifest_capabilities, manifest_capabilities_v2 = (
            parse_capability_profile(manifest.get("capabilities"))
        )
        if manifest_capabilities.as_dict() != mutable_json(
            self.contract.requested_capabilities
        ):
            raise ValueError("Generated ToolArtifact capability 与 manifest 不一致")
        if self.contract.contract_version == 1 and (
            manifest_capabilities_v2
            != ToolCapabilityV2.from_legacy(manifest_capabilities)
        ):
            raise ValueError(
                "Generated ToolArtifact v1 无法表示 v2 capability policy"
            )
        if self.contract.contract_version == 2 and (
            manifest_capabilities_v2.as_dict()
            != mutable_json(self.contract.requested_capabilities_v2)
        ):
            raise ValueError(
                "Generated ToolArtifact capability v2 与 manifest 不一致"
            )
        if self.bundle_digest != canonical_bundle_digest(
            manifest,
            self.source,
            self.tests_source,
        ):
            raise ValueError("Generated ToolArtifact bundle digest 不匹配")

    def _calculate_artifact_digest(self) -> str:
        metadata = {
            "contract": self.contract.as_dict(),
            "handler_name": self.handler_name,
            "source_hash": self.source_hash,
            "schema": mutable_json(self.schema),
            "source_type": self.source_type,
            "generation": self.generation,
            "filename": self.filename,
            "bundle_id": self.bundle_id,
            "bundle_digest": self.bundle_digest,
        }
        if self.artifact_version == 2:
            metadata["artifact_version"] = self.artifact_version
        manifest = (
            _canonical_json(mutable_json(self.bundle_manifest))
            if self.bundle_manifest is not None
            else b""
        )
        prefix = (
            b"moellm-tool-artifact-v1\0"
            if self.artifact_version == 1
            else b"moellm-tool-artifact-v2\0"
        )
        return hashlib.sha256(
            prefix
            + _canonical_json(metadata)
            + b"\0"
            + self.source
            + b"\0"
            + self.tests_source
            + b"\0"
            + manifest
        ).hexdigest()

    def verify(
        self,
        *,
        expected_artifact_digest: str,
        expected_bundle_digest: str | None,
        generation: int,
    ) -> None:
        """Recompute all digests against the request-pinned snapshot."""

        if self.generation != generation:
            raise ValueError("ToolArtifact 与请求 generation 不匹配")
        if (
            not _SHA256_RE.fullmatch(expected_artifact_digest)
            or self.artifact_digest != expected_artifact_digest
        ):
            raise ValueError("ToolArtifact 与请求固定 artifact digest 不匹配")
        if source_sha256(self.source) != self.source_hash:
            raise ValueError("ToolArtifact 源码摘要校验失败")
        self.contract.verify_spec(self.spec)
        self._verify_schema_contract()
        if self.source_type == "generated":
            if (
                expected_bundle_digest is None
                or expected_bundle_digest != self.bundle_digest
            ):
                raise ValueError("Generated ToolArtifact 与请求固定 bundle digest 不匹配")
            assert self.bundle_manifest is not None
            if canonical_bundle_digest(
                mutable_json(self.bundle_manifest),
                self.source,
                self.tests_source,
            ) != self.bundle_digest:
                raise ValueError("Generated ToolArtifact 运行时 bundle digest 校验失败")
        elif expected_bundle_digest is not None:
            raise ValueError("Custom ToolArtifact 不接受 bundle digest")
        if self._calculate_artifact_digest() != self.artifact_digest:
            raise ValueError("ToolArtifact 运行时摘要校验失败")

    def schema_dict(self) -> dict[str, Any]:
        return mutable_json(self.schema)

    def __deepcopy__(self, _memo: dict[int, Any]) -> ToolArtifact:
        return self

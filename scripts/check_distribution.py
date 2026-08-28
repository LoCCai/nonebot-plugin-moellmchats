#!/usr/bin/env python3
"""Verify protocol resources and forbidden members in wheel/sdist archives."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
from typing import Protocol
from zipfile import ZipFile

RESOURCE_SUFFIXES = (
    "nonebot_plugin_moellmchats/protocol_resources/NOTICE.md",
    "nonebot_plugin_moellmchats/protocol_resources/__init__.py",
    "nonebot_plugin_moellmchats/protocol_resources/actions.json",
    "nonebot_plugin_moellmchats/protocol_resources/policies.json",
    "nonebot_plugin_moellmchats/protocol_resources/sources.json",
)


class DistributionCheckError(RuntimeError):
    """A built archive is incomplete or contains forbidden workspace data."""


class ArchiveReader(Protocol):
    def names(self) -> tuple[str, ...]: ...

    def read(self, name: str) -> bytes: ...


class WheelReader:
    def __init__(self, path: Path) -> None:
        self._archive = ZipFile(path)

    def names(self) -> tuple[str, ...]:
        return tuple(self._archive.namelist())

    def read(self, name: str) -> bytes:
        return self._archive.read(name)

    def close(self) -> None:
        self._archive.close()


class SdistReader:
    def __init__(self, path: Path) -> None:
        self._archive = tarfile.open(path, mode="r:gz")

    def names(self) -> tuple[str, ...]:
        return tuple(member.name for member in self._archive.getmembers() if member.isfile())

    def read(self, name: str) -> bytes:
        member = self._archive.getmember(name)
        handle = self._archive.extractfile(member)
        if handle is None:
            raise DistributionCheckError(f"archive member is not readable: {name}")
        return handle.read()

    def close(self) -> None:
        self._archive.close()


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resource_name(names: tuple[str, ...], suffix: str) -> str:
    matches = [name for name in names if name == suffix or name.endswith(f"/{suffix}")]
    if len(matches) != 1:
        raise DistributionCheckError(f"expected exactly one {suffix}, found {matches!r}")
    return matches[0]


def _json_resource(reader: ArchiveReader, names: tuple[str, ...], suffix: str) -> dict[str, object]:
    value = json.loads(reader.read(_resource_name(names, suffix)).decode("utf-8"))
    if not isinstance(value, dict):
        raise DistributionCheckError(f"archive JSON root is not an object: {suffix}")
    return value


def check_archive(path: Path) -> None:
    if path.suffix == ".whl":
        reader: WheelReader | SdistReader = WheelReader(path)
    elif path.name.endswith(".tar.gz"):
        reader = SdistReader(path)
    else:
        raise DistributionCheckError(f"unsupported archive type: {path}")
    try:
        names = reader.names()
        for name in names:
            parts = PurePosixPath(name).parts
            if "uv.lock" in parts or "__pycache__" in parts or name.endswith((".pyc", ".pyo")):
                raise DistributionCheckError(f"forbidden archive member: {name}")
        for suffix in RESOURCE_SUFFIXES:
            resource_name = _resource_name(names, suffix)
            if not reader.read(resource_name):
                raise DistributionCheckError(f"empty protocol resource: {resource_name}")
        if path.name.endswith(".tar.gz"):
            generator_name = _resource_name(names, "scripts/generate_protocol_manifests.py")
            if not reader.read(generator_name):
                raise DistributionCheckError("sdist protocol generator is empty")

        inventory = _json_resource(
            reader,
            names,
            "nonebot_plugin_moellmchats/protocol_resources/actions.json",
        )
        policy = _json_resource(
            reader,
            names,
            "nonebot_plugin_moellmchats/protocol_resources/policies.json",
        )
        sources = _json_resource(
            reader,
            names,
            "nonebot_plugin_moellmchats/protocol_resources/sources.json",
        )
        actions = inventory.get("actions")
        policies = policy.get("policies")
        wrappers = policy.get("wrappers")
        if not isinstance(actions, list) or len(actions) != 244:
            raise DistributionCheckError("packaged protocol action inventory is incomplete")
        if not isinstance(policies, list) or len(policies) != 244:
            raise DistributionCheckError("packaged protocol policies are incomplete")
        if not isinstance(wrappers, list) or len(wrappers) != 3:
            raise DistributionCheckError("packaged protocol wrappers are incomplete")
        if inventory.get("actions_sha256") != _canonical_digest(actions):
            raise DistributionCheckError("packaged action digest mismatch")
        if policy.get("action_inventory_sha256") != inventory.get("actions_sha256"):
            raise DistributionCheckError("packaged policy/action binding mismatch")
        if policy.get("policies_sha256") != _canonical_digest(policies):
            raise DistributionCheckError("packaged policy digest mismatch")
        if policy.get("wrappers_sha256") != _canonical_digest(wrappers):
            raise DistributionCheckError("packaged wrapper digest mismatch")
        if inventory.get("source_lock_sha256") != _canonical_digest(sources):
            raise DistributionCheckError("packaged source-lock digest mismatch")
        source_entries = sources.get("sources")
        if not isinstance(source_entries, dict):
            raise DistributionCheckError("packaged source lock is incomplete")
        expected_commits = {
            "napcat_docs": "14ad6896579abf17c761cdf8d9dfb7c3ea396305",
            "nonebot_adapter_onebot": "3ac943fc4470d851219f368cacadf3dcdd649ee7",
            "onebot_v11": "d4456ee706f9ada9c2dfde56a2bcfc69752600e4",
            "onebot_v12": "d533f0fca3bd14781d4461776dba8d907d9de253",
        }
        if {
            name: entry.get("commit") if isinstance(entry, dict) else None for name, entry in source_entries.items()
        } != expected_commits:
            raise DistributionCheckError("packaged pinned source commits drifted")
        notice_name = _resource_name(
            names,
            "nonebot_plugin_moellmchats/protocol_resources/NOTICE.md",
        )
        notice = reader.read(notice_name).decode("utf-8")
        notice_markers = {
            "MIT License",
            "Copyright (c) 2021 OneBot Community",
            "Copyright (c) 2021 NoneBot",
            "Copyright (c) 2024 NapCat",
            "Permission is hereby granted",
            "905ff1faa265cdfa",
        }
        if any(marker not in notice for marker in notice_markers):
            raise DistributionCheckError("packaged protocol attribution is incomplete")
    finally:
        reader.close()
    print(f"DISTRIBUTION_CONTENT_OK {path}")  # noqa: T201 - release-check evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    return parser.parse_args()


def main() -> int:
    for path in parse_args().archives:
        check_archive(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

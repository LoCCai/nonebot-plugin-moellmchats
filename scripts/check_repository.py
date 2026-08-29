#!/usr/bin/env python3
"""Run offline repository, documentation, dependency, and manifest checks."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # pyright: ignore[reportMissingImports]


ROOT = Path(__file__).resolve().parents[1]
DOC_FILES = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))
RESOURCE_DIR = ROOT / "nonebot_plugin_moellmchats" / "protocol_resources"
FENCE_RE = re.compile(r"^```(json|toml|python)\s*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^]]*]\((<[^>]+>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
HTML_LINK_RE = re.compile(r"\b(?:href|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)
HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
REMOTE_SCHEME_RE = re.compile(r"^(?:https?|mailto|data):", re.IGNORECASE)


class RepositoryCheckError(RuntimeError):
    """An offline release prerequisite is inconsistent."""


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _check_snippets() -> dict[str, int]:
    counts = {"json": 0, "toml": 0, "python": 0}
    for path in DOC_FILES:
        text = path.read_text(encoding="utf-8")
        for match in FENCE_RE.finditer(text):
            language, source = match.groups()
            if language == "python" and (ROOT / "docs" / "规划") in path.parents:
                # Architecture plans deliberately use Python-labelled
                # pseudocode. User-facing Python examples remain executable.
                continue
            counts[language] += 1
            try:
                if language == "json":
                    json.loads(source)
                elif language == "toml":
                    tomllib.loads(source)
                else:
                    ast.parse(source, filename=str(path))
            except (SyntaxError, ValueError, json.JSONDecodeError) as error:
                relative = path.relative_to(ROOT)
                raise RepositoryCheckError(f"invalid {language} example in {relative}: {error}") from error
    minimum = {"json": 11, "toml": 8, "python": 8}
    if any(counts[name] < expected for name, expected in minimum.items()):
        raise RepositoryCheckError(f"documentation example coverage regressed: {counts!r}")
    return counts


def _heading_slug(value: str) -> str:
    value = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[`*~]", "", value).strip().lower()
    result: list[str] = []
    for character in value:
        if character.isspace():
            result.append("-")
        elif character in {"-", "_"}:
            result.append(character)
        elif unicodedata.category(character)[0] in {"L", "N"}:
            result.append(character)
    return "".join(result)


def _anchors(path: Path) -> frozenset[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING_RE.match(line)
        if match is None:
            continue
        base = _heading_slug(match.group(1))
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return frozenset(anchors)


def _local_targets(text: str) -> tuple[str, ...]:
    without_fences = re.sub(r"^```.*?^```\s*$", "", text, flags=re.MULTILINE | re.DOTALL)
    markdown = [match.group(1).strip("<>") for match in MARKDOWN_LINK_RE.finditer(without_fences)]
    html = [match.group(1) for match in HTML_LINK_RE.finditer(without_fences)]
    return (*markdown, *html)


def _check_links() -> tuple[int, int]:
    checked_files = 0
    checked_links = 0
    anchor_cache: dict[Path, frozenset[str]] = {}
    for source in DOC_FILES:
        file_links = 0
        for target in _local_targets(source.read_text(encoding="utf-8")):
            if REMOTE_SCHEME_RE.match(target) or target.startswith("//"):
                continue
            decoded = unquote(target)
            path_part, separator, fragment = decoded.partition("#")
            path_part = path_part.split("?", 1)[0]
            destination = (source.parent / path_part).resolve() if path_part else source.resolve()
            if destination.is_dir():
                destination /= "README.md"
            try:
                destination.relative_to(ROOT)
            except ValueError as error:
                raise RepositoryCheckError(f"local link escapes repository: {source.relative_to(ROOT)} -> {target}") from error
            if not destination.is_file():
                raise RepositoryCheckError(f"missing local link: {source.relative_to(ROOT)} -> {target}")
            if separator and fragment and destination.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(destination, _anchors(destination))
                if fragment not in anchors:
                    raise RepositoryCheckError(f"missing Markdown anchor: {source.relative_to(ROOT)} -> {target}")
            file_links += 1
            checked_links += 1
        if file_links:
            checked_files += 1
    if checked_links < 100:
        raise RepositoryCheckError(f"local Markdown link coverage regressed: {checked_links}")
    return checked_files, checked_links


def _check_dependencies() -> tuple[int, int]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    poetry = project.get("tool", {}).get("poetry", {})
    runtime = poetry.get("dependencies", {})
    development = poetry.get("group", {}).get("dev", {}).get("dependencies", {})
    if not isinstance(runtime, dict) or not isinstance(development, dict):
        raise RepositoryCheckError("pyproject Poetry dependency tables are missing")
    expected_runtime = {
        "aiohttp",
        "alembic",
        "asyncpg",
        "mcp",
        "nonebot-adapter-onebot",
        "nonebot-plugin-localstore",
        "nonebot2",
        "python-dotenv",
        "redis",
        "sqlalchemy",
        "tomli",
        "ujson",
    }
    if set(runtime) - {"python"} != expected_runtime:
        raise RepositoryCheckError(f"runtime dependency set drifted: {sorted(set(runtime) - {'python'})}")
    expected_development = {
        "build",
        "fakeredis",
        "filelock",
        "nonemoji",
        "pre-commit",
        "pyright",
        "pytest",
        "pytest-asyncio",
        "ruff",
        "twine",
    }
    if set(development) != expected_development:
        raise RepositoryCheckError(f"development dependency set drifted: {sorted(development)}")
    dependency_document = (ROOT / "docs" / "dependencies.md").read_text(encoding="utf-8")
    for name in sorted(expected_runtime | expected_development):
        if re.search(rf"`{re.escape(name)}(?:`|[^A-Za-z0-9_-])", dependency_document) is None:
            raise RepositoryCheckError(f"dependency is undocumented: {name}")
    includes = poetry.get("include")
    if not isinstance(includes, list) or not any(
        isinstance(item, dict)
        and item.get("path") == "nonebot_plugin_moellmchats/protocol_resources"
        and set(item.get("format", ())) == {"sdist", "wheel"}
        for item in includes
    ):
        raise RepositoryCheckError("wheel/sdist protocol resource include is missing")
    if poetry.get("version") != "0.26.3":
        raise RepositoryCheckError("package version is not 0.26.3")
    return len(expected_runtime), len(expected_development)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RepositoryCheckError(f"JSON root must be an object: {path.relative_to(ROOT)}")
    return value


def _check_protocol_resources() -> tuple[int, int, int]:
    inventory = _load_json(RESOURCE_DIR / "actions.json")
    policy = _load_json(RESOURCE_DIR / "policies.json")
    sources = _load_json(RESOURCE_DIR / "sources.json")
    actions = inventory.get("actions")
    policies = policy.get("policies")
    wrappers = policy.get("wrappers")
    if not isinstance(actions, list) or not isinstance(policies, list) or not isinstance(wrappers, list):
        raise RepositoryCheckError("protocol inventory or policy lists are missing")
    if inventory.get("counts") != {"napcat_v11": 175, "onebot_v11": 38, "onebot_v12": 31}:
        raise RepositoryCheckError("protocol action counts drifted")
    if len(actions) != 244 or len(policies) != 244 or len(wrappers) != 3:
        raise RepositoryCheckError("protocol resource cardinality drifted")
    if inventory.get("actions_sha256") != _canonical_digest(actions):
        raise RepositoryCheckError("protocol action digest drifted")
    if policy.get("action_inventory_sha256") != inventory.get("actions_sha256"):
        raise RepositoryCheckError("protocol policy inventory binding drifted")
    if policy.get("policies_sha256") != _canonical_digest(policies):
        raise RepositoryCheckError("protocol policy digest drifted")
    if policy.get("wrappers_sha256") != _canonical_digest(wrappers):
        raise RepositoryCheckError("protocol wrapper digest drifted")
    if inventory.get("source_lock_sha256") != _canonical_digest(sources):
        raise RepositoryCheckError("protocol source-lock digest drifted")

    sys.path.insert(0, str(ROOT))
    from scripts.generate_protocol_manifests import _render_action_reference

    expected_reference = _render_action_reference(inventory, policy)
    actual_reference = (ROOT / "docs" / "protocol-actions.md").read_text(encoding="utf-8")
    if actual_reference != expected_reference:
        raise RepositoryCheckError("generated protocol action reference is stale")
    notice = (RESOURCE_DIR / "NOTICE.md").read_text(encoding="utf-8")
    notice_markers = {
        "MIT License",
        "Copyright (c) 2021 OneBot Community",
        "Copyright (c) 2021 NoneBot",
        "Copyright (c) 2024 NapCat",
        "Permission is hereby granted",
        "905ff1faa265cdfa",
    }
    if any(marker not in notice for marker in notice_markers):
        raise RepositoryCheckError("protocol source attribution is incomplete")
    return len(actions), len(policies), len(wrappers)


def main() -> int:
    snippets = _check_snippets()
    files, links = _check_links()
    runtime_dependencies, development_dependencies = _check_dependencies()
    actions, policies, wrappers = _check_protocol_resources()
    print(f"DOC_SNIPPETS_OK {snippets}")  # noqa: T201 - release-check evidence
    print(f"MARKDOWN_LINKS_OK files={files} local_links={links}")  # noqa: T201 - release-check evidence
    print(  # noqa: T201 - release-check evidence
        f"DEPENDENCIES_OK runtime={runtime_dependencies} development={development_dependencies}"
    )
    print(  # noqa: T201 - release-check evidence
        f"PROTOCOL_RESOURCES_OK actions={actions} policies={policies} wrappers={wrappers}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RepositoryCheckError) as error:
        print(f"repository check failed: {error}", file=sys.stderr)  # noqa: T201 - maintainer diagnostic
        raise SystemExit(1) from error

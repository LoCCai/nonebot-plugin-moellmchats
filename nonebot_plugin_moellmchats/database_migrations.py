from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import re
from typing import TextIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

_OFFLINE_ONLY_ATTRIBUTE = "moellmchats_offline_only"
_REVISION_SELECTOR_MAX_CHARS = 128
_REVISION_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REQUIRED_MIGRATION_FILES = (
    "README.md",
    "env.py",
    "script.py.mako",
    "versions/0001_users_conversations.py",
    "versions/0002_agent_runtime.py",
    "versions/0003_agent_steps.py",
    "versions/0004_tool_calls.py",
    "versions/README.md",
)


class DatabaseMigrationError(RuntimeError):
    """Base error for the explicit, offline-only migration boundary."""


class DatabaseMigrationConfigurationError(DatabaseMigrationError):
    """The packaged migration environment or revision graph is invalid."""


class DatabaseMigrationOnlineDisabledError(DatabaseMigrationError):
    """Online migration execution is intentionally unavailable."""


@dataclass(frozen=True)
class DatabaseMigrationGraph:
    """A deterministic snapshot of the packaged linear Alembic graph."""

    revisions: tuple[str, ...]
    bases: tuple[str, ...]
    heads: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, values in (
            ("revisions", self.revisions),
            ("bases", self.bases),
            ("heads", self.heads),
        ):
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value for value in values):
                raise TypeError(f"{label} 必须是字符串 tuple")
            if len(values) != len(set(values)):
                raise ValueError(f"{label} 不得包含重复 revision")
            if any(not _REVISION_SELECTOR_RE.fullmatch(value) for value in values):
                raise ValueError(f"{label} 包含不安全的 revision 标识")
        if len(self.bases) > 1 or len(self.heads) > 1:
            raise ValueError("数据库 migration graph 必须保持单一线性 base/head")
        if not self.revisions:
            if self.bases or self.heads:
                raise ValueError("空 migration graph 不得声明 base/head")
        elif (
            len(self.bases) != 1
            or len(self.heads) != 1
            or self.bases[0] not in self.revisions
            or self.heads[0] not in self.revisions
        ):
            raise ValueError("非空 migration graph 必须声明图内唯一 base/head")

    @property
    def revision_count(self) -> int:
        return len(self.revisions)

    @property
    def empty(self) -> bool:
        return not self.revisions

    def as_dict(self) -> dict[str, bool | int | list[str]]:
        """Return a fresh JSON-primitive snapshot for local diagnostics."""

        return {
            "revision_count": self.revision_count,
            "empty": self.empty,
            "revisions": list(self.revisions),
            "bases": list(self.bases),
            "heads": list(self.heads),
        }


def database_migration_script_location() -> Path:
    """Resolve and validate the migration files shipped inside the package."""

    location = Path(__file__).resolve().with_name("migrations")
    if not location.is_dir():
        raise DatabaseMigrationConfigurationError("打包的 Alembic migration 目录不存在")
    missing = tuple(relative for relative in _REQUIRED_MIGRATION_FILES if not (location / relative).is_file())
    if missing:
        raise DatabaseMigrationConfigurationError("打包的 Alembic migration 文件不完整")
    return location


def build_offline_alembic_config(*, output_buffer: TextIO | None = None) -> Config:
    """Build an in-memory Alembic config with no URL or secret lookup."""

    location = database_migration_script_location()
    config = Config(
        file_=None,
        output_buffer=output_buffer,
        stdout=StringIO(),
        attributes={_OFFLINE_ONLY_ATTRIBUTE: True},
    )
    config.set_main_option("script_location", str(location).replace("%", "%%"))
    config.set_main_option("version_path_separator", "os")
    return config


def inspect_database_migration_graph() -> DatabaseMigrationGraph:
    """Load the packaged revision graph without running an Alembic environment."""

    try:
        scripts = ScriptDirectory.from_config(build_offline_alembic_config())
        newest_first = tuple(scripts.walk_revisions(base="base", head="heads"))
        heads = tuple(scripts.get_heads())
        bases = tuple(scripts.get_bases())
    except CommandError as error:
        raise DatabaseMigrationConfigurationError(f"无法解析数据库 migration graph ({type(error).__name__})") from None

    for revision in newest_first:
        if not _REVISION_SELECTOR_RE.fullmatch(revision.revision):
            raise DatabaseMigrationConfigurationError("数据库 migration revision 标识不安全")
        if isinstance(revision.down_revision, tuple):
            raise DatabaseMigrationConfigurationError("数据库 migration graph 不允许 merge revision")
        if revision.branch_labels or revision.dependencies:
            raise DatabaseMigrationConfigurationError("数据库 migration graph 不允许 branch label 或 depends_on")
    if len(bases) > 1 or len(heads) > 1:
        raise DatabaseMigrationConfigurationError("数据库 migration graph 必须保持单一线性 base/head")

    revisions = tuple(revision.revision for revision in reversed(newest_first))
    return DatabaseMigrationGraph(revisions=revisions, bases=bases, heads=heads)


def _validate_revision_selector(
    value: object,
    *,
    label: str,
    allowed_symbols: frozenset[str],
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _REVISION_SELECTOR_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} 必须是有限长度的安全 revision 标识")
    if value not in allowed_symbols and not _REVISION_SELECTOR_RE.fullmatch(value):
        raise ValueError(f"{label} 必须是单一 revision 标识")
    return value


def render_offline_upgrade_sql(
    *,
    starting_revision: str = "base",
    target_revision: str = "heads",
) -> str:
    """Render an explicit upgrade range without reading a DSN or opening a connection."""

    starting_revision = _validate_revision_selector(
        starting_revision,
        label="starting_revision",
        allowed_symbols=frozenset({"base"}),
    )
    target_revision = _validate_revision_selector(
        target_revision,
        label="target_revision",
        allowed_symbols=frozenset({"head", "heads"}),
    )
    graph = inspect_database_migration_graph()
    known_revisions = frozenset(graph.revisions)
    if starting_revision != "base" and starting_revision not in known_revisions:
        raise DatabaseMigrationConfigurationError("starting_revision 不在打包的 migration graph 中")
    if target_revision not in {"head", "heads"} and target_revision not in known_revisions:
        raise DatabaseMigrationConfigurationError("target_revision 不在打包的 migration graph 中")
    if graph.empty:
        return ""

    revision_range = target_revision if starting_revision == "base" else f"{starting_revision}:{target_revision}"
    output = StringIO()
    try:
        command.upgrade(
            build_offline_alembic_config(output_buffer=output),
            revision_range,
            sql=True,
        )
    except CommandError as error:
        raise DatabaseMigrationConfigurationError(f"无法生成离线数据库 migration SQL ({type(error).__name__})") from None
    return output.getvalue()

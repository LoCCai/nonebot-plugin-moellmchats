from __future__ import annotations

import ast
from io import StringIO
from pathlib import Path
import shutil
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

from nonebot_plugin_moellmchats.database_metadata import (
    database_metadata,
    database_naming_convention,
)
from nonebot_plugin_moellmchats.database_migrations import (
    DatabaseMigrationConfigurationError,
    DatabaseMigrationGraph,
    DatabaseMigrationOnlineDisabledError,
    build_offline_alembic_config,
    database_migration_script_location,
    inspect_database_migration_graph,
    render_offline_upgrade_sql,
)


def test_database_metadata_starts_empty_with_deterministic_names() -> None:
    expected = {
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }

    convention = database_naming_convention()
    assert convention == expected
    assert database_metadata.naming_convention == expected
    assert not database_metadata.tables

    convention["pk"] = "changed"
    assert database_naming_convention() == expected


def test_packaged_migration_layout_is_complete() -> None:
    location = database_migration_script_location()

    assert location.is_absolute()
    assert location.name == "migrations"
    assert (location / "README.md").is_file()
    assert (location / "env.py").is_file()
    assert (location / "script.py.mako").is_file()
    assert (location / "versions" / "README.md").is_file()


def test_offline_config_has_no_database_url_or_external_file() -> None:
    output = StringIO()
    config = build_offline_alembic_config(output_buffer=output)

    assert config.config_file_name is None
    assert config.output_buffer is output
    assert config.get_main_option("sqlalchemy.url", default=None) is None
    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location) == database_migration_script_location()
    assert config.attributes == {"moellmchats_offline_only": True}


def test_empty_migration_graph_is_linear_and_returns_fresh_diagnostics() -> None:
    graph = inspect_database_migration_graph()
    diagnostics = graph.as_dict()

    assert graph == DatabaseMigrationGraph(revisions=(), bases=(), heads=())
    assert graph.revision_count == 0
    assert graph.empty is True
    assert diagnostics == {
        "revision_count": 0,
        "empty": True,
        "revisions": [],
        "bases": [],
        "heads": [],
    }
    diagnostics["revisions"].append("changed")  # type: ignore[union-attr]
    assert graph.as_dict()["revisions"] == []


def test_migration_graph_value_object_accepts_consistent_nonempty_snapshot() -> None:
    graph = DatabaseMigrationGraph(
        revisions=("0001_users", "0002_agent"),
        bases=("0001_users",),
        heads=("0002_agent",),
    )

    assert graph.empty is False
    assert graph.revision_count == 2


@pytest.mark.parametrize(
    "values",
    [
        {"revisions": (), "bases": ("0001",), "heads": ()},
        {"revisions": ("0001",), "bases": (), "heads": ("0001",)},
        {"revisions": ("0001",), "bases": ("missing",), "heads": ("0001",)},
        {"revisions": ("0001",), "bases": ("0001",), "heads": ("missing",)},
        {"revisions": ("0001", "0001"), "bases": ("0001",), "heads": ("0001",)},
        {"revisions": ("bad:revision",), "bases": ("bad:revision",), "heads": ("bad:revision",)},
    ],
)
def test_migration_graph_value_object_rejects_inconsistent_snapshots(values: dict[str, tuple[str, ...]]) -> None:
    with pytest.raises(ValueError, match=r"migration graph|重复 revision|revision 标识"):
        DatabaseMigrationGraph(**values)


def test_revision_template_generates_valid_python_in_temporary_copy(tmp_path: Path) -> None:
    temporary_location = tmp_path / "migrations"
    shutil.copytree(database_migration_script_location(), temporary_location)
    config = build_offline_alembic_config()
    config.set_main_option("script_location", str(temporary_location).replace("%", "%%"))
    scripts = ScriptDirectory.from_config(config)

    revision = scripts.generate_revision(
        revid="0001_example",
        message="example",
        head="head",
    )

    assert revision is not None
    generated_path = Path(revision.path)
    assert generated_path.parent == temporary_location / "versions"
    source = generated_path.read_text(encoding="utf-8")
    ast.parse(source)
    assert revision.revision == "0001_example"
    assert revision.down_revision is None
    assert "down_revision: str | Sequence[str] | None = None" in source


def test_offline_upgrade_renders_without_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_connection(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("offline migration attempted to create a database engine")

    monkeypatch.setattr("sqlalchemy.create_engine", unexpected_connection)
    monkeypatch.setattr("sqlalchemy.engine.create.create_engine", unexpected_connection)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", unexpected_connection)

    assert render_offline_upgrade_sql() == ""
    direct_output = StringIO()
    command.upgrade(
        build_offline_alembic_config(output_buffer=direct_output),
        "heads",
        sql=True,
    )
    assert direct_output.getvalue() == ""


def test_online_upgrade_fails_closed_before_engine_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_connection(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("online-disabled migration attempted to create a database engine")

    monkeypatch.setattr("sqlalchemy.create_engine", unexpected_connection)
    monkeypatch.setattr("sqlalchemy.engine.create.create_engine", unexpected_connection)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", unexpected_connection)

    with pytest.raises(DatabaseMigrationOnlineDisabledError, match="禁止在线 migration"):
        command.upgrade(build_offline_alembic_config(), "head", sql=False)


def test_external_alembic_config_cannot_bypass_offline_marker() -> None:
    config = Config(file_=None, output_buffer=StringIO(), stdout=StringIO())
    config.set_main_option(
        "script_location",
        str(database_migration_script_location()).replace("%", "%%"),
    )

    with pytest.raises(DatabaseMigrationConfigurationError, match="显式离线配置"):
        command.upgrade(config, "head", sql=True)


def test_offline_environment_rejects_database_url() -> None:
    config = build_offline_alembic_config(output_buffer=StringIO())
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://user:secret@database/private")

    with pytest.raises(DatabaseMigrationConfigurationError, match=r"不得包含 sqlalchemy\.url") as error:
        command.upgrade(config, "head", sql=True)

    rendered = str(error.value)
    for secret in ("user", "secret", "database", "private"):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("starting_revision", None),
        ("starting_revision", ""),
        ("starting_revision", "head:base"),
        ("starting_revision", "x" * 129),
        ("target_revision", None),
        ("target_revision", ""),
        ("target_revision", "base:heads"),
        ("target_revision", "revision\nleak"),
    ],
)
def test_offline_upgrade_rejects_unsafe_revision_selectors(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        render_offline_upgrade_sql(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("starting_revision", "0001_missing"),
        ("target_revision", "0001_missing"),
    ],
)
def test_offline_upgrade_rejects_unknown_revision_selectors(field: str, value: str) -> None:
    with pytest.raises(DatabaseMigrationConfigurationError, match=field):
        render_offline_upgrade_sql(**{field: value})

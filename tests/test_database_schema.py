from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from nonebot_plugin_moellmchats.database_migrations import (
    build_offline_alembic_config,
)
import nonebot_plugin_moellmchats.database_schema as database_schema_module
from nonebot_plugin_moellmchats.database_schema import (
    DATABASE_TABLES,
    conversations_table,
    database_metadata,
    messages_table,
    users_table,
)

if TYPE_CHECKING:
    import pytest


def _normalize_sql(value: object) -> str:
    return " ".join(str(value).split())


def _column_signature(column: sa.Column[Any]) -> tuple[object, ...]:
    default = column.server_default
    default_sql = None
    if default is not None and column.identity is None:
        default_sql = _normalize_sql(getattr(default, "arg", default))
    return (
        column.name,
        type(column.type).__name__,
        getattr(column.type, "length", None),
        getattr(column.type, "timezone", None),
        column.nullable,
        column.identity is not None,
        default_sql,
    )


def _constraint_signature(constraint: sa.Constraint) -> tuple[object, ...]:
    if isinstance(constraint, sa.PrimaryKeyConstraint):
        return ("primary_key", constraint.name, tuple(constraint.columns.keys()))
    if isinstance(constraint, sa.UniqueConstraint):
        return ("unique", constraint.name, tuple(constraint.columns.keys()))
    if isinstance(constraint, sa.ForeignKeyConstraint):
        return (
            "foreign_key",
            constraint.name,
            tuple(constraint.columns.keys()),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
    if isinstance(constraint, sa.CheckConstraint):
        return ("check", constraint.name, _normalize_sql(constraint.sqltext))
    raise AssertionError(f"unexpected constraint type: {type(constraint).__name__}")


def _index_signature(index: sa.Index) -> tuple[object, ...]:
    dialect = postgresql.dialect()
    expressions = tuple(
        _normalize_sql(
            cast("Any", expression).compile(
                dialect=dialect,
                compile_kwargs={"include_table": False},
            )
        )
        for expression in index.expressions
    )
    where: Any = index.dialect_options["postgresql"].get("where")
    where_sql = None
    if where is not None:
        where_sql = _normalize_sql(
            where.compile(
                dialect=dialect,
                compile_kwargs={"include_table": False},
            )
        )
    return (index.name, expressions, bool(index.unique), where_sql)


def _table_signature(table: sa.Table) -> tuple[object, ...]:
    return (
        tuple(_column_signature(column) for column in table.columns),
        frozenset(_constraint_signature(constraint) for constraint in table.constraints),
        frozenset(_index_signature(index) for index in table.indexes),
    )


class _MigrationRecorder:
    def __init__(self) -> None:
        self.metadata = sa.MetaData()

    def create_table(self, name: str, *elements: Any, **kwargs: Any) -> sa.Table:
        assert not kwargs
        return sa.Table(name, self.metadata, *elements)

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: tuple[Any, ...],
        *,
        unique: bool = False,
        **kwargs: Any,
    ) -> sa.Index:
        table = self.metadata.tables[table_name]
        expressions: tuple[Any, ...] = tuple(table.c[column] if isinstance(column, str) else column for column in columns)
        return sa.Index(name, *expressions, unique=unique, **kwargs)


def test_first_schema_has_exact_tables_and_columns() -> None:
    assert DATABASE_TABLES == (users_table, conversations_table, messages_table)
    assert tuple(database_metadata.tables) == ("users", "conversations", "messages")
    assert all(table.metadata is database_metadata for table in DATABASE_TABLES)

    assert tuple(_column_signature(column) for column in users_table.columns) == (
        ("id", "String", 128, None, False, False, None),
        ("platform", "String", 32, None, False, False, None),
        ("platform_user_id", "String", 128, None, False, False, None),
        ("display_name", "String", 255, None, True, False, None),
        ("created_at", "DateTime", None, True, False, False, "CURRENT_TIMESTAMP"),
        ("updated_at", "DateTime", None, True, False, False, "CURRENT_TIMESTAMP"),
    )
    assert tuple(_column_signature(column) for column in conversations_table.columns) == (
        ("id", "String", 128, None, False, False, None),
        ("type", "String", 32, None, False, False, None),
        ("platform", "String", 32, None, False, False, None),
        ("group_id", "String", 128, None, True, False, None),
        ("user_id", "String", 128, None, True, False, None),
        ("created_at", "DateTime", None, True, False, False, "CURRENT_TIMESTAMP"),
        ("updated_at", "DateTime", None, True, False, False, "CURRENT_TIMESTAMP"),
        ("last_message_at", "DateTime", None, True, True, False, None),
    )
    assert tuple(_column_signature(column) for column in messages_table.columns) == (
        ("id", "BigInteger", None, None, False, True, None),
        ("conversation_id", "String", 128, None, False, False, None),
        ("platform_message_id", "String", 128, None, True, False, None),
        ("role", "String", 32, None, False, False, None),
        ("sender_id", "String", 128, None, True, False, None),
        ("content", "Text", None, None, True, False, None),
        ("structured_content", "JSONB", None, None, True, False, None),
        ("created_at", "DateTime", None, True, False, False, "CURRENT_TIMESTAMP"),
    )
    assert isinstance(messages_table.c.structured_content.type, postgresql.JSONB)
    assert isinstance(messages_table.c.id.identity, sa.Identity)
    assert messages_table.c.id.identity.always is False


def test_first_schema_has_exact_constraints_and_indexes() -> None:
    assert frozenset(_constraint_signature(value) for value in users_table.constraints) == frozenset(
        {
            ("primary_key", "pk_users", ("id",)),
            ("unique", "uq_users_platform_platform_user_id", ("platform", "platform_user_id")),
            ("check", "ck_users_id_present", "char_length(id) > 0"),
            ("check", "ck_users_platform_present", "char_length(platform) > 0"),
            ("check", "ck_users_platform_user_id_present", "char_length(platform_user_id) > 0"),
            (
                "check",
                "ck_users_display_name_present",
                "display_name IS NULL OR char_length(display_name) > 0",
            ),
            ("check", "ck_users_timestamp_order", "updated_at >= created_at"),
        }
    )
    assert frozenset(_constraint_signature(value) for value in conversations_table.constraints) == frozenset(
        {
            ("primary_key", "pk_conversations", ("id",)),
            (
                "foreign_key",
                "fk_conversations_user_id_users",
                ("user_id",),
                ("users.id",),
                "RESTRICT",
            ),
            ("check", "ck_conversations_id_present", "char_length(id) > 0"),
            ("check", "ck_conversations_type_present", "char_length(type) > 0"),
            ("check", "ck_conversations_platform_present", "char_length(platform) > 0"),
            (
                "check",
                "ck_conversations_group_id_present",
                "group_id IS NULL OR char_length(group_id) > 0",
            ),
            (
                "check",
                "ck_conversations_user_id_present",
                "user_id IS NULL OR char_length(user_id) > 0",
            ),
            ("check", "ck_conversations_scope_present", "group_id IS NOT NULL OR user_id IS NOT NULL"),
            ("check", "ck_conversations_timestamp_order", "updated_at >= created_at"),
        }
    )
    assert frozenset(_constraint_signature(value) for value in messages_table.constraints) == frozenset(
        {
            ("primary_key", "pk_messages", ("id",)),
            (
                "foreign_key",
                "fk_messages_conversation_id_conversations",
                ("conversation_id",),
                ("conversations.id",),
                "RESTRICT",
            ),
            (
                "foreign_key",
                "fk_messages_sender_id_users",
                ("sender_id",),
                ("users.id",),
                "RESTRICT",
            ),
            (
                "check",
                "ck_messages_platform_message_id_present",
                "platform_message_id IS NULL OR char_length(platform_message_id) > 0",
            ),
            ("check", "ck_messages_role_present", "char_length(role) > 0"),
            (
                "check",
                "ck_messages_sender_id_present",
                "sender_id IS NULL OR char_length(sender_id) > 0",
            ),
            (
                "check",
                "ck_messages_payload_present",
                "content IS NOT NULL OR structured_content IS NOT NULL",
            ),
        }
    )
    assert not users_table.indexes
    assert frozenset(_index_signature(value) for value in conversations_table.indexes) == frozenset(
        {
            (
                "uq_conversations_platform_type_group_id",
                ("platform", "type", "group_id"),
                True,
                "group_id IS NOT NULL",
            ),
            (
                "uq_conversations_platform_type_user_id",
                ("platform", "type", "user_id"),
                True,
                "group_id IS NULL AND user_id IS NOT NULL",
            ),
        }
    )
    assert frozenset(_index_signature(value) for value in messages_table.indexes) == frozenset(
        {
            (
                "ix_messages_conversation_id_id_desc",
                ("conversation_id", "id DESC"),
                False,
                None,
            ),
            ("ix_messages_created_at", ("created_at",), False, None),
            (
                "uq_messages_conversation_id_platform_message_id",
                ("conversation_id", "platform_message_id"),
                True,
                "platform_message_id IS NOT NULL",
            ),
        }
    )


def test_revision_operations_are_identical_to_declared_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _MigrationRecorder()
    scripts = ScriptDirectory.from_config(build_offline_alembic_config())
    revision = scripts.get_revision("0001_users_conversations")
    assert revision is not None
    module = revision.module
    monkeypatch.setattr(module.op, "create_table", recorder.create_table)
    monkeypatch.setattr(module.op, "create_index", recorder.create_index)

    module.upgrade()

    assert module.revision == "0001_users_conversations"
    assert module.down_revision is None
    assert module.branch_labels is None
    assert module.depends_on is None
    assert tuple(recorder.metadata.tables) == tuple(database_metadata.tables)
    for table_name, table in database_metadata.tables.items():
        assert _table_signature(recorder.metadata.tables[table_name]) == _table_signature(table)


def test_schema_declaration_has_no_engine_session_or_connection() -> None:
    forbidden_runtime_objects = (Engine, AsyncEngine, Session, AsyncSession)

    assert not any(isinstance(value, forbidden_runtime_objects) for value in vars(database_schema_module).values())
    assert "DatabaseEngineManager" not in vars(database_schema_module)

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from alembic import op as alembic_op
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from nonebot_plugin_moellmchats.agent_runtime import (
    AgentRunState,
    AgentStepStatus,
    AgentStepType,
)
from nonebot_plugin_moellmchats.database_migrations import (
    build_offline_alembic_config,
)
import nonebot_plugin_moellmchats.database_schema as database_schema_module
from nonebot_plugin_moellmchats.database_schema import (
    AGENT_RUN_STATUS_VALUES,
    AGENT_RUN_TERMINAL_STATUS_VALUES,
    AGENT_STEP_ERROR_STATUS_VALUES,
    AGENT_STEP_STATUS_VALUES,
    AGENT_STEP_TERMINAL_STATUS_VALUES,
    AGENT_STEP_TYPE_VALUES,
    DATABASE_TABLES,
    agent_runs_table,
    agent_steps_table,
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
        _normalize_sql(column.type.compile(dialect=postgresql.dialect())),
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
    assert DATABASE_TABLES == (
        users_table,
        conversations_table,
        messages_table,
        agent_runs_table,
        agent_steps_table,
    )
    assert tuple(database_metadata.tables) == (
        "users",
        "conversations",
        "messages",
        "agent_runs",
        "agent_steps",
    )
    assert all(table.metadata is database_metadata for table in DATABASE_TABLES)

    assert tuple(_column_signature(column) for column in users_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("platform", "VARCHAR(32)", False, False, None),
        ("platform_user_id", "VARCHAR(128)", False, False, None),
        ("display_name", "VARCHAR(255)", True, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
    )
    assert tuple(_column_signature(column) for column in conversations_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("type", "VARCHAR(32)", False, False, None),
        ("platform", "VARCHAR(32)", False, False, None),
        ("group_id", "VARCHAR(128)", True, False, None),
        ("user_id", "VARCHAR(128)", True, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
        ("last_message_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
    )
    assert tuple(_column_signature(column) for column in messages_table.columns) == (
        ("id", "BIGINT", False, True, None),
        ("conversation_id", "VARCHAR(128)", False, False, None),
        ("platform_message_id", "VARCHAR(128)", True, False, None),
        ("role", "VARCHAR(32)", False, False, None),
        ("sender_id", "VARCHAR(128)", True, False, None),
        ("content", "TEXT", True, False, None),
        ("structured_content", "JSONB", True, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
    )
    assert isinstance(messages_table.c.structured_content.type, postgresql.JSONB)
    assert isinstance(messages_table.c.id.identity, sa.Identity)
    assert messages_table.c.id.identity.always is False


def test_agent_run_schema_has_exact_columns_and_domain_states() -> None:
    assert (
        AGENT_RUN_STATUS_VALUES
        == tuple(state.value for state in AgentRunState)
        == (
            "created",
            "admitted",
            "classifying",
            "planning",
            "executing",
            "waiting_confirmation",
            "summarizing",
            "completed",
            "failed",
            "cancelled",
            "timed_out",
            "rejected",
        )
    )
    assert AGENT_RUN_TERMINAL_STATUS_VALUES == (
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "rejected",
    )
    assert tuple(_column_signature(column) for column in agent_runs_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("request_id", "BIGINT", False, False, None),
        ("user_id", "VARCHAR(128)", False, False, None),
        ("group_id", "VARCHAR(128)", True, False, None),
        ("conversation_id", "VARCHAR(128)", False, False, None),
        ("generation", "BIGINT", False, False, None),
        ("model", "VARCHAR(255)", True, False, None),
        ("status", "VARCHAR(32)", False, False, None),
        ("started_at", "TIMESTAMP WITH TIME ZONE", False, False, None),
        ("finished_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
        ("input_tokens", "BIGINT", True, False, None),
        ("output_tokens", "BIGINT", True, False, None),
        ("cost", "NUMERIC(24, 12)", True, False, None),
        ("error_type", "VARCHAR(128)", True, False, None),
        ("error_message", "TEXT", True, False, None),
    )
    assert isinstance(agent_runs_table.c.cost.type, sa.Numeric)
    assert agent_runs_table.c.cost.type.precision == 24
    assert agent_runs_table.c.cost.type.scale == 12


def test_agent_step_schema_has_exact_columns_and_domain_states() -> None:
    assert (
        AGENT_STEP_TYPE_VALUES
        == tuple(value.value for value in AgentStepType)
        == (
            "classification",
            "model",
            "tool",
            "summary",
            "vision",
            "confirmation",
            "memory",
        )
    )
    assert (
        AGENT_STEP_STATUS_VALUES
        == tuple(value.value for value in AgentStepStatus)
        == (
            "pending",
            "running",
            "completed",
            "failed",
            "cancelled",
            "timed_out",
            "skipped",
        )
    )
    assert AGENT_STEP_TERMINAL_STATUS_VALUES == (
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "skipped",
    )
    assert AGENT_STEP_ERROR_STATUS_VALUES == (
        "failed",
        "cancelled",
        "timed_out",
        "skipped",
    )
    assert tuple(_column_signature(column) for column in agent_steps_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("run_id", "VARCHAR(128)", False, False, None),
        ("step_index", "BIGINT", False, False, None),
        ("step_type", "VARCHAR(32)", False, False, None),
        ("model", "VARCHAR(255)", True, False, None),
        ("tool_name", "VARCHAR(64)", True, False, None),
        ("status", "VARCHAR(32)", False, False, None),
        ("started_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
        ("finished_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
        ("duration_ms", "BIGINT", True, False, None),
        ("input_preview", "TEXT", True, False, None),
        ("output_preview", "TEXT", True, False, None),
        ("error", "TEXT", True, False, None),
    )


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


def test_agent_run_schema_has_exact_constraints_and_indexes() -> None:
    status_sql = ", ".join(f"'{value}'" for value in AGENT_RUN_STATUS_VALUES)
    terminal_status_sql = ", ".join(f"'{value}'" for value in AGENT_RUN_TERMINAL_STATUS_VALUES)
    finish_status_check = (
        f"(status IN ({terminal_status_sql}) AND finished_at IS NOT NULL) OR "
        f"(status NOT IN ({terminal_status_sql}) AND finished_at IS NULL)"
    )

    assert frozenset(_constraint_signature(value) for value in agent_runs_table.constraints) == frozenset(
        {
            ("primary_key", "pk_agent_runs", ("id",)),
            (
                "foreign_key",
                "fk_agent_runs_user_id_users",
                ("user_id",),
                ("users.id",),
                "RESTRICT",
            ),
            (
                "foreign_key",
                "fk_agent_runs_conversation_id_conversations",
                ("conversation_id",),
                ("conversations.id",),
                "RESTRICT",
            ),
            ("check", "ck_agent_runs_id_present", "char_length(id) > 0"),
            ("check", "ck_agent_runs_request_id_positive", "request_id > 0"),
            ("check", "ck_agent_runs_user_id_present", "char_length(user_id) > 0"),
            (
                "check",
                "ck_agent_runs_group_id_present",
                "group_id IS NULL OR char_length(group_id) > 0",
            ),
            (
                "check",
                "ck_agent_runs_conversation_id_present",
                "char_length(conversation_id) > 0",
            ),
            ("check", "ck_agent_runs_generation_nonnegative", "generation >= 0"),
            (
                "check",
                "ck_agent_runs_model_present",
                "model IS NULL OR char_length(model) > 0",
            ),
            ("check", "ck_agent_runs_status_valid", f"status IN ({status_sql})"),
            ("check", "ck_agent_runs_finish_matches_status", finish_status_check),
            (
                "check",
                "ck_agent_runs_timestamp_order",
                "finished_at IS NULL OR finished_at >= started_at",
            ),
            (
                "check",
                "ck_agent_runs_input_tokens_nonnegative",
                "input_tokens IS NULL OR input_tokens >= 0",
            ),
            (
                "check",
                "ck_agent_runs_output_tokens_nonnegative",
                "output_tokens IS NULL OR output_tokens >= 0",
            ),
            (
                "check",
                "ck_agent_runs_cost_nonnegative",
                "cost IS NULL OR cost >= 0",
            ),
            (
                "check",
                "ck_agent_runs_error_type_present",
                "error_type IS NULL OR char_length(error_type) > 0",
            ),
            (
                "check",
                "ck_agent_runs_error_message_present",
                "error_message IS NULL OR char_length(error_message) > 0",
            ),
        }
    )
    assert frozenset(_index_signature(value) for value in agent_runs_table.indexes) == frozenset(
        {
            (
                "ix_agent_runs_conversation_id_started_at_id_desc",
                ("conversation_id", "started_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_agent_runs_user_id_started_at",
                ("user_id", "started_at DESC"),
                False,
                None,
            ),
            (
                "ix_agent_runs_status_started_at",
                ("status", "started_at"),
                False,
                None,
            ),
        }
    )


def test_agent_step_schema_has_exact_constraints_and_index() -> None:
    step_type_sql = ", ".join(f"'{value}'" for value in AGENT_STEP_TYPE_VALUES)
    status_sql = ", ".join(f"'{value}'" for value in AGENT_STEP_STATUS_VALUES)
    terminal_status_sql = ", ".join(f"'{value}'" for value in AGENT_STEP_TERMINAL_STATUS_VALUES)
    error_status_sql = ", ".join(f"'{value}'" for value in AGENT_STEP_ERROR_STATUS_VALUES)
    lifecycle_check = (
        "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL AND duration_ms IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND duration_ms IS NULL) OR "
        f"(status IN ({terminal_status_sql}) AND started_at IS NOT NULL "
        "AND finished_at IS NOT NULL AND duration_ms IS NOT NULL)"
    )

    assert frozenset(_constraint_signature(value) for value in agent_steps_table.constraints) == frozenset(
        {
            ("primary_key", "pk_agent_steps", ("id",)),
            (
                "foreign_key",
                "fk_agent_steps_run_id_agent_runs",
                ("run_id",),
                ("agent_runs.id",),
                "RESTRICT",
            ),
            ("unique", "uq_agent_steps_run_id_step_index", ("run_id", "step_index")),
            ("check", "ck_agent_steps_id_present", "char_length(id) > 0"),
            ("check", "ck_agent_steps_run_id_present", "char_length(run_id) > 0"),
            ("check", "ck_agent_steps_index_nonnegative", "step_index >= 0"),
            ("check", "ck_agent_steps_type_valid", f"step_type IN ({step_type_sql})"),
            (
                "check",
                "ck_agent_steps_model_present",
                "model IS NULL OR char_length(model) > 0",
            ),
            (
                "check",
                "ck_agent_steps_tool_name_present",
                "tool_name IS NULL OR char_length(tool_name) > 0",
            ),
            (
                "check",
                "ck_agent_steps_type_identity",
                "(step_type <> 'model' OR model IS NOT NULL) AND (step_type <> 'tool' OR tool_name IS NOT NULL)",
            ),
            ("check", "ck_agent_steps_status_valid", f"status IN ({status_sql})"),
            ("check", "ck_agent_steps_lifecycle_fields", lifecycle_check),
            (
                "check",
                "ck_agent_steps_timestamp_order",
                "finished_at IS NULL OR finished_at >= started_at",
            ),
            (
                "check",
                "ck_agent_steps_duration_nonnegative",
                "duration_ms IS NULL OR duration_ms >= 0",
            ),
            (
                "check",
                "ck_agent_steps_input_preview_bounded",
                "input_preview IS NULL OR (char_length(input_preview) > 0 AND char_length(input_preview) <= 6000)",
            ),
            (
                "check",
                "ck_agent_steps_output_preview_bounded",
                "output_preview IS NULL OR (char_length(output_preview) > 0 AND char_length(output_preview) <= 6000)",
            ),
            (
                "check",
                "ck_agent_steps_output_matches_status",
                f"status IN ({terminal_status_sql}) OR output_preview IS NULL",
            ),
            (
                "check",
                "ck_agent_steps_error_bounded",
                "error IS NULL OR (char_length(error) > 0 AND char_length(error) <= 6000)",
            ),
            (
                "check",
                "ck_agent_steps_error_matches_status",
                f"status IN ({error_status_sql}) OR error IS NULL",
            ),
        }
    )
    assert not agent_steps_table.indexes


def test_linear_revision_operations_are_identical_to_declared_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _MigrationRecorder()
    scripts = ScriptDirectory.from_config(build_offline_alembic_config())
    monkeypatch.setattr(alembic_op, "create_table", recorder.create_table)
    monkeypatch.setattr(alembic_op, "create_index", recorder.create_index)

    for revision_id, down_revision in (
        ("0001_users_conversations", None),
        ("0002_agent_runtime", "0001_users_conversations"),
        ("0003_agent_steps", "0002_agent_runtime"),
    ):
        revision = scripts.get_revision(revision_id)
        assert revision is not None
        module = revision.module
        assert module.op is alembic_op

        module.upgrade()

        assert module.revision == revision_id
        assert module.down_revision == down_revision
        assert module.branch_labels is None
        assert module.depends_on is None

    assert tuple(recorder.metadata.tables) == tuple(database_metadata.tables)
    for table_name, table in database_metadata.tables.items():
        assert _table_signature(recorder.metadata.tables[table_name]) == _table_signature(table)


def test_schema_declaration_has_no_engine_session_or_connection() -> None:
    forbidden_runtime_objects = (Engine, AsyncEngine, Session, AsyncSession)

    assert not any(isinstance(value, forbidden_runtime_objects) for value in vars(database_schema_module).values())
    assert "DatabaseEngineManager" not in vars(database_schema_module)

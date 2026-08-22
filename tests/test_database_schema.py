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
    ToolCallStatus,
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
    TOOL_BUNDLE_VERSION_STATE_VALUES,
    TOOL_CALL_STATUS_VALUES,
    TOOL_CALL_TERMINAL_STATUS_VALUES,
    TOOL_SOURCE_VALUES,
    agent_runs_table,
    agent_steps_table,
    audit_events_table,
    conversations_table,
    database_metadata,
    messages_table,
    tool_bundle_versions_table,
    tool_bundles_table,
    tool_calls_table,
    users_table,
)
from nonebot_plugin_moellmchats.generated_tool_lifecycle import VersionState
from nonebot_plugin_moellmchats.tool_providers import ToolSource

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
        expressions: list[Any] = []
        for column in columns:
            if isinstance(column, str):
                expressions.append(table.c[column])
                continue
            text = getattr(column, "text", None)
            if isinstance(text, str) and text.endswith(" DESC"):
                column_name = text.removesuffix(" DESC")
                if column_name in table.c:
                    expressions.append(table.c[column_name].desc())
                    continue
            expressions.append(column)
        return sa.Index(name, *expressions, unique=unique, **kwargs)

    def create_unique_constraint(
        self,
        name: str,
        table_name: str,
        columns: tuple[str, ...],
        **kwargs: Any,
    ) -> sa.UniqueConstraint:
        assert not kwargs
        table = self.metadata.tables[table_name]
        return sa.UniqueConstraint(
            *(table.c[column] for column in columns),
            name=name,
        )

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        referent_table: str,
        local_columns: tuple[str, ...],
        remote_columns: tuple[str, ...],
        **kwargs: Any,
    ) -> sa.ForeignKeyConstraint:
        ondelete = kwargs.pop("ondelete", None)
        assert not kwargs
        constraint = sa.ForeignKeyConstraint(
            tuple(local_columns),
            tuple(f"{referent_table}.{column}" for column in remote_columns),
            name=name,
            ondelete=ondelete,
        )
        self.metadata.tables[source_table].append_constraint(constraint)
        return constraint


def test_first_schema_has_exact_tables_and_columns() -> None:
    assert DATABASE_TABLES == (
        users_table,
        conversations_table,
        messages_table,
        agent_runs_table,
        agent_steps_table,
        tool_calls_table,
        tool_bundles_table,
        tool_bundle_versions_table,
        audit_events_table,
    )
    assert tuple(database_metadata.tables) == (
        "users",
        "conversations",
        "messages",
        "agent_runs",
        "agent_steps",
        "tool_calls",
        "tool_bundles",
        "tool_bundle_versions",
        "audit_events",
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


def test_tool_call_schema_has_exact_columns_and_domain_identities() -> None:
    assert (
        TOOL_CALL_STATUS_VALUES
        == tuple(value.value for value in ToolCallStatus)
        == (
            "pending",
            "waiting_confirmation",
            "running",
            "completed",
            "failed",
            "cancelled",
            "timed_out",
            "rejected",
        )
    )
    assert TOOL_CALL_TERMINAL_STATUS_VALUES == (
        "completed",
        "failed",
        "cancelled",
        "timed_out",
        "rejected",
    )
    assert (
        TOOL_SOURCE_VALUES
        == tuple(value.value for value in ToolSource)
        == (
            "registered",
            "custom_file",
            "generated",
            "mcp",
            "builtin",
            "nonebot_plugin",
        )
    )
    assert tuple(_column_signature(column) for column in tool_calls_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("run_id", "VARCHAR(128)", False, False, None),
        ("step_id", "VARCHAR(128)", False, False, None),
        ("tool_name", "VARCHAR(64)", False, False, None),
        ("tool_source", "VARCHAR(32)", False, False, None),
        ("bundle_id", "VARCHAR(64)", True, False, None),
        ("bundle_digest", "VARCHAR(64)", True, False, None),
        ("arguments_json", "JSONB", False, False, None),
        ("result_preview", "TEXT", True, False, None),
        ("confirmed", "BOOLEAN", False, False, None),
        ("confirmation_id", "VARCHAR(128)", True, False, None),
        ("status", "VARCHAR(32)", False, False, None),
        ("duration_ms", "BIGINT", True, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, None),
        ("finished_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
    )
    assert isinstance(tool_calls_table.c.arguments_json.type, postgresql.JSONB)


def test_tool_bundle_schema_has_exact_columns_and_domain_states() -> None:
    assert (
        TOOL_BUNDLE_VERSION_STATE_VALUES
        == tuple(value.value for value in VersionState)
        == ("approved", "activated", "deprecated", "archived")
    )
    assert tuple(_column_signature(column) for column in tool_bundles_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("bundle_id", "VARCHAR(64)", False, False, None),
        ("description", "TEXT", False, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
        ("updated_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
        ("active_version_id", "VARCHAR(128)", True, False, None),
    )
    assert tuple(_column_signature(column) for column in tool_bundle_versions_table.columns) == (
        ("id", "VARCHAR(128)", False, False, None),
        ("bundle_id", "VARCHAR(64)", False, False, None),
        ("digest", "VARCHAR(64)", False, False, None),
        ("manifest_json", "JSONB", False, False, None),
        ("source", "TEXT", False, False, None),
        ("tests_source", "TEXT", False, False, None),
        ("state", "VARCHAR(32)", False, False, None),
        ("risks_json", "JSONB", False, False, None),
        ("capabilities_json", "JSONB", False, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, None),
        ("approved_at", "TIMESTAMP WITH TIME ZONE", False, False, None),
        ("activated_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
        ("deprecated_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
        ("archived_at", "TIMESTAMP WITH TIME ZONE", True, False, None),
    )
    assert isinstance(tool_bundle_versions_table.c.manifest_json.type, postgresql.JSONB)
    assert isinstance(tool_bundle_versions_table.c.risks_json.type, postgresql.JSONB)
    assert isinstance(tool_bundle_versions_table.c.capabilities_json.type, postgresql.JSONB)


def test_audit_schema_has_exact_columns() -> None:
    assert tuple(_column_signature(column) for column in audit_events_table.columns) == (
        ("id", "BIGINT", False, True, None),
        ("event_type", "VARCHAR(128)", False, False, None),
        ("actor_user_id", "VARCHAR(128)", True, False, None),
        ("actor_type", "VARCHAR(32)", False, False, None),
        ("target_type", "VARCHAR(64)", False, False, None),
        ("target_id", "VARCHAR(128)", False, False, None),
        ("run_id", "VARCHAR(128)", True, False, None),
        ("tool_call_id", "VARCHAR(128)", True, False, None),
        ("metadata_json", "JSONB", False, False, None),
        ("created_at", "TIMESTAMP WITH TIME ZONE", False, False, "CURRENT_TIMESTAMP"),
    )
    assert isinstance(audit_events_table.c.metadata_json.type, postgresql.JSONB)


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
            ("unique", "uq_agent_steps_run_id_id", ("run_id", "id")),
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


def test_tool_call_schema_has_exact_constraints_and_indexes() -> None:
    source_sql = ", ".join(f"'{value}'" for value in TOOL_SOURCE_VALUES)
    status_sql = ", ".join(f"'{value}'" for value in TOOL_CALL_STATUS_VALUES)
    terminal_status_sql = ", ".join(f"'{value}'" for value in TOOL_CALL_TERMINAL_STATUS_VALUES)
    lifecycle_check = (
        f"(status IN ({terminal_status_sql}) AND finished_at IS NOT NULL "
        "AND duration_ms IS NOT NULL) OR "
        f"(status NOT IN ({terminal_status_sql}) AND finished_at IS NULL "
        "AND duration_ms IS NULL)"
    )

    assert frozenset(_constraint_signature(value) for value in tool_calls_table.constraints) == frozenset(
        {
            ("primary_key", "pk_tool_calls", ("id",)),
            ("unique", "uq_tool_calls_run_id_id", ("run_id", "id")),
            (
                "foreign_key",
                "fk_tool_calls_run_id_agent_runs",
                ("run_id",),
                ("agent_runs.id",),
                "RESTRICT",
            ),
            (
                "foreign_key",
                "fk_tool_calls_run_id_step_id_agent_steps",
                ("run_id", "step_id"),
                ("agent_steps.run_id", "agent_steps.id"),
                "RESTRICT",
            ),
            ("check", "ck_tool_calls_id_present", "char_length(id) > 0"),
            ("check", "ck_tool_calls_run_id_present", "char_length(run_id) > 0"),
            ("check", "ck_tool_calls_step_id_present", "char_length(step_id) > 0"),
            ("check", "ck_tool_calls_tool_name_present", "char_length(tool_name) > 0"),
            ("check", "ck_tool_calls_source_valid", f"tool_source IN ({source_sql})"),
            (
                "check",
                "ck_tool_calls_bundle_id_valid",
                "bundle_id IS NULL OR bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
            ),
            (
                "check",
                "ck_tool_calls_bundle_digest_valid",
                "bundle_digest IS NULL OR bundle_digest ~ '^[a-f0-9]{64}$'",
            ),
            (
                "check",
                "ck_tool_calls_bundle_matches_source",
                "(tool_source = 'generated' AND bundle_id IS NOT NULL AND bundle_digest IS NOT NULL) OR "
                "(tool_source <> 'generated' AND bundle_id IS NULL AND bundle_digest IS NULL)",
            ),
            (
                "check",
                "ck_tool_calls_arguments_object",
                "jsonb_typeof(arguments_json) = 'object'",
            ),
            (
                "check",
                "ck_tool_calls_result_preview_bounded",
                "result_preview IS NULL OR (char_length(result_preview) > 0 AND char_length(result_preview) <= 6000)",
            ),
            (
                "check",
                "ck_tool_calls_confirmation_id_present",
                "confirmation_id IS NULL OR char_length(confirmation_id) > 0",
            ),
            (
                "check",
                "ck_tool_calls_confirmed_has_confirmation",
                "NOT confirmed OR confirmation_id IS NOT NULL",
            ),
            (
                "check",
                "ck_tool_calls_waiting_confirmation_fields",
                "status <> 'waiting_confirmation' OR (NOT confirmed AND confirmation_id IS NOT NULL)",
            ),
            ("check", "ck_tool_calls_status_valid", f"status IN ({status_sql})"),
            ("check", "ck_tool_calls_lifecycle_fields", lifecycle_check),
            (
                "check",
                "ck_tool_calls_timestamp_order",
                "finished_at IS NULL OR finished_at >= created_at",
            ),
            (
                "check",
                "ck_tool_calls_duration_nonnegative",
                "duration_ms IS NULL OR duration_ms >= 0",
            ),
            (
                "check",
                "ck_tool_calls_result_matches_status",
                f"status IN ({terminal_status_sql}) OR result_preview IS NULL",
            ),
            (
                "check",
                "ck_tool_calls_completed_has_result",
                "status <> 'completed' OR result_preview IS NOT NULL",
            ),
        }
    )
    assert frozenset(_index_signature(value) for value in tool_calls_table.indexes) == frozenset(
        {
            (
                "ix_tool_calls_run_id_created_at_id_desc",
                ("run_id", "created_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_tool_calls_step_id_created_at",
                ("step_id", "created_at"),
                False,
                None,
            ),
            (
                "ix_tool_calls_status_created_at",
                ("status", "created_at"),
                False,
                None,
            ),
            (
                "uq_tool_calls_confirmation_id",
                ("confirmation_id",),
                True,
                "confirmation_id IS NOT NULL",
            ),
        }
    )


def test_tool_bundle_schema_has_exact_constraints_and_indexes() -> None:
    assert frozenset(_constraint_signature(value) for value in tool_bundles_table.constraints) == frozenset(
        {
            ("primary_key", "pk_tool_bundles", ("id",)),
            ("unique", "uq_tool_bundles_bundle_id", ("bundle_id",)),
            (
                "foreign_key",
                "fk_tool_bundles_active_version",
                ("bundle_id", "active_version_id"),
                (
                    "tool_bundle_versions.bundle_id",
                    "tool_bundle_versions.id",
                ),
                "RESTRICT",
            ),
            ("check", "ck_tool_bundles_id_present", "char_length(id) > 0"),
            (
                "check",
                "ck_tool_bundles_bundle_id_valid",
                "bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
            ),
            (
                "check",
                "ck_tool_bundles_description_bounded",
                "char_length(btrim(description)) > 0 AND octet_length(description) <= 65536",
            ),
            (
                "check",
                "ck_tool_bundles_active_version_id_present",
                "active_version_id IS NULL OR char_length(active_version_id) > 0",
            ),
            (
                "check",
                "ck_tool_bundles_timestamp_order",
                "updated_at >= created_at",
            ),
        }
    )
    assert frozenset(_index_signature(value) for value in tool_bundles_table.indexes) == frozenset(
        {
            (
                "ix_tool_bundles_updated_at_id_desc",
                ("updated_at DESC", "id DESC"),
                False,
                None,
            ),
        }
    )

    state_sql = ", ".join(f"'{value}'" for value in TOOL_BUNDLE_VERSION_STATE_VALUES)
    lifecycle_check = (
        "(state = 'approved' AND activated_at IS NULL AND deprecated_at IS NULL AND archived_at IS NULL) OR "
        "(state = 'activated' AND activated_at IS NOT NULL AND deprecated_at IS NULL AND archived_at IS NULL) OR "
        "(state = 'deprecated' AND activated_at IS NOT NULL AND deprecated_at IS NOT NULL AND archived_at IS NULL) OR "
        "(state = 'archived' AND activated_at IS NOT NULL AND deprecated_at IS NOT NULL AND archived_at IS NOT NULL)"
    )
    timestamp_check = (
        "approved_at >= created_at AND "
        "(activated_at IS NULL OR activated_at >= approved_at) AND "
        "(deprecated_at IS NULL OR deprecated_at >= activated_at) AND "
        "(archived_at IS NULL OR archived_at >= deprecated_at)"
    )
    assert frozenset(_constraint_signature(value) for value in tool_bundle_versions_table.constraints) == frozenset(
        {
            ("primary_key", "pk_tool_bundle_versions", ("id",)),
            (
                "foreign_key",
                "fk_tool_bundle_versions_bundle_id_tool_bundles",
                ("bundle_id",),
                ("tool_bundles.bundle_id",),
                "RESTRICT",
            ),
            (
                "unique",
                "uq_tool_bundle_versions_bundle_id_digest",
                ("bundle_id", "digest"),
            ),
            (
                "unique",
                "uq_tool_bundle_versions_bundle_id_id",
                ("bundle_id", "id"),
            ),
            (
                "check",
                "ck_tool_bundle_versions_id_present",
                "char_length(id) > 0",
            ),
            (
                "check",
                "ck_tool_bundle_versions_bundle_id_valid",
                "bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
            ),
            (
                "check",
                "ck_tool_bundle_versions_digest_valid",
                "digest ~ '^[a-f0-9]{64}$'",
            ),
            (
                "check",
                "ck_tool_bundle_versions_manifest_object",
                "jsonb_typeof(manifest_json) = 'object'",
            ),
            (
                "check",
                "ck_tool_bundle_versions_manifest_identity",
                "manifest_json ? 'bundle_id' AND "
                "jsonb_typeof(manifest_json -> 'bundle_id') = 'string' AND "
                "manifest_json ->> 'bundle_id' = bundle_id",
            ),
            (
                "check",
                "ck_tool_bundle_versions_manifest_bounded",
                "octet_length(manifest_json::text) <= 65536",
            ),
            (
                "check",
                "ck_tool_bundle_versions_source_bounded",
                "octet_length(source) BETWEEN 1 AND 65536",
            ),
            (
                "check",
                "ck_tool_bundle_versions_tests_source_bounded",
                "octet_length(tests_source) BETWEEN 1 AND 65536",
            ),
            (
                "check",
                "ck_tool_bundle_versions_state_valid",
                f"state IN ({state_sql})",
            ),
            (
                "check",
                "ck_tool_bundle_versions_risks_array",
                "jsonb_typeof(risks_json) = 'array' AND octet_length(risks_json::text) <= 65536",
            ),
            (
                "check",
                "ck_tool_bundle_versions_capabilities_object",
                "jsonb_typeof(capabilities_json) = 'object' AND octet_length(capabilities_json::text) <= 65536",
            ),
            (
                "check",
                "ck_tool_bundle_versions_lifecycle_fields",
                lifecycle_check,
            ),
            (
                "check",
                "ck_tool_bundle_versions_timestamp_order",
                timestamp_check,
            ),
        }
    )
    assert frozenset(_index_signature(value) for value in tool_bundle_versions_table.indexes) == frozenset(
        {
            (
                "ix_tool_bundle_versions_bundle_id_created_at_id_desc",
                ("bundle_id", "created_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_tool_bundle_versions_state_created_at",
                ("state", "created_at"),
                False,
                None,
            ),
            (
                "uq_tool_bundle_versions_active_bundle_id",
                ("bundle_id",),
                True,
                "state = 'activated'",
            ),
        }
    )


def test_audit_schema_has_exact_constraints_and_indexes() -> None:
    assert frozenset(_constraint_signature(value) for value in audit_events_table.constraints) == frozenset(
        {
            ("primary_key", "pk_audit_events", ("id",)),
            (
                "foreign_key",
                "fk_audit_events_actor_user_id_users",
                ("actor_user_id",),
                ("users.id",),
                "RESTRICT",
            ),
            (
                "foreign_key",
                "fk_audit_events_run_id_agent_runs",
                ("run_id",),
                ("agent_runs.id",),
                "RESTRICT",
            ),
            (
                "foreign_key",
                "fk_audit_events_run_id_tool_call_id_tool_calls",
                ("run_id", "tool_call_id"),
                ("tool_calls.run_id", "tool_calls.id"),
                "RESTRICT",
            ),
            (
                "check",
                "ck_audit_events_event_type_valid",
                "event_type ~ '^[a-z][a-z0-9_.:-]{0,127}$'",
            ),
            (
                "check",
                "ck_audit_events_actor_user_id_present",
                "actor_user_id IS NULL OR char_length(actor_user_id) > 0",
            ),
            (
                "check",
                "ck_audit_events_actor_type_valid",
                "actor_type ~ '^[a-z][a-z0-9_.:-]{0,31}$'",
            ),
            (
                "check",
                "ck_audit_events_target_type_valid",
                "target_type ~ '^[a-z][a-z0-9_.:-]{0,63}$'",
            ),
            (
                "check",
                "ck_audit_events_target_id_present",
                "char_length(target_id) > 0",
            ),
            (
                "check",
                "ck_audit_events_run_id_present",
                "run_id IS NULL OR char_length(run_id) > 0",
            ),
            (
                "check",
                "ck_audit_events_tool_call_id_present",
                "tool_call_id IS NULL OR char_length(tool_call_id) > 0",
            ),
            (
                "check",
                "ck_audit_events_tool_call_has_run",
                "tool_call_id IS NULL OR run_id IS NOT NULL",
            ),
            (
                "check",
                "ck_audit_events_metadata_object",
                "jsonb_typeof(metadata_json) = 'object'",
            ),
            (
                "check",
                "ck_audit_events_metadata_bounded",
                "octet_length(metadata_json::text) <= 65536",
            ),
        }
    )
    assert frozenset(_index_signature(value) for value in audit_events_table.indexes) == frozenset(
        {
            (
                "ix_audit_events_run_id_created_at_id_desc",
                ("run_id", "created_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_audit_events_tool_call_id_created_at_id_desc",
                ("tool_call_id", "created_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_audit_events_actor_created_at_id_desc",
                ("actor_type", "actor_user_id", "created_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_audit_events_target_created_at_id_desc",
                ("target_type", "target_id", "created_at DESC", "id DESC"),
                False,
                None,
            ),
            (
                "ix_audit_events_event_type_created_at_id_desc",
                ("event_type", "created_at DESC", "id DESC"),
                False,
                None,
            ),
        }
    )


def test_linear_revision_operations_are_identical_to_declared_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _MigrationRecorder()
    scripts = ScriptDirectory.from_config(build_offline_alembic_config())
    monkeypatch.setattr(alembic_op, "create_table", recorder.create_table)
    monkeypatch.setattr(alembic_op, "create_index", recorder.create_index)
    monkeypatch.setattr(
        alembic_op,
        "create_unique_constraint",
        recorder.create_unique_constraint,
    )
    monkeypatch.setattr(
        alembic_op,
        "create_foreign_key",
        recorder.create_foreign_key,
    )

    for revision_id, down_revision in (
        ("0001_users_conversations", None),
        ("0002_agent_runtime", "0001_users_conversations"),
        ("0003_agent_steps", "0002_agent_runtime"),
        ("0004_tool_calls", "0003_agent_steps"),
        ("0005_tool_bundle_metadata", "0004_tool_calls"),
        ("0006_audit_events", "0005_tool_bundle_metadata"),
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

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from .agent_runtime import (
    AgentRunState,
    AgentStepStatus,
    AgentStepType,
    ToolCallStatus,
)
from .database_metadata import database_metadata
from .generated_tool_lifecycle import VersionState
from .tool_providers import ToolSource

ENTITY_ID_MAX_CHARS = 128
PLATFORM_MAX_CHARS = 32
CONVERSATION_TYPE_MAX_CHARS = 32
DISPLAY_NAME_MAX_CHARS = 255
MESSAGE_ROLE_MAX_CHARS = 32
MODEL_NAME_MAX_CHARS = 255
AGENT_RUN_STATUS_MAX_CHARS = 32
AGENT_RUN_ERROR_TYPE_MAX_CHARS = 128
AGENT_RUN_COST_PRECISION = 24
AGENT_RUN_COST_SCALE = 12
AGENT_STEP_TYPE_MAX_CHARS = 32
AGENT_STEP_STATUS_MAX_CHARS = 32
TOOL_NAME_MAX_CHARS = 64
AGENT_STEP_PREVIEW_MAX_CHARS = 6_000
AGENT_STEP_ERROR_MAX_CHARS = 6_000
TOOL_SOURCE_MAX_CHARS = 32
TOOL_CALL_STATUS_MAX_CHARS = 32
BUNDLE_ID_MAX_CHARS = 64
BUNDLE_DIGEST_MAX_CHARS = 64
TOOL_CALL_RESULT_PREVIEW_MAX_CHARS = 6_000
TOOL_BUNDLE_DESCRIPTION_MAX_BYTES = 65_536
TOOL_BUNDLE_SOURCE_MAX_BYTES = 65_536
TOOL_BUNDLE_VERSION_STATE_MAX_CHARS = 32
TOOL_BUNDLE_METADATA_MAX_BYTES = 65_536

AGENT_RUN_STATUS_VALUES = tuple(state.value for state in AgentRunState)
AGENT_RUN_TERMINAL_STATUS_VALUES = (
    AgentRunState.COMPLETED.value,
    AgentRunState.FAILED.value,
    AgentRunState.CANCELLED.value,
    AgentRunState.TIMED_OUT.value,
    AgentRunState.REJECTED.value,
)
AGENT_STEP_TYPE_VALUES = tuple(step_type.value for step_type in AgentStepType)
AGENT_STEP_STATUS_VALUES = tuple(status.value for status in AgentStepStatus)
AGENT_STEP_TERMINAL_STATUS_VALUES = (
    AgentStepStatus.COMPLETED.value,
    AgentStepStatus.FAILED.value,
    AgentStepStatus.CANCELLED.value,
    AgentStepStatus.TIMED_OUT.value,
    AgentStepStatus.SKIPPED.value,
)
AGENT_STEP_ERROR_STATUS_VALUES = (
    AgentStepStatus.FAILED.value,
    AgentStepStatus.CANCELLED.value,
    AgentStepStatus.TIMED_OUT.value,
    AgentStepStatus.SKIPPED.value,
)
TOOL_SOURCE_VALUES = tuple(source.value for source in ToolSource)
TOOL_CALL_STATUS_VALUES = tuple(status.value for status in ToolCallStatus)
TOOL_CALL_TERMINAL_STATUS_VALUES = (
    ToolCallStatus.COMPLETED.value,
    ToolCallStatus.FAILED.value,
    ToolCallStatus.CANCELLED.value,
    ToolCallStatus.TIMED_OUT.value,
    ToolCallStatus.REJECTED.value,
)
TOOL_BUNDLE_VERSION_STATE_VALUES = tuple(state.value for state in VersionState)


def _sql_string_list(values: tuple[str, ...]) -> str:
    if not values or any(not value or "'" in value for value in values):
        raise ValueError("database enum values must be non-empty safe strings")
    return ", ".join(f"'{value}'" for value in values)


users_table = sa.Table(
    "users",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("platform", sa.String(PLATFORM_MAX_CHARS), nullable=False),
    sa.Column("platform_user_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("display_name", sa.String(DISPLAY_NAME_MAX_CHARS), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.PrimaryKeyConstraint("id", name="pk_users"),
    sa.UniqueConstraint(
        "platform",
        "platform_user_id",
        name="uq_users_platform_platform_user_id",
    ),
    sa.CheckConstraint("char_length(id) > 0", name="ck_users_id_present"),
    sa.CheckConstraint("char_length(platform) > 0", name="ck_users_platform_present"),
    sa.CheckConstraint(
        "char_length(platform_user_id) > 0",
        name="ck_users_platform_user_id_present",
    ),
    sa.CheckConstraint(
        "display_name IS NULL OR char_length(display_name) > 0",
        name="ck_users_display_name_present",
    ),
    sa.CheckConstraint("updated_at >= created_at", name="ck_users_timestamp_order"),
)

conversations_table = sa.Table(
    "conversations",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("type", sa.String(CONVERSATION_TYPE_MAX_CHARS), nullable=False),
    sa.Column("platform", sa.String(PLATFORM_MAX_CHARS), nullable=False),
    sa.Column("group_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=True),
    sa.Column("user_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint("id", name="pk_conversations"),
    sa.ForeignKeyConstraint(
        ("user_id",),
        ("users.id",),
        name="fk_conversations_user_id_users",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint("char_length(id) > 0", name="ck_conversations_id_present"),
    sa.CheckConstraint("char_length(type) > 0", name="ck_conversations_type_present"),
    sa.CheckConstraint(
        "char_length(platform) > 0",
        name="ck_conversations_platform_present",
    ),
    sa.CheckConstraint(
        "group_id IS NULL OR char_length(group_id) > 0",
        name="ck_conversations_group_id_present",
    ),
    sa.CheckConstraint(
        "user_id IS NULL OR char_length(user_id) > 0",
        name="ck_conversations_user_id_present",
    ),
    sa.CheckConstraint(
        "group_id IS NOT NULL OR user_id IS NOT NULL",
        name="ck_conversations_scope_present",
    ),
    sa.CheckConstraint(
        "updated_at >= created_at",
        name="ck_conversations_timestamp_order",
    ),
)

sa.Index(
    "uq_conversations_platform_type_group_id",
    conversations_table.c.platform,
    conversations_table.c.type,
    conversations_table.c.group_id,
    unique=True,
    postgresql_where=conversations_table.c.group_id.is_not(None),
)
sa.Index(
    "uq_conversations_platform_type_user_id",
    conversations_table.c.platform,
    conversations_table.c.type,
    conversations_table.c.user_id,
    unique=True,
    postgresql_where=conversations_table.c.group_id.is_(None) & conversations_table.c.user_id.is_not(None),
)

messages_table = sa.Table(
    "messages",
    database_metadata,
    sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
    sa.Column("conversation_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("platform_message_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=True),
    sa.Column("role", sa.String(MESSAGE_ROLE_MAX_CHARS), nullable=False),
    sa.Column("sender_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=True),
    sa.Column("content", sa.Text(), nullable=True),
    sa.Column("structured_content", postgresql.JSONB(), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.PrimaryKeyConstraint("id", name="pk_messages"),
    sa.ForeignKeyConstraint(
        ("conversation_id",),
        ("conversations.id",),
        name="fk_messages_conversation_id_conversations",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ("sender_id",),
        ("users.id",),
        name="fk_messages_sender_id_users",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "platform_message_id IS NULL OR char_length(platform_message_id) > 0",
        name="ck_messages_platform_message_id_present",
    ),
    sa.CheckConstraint("char_length(role) > 0", name="ck_messages_role_present"),
    sa.CheckConstraint(
        "sender_id IS NULL OR char_length(sender_id) > 0",
        name="ck_messages_sender_id_present",
    ),
    sa.CheckConstraint(
        "content IS NOT NULL OR structured_content IS NOT NULL",
        name="ck_messages_payload_present",
    ),
)

sa.Index(
    "ix_messages_conversation_id_id_desc",
    messages_table.c.conversation_id,
    messages_table.c.id.desc(),
)
sa.Index("ix_messages_created_at", messages_table.c.created_at)
sa.Index(
    "uq_messages_conversation_id_platform_message_id",
    messages_table.c.conversation_id,
    messages_table.c.platform_message_id,
    unique=True,
    postgresql_where=messages_table.c.platform_message_id.is_not(None),
)

_agent_run_status_sql = _sql_string_list(AGENT_RUN_STATUS_VALUES)
_agent_run_terminal_status_sql = _sql_string_list(AGENT_RUN_TERMINAL_STATUS_VALUES)

agent_runs_table = sa.Table(
    "agent_runs",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("request_id", sa.BigInteger(), nullable=False),
    sa.Column("user_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("group_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=True),
    sa.Column("conversation_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("generation", sa.BigInteger(), nullable=False),
    sa.Column("model", sa.String(MODEL_NAME_MAX_CHARS), nullable=True),
    sa.Column("status", sa.String(AGENT_RUN_STATUS_MAX_CHARS), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("input_tokens", sa.BigInteger(), nullable=True),
    sa.Column("output_tokens", sa.BigInteger(), nullable=True),
    sa.Column(
        "cost",
        sa.Numeric(AGENT_RUN_COST_PRECISION, AGENT_RUN_COST_SCALE),
        nullable=True,
    ),
    sa.Column("error_type", sa.String(AGENT_RUN_ERROR_TYPE_MAX_CHARS), nullable=True),
    sa.Column("error_message", sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    sa.ForeignKeyConstraint(
        ("user_id",),
        ("users.id",),
        name="fk_agent_runs_user_id_users",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ("conversation_id",),
        ("conversations.id",),
        name="fk_agent_runs_conversation_id_conversations",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint("char_length(id) > 0", name="ck_agent_runs_id_present"),
    sa.CheckConstraint("request_id > 0", name="ck_agent_runs_request_id_positive"),
    sa.CheckConstraint(
        "char_length(user_id) > 0",
        name="ck_agent_runs_user_id_present",
    ),
    sa.CheckConstraint(
        "group_id IS NULL OR char_length(group_id) > 0",
        name="ck_agent_runs_group_id_present",
    ),
    sa.CheckConstraint(
        "char_length(conversation_id) > 0",
        name="ck_agent_runs_conversation_id_present",
    ),
    sa.CheckConstraint("generation >= 0", name="ck_agent_runs_generation_nonnegative"),
    sa.CheckConstraint(
        "model IS NULL OR char_length(model) > 0",
        name="ck_agent_runs_model_present",
    ),
    sa.CheckConstraint(
        f"status IN ({_agent_run_status_sql})",
        name="ck_agent_runs_status_valid",
    ),
    sa.CheckConstraint(
        f"(status IN ({_agent_run_terminal_status_sql}) AND finished_at IS NOT NULL) OR "
        f"(status NOT IN ({_agent_run_terminal_status_sql}) AND finished_at IS NULL)",
        name="ck_agent_runs_finish_matches_status",
    ),
    sa.CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at",
        name="ck_agent_runs_timestamp_order",
    ),
    sa.CheckConstraint(
        "input_tokens IS NULL OR input_tokens >= 0",
        name="ck_agent_runs_input_tokens_nonnegative",
    ),
    sa.CheckConstraint(
        "output_tokens IS NULL OR output_tokens >= 0",
        name="ck_agent_runs_output_tokens_nonnegative",
    ),
    sa.CheckConstraint(
        "cost IS NULL OR cost >= 0",
        name="ck_agent_runs_cost_nonnegative",
    ),
    sa.CheckConstraint(
        "error_type IS NULL OR char_length(error_type) > 0",
        name="ck_agent_runs_error_type_present",
    ),
    sa.CheckConstraint(
        "error_message IS NULL OR char_length(error_message) > 0",
        name="ck_agent_runs_error_message_present",
    ),
)

sa.Index(
    "ix_agent_runs_conversation_id_started_at_id_desc",
    agent_runs_table.c.conversation_id,
    agent_runs_table.c.started_at.desc(),
    agent_runs_table.c.id.desc(),
)
sa.Index(
    "ix_agent_runs_user_id_started_at",
    agent_runs_table.c.user_id,
    agent_runs_table.c.started_at.desc(),
)
sa.Index(
    "ix_agent_runs_status_started_at",
    agent_runs_table.c.status,
    agent_runs_table.c.started_at,
)

_agent_step_type_sql = _sql_string_list(AGENT_STEP_TYPE_VALUES)
_agent_step_status_sql = _sql_string_list(AGENT_STEP_STATUS_VALUES)
_agent_step_terminal_status_sql = _sql_string_list(AGENT_STEP_TERMINAL_STATUS_VALUES)
_agent_step_error_status_sql = _sql_string_list(AGENT_STEP_ERROR_STATUS_VALUES)

agent_steps_table = sa.Table(
    "agent_steps",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("run_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("step_index", sa.BigInteger(), nullable=False),
    sa.Column("step_type", sa.String(AGENT_STEP_TYPE_MAX_CHARS), nullable=False),
    sa.Column("model", sa.String(MODEL_NAME_MAX_CHARS), nullable=True),
    sa.Column("tool_name", sa.String(TOOL_NAME_MAX_CHARS), nullable=True),
    sa.Column("status", sa.String(AGENT_STEP_STATUS_MAX_CHARS), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("duration_ms", sa.BigInteger(), nullable=True),
    sa.Column("input_preview", sa.Text(), nullable=True),
    sa.Column("output_preview", sa.Text(), nullable=True),
    sa.Column("error", sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint("id", name="pk_agent_steps"),
    sa.ForeignKeyConstraint(
        ("run_id",),
        ("agent_runs.id",),
        name="fk_agent_steps_run_id_agent_runs",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "run_id",
        "step_index",
        name="uq_agent_steps_run_id_step_index",
    ),
    sa.UniqueConstraint(
        "run_id",
        "id",
        name="uq_agent_steps_run_id_id",
    ),
    sa.CheckConstraint("char_length(id) > 0", name="ck_agent_steps_id_present"),
    sa.CheckConstraint("char_length(run_id) > 0", name="ck_agent_steps_run_id_present"),
    sa.CheckConstraint("step_index >= 0", name="ck_agent_steps_index_nonnegative"),
    sa.CheckConstraint(
        f"step_type IN ({_agent_step_type_sql})",
        name="ck_agent_steps_type_valid",
    ),
    sa.CheckConstraint(
        "model IS NULL OR char_length(model) > 0",
        name="ck_agent_steps_model_present",
    ),
    sa.CheckConstraint(
        "tool_name IS NULL OR char_length(tool_name) > 0",
        name="ck_agent_steps_tool_name_present",
    ),
    sa.CheckConstraint(
        "(step_type <> 'model' OR model IS NOT NULL) AND (step_type <> 'tool' OR tool_name IS NOT NULL)",
        name="ck_agent_steps_type_identity",
    ),
    sa.CheckConstraint(
        f"status IN ({_agent_step_status_sql})",
        name="ck_agent_steps_status_valid",
    ),
    sa.CheckConstraint(
        "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL AND duration_ms IS NULL) OR "
        "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND duration_ms IS NULL) OR "
        f"(status IN ({_agent_step_terminal_status_sql}) AND started_at IS NOT NULL "
        "AND finished_at IS NOT NULL AND duration_ms IS NOT NULL)",
        name="ck_agent_steps_lifecycle_fields",
    ),
    sa.CheckConstraint(
        "finished_at IS NULL OR finished_at >= started_at",
        name="ck_agent_steps_timestamp_order",
    ),
    sa.CheckConstraint(
        "duration_ms IS NULL OR duration_ms >= 0",
        name="ck_agent_steps_duration_nonnegative",
    ),
    sa.CheckConstraint(
        f"input_preview IS NULL OR (char_length(input_preview) > 0 AND "
        f"char_length(input_preview) <= {AGENT_STEP_PREVIEW_MAX_CHARS})",
        name="ck_agent_steps_input_preview_bounded",
    ),
    sa.CheckConstraint(
        f"output_preview IS NULL OR (char_length(output_preview) > 0 AND "
        f"char_length(output_preview) <= {AGENT_STEP_PREVIEW_MAX_CHARS})",
        name="ck_agent_steps_output_preview_bounded",
    ),
    sa.CheckConstraint(
        f"status IN ({_agent_step_terminal_status_sql}) OR output_preview IS NULL",
        name="ck_agent_steps_output_matches_status",
    ),
    sa.CheckConstraint(
        f"error IS NULL OR (char_length(error) > 0 AND char_length(error) <= {AGENT_STEP_ERROR_MAX_CHARS})",
        name="ck_agent_steps_error_bounded",
    ),
    sa.CheckConstraint(
        f"status IN ({_agent_step_error_status_sql}) OR error IS NULL",
        name="ck_agent_steps_error_matches_status",
    ),
)

_tool_source_sql = _sql_string_list(TOOL_SOURCE_VALUES)
_tool_call_status_sql = _sql_string_list(TOOL_CALL_STATUS_VALUES)
_tool_call_terminal_status_sql = _sql_string_list(TOOL_CALL_TERMINAL_STATUS_VALUES)

tool_calls_table = sa.Table(
    "tool_calls",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("run_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("step_id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("tool_name", sa.String(TOOL_NAME_MAX_CHARS), nullable=False),
    sa.Column("tool_source", sa.String(TOOL_SOURCE_MAX_CHARS), nullable=False),
    sa.Column("bundle_id", sa.String(BUNDLE_ID_MAX_CHARS), nullable=True),
    sa.Column(
        "bundle_digest",
        sa.String(BUNDLE_DIGEST_MAX_CHARS),
        nullable=True,
    ),
    sa.Column("arguments_json", postgresql.JSONB(), nullable=False),
    sa.Column("result_preview", sa.Text(), nullable=True),
    sa.Column("confirmed", sa.Boolean(), nullable=False),
    sa.Column(
        "confirmation_id",
        sa.String(ENTITY_ID_MAX_CHARS),
        nullable=True,
    ),
    sa.Column("status", sa.String(TOOL_CALL_STATUS_MAX_CHARS), nullable=False),
    sa.Column("duration_ms", sa.BigInteger(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint("id", name="pk_tool_calls"),
    sa.ForeignKeyConstraint(
        ("run_id",),
        ("agent_runs.id",),
        name="fk_tool_calls_run_id_agent_runs",
        ondelete="RESTRICT",
    ),
    sa.ForeignKeyConstraint(
        ("run_id", "step_id"),
        ("agent_steps.run_id", "agent_steps.id"),
        name="fk_tool_calls_run_id_step_id_agent_steps",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint("char_length(id) > 0", name="ck_tool_calls_id_present"),
    sa.CheckConstraint(
        "char_length(run_id) > 0",
        name="ck_tool_calls_run_id_present",
    ),
    sa.CheckConstraint(
        "char_length(step_id) > 0",
        name="ck_tool_calls_step_id_present",
    ),
    sa.CheckConstraint(
        "char_length(tool_name) > 0",
        name="ck_tool_calls_tool_name_present",
    ),
    sa.CheckConstraint(
        f"tool_source IN ({_tool_source_sql})",
        name="ck_tool_calls_source_valid",
    ),
    sa.CheckConstraint(
        "bundle_id IS NULL OR bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
        name="ck_tool_calls_bundle_id_valid",
    ),
    sa.CheckConstraint(
        "bundle_digest IS NULL OR bundle_digest ~ '^[a-f0-9]{64}$'",
        name="ck_tool_calls_bundle_digest_valid",
    ),
    sa.CheckConstraint(
        "(tool_source = 'generated' AND bundle_id IS NOT NULL AND bundle_digest IS NOT NULL) OR "
        "(tool_source <> 'generated' AND bundle_id IS NULL AND bundle_digest IS NULL)",
        name="ck_tool_calls_bundle_matches_source",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(arguments_json) = 'object'",
        name="ck_tool_calls_arguments_object",
    ),
    sa.CheckConstraint(
        f"result_preview IS NULL OR (char_length(result_preview) > 0 AND "
        f"char_length(result_preview) <= {TOOL_CALL_RESULT_PREVIEW_MAX_CHARS})",
        name="ck_tool_calls_result_preview_bounded",
    ),
    sa.CheckConstraint(
        "confirmation_id IS NULL OR char_length(confirmation_id) > 0",
        name="ck_tool_calls_confirmation_id_present",
    ),
    sa.CheckConstraint(
        "NOT confirmed OR confirmation_id IS NOT NULL",
        name="ck_tool_calls_confirmed_has_confirmation",
    ),
    sa.CheckConstraint(
        "status <> 'waiting_confirmation' OR (NOT confirmed AND confirmation_id IS NOT NULL)",
        name="ck_tool_calls_waiting_confirmation_fields",
    ),
    sa.CheckConstraint(
        f"status IN ({_tool_call_status_sql})",
        name="ck_tool_calls_status_valid",
    ),
    sa.CheckConstraint(
        f"(status IN ({_tool_call_terminal_status_sql}) AND finished_at IS NOT NULL "
        "AND duration_ms IS NOT NULL) OR "
        f"(status NOT IN ({_tool_call_terminal_status_sql}) AND finished_at IS NULL "
        "AND duration_ms IS NULL)",
        name="ck_tool_calls_lifecycle_fields",
    ),
    sa.CheckConstraint(
        "finished_at IS NULL OR finished_at >= created_at",
        name="ck_tool_calls_timestamp_order",
    ),
    sa.CheckConstraint(
        "duration_ms IS NULL OR duration_ms >= 0",
        name="ck_tool_calls_duration_nonnegative",
    ),
    sa.CheckConstraint(
        f"status IN ({_tool_call_terminal_status_sql}) OR result_preview IS NULL",
        name="ck_tool_calls_result_matches_status",
    ),
    sa.CheckConstraint(
        "status <> 'completed' OR result_preview IS NOT NULL",
        name="ck_tool_calls_completed_has_result",
    ),
)

sa.Index(
    "ix_tool_calls_run_id_created_at_id_desc",
    tool_calls_table.c.run_id,
    tool_calls_table.c.created_at.desc(),
    tool_calls_table.c.id.desc(),
)
sa.Index(
    "ix_tool_calls_step_id_created_at",
    tool_calls_table.c.step_id,
    tool_calls_table.c.created_at,
)
sa.Index(
    "ix_tool_calls_status_created_at",
    tool_calls_table.c.status,
    tool_calls_table.c.created_at,
)
sa.Index(
    "uq_tool_calls_confirmation_id",
    tool_calls_table.c.confirmation_id,
    unique=True,
    postgresql_where=tool_calls_table.c.confirmation_id.is_not(None),
)

tool_bundles_table = sa.Table(
    "tool_bundles",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("bundle_id", sa.String(BUNDLE_ID_MAX_CHARS), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    ),
    sa.Column(
        "active_version_id",
        sa.String(ENTITY_ID_MAX_CHARS),
        nullable=True,
    ),
    sa.PrimaryKeyConstraint("id", name="pk_tool_bundles"),
    sa.UniqueConstraint("bundle_id", name="uq_tool_bundles_bundle_id"),
    sa.ForeignKeyConstraint(
        ("bundle_id", "active_version_id"),
        (
            "tool_bundle_versions.bundle_id",
            "tool_bundle_versions.id",
        ),
        name="fk_tool_bundles_active_version",
        ondelete="RESTRICT",
    ),
    sa.CheckConstraint(
        "char_length(id) > 0",
        name="ck_tool_bundles_id_present",
    ),
    sa.CheckConstraint(
        "bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
        name="ck_tool_bundles_bundle_id_valid",
    ),
    sa.CheckConstraint(
        f"char_length(btrim(description)) > 0 AND octet_length(description) <= {TOOL_BUNDLE_DESCRIPTION_MAX_BYTES}",
        name="ck_tool_bundles_description_bounded",
    ),
    sa.CheckConstraint(
        "active_version_id IS NULL OR char_length(active_version_id) > 0",
        name="ck_tool_bundles_active_version_id_present",
    ),
    sa.CheckConstraint(
        "updated_at >= created_at",
        name="ck_tool_bundles_timestamp_order",
    ),
)

sa.Index(
    "ix_tool_bundles_updated_at_id_desc",
    tool_bundles_table.c.updated_at.desc(),
    tool_bundles_table.c.id.desc(),
)

_tool_bundle_version_state_sql = _sql_string_list(TOOL_BUNDLE_VERSION_STATE_VALUES)

tool_bundle_versions_table = sa.Table(
    "tool_bundle_versions",
    database_metadata,
    sa.Column("id", sa.String(ENTITY_ID_MAX_CHARS), nullable=False),
    sa.Column("bundle_id", sa.String(BUNDLE_ID_MAX_CHARS), nullable=False),
    sa.Column("digest", sa.String(BUNDLE_DIGEST_MAX_CHARS), nullable=False),
    sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
    sa.Column("source", sa.Text(), nullable=False),
    sa.Column("tests_source", sa.Text(), nullable=False),
    sa.Column(
        "state",
        sa.String(TOOL_BUNDLE_VERSION_STATE_MAX_CHARS),
        nullable=False,
    ),
    sa.Column("risks_json", postgresql.JSONB(), nullable=False),
    sa.Column("capabilities_json", postgresql.JSONB(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint("id", name="pk_tool_bundle_versions"),
    sa.ForeignKeyConstraint(
        ("bundle_id",),
        ("tool_bundles.bundle_id",),
        name="fk_tool_bundle_versions_bundle_id_tool_bundles",
        ondelete="RESTRICT",
    ),
    sa.UniqueConstraint(
        "bundle_id",
        "digest",
        name="uq_tool_bundle_versions_bundle_id_digest",
    ),
    sa.UniqueConstraint(
        "bundle_id",
        "id",
        name="uq_tool_bundle_versions_bundle_id_id",
    ),
    sa.CheckConstraint(
        "char_length(id) > 0",
        name="ck_tool_bundle_versions_id_present",
    ),
    sa.CheckConstraint(
        "bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
        name="ck_tool_bundle_versions_bundle_id_valid",
    ),
    sa.CheckConstraint(
        "digest ~ '^[a-f0-9]{64}$'",
        name="ck_tool_bundle_versions_digest_valid",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(manifest_json) = 'object'",
        name="ck_tool_bundle_versions_manifest_object",
    ),
    sa.CheckConstraint(
        "manifest_json ? 'bundle_id' AND "
        "jsonb_typeof(manifest_json -> 'bundle_id') = 'string' AND "
        "manifest_json ->> 'bundle_id' = bundle_id",
        name="ck_tool_bundle_versions_manifest_identity",
    ),
    sa.CheckConstraint(
        f"octet_length(manifest_json::text) <= {TOOL_BUNDLE_METADATA_MAX_BYTES}",
        name="ck_tool_bundle_versions_manifest_bounded",
    ),
    sa.CheckConstraint(
        f"octet_length(source) BETWEEN 1 AND {TOOL_BUNDLE_SOURCE_MAX_BYTES}",
        name="ck_tool_bundle_versions_source_bounded",
    ),
    sa.CheckConstraint(
        f"octet_length(tests_source) BETWEEN 1 AND {TOOL_BUNDLE_SOURCE_MAX_BYTES}",
        name="ck_tool_bundle_versions_tests_source_bounded",
    ),
    sa.CheckConstraint(
        f"state IN ({_tool_bundle_version_state_sql})",
        name="ck_tool_bundle_versions_state_valid",
    ),
    sa.CheckConstraint(
        f"jsonb_typeof(risks_json) = 'array' AND octet_length(risks_json::text) <= {TOOL_BUNDLE_METADATA_MAX_BYTES}",
        name="ck_tool_bundle_versions_risks_array",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(capabilities_json) = 'object' AND "
        f"octet_length(capabilities_json::text) <= {TOOL_BUNDLE_METADATA_MAX_BYTES}",
        name="ck_tool_bundle_versions_capabilities_object",
    ),
    sa.CheckConstraint(
        "(state = 'approved' AND activated_at IS NULL AND deprecated_at IS NULL AND archived_at IS NULL) OR "
        "(state = 'activated' AND activated_at IS NOT NULL AND deprecated_at IS NULL AND archived_at IS NULL) OR "
        "(state = 'deprecated' AND activated_at IS NOT NULL AND deprecated_at IS NOT NULL AND archived_at IS NULL) OR "
        "(state = 'archived' AND activated_at IS NOT NULL AND deprecated_at IS NOT NULL AND archived_at IS NOT NULL)",
        name="ck_tool_bundle_versions_lifecycle_fields",
    ),
    sa.CheckConstraint(
        "approved_at >= created_at AND "
        "(activated_at IS NULL OR activated_at >= approved_at) AND "
        "(deprecated_at IS NULL OR deprecated_at >= activated_at) AND "
        "(archived_at IS NULL OR archived_at >= deprecated_at)",
        name="ck_tool_bundle_versions_timestamp_order",
    ),
)

sa.Index(
    "ix_tool_bundle_versions_bundle_id_created_at_id_desc",
    tool_bundle_versions_table.c.bundle_id,
    tool_bundle_versions_table.c.created_at.desc(),
    tool_bundle_versions_table.c.id.desc(),
)
sa.Index(
    "ix_tool_bundle_versions_state_created_at",
    tool_bundle_versions_table.c.state,
    tool_bundle_versions_table.c.created_at,
)
sa.Index(
    "uq_tool_bundle_versions_active_bundle_id",
    tool_bundle_versions_table.c.bundle_id,
    unique=True,
    postgresql_where=sa.text("state = 'activated'"),
)

DATABASE_TABLES = (
    users_table,
    conversations_table,
    messages_table,
    agent_runs_table,
    agent_steps_table,
    tool_calls_table,
    tool_bundles_table,
    tool_bundle_versions_table,
)

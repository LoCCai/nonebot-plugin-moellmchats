from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from .agent_runtime import AgentRunState
from .database_metadata import database_metadata

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

AGENT_RUN_STATUS_VALUES = tuple(state.value for state in AgentRunState)
AGENT_RUN_TERMINAL_STATUS_VALUES = (
    AgentRunState.COMPLETED.value,
    AgentRunState.FAILED.value,
    AgentRunState.CANCELLED.value,
    AgentRunState.TIMED_OUT.value,
    AgentRunState.REJECTED.value,
)


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

DATABASE_TABLES = (
    users_table,
    conversations_table,
    messages_table,
    agent_runs_table,
)

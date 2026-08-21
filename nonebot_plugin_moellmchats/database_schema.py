from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from .database_metadata import database_metadata

ENTITY_ID_MAX_CHARS = 128
PLATFORM_MAX_CHARS = 32
CONVERSATION_TYPE_MAX_CHARS = 32
DISPLAY_NAME_MAX_CHARS = 255
MESSAGE_ROLE_MAX_CHARS = 32

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

DATABASE_TABLES = (
    users_table,
    conversations_table,
    messages_table,
)

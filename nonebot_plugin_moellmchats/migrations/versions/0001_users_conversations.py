"""Create users, conversations, and messages.

Revision ID: 0001_users_conversations
Revises:
Create Date: 2026-08-21 08:05:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_users_conversations"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("platform_user_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "display_name IS NULL OR char_length(display_name) > 0",
            name="ck_users_display_name_present",
        ),
        sa.CheckConstraint("char_length(id) > 0", name="ck_users_id_present"),
        sa.CheckConstraint("char_length(platform) > 0", name="ck_users_platform_present"),
        sa.CheckConstraint(
            "char_length(platform_user_id) > 0",
            name="ck_users_platform_user_id_present",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="ck_users_timestamp_order"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint(
            "platform",
            "platform_user_id",
            name="uq_users_platform_platform_user_id",
        ),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("group_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "group_id IS NULL OR char_length(group_id) > 0",
            name="ck_conversations_group_id_present",
        ),
        sa.CheckConstraint(
            "char_length(id) > 0",
            name="ck_conversations_id_present",
        ),
        sa.CheckConstraint(
            "char_length(platform) > 0",
            name="ck_conversations_platform_present",
        ),
        sa.CheckConstraint(
            "group_id IS NOT NULL OR user_id IS NOT NULL",
            name="ck_conversations_scope_present",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_conversations_timestamp_order",
        ),
        sa.CheckConstraint(
            "char_length(type) > 0",
            name="ck_conversations_type_present",
        ),
        sa.CheckConstraint(
            "user_id IS NULL OR char_length(user_id) > 0",
            name="ck_conversations_user_id_present",
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name="fk_conversations_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
    )
    op.create_index(
        "uq_conversations_platform_type_group_id",
        "conversations",
        ("platform", "type", "group_id"),
        unique=True,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_index(
        "uq_conversations_platform_type_user_id",
        "conversations",
        ("platform", "type", "user_id"),
        unique=True,
        postgresql_where=sa.text("group_id IS NULL AND user_id IS NOT NULL"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("platform_message_id", sa.String(length=128), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("sender_id", sa.String(length=128), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("structured_content", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content IS NOT NULL OR structured_content IS NOT NULL",
            name="ck_messages_payload_present",
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
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
    )
    op.create_index(
        "ix_messages_conversation_id_id_desc",
        "messages",
        ("conversation_id", sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_messages_created_at",
        "messages",
        ("created_at",),
        unique=False,
    )
    op.create_index(
        "uq_messages_conversation_id_platform_message_id",
        "messages",
        ("conversation_id", "platform_message_id"),
        unique=True,
        postgresql_where=sa.text("platform_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_messages_conversation_id_platform_message_id",
        table_name="messages",
    )
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_index("ix_messages_conversation_id_id_desc", table_name="messages")
    op.drop_table("messages")
    op.drop_index(
        "uq_conversations_platform_type_user_id",
        table_name="conversations",
    )
    op.drop_index(
        "uq_conversations_platform_type_group_id",
        table_name="conversations",
    )
    op.drop_table("conversations")
    op.drop_table("users")

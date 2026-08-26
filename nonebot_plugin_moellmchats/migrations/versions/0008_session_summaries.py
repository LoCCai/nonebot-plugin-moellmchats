"""Add append-only session summary chains.

Revision ID: 0008_session_summaries
Revises: 0007_model_usage
Create Date: 2026-08-22 21:25:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0008_session_summaries"
down_revision: str | Sequence[str] | None = "0007_model_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_messages_conversation_id_id",
        "messages",
        ("conversation_id", "id"),
    )
    op.create_table(
        "session_summaries",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("previous_summary_id", sa.String(length=128), nullable=True),
        sa.Column("covered_from_message_id", sa.BigInteger(), nullable=False),
        sa.Column("covered_through_message_id", sa.BigInteger(), nullable=False),
        sa.Column("covered_message_count", sa.BigInteger(), nullable=False),
        sa.Column("source_message_count", sa.BigInteger(), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("trigger_message_count", sa.BigInteger(), nullable=False),
        sa.Column("keep_recent_message_count", sa.BigInteger(), nullable=False),
        sa.Column("max_source_chars", sa.BigInteger(), nullable=False),
        sa.Column("source_char_count", sa.BigInteger(), nullable=False),
        sa.Column("model_provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(btrim(content)) > 0 AND char_length(content) <= 16000",
            name="ck_session_summaries_content_bounded",
        ),
        sa.CheckConstraint(
            "covered_message_count > 0",
            name="ck_session_summaries_covered_count_positive",
        ),
        sa.CheckConstraint(
            "char_length(conversation_id) > 0",
            name="ck_session_summaries_conversation_id_present",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_session_summaries_generation_positive",
        ),
        sa.CheckConstraint(
            "char_length(id) > 0",
            name="ck_session_summaries_id_present",
        ),
        sa.CheckConstraint(
            "generation <> 1 OR source_message_count = covered_message_count",
            name="ck_session_summaries_initial_count_valid",
        ),
        sa.CheckConstraint(
            "trigger_message_count BETWEEN 2 AND 200 AND keep_recent_message_count BETWEEN 1 AND trigger_message_count - 1",
            name="ck_session_summaries_message_policy_bounded",
        ),
        sa.CheckConstraint(
            "covered_from_message_id > 0 AND covered_through_message_id >= covered_from_message_id",
            name="ck_session_summaries_message_watermark_order",
        ),
        sa.CheckConstraint(
            "char_length(btrim(model)) > 0",
            name="ck_session_summaries_model_present",
        ),
        sa.CheckConstraint(
            "char_length(btrim(model_provider)) > 0",
            name="ck_session_summaries_model_provider_present",
        ),
        sa.CheckConstraint(
            "policy_version ~ '^[a-z][a-z0-9_.-]{0,31}$'",
            name="ck_session_summaries_policy_version_valid",
        ),
        sa.CheckConstraint(
            "max_source_chars BETWEEN 1024 AND 1000000 AND source_char_count BETWEEN 1 AND max_source_chars",
            name="ck_session_summaries_source_chars_bounded",
        ),
        sa.CheckConstraint(
            "(generation = 1 AND previous_summary_id IS NULL) OR (generation > 1 AND previous_summary_id IS NOT NULL)",
            name="ck_session_summaries_previous_matches_generation",
        ),
        sa.CheckConstraint(
            "source_message_count > 0 AND source_message_count <= covered_message_count",
            name="ck_session_summaries_source_count_valid",
        ),
        sa.CheckConstraint(
            "source_digest ~ '^[a-f0-9]{64}$'",
            name="ck_session_summaries_source_digest_valid",
        ),
        sa.ForeignKeyConstraint(
            ("conversation_id",),
            ("conversations.id",),
            name="fk_session_summaries_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("conversation_id", "covered_from_message_id"),
            ("messages.conversation_id", "messages.id"),
            name="fk_session_summaries_covered_from_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("conversation_id", "covered_through_message_id"),
            ("messages.conversation_id", "messages.id"),
            name="fk_session_summaries_covered_through_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("conversation_id", "previous_summary_id"),
            ("session_summaries.conversation_id", "session_summaries.id"),
            name="fk_session_summaries_previous_summary",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_session_summaries"),
        sa.UniqueConstraint(
            "conversation_id",
            "generation",
            name="uq_session_summaries_conversation_generation",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "id",
            name="uq_session_summaries_conversation_id_id",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "covered_through_message_id",
            name="uq_session_summaries_conversation_watermark",
        ),
    )
    op.create_index(
        "uq_session_summaries_conversation_previous",
        "session_summaries",
        ("conversation_id", "previous_summary_id"),
        unique=True,
        postgresql_where=sa.text("previous_summary_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_session_summaries_conversation_previous",
        table_name="session_summaries",
    )
    op.drop_table("session_summaries")
    op.drop_constraint(
        "uq_messages_conversation_id_id",
        "messages",
        type_="unique",
    )

"""Create the initial agent runtime table.

Revision ID: 0002_agent_runtime
Revises: 0001_users_conversations
Create Date: 2026-08-21 08:42:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_agent_runtime"
down_revision: str | Sequence[str] | None = "0001_users_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("group_id", sa.String(length=128), nullable=True),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "char_length(conversation_id) > 0",
            name="ck_agent_runs_conversation_id_present",
        ),
        sa.CheckConstraint(
            "cost IS NULL OR cost >= 0",
            name="ck_agent_runs_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "error_message IS NULL OR char_length(error_message) > 0",
            name="ck_agent_runs_error_message_present",
        ),
        sa.CheckConstraint(
            "error_type IS NULL OR char_length(error_type) > 0",
            name="ck_agent_runs_error_type_present",
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'cancelled', 'timed_out', 'rejected') "
            "AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('completed', 'failed', 'cancelled', 'timed_out', 'rejected') "
            "AND finished_at IS NULL)",
            name="ck_agent_runs_finish_matches_status",
        ),
        sa.CheckConstraint(
            "generation >= 0",
            name="ck_agent_runs_generation_nonnegative",
        ),
        sa.CheckConstraint(
            "group_id IS NULL OR char_length(group_id) > 0",
            name="ck_agent_runs_group_id_present",
        ),
        sa.CheckConstraint("char_length(id) > 0", name="ck_agent_runs_id_present"),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_agent_runs_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "model IS NULL OR char_length(model) > 0",
            name="ck_agent_runs_model_present",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_agent_runs_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "request_id > 0",
            name="ck_agent_runs_request_id_positive",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'admitted', 'classifying', 'planning', 'executing', "
            "'waiting_confirmation', 'summarizing', 'completed', 'failed', 'cancelled', "
            "'timed_out', 'rejected')",
            name="ck_agent_runs_status_valid",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_agent_runs_timestamp_order",
        ),
        sa.CheckConstraint(
            "char_length(user_id) > 0",
            name="ck_agent_runs_user_id_present",
        ),
        sa.ForeignKeyConstraint(
            ("conversation_id",),
            ("conversations.id",),
            name="fk_agent_runs_conversation_id_conversations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("user_id",),
            ("users.id",),
            name="fk_agent_runs_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index(
        "ix_agent_runs_conversation_id_started_at_id_desc",
        "agent_runs",
        ("conversation_id", sa.text("started_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_user_id_started_at",
        "agent_runs",
        ("user_id", sa.text("started_at DESC")),
        unique=False,
    )
    op.create_index(
        "ix_agent_runs_status_started_at",
        "agent_runs",
        ("status", "started_at"),
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_status_started_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id_started_at", table_name="agent_runs")
    op.drop_index(
        "ix_agent_runs_conversation_id_started_at_id_desc",
        table_name="agent_runs",
    )
    op.drop_table("agent_runs")

"""Create tool call audit records.

Revision ID: 0004_tool_calls
Revises: 0003_agent_steps
Create Date: 2026-08-21 09:25:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004_tool_calls"
down_revision: str | Sequence[str] | None = "0003_agent_steps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_agent_steps_run_id_id",
        "agent_steps",
        ("run_id", "id"),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("step_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("tool_source", sa.String(length=32), nullable=False),
        sa.Column("bundle_id", sa.String(length=64), nullable=True),
        sa.Column("bundle_digest", sa.String(length=64), nullable=True),
        sa.Column("arguments_json", postgresql.JSONB(), nullable=False),
        sa.Column("result_preview", sa.Text(), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmation_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "bundle_digest IS NULL OR bundle_digest ~ '^[a-f0-9]{64}$'",
            name="ck_tool_calls_bundle_digest_valid",
        ),
        sa.CheckConstraint(
            "bundle_id IS NULL OR bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
            name="ck_tool_calls_bundle_id_valid",
        ),
        sa.CheckConstraint(
            "(tool_source = 'generated' AND bundle_id IS NOT NULL AND bundle_digest IS NOT NULL) OR "
            "(tool_source <> 'generated' AND bundle_id IS NULL AND bundle_digest IS NULL)",
            name="ck_tool_calls_bundle_matches_source",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR result_preview IS NOT NULL",
            name="ck_tool_calls_completed_has_result",
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
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_tool_calls_duration_nonnegative",
        ),
        sa.CheckConstraint(
            "char_length(id) > 0",
            name="ck_tool_calls_id_present",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(arguments_json) = 'object'",
            name="ck_tool_calls_arguments_object",
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'failed', 'cancelled', 'timed_out', 'rejected') "
            "AND finished_at IS NOT NULL AND duration_ms IS NOT NULL) OR "
            "(status NOT IN ('completed', 'failed', 'cancelled', 'timed_out', 'rejected') "
            "AND finished_at IS NULL AND duration_ms IS NULL)",
            name="ck_tool_calls_lifecycle_fields",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed', 'cancelled', 'timed_out', 'rejected') OR result_preview IS NULL",
            name="ck_tool_calls_result_matches_status",
        ),
        sa.CheckConstraint(
            "result_preview IS NULL OR (char_length(result_preview) > 0 AND char_length(result_preview) <= 6000)",
            name="ck_tool_calls_result_preview_bounded",
        ),
        sa.CheckConstraint(
            "char_length(run_id) > 0",
            name="ck_tool_calls_run_id_present",
        ),
        sa.CheckConstraint(
            "char_length(step_id) > 0",
            name="ck_tool_calls_step_id_present",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'waiting_confirmation', 'running', 'completed', "
            "'failed', 'cancelled', 'timed_out', 'rejected')",
            name="ck_tool_calls_status_valid",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= created_at",
            name="ck_tool_calls_timestamp_order",
        ),
        sa.CheckConstraint(
            "char_length(tool_name) > 0",
            name="ck_tool_calls_tool_name_present",
        ),
        sa.CheckConstraint(
            "tool_source IN ('registered', 'custom_file', 'generated', 'mcp', 'builtin', 'nonebot_plugin')",
            name="ck_tool_calls_source_valid",
        ),
        sa.CheckConstraint(
            "status <> 'waiting_confirmation' OR (NOT confirmed AND confirmation_id IS NOT NULL)",
            name="ck_tool_calls_waiting_confirmation_fields",
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_tool_calls"),
    )
    op.create_index(
        "ix_tool_calls_run_id_created_at_id_desc",
        "tool_calls",
        ("run_id", sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_tool_calls_status_created_at",
        "tool_calls",
        ("status", "created_at"),
        unique=False,
    )
    op.create_index(
        "ix_tool_calls_step_id_created_at",
        "tool_calls",
        ("step_id", "created_at"),
        unique=False,
    )
    op.create_index(
        "uq_tool_calls_confirmation_id",
        "tool_calls",
        ("confirmation_id",),
        unique=True,
        postgresql_where=sa.text("confirmation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tool_calls_confirmation_id", table_name="tool_calls")
    op.drop_index("ix_tool_calls_step_id_created_at", table_name="tool_calls")
    op.drop_index("ix_tool_calls_status_created_at", table_name="tool_calls")
    op.drop_index(
        "ix_tool_calls_run_id_created_at_id_desc",
        table_name="tool_calls",
    )
    op.drop_table("tool_calls")
    op.drop_constraint(
        "uq_agent_steps_run_id_id",
        "agent_steps",
        type_="unique",
    )

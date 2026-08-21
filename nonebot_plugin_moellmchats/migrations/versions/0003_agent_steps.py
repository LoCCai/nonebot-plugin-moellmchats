"""Create agent runtime steps.

Revision ID: 0003_agent_steps
Revises: 0002_agent_runtime
Create Date: 2026-08-21 09:05:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0003_agent_steps"
down_revision: str | Sequence[str] | None = "0002_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("step_index", sa.BigInteger(), nullable=False),
        sa.Column("step_type", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("input_preview", sa.Text(), nullable=True),
        sa.Column("output_preview", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_agent_steps_duration_nonnegative",
        ),
        sa.CheckConstraint(
            "error IS NULL OR (char_length(error) > 0 AND char_length(error) <= 6000)",
            name="ck_agent_steps_error_bounded",
        ),
        sa.CheckConstraint(
            "status IN ('failed', 'cancelled', 'timed_out', 'skipped') OR error IS NULL",
            name="ck_agent_steps_error_matches_status",
        ),
        sa.CheckConstraint("char_length(id) > 0", name="ck_agent_steps_id_present"),
        sa.CheckConstraint(
            "input_preview IS NULL OR (char_length(input_preview) > 0 AND char_length(input_preview) <= 6000)",
            name="ck_agent_steps_input_preview_bounded",
        ),
        sa.CheckConstraint(
            "step_index >= 0",
            name="ck_agent_steps_index_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL AND duration_ms IS NULL) OR "
            "(status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL AND duration_ms IS NULL) OR "
            "(status IN ('completed', 'failed', 'cancelled', 'timed_out', 'skipped') "
            "AND started_at IS NOT NULL AND finished_at IS NOT NULL AND duration_ms IS NOT NULL)",
            name="ck_agent_steps_lifecycle_fields",
        ),
        sa.CheckConstraint(
            "model IS NULL OR char_length(model) > 0",
            name="ck_agent_steps_model_present",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed', 'cancelled', 'timed_out', 'skipped') OR output_preview IS NULL",
            name="ck_agent_steps_output_matches_status",
        ),
        sa.CheckConstraint(
            "output_preview IS NULL OR (char_length(output_preview) > 0 AND char_length(output_preview) <= 6000)",
            name="ck_agent_steps_output_preview_bounded",
        ),
        sa.CheckConstraint(
            "char_length(run_id) > 0",
            name="ck_agent_steps_run_id_present",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'timed_out', 'skipped')",
            name="ck_agent_steps_status_valid",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_agent_steps_timestamp_order",
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
            "step_type IN ('classification', 'model', 'tool', 'summary', 'vision', 'confirmation', 'memory')",
            name="ck_agent_steps_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ("run_id",),
            ("agent_runs.id",),
            name="fk_agent_steps_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_steps"),
        sa.UniqueConstraint(
            "run_id",
            "step_index",
            name="uq_agent_steps_run_id_step_index",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_steps")

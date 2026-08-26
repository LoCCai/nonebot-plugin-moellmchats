"""Create per-run model usage records.

Revision ID: 0007_model_usage
Revises: 0006_audit_events
Create Date: 2026-08-22 14:56:44+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0007_model_usage"
down_revision: str | Sequence[str] | None = "0006_audit_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cached_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cached_tokens >= 0",
            name="ck_model_usage_cached_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "cost IS NULL OR cost >= 0",
            name="ck_model_usage_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0",
            name="ck_model_usage_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "char_length(btrim(model)) > 0",
            name="ck_model_usage_model_present",
        ),
        sa.CheckConstraint(
            "output_tokens >= 0",
            name="ck_model_usage_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "char_length(btrim(provider)) > 0",
            name="ck_model_usage_provider_present",
        ),
        sa.CheckConstraint(
            "reasoning_tokens >= 0",
            name="ck_model_usage_reasoning_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "char_length(run_id) > 0",
            name="ck_model_usage_run_id_present",
        ),
        sa.ForeignKeyConstraint(
            ("run_id",),
            ("agent_runs.id",),
            name="fk_model_usage_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_usage"),
    )
    op.create_index(
        "ix_model_usage_created_at_id_desc",
        "model_usage",
        (sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_model_usage_provider_model_created_at_id_desc",
        "model_usage",
        (
            "provider",
            "model",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ),
        unique=False,
    )
    op.create_index(
        "ix_model_usage_run_id_created_at_id_desc",
        "model_usage",
        ("run_id", sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_usage_run_id_created_at_id_desc",
        table_name="model_usage",
    )
    op.drop_index(
        "ix_model_usage_provider_model_created_at_id_desc",
        table_name="model_usage",
    )
    op.drop_index(
        "ix_model_usage_created_at_id_desc",
        table_name="model_usage",
    )
    op.drop_table("model_usage")

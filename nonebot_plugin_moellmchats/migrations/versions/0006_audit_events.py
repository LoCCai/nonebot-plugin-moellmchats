"""Create bounded append-only audit event records.

Revision ID: 0006_audit_events
Revises: 0005_tool_bundle_metadata
Create Date: 2026-08-22 14:20:19+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_audit_events"
down_revision: str | Sequence[str] | None = "0005_tool_bundle_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_tool_calls_run_id_id",
        "tool_calls",
        ("run_id", "id"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=True),
        sa.Column("tool_call_id", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "actor_type ~ '^[a-z][a-z0-9_.:-]{0,31}$'",
            name="ck_audit_events_actor_type_valid",
        ),
        sa.CheckConstraint(
            "actor_user_id IS NULL OR char_length(actor_user_id) > 0",
            name="ck_audit_events_actor_user_id_present",
        ),
        sa.CheckConstraint(
            "event_type ~ '^[a-z][a-z0-9_.:-]{0,127}$'",
            name="ck_audit_events_event_type_valid",
        ),
        sa.CheckConstraint(
            "octet_length(metadata_json::text) <= 65536",
            name="ck_audit_events_metadata_bounded",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata_json) = 'object'",
            name="ck_audit_events_metadata_object",
        ),
        sa.CheckConstraint(
            "run_id IS NULL OR char_length(run_id) > 0",
            name="ck_audit_events_run_id_present",
        ),
        sa.CheckConstraint(
            "char_length(target_id) > 0",
            name="ck_audit_events_target_id_present",
        ),
        sa.CheckConstraint(
            "target_type ~ '^[a-z][a-z0-9_.:-]{0,63}$'",
            name="ck_audit_events_target_type_valid",
        ),
        sa.CheckConstraint(
            "tool_call_id IS NULL OR run_id IS NOT NULL",
            name="ck_audit_events_tool_call_has_run",
        ),
        sa.CheckConstraint(
            "tool_call_id IS NULL OR char_length(tool_call_id) > 0",
            name="ck_audit_events_tool_call_id_present",
        ),
        sa.ForeignKeyConstraint(
            ("actor_user_id",),
            ("users.id",),
            name="fk_audit_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("run_id",),
            ("agent_runs.id",),
            name="fk_audit_events_run_id_agent_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ("run_id", "tool_call_id"),
            ("tool_calls.run_id", "tool_calls.id"),
            name="fk_audit_events_run_id_tool_call_id_tool_calls",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_actor_created_at_id_desc",
        "audit_events",
        (
            "actor_type",
            "actor_user_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ),
        unique=False,
    )
    op.create_index(
        "ix_audit_events_event_type_created_at_id_desc",
        "audit_events",
        ("event_type", sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_audit_events_run_id_created_at_id_desc",
        "audit_events",
        ("run_id", sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_audit_events_target_created_at_id_desc",
        "audit_events",
        (
            "target_type",
            "target_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ),
        unique=False,
    )
    op.create_index(
        "ix_audit_events_tool_call_id_created_at_id_desc",
        "audit_events",
        ("tool_call_id", sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_events_tool_call_id_created_at_id_desc",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_target_created_at_id_desc",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_run_id_created_at_id_desc",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_event_type_created_at_id_desc",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_actor_created_at_id_desc",
        table_name="audit_events",
    )
    op.drop_table("audit_events")
    op.drop_constraint(
        "uq_tool_calls_run_id_id",
        "tool_calls",
        type_="unique",
    )

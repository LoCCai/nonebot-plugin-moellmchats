"""Create durable generated tool bundle metadata.

Revision ID: 0005_tool_bundle_metadata
Revises: 0004_tool_calls
Create Date: 2026-08-22 00:00:00+00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_tool_bundle_metadata"
down_revision: str | Sequence[str] | None = "0004_tool_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_bundles",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
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
        sa.Column("active_version_id", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "active_version_id IS NULL OR char_length(active_version_id) > 0",
            name="ck_tool_bundles_active_version_id_present",
        ),
        sa.CheckConstraint(
            "bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
            name="ck_tool_bundles_bundle_id_valid",
        ),
        sa.CheckConstraint(
            "char_length(btrim(description)) > 0 AND octet_length(description) <= 65536",
            name="ck_tool_bundles_description_bounded",
        ),
        sa.CheckConstraint(
            "char_length(id) > 0",
            name="ck_tool_bundles_id_present",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_tool_bundles_timestamp_order",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_bundles"),
        sa.UniqueConstraint("bundle_id", name="uq_tool_bundles_bundle_id"),
    )
    op.create_index(
        "ix_tool_bundles_updated_at_id_desc",
        "tool_bundles",
        (sa.text("updated_at DESC"), sa.text("id DESC")),
        unique=False,
    )

    op.create_table(
        "tool_bundle_versions",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("tests_source", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("risks_json", postgresql.JSONB(), nullable=False),
        sa.Column("capabilities_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "bundle_id ~ '^[A-Za-z][A-Za-z0-9_-]{0,63}$'",
            name="ck_tool_bundle_versions_bundle_id_valid",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities_json) = 'object' AND octet_length(capabilities_json::text) <= 65536",
            name="ck_tool_bundle_versions_capabilities_object",
        ),
        sa.CheckConstraint(
            "digest ~ '^[a-f0-9]{64}$'",
            name="ck_tool_bundle_versions_digest_valid",
        ),
        sa.CheckConstraint(
            "char_length(id) > 0",
            name="ck_tool_bundle_versions_id_present",
        ),
        sa.CheckConstraint(
            "(state = 'approved' AND activated_at IS NULL AND deprecated_at IS NULL AND archived_at IS NULL) OR "
            "(state = 'activated' AND activated_at IS NOT NULL AND deprecated_at IS NULL AND archived_at IS NULL) OR "
            "(state = 'deprecated' AND activated_at IS NOT NULL AND deprecated_at IS NOT NULL AND archived_at IS NULL) OR "
            "(state = 'archived' AND activated_at IS NOT NULL AND deprecated_at IS NOT NULL AND archived_at IS NOT NULL)",
            name="ck_tool_bundle_versions_lifecycle_fields",
        ),
        sa.CheckConstraint(
            "octet_length(manifest_json::text) <= 65536",
            name="ck_tool_bundle_versions_manifest_bounded",
        ),
        sa.CheckConstraint(
            "manifest_json ? 'bundle_id' AND "
            "jsonb_typeof(manifest_json -> 'bundle_id') = 'string' AND "
            "manifest_json ->> 'bundle_id' = bundle_id",
            name="ck_tool_bundle_versions_manifest_identity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(manifest_json) = 'object'",
            name="ck_tool_bundle_versions_manifest_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(risks_json) = 'array' AND octet_length(risks_json::text) <= 65536",
            name="ck_tool_bundle_versions_risks_array",
        ),
        sa.CheckConstraint(
            "octet_length(source) BETWEEN 1 AND 65536",
            name="ck_tool_bundle_versions_source_bounded",
        ),
        sa.CheckConstraint(
            "state IN ('approved', 'activated', 'deprecated', 'archived')",
            name="ck_tool_bundle_versions_state_valid",
        ),
        sa.CheckConstraint(
            "octet_length(tests_source) BETWEEN 1 AND 65536",
            name="ck_tool_bundle_versions_tests_source_bounded",
        ),
        sa.CheckConstraint(
            "approved_at >= created_at AND "
            "(activated_at IS NULL OR activated_at >= approved_at) AND "
            "(deprecated_at IS NULL OR deprecated_at >= activated_at) AND "
            "(archived_at IS NULL OR archived_at >= deprecated_at)",
            name="ck_tool_bundle_versions_timestamp_order",
        ),
        sa.ForeignKeyConstraint(
            ("bundle_id",),
            ("tool_bundles.bundle_id",),
            name="fk_tool_bundle_versions_bundle_id_tool_bundles",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tool_bundle_versions"),
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
    )
    op.create_index(
        "ix_tool_bundle_versions_bundle_id_created_at_id_desc",
        "tool_bundle_versions",
        ("bundle_id", sa.text("created_at DESC"), sa.text("id DESC")),
        unique=False,
    )
    op.create_index(
        "ix_tool_bundle_versions_state_created_at",
        "tool_bundle_versions",
        ("state", "created_at"),
        unique=False,
    )
    op.create_index(
        "uq_tool_bundle_versions_active_bundle_id",
        "tool_bundle_versions",
        ("bundle_id",),
        unique=True,
        postgresql_where=sa.text("state = 'activated'"),
    )
    op.create_foreign_key(
        "fk_tool_bundles_active_version",
        "tool_bundles",
        "tool_bundle_versions",
        ["bundle_id", "active_version_id"],
        ["bundle_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tool_bundles_active_version",
        "tool_bundles",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_tool_bundle_versions_active_bundle_id",
        table_name="tool_bundle_versions",
    )
    op.drop_index(
        "ix_tool_bundle_versions_state_created_at",
        table_name="tool_bundle_versions",
    )
    op.drop_index(
        "ix_tool_bundle_versions_bundle_id_created_at_id_desc",
        table_name="tool_bundle_versions",
    )
    op.drop_table("tool_bundle_versions")
    op.drop_index(
        "ix_tool_bundles_updated_at_id_desc",
        table_name="tool_bundles",
    )
    op.drop_table("tool_bundles")

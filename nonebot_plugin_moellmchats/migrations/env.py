from __future__ import annotations

from alembic import context
from alembic.script import ScriptDirectory

from nonebot_plugin_moellmchats.database_migrations import (
    DatabaseMigrationConfigurationError,
    DatabaseMigrationOnlineDisabledError,
)
from nonebot_plugin_moellmchats.database_schema import database_metadata

_OFFLINE_ONLY_ATTRIBUTE = "moellmchats_offline_only"


def run_migrations_offline() -> None:
    """Render PostgreSQL DDL without a URL, engine, or network connection."""

    config = context.config
    if config.attributes.get(_OFFLINE_ONLY_ATTRIBUTE) is not True:
        raise DatabaseMigrationConfigurationError("Alembic 环境只能通过显式离线配置调用")
    if config.get_main_option("sqlalchemy.url", default=None) is not None:
        raise DatabaseMigrationConfigurationError("离线 Alembic 配置不得包含 sqlalchemy.url")
    if not ScriptDirectory.from_config(config).get_heads():
        return

    context.configure(
        dialect_name="postgresql",
        dialect_opts={"paramstyle": "named"},
        target_metadata=database_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        render_as_batch=False,
        transactional_ddl=True,
        transaction_per_migration=True,
        version_table="alembic_version",
        version_table_pk=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Fail closed until an explicit online migration design is reviewed."""

    raise DatabaseMigrationOnlineDisabledError("当前禁止在线 migration；仅允许离线 SQL 渲染")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

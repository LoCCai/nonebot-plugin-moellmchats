# MoEllmChats database migrations

This packaged Alembic environment remains intentionally offline-only in F-04.

- It never reads a database URL, environment variable, plugin config, or secret file.
- It cannot create an engine or open a database connection.
- Revision `0001_users_conversations` defines the first business tables.
- Production migration execution requires a later, separately reviewed release gate.

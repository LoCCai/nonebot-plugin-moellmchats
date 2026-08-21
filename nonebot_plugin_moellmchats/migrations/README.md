# MoEllmChats database migrations

This packaged Alembic environment is intentionally offline-only in F-03.

- It never reads a database URL, environment variable, plugin config, or secret file.
- It cannot create an engine or open a database connection.
- Business tables and the first revision begin in F-04.
- Production migration execution requires a later, separately reviewed release gate.

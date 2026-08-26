# MoEllmChats database migrations

This packaged Alembic environment remains intentionally offline-only in F-04.

- It never reads a database URL, environment variable, plugin config, or secret file.
- It cannot create an engine or open a database connection.
- Revision `0001_users_conversations` defines the first business tables.
- Revision `0002_agent_runtime` adds the initial `agent_runs` table.
- Revision `0003_agent_steps` adds bounded step records.
- Revision `0004_tool_calls` adds source-bound tool call audit records.
- Revision `0005_tool_bundle_metadata` adds bundle/version metadata and lifecycle constraints.
- Revision `0006_audit_events` adds bounded, reference-safe audit event records.
- Revision `0007_model_usage` adds per-run provider/model token and cost records.
- Revision `0008_session_summaries` adds append-only, source-bound session summary chains.
- Production migration execution requires a later, separately reviewed release gate.

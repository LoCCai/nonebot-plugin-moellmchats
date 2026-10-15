# Temporary model service failures

Chat and classification requests switch to eligible configured backups on HTTP
500, 502, 503 and 504, as well as the existing 429 and explicit billing quota
failures. Temporary server failures do not mark a model's quota exhausted.
Permission and authentication failures retain their existing handling.

Selection stays within the configured role pool or explicit fallback mapping.
Vision requests require a vision model; requests with tools require tool support.
The original ordered fallback list remains available if an intermediate backup
also fails or the primary had already exhausted its quota. Each candidate is
tried at most once within a switched chain. A standalone model without eligible
backups retains the existing bounded retry behavior.

Switching replaces the provider transport and model options while preserving
conversation messages, tool schemas and completed tool results. It does not
execute a completed tool again. Classification retains its original overall
deadline and never caches a backup answer under the primary model's identity.

Validation: 314 tests covering transport status handling, model and role pools,
vision eligibility, chained failures, classification cache/deadline behavior and
request recovery; Ruff and diff whitespace checks pass.

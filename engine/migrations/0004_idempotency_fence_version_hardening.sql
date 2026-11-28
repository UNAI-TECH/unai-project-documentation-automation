-- UTDA Forward-Only Migration 0004: Harden attempt_fence_version to non-negative BIGINT
-- Preserves historical migration 0003_idempotency_fencing.sql, active_attempt_id FK, and indexes.

ALTER TABLE utda.idempotency_keys
    ALTER COLUMN attempt_fence_version TYPE BIGINT;

ALTER TABLE utda.idempotency_keys
    DROP CONSTRAINT IF EXISTS chk_idempotency_fence_nonnegative;

ALTER TABLE utda.idempotency_keys
    ADD CONSTRAINT chk_idempotency_fence_nonnegative
    CHECK (attempt_fence_version >= 0);

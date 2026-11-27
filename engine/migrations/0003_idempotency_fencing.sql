-- UTDA Forward-Only Migration 0003: Active-Attempt Fencing & Lease Version Tracking
-- Never modifies prior migrations (0001, 0002). Executed as utda_migrator.

ALTER TABLE utda.idempotency_keys
    ADD COLUMN IF NOT EXISTS active_attempt_id VARCHAR(64);

ALTER TABLE utda.idempotency_keys
    ADD COLUMN IF NOT EXISTS attempt_fence_version INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_idempotency_keys_active_attempt
    ON utda.idempotency_keys (active_attempt_id)
    WHERE active_attempt_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_processing_attempts_key_attempt
    ON utda.processing_attempts (idempotency_key, attempt_id);

ALTER TABLE utda.idempotency_keys
    DROP CONSTRAINT IF EXISTS fk_idempotency_active_attempt;

ALTER TABLE utda.idempotency_keys
    ADD CONSTRAINT fk_idempotency_active_attempt
    FOREIGN KEY (idempotency_key, active_attempt_id)
    REFERENCES utda.processing_attempts (idempotency_key, attempt_id)
    DEFERRABLE INITIALLY DEFERRED;

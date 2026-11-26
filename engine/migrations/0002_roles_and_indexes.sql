-- UTDA Operational Indexes & Least-Privilege Role Grants
-- Role grants execute conditionally if utda_runtime / utda_migrator roles exist,
-- keeping this migration portable across Supabase and standard PostgreSQL.

CREATE INDEX IF NOT EXISTS idx_event_receipts_repo_feature
    ON utda.github_event_receipts (repository, feature_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_feature_states_status_updated
    ON utda.feature_processing_states (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_processing_attempts_retry
    ON utda.processing_attempts (retry_state, next_retry_at);

CREATE INDEX IF NOT EXISTS idx_document_proposals_project_doc
    ON utda.document_proposals (project_id, document_id, pr_state);

CREATE INDEX IF NOT EXISTS idx_approval_records_project_change
    ON utda.approval_records (project_id, change_id, decided_at DESC);

CREATE INDEX IF NOT EXISTS idx_drive_mappings_drift
    ON utda.drive_file_mappings (drift_detected)
    WHERE drift_detected = TRUE;

CREATE INDEX IF NOT EXISTS idx_audit_events_project_occurred
    ON utda.audit_events (project_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_type_occurred
    ON utda.audit_events (event_type, occurred_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_runtime') THEN
        REVOKE ALL ON SCHEMA public FROM utda_runtime;
        GRANT USAGE ON SCHEMA utda TO utda_runtime;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA utda TO utda_runtime;
        REVOKE INSERT, UPDATE, DELETE ON utda.schema_migrations FROM utda_runtime;
        GRANT SELECT ON utda.schema_migrations TO utda_runtime;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_migrator') THEN
        GRANT USAGE, CREATE ON SCHEMA utda TO utda_migrator;
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA utda TO utda_migrator;
    END IF;
END
$$;

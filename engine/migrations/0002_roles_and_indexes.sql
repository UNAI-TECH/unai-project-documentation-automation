-- UTDA Operational Indexes, Explicit Table-by-Table Grants, Ownership & Default Privileges
-- Fails closed if required roles (utda_migrator, utda_runtime, utda_governance) are missing.

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

-- 1. Fail closed if required database roles do not exist.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_migrator') THEN
        RAISE EXCEPTION 'Required role utda_migrator is missing. Provision database roles before running 0002_roles_and_indexes.sql.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_runtime') THEN
        RAISE EXCEPTION 'Required role utda_runtime is missing. Provision database roles before running 0002_roles_and_indexes.sql.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_governance') THEN
        RAISE EXCEPTION 'Required role utda_governance is missing. Provision database roles before running 0002_roles_and_indexes.sql.';
    END IF;
END
$$;

-- 2. Establish explicit migration ownership (utda_migrator owns schema utda and its tables).
DO $$
DECLARE
    tbl RECORD;
BEGIN
    IF CURRENT_USER <> 'utda_migrator' THEN
        IF NOT pg_has_role(CURRENT_USER, 'utda_migrator', 'MEMBER') THEN
            RAISE EXCEPTION 'Current migration user (%) is neither utda_migrator nor a member of utda_migrator; cannot establish DDL ownership.', CURRENT_USER;
        END IF;
        EXECUTE 'ALTER SCHEMA utda OWNER TO utda_migrator';
        FOR tbl IN
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'utda'
        LOOP
            EXECUTE format('ALTER TABLE utda.%I OWNER TO utda_migrator', tbl.tablename);
        END LOOP;
    END IF;
END
$$;

-- 3. Revoke default public/broad privileges on schema utda and tables.
REVOKE ALL ON SCHEMA utda FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM utda_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM utda_governance;

-- 4. Schema-level privileges (only utda_migrator has CREATE; runtime/governance have USAGE only).
GRANT USAGE, CREATE ON SCHEMA utda TO utda_migrator;
GRANT USAGE ON SCHEMA utda TO utda_runtime;
GRANT USAGE ON SCHEMA utda TO utda_governance;

-- 5. Explicit table-by-table permissions for utda_runtime (Phase 1 least privilege).
-- 5a. Mutable operational tables required by Phase 1 core engine:
GRANT SELECT, INSERT, UPDATE ON utda.projects TO utda_runtime;
GRANT SELECT, INSERT, UPDATE ON utda.feature_processing_states TO utda_runtime;
GRANT SELECT, INSERT, UPDATE ON utda.idempotency_keys TO utda_runtime;
GRANT SELECT, INSERT, UPDATE ON utda.processing_attempts TO utda_runtime;

-- 5b. Append-only / immutable Phase 1 operational tables (no UPDATE, no DELETE):
GRANT SELECT, INSERT ON utda.github_event_receipts TO utda_runtime;
GRANT SELECT, INSERT ON utda.final_reports TO utda_runtime;
GRANT SELECT, INSERT ON utda.audit_events TO utda_runtime;

-- 5c. Read-only governance, migration, and future-phase tables for utda_runtime:
GRANT SELECT ON utda.schema_migrations TO utda_runtime;
GRANT SELECT ON utda.template_releases TO utda_runtime;
GRANT SELECT ON utda.project_provisioning_runs TO utda_runtime;
GRANT SELECT ON utda.document_proposals TO utda_runtime;
GRANT SELECT ON utda.approval_records TO utda_runtime;
GRANT SELECT ON utda.project_master_versions TO utda_runtime;
GRANT SELECT ON utda.company_aggregation_runs TO utda_runtime;
GRANT SELECT ON utda.drive_file_mappings TO utda_runtime;
GRANT SELECT ON utda.drive_sync_revisions TO utda_runtime;
GRANT SELECT ON utda.rollback_operations TO utda_runtime;

-- 6. Explicit table-by-table permissions for utda_governance (privileged approval & rollback path).
GRANT SELECT, INSERT ON utda.approval_records TO utda_governance;
GRANT SELECT, INSERT, UPDATE ON utda.rollback_operations TO utda_governance;
GRANT SELECT, INSERT ON utda.audit_events TO utda_governance;
GRANT SELECT ON utda.projects TO utda_governance;
GRANT SELECT ON utda.document_proposals TO utda_governance;
GRANT SELECT ON utda.project_master_versions TO utda_governance;
GRANT SELECT ON utda.company_aggregation_runs TO utda_governance;
GRANT SELECT ON utda.drive_file_mappings TO utda_governance;
GRANT SELECT ON utda.drive_sync_revisions TO utda_governance;

-- 7. Default privileges for future tables created by migration owner utda_migrator:
--    Future tables default to SELECT-only for utda_runtime (never implicit INSERT/UPDATE/DELETE).
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON TABLES FROM utda_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON TABLES FROM utda_governance;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    GRANT SELECT ON TABLES TO utda_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    GRANT SELECT ON TABLES TO utda_governance;

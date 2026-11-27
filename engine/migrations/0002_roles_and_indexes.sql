-- UTDA Operational Indexes, Explicit Table-by-Table Grants, Ownership & Default Privileges
-- Fails closed on missing required roles or unsafe role inheritance.

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

-- 1. Fail closed if required database roles do not exist or have unsafe inheritance.
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

    IF pg_has_role('utda_runtime', 'utda_migrator', 'MEMBER')
       OR pg_has_role('utda_runtime', 'utda_governance', 'MEMBER') THEN
        RAISE EXCEPTION 'Security violation: utda_runtime must not be a member of utda_migrator or utda_governance.';
    END IF;
    IF pg_has_role('utda_governance', 'utda_migrator', 'MEMBER') THEN
        RAISE EXCEPTION 'Security violation: utda_governance must not be a member of utda_migrator.';
    END IF;
END
$$;

-- 2. Establish explicit migration ownership (utda_migrator owns schema utda, tables, and sequences).
--    Direct execution as utda_migrator is the preferred path; bootstrap ownership transfer
--    from an authorized member of utda_migrator is supported cleanly if required.
DO $$
DECLARE
    tbl RECORD;
    seq RECORD;
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
        FOR seq IN
            SELECT c.relname AS seqname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'utda' AND c.relkind = 'S'
        LOOP
            EXECUTE format('ALTER SEQUENCE utda.%I OWNER TO utda_migrator', seq.seqname);
        END LOOP;
    END IF;
END
$$;

-- 3. Revoke default public/broad privileges on schema utda, tables, and sequences.
REVOKE ALL ON SCHEMA utda FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA utda FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM utda_runtime;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA utda FROM utda_runtime;
REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM utda_governance;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA utda FROM utda_governance;

-- 4. Explicitly revoke privileges from Supabase API roles (anon, authenticated) if present.
DO $$
DECLARE
    api_role TEXT;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA utda FROM %I', api_role);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA utda FROM %I', api_role);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA utda FROM %I', api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda REVOKE ALL ON TABLES FROM %I', api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda REVOKE ALL ON SEQUENCES FROM %I', api_role);
        END IF;
    END LOOP;
END
$$;

-- 5. Schema-level privileges (only utda_migrator has CREATE; runtime/governance have USAGE only).
GRANT USAGE, CREATE ON SCHEMA utda TO utda_migrator;
GRANT USAGE ON SCHEMA utda TO utda_runtime;
GRANT USAGE ON SCHEMA utda TO utda_governance;

-- 6. Explicit table-by-table permissions for utda_runtime (Phase 1 least privilege).
-- 6a. Mutable operational tables required by Phase 1 core engine:
GRANT SELECT, INSERT, UPDATE ON utda.projects TO utda_runtime;
GRANT SELECT, INSERT, UPDATE ON utda.feature_processing_states TO utda_runtime;
GRANT SELECT, INSERT, UPDATE ON utda.idempotency_keys TO utda_runtime;
GRANT SELECT, INSERT, UPDATE ON utda.processing_attempts TO utda_runtime;

-- 6b. Role-enforced append-only Phase 1 tables (no UPDATE or DELETE granted to utda_runtime):
GRANT SELECT, INSERT ON utda.github_event_receipts TO utda_runtime;
GRANT SELECT, INSERT ON utda.final_reports TO utda_runtime;
GRANT SELECT, INSERT ON utda.audit_events TO utda_runtime;

-- 6c. Read-only governance, migration, and future-phase tables for utda_runtime:
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

-- 7. Explicit table-by-table permissions for utda_governance (privileged approval & rollback path).
GRANT SELECT, INSERT ON utda.approval_records TO utda_governance;
GRANT SELECT, INSERT, UPDATE ON utda.rollback_operations TO utda_governance;
GRANT SELECT, INSERT ON utda.audit_events TO utda_governance;
GRANT SELECT ON utda.projects TO utda_governance;
GRANT SELECT ON utda.document_proposals TO utda_governance;
GRANT SELECT ON utda.project_master_versions TO utda_governance;
GRANT SELECT ON utda.company_aggregation_runs TO utda_governance;
GRANT SELECT ON utda.drive_file_mappings TO utda_governance;
GRANT SELECT ON utda.drive_sync_revisions TO utda_governance;

-- 8. Default privileges for future tables & sequences created by migration owner utda_migrator:
--    Future tables default to SELECT-only for utda_runtime and utda_governance; zero access for PUBLIC.
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON TABLES FROM utda_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON SEQUENCES FROM utda_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON TABLES FROM utda_governance;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    REVOKE ALL ON SEQUENCES FROM utda_governance;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    GRANT SELECT ON TABLES TO utda_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda
    GRANT SELECT ON TABLES TO utda_governance;

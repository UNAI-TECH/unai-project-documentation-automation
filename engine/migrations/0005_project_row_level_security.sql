-- UTDA Forward-Only Migration 0005: Database-Level Project Row-Level Security (RLS)
--
-- Security boundary note:
--   Policies below evaluate transaction-local `utda.current_project_id` set via
--   `SELECT set_config('utda.current_project_id', ..., true)`. This GUC is an
--   application-provided transaction context, NOT an authenticated database
--   tenant identity. These RLS policies protect against accidental unscoped
--   queries and cross-project bugs, not against arbitrary raw SQL executed under
--   a compromised shared `utda_runtime` credential.
--
-- Privileges & ownership invariants preserved from 0002_roles_and_indexes.sql:
--   - Zero new table GRANTs are added (no premature Phase 2+ writes).
--   - `utda_migrator` remains schema and table OWNER without FORCE ROW LEVEL
--     SECURITY so migrations and `pg_dump --schema=utda` backups execute safely.
--   - `utda_runtime` and `utda_governance` are non-owners, non-superusers, and
--     lack BYPASSRLS; PostgreSQL strictly enforces RLS on all enabled tables.

-- 1. Fail closed if roles have unsafe bypass/superuser attributes or inheritance.
DO $$
DECLARE
    role_rec RECORD;
BEGIN
    FOR role_rec IN
        SELECT rolname, rolsuper, rolbypassrls, rolinherit
        FROM pg_roles
        WHERE rolname IN ('utda_runtime', 'utda_governance')
    LOOP
        IF role_rec.rolsuper THEN
            RAISE EXCEPTION 'Security violation: role % must not have SUPERUSER.', role_rec.rolname;
        END IF;
        IF role_rec.rolbypassrls THEN
            RAISE EXCEPTION 'Security violation: role % must not have BYPASSRLS.', role_rec.rolname;
        END IF;
        IF role_rec.rolinherit THEN
            RAISE EXCEPTION 'Security violation: role % must be NOINHERIT.', role_rec.rolname;
        END IF;
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_runtime')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_governance')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'utda_migrator') THEN
        RAISE EXCEPTION 'Required UTDA roles are missing before 0005_project_row_level_security.sql.';
    END IF;

    IF pg_has_role('utda_runtime', 'utda_migrator', 'MEMBER')
       OR pg_has_role('utda_runtime', 'utda_governance', 'MEMBER')
       OR pg_has_role('utda_governance', 'utda_migrator', 'MEMBER') THEN
        RAISE EXCEPTION 'Security violation: unsafe role inheritance detected before RLS enablement.';
    END IF;
END
$$;

-- Global tables (`utda.schema_migrations`, `utda.template_releases`) intentionally
-- remain without project-level RLS because they hold global catalog/health state.

-- 2. utda.projects
ALTER TABLE utda.projects ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_projects_select ON utda.projects;
DROP POLICY IF EXISTS p_runtime_projects_insert ON utda.projects;
DROP POLICY IF EXISTS p_runtime_projects_update ON utda.projects;
DROP POLICY IF EXISTS p_governance_projects_select ON utda.projects;

CREATE POLICY p_runtime_projects_select ON utda.projects
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_projects_insert ON utda.projects
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_projects_update ON utda.projects
    AS PERMISSIVE FOR UPDATE TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    )
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_projects_select ON utda.projects
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
    );

-- 3. utda.project_provisioning_runs (runtime SELECT-only per 0002)
ALTER TABLE utda.project_provisioning_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_provisioning_select ON utda.project_provisioning_runs;

CREATE POLICY p_runtime_provisioning_select ON utda.project_provisioning_runs
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 4. utda.github_event_receipts (nullable project_id: NULL rows strictly invisible/disallowed)
ALTER TABLE utda.github_event_receipts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_receipts_select ON utda.github_event_receipts;
DROP POLICY IF EXISTS p_runtime_receipts_insert ON utda.github_event_receipts;

CREATE POLICY p_runtime_receipts_select ON utda.github_event_receipts
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_receipts_insert ON utda.github_event_receipts
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 5. utda.feature_processing_states
ALTER TABLE utda.feature_processing_states ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_feature_states_select ON utda.feature_processing_states;
DROP POLICY IF EXISTS p_runtime_feature_states_insert ON utda.feature_processing_states;
DROP POLICY IF EXISTS p_runtime_feature_states_update ON utda.feature_processing_states;

CREATE POLICY p_runtime_feature_states_select ON utda.feature_processing_states
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_feature_states_insert ON utda.feature_processing_states
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_feature_states_update ON utda.feature_processing_states
    AS PERMISSIVE FOR UPDATE TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    )
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 6. utda.final_reports (append-only for runtime)
ALTER TABLE utda.final_reports ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_final_reports_select ON utda.final_reports;
DROP POLICY IF EXISTS p_runtime_final_reports_insert ON utda.final_reports;

CREATE POLICY p_runtime_final_reports_select ON utda.final_reports
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_final_reports_insert ON utda.final_reports
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 7. utda.idempotency_keys
ALTER TABLE utda.idempotency_keys ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_idem_keys_select ON utda.idempotency_keys;
DROP POLICY IF EXISTS p_runtime_idem_keys_insert ON utda.idempotency_keys;
DROP POLICY IF EXISTS p_runtime_idem_keys_update ON utda.idempotency_keys;

CREATE POLICY p_runtime_idem_keys_select ON utda.idempotency_keys
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_idem_keys_insert ON utda.idempotency_keys
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_idem_keys_update ON utda.idempotency_keys
    AS PERMISSIVE FOR UPDATE TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    )
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 8. utda.processing_attempts (child table verified via parent idempotency_keys invoker visibility)
ALTER TABLE utda.processing_attempts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_attempts_select ON utda.processing_attempts;
DROP POLICY IF EXISTS p_runtime_attempts_insert ON utda.processing_attempts;
DROP POLICY IF EXISTS p_runtime_attempts_update ON utda.processing_attempts;

CREATE POLICY p_runtime_attempts_select ON utda.processing_attempts
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND EXISTS (
            SELECT 1
            FROM utda.idempotency_keys ik
            WHERE ik.idempotency_key = processing_attempts.idempotency_key
              AND ik.project_id = current_setting('utda.current_project_id', true)
        )
    );
CREATE POLICY p_runtime_attempts_insert ON utda.processing_attempts
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND EXISTS (
            SELECT 1
            FROM utda.idempotency_keys ik
            WHERE ik.idempotency_key = processing_attempts.idempotency_key
              AND ik.project_id = current_setting('utda.current_project_id', true)
        )
    );
CREATE POLICY p_runtime_attempts_update ON utda.processing_attempts
    AS PERMISSIVE FOR UPDATE TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND EXISTS (
            SELECT 1
            FROM utda.idempotency_keys ik
            WHERE ik.idempotency_key = processing_attempts.idempotency_key
              AND ik.project_id = current_setting('utda.current_project_id', true)
        )
    )
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND EXISTS (
            SELECT 1
            FROM utda.idempotency_keys ik
            WHERE ik.idempotency_key = processing_attempts.idempotency_key
              AND ik.project_id = current_setting('utda.current_project_id', true)
        )
    );

-- 9. utda.document_proposals (SELECT-only in 0002; no premature writes)
ALTER TABLE utda.document_proposals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_proposals_select ON utda.document_proposals;
DROP POLICY IF EXISTS p_governance_proposals_select ON utda.document_proposals;

CREATE POLICY p_runtime_proposals_select ON utda.document_proposals
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_proposals_select ON utda.document_proposals
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 10. utda.approval_records (runtime read-only; governance append-only)
ALTER TABLE utda.approval_records ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_approvals_select ON utda.approval_records;
DROP POLICY IF EXISTS p_governance_approvals_select ON utda.approval_records;
DROP POLICY IF EXISTS p_governance_approvals_insert ON utda.approval_records;

CREATE POLICY p_runtime_approvals_select ON utda.approval_records
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_approvals_select ON utda.approval_records
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_approvals_insert ON utda.approval_records
    AS PERMISSIVE FOR INSERT TO utda_governance
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );

-- 11. utda.project_master_versions (runtime scoped to own project; governance aggregation reads only eligible versions)
ALTER TABLE utda.project_master_versions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_master_versions_select ON utda.project_master_versions;
DROP POLICY IF EXISTS p_governance_master_versions_select ON utda.project_master_versions;

CREATE POLICY p_runtime_master_versions_select ON utda.project_master_versions
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_master_versions_select ON utda.project_master_versions
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND eligible_for_aggregation = TRUE
        )
    );

-- 12. utda.company_aggregation_runs (completely isolated from utda_runtime; zero permissive policy for runtime)
ALTER TABLE utda.company_aggregation_runs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_governance_company_agg_select ON utda.company_aggregation_runs;

CREATE POLICY p_governance_company_agg_select ON utda.company_aggregation_runs
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION');

-- 13. utda.drive_file_mappings (nullable project_id reserved for COMPANY_MASTER; hidden from utda_runtime)
ALTER TABLE utda.drive_file_mappings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_drive_mappings_select ON utda.drive_file_mappings;
DROP POLICY IF EXISTS p_governance_drive_mappings_select ON utda.drive_file_mappings;

CREATE POLICY p_runtime_drive_mappings_select ON utda.drive_file_mappings
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_drive_mappings_select ON utda.drive_file_mappings
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
            AND document_id = 'COMPANY_MASTER'
        )
    );

-- 14. utda.drive_sync_revisions (child table scoped via parent drive_file_mappings invoker visibility)
ALTER TABLE utda.drive_sync_revisions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_drive_revisions_select ON utda.drive_sync_revisions;
DROP POLICY IF EXISTS p_governance_drive_revisions_select ON utda.drive_sync_revisions;

CREATE POLICY p_runtime_drive_revisions_select ON utda.drive_sync_revisions
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND EXISTS (
            SELECT 1
            FROM utda.drive_file_mappings dm
            WHERE dm.mapping_id = drive_sync_revisions.mapping_id
              AND dm.project_id = current_setting('utda.current_project_id', true)
        )
    );
CREATE POLICY p_governance_drive_revisions_select ON utda.drive_sync_revisions
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        EXISTS (
            SELECT 1
            FROM utda.drive_file_mappings dm
            WHERE dm.mapping_id = drive_sync_revisions.mapping_id
              AND (
                  (
                      current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
                      AND dm.project_id = current_setting('utda.current_project_id', true)
                  )
                  OR (
                      current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
                      AND dm.project_id IS NULL
                      AND dm.document_id = 'COMPANY_MASTER'
                  )
              )
        )
    );

-- 15. utda.rollback_operations (runtime SELECT-only with non-NULL project_id; governance scoped write)
ALTER TABLE utda.rollback_operations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_rollbacks_select ON utda.rollback_operations;
DROP POLICY IF EXISTS p_governance_rollbacks_select ON utda.rollback_operations;
DROP POLICY IF EXISTS p_governance_rollbacks_insert ON utda.rollback_operations;
DROP POLICY IF EXISTS p_governance_rollbacks_update ON utda.rollback_operations;

CREATE POLICY p_runtime_rollbacks_select ON utda.rollback_operations
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_rollbacks_select ON utda.rollback_operations
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
            AND document_id = 'COMPANY_MASTER'
        )
    );
CREATE POLICY p_governance_rollbacks_insert ON utda.rollback_operations
    AS PERMISSIVE FOR INSERT TO utda_governance
    WITH CHECK (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
            AND document_id = 'COMPANY_MASTER'
        )
    );
CREATE POLICY p_governance_rollbacks_update ON utda.rollback_operations
    AS PERMISSIVE FOR UPDATE TO utda_governance
    USING (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
            AND document_id = 'COMPANY_MASTER'
        )
    )
    WITH CHECK (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
            AND document_id = 'COMPANY_MASTER'
        )
    );

-- 16. utda.audit_events (nullable project_id: runtime strictly requires valid project_id; governance permits NULL only under COMPANY_AGGREGATION)
ALTER TABLE utda.audit_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_runtime_audit_select ON utda.audit_events;
DROP POLICY IF EXISTS p_runtime_audit_insert ON utda.audit_events;
DROP POLICY IF EXISTS p_governance_audit_select ON utda.audit_events;
DROP POLICY IF EXISTS p_governance_audit_insert ON utda.audit_events;

CREATE POLICY p_runtime_audit_select ON utda.audit_events
    AS PERMISSIVE FOR SELECT TO utda_runtime
    USING (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_runtime_audit_insert ON utda.audit_events
    AS PERMISSIVE FOR INSERT TO utda_runtime
    WITH CHECK (
        current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
        AND project_id = current_setting('utda.current_project_id', true)
    );
CREATE POLICY p_governance_audit_select ON utda.audit_events
    AS PERMISSIVE FOR SELECT TO utda_governance
    USING (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
        )
    );
CREATE POLICY p_governance_audit_insert ON utda.audit_events
    AS PERMISSIVE FOR INSERT TO utda_governance
    WITH CHECK (
        (
            current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
            AND project_id = current_setting('utda.current_project_id', true)
        )
        OR (
            current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
            AND project_id IS NULL
        )
    );

COMMENT ON POLICY p_runtime_idem_keys_select ON utda.idempotency_keys IS
    'Transaction-local project scoping context via utda.current_project_id; protects against accidental unscoped queries, not compromised runtime credentials.';
COMMENT ON POLICY p_governance_company_agg_select ON utda.company_aggregation_runs IS
    'Isolates company-wide aggregation runs exclusively to utda_governance under COMPANY_AGGREGATION scope.';

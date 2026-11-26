-- UTDA Initial PostgreSQL Schema (Portable Standard PostgreSQL / Supabase)
-- Stores durable automation metadata, idempotency, correlation, approvals,
-- Master Record provenance, and Drive sync metadata. No binary DOCX files.

CREATE SCHEMA IF NOT EXISTS utda;

CREATE TABLE IF NOT EXISTS utda.schema_migrations (
    version VARCHAR(32) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    checksum_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('APPLIED', 'FAILED')),
    applied_by VARCHAR(128) NOT NULL,
    execution_ms INTEGER NOT NULL DEFAULT 0,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE IF NOT EXISTS utda.projects (
    project_id VARCHAR(32) PRIMARY KEY,
    project_name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    github_owner VARCHAR(128) NOT NULL,
    github_repo VARCHAR(255) NOT NULL,
    default_branch VARCHAR(128) NOT NULL DEFAULT 'main',
    drive_folder_id VARCHAR(255) NOT NULL DEFAULT '',
    template_version VARCHAR(64) NOT NULL DEFAULT '',
    registry_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    CONSTRAINT uq_projects_github_repo UNIQUE (github_owner, github_repo)
);

CREATE TABLE IF NOT EXISTS utda.template_releases (
    release_id VARCHAR(64) PRIMARY KEY,
    repository VARCHAR(255) NOT NULL,
    version_tag VARCHAR(64) NOT NULL UNIQUE,
    commit_sha VARCHAR(64) NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE IF NOT EXISTS utda.project_provisioning_runs (
    provisioning_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    template_release_id VARCHAR(64) REFERENCES utda.template_releases(release_id),
    checkpoint_state VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED', 'SKIPPED_DUPLICATE')),
    manifest_checksums JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_summary TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS utda.github_event_receipts (
    receipt_id VARCHAR(64) PRIMARY KEY,
    delivery_id VARCHAR(128) NOT NULL UNIQUE,
    event_name VARCHAR(64) NOT NULL,
    repository VARCHAR(255) NOT NULL,
    project_id VARCHAR(32) REFERENCES utda.projects(project_id) ON DELETE SET NULL,
    feature_id VARCHAR(128),
    git_sha VARCHAR(64) NOT NULL,
    pr_number INTEGER,
    report_blob_sha VARCHAR(64),
    payload_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE IF NOT EXISTS utda.feature_processing_states (
    state_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    feature_id VARCHAR(128) NOT NULL,
    status VARCHAR(64) NOT NULL,
    pr_number INTEGER,
    pr_merged_sha VARCHAR(64),
    report_blob_sha VARCHAR(64),
    correlated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    CONSTRAINT uq_feature_state_project_feature UNIQUE (project_id, feature_id)
);

CREATE TABLE IF NOT EXISTS utda.final_reports (
    report_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    feature_id VARCHAR(128) NOT NULL,
    feature_name VARCHAR(255) NOT NULL,
    change_type VARCHAR(64) NOT NULL,
    implementation_status VARCHAR(64) NOT NULL,
    report_path VARCHAR(512) NOT NULL,
    report_blob_sha VARCHAR(64) NOT NULL,
    git_commit_sha VARCHAR(64) NOT NULL,
    report_metadata JSONB NOT NULL,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    validated_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    CONSTRAINT uq_final_reports_blob UNIQUE (project_id, feature_id, report_blob_sha)
);

CREATE TABLE IF NOT EXISTS utda.idempotency_keys (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    scope VARCHAR(64) NOT NULL,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    feature_id VARCHAR(128) NOT NULL,
    report_blob_sha VARCHAR(64) NOT NULL,
    target_document_id VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED', 'SKIPPED')),
    locked_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    expires_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS utda.processing_attempts (
    attempt_id VARCHAR(64) PRIMARY KEY,
    event_receipt_id VARCHAR(64) REFERENCES utda.github_event_receipts(receipt_id) ON DELETE SET NULL,
    idempotency_key VARCHAR(128) NOT NULL REFERENCES utda.idempotency_keys(idempotency_key) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status VARCHAR(32) NOT NULL,
    retry_state VARCHAR(32) NOT NULL DEFAULT 'NONE',
    next_retry_at TIMESTAMPTZ,
    error_code VARCHAR(64),
    error_summary TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_attempt_key_num UNIQUE (idempotency_key, attempt_number)
);

CREATE TABLE IF NOT EXISTS utda.document_proposals (
    proposal_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    feature_id VARCHAR(128) NOT NULL,
    document_id VARCHAR(32) NOT NULL,
    section_id VARCHAR(255) NOT NULL,
    change_id VARCHAR(128) NOT NULL,
    approval_class VARCHAR(32) NOT NULL CHECK (approval_class IN ('routine', 'review', 'mandatory_human')),
    required_roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    branch_name VARCHAR(255),
    pr_number INTEGER,
    pr_url VARCHAR(512),
    pr_state VARCHAR(32) NOT NULL DEFAULT 'DRAFT',
    merged_commit_sha VARCHAR(64),
    pre_docx_sha256 VARCHAR(64),
    post_docx_sha256 VARCHAR(64),
    proposal_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    merged_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS utda.approval_records (
    approval_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    document_id VARCHAR(32) NOT NULL,
    section_id VARCHAR(255) NOT NULL,
    change_id VARCHAR(128) NOT NULL,
    approver_identity VARCHAR(255) NOT NULL,
    approver_role VARCHAR(64) NOT NULL,
    decision VARCHAR(32) NOT NULL CHECK (decision IN ('approved', 'rejected', 'changes_requested', 'auto_published')),
    evidence_reference VARCHAR(512) NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS utda.project_master_versions (
    master_version_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    document_id VARCHAR(32) NOT NULL DEFAULT 'MASTER' CHECK (document_id = 'MASTER'),
    commit_sha VARCHAR(64) NOT NULL,
    docx_sha256 VARCHAR(64) NOT NULL,
    pr_number INTEGER,
    eligible_for_aggregation BOOLEAN NOT NULL DEFAULT TRUE,
    merged_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    CONSTRAINT uq_project_master_commit UNIQUE (project_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS utda.company_aggregation_runs (
    aggregation_id VARCHAR(64) PRIMARY KEY,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    source_project_id VARCHAR(32) NOT NULL REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    source_master_version_id VARCHAR(64) NOT NULL REFERENCES utda.project_master_versions(master_version_id) ON DELETE CASCADE,
    source_commit_sha VARCHAR(64) NOT NULL,
    target_document_id VARCHAR(32) NOT NULL DEFAULT 'COMPANY_MASTER' CHECK (target_document_id = 'COMPANY_MASTER'),
    target_pr_number INTEGER,
    target_merged_sha VARCHAR(64),
    status VARCHAR(32) NOT NULL,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    aggregated_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE IF NOT EXISTS utda.drive_file_mappings (
    mapping_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) REFERENCES utda.projects(project_id) ON DELETE CASCADE,
    document_id VARCHAR(32) NOT NULL,
    github_repository VARCHAR(255) NOT NULL,
    github_path VARCHAR(512) NOT NULL,
    drive_file_id VARCHAR(255) NOT NULL,
    drive_folder_id VARCHAR(255) NOT NULL,
    last_synced_commit_sha VARCHAR(64),
    last_synced_sha256 VARCHAR(64),
    last_drive_version VARCHAR(64),
    last_synced_at TIMESTAMPTZ,
    drift_detected BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT uq_drive_mapping_repo_doc UNIQUE (github_repository, document_id),
    CONSTRAINT uq_drive_mapping_file UNIQUE (drive_file_id)
);

CREATE TABLE IF NOT EXISTS utda.drive_sync_revisions (
    sync_id VARCHAR(64) PRIMARY KEY,
    mapping_id VARCHAR(64) NOT NULL REFERENCES utda.drive_file_mappings(mapping_id) ON DELETE CASCADE,
    github_commit_sha VARCHAR(64) NOT NULL,
    github_sha256 VARCHAR(64) NOT NULL,
    drive_version VARCHAR(64),
    drive_md5_checksum VARCHAR(64),
    backup_drive_file_id VARCHAR(255),
    sync_status VARCHAR(32) NOT NULL CHECK (sync_status IN ('SYNCED', 'DRIFT_DETECTED', 'SYNC_FAILED', 'CONFLICT_ABORTED')),
    drift_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE IF NOT EXISTS utda.rollback_operations (
    rollback_id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(32) REFERENCES utda.projects(project_id) ON DELETE SET NULL,
    document_id VARCHAR(32) NOT NULL,
    target_scope VARCHAR(32) NOT NULL CHECK (target_scope IN ('GITHUB', 'DRIVE')),
    restored_from_ref VARCHAR(255) NOT NULL,
    restored_sha256 VARCHAR(64) NOT NULL,
    operator_identity VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW())
);

CREATE TABLE IF NOT EXISTS utda.audit_events (
    event_id VARCHAR(64) PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc', NOW()),
    actor VARCHAR(255) NOT NULL,
    event_type VARCHAR(128) NOT NULL,
    project_id VARCHAR(32) REFERENCES utda.projects(project_id) ON DELETE SET NULL,
    feature_id VARCHAR(128),
    document_id VARCHAR(32),
    idempotency_key VARCHAR(128),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

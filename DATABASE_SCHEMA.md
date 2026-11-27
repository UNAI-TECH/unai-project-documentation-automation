# UTDA Database Schema & Connection Architecture

## 1. Infrastructure Scope
- **Provider:** Dedicated Supabase project (`unai-utda`, Supabase Free Plan, managed PostgreSQL).
- **Isolation:** Dedicated solely to UTDA automation metadata, idempotency, correlation, approvals, and audit trails. No application databases share this project, and **no DOCX binaries** are stored in PostgreSQL.
- **Portability:** Uses standard PostgreSQL DDL (`utda` schema, `TIMESTAMPTZ` UTC timestamps, `JSONB`, standard constraints, `pg_advisory_lock`) with zero Supabase-proprietary extensions.
- **Driver:** `psycopg` v3 (`psycopg[binary]`, `psycopg_pool`).

---

## 2. Supabase Connection Modes & Selection

| Mode | Endpoint Pattern | IPv4 / IPv6 | Capabilities & Limits | UTDA Usage |
| --- | --- | --- | --- | --- |
| **Direct Connection** | `db.<ref>.supabase.co:5432` | **IPv6 required** on Free Plan (IPv4 requires paid add-on) | Full PostgreSQL session support (DDL, advisory locks, prepared statements) | Optional for IPv6-capable admin workstations; **not assumed** for GitHub-hosted runners |
| **Session Pooler (Supavisor)** | `aws-0-<region>.pooler.supabase.com:5432` | **IPv4 + IPv6** | Preserves session state, advisory locks, prepared statements, and DDL transactions | **Selected primary mode** for GitHub Actions & schema migrations |
| **Transaction Pooler (Supavisor)** | `aws-0-<region>.pooler.supabase.com:6543` | **IPv4 + IPv6** | High-concurrency multiplexing; **no** persistent session state or prepared statements (`prepare_threshold=None`) | Optional for high-volume short-lived runtime queries; **blocked** by `runner.py` for DDL migrations |

---

## 3. Schema Entity Overview (`utda.*`)

Defined in [0001_initial_utda_schema.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql) and [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql):

| Table | Purpose | Key Constraints |
| --- | --- | --- |
| `utda.schema_migrations` | Tracks applied SQL migration versions & SHA-256 checksums | PK `(version)`, `status IN ('APPLIED', 'FAILED')` |
| `utda.projects` | Project registry mirror & Drive folder mapping | PK `(project_id)`, UNIQUE `(github_owner, github_repo)` |
| `utda.template_releases` | Repo A (`unai-project-documentation`) template releases | PK `(release_id)`, UNIQUE `(version_tag)` |
| `utda.project_provisioning_runs` | Checkpointed project provisioning runs | PK `(provisioning_id)`, FK `project_id`, FK `template_release_id` |
| `utda.github_event_receipts` | Deduplicated GitHub webhook/dispatch receipts | PK `(receipt_id)`, UNIQUE `(delivery_id)` |
| `utda.feature_processing_states` | PR merge & JSON report correlation (`WAITING_FOR_FINAL_REPORT`, `AWAIT_PR_MERGE`, `READY`, etc.) | PK `(state_id)`, UNIQUE `(project_id, feature_id)` |
| `utda.final_reports` | Canonical JSON report metadata & evidence provenance | PK `(report_id)`, UNIQUE `(project_id, feature_id, report_blob_sha)` |
| `utda.idempotency_keys` | Atomic locking & deduplication per `(project, feature, blob, doc)` | PK `(idempotency_key)` |
| `utda.processing_attempts` | Execution attempt & retry state history | PK `(attempt_id)`, UNIQUE `(idempotency_key, attempt_number)` |
| `utda.document_proposals` | Documentation branches, PRs, merged commit SHAs, DOCX SHA-256s | PK `(proposal_id)`, FK `project_id` |
| `utda.approval_records` | Role-aware approval records (`approval_record.schema.json`) | PK `(approval_id)`, FK `project_id` |
| `utda.project_master_versions` | Merged project `MASTER` versions eligible for aggregation | PK `(master_version_id)`, UNIQUE `(project_id, commit_sha)` |
| `utda.company_aggregation_runs` | Incremental `COMPANY_MASTER` aggregation runs & provenance | PK `(aggregation_id)`, UNIQUE `(idempotency_key)` |
| `utda.drive_file_mappings` | GitHub path ↔ Google Drive `file_id`, checksum & `drift_detected` | PK `(mapping_id)`, UNIQUE `(github_repository, document_id)`, UNIQUE `(drive_file_id)` |
| `utda.drive_sync_revisions` | GitHub-to-Drive sync outcomes (`SYNCED`, `DRIFT_DETECTED`, `SYNC_FAILED`, `CONFLICT_ABORTED`) | PK `(sync_id)`, FK `mapping_id` |
| `utda.rollback_operations` | GitHub & Drive rollback audit records | PK `(rollback_id)` |
| `utda.audit_events` | Role-enforced append-only UTC audit trail (`JSONB` details, no binaries/secrets) | PK `(event_id)` |

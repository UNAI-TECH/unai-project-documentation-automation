# UTDA Database Security & Access Control

This document is strictly synchronized with [0001_initial_utda_schema.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql), [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql), [engine/migrations/runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py), and [engine/db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py).

---

## 1. Credential, Transport & Identifier Security
- **Mandatory Separate Credentials:**
  - `UTDA_DATABASE_URL` (`utda_runtime`): required for runtime automation.
  - `UTDA_MIGRATION_DATABASE_URL` (`utda_migrator`): **mandatory** for `runner.py --apply` and `apply_migrations(..., dry_run=False)`; silent fallback to runtime `UTDA_DATABASE_URL` is prohibited.
  - `UTDA_GOVERNANCE_DATABASE_URL` (`utda_governance`): planned separate credential for Phase 2/6 approval and rollback workflows.
- **Supabase Key Rejection:** [engine/db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py) rejects `http://`/`https://` URLs, JWT strings (`eyJ...`), and Supabase `anon` / `service_role` keys (`sb_publishable_`, `sb_secret_`).
- **Independent TLS Enforcement:** Both runtime and migration DSNs are validated independently (`_validate_ssl_settings`). Remote `sslmode=disable`, `allow`, or `prefer` in either env vars or DSN query parameters is rejected.
- **Identifier Validation & Quoting:** Schema and table names are validated against `^[a-z_][a-z0-9_]{0,62}$` (`validate_identifier`) and double-quoted (`quote_ident`) before SQL composition. Data values use parameterized `%s` bindings.
- **Log Sanitization:** `DatabaseConfig.__repr__`, `redact_dsn()`, `SanitizingFilter` (`sanitize_log_text()`), and `AuditStore._scrub_value()` redact DSN passwords, sensitive URI query parameters (`password`, `pass`, `pwd`, `apikey`, `api_key`, `token`, `secret`, `access_token`, `jwt`), GitHub tokens (`ghp_...`, `github_pat_...`), Supabase keys (`sb_publishable_...`, `sb_secret_...`), API keys (`sk-...`), Bearer tokens, JWTs, and PEM private keys before logging or artifact export.

---

## 2. Explicit Migration Ownership & DDL Authority Model

`GRANT ALL PRIVILEGES` does **not** confer PostgreSQL DDL ownership (`ALTER TABLE`, `DROP INDEX`, `ALTER DEFAULT PRIVILEGES`).

1. **Preferred Direct `utda_migrator` Path:** Connect directly via `UTDA_MIGRATION_DATABASE_URL` authenticated as `utda_migrator` (with schema `utda AUTHORIZATION utda_migrator` pre-created or `CREATE ON DATABASE` granted). [engine/migrations/runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py) verifies `SELECT CURRENT_USER` == `'utda_migrator'` before applying any migration.
2. **Bootstrap Ownership-Transfer Fallback:** If an authorized member of `utda_migrator` (e.g., `postgres` on initial manual bootstrap) runs `0001` first, `0002_roles_and_indexes.sql` transfers ownership of schema `utda`, all tables, and all sequences to `utda_migrator`. Standard CI/automation `--apply` rejects non-`utda_migrator` connections.
3. **Fail-Closed Checks (`0002` & `runner.py`):**
   - Fails closed if `utda_migrator`, `utda_runtime`, or `utda_governance` does not exist.
   - Fails closed if `utda_runtime` inherits `utda_migrator` or `utda_governance`, or if `utda_governance` inherits `utda_migrator`.
   - Rejects Transaction Pooler (`port=6543` / `transaction`), modified historical checksums (`MigrationChecksumError`), and unknown DB migration versions absent from the repo (`UnknownMigrationError`).
4. **Default Privileges for Future Tables:** `ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda` grants **only `SELECT`** to `utda_runtime` and `utda_governance` and revokes all access from `PUBLIC`, `anon`, and `authenticated` on future tables/sequences created by `utda_migrator`.

---

## 3. Exact Role-to-Privilege Matrix (`0002_roles_and_indexes.sql`)

> **Important Distinction on Append-Only Tables:** Revoking `UPDATE` and `DELETE` from `utda_runtime` / `utda_governance` provides **role-enforced append-only** protection for those application roles. These tables are **not** absolutely immutable against the table owner (`utda_migrator`), PostgreSQL superusers, or Supabase admin roles (`postgres` / `service_role`).

| Object | `utda_migrator` | `utda_runtime` (Phase 1 Core Engine) | `utda_governance` (Approval & Rollback) | `PUBLIC` / `anon` / `authenticated` |
| --- | --- | --- | --- | --- |
| Schema `utda` | **OWNER**, `USAGE, CREATE` | `USAGE` only (no `CREATE`) | `USAGE` only (no `CREATE`) | None (explicitly revoked) |
| `utda.projects` | **OWNER** (all DDL/DML) | `SELECT, INSERT, UPDATE` | `SELECT` | None |
| `utda.feature_processing_states` | **OWNER** | `SELECT, INSERT, UPDATE` | None | None |
| `utda.idempotency_keys` | **OWNER** | `SELECT, INSERT, UPDATE` | None | None |
| `utda.processing_attempts` | **OWNER** | `SELECT, INSERT, UPDATE` | None | None |
| `utda.github_event_receipts` | **OWNER** | `SELECT, INSERT` *(role-enforced append-only)* | None | None |
| `utda.final_reports` | **OWNER** | `SELECT, INSERT` *(role-enforced append-only)* | None | None |
| `utda.audit_events` | **OWNER** | `SELECT, INSERT` *(role-enforced append-only)* | `SELECT, INSERT` *(role-enforced append-only)* | None |
| `utda.approval_records` | **OWNER** | **`SELECT` only** (cannot write) | `SELECT, INSERT` *(role-enforced append-only)* | None |
| `utda.rollback_operations` | **OWNER** | **`SELECT` only** (cannot write) | `SELECT, INSERT, UPDATE` (no `DELETE`) | None |
| `utda.schema_migrations` | **OWNER** | `SELECT` only | None | None |
| `utda.template_releases` | **OWNER** | `SELECT` only | None | None |
| `utda.project_provisioning_runs` | **OWNER** | `SELECT` only | None | None |
| `utda.document_proposals` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.project_master_versions` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.company_aggregation_runs` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.drive_file_mappings` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.drive_sync_revisions` | **OWNER** | `SELECT` only | `SELECT` | None |
| Future tables by `utda_migrator` | **OWNER** | `SELECT` only (default privilege) | `SELECT` only (default privilege) | None (explicitly revoked) |

---

## 4. Supabase API Roles (`anon`, `authenticated`, `service_role`)
1. `0002_roles_and_indexes.sql` explicitly revokes schema, table, sequence, and future default privileges in `utda` from `anon` and `authenticated` when those roles exist.
2. In Supabase Dashboard → **API Settings**, exclude `utda` from PostgREST Exposed Schemas.
3. **`service_role` Caveat:** Do **not** assume excluding `utda` from PostgREST removes database-level privileges for Supabase `service_role` (or `postgres`). Keep the Supabase `service_role` secret strictly restricted to Supabase project administrators, never configure it in UTDA GitHub Actions or application code, and rely on `utda_runtime` / `utda_migrator` / `utda_governance` PostgreSQL DSNs only.

---

## 5. Separately Controlled Authorization Path for Approval & Rollback Writes
- `utda_runtime` holds `SELECT` only on `utda.approval_records` and `utda.rollback_operations`.
- Privileged approval persistence (`approval_router.py`, Phase 2) and rollback operations (`rollback.py`, Phase 6) must authenticate with `UTDA_GOVERNANCE_DATABASE_URL` (`utda_governance`) inside reviewer-gated GitHub Environments (`utda-production`), isolated from ordinary feature/CI runs and Repo C.

---

## 6. One-Time DBA Role Provisioning (Preferred Direct `utda_migrator` Model)
```sql
CREATE ROLE utda_migrator NOINHERIT LOGIN PASSWORD '<STRONG_MIGRATOR_PASSWORD>';
CREATE ROLE utda_runtime NOINHERIT LOGIN PASSWORD '<STRONG_RUNTIME_PASSWORD>';
CREATE ROLE utda_governance NOINHERIT LOGIN PASSWORD '<STRONG_GOVERNANCE_PASSWORD>';

-- Preferred model: pre-create schema owned by utda_migrator so utda_migrator runs 0001 and 0002 directly:
CREATE SCHEMA IF NOT EXISTS utda AUTHORIZATION utda_migrator;

-- Harden public schema against runtime and governance roles:
REVOKE CREATE ON SCHEMA public FROM utda_runtime, utda_governance;
```
*(If using the legacy bootstrap transfer path where `postgres` runs `0001` before transferring ownership in `0002`, temporarily grant `GRANT utda_migrator TO postgres;` and run `0002` before revoking bootstrap access; direct `utda_migrator` execution is preferred.)*

---

## 7. Unresolved Security Limitations
1. **Governance Python Layer Pending:** `utda_governance` DB privileges are enforced and verified on PostgreSQL 16, while Phase 2 (`approval_router.py`) and Phase 6 (`rollback.py`) callers are not yet implemented.
2. **Application-Level Tenant Isolation:** Cross-project isolation (`project_id`) is enforced in engine logic rather than PostgreSQL RLS because `utda_runtime` serves central Repo B automation.
3. **Supabase Free Plan Network & Backup Limits:** No IP allowlisting, private VPC, or managed PITR backups; relies on TLS (`require`/`verify-full`) + external `pg_dump` backups.
4. **Hosted Supabase Verification Pending:** Live checks against `unai-utda` (including PostgREST exposure settings and `service_role` isolation audit) remain unexecuted until owner review and approval.

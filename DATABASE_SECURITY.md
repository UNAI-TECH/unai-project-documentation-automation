# UTDA Database Security & Access Control

This document is strictly synchronized with [0001_initial_utda_schema.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql), [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql), [engine/migrations/runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py), and [engine/db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py).

---

## 1. Credential, Transport & Identifier Security
- **Environment Secrets Only:** `UTDA_DATABASE_URL` (`utda_runtime`), `UTDA_MIGRATION_DATABASE_URL` (`utda_migrator`), and future privileged `UTDA_GOVERNANCE_DATABASE_URL` (`utda_governance`) are supplied exclusively via GitHub Environment Secrets or gitignored `.env`.
- **Supabase Key Rejection:** [engine/db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py) rejects `http://`/`https://` URLs, JWT strings (`eyJ...`), and Supabase `anon` / `service_role` keys (`sb_publishable_`, `sb_secret_`).
- **TLS Enforcement:** Remote connections enforce `sslmode=require`, `verify-ca`, or `verify-full` (with `UTDA_DATABASE_SSLROOTCERT`). `disable`/`allow`/`prefer` are rejected for all non-local hosts.
- **Identifier Validation & Quoting:** Schema and table names are validated against `^[a-z_][a-z0-9_]{0,62}$` (`validate_identifier`) and double-quoted (`quote_ident`) before any SQL composition. All data values use parameterized `%s` bindings.
- **Log Sanitization:** `DatabaseConfig.__repr__`, `redact_dsn()`, `SanitizingFilter`, and `AuditStore._scrub_value()` scrub DSN passwords, API keys, and GitHub tokens before logging or artifact export.

---

## 2. Explicit Migration Ownership & DDL Authority Model

`GRANT ALL PRIVILEGES` does **not** confer PostgreSQL DDL ownership (`ALTER TABLE`, `DROP INDEX`, `ALTER DEFAULT PRIVILEGES`). Therefore:
1. **`utda_migrator` owns `schema utda` and every table in `utda.*`:**
   - In [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql), if the bootstrap administrator (`postgres` on Supabase) runs initial migrations, it transfers ownership via `ALTER SCHEMA utda OWNER TO utda_migrator` and `ALTER TABLE utda.<tbl> OWNER TO utda_migrator`.
   - For that ownership transfer to succeed under non-superuser managed PostgreSQL (such as Supabase's `postgres` role), the bootstrap administrator must first be granted membership in `utda_migrator` (`GRANT utda_migrator TO postgres;`).
2. **Fail-Closed Role Check:** `0002_roles_and_indexes.sql` raises an exception and rolls back if `utda_migrator`, `utda_runtime`, or `utda_governance` does not exist.
3. **Default Privileges for Future Tables:** `ALTER DEFAULT PRIVILEGES FOR ROLE utda_migrator IN SCHEMA utda` revokes `PUBLIC` access and grants **only `SELECT`** to `utda_runtime` and `utda_governance` on any future table created by `utda_migrator`, preventing accidental `INSERT`/`UPDATE`/`DELETE` exposure until a migration adds explicit table-by-table grants.
4. **Migration Integrity:** [engine/migrations/runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py) blocks Transaction Pooler (`port=6543` / `pool_mode=transaction`), acquires a session `pg_advisory_lock`, detects **both** modified historical checksums (`MigrationChecksumError`) and unknown DB versions absent from the repo (`UnknownMigrationError`), and rolls back failed/interrupted migrations atomically.

---

## 3. Exact Role-to-Privilege Matrix (`0002_roles_and_indexes.sql`)

| Object | `utda_migrator` | `utda_runtime` (Phase 1 Core Engine) | `utda_governance` (Privileged Approval & Rollback) | `PUBLIC` / `anon` / `authenticated` |
| --- | --- | --- | --- | --- |
| Schema `utda` | **OWNER**, `USAGE, CREATE` | `USAGE` only (no `CREATE`) | `USAGE` only (no `CREATE`) | None (revoked) |
| `utda.projects` | **OWNER** (all DDL/DML) | `SELECT, INSERT, UPDATE` | `SELECT` | None |
| `utda.feature_processing_states` | **OWNER** | `SELECT, INSERT, UPDATE` | None (unless explicitly granted) | None |
| `utda.idempotency_keys` | **OWNER** | `SELECT, INSERT, UPDATE` | None | None |
| `utda.processing_attempts` | **OWNER** | `SELECT, INSERT, UPDATE` | None | None |
| `utda.github_event_receipts` | **OWNER** | `SELECT, INSERT` (immutable) | None | None |
| `utda.final_reports` | **OWNER** | `SELECT, INSERT` (immutable) | None | None |
| `utda.audit_events` | **OWNER** | `SELECT, INSERT` (immutable) | `SELECT, INSERT` (immutable) | None |
| `utda.approval_records` | **OWNER** | **`SELECT` only** (cannot write) | `SELECT, INSERT` (immutable) | None |
| `utda.rollback_operations` | **OWNER** | **`SELECT` only** (cannot write) | `SELECT, INSERT, UPDATE` (no `DELETE`) | None |
| `utda.schema_migrations` | **OWNER** | `SELECT` only | None | None |
| `utda.template_releases` | **OWNER** | `SELECT` only | None | None |
| `utda.project_provisioning_runs` | **OWNER** | `SELECT` only | None | None |
| `utda.document_proposals` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.project_master_versions` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.company_aggregation_runs` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.drive_file_mappings` | **OWNER** | `SELECT` only | `SELECT` | None |
| `utda.drive_sync_revisions` | **OWNER** | `SELECT` only | `SELECT` | None |
| Future tables by `utda_migrator` | **OWNER** | `SELECT` only (default privilege) | `SELECT` only (default privilege) | None |

---

## 4. Separately Controlled Authorization Path for Approval & Rollback Writes

Because `utda_runtime` has `SELECT`-only access on `utda.approval_records` and `utda.rollback_operations`, ordinary feature/webhook processing **cannot** forge approval decisions or record rollback operations:
1. **Dedicated Role (`utda_governance`):** Privileged approval persistence (`approval_router.py`, Phase 2) and rollback execution (`rollback.py`, Phase 6) authenticate with a separate connection credential (`UTDA_GOVERNANCE_DATABASE_URL`, backed by `utda_governance`).
2. **Environment Isolation:** `UTDA_GOVERNANCE_DATABASE_URL` is stored only in protected GitHub Environments (`utda-production` / gated governance workflows) with required human reviewers—never in ordinary `ci-*` or Phase 1 event-ingestion environments, and never exposed to Repo C.
3. **Immutability:** Even `utda_governance` has `SELECT, INSERT` only on `approval_records` and `audit_events` (no `UPDATE` or `DELETE`), and no `DELETE` on `rollback_operations`.

---

## 5. One-Time DBA Role Provisioning (Run by Supabase Owner Before First Migration)

```sql
CREATE ROLE utda_migrator NOINHERIT LOGIN PASSWORD '<STRONG_MIGRATOR_PASSWORD>';
CREATE ROLE utda_runtime NOINHERIT LOGIN PASSWORD '<STRONG_RUNTIME_PASSWORD>';
CREATE ROLE utda_governance NOINHERIT LOGIN PASSWORD '<STRONG_GOVERNANCE_PASSWORD>';

-- Required if initial bootstrap runs as Supabase 'postgres' before switching to utda_migrator:
GRANT utda_migrator TO postgres;

-- Harden public schema against runtime/governance roles:
REVOKE CREATE ON SCHEMA public FROM utda_runtime;
REVOKE CREATE ON SCHEMA public FROM utda_governance;
```
*(Exclude `utda` from Supabase Dashboard -> API Settings -> Exposed Schemas so PostgREST never serves `utda.*`.)*

---

## 6. Unresolved Security Limitations

1. **Governance Role Enforcement in Engine Code:** `utda_governance` SQL permissions are enforced at the database layer now, while the Phase 2 (`approval_router.py`) and Phase 6 (`rollback.py`) Python callers and separate `UTDA_GOVERNANCE_DATABASE_URL` environment config are not implemented yet.
2. **No Database-Layer Row-Level Tenant Isolation (RLS):** Project isolation (`project_id`) is enforced by application queries and Repo B workflow boundaries rather than per-project PostgreSQL roles/RLS, because `utda_runtime` serves the central Repo B engine across projects.
3. **Supabase Free Plan Network & Backup Constraints:** Free Plan lacks IP allowlisting/private VPC peering and automated PITR backups; transport relies on TLS + credentials (`sslmode=require`/`verify-full`) and external `pg_dump` backups.
4. **TLS Certificate Pinning:** Default `sslmode=require` encrypts transit without pinning the Supabase Root CA unless the owner provides `UTDA_DATABASE_SSLROOTCERT` with `sslmode=verify-full`.
5. **Hosted Verification Pending:** `unai-utda` credentials are not configured yet, so live Supabase role provisioning and migration verification remain pending owner approval.

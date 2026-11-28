# UTDA Phase 2B.3 — Database Row-Level Security (RLS), Migration `0005` & PostgreSQL 16 Verification Report

## 1. Task Information & Commit Provenance

- **Task Name & Objective:** UTDA Phase 2B.3 — Design and implement database-level project row isolation via forward-only migration `0005_project_row_level_security.sql`, verify all 12 RLS isolation scenarios, `0001`–`0004` immutability, upgrade/rollback/idempotency, and `pg_dump`/`pg_restore` against real PostgreSQL 16 (`postgres:16-alpine`).
- **Date of Execution:** `2026-11-28`
- **Branch:** `setup/utda-foundation`
- **Prior Verified Baselines:** Phase 1 closure `c0deb74`; Phase 2B.1–2B.2 closure `7e1dc5c` (report `b00c0aa`)
- **Verified Phase 2B.3 Implementation & Test Commit SHA:** `6712928` (`67129284e92f83f51436ed304dd0e40fdf010592`)
  - `591b38c` — Added [PHASE2B3_RLS_ARCHITECTURE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/security/PHASE2B3_RLS_ARCHITECTURE.md), [0005_project_row_level_security.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0005_project_row_level_security.sql), GUC-literal preservation in [runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py), and `test_real_postgres_row_level_security_and_cross_project_isolation` in [test_database_phase1.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py).
  - `89a07cf` — Scoped raw verification queries in `test_real_postgres_concurrent_workers_and_atomic_completion` to `CI_CONC_TEST`.
  - `271a8b1` — Updated `test_real_postgres_bootstrap_ownership_transfer` expected applied migration list to include `0005_project_row_level_security`.
  - `6e83a3d` — Updated `UTDADatabaseRepository.claim_idempotency_and_attempt()` in [database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py) so an RLS-hidden cross-project `idempotency_key` collision (`not inserted_new and key_row is None`) raises `IdempotencyIdentityMismatchError` rather than `RuntimeError`, with matching `_FakeCursor` project scoping in unit tests.
  - `6712928` — Populated `migration_dsn` and `migration_sslmode` via `DatabaseConfig.from_migration_env(...)` for `rls_upg_cfg` in the isolated `utda_rls_upgrade_test` upgrade/rollback test.
- **Safety Restrictions Enforced:** `UTDA_DRY_RUN=true`; zero hosted Supabase connections; zero modifications to historical migrations `0001`–`0004`; zero new table `GRANT` statements; zero DOCX generation, Google Drive writes, OpenRouter calls, repository provisioning, or production GitHub PR writes.

---

## 2. RLS Architecture, Policy Matrix & Threat Model Summary

Full specification recorded in [PHASE2B3_RLS_ARCHITECTURE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/security/PHASE2B3_RLS_ARCHITECTURE.md) and implemented verbatim in [0005_project_row_level_security.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0005_project_row_level_security.sql):

### 2.1 Explicit Threat-Model Boundary (`utda.current_project_id`)

- **`utda.current_project_id` (and `utda.governance_scope`) is an application-provided transaction-local scoping context (`set_config(..., true)`), NOT an authenticated database tenant identity.** All project automation connects via the shared `utda_runtime` credential.
- **Mitigates:** Omitted or buggy `WHERE project_id = ...` clauses, accidental cross-project reads/inserts/updates inside project-scoped transactions, unset/empty/whitespace/malformed project contexts (`current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'`), `NULL` `project_id` leaks into runtime, and runtime access to `company_aggregation_runs` or `COMPANY_MASTER` rows.
- **Does NOT independently stop:** A compromised `utda_runtime` credential or arbitrary raw SQL executing `set_config('utda.current_project_id', 'OTHER_PROJ', true)`. Defense in depth remains `resolve_project()` identity resolution, parameterized `%s` queries, `0002` table privilege separation (no runtime `DELETE`, no runtime writes to `approval_records`/`rollback_operations`/`schema_migrations`), and separate `utda_governance` / `utda_migrator` roles.

### 2.2 Role Ownership, `NOINHERIT`, `BYPASSRLS`, and `FORCE ROW LEVEL SECURITY` Decision

- Migration `0005` fails closed unless `utda_runtime` and `utda_governance` both exist with `rolsuper = false`, `rolbypassrls = false`, `rolinherit = false`, and zero membership in privileged roles (`pg_has_role(...)`).
- All 15 project-scoped / hybrid tables enable **`ENABLE ROW LEVEL SECURITY` without `FORCE ROW LEVEL SECURITY`**:
  - **`utda_runtime` & `utda_governance`** are non-owners, non-superusers, and lack `BYPASSRLS`, so PostgreSQL 16 strictly enforces RLS on every query and mutation they issue.
  - **`utda_migrator`** remains table owner so schema migrations and `pg_dump --schema=utda` backups execute without exporting 0 rows.
- Global catalog/migration metadata tables **`utda.schema_migrations`** and **`utda.template_releases`** intentionally keep RLS disabled so health probes (`db_health.py`) remain operational without a project context.
- Every policy uses explicit single-role targeting (`AS PERMISSIVE FOR <CMD> TO utda_runtime` or `TO utda_governance`, never `TO PUBLIC`), preserving exact `0002` table grants with **zero** new `GRANT`s.
- **`utda.company_aggregation_runs`** intentionally defines **zero permissive policies for `utda_runtime`** (PostgreSQL default-deny: `0` rows on `SELECT`), while allowing `utda_governance` read access only under `utda.governance_scope = 'COMPANY_AGGREGATION'`.

---

## 3. PostgreSQL 16 Real CI Verification Results (`6712928`)

### 3.1 GitHub Actions Runs Executed Against Commit `6712928`

| Workflow | Run ID | URL | Environment | Job & Result | Steps | Artifacts |
| --- | --- | --- | --- | --- | --- | --- |
| `UTDA Database Integration CI` (`.github/workflows/ci-database.yml`) | **`46120627657`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46120627657 | `postgres:16-alpine` service container (`ubuntu-latest`, Python 3.12, `psql`) | `database-tests`: **`completed: success`** | Steps 1–12 + post/complete steps all **`success`** (`22` post-setup-python `skipped` as standard cleanup) | `utda-pg16-security-ci-46120627657` (`id: 15254110863`, `3320` bytes) |
| `UTDA Phase 0 Baseline Validation` (`.github/workflows/ci-baseline.yml`) | **`46120627730`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46120627730 | `ubuntu-latest` | `validate-contracts`: **`completed: success`** | All steps **`success`** | — |

### 3.2 CI Steps & Real PostgreSQL 16 Security Checklist Coverage

In Run `46120627657` (commit `6712928`), CI executed and passed:
- **Step 7 (`Verify fail-closed checks`):** Confirmed missing `UTDA_MIGRATION_DATABASE_URL`, non-`utda_migrator` migration connection, and missing required roles all fail closed.
- **Step 8 (`Provision roles and apply migrations directly as utda_migrator on PostgreSQL 16`):** Provisioned non-superuser `NOINHERIT` roles `utda_migrator`, `utda_runtime`, `utda_governance`, `anon`, `authenticated`; executed `runner --plan`, `runner --apply` (applying `0001`–`0005` as `utda_migrator`), and `db_health --json`.
- **Step 9 (`Run full Phase 0 and Phase 1 PostgreSQL 16 security checklist suite`):** Executed full `pytest` suite including all 6 real PG16 tests (`test_live_supabase_readonly_health_check` remains intentionally skipped without hosted creds):
  1. `test_real_local_postgres_migrations_and_persistence` — Idempotent `0001`–`0005` re-run, advisory lock serialization, `0006_broken` rollback, `0001` tamper checksum rejection, and `0003`/`0004` upgrade checks.
  2. `test_real_postgres_least_privilege_and_default_privileges` — Ownership, `NOINHERIT`, role isolation, `anon`/`authenticated`/`PUBLIC` denials, `utda_runtime` + `utda_governance` allowed vs denied statements, and future table default privileges.
  3. `test_real_postgres_bootstrap_ownership_transfer` — Bootstrap `0001`–`0005` apply + `utda_migrator` ownership transfer on `utda_bootstrap_test`.
  4. `test_real_postgres_concurrent_workers_and_atomic_completion` — 8-worker concurrency claim, cross-project `idempotency_key` collision under RLS (`IdempotencyIdentityMismatchError`), expired lease recovery, stale attempt rejection, `pool_size=1` `utda.current_project_id` reset lifecycle, and ambiguous-commit reconciliation.
  5. `test_real_postgres_row_level_security_and_cross_project_isolation` — Covered all 12 Phase 2B.3 criteria on real PostgreSQL 16:
     - Role attributes (`rolsuper=false, rolbypassrls=false, rolinherit=false`) and `relrowsecurity=true, relforcerowsecurity=false, relowner='utda_migrator'` across all 15 RLS tables (`false` on `schema_migrations` & `template_releases`).
     - Isolated schema (`utda_rls_upgrade_test`) upgrade from `0001`–`0004` $\to$ broken `0005` rollback (`relrowsecurity=false`) $\to$ clean `0005` apply & idempotent re-run (`pending=[]`, `applied=[]`).
     - Two isolated projects (`RLS_PROJ_A`, `RLS_PROJ_B`) plus `NULL`-project and `COMPANY_MASTER` rows seeded as owner `utda_migrator`.
     - Missing (`None`) and malformed (`""`, `"   "`, `"rls_proj_a"`, `" RLS_PROJ_A "`) contexts fail closed (`0` rows across all 15 RLS tables, `InsufficientPrivilege` on `INSERT`).
     - `utda_runtime` under `RLS_PROJ_A` reads only A's permitted rows (`0` from B, `NULL`, `COMPANY_MASTER`, `company_aggregation_runs` even when runtime attempts `set_config('utda.governance_scope', 'COMPANY_AGGREGATION', true)`), blocks cross-project/NULL `INSERT` (`InsufficientPrivilege`), blocks cross-project `UPDATE` (`0` rows) & reassignment (`InsufficientPrivilege`), blocks all `DELETE` (`InsufficientPrivilege`), and succeeds on A's permitted `UPDATE`/`INSERT`.
     - `utda_governance` denied when context unset; scoped to A for `approval_records`/`rollback_operations` under `RLS_PROJ_A` (`0` rows on `company_aggregation_runs`); and scoped to eligible `project_master_versions`, `company_aggregation_runs`, `COMPANY_MASTER` `drive_file_mappings` & `drive_sync_revisions` only under `utda.governance_scope = 'COMPANY_AGGREGATION'` (while still rejecting non-`COMPANY_MASTER` `NULL` rollback inserts).
     - Parent-child invoker RLS on `processing_attempts` $\leftrightarrow$ `idempotency_keys` and `drive_sync_revisions` $\leftrightarrow$ `drive_file_mappings` prevents cross-project reads/inserts/reassignments under `utda_runtime`, while table owner `utda_migrator` performs cascading maintenance deletes cleanly.
  6. `test_real_postgres_full_event_correlation_and_recovery` — Full GitHub PR/report correlation and recovery on PG16.
- **Step 10 (`Verify pg_dump backup and pg_restore recovery`):** `pg_dump --format=custom --schema=utda` $\to$ `pg_restore --exit-on-error` into `utda_restore_verify` verified restored `schema_migrations` (`0001`–`0005`), `CI_SEC_TEST` data, `utda_migrator` table ownership, and `db_health --json`.
- **Step 11 (`Verify read-only Supabase probe defaults cleanly to pending when unset`):** Exited `0` with `status: "pending"`.

*(Limitation: Unauthenticated GitHub REST API blocks artifact ZIP download (`HTTP 401`) and raw step log text (`HTTP 403`), so individual per-case XML contents inside `utda-pg16-security-ci-46120627657` were not inspected directly; job `completed: success` across all active steps 1–12 proves clean execution and `pytest` exit `0`.)*

---

## 4. Migration Integrity & Checksum Verification (`0001`–`0005`)

Verified via `discover_migrations(Path('engine/migrations'))` and `git diff --stat c0deb74 -- engine/migrations/0001*..0004*` (empty diff):

| Migration File | Status | SHA-256 Checksum | Preserved Since `c0deb74`? |
| --- | --- | --- | --- |
| `0001_initial_utda_schema.sql` | Baseline | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | **Exact Match** (0 changes) |
| `0002_roles_and_indexes.sql` | Baseline | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | **Exact Match** (0 changes) |
| `0003_idempotency_fencing.sql` | Baseline | `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b` | **Exact Match** (0 changes) |
| `0004_idempotency_fence_version_hardening.sql` | Baseline | `7060f50e1820b12194c21cac19c0505454d2a7527c941aac4917e2d2032567ad` | **Exact Match** (0 changes) |
| `0005_project_row_level_security.sql` | **New (Forward-Only)** | `730a085605fc76475dcfd29187a81615b2e3f39d9830886aa4d35d1b38eccae9` | N/A (new migration) |

---

## 5. Local Windows Checks & Remaining Non-Scope Confirmations

- **Local syntax & unit suite (`6712928`):**
  - `.venv\Scripts\python.exe -m compileall -q engine tests` $\to$ Exit **`0`**.
  - `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs` $\to$ **`31 passed, 0 failed, 7 skipped in 21.13s`** (the 6 real PG16 CI tests run in Run `46120627657` above; `test_live_supabase_readonly_health_check` stays skipped intentionally without hosted credentials).
- **Hosted Supabase & External Services:** Zero connections made to hosted Supabase; `0005` was NOT applied to Supabase; zero Google Drive writes, OpenRouter calls, DOCX generation, repository provisioning, or production GitHub PR mutations occurred.

---

## 6. Assessment

**PASS WITH LIMITATIONS** — Forward-only migration `0005_project_row_level_security.sql` implements database-level project row isolation on all 15 project-scoped/hybrid tables without modifying `0001`–`0004` or granting premature Phase 2+ writes; real PostgreSQL 16 CI (`Run 46120627657`) and baseline contract validation (`Run 46120627730`) both completed with **`success`**; remaining limitations are the explicitly documented `utda.current_project_id` shared-role GUC trust boundary (application context rather than per-tenant DB authentication), non-execution against hosted Supabase, and GitHub's unauthenticated artifact download restriction.

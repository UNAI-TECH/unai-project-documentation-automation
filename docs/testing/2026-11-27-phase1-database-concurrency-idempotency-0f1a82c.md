# Testing & Verification Report — UTDA Phase 1 Database Concurrency and Idempotency Hardening

## 1. Task Information

- **Task Name & Objective:** UTDA Phase 1 — Database Concurrency and Idempotency Hardening (`engine/database.py`, schema migration `0003_idempotency_fencing.sql`, `engine/audit_store.py`, `engine/trigger.py`) against concurrent workers, stale processing attempts, idempotency-key identity reuse, ambiguous transaction commits, and duplicate receipt/report ID lookup.
- **Date of Execution:** `2026-11-27`
- **Branch Name:** `setup/utda-foundation`
- **Implementation Commit SHA:** `0f1a82c` (hardening implementation & PG16 tests)
- **Prior Baseline Commits Referenced:** `d1ee618`, `f034f4e`
- **Files Created:**
  - `engine/migrations/0003_idempotency_fencing.sql`
  - `docs/testing/README.md`
  - `docs/testing/2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md` (this report)
- **Files Modified:**
  - `engine/database.py`
  - `engine/audit_store.py`
  - `engine/trigger.py`
  - `tests/test_database_phase1.py`
  - `tests/test_phase1_event_pipeline.py`
- **Files Deleted:** None.
- **Historical Migrations Unchanged:** `engine/migrations/0001_initial_utda_schema.sql` and `engine/migrations/0002_roles_and_indexes.sql` were verified unmodified via `git diff --name-only -- engine/migrations/0001_initial_utda_schema.sql engine/migrations/0002_roles_and_indexes.sql` (empty diff).

---

## 2. Implementation Summary & Per-File Change Accountability

| File | What Changed | Why It Changed | Covering Tests | Execution Status | Verification Gaps |
| --- | --- | --- | --- | --- | --- |
| `engine/migrations/0003_idempotency_fencing.sql` *(new)* | Added `active_attempt_id`, `attempt_fence_version` to `utda.idempotency_keys`; created `idx_idempotency_keys_active_attempt`, `uq_processing_attempts_key_attempt`, and deferrable FK `fk_idempotency_active_attempt`. | Enables database-enforced active-attempt fencing without modifying applied migrations `0001` or `0002`. | `test_migrations_empty_db_upgrade_checksum_and_rollback`, `test_real_local_postgres_migrations_and_persistence`, `test_real_postgres_bootstrap_ownership_transfer`, `test_real_postgres_concurrent_workers_and_atomic_completion` | Executed locally (simulated) & in GitHub Actions PG16 CI (`45997758365`). | Hosted Supabase migration execution intentionally deferred. |
| `engine/database.py` | Added `StaleAttemptError`, `IdempotencyIdentityMismatchError`, and `DatabaseManager.run_readonly`; removed generic `retry_on_ambiguous_commit` from `transaction` / `run_in_transaction`; implemented fenced `claim_idempotency_and_attempt` (with `FOR UPDATE`, immutable 5-tuple identity check, active `attempt_id` binding) and fenced `finalize_processing_attempt` (requiring & verifying `active_attempt_id` and non-expired lease before state updates); added stable-ID reconciliation on `CommitOutcomeUnknownError`; updated `record_event_receipt` & `record_final_report` to return existing IDs on conflict; deprecated and fenced `acquire_idempotency_key`, `next_attempt_number`, `complete_idempotency_key`, and `record_processing_attempt`. | Prevents split-brain locks, stale-worker overwrites, idempotency-key identity hijacking, blind replay after ambiguous commits, missing IDs on duplicate receipts/reports, and un-fenced legacy bypasses. | `test_atomic_attempt_completion_concurrency_and_ambiguous_commit`, `test_event_receipt_correlation_idempotency_concurrency_and_audit`, `test_real_postgres_concurrent_workers_and_atomic_completion`, `test_real_postgres_least_privilege_and_default_privileges`, `test_real_local_postgres_migrations_and_persistence` | Executed locally (`_FakeDBState` / `_FakePool`) & in GitHub Actions PG16 CI (`45997758365`). | Live network drop during Supabase Supavisor session commit not tested. |
| `engine/audit_store.py` | Updated `claim_processing_lock` to return `(claimed, attempt_number, attempt_id)` and updated `record_attempt_outcome` to require `attempt_id`. | Propagates the active fencing token across the audit orchestration boundary. | `test_retry_recovery_after_failed_attempt_and_pending_supabase_probe`, `test_pr_first_then_report_push_correlation_and_duplicate_skip`, `test_real_postgres_full_event_correlation_and_recovery` | Executed locally (unit) & in GitHub Actions PG16 CI (`45997758365`). | None for Phase 1 boundary. |
| `engine/trigger.py` | Unpacked `attempt_id` from `audit.claim_processing_lock` and passed `attempt_id` to both success and failure paths of `audit.record_attempt_outcome`. | Ensures runtime trigger paths always present a valid active fencing token when finalizing or recording failure. | `test_pr_first_then_report_push_correlation_and_duplicate_skip`, `test_post_claim_failure_and_simulated_db_connection_outage`, `test_real_postgres_full_event_correlation_and_recovery` | Executed locally (unit) & in GitHub Actions PG16 CI (`45997758365`). | None for Phase 1 boundary. |
| `tests/test_database_phase1.py` | Upgraded `_FakeDBState` / `_FakeCursor` with transactional undo, fenced claim/finalize simulation, duplicate ID SELECTs, and identity checks; updated migration expectations for `0003`; added assertions & PG16 tests for concurrent claims, expired lock recovery, stale-worker rejection, identity mismatch, ambiguous commit reconciliation, duplicate IDs, legacy deprecation/fencing, and rollback recovery. | Exercises both simulated unit paths and real PostgreSQL 16 concurrency, locking, and constraint paths. | `tests/test_database_phase1.py` (config, simulated lifecycle, PG16 security/concurrency, and live Supabase health test). | **13 passed, 5 skipped** locally (`pytest -v -rs`); Step 9 executed `pytest` in CI (`45997758365`, `completed: success`), exact per-file CI JUnit counts not downloaded (`HTTP 401` on artifact ZIP). | `test_live_supabase_readonly_health_check` skipped intentionally; CI JUnit XML requires authenticated GitHub artifact access. |
| `tests/test_phase1_event_pipeline.py` | Updated `test_retry_recovery_after_failed_attempt_and_pending_supabase_probe` for `(claimed, att_no, attempt_id)` and updated `test_real_postgres_full_event_correlation_and_recovery` to recover expired locks using fenced `claim_idempotency_and_attempt` with matching `report_blob_sha`. | Aligns end-to-end event pipeline tests with active-attempt fencing and immutable request identity enforcement. | `tests/test_phase1_event_pipeline.py` (simulated pipeline unit tests + PG16 lifecycle test). | **9 passed, 1 skipped** locally (`pytest -v -rs`); Step 9 executed `pytest` in CI (`45997758365`, `completed: success`), exact per-file CI JUnit counts not downloaded (`HTTP 401` on artifact ZIP). | Per-case CI JUnit XML requires authenticated GitHub artifact access. |

---

## 3. Testing Performed

### 3.1 Locally Executed Tests & Static Checks (Windows Python `3.14.4`, `UTDA_DRY_RUN=true`)

1. **Full local unit & contract suite (`pytest`):**
   - **Command:** `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs`
   - **Test files executed:**
     - `tests/test_phase0_contracts.py` (7 contract/schema/workflow tests)
     - `tests/test_database_phase1.py` (13 local config, lifecycle, sanitization, fencing, reconciliation unit tests executed; 5 live DB/PG16 tests skipped because `UTDA_TEST_*` / `UTDA_RUN_LIVE_SUPABASE_TESTS` unset)
     - `tests/test_phase1_event_pipeline.py` (9 pipeline, Git integrity, correlation, outage, recovery unit tests executed; 1 PG16 test skipped)
   - **Observed Distinction:** Local unit tests in `tests/test_database_phase1.py` and `tests/test_phase1_event_pipeline.py` use `_FakeDBState` / `_FakePool` in-memory transaction simulation and temporary local Git repositories.
2. **Python syntax / bytecode check:**
   - **Command:** `.venv\Scripts\python.exe -m compileall -q engine tests`
3. **Linter / Type-Checker Configuration Check:**
   - Checked `requirements-dev.txt` (contains only `pytest>=8.0,<9` and `PyYAML>=6.0,<7`). No `ruff`, `flake8`, `black`, or `mypy` configuration or binary is installed in this repository.

### 3.2 GitHub Actions Integration & Regression Suites (`ubuntu-latest` + `postgres:16-alpine`)

1. **Contract Baseline Workflow (`.github/workflows/ci-baseline.yml`):**
   - **Command:** `python -m unittest discover -s tests -v`
2. **PostgreSQL 16 Database Integration Workflow (`.github/workflows/ci-database.yml`):**
   - **Migration Plan & Apply (as `utda_migrator`):** `python -m engine.migrations.runner --plan`, `python -m engine.migrations.runner --apply`, `python -m engine.db_health --json`
   - **Full Security, Concurrency & Pipeline Suite:** `python -m pytest -v -rs --junitxml=artifacts/pg16-junit.xml | tee artifacts/pg16-test-output.txt`
   - **Backup & Restore Verification (`postgres:16-alpine`):** `pg_dump --format=custom --schema=utda`, `pg_restore --exit-on-error`, row & `utda_migrator` ownership verification on `utda_restore_verify`
   - **Read-only Supabase Default Check:** `python -m engine.db_health --readonly-verify --json`

---

## 4. Testing Results

### 4.1 Local Execution Output (`0f1a82c`)

- **Passed:** **29**
- **Failed:** **0**
- **Skipped:** **6** (5 PG16 CI-only tests due to unset `UTDA_TEST_*`, 1 intentionally deferred `test_live_supabase_readonly_health_check`)
- **Duration:** `16.54s`
- **Errors / Warnings:** Expected `DeprecationWarning` emissions verified explicitly inside `warnings.catch_warnings` when testing deprecated legacy methods (`complete_idempotency_key`, `record_processing_attempt`).

Concise excerpt from local run:

```text
tests/test_database_phase1.py::Phase1DatabaseLifecycleAndPersistenceTests::test_atomic_attempt_completion_concurrency_and_ambiguous_commit PASSED [ 20%]
tests/test_database_phase1.py::Phase1DatabaseLifecycleAndPersistenceTests::test_event_receipt_correlation_idempotency_concurrency_and_audit PASSED [ 25%]
tests/test_database_phase1.py::Phase1DatabaseLifecycleAndPersistenceTests::test_migrations_empty_db_upgrade_checksum_and_rollback PASSED [ 31%]
tests/test_database_phase1.py::Phase1LivePostgreSQLTests::test_real_postgres_concurrent_workers_and_atomic_completion SKIPPED [ 48%]
tests/test_phase1_event_pipeline.py::Phase1EventPipelineTests::test_post_claim_failure_and_simulated_db_connection_outage PASSED [ 85%]
======================= 29 passed, 6 skipped in 16.54s ========================
```

### 4.2 CI Execution Results (`0f1a82c`) — [Reconciled 2026-11-28]

- **Observed from GitHub Actions API (`/actions/runs`, `/jobs`, `/artifacts`):**
  - `UTDA Phase 0 Baseline Validation` (`45997758185`): `completed: success`.
  - `UTDA Database Integration CI` (`45997758365`): `completed: success` across all active steps (`Steps 1–12`), uploading artifact `utda-pg16-security-ci-45997758365` (`id: 15196073867`, `3127` bytes, `sha256:5dc01e682ada76d6f8ee5b71df2546e88051cc0304aca8075c8c2c3d902feb13`).
- **Limitation on Per-Case CI Counts:** Unauthenticated requests to `/actions/artifacts/15196073867/zip` returned `HTTP 401 Unauthorized` (and `/logs` returned `HTTP 403`). Exact per-case `passed`/`skipped` counts inside CI `artifacts/pg16-junit.xml` and `artifacts/pg16-test-output.txt` were therefore not directly readable without authentication; do **not** infer exact CI passed/skipped counts from test source files until the CI artifact ZIP is supplied.
- **Follow-up Verification:** See [2026-11-28-phase1-verification-reconciliation-1576d19.md](2026-11-28-phase1-verification-reconciliation-1576d19.md) for fence-version, lease-renewal, migration `0003` constraint/upgrade, and Node 24 workflow updates.

---

## 5. Database Verification (`postgres:16-alpine`)

- **PostgreSQL Version:** `postgres:16-alpine` (Docker service container in GitHub Actions; `pg_dump` / `pg_restore` executed via matching `postgres:16-alpine` container).
- **Database State:** Clean isolated databases provisioned per CI run (`utda_test_db`, `utda_missing_roles_test`, `utda_bootstrap_test`, `utda_restore_verify`).
- **Migration Plan & Execution:**
  - Direct execution as `utda_migrator` applied `0001_initial_utda_schema`, `0002_roles_and_indexes`, and `0003_idempotency_fencing` cleanly; re-running `--plan` and `--apply` was idempotent (`[]`).
  - Bootstrap ownership-transfer path (`utda_bootstrap_test`) applied all 3 migrations cleanly and confirmed schema `utda` + all 17 tables owned by `utda_migrator`.
  - Fail-closed checks passed for missing `UTDA_MIGRATION_DATABASE_URL`, missing roles, non-`utda_migrator` identity, unknown DB versions (`UnknownMigrationError`), checksum drift (`MigrationChecksumError`), `FAILED` status history, and DDL rollback atomicity.
- **Permission & Security Boundary:** Confirmed `utda_runtime` and `utda_governance` multi-role isolation, `NOINHERIT`, `PUBLIC`/`anon`/`authenticated` zero effective privileges (via `has_schema_privilege`, `has_table_privilege`, and live `SET LOCAL ROLE`), and `SELECT`-only future table defaults.
- **Concurrency, Fencing & Idempotency Verification (Real PG16 CI):**
  - **Concurrent claims:** 8 threads racing on `claim_idempotency_and_attempt` yielded 1 winner (`attempt_number = 1`, active `attempt_id`).
  - **Expired lock recovery:** Expiring `expires_at` in PostgreSQL followed by 6 concurrent workers yielded 1 winner (`attempt_number = 2`, new `attempt_id`) and retired attempt #1 (`FAILED`, `LOCK_EXPIRED`).
  - **Stale-worker rejection:** Finalizing with attempt #1's `attempt_id` after Worker 2 claimed attempt #2 raised `StaleAttemptError`, leaving `idempotency_keys` (`IN_PROGRESS`, `active_attempt_id = att2_id`) and attempt #2 untouched; finalizing again after completion also raised `StaleAttemptError`.
  - **Immutable request identity:** Conflicting reuse of an `idempotency_key` with modified `scope`, `project_id`, `feature_id`, `report_blob_sha`, or `target_document_id` raised `IdempotencyIdentityMismatchError`.
  - **Ambiguous commit behavior:** Simulated post-commit `psycopg.OperationalError` drop was reconciled safely via stable `attempt_id` lookup without duplicating attempts or failing completion.
  - **Duplicate receipt & report IDs:** Duplicate `record_event_receipt` and `record_final_report` calls returned `inserted = False` along with the exact original `receipt_id` and `report_id`.
  - **Transaction rollback & recovery:** Mid-transaction aborted update left `attempt #2` clean for final completion.
- **Unexecuted Database Tests:** Hosted Supabase live checks (`test_live_supabase_readonly_health_check`, live Supavisor TLS handshake, hosted migrations, and Supabase PostgREST exposed-schema inspection) were intentionally **not executed**.

---

## 6. CI Verification

| Workflow Name | Run ID | Commit SHA | Status / Conclusion | Artifact Name & Notes | Run URL |
| --- | --- | --- | --- | --- | --- |
| `UTDA Phase 0 Baseline Validation` | `45997758185` | `0f1a82c` | `completed: success` | — | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45997758185 |
| `UTDA Database Integration CI` | `45997758365` | `0f1a82c` | `completed: success` (Steps 1–12 all `success`) | `utda-pg16-security-ci-45997758365` (`artifacts/pg16-junit.xml`, `artifacts/pg16-test-output.txt`) | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45997758365 |

---

## 7. Outstanding Issues

1. **Failed Tests:** None (`0 failed`).
2. **Skipped Tests:**
   - **Locally (6 skipped):** 5 real PostgreSQL 16 integration tests (`test_real_local_postgres_migrations_and_persistence`, `test_real_postgres_least_privilege_and_default_privileges`, `test_real_postgres_bootstrap_ownership_transfer`, `test_real_postgres_concurrent_workers_and_atomic_completion`, `test_real_postgres_full_event_correlation_and_recovery`) skip locally because Windows workspace lacks local PostgreSQL/Docker and `UTDA_TEST_*` variables (all 5 executed in CI run `45997758365`); `test_live_supabase_readonly_health_check` skips unless `UTDA_RUN_LIVE_SUPABASE_TESTS=true` and `UTDA_DATABASE_URL` are supplied.
   - **In CI (1 skipped):** `test_live_supabase_readonly_health_check` intentionally skipped (no hosted Supabase credentials in Phase 1).
3. **Known Limitations & Risks:**
   - Hosted Supabase `unai-utda` TLS/pooler connectivity, role provisioning, and PostgREST exposed-schema exclusion remain unverified until credentials and owner authorization are granted.
   - `utda_runtime` permissions remain table-level without Row-Level Security (RLS) scoped by `project_id`.
   - No `ruff`/`mypy`/`flake8` static lint/type-check suite is configured in `requirements-dev.txt`.
   - Unauthenticated GitHub API returns HTTP 403 on `/logs` and `/artifacts/*/zip`, so CI run `45997758365` was verified via workflow job & step status metadata rather than downloaded artifact XML.
4. **Recommended Next Actions:**
   - Commit this reporting index and report alongside branch updates.
   - When ready for hosted cutover verification, execute read-only `engine.db_health --readonly-verify --json` and verify `utda` schema exclusion in Supabase `Settings -> API -> Exposed schemas`.

---

## 8. Final Assessment

**PASS WITH LIMITATIONS**

All code changes, forward-only schema migration `0003_idempotency_fencing.sql`, local unit/contract tests (`29 passed, 6 skipped`), and GitHub Actions PostgreSQL 16 concurrency/fencing/security/backup integration tests (`Run 45997758365`, `completed: success`) passed. Hosted Supabase connectivity checks remain intentionally deferred, and automated lint/type tools (`ruff`/`mypy`) are not configured in the repository.

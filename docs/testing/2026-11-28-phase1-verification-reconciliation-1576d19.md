# Verification & Reconciliation Report — UTDA Phase 1 Concurrency, Fencing, Lease Renewal & CI Maintenance

## 1. Metadata

- **Task Name & Objective:** Final verification and evidence reconciliation of UTDA Phase 1 Database Concurrency and Idempotency Hardening (`0f1a82c`), verifying `attempt_fence_version` monotonic & completion-preservation semantics, implementing and testing fenced lease renewal (`renew_attempt_lease`), reviewing migration `0003_idempotency_fencing.sql`, upgrading GitHub Actions away from Node.js 20, and reconciling CI/local test reporting.
- **Date of Execution:** `2026-11-28`
- **Branch Name:** `setup/utda-foundation`
- **Verified Code Commit SHA:** `1576d19` (superseding `0f1a82c` with verified fence preservation, `renew_attempt_lease`, `BIGINT` + `CHECK (>= 0)` on `0003`, PG16 tests, and `@v7` workflows)
- **Prior Baseline Commits:** `0f1a82c`, `37f32de`
- **Files Inspected:**
  - `engine/database.py`
  - `engine/migrations/0001_initial_utda_schema.sql` (unchanged)
  - `engine/migrations/0002_roles_and_indexes.sql` (unchanged)
  - `engine/migrations/0003_idempotency_fencing.sql`
  - `engine/audit_store.py`
  - `engine/trigger.py`
  - `tests/test_phase0_contracts.py`
  - `tests/test_database_phase1.py`
  - `tests/test_phase1_event_pipeline.py`
  - `.github/workflows/ci-baseline.yml`, `.github/workflows/ci-database.yml`, `.github/workflows/documentation-trigger.yml`, `.github/workflows/reusable-docs.yml`
  - `docs/testing/2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md`
- **Files Modified / Created:**
  - `engine/migrations/0003_idempotency_fencing.sql` (updated `attempt_fence_version` to `BIGINT NOT NULL DEFAULT 0` with `chk_idempotency_fence_nonnegative CHECK (attempt_fence_version >= 0)`)
  - `engine/database.py` (preserved `attempt_fence_version` on completion; added optional `expected_fence_version` check on `finalize_processing_attempt`; added `renew_attempt_lease`)
  - `tests/test_database_phase1.py` (updated `_FakeCursor` & unit/PG16 tests for fence-version preservation, multiple recovery cycles `[1, 2, 3, 4]`, stale/concurrent `renew_attempt_lease`, `0003` `BIGINT`/CHECK/deferred-FK checks, and `0001+0002 -> 0003` upgrade with pre-existing rows)
  - `.github/workflows/ci-baseline.yml`, `.github/workflows/ci-database.yml`, `.github/workflows/documentation-trigger.yml`, `.github/workflows/reusable-docs.yml` (upgraded `actions/checkout@v7`, `actions/setup-python@v7`, `actions/upload-artifact@v7` for Node 24 compatibility)
  - `docs/testing/2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md` (reconciled inferred CI counts & added unauthenticated artifact access note)
  - `docs/testing/README.md` (added link to this report)
  - `docs/testing/2026-11-28-phase1-verification-reconciliation-1576d19.md` (this report)
- **Historical Migrations Unchanged:** Confirmed `engine/migrations/0001_initial_utda_schema.sql` and `engine/migrations/0002_roles_and_indexes.sql` had zero modifications (`git diff --name-only` returned empty output).

---

## 2. Exact Commands Executed

1. **Syntax & bytecode verification:**
   ```powershell
   .venv\Scripts\python.exe -m compileall -q engine tests
   ```
   Observed exit code: `0`.
2. **Local pytest verification (`UTDA_DRY_RUN=true`):**
   ```powershell
   $env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs
   ```
3. **GitHub Actions CI checks (unauthenticated REST API verification):**
   - `GET /repos/UNAI-TECH/unai-project-documentation-automation/actions/runs?branch=setup/utda-foundation`
   - `GET /repos/UNAI-TECH/unai-project-documentation-automation/actions/runs/46110694595/jobs`
   - `GET /repos/UNAI-TECH/unai-project-documentation-automation/actions/runs/46110694595/artifacts`
   - `GET /repos/UNAI-TECH/unai-project-documentation-automation/actions/artifacts/15196073867/zip` -> returned `HTTP 401 Unauthorized`.

---

## 3. Reconciled Local & CI Test Summaries

| Category | Scope / Execution Location | Passed | Failed | Skipped | Total | Evidence & Notes |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| **Local Unit Tests (No DB / Pure Logic & Schema)** | `tests/test_phase0_contracts.py` (7), `Phase1DatabaseConfigTests` (6), plus non-DB git/env unit tests in `tests/test_phase1_event_pipeline.py` (2: `test_dry_run_default_and_secret_scrubbing`, `test_github_event_parsing_from_webhook_payload_and_branch_ref`) | **15** | **0** | **0** | **15** | Directly executed on Windows (`pytest -v -rs`, `17.24s` total session). |
| **Simulated In-Memory DB Tests (`_FakeDBState` / `_FakePool`)** | `Phase1DatabaseLifecycleAndPersistenceTests` (7) + DB-backed simulated pipeline tests in `tests/test_phase1_event_pipeline.py` (7) | **14** | **0** | **0** | **14** | Directly executed locally (`pytest -v -rs`). Exercises SQL paths via `_FakeCursor`, not live PostgreSQL locking/constraints. |
| **Local Skipped Tests (Live PG16 CI + Live Supabase)** | 5 live PostgreSQL 16 tests (`UTDA_TEST_*` unset locally) + 1 hosted Supabase test (`test_live_supabase_readonly_health_check`) | **0** | **0** | **6** | **6** | Explicitly skipped locally; see reasons below. |
| **Total Local Session** | Combined local `pytest -v -rs` (`1576d19`) | **29** | **0** | **6** | **35** | Duration: `17.24s`. Exit code `0`. |
| **GitHub Actions Baseline Workflow** | `UTDA Phase 0 Baseline Validation` (`46110694641` on `1576d19`, `python -m unittest discover -s tests -v`) | *step `success`* | *step `success`* | *n/a* | *n/a* | Workflow conclusion: **`completed: success`**. Raw step log HTTP 403 unauthenticated; `@v7` checkout & setup-python succeeded. |
| **Real PostgreSQL 16 Integration Tests (CI)** | `UTDA Database Integration CI` (`46110694595` on `1576d19`, `postgres:16-alpine` service container) | *steps `success`* | *steps `success`* | *n/a* | *n/a* | Workflow conclusion: **`completed: success`** across Steps 1–12 (`runner --apply`, `pytest -v -rs`, `pg_dump`/`pg_restore`, artifact `utda-pg16-security-ci-46110694595`). **Limitation:** exact JUnit XML counts inside artifact `15249244099` (`3137` bytes) could not be downloaded unauthenticated (`HTTP 401`). |
| **Skipped Live Supabase Tests** | `test_live_supabase_readonly_health_check` (both locally and in CI) | **0** | **0** | **1** | **1** | Intentionally deferred: hosted Supabase connections and migrations prohibited in Phase 1. |

---

## 4. Fence-Version Assertions & Observed Behavior

Updated `finalize_processing_attempt` in `engine/database.py` so **completion preserves the active attempt's fence version** (removing the extra `+ 1` during completion) and verified the full contract in both simulated unit tests and PostgreSQL 16 CI tests (`test_atomic_attempt_completion_concurrency_and_ambiguous_commit` + `test_real_postgres_concurrent_workers_and_atomic_completion`):

- **Initial ownership acquisition:** `attempt_fence_version` transitions `0 -> 1` cleanly.
- **Expired-attempt recovery:** After lock expiry (`expires_at < NOW()`), recovering worker claims new `attempt_id` and transitions `attempt_fence_version` `1 -> 2`.
- **Stale-worker rejection & version immutability:** Stale Worker 1 calling `finalize_processing_attempt(attempt_id=att1_id)` or `renew_attempt_lease(attempt_id=att1_id, expected_fence_version=1)` raises `StaleAttemptError`; `idempotency_keys.attempt_fence_version` stays at `2` and `active_attempt_id` stays at `att2_id`.
- **Completion preserves expected version:** Completing `att2_id` clears `active_attempt_id = NULL` and preserves `attempt_fence_version == 2` (checked via DB query on `1576d19`).
- **Multiple recovery cycles:** Dedicated multi-cycle PG16 test (`ci-pg16-multi-fence-1`) drove Claim 1 -> Expire -> Claim 2 -> Finalize FAILED -> Claim 3 -> Expire -> Claim 4 -> Finalize COMPLETED, asserting exact monotonic progression `[1, 2, 3, 4]` and post-completion `attempt_fence_version == 4`.

---

## 5. Lease Renewal Status (`renew_attempt_lease`)

- **Finding & Resolution:** Initial code in `0f1a82c` lacked a lease renewal API. Because Phase 1 hardening also required ensuring stale workers cannot renew an attempt, `renew_attempt_lease(...)` was implemented directly in `UTDADatabaseRepository` (`engine/database.py`) without starting Phase 2:
  - Locks `utda.idempotency_keys` (`FOR UPDATE`), requires matching `active_attempt_id`, matching `expected_fence_version`, `status == 'IN_PROGRESS'`, non-expired `expires_at`, and active `IN_PROGRESS` row in `utda.processing_attempts`.
  - Atomically extends `expires_at = NOW() + (%s::integer * INTERVAL '1 second')` while preserving `attempt_fence_version`.
  - Verified in simulated unit tests and real PostgreSQL 16 concurrent race (`ThreadPoolExecutor(max_workers=4)` racing active renewals against stale renewals + wrong-version rejections + post-completion rejection).

---

## 6. Migration `0003_idempotency_fencing.sql` Verification

- **Upgrades Applied in `0003`:** Upgraded `attempt_fence_version` from `INTEGER` to `BIGINT NOT NULL DEFAULT 0` and added explicit non-negative constraint `chk_idempotency_fence_nonnegative CHECK (attempt_fence_version >= 0)`.
- **Schema & PG16 Assertions (`test_real_local_postgres_migrations_and_persistence`):**
  - Composite FK column types match (`idempotency_key VARCHAR(128)`, `active_attempt_id`/`attempt_id VARCHAR(64)`).
  - Unique composite index `uq_processing_attempts_key_attempt` covers `(idempotency_key, attempt_id)`.
  - `attempt_fence_version` confirmed as `bigint` in `information_schema.columns`; negative insertion rejected with `psycopg.errors.CheckViolation`.
  - `fk_idempotency_active_attempt` (`DEFERRABLE INITIALLY DEFERRED`) permits intra-transaction `claim_idempotency_and_attempt` ordering while rejecting dangling `active_attempt_id` values at `COMMIT` (`psycopg.errors.ForeignKeyViolation`).
  - Pre-existing row upgrade test (`utda_upgrade_0003_test`): seeded `0001` + `0002` with completed `idempotency_keys` and `processing_attempts` rows, applied `0003`, confirmed `active_attempt_id IS NULL` and `attempt_fence_version == 0`, and confirmed repeat-run idempotency (`applied == []`).

---

## 7. GitHub Actions Node.js 20 Deprecation Maintenance

- Updated all 4 workflow files (`.github/workflows/ci-baseline.yml`, `ci-database.yml`, `documentation-trigger.yml`, `reusable-docs.yml`) from `actions/checkout@v4`, `actions/setup-python@v5`, and `actions/upload-artifact@v4` to Node 24-compatible `@v7` major versions (`actions/checkout@v7`, `actions/setup-python@v7`, `actions/upload-artifact@v7`).
- Both CI workflows re-ran on commit `1576d19` and succeeded (`46110694641` and `46110694595`).

---

## 8. Skipped, Inaccessible Checks & Outstanding Risks

1. **Unauthenticated GitHub CI Artifact & Log Download Blocked:** Requests to `/actions/artifacts/*/zip` return `HTTP 401 Unauthorized` (and `/logs` returns `HTTP 403`), so per-test counts and full stdout/deprecation-warning banners inside `utda-pg16-security-ci-46110694595` (`id: 15249244099`) and `utda-pg16-security-ci-45997758365` (`id: 15196073867`) could not be inspected locally. **Action requested:** Download and inspect `pg16-junit.xml` / `pg16-test-output.txt` from Run `46110694595` with an authenticated GitHub account or supply the artifact ZIP if case-level XML archiving is required.
2. **Hosted Supabase Deferred:** `test_live_supabase_readonly_health_check`, live Supavisor TLS handshake, hosted migrations, and Supabase PostgREST exposed-schema checks (`F.7`) were not executed under Phase 1 restrictions.
3. **Static Lint / Type Tools & RLS:** No `ruff`/`mypy` config is present (`compileall` used locally); runtime DB grants remain table-level without Row-Level Security per `project_id`.

---

## 9. Final Assessment

**PASS WITH LIMITATIONS**

All 29 locally executed unit and simulated DB checks passed (`0 failed, 6 skipped`), all migration `0003`, fence-version, lease-renewal, and Node 24 CI workflow updates passed on GitHub Actions PostgreSQL 16 CI (`46110694595` & `46110694641` on `1576d19`), and `0001`/`0002` remain untouched. Limitations remain for unauthenticated CI artifact XML download (`HTTP 401`) and deferred hosted Supabase verification.

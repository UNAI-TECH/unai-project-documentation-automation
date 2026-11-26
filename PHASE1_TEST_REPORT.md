# Phase 1 Test Report — Core Engine & PostgreSQL 16 Verification

**Local Test Environment:** Python 3.14.4 (`psycopg 3.3.9`, `psycopg_pool 3.3.3`, `pytest 8.4.2`)  
**CI Environment:** GitHub Actions `ubuntu-latest` with `postgres:16-alpine` service container  
**Live Hosted Supabase (`unai-utda`):** **PENDING** (credentials not configured; no hosted migrations executed).

---

## 1. GitHub Actions PostgreSQL 16 CI Workflow Results

- **UTDA Phase 0 Baseline Validation:** Run [`45820044293`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45820044293) (commit `4b71ef3`) — **`completed: success`**
- **UTDA Database Integration CI:** Run [`45820044223`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45820044223) (commit `4b71ef3`) — **`completed: success`**

| CI Step (`45820044223`) | Result | Verified Against PostgreSQL 16 |
| --- | --- | --- |
| Initialize `postgres:16-alpine` & install deps | **success** | Real PG 16 service ready |
| Create `utda_migrator`/`utda_runtime`, `--plan`, `--apply`, `db_health` | **success** | `0001` and `0002` applied cleanly |
| Verify least-privilege role enforcement | **success** | `utda_runtime` inserted into `utda.audit_events` and was blocked from `DELETE`, `UPDATE` on `audit_events`, and `CREATE TABLE` in `utda` |
| Run full Phase 0 and Phase 1 `pytest` suite (`UTDA_TEST_DATABASE_URL` active) | **success** | Executed real PG 16 advisory lock contention, atomic DDL rollback (`0003_broken.sql`), idempotency lock/recovery, and full PR-first correlation lifecycle |
| Verify `pg_dump` backup and `pg_restore` recovery (`postgres:16-alpine`) | **success** | Custom-format dump of `utda` restored into `utda_restore_verify` (`--exit-on-error`) and verified with `engine.db_health --json` |
| Verify read-only Supabase probe defaults cleanly to `pending` when unset | **success** | Returned `{"status": "pending", "verified": false}` without error |

---

## 2. Local Automated Test Results

- **Command:** `.venv\Scripts\python.exe -m pytest -v -rs`
- **Result:** **`20 passed, 3 skipped in 5.54s`** (local skips are exclusively the 2 `UTDA_TEST_DATABASE_URL` PG16 tests that ran in CI `45820044223` and the 1 live Supabase `UTDA_DATABASE_URL` probe marked `PENDING`).

### Verified Coverage Areas
1. **Event Normalization & Pipeline (`tests/test_phase1_event_pipeline.py`):**
   - `UTDA_DRY_RUN` fail-safe default (`true` unless explicit `'false'`) and secret scrubbing (`ghp_...`, `sk-...`, DSN passwords).
   - Unmerged closed PR ignored (`IGNORED_UNMERGED_PR`).
   - **PR-first correlation:** Merged PR without JSON report -> `WAITING_FOR_FINAL_REPORT` -> subsequent report push -> `DRY_RUN_READY`.
   - **Report-first correlation:** Report push without PR merge -> `AWAIT_PR_MERGE` -> subsequent PR merge -> `DRY_RUN_READY`.
   - **Duplicate events:** Duplicate `delivery_id` -> `SKIPPED_DUPLICATE_RECEIPT`; new `delivery_id` with identical report blob SHA -> `SKIPPED_DUPLICATE`.
   - **Validation guardrails:** Unsubstantiated `tests[].result == "passed"` rejected (`VALIDATION_FAILED`).
   - **Retry & recovery:** `FAILED` idempotency key atomically re-claimed on recovery with `attempt_number` incremented (`1 -> 2`).
2. **Database Lifecycle, Migrations & Security (`tests/test_database_phase1.py`):**
   - Missing/invalid DSNs, Supabase HTTP/JWT rejection, `sslmode` enforcement, pooler mode handling, credential-free logging, transient error recovery, transaction commit/rollback, concurrent 6-thread idempotency lock contention, migration checksum tamper detection, atomic SQL rollback, advisory lock contention, and `db_health` 500 MB Free Plan checks.
3. **Phase 0 Contracts (`tests/test_phase0_contracts.py`):** All 7 schema, registry, matrix, and workflow contract tests passed.

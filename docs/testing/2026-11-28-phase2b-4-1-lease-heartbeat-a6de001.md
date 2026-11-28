# UTDA Phase 2B.4.1 — Worker Lease Heartbeat & Renewal Verification Report

## 1. Task Information & Commit Provenance

- **Task Name & Objective:** UTDA Phase 2B.4.1 — Document lease lifecycle and transaction boundaries, harden transaction-safe lease renewal (`renew_attempt_lease` reconciliation), implement configurable `LeaseConfig` + `LeaseHeartbeat` background lease keeping with fail-closed abort/self-fencing, wire heartbeat & `expected_fence_version` propagation into `AuditStore` + `trigger.py`, and verify all 9 required scenarios in unit and PostgreSQL 16 CI tests.
- **Date of Execution:** `2026-11-28`
- **Branch:** `setup/utda-foundation`
- **Baseline Commit:** `6712928` (`67129284e92f83f51436ed304dd0e40fdf010592`, report commit `3d1aff6`)
- **Verified Phase 2B.4.1 Implementation & Test Commit SHA:** `a6de001` (`a6de001e4ed8dd21b1a919ef9053312a9d62e82c`)
  - `0db47ba` — Added [PHASE2B4_1_LEASE_HEARTBEAT.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/architecture/PHASE2B4_1_LEASE_HEARTBEAT.md), [lease_heartbeat.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/lease_heartbeat.py), `CommitOutcomeUnknownError` reconciliation & optional `expected_fence_version` in [database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py), `ProcessingLease` / `renew_processing_lease` / `lease_heartbeat` / `expected_fence_version` in [audit_store.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py), coordinated heartbeat loop + non-overwrite abort path in [trigger.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py), and unit + PG16 integration suites in [test_database_phase1.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py) and [test_phase1_event_pipeline.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_phase1_event_pipeline.py).
  - `a6de001` — Configured `min_size=0` on the unreachable-DB `ConnectionPool` in `test_real_postgres_worker_lease_heartbeat_and_renewal` so DB connection failure is exercised inside `LeaseHeartbeat` renewal rather than pool startup, with slight timing cushions for CI runner jitter.
- **Safety & Scope Constraints Preserved:** Migrations `0001`–`0005` untouched; `UTDA_DRY_RUN=true` enforced; zero hosted Supabase connections; zero DOCX generation, Google Drive sync, OpenRouter integration, or production GitHub writes; halted before Phase 2B.4.2.

---

## 2. Files Changed & Transaction/Lease Lifecycle Summary

1. **[PHASE2B4_1_LEASE_HEARTBEAT.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/architecture/PHASE2B4_1_LEASE_HEARTBEAT.md):** Documents existing lease tables (`utda.idempotency_keys`, `utda.processing_attempts`), claim/renewal/finalization state machine, and all 8 transaction boundaries (`TB-1`–`TB-8`).
2. **[engine/lease_heartbeat.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/lease_heartbeat.py):** Implements:
   - `LeaseConfigurationError(ValueError)` and `LeaseConfig` (`lock_ttl_seconds`, `heartbeat_interval_seconds`, `self_fence_margin_seconds`, `from_env()`): enforces integer `lock_ttl_seconds >= 1`, finite `0 < heartbeat_interval_seconds < lock_ttl_seconds / 2`, and `heartbeat_interval_seconds < lock_ttl_seconds - self_fence_margin_seconds`.
   - `LeaseLostError(RuntimeError)` and `LeaseHeartbeat`: runs periodic renewals on a dedicated background thread (each tick opening its own short-lived pooled transaction via `renew_fn`, never sharing the worker connection), wakes promptly on `stop_event`, sets `abort_event` immediately on definitive loss (`StaleAttemptError`, `IdempotencyIdentityMismatchError`, `DatabaseConfigError`, `ValueError`, or unexpected exceptions) or transient DB unavailability crossing `self_fence_deadline_seconds`, and provides `raise_if_lost()` / `wait_or_raise()` for fail-closed cooperative abortion.
3. **[engine/database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py):** Hardens `UTDADatabaseRepository.renew_attempt_lease()`:
   - Validates `project_id`, non-empty `idempotency_key` & `attempt_id`, positive integer `extend_ttl_seconds`, and optional non-negative `expected_fence_version`.
   - Locks `utda.idempotency_keys` (`FOR UPDATE` under RLS `utda.current_project_id`) and `utda.processing_attempts` (`FOR UPDATE`), verifying project ownership, `IN_PROGRESS` state on both tables, `active_attempt_id == attempt_id`, matching `attempt_fence_version` (when supplied or locked), and non-expired `expires_at`.
   - Adds `CommitOutcomeUnknownError` reconciliation via `run_project_readonly()` so socket drops after server `COMMIT` verify persisted lease state and expiry instead of failing blindly.
4. **[engine/audit_store.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py):** Adds `ProcessingLease`, `claim_processing_lease()`, `renew_processing_lease()`, `lease_heartbeat()`, and `expected_fence_version` forwarding in `record_attempt_outcome()`, while preserving `claim_processing_lock()` compatibility.
5. **[engine/trigger.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py):** Updates `process_event()` to load `LeaseConfig`, claim via `claim_processing_lease()`, run work inside `with audit.lease_heartbeat(...)`, check `heartbeat.raise_if_lost()`, pass `expected_fence_version` on finalization, and skip `FAILED` overwrite if lease ownership was lost so a new owner's active lease is never corrupted.

---

## 3. Unit Test Results (`a6de001`)

- `.venv\Scripts\python.exe -m compileall -q engine tests` $\to$ Exit **`0`**.
- `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs` $\to$ **`33 passed, 0 failed, 9 skipped in 26.34s`** (42 collected):
  - **`Phase1LivePostgreSQLTests.test_lease_config_validation_and_heartbeat_unit`** (`tests/test_database_phase1.py`): Passed (covers `LeaseConfig` bounds/env validation, successful renewal with and without `expected_fence_version`, non-owner rejection, cross-project rejection, concurrent renewal race, periodic renewal during active work, prompt termination on completion and failure, expired-lease rejection + heartbeat abort, `CommitOutcomeUnknownError` reconciliation, and sustained DB outage proactive self-fencing).
  - **`Phase1EventPipelineTests.test_process_event_heartbeat_renewal_completion_failure_and_lease_loss`** (`tests/test_phase1_event_pipeline.py`): Passed (covers `process_event` multi-tick renewal & clean shutdown, worker exception stopping heartbeat cleanly, and mid-processing lease theft triggering `LeaseLostError` without corrupting the winner's state).
  - Remaining 31 existing unit/contract/pipeline tests passed cleanly without regression.
  - **Skipped locally (9):** 8 CI PostgreSQL 16 integration tests (`UTDA_TEST_*` unset locally; executed in CI Run `46122874962` below) + 1 intentionally deferred `test_live_supabase_readonly_health_check`.

---

## 4. PostgreSQL 16 CI Integration Test Results (`a6de001`)

| Workflow | Run ID | URL | Environment | Job & Result | Steps | Artifacts |
| --- | --- | --- | --- | --- | --- | --- |
| `UTDA Database Integration CI` (`.github/workflows/ci-database.yml`) | **`46122874962`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46122874962 | `postgres:16-alpine` service container (`ubuntu-latest`, Python 3.12, `psql`) | `database-tests`: **`completed: success`** | Active Steps 1–12 all **`success`** (`22` post-setup `skipped` cleanup) | `utda-pg16-security-ci-46122874962` (`id: 15254347725`, `3481` bytes) |
| `UTDA Phase 0 Baseline Validation` (`.github/workflows/ci-baseline.yml`) | **`46122874998`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46122874998 | `ubuntu-latest` | `validate-contracts`: **`completed: success`** | All steps **`success`** | — |

PostgreSQL 16 CI Step 9 executed and passed all 8 PG16 integration tests, including:
- `test_real_postgres_worker_lease_heartbeat_and_renewal`: Verifies real PG16 `LeaseHeartbeat` renewals extending `expires_at` (`fence_version` preserved), optional vs explicit `expected_fence_version`, non-owner `StaleAttemptError`, cross-project `HB_PROJ_B` rejection under `0005` RLS, 6-thread concurrent renewal race, prompt thread shutdown on worker exception, `_DropOnceRenewConnProxy` ambiguous-commit reconciliation, post-expiry rejection + attempt #2 recovery, and unreachable-DB proactive self-fencing.
- `test_real_postgres_process_event_heartbeat_lifecycle`: Verifies real PG16 `process_event()` across normal completion with >=2 renewals (`COMPLETED`, `fence_version=1`), worker exception shutdown, and mid-run lease expiry/takeover raising `LeaseLostError` without overwriting winner attempt #3 (`IN_PROGRESS`, `fence_version=3`).

*(Note: Unauthenticated GitHub REST API blocks downloading `utda-pg16-security-ci-46122874962` ZIP/raw logs via `HTTP 401`/`403`; CI job/step `completed: success` confirms `pytest` exited `0`.)*

---

## 5. Migration Immutability (`0001`–`0005`) & External Isolation Confirmation

Verified via `discover_migrations(Path('engine/migrations'))` and `git diff --stat 6712928 -- engine/migrations/` (`0` changes):

| Migration File | Status | SHA-256 Checksum | Changed? |
| --- | --- | --- | --- |
| `0001_initial_utda_schema.sql` | Baseline | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | **No** |
| `0002_roles_and_indexes.sql` | Baseline | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | **No** |
| `0003_idempotency_fencing.sql` | Baseline | `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b` | **No** |
| `0004_idempotency_fence_version_hardening.sql` | Baseline | `7060f50e1820b12194c21cac19c0505454d2a7527c941aac4917e2d2032567ad` | **No** |
| `0005_project_row_level_security.sql` | Baseline | `730a085605fc76475dcfd29187a81615b2e3f39d9830886aa4d35d1b38eccae9` | **No** |

- Confirmed zero hosted Supabase connections (`test_live_supabase_readonly_health_check` skipped; Step 11 probe returned `pending`).
- Confirmed `UTDA_DRY_RUN=true` throughout and zero DOCX, Drive, OpenRouter, or production GitHub writes.

---

## 6. Known Limitations & Unresolved Risks

1. **Cooperative Worker Abortion:** `LeaseHeartbeat` sets `abort_event` and wakes `wait_or_raise()`/`raise_if_lost()` immediately, and Phase 2 external stages must check `heartbeat.raise_if_lost()` and pass cancellation/timeouts to HTTP/subprocess calls because Python cannot safely kill an arbitrary thread mid-syscall (DB finality remains protected by `active_attempt_id` + `attempt_fence_version`).
2. **ThreadPool Timeout Sizing When `heartbeat_interval_seconds` Is Very Small Under Heavy Pool Contention:** Because renewals borrow a short-lived pooled connection from `DatabaseManager`, `UTDA_DATABASE_POOL_SIZE` (default `4`) must stay `>= 2` so concurrent worker + heartbeat queries never contend.
3. **`next_retry_at` Backoff Scheduling Not Yet Enforced (Deferred):** `retry_state='RETRY_SCHEDULED'` does not yet enforce `next_retry_at` cool-down windows or `SIGTERM` signal hooks (`PHASE2B.4.2+` scope).
4. **Unauthenticated CI Log Access:** Per-case JUnit XML inside `utda-pg16-security-ci-46122874962` requires authenticated GitHub download.

---

## 7. Assessment

**PASS WITH LIMITATIONS** — Phase 2B.4.1 lease heartbeat, configurable interval/TTL validation, transaction-safe `renew_attempt_lease` reconciliation, and fail-closed worker abort semantics are implemented and verified across all 33 local tests and real PostgreSQL 16 CI (`Run 46122874962` `completed: success`), with migrations `0001`–`0005` preserved unchanged. Ready for review before Phase 2B.4.2.

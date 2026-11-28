# UTDA Phase 2B.1–2B.2 — Project Identity and Database Context Hardening Report

## 1. Task Information

- **Task Name & Objective:** Phase 2B.1–2B.2 — Implement authoritative repository-bound project resolution (Workstream A) and transaction-local database project context (`utda.current_project_id`) with strict project-ID enforcement across all project-scoped repository methods (Workstream B).
- **Date of Execution:** `2026-11-28`
- **Branch / Base Commit:** `setup/utda-foundation` (base `b6bc9a3`, local Phase 2A + Phase 2B.1–2B.2 workspace changes)
- **Safety & Scope Confirmation:**
  - `UTDA_DRY_RUN=true` enforced.
  - Historical migrations `0001`–`0004` preserved untouched (`discover_migrations()` SHA-256 digests unchanged).
  - Migration `0005` / PostgreSQL RLS policies, `LeaseHeartbeat`, DOCX generation, Google Drive sync, OpenRouter calls, project provisioning, PR orchestration, and hosted Supabase connections were **not** executed or implemented.

---

## 2. Files Changed

| File | Workstream | Summary of Changes |
| --- | --- | --- |
| [engine/db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py) | A & B | Added `PROJECT_ID_RE` (`^[A-Z0-9][A-Z0-9_-]{2,31}$`) and `validate_project_id()` (strict format check without `.strip()` or uppercase normalization; raises `DatabaseConfigError`). |
| [engine/config_loader.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/config_loader.py) | A | Added `ProjectIdentityError(ConfigError)` with sanitized error codes (`ERR_MISSING_REPOSITORY`, `ERR_INVALID_REPOSITORY`, `ERR_PROJECT_NOT_REGISTERED`, `ERR_AMBIGUOUS_REPOSITORY`, `ERR_PROJECT_ARCHIVED`, `ERR_INVALID_REGISTRY_PROJECT_ID`, `ERR_INVALID_PROJECT_ID`, `ERR_PROJECT_ID_MISMATCH`, `ERR_INVALID_LOCAL_PROJECT_ID`, `ERR_LOCAL_CONFIG_PROJECT_MISMATCH`). Updated `resolve_project()` to resolve exclusively by exact trusted `owner/repo` match, reject unregistered/archived entries, and reject any mismatched/unnormalized `UTDA_PROJECT_ID` or `documentation/project-config.json` ID. |
| [engine/trigger.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py) | A & B | Preserved raw whitespace/case on `GITHUB_REPOSITORY` and non-empty `UTDA_PROJECT_ID` in `normalize_event()`, updated `process_event()` to pass `local_project_id` separately to `resolve_project()` before any DB write, and forwarded `project_id` to `audit.record_attempt_outcome()`. |
| [engine/database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py) | B | Added `set_transaction_project_context()` (`SELECT set_config('utda.current_project_id', %s, true)`), added `project_id` scoping to `DatabaseManager.transaction()`, `run_in_transaction()`, and `run_readonly()`, added mandatory-project helpers (`project_transaction()`, `run_in_project_transaction()`, `run_project_readonly()`), and required non-empty validated `project_id` before SQL on every project-scoped `UTDADatabaseRepository` method (`upsert_project`, `record_event_receipt`, `correlate_feature_state`, `get_feature_state`, `claim_idempotency_and_attempt`, `finalize_processing_attempt`, `renew_attempt_lease`, `record_final_report`, `append_audit_event`, and deprecated helpers) with explicit cross-project row checks in `finalize_processing_attempt` / `renew_attempt_lease` / `record_processing_attempt`. Documented security boundary that `utda.current_project_id` is a scoping context, not an authenticated tenant identity. |
| [engine/audit_store.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py) | B | Made `project_id: str` required on `record_receipt()`, `record_attempt_outcome()`, and `append_audit()`. |
| [tests/test_phase1_event_pipeline.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_phase1_event_pipeline.py) | A & C | Added `test_authoritative_project_resolution_and_zero_writes_on_failure` covering valid resolution, GitHub repo case handling, whitespace/lowercase rejection without normalization, foreign `UTDA_PROJECT_ID`, mismatched local `project-config.json`, unregistered/archived repos, mismatched report `project_id`, and zero DB writes on failed resolution. |
| [tests/test_database_phase1.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py) | B & C | Updated `_FakeCursor` / `_FakeConnection` / `_FakePool` to track transaction-local `set_config` & reset on commit/rollback + connection reuse; added `test_transaction_local_project_context_and_strict_repo_scoping` + PG16 live assertions inside `test_real_postgres_concurrent_workers_and_atomic_completion`. |
| [docs/testing/README.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/testing/README.md) | Reporting | Added index entry for this implementation report. |

---

## 3. Migration Integrity Confirmation (`0001`–`0004`)

- `git diff --stat engine/migrations`: empty (zero modifications).
- `discover_migrations(Path('engine/migrations'))` SHA-256 digests:
  - `0001`: `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78`
  - `0002`: `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f`
  - `0003`: `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b`
  - `0004`: `7060f50e1820b12194c21cac19c0505454d2a7527c941aac4917e2d2032567ad`

---

## 4. Verification Results (`UTDA_DRY_RUN=true`)

| Check | Exact Command | Outcome | Notes |
| --- | --- | --- | --- |
| Syntax / bytecode compile | `.venv\Scripts\python.exe -m compileall -q engine tests` | **Exit `0`** | Ran cleanly across `engine/` and `tests/`. |
| Full local unit, contract & simulated transaction suite | `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs` | **`31 passed, 0 failed, 6 skipped`** (`37 collected`) | Includes all Phase 0, Phase 1, Workstream A (`test_authoritative_project_resolution_and_zero_writes_on_failure`), and Workstream B (`test_transaction_local_project_context_and_strict_repo_scoping`) tests. Note: `_FakePool`/`_FakeConnection` tests simulated transactions and do **not** claim real PostgreSQL 16 execution. |
| Local PostgreSQL 16 CLI/Docker check | `Get-Command docker, psql -ErrorAction SilentlyContinue` | **Not available locally** | Neither `docker` nor `psql` is installed on this Windows host; live PG16 execution runs via `.github/workflows/ci-database.yml`. |

### Skipped Tests (6 of 37)
1. [test_database_phase1.py#L2960](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L2960): `test_live_supabase_readonly_health_check` — *Live hosted Supabase check deferred (`UTDA_DATABASE_URL` / `UTDA_RUN_LIVE_SUPABASE_TESTS=true` unset).*
2. [test_database_phase1.py#L1396](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L1396): `test_real_local_postgres_migrations_and_persistence` — *CI PG16 only (`UTDA_TEST_DATABASE_URL` / `UTDA_TEST_MIGRATOR_URL` unset locally).*
3. [test_database_phase1.py#L1946](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L1946): `test_real_postgres_least_privilege_and_default_privileges` — *CI PG16 only.*
4. [test_database_phase1.py#L2261](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L2261): `test_real_postgres_bootstrap_ownership_transfer` — *CI PG16 only (`UTDA_TEST_BOOTSTRAP_URL` unset).*
5. [test_database_phase1.py#L2298](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L2298): `test_real_postgres_concurrent_workers_and_atomic_completion` — *CI PG16 only (contains new real PG16 `set_config` commit/rollback/connection-reuse assertions).*
6. [test_phase1_event_pipeline.py#L402](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_phase1_event_pipeline.py#L402): `test_real_postgres_full_event_correlation_and_recovery` — *CI PG16 only.*

---

## 5. Remaining Risks & Limitations

1. **Security Boundary of Transaction-Local GUC:** Setting `utda.current_project_id` via `SELECT set_config('utda.current_project_id', %s, true)` provides transaction-scoped project context and prevents accidental unscoped queries inside application code, **not** cryptographic tenant authentication. It does not independently block arbitrary raw SQL run under the shared `utda_runtime` credential until Phase 2B.3 PostgreSQL RLS (`0005`) is approved and applied.
2. **Real PostgreSQL 16 Verification Pending CI Run:** While simulated transaction tests verified ordering, commit/rollback reset, and pooler reuse locally, the live PostgreSQL 16 `set_config` / connection-reuse assertions in `test_real_postgres_concurrent_workers_and_atomic_completion` await execution in `.github/workflows/ci-database.yml`.
3. **Database Schema `0005` & Hosted Supabase Still Pending:** RLS enforcement (`0005`), `LeaseHeartbeat`, and live `unai-utda` verification remain out of scope for Phase 2B.1–2B.2.

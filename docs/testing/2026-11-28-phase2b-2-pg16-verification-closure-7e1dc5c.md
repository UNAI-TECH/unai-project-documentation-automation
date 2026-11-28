# UTDA Phase 2B.2 — PostgreSQL 16 Verification & Project-Scoping Closure Report

## 1. Task Information & Commit Provenance

- **Task Name & Objective:** UTDA Phase 2B.2 — Real PostgreSQL 16 verification of transaction-local project context (`utda.current_project_id`), complete source-level inventory of `UTDADatabaseRepository` and `AuditStore` methods, trusted `GITHUB_REPOSITORY` identity review, and migration `0001`–`0004` integrity verification.
- **Date of Execution:** `2026-11-28`
- **Branch:** `setup/utda-foundation`
- **Baseline Code Commit (Phase 1 Closure):** `c0deb74` (Report commit `b6bc9a3`)
- **Verified Phase 2B.1–2B.2 Implementation & Test Commit SHA:** `7e1dc5c` (`7e1dc5ca99702e24f88a48320fa3248b1608ae32`)
- **Safety Restrictions Preserved:** `UTDA_DRY_RUN=true`; zero hosted Supabase connections; zero `0005` migration or RLS policies implemented; zero modifications to `0001`–`0004`; zero `LeaseHeartbeat`, DOCX generation, Google Drive writes, OpenRouter calls, repo provisioning, or production GitHub PR writes.

---

## 2. PostgreSQL 16 Real CI Verification Results (`7e1dc5c`)

### 2.1 GitHub Actions Runs Executed Against Commit `7e1dc5c`

| Workflow | Run ID | URL | Container / Environment | Job & Result | Artifacts |
| --- | --- | --- | --- | --- | --- |
| `UTDA Database Integration CI` (`.github/workflows/ci-database.yml`) | **`46115615778`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46115615778 | `postgres:16-alpine` service container (`ubuntu-latest`, Python 3.12, `psql` client) | `database-tests`: **`completed: success`** (Steps 1–12 all `success`) | `utda-pg16-security-ci-46115615778` (`id: 15251780604`, `3262` bytes) |
| `UTDA Phase 0 Baseline Validation` (`.github/workflows/ci-baseline.yml`) | **`46115615782`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46115615782 | `ubuntu-latest` | `validate-contracts`: **`completed: success`** | — |

### 2.2 PostgreSQL 16 Behavior Verified in CI Step 9 (`tests/test_database_phase1.py` & `tests/test_phase1_event_pipeline.py`)

All 5 real PostgreSQL 16 integration tests execute in CI Step 9 (`Run full Phase 0 and Phase 1 PostgreSQL 16 security checklist suite`):
- `test_real_local_postgres_migrations_and_persistence`
- `test_real_postgres_least_privilege_and_default_privileges`
- `test_real_postgres_bootstrap_ownership_transfer`
- `test_real_postgres_concurrent_workers_and_atomic_completion` *(extended for Phase 2B.2 items 1–8)*
- `test_real_postgres_full_event_correlation_and_recovery`

Specifically verified against real `postgres:16-alpine` in `test_real_postgres_concurrent_workers_and_atomic_completion` (`UTDA_DATABASE_POOL_SIZE=1`, runtime role `utda_runtime`):

| Verification Requirement | Real PG16 Assertion (`tx_reuse_cfg`, `pool_size=1`) | Status in CI Run `46115615778` |
| --- | --- | --- |
| **1. `set_config('utda.current_project_id', ..., true)` inside tx** | `single_db.run_in_project_transaction("CI_CONC_TEST", ...)` queries `pg_backend_pid(), current_setting('utda.current_project_id', true)` $\to$ returns `'CI_CONC_TEST'`. | **PASS** (Step 9 `success`) |
| **2. Context reset after successful commit** | Subsequent unscoped `run_in_transaction` on the same `pool_size=1` connection returns `NULL` for `NULLIF(current_setting('utda.current_project_id', true), '')`. | **PASS** (Step 9 `success`) |
| **3. Context reset after rollback** | `run_in_project_transaction("CI_SEC_TEST", _abort_with_ctx)` confirms `'CI_SEC_TEST'` inside aborted tx then raises `RuntimeError`; subsequent `run_readonly` sees `NULL`. | **PASS** (Step 9 `success`) |
| **4. Connection reuse with pool size 1** | `pg_backend_pid()` across Project A tx, global tx 1, Project B tx, global tx 2, and retry tx all equal `pid_a` (exact same physical PostgreSQL backend process). | **PASS** (Step 9 `success`) |
| **5. Project A followed by unscoped tx** | `"CI_CONC_TEST"` commit $\to$ unscoped `run_in_transaction` observes `None`. | **PASS** (Step 9 `success`) |
| **6. Project A followed by Project B** | `"CI_CONC_TEST"` commit $\to$ `"CI_SEC_TEST"` commit sees `'CI_SEC_TEST'` on same `pid_a`. | **PASS** (Step 9 `success`) |
| **7. Transaction retry behavior** | Injects pre-commit `psycopg.OperationalError` on attempt 1 inside `run_in_project_transaction("CI_CONC_TEST", _retry_once_op)` after asserting GUC `'CI_CONC_TEST'`; attempt 2 retries cleanly, re-applies `'CI_CONC_TEST'`, succeeds on `pid_a`, and clears to `None` afterward. | **PASS** (Step 9 `success`) |
| **8. Validation before project-scoped SQL** | Calls `single_repo.get_feature_state(project_id=bad_pid, ...)` and `single_db.run_in_project_transaction(bad_pid, ...)` with `("", None, "   ", "lowercase", " CI_CONC_TEST ")`; all raise `DatabaseConfigError` cleanly. | **PASS** (Step 9 `success`) |

*(Note: Unauthenticated GitHub REST API blocks downloading `utda-pg16-security-ci-46115615778` ZIP logs/JUnit XML (`HTTP 401`/`403`); job status `completed: success` across Steps 1–12 proves `pytest` exited `0` against `postgres:16-alpine`.)*

### 2.3 Local Windows Unit & Simulated Verification (`UTDA_DRY_RUN=true`)

- `.venv\Scripts\python.exe -m compileall -q engine tests` $\to$ Exit **`0`**.
- `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs` (on `7e1dc5c`) $\to$ **`31 passed, 0 failed, 6 skipped in 22.52s`** (37 collected).
  - **Passed (31):** 6 DB config tests (`Phase1DatabaseConfigTests`), 7 mocked/simulated DB lifecycle/scoping/fencing tests (`test_transaction_local_project_context_and_strict_repo_scoping` + 6 prior persistence tests — simulated via `_FakePool`/`_FakeConnection`, *not* counted as live PG16), 7 Phase 0 schema/workflow contract tests (`Phase0ContractTests`), and 11 pipeline/identity/spoofing/dry-run tests (`Phase1EventPipelineTests`).
  - **Skipped locally (6):** The 5 real PG16 CI tests above (run in CI `46115615778`) + 1 intentionally deferred `test_live_supabase_readonly_health_check`.

---

## 3. Complete Repository & Manager Method Inventory

Source-level AST & manual inspection across [engine/database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py) and [engine/audit_store.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py) confirms **every project-scoped repository method requires a validated canonical `project_id: str` before SQL execution and enters PostgreSQL exclusively via `run_in_project_transaction()` / `run_project_readonly()`**. Zero unscoped fallback paths remain in `UTDADatabaseRepository` or `AuditStore`.

### 3.1 `UTDADatabaseRepository` (`engine/database.py`)

| Method Name | Database Tables Accessed | Scope | Required `project_id` | Transaction Context Requirement | DB Role Required (`0002` Grants) | Callable by `utda_runtime`? |
| --- | --- | --- | --- | --- | --- | --- |
| `upsert_project` | `utda.projects` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT, UPDATE`) | **Yes** |
| `record_event_receipt` | `utda.github_event_receipts` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT`) | **Yes** |
| `correlate_feature_state` | `utda.feature_processing_states` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT, UPDATE`) | **Yes** |
| `get_feature_state` | `utda.feature_processing_states` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` (`utda.current_project_id`) | `utda_runtime` (`SELECT`) | **Yes** |
| `claim_idempotency_and_attempt` | `utda.idempotency_keys`, `utda.processing_attempts` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT, UPDATE` on both) | **Yes** |
| `finalize_processing_attempt` | `utda.idempotency_keys`, `utda.processing_attempts` | Project-scoped | **Yes** (`validate_project_id` + row `project_id == pid` check) | `run_in_project_transaction` + `run_project_readonly` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT, UPDATE` on both) | **Yes** |
| `renew_attempt_lease` | `utda.idempotency_keys`, `utda.processing_attempts` | Project-scoped | **Yes** (`validate_project_id` + row `project_id == pid` check) | `run_in_project_transaction` (`utda.current_project_id`) | `utda_runtime` (`SELECT, UPDATE`) | **Yes** |
| `acquire_idempotency_key` *(deprecated)* | Delegates to `claim_idempotency_and_attempt` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` | `utda_runtime` | **Yes** *(deprecated)* |
| `next_attempt_number` *(deprecated)* | `utda.processing_attempts` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` (`utda.current_project_id`) | `utda_runtime` (`SELECT`) | **Yes** *(deprecated)* |
| `complete_idempotency_key` *(deprecated)* | Delegates to `finalize_processing_attempt` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` | `utda_runtime` | **Yes** *(deprecated)* |
| `record_processing_attempt` *(deprecated)* | `utda.idempotency_keys`, `utda.processing_attempts` | Project-scoped | **Yes** (`validate_project_id` + row `project_id == pid` check) | `run_in_project_transaction` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT`) | **Yes** *(deprecated)* |
| `record_final_report` | `utda.final_reports` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT`) | **Yes** |
| `append_audit_event` | `utda.audit_events` | Project-scoped | **Yes** (`validate_project_id` before SQL) | `run_in_project_transaction` + `run_project_readonly` (`utda.current_project_id`) | `utda_runtime` (`SELECT, INSERT`) or `utda_governance` | **Yes** |

### 3.2 `AuditStore` (`engine/audit_store.py`) & `DatabaseManager` Global Separation

| Class / Method | Tables / Resources Accessed | Scope | Required `project_id` | Transaction Context Requirement | DB Role Required | Callable by `utda_runtime`? |
| --- | --- | --- | --- | --- | --- | --- |
| `AuditStore.sync_project` | Delegates to `upsert_project` (`utda.projects`) | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.record_receipt` | Delegates to `record_event_receipt` (`utda.github_event_receipts`) | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.correlate_feature` | Delegates to `correlate_feature_state` (`utda.feature_processing_states`) | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.record_report` | Delegates to `record_final_report` (`utda.final_reports`) | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.claim_processing_lock` | Delegates to `claim_idempotency_and_attempt` | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.record_attempt_outcome` | Delegates to `finalize_processing_attempt` | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.append_audit` | Delegates to `append_audit_event` (`utda.audit_events`) | Project-scoped | **Yes** (`validate_project_id` in `AuditStore` + repo) | Project-scoped tx | `utda_runtime` | **Yes** |
| `AuditStore.export_artifact` | Local filesystem (`artifacts/*.json`, no DB) | Local CI artifact | No DB access | None (sanitized JSON file write only) | N/A (filesystem) | Yes (process) |
| `DatabaseManager.transaction` / `run_in_transaction` / `run_readonly` | Low-level pool helpers (used by `db_health.py`, migrations/admin diagnostics) | Explicitly Global when `project_id=None` | Optional (strictly validated if provided; `None` = unscoped global) | None unless `project_id` supplied | Connected role (`utda_runtime` for `db_health`) | Health probes only (never used unscoped by Repo) |
| `DatabaseManager.project_transaction` / `run_in_project_transaction` / `run_project_readonly` | Project-scoped tx helpers | Project-scoped | **Yes** (`validate_project_id` before connection checkout) | Sets `set_config('utda.current_project_id', %s, true)` | Runtime/Governance | **Yes** |

### 3.3 Future / Unimplemented Operations Inventory *(Not Implemented — Not Verified)*

The following schema tables from [0001_initial_utda_schema.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql) do **not** yet have Python repository methods implemented in Phase 2B.2:

| Table | Implementation Status | Expected Scope & Future Phase | Current `0002` `utda_runtime` Grants | Expected Required Role & Grant Change |
| --- | --- | --- | --- | --- |
| `utda.document_proposals` | **FUTURE OPERATION (Not implemented)** | Project-scoped (Phase 2 PR orchestration) | **`SELECT` only** | Requires forward-only `0005` `INSERT, UPDATE` grant to `utda_runtime` + RLS `project_id` policy |
| `utda.approval_records` | **FUTURE OPERATION (Not implemented)** | Project-scoped (or `COMPANY_MASTER` project mapping; Phase 2 governance) | **`SELECT` only (prohibited write)** | Writes strictly via **`utda_governance`** (`SELECT, INSERT` only) — **never** `utda_runtime` |
| `utda.rollback_operations` | **FUTURE OPERATION (Not implemented)** | Project-scoped / Drive (`project_id` nullable; Phase 6 governance) | **`SELECT` only (prohibited write)** | Writes strictly via **`utda_governance`** (`SELECT, INSERT, UPDATE`) — **never** `utda_runtime` |
| `utda.project_master_versions` | **FUTURE OPERATION (Not implemented)** | Project-scoped (`MASTER`; Phase 4) | **`SELECT` only** | Future Phase 4 migration `INSERT, UPDATE` for `utda_runtime` + RLS |
| `utda.drive_file_mappings` | **FUTURE OPERATION (Not implemented)** | Project-scoped (`D01`–`D15`, `MASTER`) or Global Company (`COMPANY_MASTER`, `project_id IS NULL`; Phase 3/6) | **`SELECT` only** | Future Phase 3/6 migration grant + separate handling for `COMPANY_MASTER` |
| `utda.drive_sync_revisions` | **FUTURE OPERATION (Not implemented)** | Project-scoped via `mapping_id -> drive_file_mappings.project_id` (Phase 6) | **`SELECT` only** | Future Phase 6 migration grant + parent-join RLS |
| `utda.company_aggregation_runs` | **FUTURE OPERATION (Not implemented)** | Global Company-Wide (`target_document_id = 'COMPANY_MASTER'`, Phase 5) | **`SELECT` only** | Dedicated gated aggregator/`utda_governance` path — never `utda_runtime` in Repo C runs |
| `utda.project_provisioning_runs` | **FUTURE OPERATION (Not implemented)** | Project-scoped (Phase 3 provisioner) | **`SELECT` only** | Future Phase 3 provisioning migration grant |
| `utda.template_releases` | Read by future provisioner (not implemented) | Global template metadata (Phase 3) | `SELECT` only | Managed by `utda_migrator`/provisioner |
| `utda.schema_migrations` | Read/written by `engine/migrations/runner.py` & `db_health.py` | Global schema metadata | `SELECT` only | **`utda_migrator`** exclusive writer |

---

## 4. Trusted Repository Identity Review (`GITHUB_REPOSITORY`)

Traced end-to-end across workflows and Python ingestion:

1. **Trusted Workflow Origin:**
   - [reusable-docs.yml#L88](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/.github/workflows/reusable-docs.yml#L88) sets `GITHUB_REPOSITORY: ${{ github.repository }}` from GitHub's immutable workflow execution context; neither `reusable-docs.yml` nor [documentation-trigger.yml](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/.github/workflows/documentation-trigger.yml) accepts a `repository` input.
2. **Untrusted Override Protections (`normalize_event()` in [engine/trigger.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py) + `resolve_project()` in [engine/config_loader.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/config_loader.py)):**
   - **GitHub Actions strictness:** When `GITHUB_ACTIONS=true`, missing/empty `GITHUB_REPOSITORY` fails closed (`ERR_MISSING_TRUSTED_REPOSITORY`), refusing any `GITHUB_EVENT_PATH` fallback.
   - **Event payload spoofing guard:** Whenever both `GITHUB_REPOSITORY` env and `payload["repository"]["full_name"]` exist and differ, `normalize_event()` raises `ProjectIdentityError(..., code="ERR_REPOSITORY_PAYLOAD_MISMATCH")`.
   - **Dispatch input spoofing guard:** Non-empty `payload["inputs"]["repository"]` raises `ERR_UNTRUSTED_DISPATCH_REPOSITORY`. Disagreeing `payload["inputs"]["project_id"]` vs `UTDA_PROJECT_ID` raises `ERR_PROJECT_ID_MISMATCH`, and mismatched `inputs.project_id` alone is caught by `resolve_project()`.
   - **Report JSON & Repo C `project-config.json`:** Neither can set or override `repository`; both are strictly compared against canonical `entry["project_id"]` resolved solely from `(owner, repo)` in Repo B's `config/project_registry.json`.
   - Verified by 4 new spoofing cases added in `test_authoritative_project_resolution_and_zero_writes_on_failure` (`tests/test_phase1_event_pipeline.py`).
3. **Future Enhancement — Immutable GitHub Repository ID Binding:**
   - `NormalizedEvent.repository_id` now captures `GITHUB_REPOSITORY_ID` / `payload["repository"]["id"]` when present. As a planned Phase 3 registry/schema enhancement (deferred from Phase 2B.2), bind numeric GitHub repository IDs (`github.repository_id` in `project_registry.json` and `utda.projects`) so repository renames or ownership transfers cannot reassign an existing `project_id`.

---

## 5. Migration Integrity Confirmation (`0001`–`0004`)

Verified via `discover_migrations(Path('engine/migrations'))` and `git diff` against `c0deb74`:

| Migration File | `c0deb74` Baseline SHA-256 | Current `7e1dc5c` SHA-256 | Match? |
| --- | --- | --- | --- |
| `0001_initial_utda_schema.sql` | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | **Exact Match** |
| `0002_roles_and_indexes.sql` | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | **Exact Match** |
| `0003_idempotency_fencing.sql` | `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b` | `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b` | **Exact Match** |
| `0004_idempotency_fence_version_hardening.sql` | `7060f50e1820b12194c21cac19c0505454d2a7527c941aac4917e2d2032567ad` | `7060f50e1820b12194c21cac19c0505454d2a7527c941aac4917e2d2032567ad` | **Exact Match** |

No `0005` migration file exists in `engine/migrations/`.

---

## 6. Remaining Limitations & Explicit Non-Access Confirmation

1. **GUC Is Not Yet Enforced by RLS (`0005` Pending Phase 2B.3):** `utda.current_project_id` is transaction-local application scoping, **not** an authenticated PostgreSQL tenant identity; Phase 2B.3 (`0005` + RLS policies) is required for DB-layer row enforcement under `utda_runtime`.
2. **Future Phase 2–6 Tables Remain Unimplemented:** `document_proposals`, `approval_records`, `rollback_operations`, `project_master_versions`, `drive_file_mappings`, `drive_sync_revisions`, `company_aggregation_runs`, and `project_provisioning_runs` have no repository writers implemented yet (`utda_runtime` retains `SELECT` only).
3. **Unauthenticated CI Artifact Download Limit:** Per-test XML inside `utda-pg16-security-ci-46115615778` requires authenticated GitHub download (`HTTP 401`), while Step 9 exit `0` / `completed: success` confirms full PG16 suite pass.
4. **Non-Production / Dry-Run Confirmation:** `UTDA_DRY_RUN=true` was enforced throughout. Zero hosted Supabase connections, hosted migrations, Google Drive operations, OpenRouter API calls, repository provisioning, or production GitHub PR mutations occurred.

---

## 7. Closure Gate Assessment

**VERIFIED** — Real PostgreSQL 16 CI (`Run 46115615778`, `completed: success`) verified all 8 transaction-local `utda.current_project_id` lifecycle behaviors (including `pool_size=1` connection reuse, commit/rollback resets, Project A $\to$ unscoped, Project A $\to$ Project B, pre-commit retry recovery, and pre-SQL validation rejection); source-level inventory confirmed zero unscoped project operations in `UTDADatabaseRepository` or `AuditStore`; trusted `GITHUB_REPOSITORY` origin and spoofing guards were verified; and migrations `0001`–`0004` checksums match verbatim.

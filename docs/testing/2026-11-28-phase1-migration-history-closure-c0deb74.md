# Migration History Preservation & Phase 1 Final Closure Report

## 1. Task Information

- **Task Name & Objective:** UTDA Phase 1 — Migration History Preservation and Final Closure (`c0deb74`). Restores `engine/migrations/0003_idempotency_fencing.sql` to its original `0f1a82c` definition, isolates `attempt_fence_version` `INTEGER -> BIGINT` and `CHECK >= 0` hardening into forward-only `engine/migrations/0004_idempotency_fence_version_hardening.sql`, expands real PostgreSQL 16 migration/upgrade/rollback coverage across all `0001`–`0004` paths, and verifies Phase 1 closure readiness.
- **Date of Execution:** `2026-11-28`
- **Branch Name:** `setup/utda-foundation`
- **Verified Code & Migration Commit SHA:** `c0deb74`
- **Files Inspected:**
  - `engine/migrations/0001_initial_utda_schema.sql`
  - `engine/migrations/0002_roles_and_indexes.sql`
  - `engine/migrations/0003_idempotency_fencing.sql`
  - `engine/migrations/runner.py`
  - `engine/database.py`
  - `engine/audit_store.py`
  - `engine/trigger.py`
- **Files Changed / Created in `c0deb74` & Report Commit:**
  - `engine/migrations/0003_idempotency_fencing.sql` (restored verbatim to `0f1a82c` text)
  - `engine/migrations/0004_idempotency_fence_version_hardening.sql` *(new forward-only migration)*
  - `tests/test_database_phase1.py` (added all 10 PG16 migration/upgrade/rollback tests for `0001`–`0004`)
  - `docs/testing/README.md` (index updated)
  - `docs/testing/2026-11-28-phase1-migration-history-closure-c0deb74.md` *(this report)*

---

## 2. Historical Migration Checksums & Immutability Decision

### 2.1 Exact SHA-256 Checksums (`discover_migrations()` UTF-8 Normalized Digest)

| Migration | Historical `0f1a82c` | Edited `1576d19` | Final `c0deb74` | Status |
| --- | --- | --- | --- | --- |
| `0001_initial_utda_schema.sql` | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | `73f2a79a913ef6b54975cbed4f40f94bc276bf046cd21a1f9d85f398990ade78` | Never modified |
| `0002_roles_and_indexes.sql` | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | `0c7417fbc8711202384e0e0156a480ae57152d02a7b2df5f994b5d734ac5c67f` | Never modified |
| `0003_idempotency_fencing.sql` | `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b` | `7e379d3434409d1c8cd622365bbfe8e0e48bf93d6fc45158fc6b32a106a8b3ba` | `0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b` | **Restored to `0f1a82c`** |
| `0004_idempotency_fence_version_hardening.sql` | *(absent)* | *(absent)* | `7060f50e1820b12194c21cac19c0505454d2a7527c941aac4917e2d2032567ad` | **New forward-only migration** |

### 2.2 Decision & Reasoning

- While hosted Supabase (`unai-utda`) has never had any migrations executed and GitHub Actions CI uses fresh ephemeral `postgres:16-alpine` databases, commit `0f1a82c` was publicly pushed on `setup/utda-foundation`. It is impossible to prove that no local developer environment, clone, or sandbox applied the original `0003` migration (`0161daae...`).
- Keeping the modified `0003` (`7e379d34...`) would cause `MigrationChecksumError` on any database holding `0f1a82c`'s `0003`. Therefore, `0003_idempotency_fencing.sql` was restored verbatim to `0161daae...`, and the schema hardening was placed cleanly into forward-only `0004_idempotency_fence_version_hardening.sql`.

---

## 3. Migration 0004 Implementation & Responsibilities

| Migration File | Responsibilities |
| --- | --- |
| `0001_initial_utda_schema.sql` | Conditional `utda` schema creation and 17 core UTDA tables (`DEFAULT NOW()`). |
| `0002_roles_and_indexes.sql` | Operational indexes, fail-closed role & inheritance validation, `utda_migrator` ownership transfer, `PUBLIC`/`anon`/`authenticated` revocations, table-by-table `utda_runtime`/`utda_governance` grants, and `SELECT`-only default privileges. |
| `0003_idempotency_fencing.sql` | Adds `active_attempt_id` and `attempt_fence_version INTEGER NOT NULL DEFAULT 0`, partial active index, `uq_processing_attempts_key_attempt`, and deferrable `fk_idempotency_active_attempt` (`DEFERRABLE INITIALLY DEFERRED`). |
| `0004_idempotency_fence_version_hardening.sql` | Converts `utda.idempotency_keys.attempt_fence_version` from `INTEGER` to `BIGINT` (`NOT NULL DEFAULT 0` retained) and adds `chk_idempotency_fence_nonnegative CHECK (attempt_fence_version >= 0)`, preserving existing rows, `active_attempt_id` FK relationships, and indexes. |

---

## 4. Per-File Changes & Test Coverage (`c0deb74`)

| File | Change Summary | Covering Tests | Verification Scope |
| --- | --- | --- | --- |
| `engine/migrations/0003_idempotency_fencing.sql` | Restored original `0f1a82c` SQL (`INTEGER` `attempt_fence_version`). | `test_migrations_empty_db_upgrade_checksum_and_rollback`, `test_real_local_postgres_migrations_and_persistence`, `test_real_postgres_bootstrap_ownership_transfer` | Verified locally (simulated) and on PG16 CI (`46112019027`). |
| `engine/migrations/0004_idempotency_fence_version_hardening.sql` *(new)* | Alters `attempt_fence_version` to `BIGINT` and adds `chk_idempotency_fence_nonnegative`. | `test_migrations_empty_db_upgrade_checksum_and_rollback`, `test_real_local_postgres_migrations_and_persistence`, `test_real_postgres_bootstrap_ownership_transfer`, `test_real_postgres_concurrent_workers_and_atomic_completion` | Verified locally (simulated) and on PG16 CI (`46112019027`). |
| `tests/test_database_phase1.py` | Asserts `0003` checksum stability (`0161daae...`); tests `0001`–`0004` clean install, `0001+0002 -> 0003 -> 0004`, `original 0003 -> 0004` with pre-existing `COMPLETED` and active `IN_PROGRESS` rows, `BIGINT` conversion, `CheckViolation` on `-1`, repeated plan/apply idempotency, and broken `0004` rollback & recovery. | Full `tests/test_database_phase1.py` suite | 13 local unit/simulated tests passed locally; all PG16 steps succeeded in CI (`46112019027`). |

---

## 5. Exact Test Totals & PostgreSQL 16 Verification Results

### 5.1 Locally Executed Commands & Totals (`UTDA_DRY_RUN=true`)

- **Command 1:** `.venv\Scripts\python.exe -m compileall -q engine tests` -> Exit **`0`**.
- **Command 2:** `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs`
  - **Passed:** **29** (**15** local unit/schema/parsing tests + **14** simulated `_FakeDBState` / `_FakePool` DB & pipeline tests)
  - **Failed:** **0**
  - **Skipped:** **6** (5 PG16 CI-only tests due to unset `UTDA_TEST_*` on Windows + 1 intentionally deferred `test_live_supabase_readonly_health_check`)
  - **Total Collected:** **35** (`16.73s`)

### 5.2 PostgreSQL 16 CI Execution & Existing-Record Upgrade Coverage (`c0deb74`)

| Workflow | Run ID | Status | URL | Artifact Metadata |
| --- | --- | --- | --- | --- |
| `UTDA Phase 0 Baseline Validation` | `46112018953` | **`completed: success`** | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46112018953 | — |
| `UTDA Database Integration CI` | `46112019027` | **`completed: success`** (Steps 1–12 all `success`) | https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/46112019027 | `utda-pg16-security-ci-46112019027` (`id: 15249679768`, `3146` bytes) |

Executed PG16 migration & upgrade verifications inside Step 8 & Step 9 of Run `46112019027`:

1. **Clean installation (`0001` through `0004`):** Both direct `utda_migrator` path (`utda_test_db`) and bootstrap ownership-transfer path (`utda_bootstrap_test`) applied `0001`–`0004` cleanly.
2. **Upgrade `0001+0002 -> 0003 -> 0004` (`utda_upgrade_0003_test`):** Pre-existing rows retained defaults (`active_attempt_id = NULL`, `attempt_fence_version = 0`) and upgraded to `bigint`.
3. **Upgrade original `0003 -> 0004` (`utda_orig0003_upgrade_test`):**
   - Verified pre-`0004` column `attempt_fence_version` was `integer`.
   - Seeded `COMPLETED` row (`attempt_fence_version = 1`, `active_attempt_id = NULL`) and active `IN_PROGRESS` row (`attempt_fence_version = 2`, `active_attempt_id = 'orig-att-2'` referencing `processing_attempts`).
   - Verified failed `0004` rolled back atomically (`attempt_fence_version` stayed `integer`, no `0004` row in `schema_migrations`, advisory lock released), and fixed `0004` recovered cleanly.
   - Verified post-`0004` type is `bigint`, existing records kept `('orig03-active-key', 'IN_PROGRESS', 'orig-att-2', 2)` and `('orig03-done-key', 'COMPLETED', None, 1)`, negative `-5` raised `CheckViolation`, `0003`/`0004` checksums matched repository digests, and repeat `--plan` / `--apply` returned `pending == []` and `applied == []`.

---

## 6. Lease-Renewal Integration Status (Phase 2 Requirement)

- `UTDADatabaseRepository.renew_attempt_lease(...)` exists in `engine/database.py` and enforces matching `active_attempt_id`, matching `expected_fence_version`, `IN_PROGRESS` status, non-expired expiry, and atomic `expires_at` extension (verified in unit tests and PG16 concurrency tests).
- **Finding:** Neither `engine/trigger.py` nor `engine/audit_store.py` calls `renew_attempt_lease` periodically during Phase 1 processing (Phase 1 dry-run correlation completes synchronously without long-running external I/O).
- **Phase 2 Requirement:** Periodic worker heartbeat / background lease renewal invoking `renew_attempt_lease` MUST be integrated when Phase 2 introduces long-running AI generation, DOCX mutations, PR orchestration, or Drive synchronization.

---

## 7. Unexecuted Tests, Outstanding Risks & Hosted Supabase Requirements

1. **Unauthenticated CI Artifact Download Blocked (`HTTP 401`):** Artifact `utda-pg16-security-ci-46112019027` (`15249679768`) ZIP and raw workflow logs (`HTTP 403`) cannot be downloaded via unauthenticated GitHub REST API; per-case JUnit counts inside `pg16-junit.xml` were not directly inspected locally.
2. **Hosted Supabase Requirements (Before Production Cutover):**
   - Provision roles (`utda_migrator`, `utda_runtime`, `utda_governance`) with `NOINHERIT` and strong credentials in `unai-utda`.
   - Run read-only connection & TLS verification (`python -m engine.db_health --readonly-verify --json` + `test_live_supabase_readonly_health_check`).
   - Run migrations `0001` through `0004` as `utda_migrator` via Session Pooler (`UTDA_MIGRATION_DATABASE_URL`, port `5432`, TLS `require` or stronger).
   - Verify `utda` schema is excluded from Supabase PostgREST exposed schemas (`Settings -> API -> Exposed schemas`, checklist item `F.7`) and confirm `anon`/`authenticated` have zero effective access.
3. **Additional Non-Blocking Limitations:** No `ruff`/`mypy` config exists in `requirements-dev.txt`; `utda_runtime` role grants are table-level without per-`project_id` RLS policies.

---

## 8. Final Assessment

**PASS WITH LIMITATIONS**

Historical migration `0003` immutability is restored (`0161daae...`), forward-only `0004` hardening is verified across all 10 PostgreSQL 16 migration/upgrade/rollback tests (`Run 46112019027`, `completed: success`), and local checks passed (`29 passed, 0 failed, 6 skipped`). Remaining items are hosted Supabase cutover verification, authenticated JUnit XML download (if desired), and Phase 2 periodic lease-renewal integration.

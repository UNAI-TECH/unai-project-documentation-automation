# UTDA Phase 2A — Architecture & Security Review Report

## 1. Task Information

- **Task Name & Objective:** UTDA Phase 2A — Security and Execution Architecture Design. Inspect the Phase 1 database schema, roles, grants, repository methods, attempt fencing, configuration loaders, prompts, schemas, and workflows, and produce reviewable Phase 2A security, project-isolation, and worker-execution specifications without implementing Phase 2 runtime code or mutating production systems.
- **Date of Execution:** `2026-11-28`
- **Repository Baseline Commit:** `b6bc9a3` (verified code/migration baseline `c0deb74` on `setup/utda-foundation`)
- **Execution Mode & Restrictions:** `UTDA_DRY_RUN=true`; zero hosted Supabase connections or migrations; zero Google Drive connections/writes; zero GitHub repository provisioning or PR writes; historical migrations `0001`–`0004` untouched.

### Files Inspected
- **Migrations & Security Specs:** [0001_initial_utda_schema.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql), [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql), [0003_idempotency_fencing.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0003_idempotency_fencing.sql), [0004_idempotency_fence_version_hardening.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0004_idempotency_fence_version_hardening.sql), [runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py), [DATABASE_SECURITY.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/DATABASE_SECURITY.md), [POSTGRESQL_SECURITY_TEST_CHECKLIST.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/POSTGRESQL_SECURITY_TEST_CHECKLIST.md), [SUPABASE_SETUP_GUIDE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/SUPABASE_SETUP_GUIDE.md)
- **Core Engine:** [database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py), [db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py), [db_health.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_health.py), [trigger.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py), [audit_store.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py), [config_loader.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/config_loader.py), [contracts.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/contracts.py), [github_reader.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/github_reader.py), [report_parser.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/report_parser.py), [sanitization.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/sanitization.py)
- **Config, Prompts, Schemas & Workflows:** [document_registry.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/config/document_registry.json), [impact_matrix.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/config/impact_matrix.json), [project_registry.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/config/project_registry.json), [document_impact_analyzer.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/prompts/document_impact_analyzer.md), [document_section_writer.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/prompts/document_section_writer.md), [document_update.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/document_update.schema.json), [document_section_output.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/document_section_output.schema.json), [approval_record.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/approval_record.schema.json), [feature_report.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/feature_report.schema.json), [project_registry.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/project_registry.schema.json), [project_config_local.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/project_config_local.schema.json), [reusable-docs.yml](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/.github/workflows/reusable-docs.yml), [documentation-trigger.yml](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/.github/workflows/documentation-trigger.yml), [ci-database.yml](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/.github/workflows/ci-database.yml)

### Deliverables Created
1. [PHASE2A_SECURITY_ARCHITECTURE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/architecture/PHASE2A_SECURITY_ARCHITECTURE.md) — Trust boundaries, 8 threat scenarios, current vs. Phase 2 role-permission matrix, AI vs. deterministic document-processing boundaries, DOCX/PR gates, and hosted Supabase verification checklist.
2. [PHASE2A_WORKER_EXECUTION_MODEL.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/architecture/PHASE2A_WORKER_EXECUTION_MODEL.md) — State machine, fenced attempt claiming, `LeaseHeartbeat` design (`TTL=180s`, `interval=60s`, self-fencing abort), stale-worker rejection, two-layer retry & `CommitOutcomeUnknownError` reconciliation, idempotent GitHub PR rules, and shutdown recovery.
3. [PHASE2A_PROJECT_ISOLATION_DESIGN.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/security/PHASE2A_PROJECT_ISOLATION_DESIGN.md) — Bidirectional `project_id` ownership validation, evaluation of PostgreSQL RLS (`set_config(..., is_local=true)` fail-closed policy) vs. alternatives across all 17 tables, child-table join policies, Supavisor pooling safety, credential boundaries, and non-DB isolation controls.
4. `docs/testing/2026-11-28-phase2a-architecture-review.md` *(this report)* + [docs/testing/README.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/testing/README.md) index entry.

---

## 2. Phase 1 Codebase Audit Findings (Direct Observations Driving Phase 2A Design)

| Finding ID | Exact Location | Observed Behavior | Architectural Impact / Action Needed in Phase 2B |
| --- | --- | --- | --- |
| **F-01** | [config_loader.py#L70-L93](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/config_loader.py#L70-L93) & [trigger.py#L161-L166](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py#L161-L166) | `resolve_project` checks `if project_id:` first and returns without verifying that `entry["github"]` matches `repository`. | If `UTDA_PROJECT_ID` or local `project-config.json` names a different project than `GITHUB_REPOSITORY`, lookup would resolve the wrong project. Must require **bidirectional equality** across `GITHUB_REPOSITORY`, `UTDA_PROJECT_ID`, and local `project-config.json`. |
| **F-02** | [0002_roles_and_indexes.sql#L131](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql#L131) & [#L144](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql#L144) | Both `utda_runtime` and `utda_governance` hold **`SELECT` only** on `utda.document_proposals`. | Intentional for Phase 1; Phase 2 `pr_orchestrator.py` requires a forward-only `0005` migration granting scoped `INSERT, UPDATE` (no `DELETE`) on `utda.document_proposals` to `utda_runtime`. |
| **F-03** | [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql) | Permissions are table-level across all 17 tables; no RLS policies exist yet. | Enable transaction-local RLS (`NULLIF(current_setting('utda.current_project_id', true), '')`) via `0005`, with parent-join policies on `processing_attempts` & `drive_sync_revisions`. |
| **F-04** | [audit_store.py#L144-L197](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py#L144-L197) & [trigger.py#L319-L368](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py#L319-L368) | `claim_processing_lock()` drops `fence_version` from `claim_idempotency_and_attempt()`; `record_attempt_outcome()` does not pass `expected_fence_version` to `finalize_processing_attempt()`. | Propagate `fence_version` end-to-end so Phase 2 finalization always asserts `expected_fence_version`. |
| **F-05** | [database.py#L896-L982](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py#L896-L982) | `renew_attempt_lease()` is defined and tested in isolation, **not** called periodically by `trigger.py`/`audit_store.py`, and lacks `CommitOutcomeUnknownError` reconciliation. | Add read-only reconciliation on `CommitOutcomeUnknownError` in `renew_attempt_lease()` and wrap Phase 2 external stages in `LeaseHeartbeat`. |
| **F-06** | [0001_initial_utda_schema.sql#L123](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql#L123) & [database.py#L656-L660](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py#L656-L660) | `processing_attempts.next_retry_at` and `idx_processing_attempts_retry` exist, but `claim_idempotency_and_attempt()` allows immediate reclaim on `FAILED` without checking backoff or terminal retry states. | Enforce `retry_state` classification (`RETRY_SCHEDULED` vs. `NON_RETRYABLE` / `EXHAUSTED`) and `next_retry_at <= NOW()` in Phase 2B. |
| **F-07** | External prerequisites | Repo A (`unai-project-documentation`) `.docx` templates + `template_map.json` and hosted `unai-utda` verification (`F.7`, `--readonly-verify`, `0001`–`0004` owner apply) are pending. | Do not start Phase 2 DOCX implementation or hosted cutover until Repo A templates are inspected and owner approves hosted DB setup. |

---

## 3. Baseline Test Execution Summary (Preserved Phase 1 Behavior)

Since Phase 2A is an architecture/security specification task and modifies **only** Markdown documentation under `docs/`, zero Python, SQL, schema, config, or workflow files were altered.

### Executed Commands & Observed Outcomes (`UTDA_DRY_RUN=true`)

| Check | Exact Command | Outcome | Scope / Notes |
| --- | --- | --- | --- |
| **Python bytecode / syntax check** | `.venv\Scripts\python.exe -m compileall -q engine tests` | **Exit `0`** | All files under `engine/` and `tests/`. |
| **Local unit & simulated contract/DB/pipeline suite** | `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs` | **`29 passed, 0 failed, 6 skipped` (35 collected in `19.87s`)** | Baseline commit `b6bc9a3` (`c0deb74` code). Identical behavior preserved after creating Phase 2A design docs (verified in post-edit run). |

### Exact Skipped Tests (6 of 35)
1. [test_database_phase1.py#L2648](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L2648): `test_live_supabase_readonly_health_check` — *`PENDING: Live Supabase verification requires UTDA_DATABASE_URL and UTDA_RUN_LIVE_SUPABASE_TESTS=true`*
2. [test_database_phase1.py#L1338](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L1338): `test_real_local_postgres_migrations_and_persistence` — *`Local PostgreSQL UTDA_TEST_DATABASE_URL / UTDA_TEST_MIGRATOR_URL not set (runs in ci-database.yml)`*
3. [test_database_phase1.py#L1887](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L1887): `test_real_postgres_least_privilege_and_default_privileges` — *`PG16 multi-role security tests require CI role DSNs (runs in ci-database.yml)`*
4. [test_database_phase1.py#L2201](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L2201): `test_real_postgres_bootstrap_ownership_transfer` — *`PG16 bootstrap ownership-transfer test requires UTDA_TEST_BOOTSTRAP_URL (runs in ci-database.yml)`*
5. [test_database_phase1.py#L2238](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_database_phase1.py#L2238): `test_real_postgres_concurrent_workers_and_atomic_completion` — *`PG16 concurrent worker & atomic transaction test requires UTDA_TEST_RUNTIME_URL (runs in ci-database.yml)`*
6. [test_phase1_event_pipeline.py#L396](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/tests/test_phase1_event_pipeline.py#L396): `test_real_postgres_full_event_correlation_and_recovery` — *`Local PostgreSQL UTDA_TEST_DATABASE_URL / UTDA_TEST_MIGRATOR_URL not set (runs in ci-database.yml)`*

*(PostgreSQL 16 live multi-role, RLS-prerequisite, and concurrency suites were most recently executed on GitHub Actions `postgres:16-alpine` at commit `c0deb74` / Run `46112019027`, `completed: success`; no new code changes occurred in Phase 2A.)*

---

## 4. Limitations & Open Decisions Requiring Owner Review Before Phase 2B

1. **Hosted Supabase Unconnected (`unai-utda`):** Role provisioning, PostgREST schema exclusion (`F.7`), `--readonly-verify`, and initial `0001`–`0004` apply await explicit owner approval and secret configuration.
2. **Repo A Templates Uninspected:** Exact OOXML anchors (`template_map.json`) for `D01`–`D15` and `MASTER` cannot be finalized until Repo A (`unai-project-documentation`) templates are available.
3. **Unresolved Governance & Isolation Decisions (Listed for Review):**
   - **Auto-Published Approval Records:** Confirm `utda_runtime` stays `SELECT`-only on `utda.approval_records` and that `auto_published` rows (when `policy.auto_publish_routine=true`) are persisted by an isolated `utda_governance` approval job rather than relaxing `utda_runtime` grants.
   - **Pre-Resolution Failure Auditing:** Decide whether unauthorized/unregistered repo triggers fail closed strictly via CI stderr/artifacts (`exit 2`) or route to a governance-only quarantine audit table.
   - **Cross-Project `COMPANY_MASTER` Context (Phase 5):** Confirm `utda_governance` (or a future `utda_aggregator` role) executes `project_master_versions` reads under explicit `utda.governance_scope = 'COMPANY_AGGREGATION'`.

---

## 5. Verdict & Next Recommendation

- **Phase 2A Design Verdict:** **READY FOR OWNER ARCHITECTURE REVIEW** (`PASS WITH PREREQUISITES`)
- **Recommended Gate Before Starting Phase 2B Implementation:**
  1. Owner reviews and approves [PHASE2A_SECURITY_ARCHITECTURE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/architecture/PHASE2A_SECURITY_ARCHITECTURE.md), [PHASE2A_WORKER_EXECUTION_MODEL.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/architecture/PHASE2A_WORKER_EXECUTION_MODEL.md), and [PHASE2A_PROJECT_ISOLATION_DESIGN.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/docs/security/PHASE2A_PROJECT_ISOLATION_DESIGN.md) plus the 3 unresolved decisions in Section 4.
  2. Complete the hosted Supabase baseline checklist (`S-01`–`S-08`) when ready.
  3. Authorize Phase 2B hardening (bidirectional `resolve_project`, migration `0005` for RLS + `document_proposals` runtime grants, `fence_version` propagation, and `LeaseHeartbeat` + `renew_attempt_lease` reconciliation) before building Phase 2 DOCX/PR modules.

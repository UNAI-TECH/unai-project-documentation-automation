# Phase 1 Test Report — Testing Without Supabase

**Local Test Environment:** Python 3.14.4 (`psycopg 3.3.9`, `psycopg_pool 3.3.3`, `pytest 8.4.2`)  
**CI Environment:** GitHub Actions `ubuntu-latest` with `postgres:16-alpine` service container  
**Configuration State:** `UTDA_DRY_RUN=true`, **no Supabase credentials configured**, **no hosted migrations executed**, **Phase 2 not started**.

---

## 1. Test Summary (Passed / Failed / Skipped)

### Local Complete Unit Test Suite
- **Command:** `.venv\Scripts\python.exe -m pytest -v -rs`
- **Passed:** **23**
- **Failed:** **0**
- **Skipped:** **3** (0 unexpected skips)
- **Duration:** `9.36s`

### PostgreSQL 16 CI Workflow Verification
- **UTDA Phase 0 Baseline Validation:** Run [`45954149831`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45954149831) (`fd94dc4`) — **`completed: success`**
- **UTDA Database Integration CI:** Run [`45954149848`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45954149848) (`fd94dc4`) — **`completed: success`** across all 10 steps against `postgres:16-alpine`:
  - Step 6: `Create least-privilege roles and run migrations against PostgreSQL 16` — **success**
  - Step 7: `Verify least-privilege role enforcement on PostgreSQL 16` — **success**
  - Step 8: `Run full Phase 0 and Phase 1 test suite against PostgreSQL 16` (`UTDA_TEST_DATABASE_URL` set) — **success**
  - Step 9: `Verify pg_dump backup and pg_restore recovery on PostgreSQL 16` — **success**
  - Step 10: `Verify read-only Supabase probe defaults cleanly to pending when unset` — **success**

---

## 2. Passed Tests (23 Local Unit & Contract Tests)

### Phase 0 Contracts (`tests/test_phase0_contracts.py` — 7 passed)
1. `test_all_schemas_are_valid_draft_2020_12` — **PASSED**
2. `test_change_type_mapping_covers_all_feature_report_enums` — **PASSED**
3. `test_document_registry_preserves_documents_and_both_master_ids` — **PASSED**
4. `test_impact_matrix_preserves_rules_and_human_locks` — **PASSED**
5. `test_project_registry_validates_against_collection_schema` — **PASSED**
6. `test_sample_payloads_validate_against_new_schemas` — **PASSED**
7. `test_workflows_and_caller_contracts` — **PASSED**

### Phase 1 Database Config, Lifecycle & Security (`tests/test_database_phase1.py` — 9 passed)
8. `test_missing_database_url_fails_safely` — **PASSED**
9. `test_rejects_insecure_sslmode_for_remote_host` — **PASSED**
10. `test_rejects_supabase_http_url_and_api_keys` — **PASSED**
11. `test_valid_session_and_transaction_pooler_configs_and_redaction` — **PASSED**
12. `test_connection_recovery_and_failure` — **PASSED**
13. `test_event_receipt_correlation_idempotency_concurrency_and_audit` — **PASSED**
14. `test_logs_never_expose_database_credentials` — **PASSED**
15. `test_migrations_empty_db_upgrade_checksum_and_rollback` — **PASSED**
16. `test_transaction_commit_and_rollback` — **PASSED**

### Phase 1 Event Pipeline, Correlation, Redaction & Safety Guardrails (`tests/test_phase1_event_pipeline.py` — 7 passed)
17. `test_dry_run_default_and_secret_scrubbing` — **PASSED**
18. `test_github_event_parsing_from_webhook_payload_and_branch_ref` — **PASSED**
19. `test_phase1_blocks_docx_drive_publishing_and_provisioning` — **PASSED** (verifies Phase 2–6 modules do not exist / are never imported, `UTDA_DRY_RUN=true`, and `.docx` + registry checksums remain unchanged)
20. `test_pr_first_then_report_push_correlation_and_duplicate_skip` — **PASSED**
21. `test_report_first_then_pr_merge_and_invalid_evidence_guardrails` — **PASSED**
22. `test_report_validation_edge_cases_and_audit_secret_redaction` — **PASSED** (includes `.md`-only rejection, ungrounded `production` deployment rejection, and secret scrubbing in both `audit_events` and `artifacts/audit_record.json`)
23. `test_retry_recovery_after_failed_attempt_and_pending_supabase_probe` — **PASSED**

---

## 3. Failed Tests
- **None (0 failed).**

---

## 4. Skipped Tests & Hosted Supabase Dependency Classification

| Skipped Test (Locally) | Reason | Genuinely Requires Hosted Supabase? |
| --- | --- | --- |
| `tests/test_database_phase1.py::Phase1LivePostgreSQLTests::test_real_local_postgres_migrations_and_persistence` | `UTDA_TEST_DATABASE_URL` not set locally (no local PostgreSQL/Docker on Windows workstation) | **No** — verified against `postgres:16-alpine` in CI run [`45954149848`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45954149848) Step 8 |
| `tests/test_phase1_event_pipeline.py::Phase1EventPipelineTests::test_real_postgres_full_event_correlation_and_recovery` | `UTDA_TEST_DATABASE_URL` not set locally | **No** — verified against `postgres:16-alpine` in CI run [`45954149848`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45954149848) Step 8 |
| `tests/test_database_phase1.py::Phase1LivePostgreSQLTests::test_live_supabase_readonly_health_check` | `PENDING: Live Supabase verification requires UTDA_DATABASE_URL and UTDA_RUN_LIVE_SUPABASE_TESTS=true` | **Yes** — genuinely requires live `unai-utda` Supabase DSN + TLS + Supavisor Session Pooler reachability |

### Additional Checks That Genuinely Require Hosted Supabase (Deferred Until Approval)
1. Live read-only Supavisor Session Pooler TLS handshake & health check (`python -m engine.db_health --readonly-verify --json`) against `unai-utda`.
2. First remote migration execution (`python -m engine.migrations.runner --apply`) and privilege verification of `utda_migrator` / `utda_runtime` inside `unai-utda` (requires explicit owner approval).
3. Supabase Free Plan pause/restore and live `pg_dump` export verification against `unai-utda`.

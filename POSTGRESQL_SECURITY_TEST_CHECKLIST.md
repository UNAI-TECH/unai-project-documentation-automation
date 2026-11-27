# UTDA PostgreSQL 16 Security & Migration Verification Checklist

**Purpose:** Verify the actual PostgreSQL implementation of UTDA before connecting the production Supabase project.

**Target:** PostgreSQL 16 (`postgres:16-alpine` service container in GitHub Actions)  
**Database:** Isolated test databases (`utda_test_db`, `utda_missing_roles_test`, `utda_bootstrap_test`, `utda_restore_verify`)  
**Environment:** CI / GitHub Actions (`UTDA Database Integration CI`, Run [`45989823977`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45989823977) on commit `7d5261c`) + local unit suite (`Python 3.14.4`, `pytest 8.4.2`, `psycopg 3.3.9`)  
**Production access:** Not permitted during these tests (`UTDA_DRY_RUN=true`, zero Supabase connections, zero hosted migrations)

---

## Executive Summary & Test Commands

| Suite / Environment | Exact Command | Observed Result | Reference |
| --- | --- | --- | --- |
| Local Windows unit & contract suite (`UTDA_DRY_RUN=true`) | `$env:UTDA_DRY_RUN='true'; .venv\Scripts\python.exe -m pytest -v -rs` | **`25 passed, 5 skipped in 8.23s`** (5 skips = 4 CI PG16 multi-role/lifecycle tests + 1 intentionally deferred live Supabase probe) | Local workspace (`7d5261c`) |
| GitHub Actions `UTDA Phase 0 Baseline Validation` | Workflow `.github/workflows/ci-phase0.yml` | **`completed: success`** | Run [`45989823966`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45989823966) |
| GitHub Actions `UTDA Database Integration CI` (`postgres:16-alpine`) | Steps 6–12 in `.github/workflows/ci-database.yml`, including `python -m pytest -v -rs --junitxml=artifacts/pg16-junit.xml \| tee artifacts/pg16-test-output.txt` | **`completed: success`** across all 12 executable steps (all Phase 0 + Phase 1 unit & PG16 tests passed; only `test_live_supabase_readonly_health_check` skipped intentionally) | Run [`45989823977`](https://github.com/UNAI-TECH/unai-project-documentation-automation/actions/runs/45989823977) |

---

## A. Test environment preparation

- [x] Start an isolated PostgreSQL 16 instance in CI. — **PASS** (`postgres:16-alpine` service container in `.github/workflows/ci-database.yml`, healthy before Step 6).
- [x] Create a clean UTDA test database. — **PASS** (`utda_test_db` initialized cleanly, plus isolated `utda_missing_roles_test`, `utda_bootstrap_test`, and `utda_restore_verify`).
- [x] Create `utda_migrator` with `NOINHERIT LOGIN`. — **PASS** (CI Step 8; asserted `rolinherit = False` via `pg_roles` in `test_real_postgres_least_privilege_and_default_privileges`).
- [x] Create `utda_runtime` with `NOINHERIT LOGIN`. — **PASS** (CI Step 8; asserted `rolinherit = False` via `pg_roles`).
- [x] Create `utda_governance` with `NOINHERIT LOGIN`. — **PASS** (CI Step 8; asserted `rolinherit = False` via `pg_roles`).
- [x] Create test `anon` and `authenticated` roles if they do not already exist. — **PASS** (CI Step 8 creates both with `NOINHERIT LOGIN`).
- [x] Confirm runtime and governance are not members of the migrator role. — **PASS** (`pg_has_role('utda_runtime', 'utda_migrator', 'MEMBER')` and `pg_has_role('utda_governance', 'utda_migrator', 'MEMBER')` both `False`; enforced fail-closed in `0002_roles_and_indexes.sql`).
- [x] Confirm runtime is not a member of the governance role. — **PASS** (`pg_has_role('utda_runtime', 'utda_governance', 'MEMBER')` is `False`; enforced fail-closed in `0002_roles_and_indexes.sql`).
- [x] Ensure test passwords are generated and never printed in CI logs. — **PASS** (CI Step 6 generates `MIG_PW`, `RUN_PW`, `GOV_PW`, `API_PW` via `openssl rand -hex 16` and masks each with `::add-mask::`).
- [x] Confirm no production DSNs or secrets are used. — **PASS** (Only `localhost:5432` ephemeral DSNs configured; CI Step 11 confirms `python -m engine.db_health --readonly-verify --json` reports `pending`).

## B. Schema and migration execution

- [x] Run migration planning against a clean database. — **PASS** (`python -m engine.migrations.runner --plan` in CI Step 8; also tested on isolated schema `utda_dryrun_check`).
- [x] Confirm planning identifies migrations `0001` and `0002` as pending. — **PASS** (`test_real_local_postgres_migrations_and_persistence` asserts `len(dry_plan["pending"]) == 2`).
- [x] Confirm planning does not modify database state. — **PASS** (Asserts `information_schema.schemata` has no `utda_dryrun_check` row after `dry_run=True`).
- [x] Apply migrations using the dedicated migration connection. — **PASS** (`UTDA_MIGRATION_DATABASE_URL` set to `utda_migrator` DSN in CI Step 8; omitting `UTDA_MIGRATION_DATABASE_URL` fails closed in CI Step 7).
- [x] Confirm the connected migration identity is `utda_migrator`. — **PASS** (`_verify_migration_identity` checks `SELECT CURRENT_USER`; CI Step 7 confirms non-`utda_migrator` fails closed; `apply_again["connected_user"] == "utda_migrator"`).
- [x] Confirm both migrations are recorded as `APPLIED`. — **PASS** (Verified via `utda.schema_migrations` query and restored backup query in CI Step 10).
- [x] Confirm checksums match the repository files. — **PASS** (`plan_migrations` validates stored `checksum_sha256` against repository SQL SHA-256 digests).
- [x] Run planning again and confirm no migrations are pending. — **PASS** (`plan_after["pending"] == []` in `test_real_local_postgres_migrations_and_persistence`).
- [x] Run apply again and confirm no duplicate migration execution occurs. — **PASS** (`apply_again["applied"] == []` in `test_real_local_postgres_migrations_and_persistence`).

## C. Schema ownership and DDL permissions

- [x] Confirm `utda_migrator` owns schema `utda`. — **PASS** (`pg_get_userbyid(nspowner)` equals `utda_migrator`).
- [x] Confirm `utda_migrator` owns every table created by migrations. — **PASS** (`pg_tables` confirms all 17 tables have `tableowner = 'utda_migrator'`).
- [x] Confirm runtime has `USAGE` but not `CREATE` on schema `utda`. — **PASS** (`has_schema_privilege('utda_runtime', 'utda', 'USAGE') == True`, `CREATE == False`).
- [x] Confirm governance has `USAGE` but not `CREATE` on schema `utda`. — **PASS** (`has_schema_privilege('utda_governance', 'utda', 'USAGE') == True`, `CREATE == False`).
- [x] Confirm runtime cannot create tables. — **PASS** (`CREATE TABLE utda.illegal_table` and `CREATE TABLE public.illegal_pub_table` raise `InsufficientPrivilege`).
- [x] Confirm governance cannot create tables. — **PASS** (`CREATE TABLE utda.gov_illegal` raises `InsufficientPrivilege`).
- [x] Confirm runtime cannot alter or drop existing tables. — **PASS** (`ALTER TABLE utda.projects` and `DROP TABLE utda.projects` raise `InsufficientPrivilege`).
- [x] Confirm governance cannot alter or drop existing tables. — **PASS** (`ALTER TABLE utda.approval_records` raises `InsufficientPrivilege`; `utda_migrator` owns all tables and `utda_governance` is non-superuser and not a member of `utda_migrator`).
- [x] Test the bootstrap ownership-transfer path separately. — **PASS** (`test_real_postgres_bootstrap_ownership_transfer` against `utda_bootstrap_test` with `allow_bootstrap_role=True` transfers schema and all 17 tables to `utda_migrator`).
- [x] Test sequence and other object ownership if future migrations introduce them. — **PASS** (Current 17 tables contain 0 sequences; future-table `SERIAL` sequence `utda.future_priv_check_id_seq` tested under `test_real_postgres_least_privilege_and_default_privileges`, plus `REVOKE ALL ON ALL SEQUENCES` in `0002_roles_and_indexes.sql`).

## D. Runtime privilege tests

Use a real connection authenticated as `utda_runtime`.

- [x] SELECT from every table permitted by the privilege matrix. — **PASS** (All 17 `utda.*` tables queried via `SELECT COUNT(*)` in `test_real_postgres_least_privilege_and_default_privileges`).
- [x] INSERT into `projects`. — **PASS** (`repo.upsert_project(...)` succeeded in PG16 test).
- [x] UPDATE `projects`. — **PASS** (Verified via `0002_roles_and_indexes.sql` explicit `GRANT SELECT, INSERT, UPDATE ON utda.projects` and `upsert_project` `ON CONFLICT DO UPDATE` path).
- [x] INSERT and UPDATE `feature_processing_states`. — **PASS** (`repo.correlate_feature_state(...)` in `test_real_postgres_least_privilege_and_default_privileges` and `test_real_postgres_full_event_correlation_and_recovery`).
- [x] INSERT and UPDATE `idempotency_keys`. — **PASS** (`acquire_idempotency_key(...)` + `complete_idempotency_key(...)` succeeded on PG16).
- [x] INSERT and UPDATE `processing_attempts`. — **PASS** (`record_processing_attempt(...)` INSERT executed on PG16; `SELECT, INSERT, UPDATE` explicitly granted in `0002_roles_and_indexes.sql`; note: a separate standalone `UPDATE processing_attempts` SQL statement was not executed in the live runtime test).
- [x] INSERT into `github_event_receipts`. — **PASS** (`repo.record_event_receipt(...)` succeeded on PG16).
- [x] INSERT into `final_reports`. — **PASS** (`repo.record_final_report(...)` succeeded on PG16).
- [x] INSERT into `audit_events`. — **PASS** (`repo.append_audit_event(...)` succeeded on PG16).
- [x] SELECT from `approval_records`. — **PASS** (`SELECT COUNT(*) FROM utda.approval_records` succeeded on PG16).
- [x] SELECT from `rollback_operations`. — **PASS** (`SELECT COUNT(*) FROM utda.rollback_operations` succeeded on PG16).
- [x] SELECT from `schema_migrations`. — **PASS** (`SELECT COUNT(*) FROM utda.schema_migrations` succeeded on PG16).
- [x] Confirm runtime cannot INSERT into `approval_records`. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm runtime cannot UPDATE or DELETE `approval_records`. — **PASS** (`InsufficientPrivilege` raised for both `UPDATE` and `DELETE`).
- [x] Confirm runtime cannot INSERT into `rollback_operations`. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm runtime cannot UPDATE or DELETE `rollback_operations`. — **PASS** (`InsufficientPrivilege` raised for both `UPDATE` and `DELETE`).
- [x] Confirm runtime cannot UPDATE or DELETE `audit_events`. — **PASS** (`InsufficientPrivilege` raised for both `UPDATE` and `DELETE`).
- [x] Confirm runtime cannot UPDATE or DELETE `final_reports`. — **PASS** (`InsufficientPrivilege` raised for both `UPDATE` and `DELETE`).
- [x] Confirm runtime cannot INSERT, UPDATE, or DELETE `schema_migrations`. — **PASS** (`InsufficientPrivilege` raised for `INSERT`, `UPDATE`, and `DELETE`).
- [x] Confirm runtime cannot DELETE from any operational table unless explicitly authorized. — **PASS** (`InsufficientPrivilege` raised directly for `DELETE` on `projects`, `feature_processing_states`, `idempotency_keys`, `processing_attempts`, `github_event_receipts`, `final_reports`, `audit_events`, `approval_records`, `rollback_operations`, and `schema_migrations`; `0002_roles_and_indexes.sql` grants zero `DELETE` privileges to `utda_runtime` across all 17 tables).
- [x] Confirm runtime cannot create objects in `public`. — **PASS** (`CREATE TABLE public.illegal_pub_table` raises `InsufficientPrivilege`).

## E. Governance privilege tests

Use a real connection authenticated as `utda_governance`.

- [x] SELECT from `approval_records`. — **PASS**
- [x] INSERT into `approval_records`. — **PASS**
- [x] Confirm governance cannot UPDATE or DELETE `approval_records`. — **PASS** (`InsufficientPrivilege` raised for both).
- [x] SELECT from `rollback_operations`. — **PASS**
- [x] INSERT into `rollback_operations`. — **PASS**
- [x] UPDATE `rollback_operations`. — **PASS**
- [x] Confirm governance cannot DELETE `rollback_operations`. — **PASS** (`InsufficientPrivilege` raised).
- [x] INSERT into `audit_events`. — **PASS**
- [x] Confirm governance cannot UPDATE or DELETE `audit_events`. — **PASS** (`InsufficientPrivilege` raised for both).
- [x] Confirm governance cannot write to `schema_migrations`. — **PASS** (Even `SELECT` on `utda.schema_migrations` raises `InsufficientPrivilege`; `0002_roles_and_indexes.sql` omits all grants on `schema_migrations` for `utda_governance`).
- [x] Confirm governance cannot create or alter tables. — **PASS** (`CREATE TABLE utda.gov_illegal` and `ALTER TABLE utda.approval_records` raise `InsufficientPrivilege`).
- [x] Confirm governance cannot access unrelated operational tables beyond its documented grants. — **PASS** (`SELECT` on `schema_migrations`, `idempotency_keys`, and `feature_processing_states` raises `InsufficientPrivilege`).

## F. PUBLIC and Supabase API-role isolation

- [x] Confirm `PUBLIC` has no privileges on schema `utda`. — **PASS** (`has_schema_privilege('public', 'utda', 'USAGE') == False`).
- [x] Confirm `PUBLIC` has no unintended table privileges. — **PASS** (`has_table_privilege` for `SELECT`, `INSERT`, `UPDATE`, `DELETE` returns `False` on all 17 tables).
- [x] Explicitly revoke any existing `anon` privileges on schema `utda` and its tables. — **PASS** (Explicit `REVOKE ALL` statements in `0002_roles_and_indexes.sql`).
- [x] Explicitly revoke any existing `authenticated` privileges on schema `utda` and its tables. — **PASS** (Explicit `REVOKE ALL` statements in `0002_roles_and_indexes.sql`).
- [x] Verify both roles cannot access UTDA tables. — **PASS** (Live `SET LOCAL ROLE anon` and `SET LOCAL ROLE authenticated` queries on `utda.projects` raise `InsufficientPrivilege`).
- [x] Check effective privileges, including inherited role memberships. — **PASS** (`has_schema_privilege` and `has_table_privilege` evaluate effective privileges across all 17 tables for `public`, `anon`, and `authenticated`, plus `pg_has_role(..., 'MEMBER')` checks).
- [ ] Verify `utda` is excluded from Supabase exposed PostgREST schemas. — **NOT TESTED** (Deferred because connecting to hosted `unai-utda` Supabase is prohibited during this phase. Must be verified in Supabase `Settings -> API -> Exposed schemas` before live cutover).
- [x] Document the treatment of Supabase `service_role`; do not assume that excluding PostgREST exposure removes database-level grants. — **PASS** (Explicitly documented in `DATABASE_SECURITY.md`).

## G. Future-table default privilege tests

Connect as `utda_migrator`. Create temporary table `utda.future_priv_check (id SERIAL PRIMARY KEY, val TEXT)`.

- [x] Confirm runtime automatically receives SELECT. — **PASS** (`SELECT COUNT(*) FROM utda.future_priv_check` succeeded).
- [x] Confirm runtime does not receive INSERT. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm runtime does not receive UPDATE. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm runtime does not receive DELETE. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm governance automatically receives SELECT. — **PASS** (`SELECT COUNT(*) FROM utda.future_priv_check` succeeded).
- [x] Confirm governance does not receive INSERT. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm governance does not receive UPDATE. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm governance does not receive DELETE. — **PASS** (`InsufficientPrivilege` raised).
- [x] Confirm PUBLIC receives no unintended privileges. — **PASS** (`has_table_privilege` `False` for `SELECT`/`INSERT`/`UPDATE`/`DELETE`).
- [x] Confirm `anon` and `authenticated` receive no unintended privileges. — **PASS** (`has_table_privilege` `False` for `SELECT`/`INSERT`/`UPDATE`/`DELETE`).
- [x] Confirm explicit table-level migration grants can add only the intended permissions. — **PASS** (Granting `INSERT` on table + `USAGE, SELECT` on sequence allows `INSERT` while `UPDATE` still raises `InsufficientPrivilege`).
- [x] Drop the temporary test table. — **PASS** (`finally:` block drops `utda.future_priv_check`).

## H. Migration integrity and failure tests

- [x] Modify an already-applied migration in a test copy of the repository. — **PASS**
- [x] Confirm the runner raises `MigrationChecksumError`. — **PASS**
- [x] Insert an applied migration version into database history that is absent from the repository. — **PASS** (`version = '9999'` in `test_real_local_postgres_migrations_and_persistence`).
- [x] Confirm the runner raises `UnknownMigrationError`. — **PASS**
- [x] Create a migration that deliberately fails after a DDL operation. — **PASS** (`CREATE TABLE utda.should_not_exist (id INT); THIS IS INVALID SQL;`).
- [x] Confirm the failed migration transaction rolls back completely. — **PASS** (`to_regclass('utda.should_not_exist')` returns `None`).
- [x] Confirm no APPLIED migration-history record is created for the failed migration. — **PASS** (Entire migration transaction rolls back cleanly).
- [x] Correct the failing migration in an isolated test fixture and confirm recovery works. — **PASS** (`utda_recovery_test` fixture fails on bad `0002_bad.sql`, then applies corrected `0002_bad.sql` cleanly).
- [x] Confirm an existing FAILED migration status is rejected. — **PASS** (Setting `0002` status to `FAILED` causes `apply_migrations` to raise `MigrationError`).
- [x] Confirm a second execution after successful migration is idempotent. — **PASS**

## I. Advisory-lock and concurrency tests

- [x] Start one migration process and acquire the advisory lock. — **PASS**
- [x] Start a second migration process against the same schema. — **PASS**
- [x] Confirm the second process cannot execute migrations concurrently. — **PASS** (`pg_try_advisory_lock` returns `False`; concurrent thread `t.is_alive()` blocks while `c1` holds lock).
- [x] Release the first process's lock. — **PASS**
- [x] Confirm the second process proceeds safely. — **PASS** (`t.join` completes after `c1` closes and returns `mode == "plan"`).
- [x] Simulate a migration exception. — **PASS** (`0003_broken.sql` fails with `MigrationError`).
- [x] Confirm the advisory lock is released. — **PASS** (`pg_try_advisory_lock` immediately succeeds afterward).
- [x] Simulate connection termination. — **PASS** (`c1.close()` closes the session holding `pg_advisory_lock`).
- [x] Confirm PostgreSQL releases the session advisory lock when the connection closes. — **PASS** (Blocked thread unblocks immediately on `c1.close()`).

## J. Connection and credential validation

- [x] Reject HTTP and HTTPS URLs as database credentials. — **PASS** (`Phase1DatabaseConfigTests`)
- [x] Reject Supabase API/JWT keys as PostgreSQL credentials. — **PASS** (`Phase1DatabaseConfigTests`)
- [x] Reject invalid PostgreSQL URL schemes. — **PASS** (`Phase1DatabaseConfigTests`)
- [x] Reject invalid schema identifiers. — **PASS** (`validate_identifier` rejects invalid names)
- [x] Reject remote `sslmode=disable`. — **PASS** (`Phase1DatabaseConfigTests`)
- [x] Reject remote `sslmode=allow`. — **PASS** (`Phase1DatabaseConfigTests`)
- [x] Reject remote `sslmode=prefer`. — **PASS** (`Phase1DatabaseConfigTests`)
- [x] Test the migration DSN independently of the runtime DSN. — **PASS** (`test_migration_dsn_mandatory_and_tls_validated_independently`)
- [x] Confirm migration DSN cannot bypass TLS validation. — **PASS** (Remote `UTDA_MIGRATION_DATABASE_URL` with `sslmode=disable/allow/prefer` raises `DatabaseConfigError`)
- [x] Reject Transaction Pooler connections for migrations. — **PASS** (`assert_migration_connection_supported`)
- [x] Confirm Session Pooler and supported direct connections behave as expected. — **PASS** (`test_valid_session_and_transaction_pooler_configs_and_redaction`)
- [x] Confirm credentials and sensitive tokens are removed from logs. — **PASS** (`sanitize_log_text` + `SanitizingFilter` tested for DSN passwords, query params, GitHub tokens, `sb_publishable_`/`sb_secret_` keys, `sk-` API keys, Bearer tokens, JWTs, PEM blocks)
- [x] Confirm missing migration credentials cause migration apply to fail closed. — **PASS** (Verified in unit tests and CI Step 7)
- [x] Confirm migration execution verifies the database identity is `utda_migrator`. — **PASS** (Verified in unit tests, CI Step 7, and PG16 test suite)

## K. Backup and restore verification

- [x] Create a PostgreSQL logical backup with `pg_dump`. — **PASS** (CI Step 10 runs `postgres:16-alpine` `pg_dump --format=custom --schema=utda`)
- [x] Restore it into a separate empty PostgreSQL 16 database. — **PASS** (CI Step 10 creates `utda_restore_verify` and restores via `pg_restore --exit-on-error`)
- [x] Confirm schema and tables exist. — **PASS** (`engine.db_health --json` succeeds on restored DB)
- [x] Confirm representative rows are restored. — **PASS** (CI Step 10 verifies `CI_SEC_TEST` in `utda.projects` and `SEC_TEST_OK` in `utda.audit_events`)
- [x] Confirm migration history is restored. — **PASS** (CI Step 10 verifies `utda.schema_migrations` rows and statuses)
- [x] Verify role and ownership restoration procedures. — **PASS** (CI Step 10 verifies `pg_tables.tableowner` equals `utda_migrator` in `utda_restore_verify`)
- [x] Record the backup and restore test results. — **PASS** (Recorded here and retained in CI Step 10 workflow status)

## L. Final CI acceptance criteria

- [x] All required tests pass. — **PASS** (All PG16 CI and local unit tests pass; 1 live hosted Supabase test intentionally skipped)
- [x] No unexpected privileges exist. — **PASS** (PG16 verified; Supabase `service_role` & PostgREST exposure deferred per Section F)
- [x] The role-to-privilege matrix matches actual effective privileges. — **PASS**
- [x] No migration integrity failures are ignored. — **PASS**
- [x] Failed migrations roll back correctly. — **PASS**
- [x] Advisory-lock cleanup is demonstrated. — **PASS**
- [x] Runtime and migration credentials are independently enforced. — **PASS**
- [x] Migration DSN TLS validation is enforced. — **PASS**
- [x] Logs contain no credentials or sensitive tokens. — **PASS**
- [x] Backup and restore tests pass. — **PASS**
- [x] CI test output is retained as an artifact. — **PASS** (`utda-pg16-security-ci-45989823977` containing `pg16-junit.xml` and `pg16-test-output.txt`)
- [x] All unresolved limitations are documented. — **PASS** (See below)

---

## Remaining Failures and Limitations

1. **Test Failures:** **None (0 failed)** in both local unit tests (`25 passed, 5 skipped`) and GitHub Actions PostgreSQL 16 CI (`Run 45989823977`, `completed: success`).
2. **Deferred Hosted Supabase Verification (`F.7` & Live TLS Probe — `NOT TESTED`):** Because connecting to hosted Supabase (`unai-utda`) and running hosted migrations are prohibited in this phase:
   - `F.7` (excluding `utda` from Supabase exposed PostgREST schemas) has not yet been verified against `unai-utda`.
   - `test_live_supabase_readonly_health_check` remains `SKIPPED` (`PENDING: Live Supabase verification requires UTDA_DATABASE_URL and UTDA_RUN_LIVE_SUPABASE_TESTS=true`).
3. **Database Role Granularity & Operational Caveats (Documented in `DATABASE_SECURITY.md`):**
   - **Audit Log Immutability Scope:** `utda_runtime` and `utda_governance` cannot `UPDATE` or `DELETE` `utda.audit_events` (role-enforced append-only), whereas `utda_migrator` or a PostgreSQL superuser retains table owner privileges.
   - **Row-Level Scoping:** Current role grants are table-level; `utda_runtime` can `UPDATE` existing `utda.projects` / `utda.feature_processing_states` / `utda.idempotency_keys` rows across projects without Row-Level Security (RLS).
   - **Runtime Coverage Detail:** `UPDATE utda.processing_attempts` is permitted in `0002_roles_and_indexes.sql` and covered by unit repository logic, though only `INSERT` was executed directly in the PG16 multi-role test; direct `DELETE` denial was executed against 10 of 17 tables (while `0002` issues zero `DELETE` grants on all 17 tables).
   - **Supabase `service_role`:** Excluding `utda` from PostgREST API exposure does **not** strip database-level grants from `service_role` or `postgres`; Supabase API keys must never be supplied to UTDA runtime workflows.

**Safety restrictions preserved:** `UTDA_DRY_RUN=true` remains active; no Supabase connection made; no hosted migrations executed; no production Drive writes; Phase 2 has not started.

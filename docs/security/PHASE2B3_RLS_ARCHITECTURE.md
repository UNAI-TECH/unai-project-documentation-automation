# UTDA Phase 2B.3 — Row-Level Security (RLS) Architecture & Policy Matrix

**Document Status:** Phase 2B.3 Authoritative Specification (Synchronized with `0001`–`0004` and `0005_project_row_level_security.sql`)  
**Baseline Commit:** `7e1dc5c` (`b00c0aa` verification report on `setup/utda-foundation`)  
**Safety Invariants:** `UTDA_DRY_RUN=true`, zero hosted Supabase connections, historical migrations `0001`–`0004` preserved byte-for-byte, zero premature write grants on unimplemented Phase 2+ tables.

---

## 1. Security Boundary & Threat Model (`utda.current_project_id`)

### 1.1 Explicit Limitation of Transaction-Local GUC Context
PostgreSQL Row-Level Security (RLS) in UTDA evaluates the transaction-local setting:
- `current_setting('utda.current_project_id', true)` (set inside `DatabaseManager.transaction()` / `project_transaction()` via `SELECT set_config('utda.current_project_id', %s, true)`)
- `current_setting('utda.governance_scope', true)` (reserved exclusively for `utda_governance` company-wide aggregation reads/audits)

**Critical Threat-Model Boundary:**
1. **`utda.current_project_id` is an application-provided transaction-scoping context, NOT an authenticated database tenant identity.** All project runs execute under the shared PostgreSQL role `utda_runtime`.
2. **What RLS DOES protect against:**
   - Omitted or buggy `WHERE project_id = ...` filters inside `UTDADatabaseRepository` or future modules.
   - Accidental cross-project reads/inserts/updates inside a transaction scoped to Project A.
   - Missing, empty, whitespace-padded, or malformed project contexts (`NULL`, `''`, `'   '`, lowercase) — every project policy requires `current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'` and fails closed (0 rows / `42501` check violation).
   - Unintended `NULL` `project_id` leakage (e.g., `COMPANY_MASTER` `drive_file_mappings` or global `audit_events` rows cannot be seen by `utda_runtime`).
3. **What RLS DOES NOT independently prevent:**
   - Arbitrary raw SQL injection or compromise of the shared `utda_runtime` credential executing an arbitrary `SELECT set_config('utda.current_project_id', 'OTHER_PROJ', true)`. Defense against that remains application repository resolution (`resolve_project`), parameterized SQL (`%s`), `utda_runtime` table-privilege denial (`approval_records`, `rollback_operations`, `schema_migrations`, zero `DELETE`), and separate `utda_governance` / `utda_migrator` credentials.

### 1.2 Role, Ownership, `BYPASSRLS`, & `FORCE ROW LEVEL SECURITY` Analysis

Inspected against [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql) and [POSTGRESQL_SECURITY_TEST_CHECKLIST.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/POSTGRESQL_SECURITY_TEST_CHECKLIST.md):

| Role | Table Owner? | `SUPERUSER`? | `BYPASSRLS`? | `NOINHERIT`? | Subject to RLS? | Rationale |
| --- | --- | --- | --- | --- | --- | --- |
| **`utda_migrator`** | **Yes** (owns `utda` schema, all 17 tables, all sequences) | No (in Supabase & CI) | No | Yes | **Bypassed as table owner** (when `FORCE` is not set) | Must run DDL migrations and off-Supabase `pg_dump` backups (`UTDA_MIGRATION_DATABASE_URL`) cleanly without setting single-project GUCs. Isolated from runtime/governance (`0002` fail-closed checks). |
| **`utda_runtime`** | **No** | **No** | **No** | **Yes** | **Strictly Enforced** | Central Phase 1/2 runtime engine role. Not a member of `utda_migrator` or `utda_governance` (verified via `pg_has_role` in `0002`). |
| **`utda_governance`** | **No** | **No** | **No** | **Yes** | **Strictly Enforced** | Reviewer-gated `utda-production` role (`approval_records`, `rollback_operations`, `COMPANY_MASTER` aggregation reads). Not a member of `utda_migrator`. |
| **`PUBLIC` / `anon` / `authenticated`** | **No** | **No** | **No** | N/A | Blocked at schema & table `GRANT` level | Zero `USAGE` on `utda`, zero table/sequence/default privileges (`0002`). |
| PostgreSQL `postgres` / CI `utda_test` / Supabase `service_role` | Superuser / Bypass | Yes / Bypass | Yes | Varies | **Bypasses RLS** | Admin/DBA roles only; never configured in Repo B/C automation (`engine/db_config.py` rejects `sb_secret_`/API keys). |

**Decision on `FORCE ROW LEVEL SECURITY`:**
- Use `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` **without** `FORCE ROW LEVEL SECURITY` on project-scoped tables.
- **Why `FORCE` is NOT enabled (and would be harmful here):** `utda_migrator` is the non-superuser table owner used for schema migrations and `pg_dump --schema=utda` backups (`DATABASE_BACKUP_AND_RECOVERY.md`). Enabling `FORCE` without adding broad `TO utda_migrator USING (true)` policies would cause `pg_dump` as `utda_migrator` to export **zero rows** cleanly-silently breaking disaster recovery backups! Meanwhile, neither `utda_runtime` nor `utda_governance` is a table owner, superuser, or `BYPASSRLS` role, nor a member of `utda_migrator`, so PostgreSQL enforces RLS unconditionally for both application roles.

### 1.3 Permissive Policy Composition Discipline
In PostgreSQL, **permissive policies targeting the same role/command combine with `OR`**.
- **Rule 1:** Every policy explicitly specifies `AS PERMISSIVE FOR <COMMAND> TO <single_role>` — **never** `TO PUBLIC` and **never** omit `TO`.
- **Rule 2:** Never give `utda_runtime` a permissive policy referencing `utda.governance_scope`. Even if a runtime connection calls `set_config('utda.governance_scope', 'COMPANY_AGGREGATION', true)`, `utda_runtime`'s policies evaluate only `utda.current_project_id`.
- **Rule 3:** Leaving a role with **zero** permissive policies on an RLS-enabled table denies all rows by default (`0` rows returned on `SELECT`, `42501` denied on writes). We use this intentionally on `utda.company_aggregation_runs` for `utda_runtime`!

---

## 2. Canonical Policy Predicates

Let canonical project predicate $\mathcal{P}(\text{col})$ be:
```sql
current_setting('utda.current_project_id', true) ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'
AND <col> = current_setting('utda.current_project_id', true)
```
And let company-aggregation governance context $\mathcal{G}$ be:
```sql
current_setting('utda.governance_scope', true) = 'COMPANY_AGGREGATION'
```
- **Why regex + equality instead of `NULLIF(..., '')` alone:** Rejects `NULL`, `''`, whitespace (`'   '`, `' PROJ_A '`), lowercase (`'proj_a'`), and malformed identifiers natively at SQL evaluation time, and evaluates to `UNKNOWN`/`false` whenever `<col>` is `NULL`.

---

## 3. Complete 17-Table RLS Policy Matrix

| # | Table | Scope | Identity / Parent Relationship | `utda_runtime` Grants (`0002`) | `utda_governance` Grants (`0002`) | `utda_migrator` | RLS Enabled / Forced | Exact Policies per Role & Operation | `project_id IS NULL` Handling | Participates in `COMPANY_MASTER` Aggregation? |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `utda.schema_migrations` | **Global** | None | `SELECT` | None | Owner | **Disabled** (`No`/`No`) | N/A — Global migration state needed by `db_health.py` without project context. | N/A | No |
| 2 | `utda.template_releases` | **Global** | None | `SELECT` | None | Owner | **Disabled** (`No`/`No`) | N/A — Global immutable Repo A template catalog. | N/A | No |
| 3 | `utda.projects` | **Project-scoped** | `project_id` (PK, `NOT NULL`) | `SELECT, INSERT, UPDATE` | `SELECT` | Owner | **Enabled (`Yes`) / Not Forced (`No`)** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`, `FOR UPDATE USING (P(project_id)) WITH CHECK (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT USING (P(project_id) OR G)`. | Column is `NOT NULL` PK. | Indirect (metadata lookup during `G`) |
| 4 | `utda.project_provisioning_runs` | **Project-scoped** | `project_id` (`NOT NULL`) | `SELECT` only | None | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`. *(No write policy / no write grants yet.)* | Column is `NOT NULL`. | No |
| 5 | `utda.github_event_receipts` | **Project-scoped** | `project_id` (**nullable** FK `ON DELETE SET NULL`) | `SELECT, INSERT` | None | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`. | **Denied completely** (`project_id = current_project_id` is false when `NULL`). `NULL` receipts cannot be inserted or read by runtime. | No |
| 6 | `utda.feature_processing_states` | **Project-scoped** | `project_id` (`NOT NULL`) | `SELECT, INSERT, UPDATE` | None | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`, `FOR UPDATE USING (P(project_id)) WITH CHECK (P(project_id))`. | Column is `NOT NULL`. | No |
| 7 | `utda.final_reports` | **Project-scoped** | `project_id` (`NOT NULL`) | `SELECT, INSERT` | None | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`. *(Append-only preserved.)* | Column is `NOT NULL`. | No |
| 8 | `utda.idempotency_keys` | **Project-scoped** | `project_id` (`NOT NULL`) | `SELECT, INSERT, UPDATE` | None | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`, `FOR UPDATE USING (P(project_id)) WITH CHECK (P(project_id))`. | Column is `NOT NULL`. | No |
| 9 | `utda.processing_attempts` | **Project-scoped (Child)** | Parent: `idempotency_key` $\to$ `utda.idempotency_keys(idempotency_key)` | `SELECT, INSERT, UPDATE` | None | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT/INSERT/UPDATE` using parent `EXISTS (SELECT 1 FROM utda.idempotency_keys ik WHERE ik.idempotency_key = processing_attempts.idempotency_key AND P(ik.project_id))`. Parent row visibility required & non-escalated (`SECURITY INVOKER`). | Parent `project_id` is `NOT NULL`; orphans impossible via FK. | No |
| 10 | `utda.document_proposals` | **Project-scoped** | `project_id` (`NOT NULL`) | `SELECT` only | `SELECT` only | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT USING (P(project_id))`. *(No write grants in `0002` or `0005`.)* | Column is `NOT NULL`. | No |
| 11 | `utda.approval_records` | **Project-scoped** | `project_id` (`NOT NULL`) | **`SELECT` only** | **`SELECT, INSERT`** | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`. | Column is `NOT NULL`. | No (`COMPANY_MASTER` approvals handled cleanly per project/governance scope in Phase 5) |
| 12 | `utda.project_master_versions` | **Project-scoped + Aggregation Source** | `project_id` (`NOT NULL`) | `SELECT` only | `SELECT` only | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT USING (P(project_id) OR (G AND eligible_for_aggregation = TRUE))`. | Column is `NOT NULL`. | **Yes** (`eligible_for_aggregation = TRUE` rows readable by `utda_governance` under `G` only) |
| 13 | `utda.company_aggregation_runs` | **Company-Wide (`COMPANY_MASTER`)** | `source_project_id` (`NOT NULL`), `target_document_id = 'COMPANY_MASTER'` | `SELECT` only | `SELECT` only | Owner | **Enabled / Not Forced** | **`utda_runtime`**: **Zero permissive policies (DENY ALL — 0 rows visible)**.<br>**`utda_governance`**: `FOR SELECT USING (G)`. | Column `source_project_id` is `NOT NULL`. | **Yes** (Primary company aggregation ledger — completely isolated from `utda_runtime`) |
| 14 | `utda.drive_file_mappings` | **Hybrid (Project-scoped or `COMPANY_MASTER`)** | `project_id` (**nullable**: non-null for Repo C files; `NULL` for `COMPANY_MASTER`) | `SELECT` only | `SELECT` only | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT USING (P(project_id) OR (G AND project_id IS NULL AND document_id = 'COMPANY_MASTER'))`. | **Invisible to `utda_runtime`**; visible to `utda_governance` **only** when `G` and `document_id = 'COMPANY_MASTER'`. | **Yes** (`COMPANY_MASTER` mapping uses `project_id IS NULL`) |
| 15 | `utda.drive_sync_revisions` | **Project-scoped / Hybrid (Child)** | Parent: `mapping_id` $\to$ `utda.drive_file_mappings(mapping_id)` | `SELECT` only | `SELECT` only | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (EXISTS (... dm.mapping_id = drive_sync_revisions.mapping_id AND P(dm.project_id)))`.<br>**`utda_governance`**: `FOR SELECT USING (EXISTS (... AND (P(dm.project_id) OR (G AND dm.project_id IS NULL AND dm.document_id = 'COMPANY_MASTER'))))`. | Inherited from parent `drive_file_mappings` (denied to runtime). | **Yes** (if revision belongs to `COMPANY_MASTER` mapping) |
| 16 | `utda.rollback_operations` | **Project-scoped + Company Governance** | `project_id` (**nullable** `ON DELETE SET NULL`) | **`SELECT` only** | **`SELECT, INSERT, UPDATE`** | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT/INSERT/UPDATE` with `(P(project_id) OR (G AND project_id IS NULL AND document_id = 'COMPANY_MASTER'))` for both `USING` and `WITH CHECK`. | **Denied to `utda_runtime`**; allowed for `utda_governance` **only** when `G` and `document_id = 'COMPANY_MASTER'`. | Conditional (`COMPANY_MASTER` Drive rollback in Phase 6) |
| 17 | `utda.audit_events` | **Project-scoped + Company Governance Audit** | `project_id` (**nullable** `ON DELETE SET NULL`) | `SELECT, INSERT` | `SELECT, INSERT` | Owner | **Enabled / Not Forced** | **`utda_runtime`**: `FOR SELECT USING (P(project_id))`, `FOR INSERT WITH CHECK (P(project_id))`.<br>**`utda_governance`**: `FOR SELECT/INSERT` with `(P(project_id) OR (G AND project_id IS NULL))` for both `USING` and `WITH CHECK`. | **Denied to `utda_runtime`** (neither readable nor insertable when `NULL`); allowed for `utda_governance` **only** under `G`. | Yes (`COMPANY_MASTER` governance audit trail) |

---

## 4. Parent-Child RLS Evaluation (`processing_attempts` & `drive_sync_revisions`)

1. **`utda.processing_attempts` $\to$ `utda.idempotency_keys`:**
   - When `utda_runtime` inserts or updates a row in `utda.processing_attempts`, PostgreSQL evaluates the child table's `USING` / `WITH CHECK` `EXISTS` subquery **with `utda_runtime`'s effective privileges** (invoker context), which triggers `utda.idempotency_keys`'s own `FOR SELECT` RLS policy.
   - No `SECURITY DEFINER` helper function is used, avoiding any privilege escalation.
   - Under `utda.current_project_id = 'PROJ_A'`:
     - Referencing `PROJ_A`'s `idempotency_key` succeeds on `SELECT`, `INSERT`, and `UPDATE`.
     - Referencing `PROJ_B`'s `idempotency_key` returns `FALSE` from `EXISTS` and fails `INSERT` / affects 0 rows on `SELECT`/`UPDATE` (or fails `WITH CHECK` `42501`).
   - Delete behavior: `utda_runtime` has zero `DELETE` table privileges on both `idempotency_keys` and `processing_attempts` (`0002_roles_and_indexes.sql`), so parent/child deletes raise `InsufficientPrivilege`. Table owner (`utda_migrator`) can still run maintenance deletes (`ON DELETE CASCADE`) without `FORCE ROW LEVEL SECURITY`.
2. **`utda.drive_sync_revisions` $\to$ `utda.drive_file_mappings`:**
   - Same invoker `EXISTS` check; `utda_runtime` only sees revisions whose parent `drive_file_mappings.project_id` equals `utda.current_project_id` (and never sees `COMPANY_MASTER` `project_id IS NULL` revisions).

---

## 5. Verification Matrix Requirements (Workstream D)

All 12 verification scenarios must be proven on real PostgreSQL 16 (`postgres:16-alpine` CI) using actual connections authenticated as `utda_runtime`, `utda_governance`, `utda_migrator`, `anon`/`authenticated`, and admin/superuser separately:
1. `utda_runtime` reads/writes own project's permitted rows when `utda.current_project_id` is set.
2. `utda_runtime` reads `0` rows from another project.
3. `utda_runtime` cannot `INSERT` a row for another project (`InsufficientPrivilege` / RLS `WITH CHECK` error `42501`).
4. `utda_runtime` cannot `UPDATE` another project's rows (`0` rows affected / `WITH CHECK` rejection if attempting to reassign `project_id` to another project!).
5. `utda_runtime` cannot `DELETE` own or another project's rows (`InsufficientPrivilege` from `0002` table grants preserved).
6. Missing `utda.current_project_id` fails closed (`0` rows on `SELECT`, rejected on `INSERT`).
7. Invalid `utda.current_project_id` (`''`, `'   '`, `'lowercase'`, `' PROJ_A '`) fails closed.
8. `utda_governance` is restricted to its explicitly approved operations (`approval_records`, `rollback_operations`, `audit_events`, read-only views) scoped by project or `COMPANY_AGGREGATION`.
9. `NULL`-project rows (`github_event_receipts`, `audit_events`, `rollback_operations`, `drive_file_mappings`) are invisible and uninsertable by `utda_runtime`.
10. Company-wide aggregation (`company_aggregation_runs`, `eligible_for_aggregation` cross-project reads, `COMPANY_MASTER` `drive_file_mappings`) is isolated from `utda_runtime` even if runtime sets `utda.governance_scope`.
11. Parent-child RLS (`processing_attempts` $\leftrightarrow$ `idempotency_keys`, `drive_sync_revisions` $\leftrightarrow$ `drive_file_mappings`) blocks cross-project child `INSERT`/`UPDATE`/`SELECT` and prevents unauthorized deletes.
12. Table owner (`utda_migrator`) and superuser (`utda_test`) bypass behavior and role attributes (`rolsuper=false`, `rolbypassrls=false`, `rolinherit=false`) are tested separately from `utda_runtime` / `utda_governance`.

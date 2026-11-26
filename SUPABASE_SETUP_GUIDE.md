# Supabase Setup Guide (`unai-utda`)

This guide explains how the project owner configures the existing dedicated **`unai-utda`** Supabase project (Free Plan) for UTDA without creating a second project or storing secrets in Git.

---

## 1. Obtaining the PostgreSQL Connection String
1. Open the Supabase Dashboard for project **`unai-utda`**.
2. Click **Connect** (or **Settings -> Database -> Connection string**).
3. Select **Session Pooler** (port **`5432`**, hostname `aws-0-<region>.pooler.supabase.com`).
   - **Why Session Pooler?**
     - Supports **IPv4 and IPv6** (GitHub Actions runners typically lack outbound IPv6, which Supabase Free Plan **Direct** connections require).
     - Preserves PostgreSQL sessions, prepared statements, and `pg_advisory_lock` required by `engine/migrations/runner.py`.
   - *(Do **not** use the Supabase `https://...` URL, `anon` key, or `service_role` key in `UTDA_DATABASE_URL`.)*
4. Ensure `?sslmode=require` (or `verify-full` with Supabase Root CA in `UTDA_DATABASE_SSLROOTCERT`).

---

## 2. Optional Least-Privilege Role Setup (Recommended)
Run once in the Supabase SQL Editor as `postgres`:
```sql
CREATE ROLE utda_migrator NOINHERIT LOGIN PASSWORD '<UNIQUE_MIGRATOR_PASSWORD>';
CREATE ROLE utda_runtime NOINHERIT LOGIN PASSWORD '<UNIQUE_RUNTIME_PASSWORD>';
```
- Put the `utda_migrator` Session Pooler DSN in `UTDA_MIGRATION_DATABASE_URL` (used only during approved schema migrations).
- Put the `utda_runtime` Session Pooler (or Transaction Pooler port `6543`) DSN in `UTDA_DATABASE_URL` (used by runtime jobs).
- Keep the `utda` schema excluded from Supabase Data API (`PostgREST`) exposed schemas.

---

## 3. GitHub Environment Secret Configuration (`unai-project-documentation-automation`)
In Repository B (**Settings -> Environments -> `utda-sandbox` / `utda-production`**):
1. Add **Secrets**:
   - `UTDA_DATABASE_URL` (Session Pooler DSN for `utda_runtime`)
   - `UTDA_MIGRATION_DATABASE_URL` (Session Pooler DSN for `utda_migrator`, kept in protected admin/migration environment only)
2. Add **Variables**:
   - `UTDA_DATABASE_SCHEMA=utda`
   - `UTDA_DATABASE_POOL_MODE=session`
   - `UTDA_DATABASE_POOL_SIZE=4`
   - `UTDA_DATABASE_SSLMODE=require`
   - `UTDA_DRY_RUN=true`

---

## 4. First Remote Migration Checklist (Requires Explicit Owner Approval)
1. Review [0001_initial_utda_schema.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql) and [0002_roles_and_indexes.sql](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql).
2. Preview pending migrations with `python -m engine.migrations.runner --plan`.
3. Only after owner approval, apply with `python -m engine.migrations.runner --apply`.
4. Verify health with `python -m engine.db_health --json`.

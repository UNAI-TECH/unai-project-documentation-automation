# UTDA Database Security & Access Control

## 1. Credential & Configuration Rules
- Store DSNs exclusively in `UTDA_DATABASE_URL` (runtime) and `UTDA_MIGRATION_DATABASE_URL` (migrations) via GitHub Environment Secrets or local `.env` (gitignored).
- **Never** use Supabase `anon` keys, `service_role` keys, or HTTPS project URLs (`https://<ref>.supabase.co`) as PostgreSQL credentials; `engine/db_config.py` explicitly rejects them.
- **Never** expose database credentials to frontend/browser code or individual application repositories (Repo C).
- **TLS/SSL:** Remote connections enforce `sslmode=require` (or `verify-ca` / `verify-full` with `UTDA_DATABASE_SSLROOTCERT`). Insecure `sslmode=disable` is rejected for non-local hosts.
- **Log Sanitization:** `DatabaseConfig.__repr__`, `redact_dsn()`, and `SanitizingFilter` strip passwords and sensitive query parameters before logging.

---

## 2. Least-Privilege Database Roles

| Role | Purpose | Privileges |
| --- | --- | --- |
| `utda_migrator` | Schema migrations only (manual / gated admin workflow) | `USAGE, CREATE` on schema `utda`; DDL ownership/privileges on `utda.*` tables |
| `utda_runtime` | Automation engine (`UTDA_DATABASE_URL`) | `USAGE` on schema `utda`; `SELECT, INSERT, UPDATE` on `utda.*` operational tables; `SELECT` only on `utda.schema_migrations`; **no** `CREATE` on `utda` or `public`, **no** `DELETE` on `audit_events` or `schema_migrations` |
| Supabase `anon` / `authenticated` | Public/API roles (unused) | **No** grants on `utda` schema; Supabase Data API / PostgREST disabled or restricted from `utda` schema |

### Role Provisioning SQL (Run Once by DB Owner)
Create `utda_migrator` and `utda_runtime` in Supabase SQL Editor with strong unique passwords before enabling `0002_roles_and_indexes.sql` grants:
```sql
CREATE ROLE utda_migrator NOINHERIT LOGIN PASSWORD '<STRONG_MIGRATOR_PASSWORD>';
CREATE ROLE utda_runtime NOINHERIT LOGIN PASSWORD '<STRONG_RUNTIME_PASSWORD>';
```
*(In Supabase Dashboard -> API Settings, keep `utda` excluded from exposed PostgREST schemas so the Data API never exposes internal automation tables.)*

---

## 3. GitHub Actions Security
- Store `UTDA_DATABASE_URL` in protected Repo B GitHub Environments (`utda-sandbox`, `utda-production`) with required reviewers.
- Set `timeout-minutes` on every DB job (`10`–`15` minutes) and `concurrency` locks to avoid runaway retries.
- Keep `UTDA_DRY_RUN=true` until Phase 7 acceptance tests pass.
- Do not allow project repository workflows (Repo C) to execute DDL migrations.

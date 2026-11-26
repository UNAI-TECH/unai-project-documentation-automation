# UTDA Database Migration Guide

## 1. Migration Architecture
- **Location:** [engine/migrations/](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/)
- **Runner:** [engine/migrations/runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py)
- **Naming Convention:** `NNNN_description.sql` (zero-padded 4-digit ordered versions, e.g., `0001_initial_utda_schema.sql`, `0002_roles_and_indexes.sql`).

### Safety Guarantees
1. **Incompatible Pooler Guard:** Migrations refuse to run over Transaction Pooler (`port=6543` or `UTDA_DATABASE_POOL_MODE=transaction`). Use Session Pooler (`port=5432`) or Direct Connection.
2. **Advisory Lock:** Acquires a session-level PostgreSQL `pg_advisory_lock` per schema to prevent concurrent migration races.
3. **Checksum Verification:** Computes SHA-256 of every `.sql` file. Modifying an already-applied migration raises `MigrationChecksumError`.
4. **Atomic Transactions:** Each pending migration runs inside a transaction (`COMMIT` on success, full `ROLLBACK` on error) so partial DDL is never silently left behind.
5. **No Automatic Production Migrations:** Ordinary project repository workflows never run migrations. Hosted Supabase migrations require explicit owner review and approval first.

---

## 2. Migration Commands

### Plan Pending Migrations (Read-Only Dry Run)
```powershell
python -m engine.migrations.runner --plan
```

### Apply Pending Migrations (After Explicit Owner Approval)
Uses `UTDA_MIGRATION_DATABASE_URL` if set, otherwise `UTDA_DATABASE_URL` (must be Session Pooler or Direct mode):
```powershell
python -m engine.migrations.runner --apply
```

### Check Database Health & Applied Version
```powershell
python -m engine.db_health --json
```

---

## 3. Authoring New Migrations
1. Add the next sequential file (e.g., `0003_add_reconciliation_table.sql`).
2. Keep SQL standard PostgreSQL (no Supabase-only extensions) and qualify objects in `utda.*`.
3. Never edit an already-applied migration file; create a forward migration instead.
4. Test against an empty local PostgreSQL instance and upgrade path via `python -m unittest discover -s tests -v`.
5. Never include destructive `DROP TABLE`, `TRUNCATE`, or `RESET` statements without explicit backup and owner sign-off.

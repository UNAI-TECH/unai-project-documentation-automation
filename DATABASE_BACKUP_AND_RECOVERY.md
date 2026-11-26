# UTDA Database Backup, Recovery & Free Plan Operations

## 1. Supabase Free Plan Operational Constraints
- **Storage Quota:** **500 MB** maximum database size.
- **Compute:** Shared CPU/RAM; keep connection pools small (`UTDA_DATABASE_POOL_SIZE=4`) and short statement timeouts (`15000ms`).
- **Inactivity Pausing:** Free projects pause after 7 days of inactivity.
- **No Automated Backups:** Supabase Free Plan does **not** include daily automated backups or Point-in-Time Recovery (PITR).
- **System Resilience Rule:** PostgreSQL is the durable automation ledger, **not** the sole copy of business documents (GitHub holds all authoritative `.docx` files and canonical `.json` reports; Google Drive holds synchronized copies).

---

## 2. Monitoring & Health Checks
Run [engine/db_health.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_health.py) via scheduled workflow or CLI:
```powershell
python -m engine.db_health --json
```
- **Warning:** `>= 70%` of 500 MB (`350 MB`) or uninitialized migrations.
- **Critical:** `>= 85%` of 500 MB (`425 MB`).

---

## 3. Retention & Pruning Strategy (Staying Well Under 500 MB)
- **Zero Binary Storage:** Never store `.docx` binaries or large PR diffs in `JSONB` columns. Store commit SHAs, file paths, Drive IDs, and SHA-256 checksums only.
- **Retention Policy (Recommended):**
  - Keep `projects`, `drive_file_mappings`, `approval_records`, `project_master_versions`, `company_aggregation_runs`, and `schema_migrations` indefinitely (tiny metadata footprint).
  - Archive/export `github_event_receipts`, `processing_attempts`, and `audit_events` older than 180 days to encrypted GitHub/object-storage archives before pruning if quota exceeds 70%.

---

## 4. External Backup, Verification & Restoration Procedure

### 4.1 Scheduled External Logical Backup (`pg_dump`)
Run against **Session Pooler (`port=5432`)** or Direct connection using `UTDA_MIGRATION_DATABASE_URL`:
```bash
pg_dump --format=custom --schema=utda --no-owner --no-privileges \
  "$UTDA_MIGRATION_DATABASE_URL" -f "utda_backup_$(date -u +%Y%m%dT%H%M%SZ).dump"
```
Store the encrypted `.dump` file in restricted off-Supabase storage (never in Git).

### 4.2 Backup Verification
Periodically restore the `.dump` into an ephemeral local `postgres:16` container and verify row counts + migration table:
```bash
pg_restore --dbname="$LOCAL_VERIFY_DSN" --no-owner --no-privileges utda_backup_*.dump
python -m engine.db_health --json
```

### 4.3 Restoration to Target PostgreSQL / Supabase
1. Provision a clean schema or portable PostgreSQL target.
2. Run `pg_restore --clean --if-exists --no-owner --no-privileges --dbname="$UTDA_MIGRATION_DATABASE_URL" utda_backup_*.dump`.
3. Re-verify `python -m engine.migrations.runner --plan` and `python -m engine.db_health --json`.

---

## 5. Recovery After a Paused Supabase Free Project
1. Log into Supabase Dashboard -> project `unai-utda` -> click **Restore project** (unpause).
2. Wait for compute/database status to become **Active** (typically 1–3 minutes).
3. Run `python -m engine.db_health --json` to verify connectivity and migration state.
4. To reduce unexpected pausing during low-activity weeks, run a lightweight scheduled read-only health-check workflow (`SELECT 1`) every 3–4 days while respecting Free Plan limits.

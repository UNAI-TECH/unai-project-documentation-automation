# UTDA Implementation Status

**Current Stage:** Phase 1 (Database Integration Slice) Complete — Awaiting Owner Review Before Hosted Migrations or Remaining Phase 1 Modules  
**Production Publishing:** Disabled (`UTDA_DRY_RUN=true`). **Hosted Supabase migrations have NOT been executed yet.**

---

## 1. Fixed Architecture Decisions

- **Master Record Scope (`BOTH`):** Project-level `MASTER` + Company-wide `COMPANY_MASTER` (`unai-company-master-record`).
- **Document Authority (`GITHUB_AUTHORITY`):** GitHub PRs + role-aware approvals are authoritative; Google Drive receives post-merge sync only.
- **Database Provider:** Dedicated Supabase project `unai-utda` (Free Plan, portable PostgreSQL via `psycopg` v3, Session Pooler primary).

---

## 2. Phase 0 & Phase 1 Progress

| Component / Deliverable | Status | Verification |
| --- | --- | --- |
| Phase 0 Architecture, Schemas, Registry, Workflows & Contracts | **Complete (Preserved)** | `tests/test_phase0_contracts.py` passing |
| [engine/db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py) | **Complete** | Unit tested (validation, TLS, pool mode, log redaction) |
| [engine/database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py) | **Complete** | Unit tested (pool lifecycle, retry, commit/rollback, concurrency, persistence) |
| [engine/migrations/](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/) (`0001`, `0002`, `runner.py`) | **Complete (Local/CI Verified; Hosted Pending)** | Empty-DB, upgrade, checksum & rollback tested |
| [engine/db_health.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_health.py) | **Complete** | Unit tested (500 MB quota & migration checks) |
| Database Docs (`DATABASE_SCHEMA.md`, `DATABASE_MIGRATION_GUIDE.md`, `DATABASE_SECURITY.md`, `DATABASE_BACKUP_AND_RECOVERY.md`, `SUPABASE_SETUP_GUIDE.md`) | **Complete** | Reviewed |
| Remaining Phase 1 Core Engine (`config_loader.py`, `github_reader.py`, `report_parser.py`, `trigger.py`) | Not Started | Pending DB review |
| Phases 2–7 (DOCX/PR pipeline, Provisioning, `MASTER`, `COMPANY_MASTER`, Drive sync, Hardening) | Not Started | Blocked behind Phase 1 |

---

## 3. Pending External Actions & Missing Prerequisites

1. Owner configuration of `UTDA_DATABASE_URL` / `UTDA_MIGRATION_DATABASE_URL` for Supabase project `unai-utda` (and optional `utda_runtime` / `utda_migrator` roles).
2. Explicit owner approval to execute the first hosted migration (`python -m engine.migrations.runner --apply`) and run live `test_live_supabase_readonly_health_check`.
3. Remaining Phase 1+ secrets & repos noted in Phase 0: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_DRIVE_SHARED_DRIVE_ID`, `UTDA_BACKUP_FOLDER_ID`, Repo A (`unai-project-documentation`), and Repo D (`unai-company-master-record`).

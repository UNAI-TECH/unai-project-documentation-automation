# UTDA Implementation Status

**Current Stage:** Phase 1 — Core Engine Complete (Verified Against PostgreSQL 16 CI; Hosted Supabase Migrations Pending Owner Approval)  
**Production Publishing:** Disabled (`UTDA_DRY_RUN=true`). **No hosted Supabase migrations, Drive writes, AI DOCX updates, provisioning, or production publishing have been executed.**

---

## 1. Fixed Architecture Decisions

- **Master Record Scope (`BOTH`):** Project-level `MASTER` + Company-wide `COMPANY_MASTER` (`unai-company-master-record`).
- **Document Authority (`GITHUB_AUTHORITY`):** GitHub PRs + role-aware approvals are authoritative; Google Drive receives post-merge sync only.
- **Database Provider:** Dedicated Supabase project `unai-utda` (Free Plan, portable PostgreSQL via `psycopg` v3, Session Pooler primary).

---

## 2. Phase 0 & Phase 1 Progress

| Component / Deliverable | Status | Verification |
| --- | --- | --- |
| Phase 0 Architecture, Schemas, Registry, Workflows & Contracts | **Complete (Preserved)** | `tests/test_phase0_contracts.py` passing (local + CI) |
| Database Config, Lifecycle & Health ([db_config.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_config.py), [database.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/database.py), [db_health.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/db_health.py)) | **Complete** | Verified in unit tests & PostgreSQL 16 CI (`45820044223`) |
| SQL Migrations & Runner ([0001](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0001_initial_utda_schema.sql), [0002](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/0002_roles_and_indexes.sql), [runner.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/migrations/runner.py)) | **Complete (PG16 CI Verified; Hosted Pending)** | Atomicity, advisory locks, least-privilege roles & `pg_dump`/`pg_restore` verified on PostgreSQL 16 (`45820044223`) |
| Core Engine Event Pipeline ([config_loader.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/config_loader.py), [github_reader.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/github_reader.py), [report_parser.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/report_parser.py), [audit_store.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/audit_store.py), [trigger.py](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/engine/trigger.py)) | **Complete** | Full lifecycle, PR-first & report-first correlation, deduplication & recovery tested (`test_phase1_event_pipeline.py` + PG16 CI) |
| Live Hosted Supabase Connection & Migrations (`unai-utda`) | **Pending Owner Action** | Read-only probe (`--readonly-verify`) ready; awaits credentials & explicit migration approval |
| Phases 2–7 (DOCX/PR pipeline, Provisioning, `MASTER`, `COMPANY_MASTER`, Drive sync, Hardening) | **Not Started** | Blocked until Phase 1 review & hosted DB approval |

---

## 3. Pending External Actions (Required Before Hosted Migration / Phase 2)

1. Owner configuration of `UTDA_DATABASE_URL` and `UTDA_MIGRATION_DATABASE_URL` for `unai-utda` (plus `utda_migrator` / `utda_runtime` roles and excluding `utda` from PostgREST).
2. Read-only live Supabase verification (`python -m engine.db_health --readonly-verify --json`) once `UTDA_DATABASE_URL` is configured.
3. **Explicit owner approval** before executing `python -m engine.migrations.runner --apply` against hosted `unai-utda`.
4. Phase 2+ external prerequisites: Repo A (`unai-project-documentation` + 16 `.docx` templates), Repo D (`unai-company-master-record`), GitHub App, OpenRouter credentials, and Google Drive folder IDs.

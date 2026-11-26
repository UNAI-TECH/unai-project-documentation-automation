# Engine Implementation Modules

Under `GITHUB_AUTHORITY` and `MASTER` + `COMPANY_MASTER` (`BOTH`), the `engine/` package is implemented across Phases 0–6:

- **Phase 0 (`contracts.py`):** Architecture constants (`GITHUB_AUTHORITY`, `BOTH`, `MASTER`, `COMPANY_MASTER`), deterministic `change_type` mapping, and baseline JSON schema/config validation helpers.
- **Phase 1 (Core Engine):** `config_loader.py`, `audit_store.py` (PostgreSQL persistence & idempotency), `trigger.py` (event normalization & PR/report correlation), `github_reader.py` (evidence extraction & secret redaction), `report_parser.py` (canonical JSON validation against `schemas/feature_report.schema.json`).
- **Phase 2 (GitHub Document Pipeline):** `impact_analyzer.py` (OpenRouter impact call + deterministic policy overlay), `ai_generator.py` (per-section proposal), `document_updater.py` (surgical DOCX edits), `document_validator.py` (structural & approval lock checks), `pr_orchestrator.py` & `approval_router.py` (documentation branch, PR creation, role-aware approvals).
- **Phase 3 (Provisioning):** `provisioner.py` (idempotent, checkpointed cloning of 16 templates, `project-config.json`, SHA-256 manifests, registry entry, and Drive folder/file creation).
- **Phase 4–5 (Master Records):** Project `MASTER` updates via PR pipeline + `master_aggregator.py` (incremental, provenance-tracked aggregation into `unai-company-master-record` `COMPANY_MASTER`).
- **Phase 6 (GitHub-to-Drive Sync & Recovery):** `drive_sync.py` (post-merge Drive upload, checksum verification, drift detection, conflict flagging) and `rollback.py` (revision/checksum rollback).

All modules must respect `UTDA_DRY_RUN=true` and never log credentials or sensitive document content.

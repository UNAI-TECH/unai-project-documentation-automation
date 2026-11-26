# UTDA Implementation Plan (Phase 0 – Phase 7)

This plan follows the fixed architecture decisions (**Master Record Scope = `BOTH`** (`MASTER` + `COMPANY_MASTER`) and **Document Authority = `GITHUB_AUTHORITY`**) across the four-repository topology (**A:** `unai-project-documentation`, **B:** `unai-project-documentation-automation`, **C:** project repositories, **D:** `unai-company-master-record`).

---

## Phase 0 — Architecture and Contract Alignment *(Current Phase)*
**Depends on:** Final architecture decisions (`BOTH`, `GITHUB_AUTHORITY`).

- Update all architecture, provisioning, data flow, security, acceptance, configuration, and status docs.
- Align [config/document_registry.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/config/document_registry.json) (`MASTER` + `COMPANY_MASTER`), [config/impact_matrix.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/config/impact_matrix.json) (`COMPANY_MASTER` + role approvals), and initialize [config/project_registry.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/config/project_registry.json).
- Add missing schemas: `project_registry_collection.schema.json`, `project_config_local.schema.json`, `document_section_output.schema.json`, `master_aggregation.schema.json`, and `approval_record.schema.json`.
- Fix GitHub workflow contracts (`requirements.txt`, multi-repo checkouts in `reusable-docs.yml`, env vars, concurrency, artifact uploads, canonical JSON trigger paths).
- Add Phase 0 contract constants (`engine/contracts.py`) and baseline validation tests (`tests/test_phase0_contracts.py`).

## Phase 1 — Core Engine
**Depends on:** Phase 0 approval + PostgreSQL database schema design.

- `engine/config_loader.py`: Load and validate registry, impact matrix, document registry, and local `project-config.json`.
- `engine/audit_store.py`: Dedicated PostgreSQL database schema/migrations and transactional repository layer for projects, provisioning checkpoints, events, PRs, approvals, `MASTER` versions, `COMPANY_MASTER` aggregation runs, Drive sync revisions, drift flags, and rollbacks.
- `engine/github_reader.py`: Extract PR metadata, commits, diffs, and report blob SHA with secret scrubbing.
- `engine/report_parser.py`: Parse and validate canonical `documentation/reports/final/<feature-id>.json` against `schemas/feature_report.schema.json` (rejecting ungrounded test/deployment claims).
- `engine/trigger.py`: Event normalization, flexible PR/report correlation (report first or PR merge first), `WAITING_FOR_FINAL_REPORT`, and PostgreSQL idempotency constraints.

## Phase 2 — GitHub Document Pipeline
**Depends on:** Phase 1 + Repo A real `.docx` template inspection (`template_map.json`).

- Inspect all 16 original `.docx` templates in Repo A and generate `template_manifest.json` and `template_map.json`.
- `engine/impact_analyzer.py`: OpenRouter impact analysis + deterministic `impact_matrix.json` / `change_type` policy overlay.
- `engine/ai_generator.py`: Per-section proposal generation (`schemas/document_section_output.schema.json`) scoped strictly to current project evidence.
- `engine/document_updater.py` & `engine/document_validator.py`: Surgical DOCX editing preserving formatting, plus pre-PR validation of structure, evidence, `always_human_fields`, and `never_auto_approve`.
- `engine/pr_orchestrator.py` & `engine/approval_router.py`: Create documentation branches and PRs in Repo C; enforce role-aware approvals (`pm_or_po`, `pm`, `cto_or_tech_lead`, `authorized_human`) and record `approval_record.schema.json` entries before merge.

## Phase 3 — Project Provisioning
**Depends on:** Phase 2 + GitHub App org setup + Repo A release tag.

- `engine/provisioner.py` + `.github/workflows/provision-project.yml`:
  - Assign unique `project_id`.
  - Clone/copy all 16 approved DOCX templates from Repo A preserving formatting/filenames (including independent project `MASTER`).
  - Create `documentation/reports/final/`, `documentation/project-config.json`, `documentation/template-manifest.json` (with SHA-256 checksums), and `.github/workflows/utda.yml`.
  - Create corresponding Google Drive project folder + initial synchronized copies (`file_id`s).
  - Register project in Repo B `config/project_registry.json` and PostgreSQL with resumable checkpoints.

## Phase 4 — Project Master Record Automation
**Depends on:** Phase 2 & Phase 3.

- Evidence-based updates to project-level `Master Record.docx` (`MASTER`) via normal Repo C documentation PR workflow.
- Enforce policy-based approvals and `mandatory_human` locks on sensitive sections (`3`, `4`, `6.3`, `7`).
- Record merged `MASTER` commit SHAs, versions, checksums, and approval history in PostgreSQL; mark merged versions eligible for company aggregation.

## Phase 5 — Company-Wide Master Record
**Depends on:** Phase 4 + `unai-company-master-record` (Repo D) setup.

- `engine/master_aggregator.py`: Incrementally collect eligible merged `MASTER` versions across registered projects.
- Validate `project_id`, `source_commit_sha`, `source_version`, and `evidence_refs` (`schemas/master_aggregation.schema.json`).
- Prevent duplicate entries, preserve historical provenance, open PR in `unai-company-master-record` (`COMPANY_MASTER`), require `company_reviewer` approval, merge, and record provenance in PostgreSQL.

## Phase 6 — Google Drive Synchronization
**Depends on:** Phase 2, Phase 4, Phase 5 + sandbox Drive folder.

- `engine/drive_sync.py`: Post-merge GitHub-to-Drive synchronization for Repo C (`D01`–`D15`, `MASTER`) and Repo D (`COMPANY_MASTER`) on stable Drive `file_id`s.
- Verify checksums after upload; record GitHub commit SHA ↔ Drive revision/checksum/timestamp in PostgreSQL.
- Detect out-of-band Drive edits as `DRIFT_DETECTED` (never silently overwrite GitHub or accept Drive edits automatically; require explicit reconciliation).
- Safe retries, concurrency locks, pre-sync backups, and `engine/rollback.py` for GitHub/Drive recovery.

## Phase 7 — Production Hardening
**Depends on:** Phases 0–6 complete.

- Security testing (secret redaction, cross-project isolation, role-bypass & prompt-injection tests).
- Multi-project integration tests, concurrency/race tests, drift/conflict tests, and failure/rollback recovery tests.
- Live end-to-end sandbox acceptance tests before enabling `UTDA_DRY_RUN=false` on a pilot repository.

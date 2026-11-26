# UTDA Phase Acceptance Criteria (Phase 0 – Phase 7)

## Phase 0 — Architecture and Contract Alignment
- **Dependencies:** Final decisions (`BOTH` Master Records, `GITHUB_AUTHORITY`).
- **Acceptance Criteria:**
  1. All 8 architecture/planning docs reflect `GITHUB_AUTHORITY`, `MASTER` + `COMPANY_MASTER`, four-repo topology (A/B/C/D), PostgreSQL audit DB, role-aware approvals, and Phase 0–7 sequence.
  2. All 8 JSON Schemas in `schemas/` pass Draft 2020-12 schema validation.
  3. `config/document_registry.json`, `config/impact_matrix.json`, and `config/project_registry.json` validate cleanly with `MASTER` and `COMPANY_MASTER` aligned and existing approval locks preserved.
  4. Workflow YAML files pass parsing/contract tests (`requirements.txt`, multi-repo checkout in `reusable-docs.yml`, `dry_run: true` default, canonical `.json` report triggers).
  5. Baseline test suite (`python -m unittest discover -s tests -v`) passes 100%.

## Phase 1 — Core Engine
- **Dependencies:** Phase 0 + PostgreSQL instance/URL (`UTDA_DATABASE_URL`).
- **Acceptance Criteria:**
  1. PostgreSQL migrations create all audit/idempotency tables with unique constraints preventing duplicate processing.
  2. Correlation works in both orders: report push before PR merge and PR merge before report push (`WAITING_FOR_FINAL_REPORT`).
  3. `report_parser.py` validates canonical `.json` reports strictly against `feature_report.schema.json` and rejects ungrounded `passed` tests or `staging`/`production` deployment claims.
  4. `github_reader.py` redacts secret/token patterns in unit tests.

## Phase 2 — GitHub Document Pipeline
- **Dependencies:** Phase 1 + Repo A real `.docx` templates (`template_map.json`).
- **Acceptance Criteria:**
  1. `template_map.json` resolves 100% of `D01`–`D15` and `MASTER` sections against real Repo A templates.
  2. `document_updater.py` preserves Word styles, tables, headers, footers, and unrelated content on template copies.
  3. `document_validator.py` blocks modifications to `always_human_fields` and `never_auto_approve` auto-merges.
  4. `pr_orchestrator.py` + `approval_router.py` open a real GitHub documentation PR in a sandbox repo, enforce role-aware approval checks, and write `approval_record.schema.json` entries to PostgreSQL.

## Phase 3 — Project Provisioning
- **Dependencies:** Phase 2 + GitHub App installed on org + sandbox Shared Drive.
- **Acceptance Criteria:**
  1. Provisioning creates all 16 intact `.docx` copies, `project-config.json`, `template-manifest.json` (with verified SHA-256 checksums), `reports/final/`, and `utda.yml` in a test Repo C.
  2. Provisioning creates an isolated Drive project folder with 16 `file_id`s and registers the project in `config/project_registry.json` and PostgreSQL.
  3. Re-running provisioning is idempotent; mid-run failure resumes cleanly from PostgreSQL checkpoint without duplicate folders/files.

## Phase 4 — Project Master Record Automation
- **Dependencies:** Phase 2 & Phase 3.
- **Acceptance Criteria:**
  1. Feature updates touching project milestones/architecture/status propose evidence-backed updates to project `MASTER` via Repo C PR.
  2. Restricted sections (`3`, `4`, `6.3`, `7`) remain human-gated (`mandatory_human`).
  3. Merged `MASTER` commit SHA and checksum are recorded in PostgreSQL and marked eligible for aggregation.

## Phase 5 — Company-Wide Master Record
- **Dependencies:** Phase 4 + `unai-company-master-record` (Repo D).
- **Acceptance Criteria:**
  1. `master_aggregator.py` aggregates eligible merged `MASTER` versions incrementally into Repo D `COMPANY_MASTER` PRs conforming to `master_aggregation.schema.json`.
  2. Re-running aggregation on the same source `(project_id, source_commit_sha)` produces zero duplicate entries.
  3. Aggregation PR requires `company_reviewer` approval before merge and never modifies other projects' `MASTER` files.

## Phase 6 — Google Drive Synchronization
- **Dependencies:** Phases 2, 4, 5 + sandbox Drive folder.
- **Acceptance Criteria:**
  1. Post-merge sync uploads merged Repo C (`D01`–`D15`, `MASTER`) and Repo D (`COMPANY_MASTER`) DOCX files to mapped Drive `file_id`s and verifies checksum equality.
  2. Out-of-band Drive edit triggers `DRIFT_DETECTED` in PostgreSQL and blocks overwrite without overwriting newer GitHub content or auto-accepting Drive edits.
  3. Simulated Drive failure leaves merged GitHub PR intact (`SYNC_FAILED`) and succeeds on retry; `rollback.py` restores prior verified revisions cleanly.

## Phase 7 — Production Hardening
- **Dependencies:** Phases 0–6 complete.
- **Acceptance Criteria:**
  1. Multi-project isolation tests prove zero cross-project AI context leakage.
  2. Concurrency, retry, drift, security redaction, and rollback tests pass against sandbox GitHub + PostgreSQL + OpenRouter + Google Drive.
  3. Full E2E pilot sign-off completes in `UTDA_DRY_RUN=true` before enabling production publishing.

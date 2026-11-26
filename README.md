# UNAI Tech Documentation Automation (UTDA)

This repository (`unai-project-documentation-automation`) is the central automation engine for **UNAI Tech Documentation Automation (UTDA)**.

> **Current Status:** Phase 0 (Architecture & Contract Alignment) in progress. Production Drive sync, AI generation, provisioning, and automated publishing are **disabled** (`UTDA_DRY_RUN=true`) until subsequent phases pass verification.

## Final Architecture Decisions

1. **Document Authority = `GITHUB_AUTHORITY`**
   - GitHub is the authoritative source of all UTDA documentation.
   - DOCX files are generated, validated, reviewed, and merged via GitHub pull requests with role-aware approvals.
   - Google Drive is a synchronized distribution and collaboration destination **only**—never the authoritative repository.
   - Automation synchronizes merged DOCX versions from GitHub to Google Drive **only after** an approved GitHub PR is merged. Out-of-band Drive edits are flagged as **drift** and require explicit reconciliation.
2. **Master Record Scope = `BOTH`**
   - **Project-level (`MASTER`):** Every registered application repository maintains its own `documentation/Project Documentation/Master Record.docx`.
   - **Company-wide (`COMPANY_MASTER`):** A dedicated `unai-company-master-record` repository maintains the consolidated company-wide `Master Record.docx`, incrementally aggregated from approved, merged project-level records with full provenance.

## Four-Repository Logical Architecture

- **A. `unai-project-documentation`** — Central master DOCX templates, template manifests/SHA-256 checksums, `document_registry.json`, `impact_matrix.json`, `template_map.json`, and schemas (template source only).
- **B. `unai-project-documentation-automation`** *(this repo)* — Python engine, reusable GitHub workflows, AI prompts, JSON schemas, central project registry (`config/project_registry.json`), PostgreSQL persistent audit integration, GitHub PR orchestration, Drive sync, aggregation, rollback, and tests.
- **C. Individual Project Repositories** — Independent copies of all 16 DOCX documents under `documentation/Project Documentation/` (including project `Master Record.docx`), canonical JSON reports under `documentation/reports/final/<feature-id>.json` (+ optional `.md` companion), `documentation/project-config.json`, `documentation/template-manifest.json`, and `.github/workflows/utda.yml`.
- **D. `unai-company-master-record`** — Authoritative company-wide `Master Record.docx` (`COMPANY_MASTER`), aggregation manifests, and restricted write permissions.

## Event & Correlation Behavior
- **Triggers:** `pull_request` (`closed` + `merged == true`), `push` to `main` under `documentation/reports/final/*.json`, and `workflow_dispatch`.
- **Event Correlation:** Correlates PR merges and final JSON reports by `project_id` and `feature_id` in either order (report first or PR merge first), persisting `WAITING_FOR_FINAL_REPORT` in the dedicated UTDA PostgreSQL database until all required evidence is available.
- **Idempotency:** Durable PostgreSQL constraints keyed on repository + `feature_id` + report blob SHA + target document ID prevent duplicate processing.

## Security
- Use a scoped GitHub App for cross-repository/organization automation (never employee PATs).
- Use GCP OIDC Workload Identity Federation or a narrowly scoped service account credential for Google Drive folder sync.
- Never log secrets, tokens, or sensitive document payloads. Keep `UTDA_DRY_RUN=true` until Phase 7 acceptance tests pass.

## Architecture & Planning Docs
- [ARCHITECTURE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/ARCHITECTURE.md)
- [DATA_FLOW.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/DATA_FLOW.md)
- [PROJECT_PROVISIONING_DESIGN.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/PROJECT_PROVISIONING_DESIGN.md)
- [SECURITY_AND_ACCESS_CONTROL.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/SECURITY_AND_ACCESS_CONTROL.md)
- [IMPLEMENTATION_PLAN.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/IMPLEMENTATION_PLAN.md)
- [PHASE_ACCEPTANCE_CRITERIA.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/PHASE_ACCEPTANCE_CRITERIA.md)
- [CONFIGURATION_GUIDE.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/CONFIGURATION_GUIDE.md)
- [IMPLEMENTATION_STATUS.md](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/IMPLEMENTATION_STATUS.md)

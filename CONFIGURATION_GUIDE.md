# UTDA Configuration Guide (`GITHUB_AUTHORITY` + `BOTH` Master Records)

## 1. Required External Credentials & Secrets

| Credential / Secret | Stored In | Scope / Purpose | Notes |
| --- | --- | --- | --- |
| `GITHUB_APP_ID` & `GITHUB_APP_PRIVATE_KEY` | Repo B Environment Secrets | Organization-scoped GitHub App (short-lived installation tokens) | Required for provisioning, Repo C doc PRs, Repo D `COMPANY_MASTER` PRs, and registry updates. Never use employee PATs |
| `UTDA_DATABASE_URL` | Repo B Environment Secrets | Dedicated UTDA PostgreSQL connection URI | Separate DB (or dedicated Supabase project); never reuse app DB credentials |
| `OPENROUTER_API_KEY` | Repo B Environment Secrets | Dedicated key with spend cap | Used for `impact_analyzer.py` and `ai_generator.py` |
| `OPENROUTER_MODEL` | Repo B Environment Variables | Pinned JSON-capable model slug | Configured per environment |
| **GCP OIDC** *(Preferred)* or `GOOGLE_SERVICE_ACCOUNT_JSON` | Repo B Environment Secrets | Google Drive API scoped to project folders & backup folder | Used strictly for post-merge GitHub-to-Drive sync & provisioning |
| `GOOGLE_DRIVE_SHARED_DRIVE_ID` | Repo B Environment Variables | Shared Drive ID hosting project folders | Passed with `supportsAllDrives=True` |
| `UTDA_BACKUP_FOLDER_ID` | Repo B Environment Variables | Restricted Drive backup folder ID | Stores pre-sync backup snapshots for rollback |

---

## 2. Environment Variables ([.env.example](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/.env.example))

- `UTDA_AUTHORITY_MODE=GITHUB_AUTHORITY` (fixed)
- `UTDA_MASTER_RECORD_SCOPE=BOTH` (fixed)
- `UTDA_DRY_RUN=true` (fail-safe default; keep `true` until Phase 7 sign-off)
- `UTDA_REGISTRY_PATH=config/project_registry.json`
- `UTDA_TEMPLATES_REPO=UNAI-TECH/unai-project-documentation`
- `UTDA_TEMPLATES_REF=<approved-tag>` (e.g., `templates-v1.0.0`)
- `UTDA_COMPANY_MASTER_REPO=UNAI-TECH/unai-company-master-record`
- `UTDA_DATABASE_URL=<postgresql-dsn>`
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`
- `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_DRIVE_SHARED_DRIVE_ID`, `UTDA_BACKUP_FOLDER_ID`
- `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`

---

## 3. Repository Configuration Matrix

1. **Repo A (`unai-project-documentation`):**
   - Contains 15 lifecycle `.docx` templates, `Master Record.docx`, `document_registry.json`, `impact_matrix.json`, `template_manifest.json`, `template_map.json`, and `CHANGELOG.md`.
   - Protected `main` branch (`CODEOWNERS` restricted to documentation admins).
2. **Repo B (`unai-project-documentation-automation`):**
   - Central `config/project_registry.json` validated against [schemas/project_registry_collection.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/project_registry_collection.schema.json) and [schemas/project_registry.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/project_registry.schema.json).
   - GitHub Environments: `utda-sandbox` and `utda-production` (with mandatory reviewers).
3. **Repo C (Each Application Repository):**
   - `documentation/Project Documentation/` (15 docs + project `Master Record.docx` = `MASTER`, original filenames preserved).
   - `documentation/reports/final/<feature-id>.json` (canonical JSON matching `schemas/feature_report.schema.json`) + optional `<feature-id>.md` companion.
   - `documentation/project-config.json` matching [schemas/project_config_local.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/project_config_local.schema.json).
   - `documentation/template-manifest.json` (template version + SHA-256 checksums).
   - `.github/workflows/utda.yml` copied from [examples/project-repository-caller.yml](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/examples/project-repository-caller.yml).
4. **Repo D (`unai-company-master-record`):**
   - Consolidated `Master Record.docx` (`COMPANY_MASTER`), aggregation metadata, source commit/version manifests, and restricted company-reviewer branch protections.

# UTDA Security and Access Control Architecture

## 1. Access Control Across the Four Repositories & External Systems

| System / Repo | Human Access | Automation Identity & Scope | Protection Controls |
| --- | --- | --- | --- |
| **Repo A (`unai-project-documentation`)** | Documentation Admins only | Read-only fetch by Repo B (`templates_ref`) | Protected `main`, `CODEOWNERS`, zero project automation write access |
| **Repo B (`unai-project-documentation-automation`)** | Platform / Automation Maintainers | GitHub App + Environment Secrets | Protected `main`, pinned release tags (`@v1`), Environment reviewer gates (`utda-production`) |
| **Repo C (Application Repos)** | Project Developers + Role Approvers (PM/PO/CTO) | Scoped GitHub App token (`Contents: R/W`, `Pull requests: R/W` for doc branches) | Protected `main`, role-aware PR approvals before merge; isolated per-project context |
| **Repo D (`unai-company-master-record`)** | Company-Level Reviewers only | Scoped GitHub App token (PR creation only) | Restricted write permissions; mandatory company-level human approval before merge |
| **PostgreSQL Audit DB** | Platform DB Admins (read-only audit views for reviewers) | Dedicated `utda` DB credential (never shared with app DBs) | TLS connections, least-privilege schema role, unique constraints & transactions |
| **Google Drive** | Project collaboration & review | GCP OIDC / Service Account scoped to project folders + backup folder | GitHub-to-Drive one-way sync after PR merge; drift detection prevents silent overwrites |

---

## 2. Authentication & Credential Rules

1. **GitHub App Over PATs:** Production cross-repo operations (PR creation, provisioning, `unai-company-master-record` PRs) must use a scoped **GitHub App** minting short-lived repo-scoped installation tokens—never employee personal access tokens.
2. **Dedicated Database Credentials:** Use a dedicated UTDA PostgreSQL database (or an isolated Supabase project dedicated solely to UTDA). **Never** reuse application database credentials.
3. **Least-Privilege Drive Access:** Prefer GitHub Actions OIDC -> GCP Workload Identity Federation; if using `GOOGLE_SERVICE_ACCOUNT_JSON`, restrict sharing to onboarded project folders and `UTDA_BACKUP_FOLDER_ID`.
4. **Secret Redaction:** `github_reader.py`, `audit_store.py`, and CI loggers must scrub tokens, private keys, API keys, and DB URLs before passing payloads to OpenRouter or writing logs/artifacts.

---

## 3. Project Isolation & Governance Locks

1. **No Cross-Project AI Leakage:** Feature processing for Project X loads only Project X's repo evidence, Project X's JSON report, and Project X's DOCX context—never another project's code, reports, or `MASTER` data.
2. **Preserved Locks (`config/impact_matrix.json` & `config/document_registry.json`):**
   - `never_auto_approve`: `D01`, `D12`, `D13`, `D14`, `MASTER`, `COMPANY_MASTER`
   - `always_human_fields`: `Prepared By`, `Reviewed By`, `Approved By`, `Name`, `Signature`, `Date of approval`, `GO / NO-GO DECISION`, `SIGN-OFF DECLARATION`, `UAT acceptance`
   - `master_record.sensitive_sections`: `3. Team Structure`, `4. Team Change Log (Swaps / Exits)`, `6.3 IT Asset Requirements`, `7. Financial Overview`
3. **Feature PR Merge Non-Implication:** Merging a code/feature PR never counts as UAT sign-off (`D12`), deployment authorization (`D13`), or Go-Live approval (`D14`).

---

## 4. Security & Reliability Risk Matrix

| Risk | Mitigation |
| --- | --- |
| **Out-of-band Drive edit overwriting GitHub truth** | `GITHUB_AUTHORITY` + `drive_sync.py` checksum/version comparison; flags `DRIFT_DETECTED` and blocks sync until explicit reconciliation |
| **Prompt injection in PRs/commits/reports** | Untrusted inputs wrapped as quoted JSON; strict `jsonschema` validation; deterministic Python policy & role gates enforce final PR/merge rules |
| **Duplicate processing / race conditions** | PostgreSQL unique constraints on `(project_id, feature_id, report_blob_sha, document_id)` and `company_aggregation_runs` idempotency keys + GitHub Actions `concurrency` |
| **Drive sync failure corrupting GitHub state** | Drive sync runs *post-merge*; failures log `SYNC_FAILED` and retry safely without rolling back the merged GitHub PR |
| **Unapproved `COMPANY_MASTER` updates** | Isolated `unai-company-master-record` repo with restricted merge permissions and mandatory company-level reviewer approval |
| **Accidental production publishing before testing** | `UTDA_DRY_RUN=true` default across `.env.example`, workflows, and `project-config.json` until Phase 7 acceptance tests pass |

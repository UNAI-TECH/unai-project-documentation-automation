# UTDA Data Flow Specification (`GITHUB_AUTHORITY` + `BOTH`)

## 1. Document Update & Post-Merge Drive Sync Flow

```mermaid
sequenceDiagram
    autonumber
    participant RepoC as Repo C (Application Repo)
    participant Engine as Repo B (UTDA Engine)
    participant DB as PostgreSQL Audit DB
    participant LLM as OpenRouter API
    participant Reviewers as Role-Aware GitHub Approvers
    participant Drive as Google Drive (Distribution)

    par Event Order Flexible
        RepoC->>Engine: pull_request (closed + merged)
    and
        RepoC->>Engine: push documentation/reports/final/<feature-id>.json
    end

    Engine->>DB: Persist event + check idempotency constraint
    alt Duplicate event/revision already completed
        DB-->>Engine: Unique constraint / completed state
        Engine-->>Engine: Exit SKIPPED_DUPLICATE
    else Correlation check (project_id + feature_id)
        alt Missing PR merge OR missing valid JSON report
            Engine->>DB: Store WAITING_FOR_FINAL_REPORT / AWAIT_PR_MERGE
            Engine-->>Engine: Exit 0 (Wait for paired event)
        else Paired evidence + JSON report valid
            Engine->>Engine: Validate JSON vs schemas/feature_report.schema.json
            Engine->>LLM: Impact analysis (scrubbed evidence + registry + impact_matrix)
            LLM-->>Engine: JSON matching schemas/document_update.schema.json
            Engine->>Engine: Enforce deterministic policy + role requirements
            Engine->>RepoC: Read current DOCX from default branch
            Engine->>LLM: Section proposal (schemas/document_section_output.schema.json)
            Engine->>Engine: Patch DOCX + validate structure & always_human_fields
            Engine->>RepoC: Create doc branch & open PR (with evidence + approval metadata)
            Engine->>DB: Record PR, branch, proposal & pending approvals
            Reviewers->>RepoC: Role-aware GitHub approval + merge PR
            RepoC->>Engine: Documentation PR merged event
            Engine->>DB: Record merged commit SHA, DOCX SHA-256 & approval_record
            Engine->>Drive: Check Drive checksum/version vs last synced PostgreSQL record
            alt Out-of-band Drive modification detected
                Engine->>DB: Flag DRIFT_DETECTED (halt overwrite, require reconciliation)
            else Clean sync target
                Engine->>Drive: Backup prior Drive copy + files().update on SAME file_id
                Engine->>Drive: Verify uploaded checksum
                Engine->>DB: Record SYNCED (GitHub SHA <-> Drive file_id + revision)
            end
        end
    end
```

---

## 2. Project-to-Company Master Record Aggregation Flow

```mermaid
sequenceDiagram
    autonumber
    participant RepoC as Repo C (MASTER Merged)
    participant DB as PostgreSQL Audit DB
    participant Agg as Repo B (master_aggregator.py)
    participant RepoD as Repo D (unai-company-master-record)
    participant CompRev as Company-Level Approver
    participant Drive as Drive (COMPANY_MASTER file_id)

    RepoC->>DB: Project MASTER PR merged (commit SHA + version + evidence)
    DB-->>Agg: Query eligible unaggregated MASTER versions
    Agg->>Agg: Validate project_id, source_commit_sha, evidence_refs & idempotency_key
    Agg->>RepoD: Read current COMPANY_MASTER (Master Record.docx)
    Agg->>Agg: Generate incremental update (preserving history & provenance; no duplicates)
    Agg->>Agg: Validate against schemas/master_aggregation.schema.json & DOCX validator
    Agg->>RepoD: Open PR in unai-company-master-record
    CompRev->>RepoD: Company-level review, approval & merge
    RepoD->>DB: Record COMPANY_MASTER merged SHA, approval_record & aggregation provenance
    Agg->>Drive: Sync merged COMPANY_MASTER to Drive (checksum verified)
    Agg->>DB: Record Drive sync status
```

---

## 3. Canonical Report & Idempotency Contracts

### 3.1 Canonical Feature Report (`Repo C`)
- **Path:** `documentation/reports/final/<feature-id>.json`
- **Companion (optional):** `documentation/reports/final/<feature-id>.md`
- **Schema:** [schemas/feature_report.schema.json](file:///c:/Users/kamal/OneDrive/Desktop/utda_starter/schemas/feature_report.schema.json) (`additionalProperties: false`)
- **Consistency:** `feature_id` must match across PR metadata, filename `<feature-id>.json`, JSON payload, and PostgreSQL audit records.

### 3.2 Deterministic `change_type` Bridge (`engine/contracts.py`)
`CHANGE_TYPE_TO_IMPACT_MATRIX` maps all 11 `feature_report.schema.json` `change_type` values to `config/impact_matrix.json` rules without altering the preserved rule definitions:
- `new_feature`, `feature_update`, `bug_fix` -> `functional_behavior`
- `refactor`, `security_change`, `api_change` -> `api_or_backend` (with `security_change` escalated for review)
- `database_change` -> `database_schema`
- `ui_change` -> `ui_ux`
- `deployment` -> `deployment`
- `test_execution` -> `test_execution`
- `other` -> `()` (evaluated strictly from explicit evidence arrays)

### 3.3 PostgreSQL Audit & Idempotency Tables (Logical Entities)
1. `projects` (project ID, Repo C owner/repo, template version, Drive folder ID)
2. `template_releases` & `project_provisioning_runs` (checkpointed provisioning state, file checksums)
3. `automation_events` & `feature_states` (`project_id`, `feature_id`, PR merge state, JSON report blob SHA, correlation state)
4. `document_proposals` & `github_pull_requests` (branch, PR number, merged commit SHA, pre/post DOCX SHA-256)
5. `approval_records` (conforming to `schemas/approval_record.schema.json`)
6. `master_record_versions` (`MASTER` merged SHAs & versions) & `company_aggregation_runs` (`COMPANY_MASTER` provenance & idempotency constraints)
7. `drive_file_mappings` & `drive_sync_revisions` (GitHub SHA ↔ Drive `file_id`, revision, checksum, sync status, `DRIFT_DETECTED` flags)
8. `rollback_operations` (target revision, restored SHA, operator, timestamp)

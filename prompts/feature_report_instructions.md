# Antigravity final implementation report requirements
Create canonical JSON at `documentation/reports/final/<feature-id>.json` only after implementation work is complete, strictly matching `schemas/feature_report.schema.json` (`additionalProperties: false`).
An optional human-readable companion Markdown summary may be generated at `documentation/reports/final/<feature-id>.md`, but automation reads and validates only the canonical `.json` report.
Required JSON fields:
- `project_id` (matching `documentation/project-config.json` and central registry)
- `feature_id` (consistent across PR metadata, filename `<feature-id>.json`, report JSON, and audit records)
- `feature_name`
- `change_type` (one of: `new_feature`, `feature_update`, `bug_fix`, `refactor`, `security_change`, `database_change`, `api_change`, `ui_change`, `deployment`, `test_execution`, `other`)
- `summary`
- `implementation_status` (`implemented` | `partially_implemented` | `proposed` | `reverted`)
- `evidence` (array of `{ kind, reference, description }` including commit SHAs, PR numbers, file paths, and command/CI outputs)
Optional detail fields: `business_context`, `modules`, `functional_changes`, `technical_changes`, `database_changes`, `api_changes`, `ui_changes`, `security_changes`, `tests` (`name`, `result`, `evidence`), `deployment_status`, `known_issues`, `files_modified`, `unknowns`.
Rules:
- Do not state `tests[].result == "passed"` unless execution `evidence` exists.
- Do not state `deployment_status` is `staging` or `production` unless deployment evidence exists in `evidence`.
- Do not include secrets, tokens, personal data, or production credentials.
- Separate implemented work from planned/future work.

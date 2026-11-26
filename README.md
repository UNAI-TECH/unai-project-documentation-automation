# UNAI Tech Documentation Automation (UTDA) — Starter

This is a starter scaffold, not a production-ready integration. Configure project_registry.json and Google Drive file IDs before enabling writes.

## Event behavior
- PR closed + merged: request processing only if the final report is present and validated.
- Push to main under documentation/reports/final/: process the report.
- Both triggers share an idempotency key based on repository + feature_id + report blob SHA + target document ID. Duplicate triggers must not create duplicate revisions.
- If PR merge happens before report push, mark WAITING_FOR_FINAL_REPORT and let the report push trigger resume. Do not publish incomplete content.

## Security
Use GitHub Actions OIDC where supported for cloud secrets; otherwise store a narrowly scoped service-account credential as an encrypted repository/environment secret. Share only each project folder with the service account. Never commit credentials. Start in dry-run mode.

## Drive update strategy
Store Google Drive file IDs in the registry. Download the existing DOCX, patch only named sections/table rows, validate, upload as a new Drive revision to the SAME file ID using media upload/update. Before upload, retain a timestamped backup in a restricted revision folder or rely on Drive revision retention plus an explicit backup copy. Do not create a new file on every run.

## Required next steps
1. Inspect all actual DOCX tables/styles in Word and map stable section/table anchors.
2. Populate project_registry.json with real repo names and Drive IDs.
3. Configure OpenRouter and Google service-account secrets.
4. Implement the engine modules described in engine/README.md.
5. Test against copied templates and a sandbox Drive folder before production.

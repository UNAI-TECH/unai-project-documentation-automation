# Engine implementation modules
Implement these modules in order: trigger.py (event normalization/idempotency); github_reader.py (report/commit/PR evidence); report_parser.py (schema validation); impact_analyzer.py (LLM call + deterministic policy); ai_generator.py (section proposal); document_updater.py (surgical DOCX edits); document_validator.py (required sections/approval locks); drive_manager.py (download/update media upload/backup); audit_store.py (event and revision ledger).
All modules must support UTDA_DRY_RUN=true. Never log credentials or full sensitive documents.

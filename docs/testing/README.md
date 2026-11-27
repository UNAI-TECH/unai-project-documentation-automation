# UTDA Testing, Verification & Reporting Index

This directory maintains the mandatory Markdown verification reports for implementation, security hardening, migration, configuration, and test changes in `unai-project-documentation-automation`.

## Reporting Policy Summary

1. **Testing is mandatory** for every code, migration, configuration, or test modification (unit, integration, regression, static/syntax checks where configured, and PostgreSQL 16 verification for database changes).
2. **Never claim a check passed without executing it** and observing its output; clearly distinguish local simulated runs, GitHub Actions PostgreSQL 16 CI runs, and skipped/unexecuted external checks.
3. **Never include secrets** (passwords, DSNs, tokens, API keys, private keys) in reports.
4. **Preserve historical reports**; add a new dated report under `docs/testing/` containing the task name and short commit SHA when available, committed alongside or immediately referencing the implementation.

---

## Report Index

| Date | Task | Commit SHA | Assessment | Report |
| --- | --- | --- | --- | --- |
| 2026-11-27 | Phase 1 — Database Concurrency & Idempotency Hardening (`0003_idempotency_fencing`) | `0f1a82c` | **PASS WITH LIMITATIONS** | [2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md](2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md) |
| 2026-11-27 | PostgreSQL 16 Security & Migration Verification Checklist | `f034f4e` (`7d5261c`) | **PASS WITH LIMITATIONS** | [POSTGRESQL_SECURITY_TEST_CHECKLIST.md](../../POSTGRESQL_SECURITY_TEST_CHECKLIST.md) |
| 2026-11-27 | Phase 1 Test Report — Initial Testing Without Supabase | — | **PASS WITH LIMITATIONS** | [PHASE1_TEST_REPORT.md](../../PHASE1_TEST_REPORT.md) |

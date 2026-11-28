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
| 2026-11-28 | Phase 2B.4.1 — Worker Lease Heartbeat & Renewal | `a6de001` | **PASS WITH LIMITATIONS** | [2026-11-28-phase2b-4-1-lease-heartbeat-a6de001.md](2026-11-28-phase2b-4-1-lease-heartbeat-a6de001.md) |
| 2026-11-28 | Phase 2B.3 — RLS Architecture, Migration `0005` & PostgreSQL 16 Verification | `6712928` | **PASS WITH LIMITATIONS** | [2026-11-28-phase2b-3-rls-pg16-verification-6712928.md](2026-11-28-phase2b-3-rls-pg16-verification-6712928.md) |
| 2026-11-28 | Phase 2B.2 — PostgreSQL 16 Verification & Project-Scoping Closure | `7e1dc5c` | **VERIFIED** | [2026-11-28-phase2b-2-pg16-verification-closure-7e1dc5c.md](2026-11-28-phase2b-2-pg16-verification-closure-7e1dc5c.md) |
| 2026-11-28 | Phase 2B.1–2B.2 — Authoritative Project Identity & Transaction-Local DB Context | `7e1dc5c` | **PASS WITH LIMITATIONS** | [2026-11-28-phase2b-1-2-project-identity-db-context.md](2026-11-28-phase2b-1-2-project-identity-db-context.md) |
| 2026-11-28 | Phase 2A — Security, Project Isolation & Worker Execution Architecture Review | `b6bc9a3` *(baseline)* | **READY FOR REVIEW (`PASS WITH PREREQUISITES`)** | [2026-11-28-phase2a-architecture-review.md](2026-11-28-phase2a-architecture-review.md) |
| 2026-11-28 | Phase 1 — Migration History Preservation (`0003` Restored + `0004`) & Final Closure | `c0deb74` | **PASS WITH LIMITATIONS** | [2026-11-28-phase1-migration-history-closure-c0deb74.md](2026-11-28-phase1-migration-history-closure-c0deb74.md) |
| 2026-11-28 | Phase 1 — Final Verification, Fence-Version, Lease Renewal & CI Maintenance | `1576d19` (`d8e67c3`) | **PASS WITH LIMITATIONS** | [2026-11-28-phase1-verification-reconciliation-1576d19.md](2026-11-28-phase1-verification-reconciliation-1576d19.md) |
| 2026-11-27 | Phase 1 — Database Concurrency & Idempotency Hardening (`0003_idempotency_fencing`) | `0f1a82c` | **PASS WITH LIMITATIONS** | [2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md](2026-11-27-phase1-database-concurrency-idempotency-0f1a82c.md) |
| 2026-11-27 | PostgreSQL 16 Security & Migration Verification Checklist | `f034f4e` (`7d5261c`) | **PASS WITH LIMITATIONS** | [POSTGRESQL_SECURITY_TEST_CHECKLIST.md](../../POSTGRESQL_SECURITY_TEST_CHECKLIST.md) |
| 2026-11-27 | Phase 1 Test Report — Initial Testing Without Supabase | — | **PASS WITH LIMITATIONS** | [PHASE1_TEST_REPORT.md](../../PHASE1_TEST_REPORT.md) |

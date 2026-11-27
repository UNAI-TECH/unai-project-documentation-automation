"""Phase 1 end-to-end event lifecycle, correlation, duplicate, and retry/recovery tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from engine.audit_store import AuditStore, make_idempotency_key
from engine.config_loader import is_dry_run
from engine.database import DatabaseManager, UTDADatabaseRepository
from engine.db_config import DatabaseConfig
from engine.db_health import verify_live_supabase_readonly
from engine.github_reader import scrub_secrets
from engine.migrations.runner import apply_migrations
from engine.trigger import normalize_event, process_event
from tests.test_database_phase1 import _FakeDBState, _FakePool

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "engine" / "migrations"


def _init_temp_git_repo(base: Path) -> Path:
    repo = base / "app_repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "ci@unai.tech"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "UTDA CI"],
        check=True,
        capture_output=True,
    )
    (repo / "documentation" / "reports" / "final").mkdir(parents=True, exist_ok=True)
    (repo / "documentation" / "project-config.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "project_id": "UTDA_PILOT",
                "project_name": "Pilot App",
                "authority_mode": "GITHUB_AUTHORITY",
                "master_record_id": "MASTER",
                "docs_dir": "documentation/Project Documentation",
                "template_source": {
                    "repository": "UNAI-TECH/unai-project-documentation",
                    "version": "templates-v1.0.0",
                    "manifest_path": "documentation/template-manifest.json",
                },
                "reports": {
                    "final_dir": "documentation/reports/final",
                    "canonical_format": "json",
                    "companion_markdown": True,
                },
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return repo


def _init_temp_utda_dir(base: Path) -> Path:
    utda = base / "utda_engine"
    (utda / "config").mkdir(parents=True, exist_ok=True)
    (utda / "schemas").mkdir(parents=True, exist_ok=True)
    for schema_file in (ROOT / "schemas").glob("*.json"):
        (utda / "schemas" / schema_file.name).write_text(
            schema_file.read_text(encoding="utf-8"), encoding="utf-8"
        )
    (utda / "config" / "project_registry.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "authority_mode": "GITHUB_AUTHORITY",
                "master_record_scope": "BOTH",
                "projects": [
                    {
                        "project_id": "UTDA_PILOT",
                        "project_name": "Pilot App",
                        "status": "active",
                        "github": {
                            "owner": "UNAI-TECH",
                            "repo": "pilot-app",
                            "default_branch": "main",
                        },
                        "drive": {
                            "folder_id": "drv-folder-1",
                            "files": {"MASTER": "drv-master-1"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return utda


def _commit_json_report(repo: Path, feature_id: str, payload: dict) -> str:
    report_path = repo / "documentation" / "reports" / "final" / f"{feature_id}.json"
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"add report {feature_id}"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Phase1EventPipelineTests(unittest.TestCase):
    def _cfg(self) -> DatabaseConfig:
        return DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
            }
        )

    def test_dry_run_default_and_secret_scrubbing(self) -> None:
        self.assertTrue(is_dry_run({}))
        self.assertTrue(is_dry_run({"UTDA_DRY_RUN": "true"}))
        self.assertTrue(is_dry_run({"UTDA_DRY_RUN": "invalid"}))
        self.assertFalse(is_dry_run({"UTDA_DRY_RUN": "false"}))

        raw = "token=ghp_123456789012345678901234567890 key=sk-abcdef12345678901234 dsn=postgresql://u:secretpw@host:5432/db"
        scrubbed = scrub_secrets(raw)
        self.assertNotIn("ghp_123456789012345678901234567890", scrubbed)
        self.assertNotIn("sk-abcdef12345678901234", scrubbed)
        self.assertNotIn("secretpw", scrubbed)

    def test_pr_first_then_report_push_correlation_and_duplicate_skip(self) -> None:
        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _init_temp_git_repo(base)
            utda = _init_temp_utda_dir(base)
            artifacts = base / "artifacts"

            with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
                audit = AuditStore(repo=UTDADatabaseRepository(db), artifacts_dir=artifacts)

                # Unmerged closed PR is ignored
                unmerged = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "pull_request",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": "HEAD",
                        "PR_NUMBER": "41",
                        "UTDA_PR_MERGED": "false",
                        "FEATURE_ID": "FEAT-101",
                    }
                )
                self.assertEqual(
                    process_event(unmerged, repo_dir=repo, utda_dir=utda, audit=audit)["status"],
                    "IGNORED_UNMERGED_PR",
                )

                # Step 1 (PR-first): Merged PR without report -> WAITING_FOR_FINAL_REPORT
                pr_event = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "pull_request",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": "HEAD",
                        "PR_NUMBER": "42",
                        "UTDA_PR_MERGED": "true",
                        "FEATURE_ID": "FEAT-101",
                        "UTDA_DELIVERY_ID": "deliv-pr-42",
                    }
                )
                res_wait = process_event(pr_event, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res_wait["status"], "WAITING_FOR_FINAL_REPORT")

                # Step 2: Report pushed -> resumes and completes DRY_RUN_READY
                commit_sha = _commit_json_report(
                    repo,
                    "FEAT-101",
                    {
                        "project_id": "UTDA_PILOT",
                        "feature_id": "FEAT-101",
                        "feature_name": "Audit Pipeline",
                        "change_type": "api_change",
                        "summary": "Implemented Phase 1 correlation.",
                        "implementation_status": "implemented",
                        "tests": [
                            {"name": "unit", "result": "passed", "evidence": "pytest:16-passed"}
                        ],
                        "evidence": [
                            {"kind": "commit", "reference": "pr:42", "description": "Merged PR #42"}
                        ],
                    },
                )
                push_event = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "push",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": commit_sha,
                        "FEATURE_ID": "FEAT-101",
                        "UTDA_DELIVERY_ID": "deliv-push-101",
                    }
                )
                res_ready = process_event(push_event, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res_ready["status"], "DRY_RUN_READY")
                self.assertTrue(res_ready["dry_run"])

                # Duplicate delivery ID -> SKIPPED_DUPLICATE_RECEIPT
                dup_receipt = process_event(push_event, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(dup_receipt["status"], "SKIPPED_DUPLICATE_RECEIPT")

                # New delivery ID with same report blob -> SKIPPED_DUPLICATE by idempotency key
                dup_idem_event = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "push",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": commit_sha,
                        "FEATURE_ID": "FEAT-101",
                        "UTDA_DELIVERY_ID": "deliv-push-101-retry",
                    }
                )
                dup_idem = process_event(dup_idem_event, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(dup_idem["status"], "SKIPPED_DUPLICATE")
                self.assertTrue((artifacts / "audit_record.json").is_file())

    def test_report_first_then_pr_merge_and_invalid_evidence_guardrails(self) -> None:
        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _init_temp_git_repo(base)
            utda = _init_temp_utda_dir(base)

            with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
                audit = AuditStore(repo=UTDADatabaseRepository(db))

                # Report claiming passed test without evidence -> VALIDATION_FAILED
                bad_sha = _commit_json_report(
                    repo,
                    "FEAT-BAD",
                    {
                        "project_id": "UTDA_PILOT",
                        "feature_id": "FEAT-BAD",
                        "feature_name": "Bad Report",
                        "change_type": "other",
                        "summary": "Missing test evidence.",
                        "implementation_status": "implemented",
                        "tests": [{"name": "smoke", "result": "passed"}],
                        "evidence": [{"kind": "commit", "reference": "abc"}],
                    },
                )
                bad_ev = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "push",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": bad_sha,
                        "FEATURE_ID": "FEAT-BAD",
                        "UTDA_DELIVERY_ID": "deliv-bad",
                    }
                )
                self.assertEqual(
                    process_event(bad_ev, repo_dir=repo, utda_dir=utda, audit=audit)["status"],
                    "VALIDATION_FAILED",
                )

                # Valid Report-first -> AWAIT_PR_MERGE -> PR merge -> DRY_RUN_READY
                good_sha = _commit_json_report(
                    repo,
                    "FEAT-202",
                    {
                        "project_id": "UTDA_PILOT",
                        "feature_id": "FEAT-202",
                        "feature_name": "Report First",
                        "change_type": "new_feature",
                        "summary": "Report pushed prior to merge.",
                        "implementation_status": "implemented",
                        "evidence": [{"kind": "commit", "reference": "c1"}],
                    },
                )
                push_first = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "push",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": good_sha,
                        "FEATURE_ID": "FEAT-202",
                        "UTDA_DELIVERY_ID": "deliv-rfirst-1",
                    }
                )
                res_await = process_event(push_first, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res_await["status"], "AWAIT_PR_MERGE")

                pr_second = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "pull_request",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": good_sha,
                        "PR_NUMBER": "99",
                        "UTDA_PR_MERGED": "true",
                        "FEATURE_ID": "FEAT-202",
                        "UTDA_DELIVERY_ID": "deliv-rfirst-2",
                    }
                )
                res_done = process_event(pr_second, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res_done["status"], "DRY_RUN_READY")

    def test_retry_recovery_after_failed_attempt_and_pending_supabase_probe(self) -> None:
        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)

        with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
            audit = AuditStore(repo=UTDADatabaseRepository(db))
            idem = make_idempotency_key("UNAI-TECH/pilot-app", "FEAT-303", "blob303", "D06")

            claimed1, att1 = audit.claim_processing_lock(
                idempotency_key=idem,
                scope="phase1_correlation",
                project_id="UTDA_PILOT",
                feature_id="FEAT-303",
                report_blob_sha="blob303",
                target_document_id="D06",
                event_receipt_id=None,
            )
            self.assertTrue(claimed1)
            self.assertEqual(att1, 1)

            # Simulate transient failure marking key FAILED
            audit.record_attempt_outcome(
                event_receipt_id=None,
                idempotency_key=idem,
                attempt_number=att1,
                succeeded=False,
                final_key_status="FAILED",
                retry_state="RETRY_SCHEDULED",
                error_code="TRANSIENT_DB",
                error_summary="connection reset",
            )

            # Recovery re-acquires lock and increments attempt_number to 2
            claimed2, att2 = audit.claim_processing_lock(
                idempotency_key=idem,
                scope="phase1_correlation",
                project_id="UTDA_PILOT",
                feature_id="FEAT-303",
                report_blob_sha="blob303",
                target_document_id="D06",
                event_receipt_id=None,
            )
            self.assertTrue(claimed2)
            self.assertEqual(att2, 2)

        # Read-only Supabase verification returns pending when env unset
        prev = os.environ.pop("UTDA_DATABASE_URL", None)
        try:
            probe = verify_live_supabase_readonly()
            self.assertEqual(probe["status"], "pending")
            self.assertFalse(probe["verified"])
        finally:
            if prev is not None:
                os.environ["UTDA_DATABASE_URL"] = prev

    @unittest.skipUnless(
        os.getenv("UTDA_TEST_DATABASE_URL") and os.getenv("UTDA_TEST_MIGRATOR_URL"),
        "Local PostgreSQL UTDA_TEST_DATABASE_URL / UTDA_TEST_MIGRATOR_URL not set (runs in ci-database.yml)",
    )
    def test_real_postgres_full_event_correlation_and_recovery(self) -> None:
        cfg = DatabaseConfig.from_migration_env(
            {
                **os.environ,
                "UTDA_DATABASE_URL": os.environ["UTDA_TEST_DATABASE_URL"],
                "UTDA_MIGRATION_DATABASE_URL": os.environ["UTDA_TEST_MIGRATOR_URL"],
            }
        )
        apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _init_temp_git_repo(base)
            utda = _init_temp_utda_dir(base)

            with DatabaseManager(cfg) as db:
                audit = AuditStore(
                    repo=UTDADatabaseRepository(db), artifacts_dir=base / "artifacts"
                )
                pr_ev = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "pull_request",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": "HEAD",
                        "PR_NUMBER": "501",
                        "UTDA_PR_MERGED": "true",
                        "FEATURE_ID": "FEAT-PG-501",
                        "UTDA_DELIVERY_ID": "pg-pr-501",
                    }
                )
                self.assertEqual(
                    process_event(pr_ev, repo_dir=repo, utda_dir=utda, audit=audit)["status"],
                    "WAITING_FOR_FINAL_REPORT",
                )

                sha = _commit_json_report(
                    repo,
                    "FEAT-PG-501",
                    {
                        "project_id": "UTDA_PILOT",
                        "feature_id": "FEAT-PG-501",
                        "feature_name": "PG16 Lifecycle",
                        "change_type": "database_change",
                        "summary": "Verified full PG16 lifecycle.",
                        "implementation_status": "implemented",
                        "evidence": [{"kind": "commit", "reference": "pg16-ci"}],
                    },
                )
                push_ev = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "push",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": sha,
                        "FEATURE_ID": "FEAT-PG-501",
                        "UTDA_DELIVERY_ID": "pg-push-501",
                    }
                )
                done = process_event(push_ev, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(done["status"], "DRY_RUN_READY")
                with db.transaction() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(*) FROM utda.audit_events WHERE project_id = %s AND feature_id = %s",
                            ("UTDA_PILOT", "FEAT-PG-501"),
                        )
                        self.assertGreaterEqual(cur.fetchone()[0], 2)

                        # Verify expired stale IN_PROGRESS lock can be recovered
                        cur.execute(
                            """
                            UPDATE utda.idempotency_keys
                            SET status = 'IN_PROGRESS',
                                expires_at = TIMEZONE('utc', NOW()) - INTERVAL '5 minutes'
                            WHERE idempotency_key = %s
                            """,
                            (done["idempotency_key"],),
                        )
                recovered = audit.repo.acquire_idempotency_key(
                    idempotency_key=done["idempotency_key"],
                    scope="phase1_correlation",
                    project_id="UTDA_PILOT",
                    feature_id="FEAT-PG-501",
                    report_blob_sha="sha-pg",
                    target_document_id="PHASE1_CORRELATION",
                )
                self.assertTrue(recovered)

    def test_github_event_parsing_from_webhook_payload_and_branch_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "event.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "repository": {"full_name": "UNAI-TECH/pilot-app"},
                        "pull_request": {
                            "number": 77,
                            "merged": True,
                            "title": "Implement [FEAT-404] audit exporter",
                            "head": {"ref": "feat/FEAT-404-audit"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            ev = normalize_event(
                {
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_EVENT_PATH": str(payload_path),
                    "GITHUB_SHA": "deadbeef",
                }
            )
            self.assertEqual(ev.repository, "UNAI-TECH/pilot-app")
            self.assertEqual(ev.pr_number, 77)
            self.assertTrue(ev.pr_merged)
            self.assertEqual(ev.feature_id, "FEAT-404")
            self.assertTrue(ev.dry_run)

            disp_path = Path(tmp) / "dispatch.json"
            disp_path.write_text(
                json.dumps({"inputs": {"feature_id": "FEAT-505"}}), encoding="utf-8"
            )
            disp_ev = normalize_event(
                {
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                    "GITHUB_EVENT_PATH": str(disp_path),
                    "UTDA_DRY_RUN": "true",
                }
            )
            self.assertEqual(disp_ev.feature_id, "FEAT-505")
            self.assertFalse(disp_ev.pr_merged)
            self.assertTrue(disp_ev.dry_run)

    def test_report_validation_edge_cases_and_audit_secret_redaction(self) -> None:
        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _init_temp_git_repo(base)
            utda = _init_temp_utda_dir(base)
            artifacts = base / "artifacts"

            # Markdown-only report without canonical .json -> MISSING with hint
            md_only = repo / "documentation" / "reports" / "final" / "FEAT-MD.md"
            md_only.write_text("# Companion only\n", encoding="utf-8")

            with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
                audit = AuditStore(repo=UTDADatabaseRepository(db), artifacts_dir=artifacts)
                ev_md = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "pull_request",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": "HEAD",
                        "PR_NUMBER": "81",
                        "UTDA_PR_MERGED": "true",
                        "FEATURE_ID": "FEAT-MD",
                        "UTDA_DELIVERY_ID": "deliv-md-only",
                    }
                )
                res_md = process_event(ev_md, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res_md["status"], "WAITING_FOR_FINAL_REPORT")
                self.assertIn("Markdown companion", state.audit_events[-1][-1])

                # Production deployment claim without deployment evidence + synthetic secret in commit/summary
                bad_dep_sha = _commit_json_report(
                    repo,
                    "FEAT-DEPLOY-BAD",
                    {
                        "project_id": "UTDA_PILOT",
                        "feature_id": "FEAT-DEPLOY-BAD",
                        "feature_name": "Prod Claim",
                        "change_type": "deployment",
                        "summary": "Leak test sk-secretkey12345678901234",
                        "implementation_status": "implemented",
                        "deployment_status": "production",
                        "evidence": [{"kind": "commit", "reference": "ghp_123456789012345678901234567890"}],
                    },
                )
                ev_dep = normalize_event(
                    {
                        "GITHUB_EVENT_NAME": "push",
                        "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                        "GITHUB_SHA": bad_dep_sha,
                        "FEATURE_ID": "FEAT-DEPLOY-BAD",
                        "UTDA_DELIVERY_ID": "deliv-bad-dep",
                    }
                )
                res_dep = process_event(ev_dep, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res_dep["status"], "VALIDATION_FAILED")

                # Explicitly verify secret redaction in audit_record.json and audit_events
                audit.append_audit(
                    actor="ci",
                    event_type="REDACTION_CHECK",
                    project_id="UTDA_PILOT",
                    details={"raw": "postgresql://u:MySecretPw@host:5432/db sk-abcdefgh1234567890"},
                )
                audit.export_artifact(
                    "audit_record.json",
                    {"note": "ghp_123456789012345678901234567890 sk-abcdefgh1234567890 postgresql://u:MySecretPw@host:5432/db"},
                )
                artifact_text = (artifacts / "audit_record.json").read_text(encoding="utf-8")
                self.assertNotIn("ghp_123456789012345678901234567890", artifact_text)
                self.assertNotIn("MySecretPw", artifact_text)
                self.assertIn("postgresql://u:***@host:5432/db", artifact_text)
                self.assertNotIn("sk-abcdefgh1234567890", str(state.audit_events[-1]))

    def test_phase1_blocks_docx_drive_publishing_and_provisioning(self) -> None:
        # Confirm Phase 2-6 production modules do not exist and are never loaded
        prohibited_modules = (
            "engine.ai_generator",
            "engine.document_updater",
            "engine.pr_orchestrator",
            "engine.provisioner",
            "engine.master_aggregator",
            "engine.drive_sync",
        )
        for mod in prohibited_modules:
            self.assertNotIn(mod, sys.modules)
            rel_py = ROOT / (mod.replace(".", "/") + ".py")
            self.assertFalse(rel_py.exists(), f"Prohibited Phase 2+ module must not exist: {rel_py}")

        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _init_temp_git_repo(base)
            utda = _init_temp_utda_dir(base)

            docs_dir = repo / "documentation" / "Project Documentation"
            docs_dir.mkdir(parents=True, exist_ok=True)
            docx_file = docs_dir / "06_Technical_Specification.docx"
            master_file = docs_dir / "Master Record.docx"
            docx_file.write_bytes(b"ORIGINAL_DOCX_CONTENT_DO_NOT_TOUCH")
            master_file.write_bytes(b"ORIGINAL_MASTER_CONTENT_DO_NOT_TOUCH")
            before_docx = hashlib.sha256(docx_file.read_bytes()).hexdigest()
            before_master = hashlib.sha256(master_file.read_bytes()).hexdigest()
            before_reg = hashlib.sha256(
                (utda / "config" / "project_registry.json").read_bytes()
            ).hexdigest()

            sha = _commit_json_report(
                repo,
                "FEAT-SAFE",
                {
                    "project_id": "UTDA_PILOT",
                    "feature_id": "FEAT-SAFE",
                    "feature_name": "Safety Boundary",
                    "change_type": "api_change",
                    "summary": "Verify Phase 1 performs zero DOCX/Drive/PR writes.",
                    "implementation_status": "implemented",
                    "evidence": [{"kind": "commit", "reference": "safe-1"}],
                },
            )
            ev = normalize_event(
                {
                    "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REPOSITORY": "UNAI-TECH/pilot-app",
                    "GITHUB_SHA": sha,
                    "PR_NUMBER": "120",
                    "UTDA_PR_MERGED": "true",
                    "FEATURE_ID": "FEAT-SAFE",
                    "UTDA_DELIVERY_ID": "deliv-safe-120",
                    "UTDA_DRY_RUN": "true",
                }
            )
            with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
                audit = AuditStore(repo=UTDADatabaseRepository(db), artifacts_dir=base / "artifacts")
                res = process_event(ev, repo_dir=repo, utda_dir=utda, audit=audit)
                self.assertEqual(res["status"], "DRY_RUN_READY")
                self.assertTrue(res["dry_run"])

            self.assertEqual(hashlib.sha256(docx_file.read_bytes()).hexdigest(), before_docx)
            self.assertEqual(hashlib.sha256(master_file.read_bytes()).hexdigest(), before_master)
            self.assertEqual(
                hashlib.sha256((utda / "config" / "project_registry.json").read_bytes()).hexdigest(),
                before_reg,
            )
            for mod in prohibited_modules:
                self.assertNotIn(mod, sys.modules)


if __name__ == "__main__":
    unittest.main()

"""Phase 1 end-to-end event lifecycle, correlation, duplicate, and retry/recovery tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
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
        os.getenv("UTDA_TEST_DATABASE_URL"),
        "Local PostgreSQL UTDA_TEST_DATABASE_URL not set (runs in ci-database.yml)",
    )
    def test_real_postgres_full_event_correlation_and_recovery(self) -> None:
        cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_DATABASE_URL")
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


if __name__ == "__main__":
    unittest.main()

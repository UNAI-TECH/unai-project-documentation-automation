"""Automated Phase 1 tests for Supabase/PostgreSQL configuration, migrations,
connection lifecycle, idempotency, concurrency, health checks, and safe logging.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest

from engine.database import (
    TRANSIENT_ERRORS,
    DatabaseManager,
    DatabaseUnavailableError,
    UTDADatabaseRepository,
)
from engine.db_config import (
    DatabaseConfig,
    DatabaseConfigError,
    redact_dsn,
)
from engine.db_health import check_database_health
from engine.migrations.runner import (
    MigrationChecksumError,
    MigrationError,
    apply_migrations,
    discover_migrations,
)

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "engine" / "migrations"


class _FakeDBState:
    """Thread-safe in-memory state simulating PostgreSQL tables for unit tests."""

    def __init__(self, fail_sql_substring: str | None = None) -> None:
        self.lock = threading.Lock()
        self.tables_initialized = False
        self.migrations: dict[str, tuple[str, str]] = {}
        self.projects: dict[str, dict] = {}
        self.receipts_by_delivery: dict[str, str] = {}
        self.feature_states: dict[tuple[str, str], dict] = {}
        self.final_reports: set[tuple[str, str, str]] = set()
        self.idempotency_keys: dict[str, dict] = {}
        self.attempts: list[tuple] = []
        self.audit_events: list[tuple] = []
        self.fail_sql_substring = fail_sql_substring
        self.size_bytes = 12 * 1024 * 1024  # 12 MB


class _FakeCursor:
    def __init__(self, conn: "_FakeConnection") -> None:
        self.conn = conn
        self._rows: list[tuple] = []
        self._idx = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.conn.executed_sql.append(sql.strip())
        state = self.conn.state
        if state.fail_sql_substring and state.fail_sql_substring in sql:
            raise RuntimeError("Injected SQL failure")

        norm = " ".join(sql.split())
        self._rows = []
        self._idx = 0

        if norm.startswith("SET LOCAL statement_timeout"):
            return
        if "pg_advisory_lock" in norm or "pg_advisory_unlock" in norm:
            self._rows = [(None,)]
            return
        if norm == "SELECT 1":
            self._rows = [(1,)]
            return
        if "pg_database_size" in norm:
            self._rows = [(state.size_bytes,)]
            return
        if "FROM information_schema.tables" in norm:
            self._rows = [(state.tables_initialized,)]
            return
        if "SELECT version, checksum_sha256, status FROM" in norm:
            self._rows = [
                (v, chk, st) for v, (chk, st) in sorted(state.migrations.items())
            ]
            return
        if "SELECT version, COUNT(*) OVER ()" in norm:
            if state.migrations:
                latest = sorted(state.migrations.keys())[-1]
                self._rows = [(latest, len(state.migrations))]
            return
        if "CREATE TABLE IF NOT EXISTS" in norm and "schema_migrations" in norm:
            self.conn.pending_ops.append(("init_tables", None))
            return
        if "CREATE INDEX IF NOT EXISTS" in norm:
            return
        if "INSERT INTO" in norm and "schema_migrations" in norm:
            version, _name, checksum, _actor, _ms = params
            self.conn.pending_ops.append(("migration", (version, checksum)))
            return
        if "INSERT INTO" in norm and ".projects" in norm:
            self.conn.pending_ops.append(("project", params[0]))
            return
        if "INSERT INTO" in norm and ".github_event_receipts" in norm:
            receipt_id, delivery_id = params[0], params[1]
            with state.lock:
                if delivery_id in state.receipts_by_delivery:
                    self._rows = []
                else:
                    state.receipts_by_delivery[delivery_id] = receipt_id
                    self._rows = [(receipt_id,)]
            return
        if "INSERT INTO" in norm and ".feature_processing_states" in norm:
            state_id, project_id, feature_id, _init, pr_num, pr_sha, rep_sha = params[:7]
            with state.lock:
                existing = state.feature_states.get((project_id, feature_id), {})
                merged_pr = pr_sha or existing.get("pr_merged_sha")
                merged_rep = rep_sha or existing.get("report_blob_sha")
                status = (
                    "READY"
                    if (merged_pr and merged_rep)
                    else ("WAITING_FOR_FINAL_REPORT" if merged_pr else "AWAIT_PR_MERGE")
                )
                rec = {
                    "state_id": existing.get("state_id", state_id),
                    "status": status,
                    "pr_merged_sha": merged_pr,
                    "report_blob_sha": merged_rep,
                }
                state.feature_states[(project_id, feature_id)] = rec
                self._rows = [(rec["state_id"], rec["status"], merged_pr, merged_rep)]
            return
        if "INSERT INTO" in norm and ".final_reports" in norm:
            report_id, project_id, feature_id = params[0], params[1], params[2]
            blob_sha = params[7]
            with state.lock:
                key = (project_id, feature_id, blob_sha)
                if key in state.final_reports:
                    self._rows = []
                else:
                    state.final_reports.add(key)
                    self._rows = [(report_id,)]
            return
        if "INSERT INTO" in norm and ".idempotency_keys" in norm:
            idem_key = params[0]
            with state.lock:
                if idem_key in state.idempotency_keys:
                    self._rows = []
                else:
                    state.idempotency_keys[idem_key] = {"status": "IN_PROGRESS"}
                    self._rows = [(idem_key,)]
            return
        if "UPDATE" in norm and ".idempotency_keys" in norm:
            status, _summary, idem_key = params
            with state.lock:
                if idem_key in state.idempotency_keys:
                    state.idempotency_keys[idem_key]["status"] = status
            return
        if "INSERT INTO" in norm and ".processing_attempts" in norm:
            self.conn.pending_ops.append(("attempt", params))
            return
        if "INSERT INTO" in norm and ".audit_events" in norm:
            self.conn.pending_ops.append(("audit", params))
            return

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, state: _FakeDBState) -> None:
        self.state = state
        self.committed = 0
        self.rolled_back = 0
        self.pending_ops: list[tuple[str, object]] = []
        self.executed_sql: list[str] = []

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1
        with self.state.lock:
            for kind, payload in self.pending_ops:
                if kind == "init_tables":
                    self.state.tables_initialized = True
                elif kind == "migration":
                    version, checksum = payload  # type: ignore[misc]
                    self.state.migrations[version] = (checksum, "APPLIED")
                elif kind == "project":
                    self.state.projects[str(payload)] = {}
                elif kind == "attempt":
                    self.state.attempts.append(payload)  # type: ignore[arg-type]
                elif kind == "audit":
                    self.state.audit_events.append(payload)  # type: ignore[arg-type]
            self.pending_ops.clear()

    def rollback(self) -> None:
        self.rolled_back += 1
        self.pending_ops.clear()


class _FakePool:
    def __init__(self, state: _FakeDBState, fail_connect_times: int = 0) -> None:
        self.state = state
        self.fail_connect_times = fail_connect_times
        self.connect_calls = 0
        self.opened = False
        self.closed = False
        self.connections: list[_FakeConnection] = []

    def open(self, wait: bool = True, timeout: float = 10.0) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def connection(self, timeout: float = 10.0):
        pool = self

        class _Ctx:
            def __enter__(self_ctx):
                pool.connect_calls += 1
                if pool.connect_calls <= pool.fail_connect_times:
                    err_cls = TRANSIENT_ERRORS[0]
                    raise err_cls("Simulated transient connection failure")
                conn = _FakeConnection(pool.state)
                pool.connections.append(conn)
                return conn

            def __exit__(self_ctx, exc_type, exc, tb):
                return None

        return _Ctx()


class Phase1DatabaseConfigTests(unittest.TestCase):
    def test_missing_database_url_fails_safely(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env({})

    def test_rejects_supabase_http_url_and_api_keys(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env({"UTDA_DATABASE_URL": "https://xyz.supabase.co"})
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env({"UTDA_DATABASE_URL": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"})
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env({"UTDA_DATABASE_URL": "sb_secret_not_a_postgres_dsn"})

    def test_rejects_insecure_sslmode_for_remote_host(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env(
                {
                    "UTDA_DATABASE_URL": "postgresql://utda:secret@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                    "UTDA_DATABASE_SSLMODE": "disable",
                }
            )

    def test_valid_session_and_transaction_pooler_configs_and_redaction(self) -> None:
        session_cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda.ref:SuperSecretPw123@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
            }
        )
        self.assertEqual(session_cfg.pool_mode, "session")
        self.assertEqual(session_cfg.sslmode, "require")
        self.assertTrue(session_cfg.supports_migrations)
        self.assertNotIn("SuperSecretPw123", repr(session_cfg))
        self.assertNotIn("SuperSecretPw123", redact_dsn(session_cfg.dsn))

        tx_cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda.ref:SuperSecretPw123@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"
            }
        )
        self.assertEqual(tx_cfg.pool_mode, "transaction")
        self.assertFalse(tx_cfg.supports_migrations)
        self.assertIsNone(tx_cfg.connect_kwargs()["prepare_threshold"])


class Phase1DatabaseLifecycleAndPersistenceTests(unittest.TestCase):
    def _cfg(self) -> DatabaseConfig:
        return DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda_user:TopSecretPass99@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
                "UTDA_DATABASE_MAX_RETRIES": "2",
            }
        )

    def test_connection_recovery_and_failure(self) -> None:
        state = _FakeDBState()
        flaky_pool = _FakePool(state, fail_connect_times=2)
        with DatabaseManager(self._cfg(), pool_factory=lambda _c: flaky_pool, sleep_fn=lambda _s: None) as db:
            result = db.run_in_transaction(lambda conn: "ok")
            self.assertEqual(result, "ok")
            self.assertEqual(flaky_pool.connect_calls, 3)

        dead_pool = _FakePool(state, fail_connect_times=10)
        with DatabaseManager(self._cfg(), pool_factory=lambda _c: dead_pool, sleep_fn=lambda _s: None) as db:
            with self.assertRaises(DatabaseUnavailableError):
                db.run_in_transaction(lambda conn: "never")

    def test_transaction_commit_and_rollback(self) -> None:
        state = _FakeDBState()
        pool = _FakePool(state)
        with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
            with db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            self.assertEqual(pool.connections[-1].committed, 1)
            self.assertEqual(pool.connections[-1].rolled_back, 0)

            with self.assertRaises(RuntimeError):
                with db.transaction():
                    raise RuntimeError("boom")
            self.assertEqual(pool.connections[-1].rolled_back, 1)

    def test_migrations_empty_db_upgrade_checksum_and_rollback(self) -> None:
        state = _FakeDBState()
        cfg = self._cfg()
        discovered = discover_migrations(MIGRATIONS_DIR)
        self.assertEqual([m.version for m in discovered], ["0001", "0002"])

        # Plan on empty DB shows both pending
        plan = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True, connect_fn=lambda _c: _FakeConnection(state))
        self.assertEqual(plan["pending"], ["0001_initial_utda_schema", "0002_roles_and_indexes"])

        # Apply from empty DB
        res1 = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))
        self.assertEqual(res1["applied"], ["0001_initial_utda_schema", "0002_roles_and_indexes"])

        # Re-running is safe / no pending migrations
        res2 = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))
        self.assertEqual(res2["applied"], [])

        # Simulate upgrade with a 0003 migration in a temp directory + failure rollback
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for m in discovered:
                (tmp_dir / m.path.name).write_text(m.sql, encoding="utf-8")
            (tmp_dir / "0003_add_sample.sql").write_text("SELECT 1;", encoding="utf-8")
            res3 = apply_migrations(cfg, tmp_dir, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))
            self.assertEqual(res3["applied"], ["0003_add_sample"])

            # Altering already-applied 0001 raises checksum mismatch
            (tmp_dir / "0001_initial_utda_schema.sql").write_text("-- tampered", encoding="utf-8")
            with self.assertRaises(MigrationChecksumError):
                apply_migrations(cfg, tmp_dir, dry_run=True, connect_fn=lambda _c: _FakeConnection(state))

        # Transaction Pooler mode blocked for migrations
        tx_cfg = DatabaseConfig.from_env(
            {"UTDA_DATABASE_URL": "postgresql://utda:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres"}
        )
        with self.assertRaises(MigrationError):
            apply_migrations(tx_cfg, MIGRATIONS_DIR, dry_run=True, connect_fn=lambda _c: _FakeConnection(state))

        # Failing SQL rolls back atomically without recording migration
        broken_state = _FakeDBState(fail_sql_substring="CREATE INDEX")
        with self.assertRaises(MigrationError):
            apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(broken_state))
        self.assertNotIn("0002", broken_state.migrations)

    def test_event_receipt_correlation_idempotency_concurrency_and_audit(self) -> None:
        state = _FakeDBState()
        state.tables_initialized = True
        state.migrations["0001"] = ("chk1", "APPLIED")
        pool = _FakePool(state)

        with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
            repo = UTDADatabaseRepository(db)
            repo.upsert_project(
                project_id="UTDA_PILOT",
                project_name="Pilot App",
                status="active",
                github_owner="UNAI-TECH",
                github_repo="pilot",
                default_branch="main",
            )

            # Event receipt deduplication
            r1 = repo.record_event_receipt(
                delivery_id="deliv-100",
                event_name="pull_request",
                repository="UNAI-TECH/pilot",
                project_id="UTDA_PILOT",
                feature_id="FEAT-1",
                git_sha="abc1234",
                pr_number=10,
            )
            r2 = repo.record_event_receipt(
                delivery_id="deliv-100",
                event_name="pull_request",
                repository="UNAI-TECH/pilot",
                project_id="UTDA_PILOT",
                feature_id="FEAT-1",
                git_sha="abc1234",
                pr_number=10,
            )
            self.assertTrue(r1["inserted"])
            self.assertFalse(r2["inserted"])

            # Correlation: PR merge first -> WAITING_FOR_FINAL_REPORT -> report push -> READY
            s1 = repo.correlate_feature_state(
                project_id="UTDA_PILOT",
                feature_id="FEAT-1",
                pr_number=10,
                pr_merged_sha="abc1234",
            )
            self.assertEqual(s1["status"], "WAITING_FOR_FINAL_REPORT")
            s2 = repo.correlate_feature_state(
                project_id="UTDA_PILOT",
                feature_id="FEAT-1",
                report_blob_sha="blob999",
            )
            self.assertEqual(s2["status"], "READY")

            # Report metadata persistence
            rep = repo.record_final_report(
                project_id="UTDA_PILOT",
                feature_id="FEAT-1",
                feature_name="Auth",
                change_type="api_change",
                implementation_status="implemented",
                report_path="documentation/reports/final/FEAT-1.json",
                report_blob_sha="blob999",
                git_commit_sha="def5678",
                report_metadata={"summary": "Added auth"},
                evidence_refs=[{"kind": "commit", "reference": "def5678"}],
            )
            self.assertTrue(rep["inserted"])

            # Concurrent idempotency contention: exactly 1 of 6 workers wins
            def _claim(_idx: int) -> bool:
                return repo.acquire_idempotency_key(
                    idempotency_key="UNAI-TECH/pilot:FEAT-1:blob999:D06",
                    scope="document_update",
                    project_id="UTDA_PILOT",
                    feature_id="FEAT-1",
                    report_blob_sha="blob999",
                    target_document_id="D06",
                )

            with ThreadPoolExecutor(max_workers=6) as ex:
                winners = list(ex.map(_claim, range(6)))
            self.assertEqual(sum(1 for w in winners if w), 1)

            repo.complete_idempotency_key(
                idempotency_key="UNAI-TECH/pilot:FEAT-1:blob999:D06",
                status="COMPLETED",
                result_summary={"pr": 11},
            )
            repo.record_processing_attempt(
                event_receipt_id=r1["receipt_id"],
                idempotency_key="UNAI-TECH/pilot:FEAT-1:blob999:D06",
                attempt_number=1,
                status="SUCCEEDED",
            )
            repo.append_audit_event(
                actor="utda-bot",
                event_type="FEATURE_CORRELATED",
                project_id="UTDA_PILOT",
                feature_id="FEAT-1",
                document_id="D06",
                idempotency_key="UNAI-TECH/pilot:FEAT-1:blob999:D06",
                details={"note": "ready"},
            )
            self.assertEqual(len(state.attempts), 1)
            self.assertEqual(len(state.audit_events), 1)

            health = check_database_health(db)
            self.assertEqual(health["status"], "healthy")
            self.assertEqual(health["storage"]["free_plan_quota_mb"], 500)

    def test_logs_never_expose_database_credentials(self) -> None:
        secret_pw = "UltraSecretDbPass_98765"
        cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": f"postgresql://utda_user:{secret_pw}@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
                "UTDA_DATABASE_MAX_RETRIES": "1",
            }
        )
        state = _FakeDBState()
        flaky_pool = _FakePool(state, fail_connect_times=1)

        captured_records: list[str] = []

        class _MemHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_records.append(self.format(record))

        logger = logging.getLogger("utda.database")
        handler = _MemHandler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with DatabaseManager(cfg, pool_factory=lambda _c: flaky_pool, sleep_fn=lambda _s: None) as db:
                db.logger.warning("Connection failed for %s", cfg.dsn)
                db.run_in_transaction(lambda _conn: "ok")
        finally:
            logger.removeHandler(handler)

        joined = "\n".join(captured_records)
        self.assertNotIn(secret_pw, joined)
        self.assertIn("***", joined)


class Phase1LivePostgreSQLTests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("UTDA_TEST_DATABASE_URL"),
        "Local PostgreSQL UTDA_TEST_DATABASE_URL not set (runs in ci-database.yml)",
    )
    def test_real_local_postgres_migrations_and_persistence(self) -> None:
        cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_DATABASE_URL")
        apply_result = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False)
        self.assertIn(apply_result["mode"], ("apply",))

        with DatabaseManager(cfg) as db:
            health = check_database_health(db)
            self.assertIn(health["status"], ("healthy", "warning"))
            repo = UTDADatabaseRepository(db)
            repo.upsert_project(
                project_id="CI_LOCAL_TEST",
                project_name="CI Local Test",
                status="active",
                github_owner="UNAI-TECH",
                github_repo="ci-local-test",
                default_branch="main",
            )
            claimed = repo.acquire_idempotency_key(
                idempotency_key="ci-local-test:FEAT-CI:sha1:D06",
                scope="ci_test",
                project_id="CI_LOCAL_TEST",
                feature_id="FEAT-CI",
                report_blob_sha="sha1",
                target_document_id="D06",
            )
            self.assertIsInstance(claimed, bool)

    @unittest.skipUnless(
        os.getenv("UTDA_RUN_LIVE_SUPABASE_TESTS") == "true" and os.getenv("UTDA_DATABASE_URL"),
        "PENDING: Live Supabase verification requires UTDA_DATABASE_URL and UTDA_RUN_LIVE_SUPABASE_TESTS=true",
    )
    def test_live_supabase_readonly_health_check(self) -> None:
        cfg = DatabaseConfig.from_env(dsn_var="UTDA_DATABASE_URL")
        with DatabaseManager(cfg) as db:
            health = check_database_health(db)
            self.assertIn(health["status"], ("healthy", "warning"))


if __name__ == "__main__":
    unittest.main()

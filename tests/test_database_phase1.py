"""Automated Phase 1 tests for Supabase/PostgreSQL configuration, migrations,
connection lifecycle, idempotency, concurrency, health checks, and safe logging.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest

from engine.database import (
    TRANSIENT_ERRORS,
    CommitOutcomeUnknownError,
    DatabaseManager,
    DatabaseUnavailableError,
    IdempotencyIdentityMismatchError,
    StaleAttemptError,
    UTDADatabaseRepository,
)
from engine.db_config import (
    DatabaseConfig,
    DatabaseConfigError,
    quote_ident,
    redact_dsn,
    validate_identifier,
    validate_project_id,
)
from engine.sanitization import sanitize_log_text, sanitize_value
from engine.db_health import check_database_health
from engine.migrations.runner import (
    MigrationChecksumError,
    MigrationError,
    UnknownMigrationError,
    apply_migrations,
    discover_migrations,
)

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "engine" / "migrations"


class _FakeDBState:
    """Thread-safe in-memory state simulating PostgreSQL tables for unit tests."""

    def __init__(
        self,
        fail_sql_substring: str | None = None,
        current_user: str = "utda_migrator",
    ) -> None:
        self.lock = threading.Lock()
        self.current_user = current_user
        self.tables_initialized = False
        self.migrations: dict[str, tuple[str, str]] = {}
        self.projects: dict[str, tuple] = {}
        self.receipts_by_delivery: dict[str, str] = {}
        self.receipt_payloads: dict[str, dict] = {}
        self.feature_states: dict[tuple[str, str], dict] = {}
        self.final_reports: dict[tuple[str, str, str], str] = {}
        self.idempotency_keys: dict[str, dict] = {}
        self.attempts: list[dict] = []
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

        norm = " ".join(sql.split()).replace('"', "")
        self._rows = []
        self._idx = 0

        if norm.startswith("SET LOCAL statement_timeout"):
            return
        if norm.startswith("SELECT set_config('utda.current_project_id'"):
            self.conn.current_project_id = params[0]
            self.conn.executed_params.append((sql.strip(), params))
            self._rows = [(params[0],)]
            return
        if "SELECT current_setting('utda.current_project_id', true)" in norm:
            self._rows = [(self.conn.current_project_id,)]
            return
        if norm == "SELECT CURRENT_USER":
            self._rows = [(state.current_user,)]
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
            self.conn.pending_ops.append(("project", params))
            return
        if "SELECT project_name, status, github_owner" in norm and ".projects" in norm:
            pid = params[0]
            with state.lock:
                proj = state.projects.get(pid)
                if proj:
                    self._rows = [proj]
            return
        if "INSERT INTO" in norm and ".github_event_receipts" in norm:
            import json as _json

            receipt_id, delivery_id = params[0], params[1]
            payload_summary = _json.loads(params[9])
            with state.lock:
                if delivery_id in state.receipts_by_delivery:
                    self._rows = []
                else:
                    self.conn.undo_ops.append(("receipt", delivery_id))
                    state.receipts_by_delivery[delivery_id] = receipt_id
                    state.receipt_payloads[delivery_id] = payload_summary
                    self._rows = [(receipt_id,)]
            return
        if "SELECT receipt_id FROM" in norm and ".github_event_receipts" in norm:
            did = params[0]
            with state.lock:
                rid = state.receipts_by_delivery.get(did)
                if rid:
                    self._rows = [(rid,)]
            return
        if "INSERT INTO" in norm and ".feature_processing_states" in norm:
            state_id, project_id, feature_id, _init, pr_num, pr_sha, rep_sha = params[:7]
            with state.lock:
                self.conn.undo_ops.append(
                    ("feature_state", (project_id, feature_id), copy.deepcopy(state.feature_states.get((project_id, feature_id))))
                )
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
                    self.conn.undo_ops.append(("final_report", key))
                    state.final_reports[key] = report_id
                    self._rows = [(report_id,)]
            return
        if "SELECT report_id FROM" in norm and ".final_reports" in norm:
            key = (params[0], params[1], params[2])
            with state.lock:
                rid = state.final_reports.get(key)
                if rid:
                    self._rows = [(rid,)]
            return
        if "INSERT INTO" in norm and ".idempotency_keys" in norm and "ON CONFLICT (idempotency_key) DO NOTHING" in norm:
            idem_key, scope, project_id, feature_id, report_blob_sha, target_doc_id, _ttl = params
            with state.lock:
                if idem_key not in state.idempotency_keys:
                    self.conn.undo_ops.append(("idem_key", idem_key, None))
                    state.idempotency_keys[idem_key] = {
                        "scope": scope,
                        "project_id": project_id,
                        "feature_id": feature_id,
                        "report_blob_sha": report_blob_sha,
                        "target_document_id": target_doc_id,
                        "status": "IN_PROGRESS",
                        "active_attempt_id": None,
                        "attempt_fence_version": 0,
                        "expired": False,
                    }
                    self._rows = [(idem_key,)]
                else:
                    self._rows = []
            return
        if "SELECT scope, project_id, feature_id, report_blob_sha, target_document_id, status" in norm and ".idempotency_keys" in norm:
            idem_key = params[0]
            with state.lock:
                rec = state.idempotency_keys.get(idem_key)
                if rec:
                    self._rows = [
                        (
                            rec["scope"],
                            rec["project_id"],
                            rec["feature_id"],
                            rec["report_blob_sha"],
                            rec["target_document_id"],
                            rec["status"],
                            rec.get("active_attempt_id"),
                            rec.get("attempt_fence_version", 0),
                            bool(rec.get("expired", False)),
                        )
                    ]
            return
        if ("SELECT project_id, status, active_attempt_id, attempt_fence_version" in norm or "SELECT status, active_attempt_id, attempt_fence_version" in norm) and ".idempotency_keys" in norm:
            idem_key = params[0]
            with state.lock:
                rec = state.idempotency_keys.get(idem_key)
                if rec:
                    if norm.startswith("SELECT project_id,"):
                        self._rows = [
                            (
                                rec["project_id"],
                                rec["status"],
                                rec.get("active_attempt_id"),
                                rec.get("attempt_fence_version", 0),
                                bool(rec.get("expired", False)),
                            )
                        ]
                    else:
                        self._rows = [
                            (
                                rec["status"],
                                rec.get("active_attempt_id"),
                                rec.get("attempt_fence_version", 0),
                                bool(rec.get("expired", False)),
                            )
                        ]
            return
        if ("SELECT project_id, status, active_attempt_id FROM" in norm or "SELECT status, active_attempt_id FROM" in norm) and ".idempotency_keys" in norm:
            idem_key = params[0]
            with state.lock:
                rec = state.idempotency_keys.get(idem_key)
                if rec:
                    if norm.startswith("SELECT project_id,"):
                        self._rows = [(rec["project_id"], rec["status"], rec.get("active_attempt_id"))]
                    else:
                        self._rows = [(rec["status"], rec.get("active_attempt_id"))]
            return
        if "SELECT pa.attempt_number, pa.status, ik.status, ik.active_attempt_id, ik.attempt_fence_version" in norm:
            attempt_id, idem_key = params
            with state.lock:
                att = next(
                    (a for a in state.attempts if a["attempt_id"] == attempt_id and a["idempotency_key"] == idem_key),
                    None,
                )
                ik = state.idempotency_keys.get(idem_key)
                if att and ik:
                    self._rows = [
                        (
                            att["attempt_number"],
                            att["status"],
                            ik["status"],
                            ik.get("active_attempt_id"),
                            ik.get("attempt_fence_version", 0),
                        )
                    ]
            return
        if "SELECT pa.status, ik.status, ik.active_attempt_id" in norm:
            attempt_id = params[0]
            idem_key = params[1]
            proj_filter = params[2] if len(params) > 2 else None
            with state.lock:
                ik = state.idempotency_keys.get(idem_key)
                if ik and (proj_filter is None or ik.get("project_id") == proj_filter):
                    att = next(
                        (a for a in state.attempts if a["attempt_id"] == attempt_id and a["idempotency_key"] == idem_key),
                        None,
                    )
                    self._rows = [
                        (
                            att["status"] if att else None,
                            ik["status"],
                            ik.get("active_attempt_id"),
                            bool(ik.get("expired", False)),
                        )
                    ]
            return
        if norm.startswith("UPDATE") and ".processing_attempts" in norm and "LOCK_EXPIRED" in norm:
            idem_key = params[0]
            with state.lock:
                self.conn.undo_ops.append(("attempts_snapshot", copy.deepcopy(state.attempts)))
                for att in state.attempts:
                    if att["idempotency_key"] == idem_key and att["status"] == "IN_PROGRESS":
                        att["status"] = "FAILED"
                        att["retry_state"] = "LOCK_EXPIRED"
            return
        if "INSERT INTO" in norm and ".processing_attempts" in norm and "COALESCE(MAX(attempt_number)" in norm:
            attempt_id, event_receipt_id, idem_key, _where_key = params
            with state.lock:
                self.conn.undo_ops.append(("attempts_snapshot", copy.deepcopy(state.attempts)))
                existing_nums = [
                    a["attempt_number"] for a in state.attempts if a["idempotency_key"] == idem_key
                ]
                next_num = max(existing_nums, default=0) + 1
                if any(
                    a["idempotency_key"] == idem_key and a["attempt_number"] == next_num
                    for a in state.attempts
                ):
                    raise RuntimeError("uq_attempt_key_num violation")
                state.attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "event_receipt_id": event_receipt_id,
                        "idempotency_key": idem_key,
                        "attempt_number": next_num,
                        "status": "IN_PROGRESS",
                        "retry_state": "NONE",
                        "error_code": None,
                        "error_summary": None,
                    }
                )
                self._rows = [(attempt_id, next_num)]
            return
        if norm.startswith("UPDATE") and ".processing_attempts" in norm:
            attempt_status, retry_state, error_code, error_summary, attempt_id, idem_key = params
            with state.lock:
                self.conn.undo_ops.append(("attempts_snapshot", copy.deepcopy(state.attempts)))
                for att in state.attempts:
                    if (
                        att["attempt_id"] == attempt_id
                        and att["idempotency_key"] == idem_key
                        and att["status"] == "IN_PROGRESS"
                    ):
                        att["status"] = attempt_status
                        att["retry_state"] = retry_state
                        att["error_code"] = error_code
                        att["error_summary"] = error_summary
                        self._rows = [(att["attempt_id"], att["attempt_number"])]
                        return
                self._rows = []
            return
        if "SELECT COALESCE(MAX(attempt_number), 0) + 1" in norm and ".processing_attempts" in norm:
            idem_key = params[0]
            with state.lock:
                existing_nums = [
                    a["attempt_number"] for a in state.attempts if a["idempotency_key"] == idem_key
                ]
                self._rows = [(max(existing_nums, default=0) + 1,)]
            return
        if "SELECT state_id, status, pr_number, pr_merged_sha, report_blob_sha" in norm:
            project_id, feature_id = params[0], params[1]
            with state.lock:
                rec = state.feature_states.get((project_id, feature_id))
                if rec:
                    self._rows = [
                        (
                            rec["state_id"],
                            rec["status"],
                            None,
                            rec.get("pr_merged_sha"),
                            rec.get("report_blob_sha"),
                        )
                    ]
            return
        if "SELECT status, status" in norm:  # pragma: no cover
            return
        if "SELECT status FROM" in norm and ".processing_attempts" in norm and "WHERE attempt_id = %s" in norm:
            attempt_id, idem_key = params
            with state.lock:
                att = next(
                    (a for a in state.attempts if a["attempt_id"] == attempt_id and a["idempotency_key"] == idem_key),
                    None,
                )
                if att:
                    self._rows = [(att["status"],)]
            return
        if "SET expires_at = NOW()" in norm and ".idempotency_keys" in norm and "attempt_fence_version = %s" in norm:
            from datetime import datetime, timedelta, timezone

            ttl, idem_key, proj_id, active_attempt_id, expected_ver = params
            with state.lock:
                rec = state.idempotency_keys.get(idem_key)
                if (
                    rec
                    and rec.get("project_id") == proj_id
                    and rec.get("status") == "IN_PROGRESS"
                    and rec.get("active_attempt_id") == active_attempt_id
                    and int(rec.get("attempt_fence_version", 0)) == int(expected_ver)
                    and not rec.get("expired")
                ):
                    self.conn.undo_ops.append(("idem_key", idem_key, copy.deepcopy(rec)))
                    new_exp = datetime.now(timezone.utc) + timedelta(seconds=int(ttl))
                    rec["expired"] = False
                    rec["expires_at"] = new_exp
                    self._rows = [(new_exp, int(rec["attempt_fence_version"]))]
                else:
                    self._rows = []
            return
        if "UPDATE" in norm and ".idempotency_keys" in norm and "active_attempt_id = NULL" in norm:
            import json as _json

            status, summary_json, idem_key, proj_id, active_attempt_id = params
            with state.lock:
                rec = state.idempotency_keys.get(idem_key)
                if (
                    rec
                    and rec.get("project_id") == proj_id
                    and rec.get("status") == "IN_PROGRESS"
                    and rec.get("active_attempt_id") == active_attempt_id
                ):
                    self.conn.undo_ops.append(("idem_key", idem_key, copy.deepcopy(rec)))
                    rec["status"] = status
                    rec["active_attempt_id"] = None
                    rec["expired"] = False
                    rec["result_summary"] = _json.loads(summary_json)
                    self._rows = [(idem_key,)]
                else:
                    self._rows = []
            return
        if "UPDATE" in norm and ".idempotency_keys" in norm and "RETURNING attempt_fence_version" in norm:
            active_attempt_id, _ttl, idem_key = params
            with state.lock:
                rec = state.idempotency_keys.get(idem_key)
                if rec:
                    self.conn.undo_ops.append(("idem_key", idem_key, copy.deepcopy(rec)))
                    rec["status"] = "IN_PROGRESS"
                    rec["active_attempt_id"] = active_attempt_id
                    rec["attempt_fence_version"] = int(rec.get("attempt_fence_version", 0)) + 1
                    rec["expired"] = False
                    self._rows = [(rec["attempt_fence_version"],)]
                else:
                    self._rows = []
            return
        if "INSERT INTO" in norm and ".processing_attempts" in norm:
            attempt_id, event_receipt_id, idem_key, attempt_number, attempt_status, retry_state, error_code, error_summary = params
            with state.lock:
                self.conn.undo_ops.append(("attempts_snapshot", copy.deepcopy(state.attempts)))
                if any(
                    a["idempotency_key"] == idem_key and a["attempt_number"] == attempt_number
                    for a in state.attempts
                ):
                    raise RuntimeError("uq_attempt_key_num violation")
                state.attempts.append(
                    {
                        "attempt_id": attempt_id,
                        "event_receipt_id": event_receipt_id,
                        "idempotency_key": idem_key,
                        "attempt_number": attempt_number,
                        "status": attempt_status,
                        "retry_state": retry_state,
                        "error_code": error_code,
                        "error_summary": error_summary,
                    }
                )
                self._rows = [(attempt_id,)]
            return
        if "SELECT event_id FROM" in norm and ".audit_events" in norm:
            eid = params[0]
            with state.lock:
                if any(ev[0] == eid for ev in state.audit_events):
                    self._rows = [(eid,)]
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
    def __init__(self, state: _FakeDBState, pool: "_FakePool | None" = None) -> None:
        self.state = state
        self.pool = pool
        self.committed = 0
        self.rolled_back = 0
        self.pending_ops: list[tuple[str, object]] = []
        self.undo_ops: list[tuple] = []
        self.executed_sql: list[str] = []
        self.executed_params: list[tuple[str, tuple]] = []
        self.current_project_id: str | None = None

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        if self.pool is not None and self.pool.fail_commit_times > 0:
            self.pool.fail_commit_calls += 1
            if self.pool.fail_commit_calls <= self.pool.fail_commit_times:
                # Clear undo_ops if configured to simulate server-side commit before client drop
                if self.pool.commit_drops_after_apply:
                    self._apply_pending()
                err_cls = TRANSIENT_ERRORS[0]
                raise err_cls("Simulated transient drop during COMMIT")
        self.committed += 1
        self._apply_pending()
        self.current_project_id = None

    def _apply_pending(self) -> None:
        with self.state.lock:
            for kind, payload in self.pending_ops:
                if kind == "init_tables":
                    self.state.tables_initialized = True
                elif kind == "migration":
                    version, checksum = payload  # type: ignore[misc]
                    self.state.migrations[version] = (checksum, "APPLIED")
                elif kind == "project":
                    self.state.projects[str(payload[0])] = tuple(payload[1:8])  # type: ignore[index]
                elif kind == "audit":
                    self.state.audit_events.append(payload)  # type: ignore[arg-type]
            self.pending_ops.clear()
            self.undo_ops.clear()

    def rollback(self) -> None:
        self.rolled_back += 1
        with self.state.lock:
            for undo in reversed(self.undo_ops):
                kind = undo[0]
                if kind == "receipt":
                    did = undo[1]
                    self.state.receipts_by_delivery.pop(did, None)
                    self.state.receipt_payloads.pop(did, None)
                elif kind == "feature_state":
                    fkey, prev = undo[1], undo[2]
                    if prev is None:
                        self.state.feature_states.pop(fkey, None)
                    else:
                        self.state.feature_states[fkey] = prev
                elif kind == "final_report":
                    self.state.final_reports.pop(undo[1], None)
                elif kind == "idem_key":
                    ikey, prev = undo[1], undo[2]
                    if prev is None:
                        self.state.idempotency_keys.pop(ikey, None)
                    else:
                        self.state.idempotency_keys[ikey] = prev
                elif kind == "attempts_snapshot":
                    self.state.attempts = undo[1]
            self.undo_ops.clear()
            self.pending_ops.clear()
            self.current_project_id = None


class _FakePool:
    def __init__(
        self,
        state: _FakeDBState,
        fail_connect_times: int = 0,
        fail_commit_times: int = 0,
        commit_drops_after_apply: bool = False,
        reuse_connections: bool = False,
    ) -> None:
        self.state = state
        self.fail_connect_times = fail_connect_times
        self.fail_commit_times = fail_commit_times
        self.commit_drops_after_apply = commit_drops_after_apply
        self.reuse_connections = reuse_connections
        self._reusable_conn: _FakeConnection | None = None
        self.connect_calls = 0
        self.fail_commit_calls = 0
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
                if pool.reuse_connections:
                    if pool._reusable_conn is None:
                        pool._reusable_conn = _FakeConnection(pool.state, pool=pool)
                        pool.connections.append(pool._reusable_conn)
                    return pool._reusable_conn
                conn = _FakeConnection(pool.state, pool=pool)
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
        for mode in ("disable", "allow", "prefer"):
            with self.assertRaises(DatabaseConfigError):
                DatabaseConfig.from_env(
                    {
                        "UTDA_DATABASE_URL": "postgresql://utda:secret@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                        "UTDA_DATABASE_SSLMODE": mode,
                    }
                )
            with self.assertRaises(DatabaseConfigError):
                DatabaseConfig.from_env(
                    {
                        "UTDA_DATABASE_URL": f"postgresql://utda:secret@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode={mode}",
                    }
                )

    def test_rejects_invalid_schemes_and_schema_identifiers(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env({"UTDA_DATABASE_URL": "mysql://u:p@host:3306/db"})
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_env(
                {
                    "UTDA_DATABASE_URL": "postgresql://utda:secret@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                    "UTDA_DATABASE_SCHEMA": "utda; DROP TABLE projects",
                }
            )

    def test_migration_dsn_mandatory_and_tls_validated_independently(self) -> None:
        # Missing UTDA_MIGRATION_DATABASE_URL fails closed without falling back to runtime DSN
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig.from_migration_env(
                {
                    "UTDA_DATABASE_URL": "postgresql://utda_runtime:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
                },
                require_migration_dsn=True,
            )

        # Insecure sslmode in migration DSN query or UTDA_MIGRATION_SSLMODE cannot bypass TLS
        for mode in ("disable", "allow", "prefer"):
            with self.assertRaises(DatabaseConfigError):
                DatabaseConfig.from_env(
                    {
                        "UTDA_DATABASE_URL": "postgresql://utda_runtime:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                        "UTDA_MIGRATION_DATABASE_URL": f"postgresql://utda_migrator:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode={mode}",
                    }
                )
            with self.assertRaises(DatabaseConfigError):
                DatabaseConfig.from_env(
                    {
                        "UTDA_DATABASE_URL": "postgresql://utda_runtime:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                        "UTDA_MIGRATION_DATABASE_URL": "postgresql://utda_migrator:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                        "UTDA_MIGRATION_SSLMODE": mode,
                    }
                )

        # Transaction Pooler migration DSN rejected
        mig_tx = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda_runtime:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
                "UTDA_MIGRATION_DATABASE_URL": "postgresql://utda_migrator:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
            }
        )
        self.assertFalse(mig_tx.supports_migrations)

    def test_valid_session_and_transaction_pooler_configs_and_redaction(self) -> None:
        session_cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda.ref:SuperSecretPw123@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?apikey=SecretQueryKey"
            }
        )
        self.assertEqual(session_cfg.pool_mode, "session")
        self.assertEqual(session_cfg.sslmode, "require")
        self.assertTrue(session_cfg.supports_migrations)
        self.assertNotIn("SuperSecretPw123", repr(session_cfg))
        self.assertNotIn("SuperSecretPw123", redact_dsn(session_cfg.dsn))
        self.assertNotIn("SecretQueryKey", redact_dsn(session_cfg.dsn))

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
                "UTDA_DATABASE_URL": "postgresql://utda_runtime:TopSecretPass99@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
                "UTDA_MIGRATION_DATABASE_URL": "postgresql://utda_migrator:MigSecretPass99@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
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
        self.assertEqual([m.version for m in discovered], ["0001", "0002", "0003", "0004"])
        orig_0003 = next(m for m in discovered if m.version == "0003")
        self.assertEqual(
            orig_0003.checksum_sha256,
            "0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b",
        )

        # Plan on empty DB shows all pending
        plan = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True, connect_fn=lambda _c: _FakeConnection(state))
        self.assertEqual(
            plan["pending"],
            [
                "0001_initial_utda_schema",
                "0002_roles_and_indexes",
                "0003_idempotency_fencing",
                "0004_idempotency_fence_version_hardening",
            ],
        )

        # Apply from empty DB
        res1 = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))
        self.assertEqual(
            res1["applied"],
            [
                "0001_initial_utda_schema",
                "0002_roles_and_indexes",
                "0003_idempotency_fencing",
                "0004_idempotency_fence_version_hardening",
            ],
        )

        # Re-running is safe / no pending migrations
        res2 = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))
        self.assertEqual(res2["applied"], [])

        # Simulate upgrade with a 0005 migration in a temp directory + failure rollback
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for m in discovered:
                (tmp_dir / m.path.name).write_text(m.sql, encoding="utf-8")
            (tmp_dir / "0005_add_sample.sql").write_text("SELECT 1;", encoding="utf-8")
            res3 = apply_migrations(cfg, tmp_dir, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))
            self.assertEqual(res3["applied"], ["0005_add_sample"])

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

        # Unknown DB migration version absent from repo fails closed
        drift_state = _FakeDBState()
        drift_state.tables_initialized = True
        drift_state.migrations["9999"] = ("deadbeef", "APPLIED")
        with self.assertRaises(UnknownMigrationError):
            apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True, connect_fn=lambda _c: _FakeConnection(drift_state))

        # Dry-run on empty DB leaves state completely unchanged
        dry_state = _FakeDBState()
        dry_res = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True, connect_fn=lambda _c: _FakeConnection(dry_state))
        self.assertEqual(dry_res["mode"], "plan")
        self.assertFalse(dry_state.tables_initialized)
        self.assertEqual(dry_state.migrations, {})

        # Identifier validation rejects injection characters
        with self.assertRaises(DatabaseConfigError):
            validate_identifier("utda; DROP TABLE users", kind="schema")
        self.assertEqual(quote_ident("utda_custom", kind="schema"), '"utda_custom"')

        # Missing migration_dsn fails closed on apply
        no_mig_cfg = DatabaseConfig(dsn=cfg.dsn, migration_dsn=None)
        with self.assertRaises(MigrationError):
            apply_migrations(no_mig_cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(state))

        # Wrong database role fails closed on apply
        wrong_role_state = _FakeDBState(current_user="utda_runtime")
        with self.assertRaises(MigrationError):
            apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: _FakeConnection(wrong_role_state))

        # Existing FAILED status in schema_migrations is rejected
        failed_state = _FakeDBState()
        failed_state.tables_initialized = True
        failed_state.migrations["0001"] = (discovered[0].checksum_sha256, "FAILED")
        with self.assertRaises(MigrationError):
            apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True, connect_fn=lambda _c: _FakeConnection(failed_state))

        # Interrupted migration (KeyboardInterrupt) rolls back, releases advisory lock, and propagates
        interrupt_state = _FakeDBState()
        interrupt_conn = _FakeConnection(interrupt_state)
        orig_execute = _FakeCursor.execute

        def _interrupting_execute(cur_self, sql, params=()):
            if "CREATE TABLE" in sql:
                raise KeyboardInterrupt("Simulated SIGINT during migration")
            return orig_execute(cur_self, sql, params)

        old_exec = _FakeCursor.execute
        _FakeCursor.execute = _interrupting_execute  # type: ignore[method-assign]
        try:
            with self.assertRaises(KeyboardInterrupt):
                apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False, connect_fn=lambda _c: interrupt_conn)
        finally:
            _FakeCursor.execute = old_exec  # type: ignore[method-assign]
        self.assertGreaterEqual(interrupt_conn.rolled_back, 1)
        self.assertFalse(interrupt_state.tables_initialized)
        self.assertTrue(any("pg_advisory_unlock" in s for s in interrupt_conn.executed_sql))

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
            self.assertIsNotNone(r1["receipt_id"])
            self.assertEqual(r2["receipt_id"], r1["receipt_id"])

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

            # Report metadata persistence + duplicate returning existing report_id
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
            rep2 = repo.record_final_report(
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
            self.assertFalse(rep2["inserted"])
            self.assertIsNotNone(rep["report_id"])
            self.assertEqual(rep2["report_id"], rep["report_id"])

            # Concurrent fenced idempotency contention: exactly 1 of 6 workers wins
            def _claim(_idx: int) -> dict:
                return repo.claim_idempotency_and_attempt(
                    idempotency_key="UNAI-TECH/pilot:FEAT-1:blob999:D06",
                    scope="document_update",
                    project_id="UTDA_PILOT",
                    feature_id="FEAT-1",
                    report_blob_sha="blob999",
                    target_document_id="D06",
                    event_receipt_id=r1["receipt_id"],
                )

            with ThreadPoolExecutor(max_workers=6) as ex:
                claims = list(ex.map(_claim, range(6)))
            winners = [c for c in claims if c["claimed"]]
            self.assertEqual(len(winners), 1)
            winner_attempt_id = winners[0]["attempt_id"]

            repo.finalize_processing_attempt(
                project_id="UTDA_PILOT",
                event_receipt_id=r1["receipt_id"],
                idempotency_key="UNAI-TECH/pilot:FEAT-1:blob999:D06",
                attempt_id=winner_attempt_id,
                attempt_number=1,
                attempt_status="SUCCEEDED",
                final_key_status="COMPLETED",
                result_summary={"pr": 11},
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
        gh_tok = "ghp_1234567890123456789012345678901234"
        sb_key = "sb_secret_abcdef1234567890"
        api_key = "sk-or-v1-abcdef1234567890123456"
        q_secret = "QuerySecretValue99"
        cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": (
                    f"postgresql://utda_user:{secret_pw}@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
                    f"?apikey={q_secret}&token=UrlTok123"
                ),
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
                db.logger.warning(
                    "Failures: dsn=%s gh=%s sb=%s api=%s",
                    cfg.dsn,
                    gh_tok,
                    sb_key,
                    api_key,
                )
                db.run_in_transaction(lambda _conn: "ok")
        finally:
            logger.removeHandler(handler)

        joined = "\n".join(captured_records)
        for leak in (secret_pw, gh_tok, sb_key, api_key, q_secret, "UrlTok123"):
            self.assertNotIn(leak, joined)
        self.assertIn("***", joined)

    def test_sanitization_shared_module_nested_dicts_lists_and_receipt_payloads(self) -> None:
        raw_nested = {
            "idempotency_key": "safe-idem-key-1",
            "event_key": "safe-event-key-1",
            "changed_count": 3,
            "headers": {
                "Authorization": "opaque-header-secret",
                "x-api-key": "raw-api-key-value",
                "content-type": "application/json",
            },
            "credentials": {
                "client_secret": "opaque-client-secret",
                "tokens": ["opaque-tok-1", "opaque-tok-2"],
            },
            "nested_list": [
                {"note": "ghp_1234567890123456789012345678901234 and AKIAIOSFODNN7EXAMPLE"},
                ("postgresql://u:SecretDbPw@db.example.com:5432/app?apikey=QuerySec9", 42),
            ],
        }
        cleaned = sanitize_value(raw_nested)
        self.assertEqual(cleaned["idempotency_key"], "safe-idem-key-1")
        self.assertEqual(cleaned["event_key"], "safe-event-key-1")
        self.assertEqual(cleaned["changed_count"], 3)
        self.assertEqual(cleaned["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(cleaned["headers"]["x-api-key"], "[REDACTED]")
        self.assertEqual(cleaned["headers"]["content-type"], "application/json")
        self.assertEqual(cleaned["credentials"]["client_secret"], "[REDACTED]")
        self.assertEqual(cleaned["credentials"]["tokens"], ["[REDACTED]", "[REDACTED]"])

        serialized = repr(cleaned)
        for leak in (
            "opaque-header-secret",
            "raw-api-key-value",
            "opaque-client-secret",
            "opaque-tok-1",
            "ghp_1234567890123456789012345678901234",
            "AKIAIOSFODNN7EXAMPLE",
            "SecretDbPw",
            "QuerySec9",
        ):
            self.assertNotIn(leak, serialized)

        # Event receipt payload_summary must be sanitized before persistence
        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)
        with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
            repo = UTDADatabaseRepository(db)
            rec = repo.record_event_receipt(
                delivery_id="deliv-san-1",
                event_name="push",
                repository="UNAI-TECH/pilot",
                project_id="UTDA_PILOT",
                feature_id="FEAT-SAN",
                git_sha="abc1234",
                payload_summary=raw_nested,
            )
            self.assertTrue(rec["inserted"])
        persisted = repr(state.receipt_payloads["deliv-san-1"])
        for leak in (
            "opaque-header-secret",
            "raw-api-key-value",
            "SecretDbPw",
            "ghp_1234567890123456789012345678901234",
        ):
            self.assertNotIn(leak, persisted)

    def test_atomic_attempt_completion_concurrency_and_ambiguous_commit(self) -> None:
        import warnings

        state = _FakeDBState()
        state.tables_initialized = True
        pool = _FakePool(state)

        with DatabaseManager(self._cfg(), pool_factory=lambda _c: pool, sleep_fn=lambda _s: None) as db:
            repo = UTDADatabaseRepository(db)
            idem = "atomic-idem-key-1"

            # 8 concurrent workers try to claim & allocate attempt number -> exactly 1 wins attempt #1
            def _worker(_idx: int) -> dict:
                return repo.claim_idempotency_and_attempt(
                    idempotency_key=idem,
                    scope="phase1_correlation",
                    project_id="UTDA_PILOT",
                    feature_id="FEAT-ATM",
                    report_blob_sha="blob-atm",
                    target_document_id="D06",
                    event_receipt_id=None,
                )

            with ThreadPoolExecutor(max_workers=8) as ex:
                claims = list(ex.map(_worker, range(8)))
            winners = [c for c in claims if c["claimed"]]
            self.assertEqual(len(winners), 1)
            att1_id = winners[0]["attempt_id"]
            self.assertTrue(att1_id)
            self.assertEqual(winners[0]["attempt_number"], 1)
            self.assertEqual(winners[0]["fence_version"], 1)
            self.assertEqual(state.idempotency_keys[idem]["active_attempt_id"], att1_id)
            self.assertEqual(state.idempotency_keys[idem]["attempt_fence_version"], 1)
            self.assertEqual(len(state.attempts), 1)
            self.assertEqual(state.attempts[0]["status"], "IN_PROGRESS")

            # Idempotency identity mismatch across any immutable field is rejected fail-closed
            for bad_kwargs in (
                {"scope": "wrong_scope"},
                {"project_id": "OTHER_PROJ"},
                {"feature_id": "OTHER_FEAT"},
                {"report_blob_sha": "other-blob"},
                {"target_document_id": "D99"},
            ):
                base_req = {
                    "idempotency_key": idem,
                    "scope": "phase1_correlation",
                    "project_id": "UTDA_PILOT",
                    "feature_id": "FEAT-ATM",
                    "report_blob_sha": "blob-atm",
                    "target_document_id": "D06",
                    "event_receipt_id": None,
                }
                base_req.update(bad_kwargs)
                with self.assertRaises(IdempotencyIdentityMismatchError):
                    repo.claim_idempotency_and_attempt(**base_req)

            # Simulate worker 1 expiring before completion -> worker 2 takes over with att2_id
            state.idempotency_keys[idem]["expired"] = True
            with ThreadPoolExecutor(max_workers=6) as ex:
                recovery_claims = list(ex.map(_worker, range(6)))
            rec_winners = [c for c in recovery_claims if c["claimed"]]
            self.assertEqual(len(rec_winners), 1)
            att2_id = rec_winners[0]["attempt_id"]
            self.assertNotEqual(att2_id, att1_id)
            self.assertEqual(rec_winners[0]["attempt_number"], 2)
            self.assertEqual(rec_winners[0]["fence_version"], 2)
            self.assertEqual(state.attempts[0]["status"], "FAILED")
            self.assertEqual(state.attempts[0]["retry_state"], "LOCK_EXPIRED")
            self.assertEqual([a["attempt_number"] for a in state.attempts], [1, 2])
            self.assertEqual(state.idempotency_keys[idem]["active_attempt_id"], att2_id)
            self.assertEqual(state.idempotency_keys[idem]["attempt_fence_version"], 2)

            # Stale worker 1 is rejected on finalize AND renew without altering attempt 2 or fence version
            with self.assertRaises(StaleAttemptError):
                repo.finalize_processing_attempt(
                    project_id="UTDA_PILOT",
                    event_receipt_id=None,
                    idempotency_key=idem,
                    attempt_id=att1_id,
                    attempt_number=1,
                    attempt_status="SUCCEEDED",
                    final_key_status="COMPLETED",
                    result_summary={"stale": True},
                )
            with self.assertRaises(StaleAttemptError):
                repo.renew_attempt_lease(
                    project_id="UTDA_PILOT",
                    idempotency_key=idem,
                    attempt_id=att1_id,
                    expected_fence_version=1,
                )
            with self.assertRaises(StaleAttemptError):
                repo.renew_attempt_lease(
                    project_id="UTDA_PILOT",
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    expected_fence_version=1,
                )
            renewed = repo.renew_attempt_lease(
                project_id="UTDA_PILOT",
                idempotency_key=idem,
                attempt_id=att2_id,
                expected_fence_version=2,
                extend_ttl_seconds=1200,
            )
            self.assertTrue(renewed["renewed"])
            self.assertEqual(renewed["fence_version"], 2)
            self.assertEqual(state.attempts[1]["status"], "IN_PROGRESS")
            self.assertEqual(state.idempotency_keys[idem]["status"], "IN_PROGRESS")
            self.assertEqual(state.idempotency_keys[idem]["active_attempt_id"], att2_id)
            self.assertEqual(state.idempotency_keys[idem]["attempt_fence_version"], 2)

            # Deprecated legacy methods cannot bypass fencing without matching active attempt_id
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                with self.assertRaises(StaleAttemptError):
                    repo.complete_idempotency_key(
                        project_id="UTDA_PILOT", idempotency_key=idem, status="COMPLETED"
                    )
                with self.assertRaises(StaleAttemptError):
                    repo.complete_idempotency_key(
                        project_id="UTDA_PILOT",
                        idempotency_key=idem,
                        status="COMPLETED",
                        attempt_id=att1_id,
                    )
                with self.assertRaises(StaleAttemptError):
                    repo.record_processing_attempt(
                        project_id="UTDA_PILOT",
                        event_receipt_id=None,
                        idempotency_key=idem,
                        attempt_number=99,
                        status="SUCCEEDED",
                    )
                self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))

            # Mid-transaction SQL failure on idempotency_keys update rolls back attempt update too
            state.fail_sql_substring = "AND active_attempt_id = %s"
            with self.assertRaises(RuntimeError):
                repo.finalize_processing_attempt(
                    project_id="UTDA_PILOT",
                    event_receipt_id=None,
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    attempt_number=2,
                    attempt_status="SUCCEEDED",
                    final_key_status="COMPLETED",
                    result_summary={"secret_token": "must-not-leak"},
                )
            self.assertEqual(state.attempts[1]["status"], "IN_PROGRESS")
            self.assertEqual(state.idempotency_keys[idem]["status"], "IN_PROGRESS")

            # Clear SQL failure and finalize atomically with active attempt_id
            state.fail_sql_substring = None
            repo.finalize_processing_attempt(
                project_id="UTDA_PILOT",
                event_receipt_id=None,
                idempotency_key=idem,
                attempt_id=att2_id,
                attempt_number=2,
                attempt_status="SUCCEEDED",
                final_key_status="COMPLETED",
                result_summary={"ok": True, "api_key": "raw-secret-api-key"},
            )
            self.assertEqual(state.attempts[1]["status"], "SUCCEEDED")
            self.assertEqual(state.idempotency_keys[idem]["status"], "COMPLETED")
            self.assertIsNone(state.idempotency_keys[idem]["active_attempt_id"])
            self.assertEqual(state.idempotency_keys[idem]["attempt_fence_version"], 2)
            self.assertNotIn("raw-secret-api-key", repr(state.idempotency_keys[idem]["result_summary"]))

            # Duplicate finalization even with att2_id is rejected once completed
            with self.assertRaises(StaleAttemptError):
                repo.finalize_processing_attempt(
                    project_id="UTDA_PILOT",
                    event_receipt_id=None,
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    attempt_number=2,
                    attempt_status="SUCCEEDED",
                    final_key_status="COMPLETED",
                )

        # Ambiguous commit outcome: DatabaseManager.run_in_transaction NEVER retries after commit drop
        ambig_pool = _FakePool(state, fail_commit_times=1, commit_drops_after_apply=True)
        with DatabaseManager(self._cfg(), pool_factory=lambda _c: ambig_pool, sleep_fn=lambda _s: None) as db:
            calls = {"count": 0}

            def _non_idempotent_op(conn) -> str:
                calls["count"] += 1
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return "done"

            with self.assertRaises(CommitOutcomeUnknownError):
                db.run_in_transaction(_non_idempotent_op)
            self.assertEqual(calls["count"], 1)

        # Repository reconciliation after commit drop (when server committed):
        # stable operation IDs reconcile without creating duplicate claims, attempts, receipts, reports, or audits
        reconcile_pool = _FakePool(state, fail_commit_times=4, commit_drops_after_apply=True)
        with DatabaseManager(self._cfg(), pool_factory=lambda _c: reconcile_pool, sleep_fn=lambda _s: None) as db:
            repo = UTDADatabaseRepository(db)
            rec_claim = repo.claim_idempotency_and_attempt(
                idempotency_key="reconcile-idem-1",
                scope="phase1_correlation",
                project_id="UTDA_PILOT",
                feature_id="FEAT-REC-COMMIT",
                report_blob_sha="blob-rc",
                target_document_id="D06",
                event_receipt_id=None,
            )
            self.assertTrue(rec_claim["claimed"])
            self.assertTrue(rec_claim.get("reconciled"))
            self.assertEqual(rec_claim["attempt_number"], 1)

            rec_fin_id = repo.finalize_processing_attempt(
                project_id="UTDA_PILOT",
                event_receipt_id=None,
                idempotency_key="reconcile-idem-1",
                attempt_id=rec_claim["attempt_id"],
                attempt_number=1,
                attempt_status="SUCCEEDED",
                final_key_status="COMPLETED",
                result_summary={"ok": True},
            )
            self.assertEqual(rec_fin_id, rec_claim["attempt_id"])

            rec_receipt = repo.record_event_receipt(
                delivery_id="reconcile-deliv-1",
                event_name="push",
                repository="UNAI-TECH/pilot",
                project_id="UTDA_PILOT",
                feature_id="FEAT-REC-COMMIT",
                git_sha="abc1234",
            )
            self.assertTrue(rec_receipt["inserted"])
            self.assertTrue(rec_receipt.get("reconciled"))

            before_audits = len(state.audit_events)
            ev_id = repo.append_audit_event(
                actor="utda_runtime",
                event_type="RECONCILE_TEST",
                project_id="UTDA_PILOT",
            )
            self.assertTrue(ev_id)
            self.assertEqual(len(state.audit_events), before_audits + 1)


class Phase1LivePostgreSQLTests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("UTDA_TEST_DATABASE_URL") and os.getenv("UTDA_TEST_MIGRATOR_URL"),
        "Local PostgreSQL UTDA_TEST_DATABASE_URL / UTDA_TEST_MIGRATOR_URL not set (runs in ci-database.yml)",
    )
    def test_real_local_postgres_migrations_and_persistence(self) -> None:
        import psycopg

        cfg = DatabaseConfig.from_migration_env(
            {
                **os.environ,
                "UTDA_DATABASE_URL": os.environ["UTDA_TEST_DATABASE_URL"],
                "UTDA_MIGRATION_DATABASE_URL": os.environ["UTDA_TEST_MIGRATOR_URL"],
            }
        )
        # Re-running plan & apply after CI step is idempotent
        plan_after = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True)
        self.assertEqual(plan_after["pending"], [])
        apply_again = apply_migrations(cfg, MIGRATIONS_DIR, dry_run=False)
        self.assertEqual(apply_again["applied"], [])
        self.assertEqual(apply_again["connected_user"], "utda_migrator")

        # Verify runner refuses to apply as non-utda_migrator role (e.g. runtime or admin)
        if os.getenv("UTDA_TEST_RUNTIME_URL"):
            bad_role_cfg = DatabaseConfig.from_migration_env(
                {
                    **os.environ,
                    "UTDA_DATABASE_URL": os.environ["UTDA_TEST_DATABASE_URL"],
                    "UTDA_MIGRATION_DATABASE_URL": os.environ["UTDA_TEST_RUNTIME_URL"],
                }
            )
            with self.assertRaises(MigrationError):
                apply_migrations(bad_role_cfg, MIGRATIONS_DIR, dry_run=False)

        # Verify advisory lock contention, release on close, and concurrent runner serialization
        with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as c1:
            with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as c2:
                with c1.cursor() as cur1, c2.cursor() as cur2:
                    cur1.execute("SELECT pg_advisory_lock(hashtext('utda_mig:utda')::bigint)")
                    c1.commit()
                    cur2.execute("SELECT pg_try_advisory_lock(hashtext('utda_mig:utda')::bigint)")
                    self.assertFalse(cur2.fetchone()[0])

                    # Concurrent migration run in Thread 2 must block until c1 releases lock
                    concurrent_result: list[dict] = []
                    def _run_concurrent() -> None:
                        concurrent_result.append(apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True))

                    t = threading.Thread(target=_run_concurrent)
                    t.start()
                    t.join(timeout=0.3)
                    self.assertTrue(t.is_alive())
                    # Closing c1 automatically releases PostgreSQL session advisory lock
                    c1.close()
                    t.join(timeout=5.0)
                    self.assertFalse(t.is_alive())
                    self.assertEqual(concurrent_result[0]["mode"], "plan")

        # Verify migration atomicity, checksum mismatch, and recovery on real PG16
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            for m in discover_migrations(MIGRATIONS_DIR):
                (tmp_dir / m.path.name).write_text(m.sql, encoding="utf-8")
            (tmp_dir / "0005_broken.sql").write_text(
                "CREATE TABLE utda.should_not_exist (id INT); THIS IS INVALID SQL;",
                encoding="utf-8",
            )
            with self.assertRaises(MigrationError):
                apply_migrations(cfg, tmp_dir, dry_run=False)

            # Advisory lock must be released after failed migration
            with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as lock_chk:
                with lock_chk.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(hashtext('utda_mig:utda')::bigint)")
                    self.assertTrue(cur.fetchone()[0])
                    cur.execute("SELECT pg_advisory_unlock(hashtext('utda_mig:utda')::bigint)")
                    lock_chk.commit()

            # Modified historical migration checksum must fail closed
            (tmp_dir / "0001_initial_utda_schema.sql").write_text("-- tampered", encoding="utf-8")
            with self.assertRaises(MigrationChecksumError):
                apply_migrations(cfg, tmp_dir, dry_run=True)

        # Verify fixing a failing migration in an isolated test schema recovers cleanly
        rec_cfg = DatabaseConfig(
            dsn=cfg.dsn,
            migration_dsn=cfg.migration_dsn,
            schema="utda_recovery_test",
            pool_mode=cfg.pool_mode,
            migration_pool_mode=cfg.migration_pool_mode,
            sslmode=cfg.sslmode,
            migration_sslmode=cfg.migration_sslmode,
        )
        with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
            with adm.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS utda_recovery_test AUTHORIZATION utda_migrator")
            adm.commit()
        try:
            with tempfile.TemporaryDirectory() as rtmp:
                rdir = Path(rtmp)
                (rdir / "0001_initial_utda_schema.sql").write_text(
                    (MIGRATIONS_DIR / "0001_initial_utda_schema.sql").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                (rdir / "0002_bad.sql").write_text(
                    "CREATE TABLE utda.rec_check (id INT); BAD SQL;",
                    encoding="utf-8",
                )
                with self.assertRaises(MigrationError):
                    apply_migrations(rec_cfg, rdir, dry_run=False)
                # Fix 0002 and re-apply
                (rdir / "0002_bad.sql").write_text(
                    "CREATE TABLE utda.rec_check (id INT PRIMARY KEY);",
                    encoding="utf-8",
                )
                rec_res = apply_migrations(rec_cfg, rdir, dry_run=False)
                self.assertEqual(rec_res["applied"], ["0002_bad"])
        finally:
            with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                with adm.cursor() as cur:
                    cur.execute("DROP SCHEMA IF EXISTS utda_recovery_test CASCADE")
                adm.commit()

        # Dry-run in isolated schema does not create tables or alter DB state
        dry_cfg = DatabaseConfig(
            dsn=cfg.dsn,
            migration_dsn=cfg.migration_dsn,
            schema="utda_dryrun_check",
            pool_mode=cfg.pool_mode,
            migration_pool_mode=cfg.migration_pool_mode,
            sslmode=cfg.sslmode,
            migration_sslmode=cfg.migration_sslmode,
        )
        dry_plan = apply_migrations(dry_cfg, MIGRATIONS_DIR, dry_run=True)
        self.assertEqual(len(dry_plan["pending"]), 4)
        with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as chk:
            with chk.cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'utda_dryrun_check'"
                )
                self.assertIsNone(cur.fetchone())

        # Unknown DB migration version & FAILED migration status fail closed
        with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as unk_conn:
            with unk_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO utda.schema_migrations (
                        version, name, checksum_sha256, status, applied_by, execution_ms
                    )
                    VALUES ('9999', 'unknown_db_mig', 'abc', 'APPLIED', 'rogue', 1)
                    """
                )
            unk_conn.commit()
        try:
            with self.assertRaises(UnknownMigrationError):
                apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True)
        finally:
            with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as cln:
                with cln.cursor() as cur:
                    cur.execute("DELETE FROM utda.schema_migrations WHERE version = '9999'")
                cln.commit()

        with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as fconn:
            with fconn.cursor() as cur:
                cur.execute("UPDATE utda.schema_migrations SET status = 'FAILED' WHERE version = '0002'")
            fconn.commit()
        try:
            with self.assertRaises(MigrationError):
                apply_migrations(cfg, MIGRATIONS_DIR, dry_run=True)
        finally:
            with psycopg.connect(cfg.migration_dsn, **cfg.connect_kwargs(for_migration=True)) as cln:
                with cln.cursor() as cur:
                    cur.execute("UPDATE utda.schema_migrations SET status = 'APPLIED' WHERE version = '0002'")
                cln.commit()

        with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as chk:
            with chk.cursor() as cur:
                cur.execute("SELECT to_regclass('utda.should_not_exist')")
                self.assertIsNone(cur.fetchone()[0])

                # Migration 0003 schema & constraint checks on PG16: BIGINT, CHECK (>=0), deferred FK
                cur.execute(
                    """
                    SELECT data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'utda'
                      AND table_name = 'idempotency_keys'
                      AND column_name = 'attempt_fence_version'
                    """
                )
                self.assertEqual(cur.fetchone()[0], "bigint")

                cur.execute(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'utda'
                      AND tablename = 'processing_attempts'
                      AND indexname = 'uq_processing_attempts_key_attempt'
                    """
                )
                self.assertEqual(cur.fetchone()[0], "uq_processing_attempts_key_attempt")

        # Verify chk_idempotency_fence_nonnegative and DEFERRABLE fk_idempotency_active_attempt
        with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as fchk:
            with fchk.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO utda.projects (
                        project_id, project_name, status, github_owner, github_repo, default_branch
                    )
                    VALUES ('MIG0003_CHK', 'Mig 0003 Check', 'active', 'UNAI-TECH', 'mig0003', 'main')
                    ON CONFLICT (project_id) DO NOTHING
                    """
                )
            fchk.commit()
            with self.assertRaises(psycopg.errors.CheckViolation):
                with fchk.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO utda.idempotency_keys (
                            idempotency_key, scope, project_id, feature_id, report_blob_sha,
                            target_document_id, status, attempt_fence_version
                        )
                        VALUES ('neg-fence-test', 's', 'MIG0003_CHK', 'F1', 'sha', 'D06', 'FAILED', -1)
                        """
                    )
            fchk.rollback()

            # Deferred FK allows updating active_attempt_id before child insert within same tx,
            # but rejects commit if child attempt_id is missing.
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                with fchk.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO utda.idempotency_keys (
                            idempotency_key, scope, project_id, feature_id, report_blob_sha,
                            target_document_id, status, active_attempt_id, attempt_fence_version
                        )
                        VALUES ('bogus-fk-test', 's', 'MIG0003_CHK', 'F1', 'sha', 'D06', 'IN_PROGRESS', 'missing-att-id', 1)
                        """
                    )
                fchk.commit()
            fchk.rollback()

        # Upgrade test: apply 0001 + 0002 with pre-existing rows, then upgrade to 0003
        upg_cfg = DatabaseConfig(
            dsn=cfg.dsn,
            migration_dsn=cfg.migration_dsn,
            schema="utda_upgrade_0003_test",
            pool_mode=cfg.pool_mode,
            migration_pool_mode=cfg.migration_pool_mode,
            sslmode=cfg.sslmode,
            migration_sslmode=cfg.migration_sslmode,
        )
        with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
            with adm.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS utda_upgrade_0003_test AUTHORIZATION utda_migrator")
            adm.commit()
        try:
            with tempfile.TemporaryDirectory() as utmp:
                udir = Path(utmp)
                for fname in ("0001_initial_utda_schema.sql", "0002_roles_and_indexes.sql"):
                    (udir / fname).write_text(
                        (MIGRATIONS_DIR / fname).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                res_pre = apply_migrations(upg_cfg, udir, dry_run=False)
                self.assertEqual(res_pre["applied"], ["0001_initial_utda_schema", "0002_roles_and_indexes"])

                # Seed pre-0003 rows (project, COMPLETED idempotency_key, historical attempt)
                with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                    with adm.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO utda_upgrade_0003_test.projects (
                                project_id, project_name, status, github_owner, github_repo, default_branch
                            ) VALUES ('PRE0003', 'Pre', 'active', 'UNAI-TECH', 'pre', 'main')
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO utda_upgrade_0003_test.idempotency_keys (
                                idempotency_key, scope, project_id, feature_id, report_blob_sha, target_document_id, status
                            ) VALUES ('pre-key-1', 'phase1', 'PRE0003', 'F1', 'blob1', 'D06', 'COMPLETED')
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO utda_upgrade_0003_test.processing_attempts (
                                attempt_id, idempotency_key, attempt_number, status
                            ) VALUES ('pre-att-1', 'pre-key-1', 1, 'SUCCEEDED')
                            """
                        )
                    adm.commit()

                # Add 0003 and 0004 and verify upgrade + pre-existing row defaults + repeat-run idempotency
                for fname in ("0003_idempotency_fencing.sql", "0004_idempotency_fence_version_hardening.sql"):
                    (udir / fname).write_text(
                        (MIGRATIONS_DIR / fname).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                res_upg = apply_migrations(upg_cfg, udir, dry_run=False)
                self.assertEqual(
                    res_upg["applied"],
                    ["0003_idempotency_fencing", "0004_idempotency_fence_version_hardening"],
                )
                res_upg_again = apply_migrations(upg_cfg, udir, dry_run=False)
                self.assertEqual(res_upg_again["applied"], [])
                with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                    with adm.cursor() as cur:
                        cur.execute(
                            "SELECT active_attempt_id, attempt_fence_version FROM utda_upgrade_0003_test.idempotency_keys WHERE idempotency_key = 'pre-key-1'"
                        )
                        self.assertEqual(cur.fetchone(), (None, 0))
                        cur.execute(
                            """
                            SELECT data_type FROM information_schema.columns
                            WHERE table_schema = 'utda_upgrade_0003_test'
                              AND table_name = 'idempotency_keys'
                              AND column_name = 'attempt_fence_version'
                            """
                        )
                        self.assertEqual(cur.fetchone()[0], "bigint")
        finally:
            with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                with adm.cursor() as cur:
                    cur.execute("DROP SCHEMA IF EXISTS utda_upgrade_0003_test CASCADE")
                adm.commit()

        # Upgrade DB where original 0003 is ALREADY applied (with COMPLETED & active IN_PROGRESS rows),
        # plus failed-0004 rollback and recovery -> validates items 3-10 of migration suite on PG16.
        orig03_cfg = DatabaseConfig(
            dsn=cfg.dsn,
            migration_dsn=cfg.migration_dsn,
            schema="utda_orig0003_upgrade_test",
            pool_mode=cfg.pool_mode,
            migration_pool_mode=cfg.migration_pool_mode,
            sslmode=cfg.sslmode,
            migration_sslmode=cfg.migration_sslmode,
        )
        with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
            with adm.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS utda_orig0003_upgrade_test AUTHORIZATION utda_migrator")
            adm.commit()
        try:
            with tempfile.TemporaryDirectory() as otmp:
                odir = Path(otmp)
                for fname in (
                    "0001_initial_utda_schema.sql",
                    "0002_roles_and_indexes.sql",
                    "0003_idempotency_fencing.sql",
                ):
                    (odir / fname).write_text(
                        (MIGRATIONS_DIR / fname).read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                pre03 = apply_migrations(orig03_cfg, odir, dry_run=False)
                self.assertEqual(
                    pre03["applied"],
                    ["0001_initial_utda_schema", "0002_roles_and_indexes", "0003_idempotency_fencing"],
                )

                # Confirm pre-0004 attempt_fence_version is integer and seed active & completed rows
                with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                    with adm.cursor() as cur:
                        cur.execute(
                            """
                            SELECT data_type FROM information_schema.columns
                            WHERE table_schema = 'utda_orig0003_upgrade_test'
                              AND table_name = 'idempotency_keys'
                              AND column_name = 'attempt_fence_version'
                            """
                        )
                        self.assertEqual(cur.fetchone()[0], "integer")
                        cur.execute(
                            """
                            INSERT INTO utda_orig0003_upgrade_test.projects (
                                project_id, project_name, status, github_owner, github_repo, default_branch
                            ) VALUES ('ORIG0003', 'Orig 0003', 'active', 'UNAI-TECH', 'orig3', 'main')
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO utda_orig0003_upgrade_test.idempotency_keys (
                                idempotency_key, scope, project_id, feature_id, report_blob_sha,
                                target_document_id, status, active_attempt_id, attempt_fence_version
                            ) VALUES
                                ('orig03-done-key', 'phase1', 'ORIG0003', 'F1', 'b1', 'D06', 'COMPLETED', NULL, 1),
                                ('orig03-active-key', 'phase1', 'ORIG0003', 'F2', 'b2', 'D06', 'IN_PROGRESS', 'orig-att-2', 2)
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO utda_orig0003_upgrade_test.processing_attempts (
                                attempt_id, idempotency_key, attempt_number, status
                            ) VALUES
                                ('orig-att-1', 'orig03-active-key', 1, 'FAILED'),
                                ('orig-att-2', 'orig03-active-key', 2, 'IN_PROGRESS')
                            """
                        )
                    adm.commit()

                # Failed 0004 rolls back atomically leaving type integer and 0004 absent from schema_migrations
                (odir / "0004_idempotency_fence_version_hardening.sql").write_text(
                    "ALTER TABLE utda.idempotency_keys ALTER COLUMN attempt_fence_version TYPE BIGINT; BROKEN SQL;",
                    encoding="utf-8",
                )
                with self.assertRaises(MigrationError):
                    apply_migrations(orig03_cfg, odir, dry_run=False)
                with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                    with adm.cursor() as cur:
                        cur.execute(
                            """
                            SELECT data_type FROM information_schema.columns
                            WHERE table_schema = 'utda_orig0003_upgrade_test'
                              AND table_name = 'idempotency_keys'
                              AND column_name = 'attempt_fence_version'
                            """
                        )
                        self.assertEqual(cur.fetchone()[0], "integer")
                        cur.execute(
                            "SELECT version FROM utda_orig0003_upgrade_test.schema_migrations WHERE version = '0004'"
                        )
                        self.assertIsNone(cur.fetchone())

                # Replace with real 0004 and recover safely
                (odir / "0004_idempotency_fence_version_hardening.sql").write_text(
                    (MIGRATIONS_DIR / "0004_idempotency_fence_version_hardening.sql").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                rec04 = apply_migrations(orig03_cfg, odir, dry_run=False)
                self.assertEqual(rec04["applied"], ["0004_idempotency_fence_version_hardening"])
                self.assertEqual(apply_migrations(orig03_cfg, odir, dry_run=True)["pending"], [])
                self.assertEqual(apply_migrations(orig03_cfg, odir, dry_run=False)["applied"], [])

                with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                    with adm.cursor() as cur:
                        cur.execute(
                            """
                            SELECT idempotency_key, status, active_attempt_id, attempt_fence_version
                            FROM utda_orig0003_upgrade_test.idempotency_keys
                            ORDER BY idempotency_key
                            """
                        )
                        self.assertEqual(
                            cur.fetchall(),
                            [
                                ("orig03-active-key", "IN_PROGRESS", "orig-att-2", 2),
                                ("orig03-done-key", "COMPLETED", None, 1),
                            ],
                        )
                        cur.execute(
                            """
                            SELECT data_type FROM information_schema.columns
                            WHERE table_schema = 'utda_orig0003_upgrade_test'
                              AND table_name = 'idempotency_keys'
                              AND column_name = 'attempt_fence_version'
                            """
                        )
                        self.assertEqual(cur.fetchone()[0], "bigint")
                        cur.execute(
                            """
                            SELECT version, checksum_sha256
                            FROM utda_orig0003_upgrade_test.schema_migrations
                            ORDER BY version
                            """
                        )
                        mig_rows = {v: c for v, c in cur.fetchall()}
                        self.assertEqual(
                            mig_rows["0003"],
                            "0161daaebb55dec4f6f1f600f5c76ca4dcd0e5e3abab2c392b5ab6f18c6d6d1b",
                        )
                        self.assertEqual(
                            mig_rows["0004"],
                            discover_migrations(MIGRATIONS_DIR)[3].checksum_sha256,
                        )
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with adm.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE utda_orig0003_upgrade_test.idempotency_keys
                                SET attempt_fence_version = -5
                                WHERE idempotency_key = 'orig03-done-key'
                                """
                            )
                    adm.rollback()
        finally:
            with psycopg.connect(cfg.dsn, **cfg.connect_kwargs()) as adm:
                with adm.cursor() as cur:
                    cur.execute("DROP SCHEMA IF EXISTS utda_orig0003_upgrade_test CASCADE")
                adm.commit()

        with DatabaseManager(cfg) as db:
            health = check_database_health(db, read_only=True)
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
            claim1 = repo.claim_idempotency_and_attempt(
                idempotency_key="ci-local-test:FEAT-CI:sha1:D06",
                scope="ci_test",
                project_id="CI_LOCAL_TEST",
                feature_id="FEAT-CI",
                report_blob_sha="sha1",
                target_document_id="D06",
                event_receipt_id=None,
            )
            self.assertTrue(claim1["claimed"])
            self.assertTrue(claim1["attempt_id"])
            self.assertFalse(
                repo.claim_idempotency_and_attempt(
                    idempotency_key="ci-local-test:FEAT-CI:sha1:D06",
                    scope="ci_test",
                    project_id="CI_LOCAL_TEST",
                    feature_id="FEAT-CI",
                    report_blob_sha="sha1",
                    target_document_id="D06",
                    event_receipt_id=None,
                )["claimed"]
            )
            repo.finalize_processing_attempt(
                project_id="CI_LOCAL_TEST",
                event_receipt_id=None,
                idempotency_key="ci-local-test:FEAT-CI:sha1:D06",
                attempt_id=claim1["attempt_id"],
                attempt_number=claim1["attempt_number"],
                attempt_status="FAILED",
                final_key_status="FAILED",
                retry_state="RETRY_SCHEDULED",
                result_summary={"error": "transient"},
            )
            claim2 = repo.claim_idempotency_and_attempt(
                idempotency_key="ci-local-test:FEAT-CI:sha1:D06",
                scope="ci_test",
                project_id="CI_LOCAL_TEST",
                feature_id="FEAT-CI",
                report_blob_sha="sha1",
                target_document_id="D06",
                event_receipt_id=None,
            )
            self.assertTrue(claim2["claimed"])
            self.assertEqual(claim2["attempt_number"], 2)

    @unittest.skipUnless(
        os.getenv("UTDA_TEST_DATABASE_URL")
        and os.getenv("UTDA_TEST_RUNTIME_URL")
        and os.getenv("UTDA_TEST_MIGRATOR_URL")
        and os.getenv("UTDA_TEST_GOVERNANCE_URL"),
        "PG16 multi-role security tests require CI role DSNs (runs in ci-database.yml)",
    )
    def test_real_postgres_least_privilege_and_default_privileges(self) -> None:
        import psycopg

        admin_cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_DATABASE_URL")
        runtime_cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_RUNTIME_URL")
        migrator_cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_MIGRATOR_URL")
        gov_cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_GOVERNANCE_URL")

        all_tables = (
            "schema_migrations",
            "projects",
            "template_releases",
            "project_provisioning_runs",
            "github_event_receipts",
            "feature_processing_states",
            "final_reports",
            "idempotency_keys",
            "processing_attempts",
            "document_proposals",
            "approval_records",
            "project_master_versions",
            "company_aggregation_runs",
            "drive_file_mappings",
            "drive_sync_revisions",
            "rollback_operations",
            "audit_events",
        )

        # 0. Verify schema/table ownership, role memberships, and effective privileges
        with psycopg.connect(admin_cfg.dsn, **admin_cfg.connect_kwargs()) as aconn:
            with aconn.cursor() as cur:
                # Ownership
                cur.execute(
                    "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'utda'"
                )
                self.assertEqual(cur.fetchone()[0], "utda_migrator")
                cur.execute(
                    "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'utda'"
                )
                owners = cur.fetchall()
                self.assertEqual(len(owners), 17)
                for tbl, owner in owners:
                    self.assertEqual(owner, "utda_migrator", f"Table {tbl} owner mismatch")

                # Role inheritance isolation & NOINHERIT
                cur.execute(
                    "SELECT rolname, rolinherit FROM pg_roles WHERE rolname IN ('utda_migrator', 'utda_runtime', 'utda_governance')"
                )
                for rname, rinh in cur.fetchall():
                    self.assertFalse(rinh, f"{rname} must be NOINHERIT")
                cur.execute("SELECT pg_has_role('utda_runtime', 'utda_migrator', 'MEMBER')")
                self.assertFalse(cur.fetchone()[0])
                cur.execute("SELECT pg_has_role('utda_runtime', 'utda_governance', 'MEMBER')")
                self.assertFalse(cur.fetchone()[0])
                cur.execute("SELECT pg_has_role('utda_governance', 'utda_migrator', 'MEMBER')")
                self.assertFalse(cur.fetchone()[0])

                # Schema effective privileges
                cur.execute(
                    "SELECT has_schema_privilege('utda_runtime', 'utda', 'USAGE'), has_schema_privilege('utda_runtime', 'utda', 'CREATE')"
                )
                self.assertEqual(cur.fetchone(), (True, False))
                cur.execute(
                    "SELECT has_schema_privilege('utda_governance', 'utda', 'USAGE'), has_schema_privilege('utda_governance', 'utda', 'CREATE')"
                )
                self.assertEqual(cur.fetchone(), (True, False))

                # Effective privileges for PUBLIC, anon, authenticated (all must be False)
                for api_role in ("public", "anon", "authenticated"):
                    cur.execute("SELECT has_schema_privilege(%s, 'utda', 'USAGE')", (api_role,))
                    self.assertFalse(cur.fetchone()[0], f"{api_role} must not have USAGE on utda")
                    for tbl in all_tables:
                        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                            cur.execute(
                                f"SELECT has_table_privilege(%s, 'utda.{tbl}', %s)",
                                (api_role, priv),
                            )
                            self.assertFalse(
                                cur.fetchone()[0],
                                f"{api_role} unexpectedly has {priv} on utda.{tbl}",
                            )

                # Test live SET ROLE anon & authenticated rejection
                for api_role in ("anon", "authenticated"):
                    cur.execute(f"SET LOCAL ROLE {api_role}")
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        cur.execute("SELECT COUNT(*) FROM utda.projects")
                    aconn.rollback()

        # 1. Runtime performs operational queries it actually requires
        with DatabaseManager(runtime_cfg) as runtime_db:
            health = check_database_health(runtime_db, read_only=True)
            self.assertIn(health["status"], ("healthy", "warning"))
            repo = UTDADatabaseRepository(runtime_db)
            repo.upsert_project(
                project_id="CI_SEC_TEST",
                project_name="CI Security Test",
                status="active",
                github_owner="UNAI-TECH",
                github_repo="ci-sec-test",
                default_branch="main",
            )
            receipt = repo.record_event_receipt(
                delivery_id="sec-deliv-1",
                event_name="push",
                repository="UNAI-TECH/ci-sec-test",
                project_id="CI_SEC_TEST",
                feature_id="FEAT-SEC",
                git_sha="abc1234",
            )
            self.assertTrue(receipt["inserted"])
            state = repo.correlate_feature_state(
                project_id="CI_SEC_TEST",
                feature_id="FEAT-SEC",
                report_blob_sha="blob-sec",
            )
            self.assertEqual(state["status"], "AWAIT_PR_MERGE")
            rep = repo.record_final_report(
                project_id="CI_SEC_TEST",
                feature_id="FEAT-SEC",
                feature_name="Sec",
                change_type="security_change",
                implementation_status="implemented",
                report_path="documentation/reports/final/FEAT-SEC.json",
                report_blob_sha="blob-sec",
                git_commit_sha="abc1234",
                report_metadata={"summary": "sec"},
                evidence_refs=[],
            )
            self.assertTrue(rep["inserted"])
            sec_claim = repo.claim_idempotency_and_attempt(
                idempotency_key="ci-sec-idem-1",
                scope="sec_test",
                project_id="CI_SEC_TEST",
                feature_id="FEAT-SEC",
                report_blob_sha="blob-sec",
                target_document_id="D06",
                event_receipt_id=receipt["receipt_id"],
            )
            self.assertTrue(sec_claim["claimed"])
            repo.finalize_processing_attempt(
                project_id="CI_SEC_TEST",
                event_receipt_id=receipt["receipt_id"],
                idempotency_key="ci-sec-idem-1",
                attempt_id=sec_claim["attempt_id"],
                attempt_number=1,
                attempt_status="SUCCEEDED",
                final_key_status="COMPLETED",
                result_summary={"ok": True},
            )
            repo.append_audit_event(
                actor="utda_runtime",
                event_type="SEC_TEST_OK",
                project_id="CI_SEC_TEST",
                feature_id="FEAT-SEC",
            )

        # 2. Runtime can SELECT all 17 tables and is blocked from prohibited DDL/DML
        blocked_runtime_sql = [
            "CREATE SCHEMA illegal_utda_schema",
            "CREATE TABLE utda.illegal_table (id INT)",
            "CREATE TABLE public.illegal_pub_table (id INT)",
            "ALTER TABLE utda.projects ADD COLUMN evil TEXT",
            "DROP TABLE utda.projects",
            "INSERT INTO utda.approval_records (approval_id, project_id, document_id, section_id, change_id, approver_identity, approver_role, decision, evidence_reference) VALUES ('a1', 'CI_SEC_TEST', 'D06', 's', 'c', 'u', 'pm', 'approved', 'pr:1')",
            "UPDATE utda.approval_records SET decision = 'rejected'",
            "DELETE FROM utda.approval_records",
            "INSERT INTO utda.rollback_operations (rollback_id, project_id, document_id, target_scope, restored_from_ref, restored_sha256, operator_identity, status) VALUES ('r1', 'CI_SEC_TEST', 'D06', 'GITHUB', 'ref', 'sha', 'u', 'DONE')",
            "UPDATE utda.rollback_operations SET status = 'TAMPERED'",
            "DELETE FROM utda.rollback_operations",
            "UPDATE utda.audit_events SET actor = 'tamper'",
            "DELETE FROM utda.audit_events",
            "UPDATE utda.final_reports SET feature_name = 'tamper'",
            "DELETE FROM utda.final_reports",
            "UPDATE utda.github_event_receipts SET event_name = 'tamper'",
            "DELETE FROM utda.github_event_receipts",
            "INSERT INTO utda.schema_migrations (version, name, checksum_sha256, status, applied_by) VALUES ('9998', 'x', 'y', 'APPLIED', 'x')",
            "UPDATE utda.schema_migrations SET status = 'FAILED'",
            "DELETE FROM utda.schema_migrations",
            "DELETE FROM utda.projects",
            "DELETE FROM utda.feature_processing_states",
            "DELETE FROM utda.idempotency_keys",
            "DELETE FROM utda.processing_attempts",
            "INSERT INTO utda.document_proposals (proposal_id, project_id, feature_id, document_id, section_id, change_id, approval_class) VALUES ('p1', 'CI_SEC_TEST', 'F1', 'D06', 's', 'c', 'routine')",
        ]
        with psycopg.connect(runtime_cfg.dsn, **runtime_cfg.connect_kwargs()) as rconn:
            for tbl in all_tables:
                with rconn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM utda.{tbl}")
                rconn.commit()
            for stmt in blocked_runtime_sql:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=f"Expected InsufficientPrivilege for: {stmt}"):
                    with rconn.cursor() as cur:
                        cur.execute(stmt)
                rconn.rollback()

        # 3. Separately controlled utda_governance role
        with psycopg.connect(gov_cfg.dsn, **gov_cfg.connect_kwargs()) as gconn:
            with gconn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM utda.approval_records")
                cur.execute("SELECT COUNT(*) FROM utda.rollback_operations")
                cur.execute(
                    """
                    INSERT INTO utda.approval_records (
                        approval_id, project_id, document_id, section_id, change_id,
                        approver_identity, approver_role, decision, evidence_reference
                    )
                    VALUES ('gov-a1', 'CI_SEC_TEST', 'D06', 'API', 'CHG-1', 'cto@unai.tech', 'cto_or_tech_lead', 'approved', 'pr:1')
                    """
                )
                cur.execute(
                    """
                    INSERT INTO utda.rollback_operations (
                        rollback_id, project_id, document_id, target_scope,
                        restored_from_ref, restored_sha256, operator_identity, status
                    )
                    VALUES ('gov-r1', 'CI_SEC_TEST', 'D06', 'GITHUB', 'sha0', 'hash0', 'admin@unai.tech', 'IN_PROGRESS')
                    """
                )
                cur.execute(
                    "UPDATE utda.rollback_operations SET status = 'COMPLETED' WHERE rollback_id = 'gov-r1'"
                )
                cur.execute(
                    "INSERT INTO utda.audit_events (event_id, actor, event_type) VALUES ('gov-ev-1', 'utda_governance', 'APPROVAL_RECORDED')"
                )
            gconn.commit()

            for stmt in (
                "UPDATE utda.approval_records SET decision = 'rejected'",
                "DELETE FROM utda.approval_records",
                "DELETE FROM utda.rollback_operations",
                "UPDATE utda.audit_events SET actor = 'tamper'",
                "DELETE FROM utda.audit_events",
                "SELECT COUNT(*) FROM utda.schema_migrations",
                "SELECT COUNT(*) FROM utda.idempotency_keys",
                "SELECT COUNT(*) FROM utda.feature_processing_states",
                "CREATE TABLE utda.gov_illegal (id INT)",
                "ALTER TABLE utda.approval_records ADD COLUMN x TEXT",
            ):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=f"Governance unexpectedly allowed: {stmt}"):
                    with gconn.cursor() as cur:
                        cur.execute(stmt)
                gconn.rollback()

        # 4. Future table + sequence created by utda_migrator receives strict default privileges
        with psycopg.connect(migrator_cfg.dsn, **migrator_cfg.connect_kwargs()) as mconn:
            with mconn.cursor() as cur:
                cur.execute("CREATE TABLE utda.future_priv_check (id SERIAL PRIMARY KEY, val TEXT)")
            mconn.commit()
        try:
            with psycopg.connect(admin_cfg.dsn, **admin_cfg.connect_kwargs()) as aconn:
                with aconn.cursor() as cur:
                    for api_role in ("public", "anon", "authenticated"):
                        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                            cur.execute(
                                f"SELECT has_table_privilege(%s, 'utda.future_priv_check', %s)",
                                (api_role, priv),
                            )
                            self.assertFalse(cur.fetchone()[0])

            with psycopg.connect(runtime_cfg.dsn, **runtime_cfg.connect_kwargs()) as rconn:
                with rconn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM utda.future_priv_check")
                rconn.commit()
                for stmt in (
                    "INSERT INTO utda.future_priv_check (val) VALUES ('a')",
                    "UPDATE utda.future_priv_check SET val = 'b'",
                    "DELETE FROM utda.future_priv_check",
                ):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with rconn.cursor() as cur:
                            cur.execute(stmt)
                    rconn.rollback()

            with psycopg.connect(gov_cfg.dsn, **gov_cfg.connect_kwargs()) as gconn:
                with gconn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM utda.future_priv_check")
                gconn.commit()
                for stmt in (
                    "INSERT INTO utda.future_priv_check (val) VALUES ('a')",
                    "UPDATE utda.future_priv_check SET val = 'b'",
                    "DELETE FROM utda.future_priv_check",
                ):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with gconn.cursor() as cur:
                            cur.execute(stmt)
                    gconn.rollback()

            # Explicit migration grant adds only INSERT for utda_runtime
            with psycopg.connect(migrator_cfg.dsn, **migrator_cfg.connect_kwargs()) as mconn:
                with mconn.cursor() as cur:
                    cur.execute("GRANT INSERT ON utda.future_priv_check TO utda_runtime")
                    cur.execute("GRANT USAGE, SELECT ON SEQUENCE utda.future_priv_check_id_seq TO utda_runtime")
                mconn.commit()
            with psycopg.connect(runtime_cfg.dsn, **runtime_cfg.connect_kwargs()) as rconn:
                with rconn.cursor() as cur:
                    cur.execute("INSERT INTO utda.future_priv_check (val) VALUES ('ok')")
                rconn.commit()
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with rconn.cursor() as cur:
                        cur.execute("UPDATE utda.future_priv_check SET val = 'nope'")
                rconn.rollback()
        finally:
            with psycopg.connect(migrator_cfg.dsn, **migrator_cfg.connect_kwargs()) as mconn:
                with mconn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS utda.future_priv_check")
                mconn.commit()

    @unittest.skipUnless(
        os.getenv("UTDA_TEST_BOOTSTRAP_URL"),
        "PG16 bootstrap ownership-transfer test requires UTDA_TEST_BOOTSTRAP_URL (runs in ci-database.yml)",
    )
    def test_real_postgres_bootstrap_ownership_transfer(self) -> None:
        import psycopg

        boot_cfg = DatabaseConfig.from_migration_env(
            {
                **os.environ,
                "UTDA_DATABASE_URL": os.environ["UTDA_TEST_BOOTSTRAP_URL"],
                "UTDA_MIGRATION_DATABASE_URL": os.environ["UTDA_TEST_BOOTSTRAP_URL"],
            }
        )
        res = apply_migrations(
            boot_cfg, MIGRATIONS_DIR, dry_run=False, allow_bootstrap_role=True
        )
        self.assertEqual(
            res["applied"],
            [
                "0001_initial_utda_schema",
                "0002_roles_and_indexes",
                "0003_idempotency_fencing",
                "0004_idempotency_fence_version_hardening",
            ],
        )
        with psycopg.connect(boot_cfg.dsn, **boot_cfg.connect_kwargs()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'utda'"
                )
                self.assertEqual(cur.fetchone()[0], "utda_migrator")
                cur.execute(
                    "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'utda'"
                )
                self.assertEqual(cur.fetchall(), [("utda_migrator",)])

    @unittest.skipUnless(
        os.getenv("UTDA_TEST_DATABASE_URL") and os.getenv("UTDA_TEST_RUNTIME_URL"),
        "PG16 concurrent worker & atomic transaction test requires UTDA_TEST_RUNTIME_URL (runs in ci-database.yml)",
    )
    def test_real_postgres_concurrent_workers_and_atomic_completion(self) -> None:
        import psycopg

        admin_cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_DATABASE_URL")
        runtime_cfg = DatabaseConfig.from_env(dsn_var="UTDA_TEST_RUNTIME_URL")
        idem = "ci-pg16-concurrent-idem-1"
        delivery = "ci-pg16-sanitized-deliv-1"

        with DatabaseManager(runtime_cfg) as db:
            repo = UTDADatabaseRepository(db)
            repo.upsert_project(
                project_id="CI_CONC_TEST",
                project_name="CI Concurrent Test",
                status="active",
                github_owner="UNAI-TECH",
                github_repo="ci-conc-test",
                default_branch="main",
            )
            # Duplicate receipt returns exact original receipt_id
            rec = repo.record_event_receipt(
                delivery_id=delivery,
                event_name="push",
                repository="UNAI-TECH/ci-conc-test",
                project_id="CI_CONC_TEST",
                feature_id="FEAT-CONC",
                git_sha="abc1234",
                payload_summary={
                    "headers": {"Authorization": "pg16-opaque-secret"},
                    "nested": [{"note": "ghp_1234567890123456789012345678901234"}],
                },
            )
            rec_dup = repo.record_event_receipt(
                delivery_id=delivery,
                event_name="push",
                repository="UNAI-TECH/ci-conc-test",
                project_id="CI_CONC_TEST",
                feature_id="FEAT-CONC",
                git_sha="abc1234",
            )
            self.assertTrue(rec["inserted"])
            self.assertFalse(rec_dup["inserted"])
            self.assertEqual(rec_dup["receipt_id"], rec["receipt_id"])

            # Duplicate report returns exact original report_id
            rep1 = repo.record_final_report(
                project_id="CI_CONC_TEST",
                feature_id="FEAT-CONC",
                feature_name="Concurrency & Fencing",
                change_type="database_change",
                implementation_status="implemented",
                report_path="documentation/reports/final/FEAT-CONC.json",
                report_blob_sha="blob-conc",
                git_commit_sha="abc1234",
                report_metadata={"summary": "Concurrent PG16 check"},
                evidence_refs=[{"kind": "commit", "reference": "abc1234"}],
            )
            rep2 = repo.record_final_report(
                project_id="CI_CONC_TEST",
                feature_id="FEAT-CONC",
                feature_name="Concurrency & Fencing",
                change_type="database_change",
                implementation_status="implemented",
                report_path="documentation/reports/final/FEAT-CONC.json",
                report_blob_sha="blob-conc",
                git_commit_sha="abc1234",
                report_metadata={"summary": "Concurrent PG16 check"},
                evidence_refs=[{"kind": "commit", "reference": "abc1234"}],
            )
            self.assertTrue(rep1["inserted"])
            self.assertFalse(rep2["inserted"])
            self.assertEqual(rep2["report_id"], rep1["report_id"])

            # Concurrent claims on real PG16: 8 threads -> 1 winner returns attempt_id #1
            def _claim_pg(_idx: int) -> dict:
                return repo.claim_idempotency_and_attempt(
                    idempotency_key=idem,
                    scope="phase1_correlation",
                    project_id="CI_CONC_TEST",
                    feature_id="FEAT-CONC",
                    report_blob_sha="blob-conc",
                    target_document_id="D06",
                    event_receipt_id=rec["receipt_id"],
                )

            with ThreadPoolExecutor(max_workers=8) as ex:
                first_wave = list(ex.map(_claim_pg, range(8)))
            first_winners = [c for c in first_wave if c["claimed"]]
            self.assertEqual(len(first_winners), 1)
            att1_id = first_winners[0]["attempt_id"]
            self.assertTrue(att1_id)
            self.assertEqual(first_winners[0]["attempt_number"], 1)
            self.assertEqual(first_winners[0]["fence_version"], 1)

            # Immutable identity mismatch rejected on real PG16 across every protected column
            for override in (
                {"scope": "mismatch"},
                {"project_id": "OTHER"},
                {"feature_id": "OTHER"},
                {"report_blob_sha": "mismatch"},
                {"target_document_id": "D99"},
            ):
                req = {
                    "idempotency_key": idem,
                    "scope": "phase1_correlation",
                    "project_id": "CI_CONC_TEST",
                    "feature_id": "FEAT-CONC",
                    "report_blob_sha": "blob-conc",
                    "target_document_id": "D06",
                    "event_receipt_id": rec["receipt_id"],
                    **override,
                }
                with self.assertRaises(IdempotencyIdentityMismatchError):
                    repo.claim_idempotency_and_attempt(**req)

            # Expire lock in DB -> 6 concurrent recovery workers -> 1 winner returns att2_id #2
            with psycopg.connect(admin_cfg.dsn, **admin_cfg.connect_kwargs()) as aconn:
                with aconn.cursor() as cur:
                    cur.execute(
                        "UPDATE utda.idempotency_keys SET expires_at = NOW() - INTERVAL '5 minutes' WHERE idempotency_key = %s",
                        (idem,),
                    )
                aconn.commit()

            with ThreadPoolExecutor(max_workers=6) as ex:
                second_wave = list(ex.map(_claim_pg, range(6)))
            second_winners = [c for c in second_wave if c["claimed"]]
            self.assertEqual(len(second_winners), 1)
            att2_id = second_winners[0]["attempt_id"]
            self.assertNotEqual(att2_id, att1_id)
            self.assertEqual(second_winners[0]["attempt_number"], 2)
            self.assertEqual(second_winners[0]["fence_version"], 2)

            # Stale worker 1 cannot finalize OR renew, and cannot alter fence version
            with self.assertRaises(StaleAttemptError):
                repo.finalize_processing_attempt(
                    project_id="CI_CONC_TEST",
                    event_receipt_id=rec["receipt_id"],
                    idempotency_key=idem,
                    attempt_id=att1_id,
                    attempt_number=1,
                    attempt_status="SUCCEEDED",
                    final_key_status="COMPLETED",
                    result_summary={"stale": True},
                )
            with self.assertRaises(StaleAttemptError):
                repo.renew_attempt_lease(
                    project_id="CI_CONC_TEST",
                    idempotency_key=idem,
                    attempt_id=att1_id,
                    expected_fence_version=1,
                )
            with self.assertRaises(StaleAttemptError):
                repo.renew_attempt_lease(
                    project_id="CI_CONC_TEST",
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    expected_fence_version=1,
                )
            with psycopg.connect(runtime_cfg.dsn, **runtime_cfg.connect_kwargs()) as chk:
                with chk.cursor() as cur:
                    cur.execute(
                        "SELECT status, active_attempt_id, attempt_fence_version, expires_at FROM utda.idempotency_keys WHERE idempotency_key = %s",
                        (idem,),
                    )
                    before_status, before_att, before_ver, before_exp = cur.fetchone()
                    self.assertEqual((before_status, before_att, before_ver), ("IN_PROGRESS", att2_id, 2))

            # Concurrent lease renewal race: valid worker renewals succeed; stale worker renewals fail
            def _race_renew(worker_idx: int) -> str:
                if worker_idx % 2 == 0:
                    res = repo.renew_attempt_lease(
                        project_id="CI_CONC_TEST",
                        idempotency_key=idem,
                        attempt_id=att2_id,
                        expected_fence_version=2,
                        extend_ttl_seconds=1800,
                    )
                    return f"ok:{res['fence_version']}"
                try:
                    repo.renew_attempt_lease(
                        project_id="CI_CONC_TEST",
                        idempotency_key=idem,
                        attempt_id=att1_id,
                        expected_fence_version=1,
                    )
                    return "unexpected_stale_ok"
                except StaleAttemptError:
                    return "rejected_stale"

            with ThreadPoolExecutor(max_workers=4) as ex:
                race_outcomes = list(ex.map(_race_renew, range(4)))
            self.assertEqual(sorted(race_outcomes), ["ok:2", "ok:2", "rejected_stale", "rejected_stale"])

            # Active worker 2 lease renewal with matching attempt_id & fence_version atomically extends expiry
            ren_ok = repo.renew_attempt_lease(
                project_id="CI_CONC_TEST",
                idempotency_key=idem,
                attempt_id=att2_id,
                expected_fence_version=2,
                extend_ttl_seconds=1800,
            )
            self.assertTrue(ren_ok["renewed"])
            self.assertEqual(ren_ok["fence_version"], 2)
            with psycopg.connect(runtime_cfg.dsn, **runtime_cfg.connect_kwargs()) as chk:
                with chk.cursor() as cur:
                    cur.execute(
                        "SELECT expires_at, attempt_fence_version FROM utda.idempotency_keys WHERE idempotency_key = %s",
                        (idem,),
                    )
                    after_exp, after_ver = cur.fetchone()
                    self.assertGreater(after_exp, before_exp)
                    self.assertEqual(after_ver, 2)

            # Rollback & recovery inside transaction: fault mid-transaction leaves attempt 2 IN_PROGRESS
            with self.assertRaises(RuntimeError):
                def _failing_tx(conn) -> None:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE utda.processing_attempts SET error_code = 'MID_FAIL' WHERE attempt_id = %s",
                            (att2_id,),
                        )
                    raise RuntimeError("Simulated mid-transaction abort")

                db.run_in_transaction(_failing_tx)

            # Real PG16 ambiguous commit test: drop connection right after server COMMIT
            # 1) DatabaseManager.run_in_transaction raises CommitOutcomeUnknownError (0 generic retries)
            # 2) Fenced repository finalization reconciles safely via stable attempt_id!
            orig_commit_pool_fn = db._pool.connection

            class _DropOnceCommitConnProxy:
                def __init__(self, inner) -> None:
                    self._inner = inner
                    self._dropped = False

                def cursor(self, *args, **kwargs):
                    return self._inner.cursor(*args, **kwargs)

                def rollback(self):
                    return self._inner.rollback()

                def commit(self):
                    self._inner.commit()
                    if not self._dropped:
                        self._dropped = True
                        raise psycopg.OperationalError("Simulated PG16 post-commit socket drop")

            drop_state = {"armed": True}
            from contextlib import contextmanager as _cm

            @_cm
            def _drop_once_conn(timeout: float = 10.0):
                with orig_commit_pool_fn(timeout=timeout) as real_conn:
                    if drop_state["armed"]:
                        drop_state["armed"] = False
                        yield _DropOnceCommitConnProxy(real_conn)
                    else:
                        yield real_conn

            db._pool.connection = _drop_once_conn  # type: ignore[method-assign]
            try:
                fin_res = repo.finalize_processing_attempt(
                    project_id="CI_CONC_TEST",
                    event_receipt_id=rec["receipt_id"],
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    attempt_number=2,
                    attempt_status="SUCCEEDED",
                    final_key_status="COMPLETED",
                    result_summary={"access_token": "pg16-secret-tok", "status": "ok"},
                )
                self.assertEqual(fin_res, att2_id)
            finally:
                db._pool.connection = orig_commit_pool_fn  # type: ignore[method-assign]

            # Stale attempt or already-completed attempt cannot finalize or renew again
            with self.assertRaises(StaleAttemptError):
                repo.finalize_processing_attempt(
                    project_id="CI_CONC_TEST",
                    event_receipt_id=rec["receipt_id"],
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    attempt_number=2,
                    attempt_status="FAILED",
                    final_key_status="FAILED",
                )
            with self.assertRaises(StaleAttemptError):
                repo.renew_attempt_lease(
                    project_id="CI_CONC_TEST",
                    idempotency_key=idem,
                    attempt_id=att2_id,
                    expected_fence_version=2,
                )

            # Multiple recovery cycles maintain strictly monotonic fence_version progression
            # and completion preserves the active attempt's version without bump or reset.
            multi_key = "ci-pg16-multi-fence-1"
            observed_versions: list[int] = []
            c1 = repo.claim_idempotency_and_attempt(
                idempotency_key=multi_key,
                scope="phase1_correlation",
                project_id="CI_CONC_TEST",
                feature_id="FEAT-FENCE",
                report_blob_sha="blob-f",
                target_document_id="D06",
                event_receipt_id=rec["receipt_id"],
            )
            observed_versions.append(c1["fence_version"])
            with psycopg.connect(admin_cfg.dsn, **admin_cfg.connect_kwargs()) as aconn:
                with aconn.cursor() as cur:
                    cur.execute(
                        "UPDATE utda.idempotency_keys SET expires_at = NOW() - INTERVAL '1 minute' WHERE idempotency_key = %s",
                        (multi_key,),
                    )
                aconn.commit()

            c2 = repo.claim_idempotency_and_attempt(
                idempotency_key=multi_key,
                scope="phase1_correlation",
                project_id="CI_CONC_TEST",
                feature_id="FEAT-FENCE",
                report_blob_sha="blob-f",
                target_document_id="D06",
                event_receipt_id=rec["receipt_id"],
            )
            observed_versions.append(c2["fence_version"])
            repo.finalize_processing_attempt(
                project_id="CI_CONC_TEST",
                event_receipt_id=rec["receipt_id"],
                idempotency_key=multi_key,
                attempt_id=c2["attempt_id"],
                expected_fence_version=2,
                attempt_status="FAILED",
                final_key_status="FAILED",
                error_summary="recoverable failure",
            )

            c3 = repo.claim_idempotency_and_attempt(
                idempotency_key=multi_key,
                scope="phase1_correlation",
                project_id="CI_CONC_TEST",
                feature_id="FEAT-FENCE",
                report_blob_sha="blob-f",
                target_document_id="D06",
                event_receipt_id=rec["receipt_id"],
            )
            observed_versions.append(c3["fence_version"])
            with psycopg.connect(admin_cfg.dsn, **admin_cfg.connect_kwargs()) as aconn:
                with aconn.cursor() as cur:
                    cur.execute(
                        "UPDATE utda.idempotency_keys SET expires_at = NOW() - INTERVAL '1 minute' WHERE idempotency_key = %s",
                        (multi_key,),
                    )
                aconn.commit()

            c4 = repo.claim_idempotency_and_attempt(
                idempotency_key=multi_key,
                scope="phase1_correlation",
                project_id="CI_CONC_TEST",
                feature_id="FEAT-FENCE",
                report_blob_sha="blob-f",
                target_document_id="D06",
                event_receipt_id=rec["receipt_id"],
            )
            observed_versions.append(c4["fence_version"])
            self.assertEqual(observed_versions, [1, 2, 3, 4])

            repo.finalize_processing_attempt(
                project_id="CI_CONC_TEST",
                event_receipt_id=rec["receipt_id"],
                idempotency_key=multi_key,
                attempt_id=c4["attempt_id"],
                expected_fence_version=4,
                attempt_status="SUCCEEDED",
                final_key_status="COMPLETED",
                result_summary={"cycle": 4},
            )

        with psycopg.connect(runtime_cfg.dsn, **runtime_cfg.connect_kwargs()) as rconn:
            with rconn.cursor() as cur:
                cur.execute(
                    "SELECT attempt_number, status, retry_state, error_code FROM utda.processing_attempts WHERE idempotency_key = %s ORDER BY attempt_number",
                    (idem,),
                )
                rows = cur.fetchall()
                self.assertEqual(
                    rows,
                    [
                        (1, "FAILED", "LOCK_EXPIRED", "LOCK_EXPIRED"),
                        (2, "SUCCEEDED", "NONE", None),
                    ],
                )

                cur.execute(
                    "SELECT payload_summary::text FROM utda.github_event_receipts WHERE delivery_id = %s",
                    (delivery,),
                )
                payload_text = cur.fetchone()[0]
                self.assertNotIn("pg16-opaque-secret", payload_text)
                self.assertNotIn("ghp_1234567890123456789012345678901234", payload_text)

                cur.execute(
                    "SELECT status, active_attempt_id, attempt_fence_version, result_summary::text FROM utda.idempotency_keys WHERE idempotency_key = %s",
                    (idem,),
                )
                kstatus, kactive, kfence, ksummary = cur.fetchone()
                self.assertEqual(kstatus, "COMPLETED")
                self.assertIsNone(kactive)
                self.assertEqual(kfence, 2)
                self.assertNotIn("pg16-secret-tok", ksummary)

                cur.execute(
                    "SELECT status, active_attempt_id, attempt_fence_version FROM utda.idempotency_keys WHERE idempotency_key = %s",
                    (multi_key,),
                )
                self.assertEqual(cur.fetchone(), ("COMPLETED", None, 4))

        # Verify real PostgreSQL 16 transaction-local project context lifecycle across commit,
        # rollback, connection reuse, and global probes.
        tx_reuse_cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": os.environ["UTDA_TEST_RUNTIME_URL"],
                "UTDA_DATABASE_POOL_SIZE": "1",
                **({"UTDA_DATABASE_SSLMODE": os.environ["UTDA_DATABASE_SSLMODE"]} if os.getenv("UTDA_DATABASE_SSLMODE") else {}),
                **({"UTDA_ALLOW_INSECURE_LOCAL_DB": os.environ["UTDA_ALLOW_INSECURE_LOCAL_DB"]} if os.getenv("UTDA_ALLOW_INSECURE_LOCAL_DB") else {}),
            }
        )
        with DatabaseManager(tx_reuse_cfg) as single_db:
            single_repo = UTDADatabaseRepository(single_db)

            # 1 & 4. Project A context active inside tx on pool_size=1; record pg_backend_pid()
            pid_a, inside_ctx = single_db.run_in_project_transaction(
                "CI_CONC_TEST",
                lambda c: c.execute(
                    "SELECT pg_backend_pid(), current_setting('utda.current_project_id', true)"
                ).fetchone(),
            )
            self.assertEqual(inside_ctx, "CI_CONC_TEST")

            # 2 & 5. Reset after successful commit -> unscoped global transaction on SAME backend sees None
            pid_global_1, after_commit_global = single_db.run_in_transaction(
                lambda c: c.execute(
                    "SELECT pg_backend_pid(), NULLIF(current_setting('utda.current_project_id', true), '')"
                ).fetchone(),
            )
            self.assertEqual(pid_global_1, pid_a)
            self.assertIsNone(after_commit_global)

            # 6. Project A followed directly by Project B on SAME reused pool_size=1 connection
            pid_b, inside_b = single_db.run_in_project_transaction(
                "CI_SEC_TEST",
                lambda c: c.execute(
                    "SELECT pg_backend_pid(), current_setting('utda.current_project_id', true)"
                ).fetchone(),
            )
            self.assertEqual(pid_b, pid_a)
            self.assertEqual(inside_b, "CI_SEC_TEST")

            # 3. Context active inside aborted tx and resets on rollback
            with self.assertRaises(RuntimeError):
                def _abort_with_ctx(conn) -> None:
                    self.assertEqual(
                        conn.execute("SELECT current_setting('utda.current_project_id', true)").fetchone()[0],
                        "CI_SEC_TEST",
                    )
                    raise RuntimeError("abort to verify GUC reset on rollback")

                single_db.run_in_project_transaction("CI_SEC_TEST", _abort_with_ctx)

            pid_global_2, after_rollback_global = single_db.run_readonly(
                lambda c: c.execute(
                    "SELECT pg_backend_pid(), NULLIF(current_setting('utda.current_project_id', true), '')"
                ).fetchone(),
            )
            self.assertEqual(pid_global_2, pid_a)
            self.assertIsNone(after_rollback_global)

            # 7. Transaction retry behavior re-establishes project context cleanly across transient failure
            retry_counter = {"attempts": 0}

            def _retry_once_op(conn):
                retry_counter["attempts"] += 1
                pid_r, val_r = conn.execute(
                    "SELECT pg_backend_pid(), current_setting('utda.current_project_id', true)"
                ).fetchone()
                self.assertEqual(val_r, "CI_CONC_TEST")
                if retry_counter["attempts"] == 1:
                    raise psycopg.OperationalError("Simulated transient pre-commit failure on PG16")
                return pid_r, val_r

            pid_retry, val_retry = single_db.run_in_project_transaction("CI_CONC_TEST", _retry_once_op)
            self.assertEqual(retry_counter["attempts"], 2)
            self.assertEqual(pid_retry, pid_a)
            self.assertEqual(val_retry, "CI_CONC_TEST")
            self.assertIsNone(
                single_db.run_readonly(
                    lambda c: c.execute(
                        "SELECT NULLIF(current_setting('utda.current_project_id', true), '')"
                    ).fetchone()[0]
                )
            )

            # 8. Invalid or missing project_id fails BEFORE executing any SQL on real PG16
            for bad_pid in ("", None, "   ", "lowercase", " CI_CONC_TEST "):
                with self.assertRaises(DatabaseConfigError):
                    single_repo.get_feature_state(project_id=bad_pid, feature_id="FEAT-CONC")  # type: ignore[arg-type]
                with self.assertRaises(DatabaseConfigError):
                    single_db.run_in_project_transaction(bad_pid, lambda _c: None)  # type: ignore[arg-type]

    def test_transaction_local_project_context_and_strict_repo_scoping(self) -> None:
        state = _FakeDBState()
        state.tables_initialized = True

        # Valid project_id validation without silent normalization
        self.assertEqual(validate_project_id("UTDA_PILOT"), "UTDA_PILOT")
        self.assertEqual(validate_project_id("P01-ALPHA_2"), "P01-ALPHA_2")
        for bad_pid in ("", None, "   ", "utda_pilot", " UTDA_PILOT", "UTDA_PILOT ", "AB", "bad;id"):
            with self.assertRaises(DatabaseConfigError):
                validate_project_id(bad_pid)  # type: ignore[arg-type]

        # Test both Session Pooler (port 5432) and Transaction Pooler (port 6543) configs
        session_cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda_runtime:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
            }
        )
        tx_cfg = DatabaseConfig.from_env(
            {
                "UTDA_DATABASE_URL": "postgresql://utda_runtime:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
            }
        )

        for pool_cfg in (session_cfg, tx_cfg):
            pool = _FakePool(state, reuse_connections=True)
            with DatabaseManager(pool_cfg, pool_factory=lambda _c, p=pool: p, sleep_fn=lambda _s: None) as db:
                repo = UTDADatabaseRepository(db)

                # Invalid & missing project_ids rejected BEFORE acquiring any connection or SQL
                for bad_pid in ("", None, "   ", "utda_pilot", " UTDA_PILOT "):
                    with self.assertRaises(DatabaseConfigError):
                        with db.project_transaction(bad_pid):  # type: ignore[arg-type]
                            pass
                    with self.assertRaises(DatabaseConfigError):
                        db.run_in_project_transaction(bad_pid, lambda _c: None)  # type: ignore[arg-type]
                    with self.assertRaises(DatabaseConfigError):
                        db.run_project_readonly(bad_pid, lambda _c: None)  # type: ignore[arg-type]
                self.assertEqual(pool.connect_calls, 0)

                # Project context set BEFORE repository SQL inside transaction, and resets on commit
                repo.upsert_project(
                    project_id="PROJ_ONE",
                    project_name="Project One",
                    status="active",
                    github_owner="UNAI-TECH",
                    github_repo="proj-one",
                    default_branch="main",
                )
                reused_conn = pool.connections[0]
                self.assertEqual(len(pool.connections), 1)
                self.assertIsNone(reused_conn.current_project_id)
                set_idx = next(
                    i for i, sql in enumerate(reused_conn.executed_sql) if "set_config('utda.current_project_id'" in sql
                )
                insert_idx = next(
                    i for i, sql in enumerate(reused_conn.executed_sql) if '"projects"' in sql
                )
                self.assertLess(set_idx, insert_idx)
                self.assertIn(("SELECT set_config('utda.current_project_id', %s, true)", ("PROJ_ONE",)), reused_conn.executed_params)

                # Explicit global health probe runs on reused connection without setting project context
                sql_count_before_health = len(reused_conn.executed_sql)
                params_count_before_health = len(reused_conn.executed_params)
                health = check_database_health(db, read_only=True)
                self.assertEqual(health["status"], "healthy")
                self.assertEqual(len(reused_conn.executed_params), params_count_before_health)
                self.assertFalse(
                    any(
                        "set_config" in s
                        for s in reused_conn.executed_sql[sql_count_before_health:]
                    )
                )
                self.assertIsNone(reused_conn.current_project_id)

                # Context active inside rollback tx, and resets to None on rollback
                with self.assertRaises(RuntimeError):
                    def _rollback_check(conn) -> None:
                        cur = conn.cursor()
                        cur.execute("SELECT current_setting('utda.current_project_id', true)")
                        self.assertEqual(cur.fetchone()[0], "PROJ_TWO")
                        raise RuntimeError("force rollback")

                    db.run_in_transaction(_rollback_check, project_id="PROJ_TWO")
                self.assertIsNone(reused_conn.current_project_id)

                # Subsequent global transaction on SAME reused connection sees clean None context
                def _global_check(conn) -> str | None:
                    cur = conn.cursor()
                    cur.execute("SELECT current_setting('utda.current_project_id', true)")
                    return cur.fetchone()[0]

                self.assertIsNone(db.run_in_transaction(_global_check))
                self.assertIsNone(db.run_readonly(_global_check))

        # Every project-scoped repository method rejects omitted (TypeError) & empty/invalid project_id BEFORE SQL
        fresh_pool = _FakePool(state, reuse_connections=True)
        with DatabaseManager(session_cfg, pool_factory=lambda _c: fresh_pool, sleep_fn=lambda _s: None) as db:
            repo = UTDADatabaseRepository(db)
            omitted_calls = [
                lambda: repo.upsert_project(
                    project_name="X", status="active", github_owner="o", github_repo="r", default_branch="main"
                ),
                lambda: repo.record_event_receipt(
                    delivery_id="d1", event_name="push", repository="o/r", feature_id="F1", git_sha="abc1234"
                ),
                lambda: repo.correlate_feature_state(feature_id="F1"),
                lambda: repo.get_feature_state(feature_id="F1"),
                lambda: repo.claim_idempotency_and_attempt(
                    idempotency_key="k1", scope="s", feature_id="F1", report_blob_sha="b", target_document_id="D06", event_receipt_id=None
                ),
                lambda: repo.finalize_processing_attempt(
                    event_receipt_id=None, idempotency_key="k1", attempt_id="a1", attempt_status="SUCCEEDED", final_key_status="COMPLETED"
                ),
                lambda: repo.renew_attempt_lease(
                    idempotency_key="k1", attempt_id="a1", expected_fence_version=1
                ),
                lambda: repo.record_final_report(
                    feature_id="F1", feature_name="n", change_type="other", implementation_status="implemented",
                    report_path="p", report_blob_sha="b", git_commit_sha="abc1234", report_metadata={}, evidence_refs=[]
                ),
                lambda: repo.append_audit_event(actor="bot", event_type="EVT"),
                lambda: repo.acquire_idempotency_key(
                    idempotency_key="k1", scope="s", feature_id="F1", report_blob_sha="b", target_document_id="D06"
                ),
                lambda: repo.next_attempt_number(idempotency_key="k1"),
                lambda: repo.complete_idempotency_key(idempotency_key="k1", status="COMPLETED", attempt_id="a1"),
                lambda: repo.record_processing_attempt(
                    event_receipt_id=None, idempotency_key="k1", attempt_number=1, status="SUCCEEDED"
                ),
            ]
            for fn in omitted_calls:
                with self.assertRaises(TypeError):
                    fn()  # type: ignore[call-arg]

            for bad_pid in (None, "", "   ", "proj_lower", " PROJ_ONE "):
                invalid_calls = [
                    lambda p=bad_pid: repo.upsert_project(
                        project_id=p, project_name="X", status="active", github_owner="o", github_repo="r", default_branch="main"
                    ),
                    lambda p=bad_pid: repo.record_event_receipt(
                        delivery_id="d1", event_name="push", repository="o/r", project_id=p, feature_id="F1", git_sha="abc1234"
                    ),
                    lambda p=bad_pid: repo.correlate_feature_state(project_id=p, feature_id="F1"),
                    lambda p=bad_pid: repo.get_feature_state(project_id=p, feature_id="F1"),
                    lambda p=bad_pid: repo.claim_idempotency_and_attempt(
                        idempotency_key="k1", scope="s", project_id=p, feature_id="F1", report_blob_sha="b", target_document_id="D06", event_receipt_id=None
                    ),
                    lambda p=bad_pid: repo.finalize_processing_attempt(
                        project_id=p, event_receipt_id=None, idempotency_key="k1", attempt_id="a1", attempt_status="SUCCEEDED", final_key_status="COMPLETED"
                    ),
                    lambda p=bad_pid: repo.renew_attempt_lease(
                        project_id=p, idempotency_key="k1", attempt_id="a1", expected_fence_version=1
                    ),
                    lambda p=bad_pid: repo.record_final_report(
                        project_id=p, feature_id="F1", feature_name="n", change_type="other", implementation_status="implemented",
                        report_path="p", report_blob_sha="b", git_commit_sha="abc1234", report_metadata={}, evidence_refs=[]
                    ),
                    lambda p=bad_pid: repo.append_audit_event(actor="bot", event_type="EVT", project_id=p),
                ]
                for fn in invalid_calls:
                    with self.assertRaises(DatabaseConfigError):
                        fn()  # type: ignore[arg-type]

            self.assertEqual(fresh_pool.connect_calls, 0)

            # Cross-project mismatch check inside finalize_processing_attempt and renew_attempt_lease
            claim_p1 = repo.claim_idempotency_and_attempt(
                idempotency_key="scoped-key-p1",
                scope="phase2_check",
                project_id="PROJ_ONE",
                feature_id="FEAT-1",
                report_blob_sha="blob1",
                target_document_id="D06",
                event_receipt_id=None,
            )
            with self.assertRaises(IdempotencyIdentityMismatchError):
                repo.renew_attempt_lease(
                    project_id="PROJ_TWO",
                    idempotency_key="scoped-key-p1",
                    attempt_id=claim_p1["attempt_id"],
                    expected_fence_version=1,
                )
            with self.assertRaises(IdempotencyIdentityMismatchError):
                repo.finalize_processing_attempt(
                    project_id="PROJ_TWO",
                    event_receipt_id=None,
                    idempotency_key="scoped-key-p1",
                    attempt_id=claim_p1["attempt_id"],
                    attempt_status="SUCCEEDED",
                    final_key_status="COMPLETED",
                )
            # Original PROJ_ONE state remains intact and finalizes cleanly
            self.assertEqual(state.idempotency_keys["scoped-key-p1"]["status"], "IN_PROGRESS")
            repo.finalize_processing_attempt(
                project_id="PROJ_ONE",
                event_receipt_id=None,
                idempotency_key="scoped-key-p1",
                attempt_id=claim_p1["attempt_id"],
                expected_fence_version=claim_p1["fence_version"],
                attempt_status="SUCCEEDED",
                final_key_status="COMPLETED",
            )
            self.assertEqual(state.idempotency_keys["scoped-key-p1"]["status"], "COMPLETED")

    @unittest.skipUnless(
        os.getenv("UTDA_RUN_LIVE_SUPABASE_TESTS") == "true" and os.getenv("UTDA_DATABASE_URL"),
        "PENDING: Live Supabase verification requires UTDA_DATABASE_URL and UTDA_RUN_LIVE_SUPABASE_TESTS=true",
    )
    def test_live_supabase_readonly_health_check(self) -> None:
        from engine.db_health import verify_live_supabase_readonly

        report = verify_live_supabase_readonly()
        self.assertTrue(report["verified"])
        self.assertTrue(report["read_only"])
        self.assertIn(report["status"], ("healthy", "warning", "critical"))


if __name__ == "__main__":
    unittest.main()

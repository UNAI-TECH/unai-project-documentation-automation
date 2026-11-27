"""PostgreSQL / Supabase database connection manager and Phase 1 persistence store.

Provides pooled connections, retry handling for transient failures, atomic
transactions with rollback on errors, graceful shutdown, and durable Phase 1
persistence for event receipts, feature correlation, idempotency, and audits.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import random
import time
from typing import Any, Callable, Iterator, TypeVar
import uuid

from engine.db_config import (
    DatabaseConfig,
    configure_sanitized_logger,
    quote_ident,
    sanitize_log_text,
    validate_identifier,
)

T = TypeVar("T")

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    PSYCOPG_AVAILABLE = True
    TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
        psycopg.OperationalError,
        psycopg.InterfaceError,
    )
except ImportError:  # pragma: no cover - handled cleanly when driver absent
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment]
    PSYCOPG_AVAILABLE = False
    TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)


class DatabaseUnavailableError(RuntimeError):
    """Raised when PostgreSQL cannot be reached after configured retries."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_driver() -> None:
    if not PSYCOPG_AVAILABLE:
        raise RuntimeError(
            "psycopg and psycopg_pool are required. Install with: pip install -r requirements.txt"
        )


def _default_pool_factory(config: DatabaseConfig) -> Any:
    _require_driver()
    return ConnectionPool(
        conninfo=config.dsn,
        min_size=1,
        max_size=config.pool_size,
        timeout=float(config.connect_timeout),
        kwargs=config.connect_kwargs(for_migration=False),
        open=False,
    )


class DatabaseManager:
    """Manages PostgreSQL connection pooling, retries, and atomic transactions."""

    def __init__(
        self,
        config: DatabaseConfig,
        pool_factory: Callable[[DatabaseConfig], Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._pool_factory = pool_factory or _default_pool_factory
        self._sleep = sleep_fn
        self._pool: Any = None
        self._closed = False
        self.logger = configure_sanitized_logger("utda.database")

    @property
    def is_open(self) -> bool:
        return self._pool is not None and not self._closed

    def open(self) -> None:
        """Initialize and open the connection pool."""
        if self._pool is not None and not self._closed:
            return
        self._closed = False
        self._pool = self._pool_factory(self.config)
        self._pool.open(wait=True, timeout=float(self.config.connect_timeout))
        atexit.register(self.close)
        self.logger.info("Database pool opened: %s", self.config.redacted_summary())

    def close(self) -> None:
        """Gracefully close the connection pool."""
        if self._pool is not None and not self._closed:
            try:
                self._pool.close()
            finally:
                self._closed = True
                self.logger.info("Database pool closed cleanly.")

    def __enter__(self) -> "DatabaseManager":
        self.open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _ensure_open(self) -> Any:
        if self._pool is None or self._closed:
            self.open()
        return self._pool

    def execute_with_retry(self, operation: Callable[[], T]) -> T:
        """Execute an operation with bounded exponential backoff on transient errors."""
        attempts = self.config.max_retries + 1
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return operation()
            except TRANSIENT_ERRORS as exc:
                last_error = exc
                self.logger.warning(
                    "Transient database error on attempt %d/%d: %s",
                    attempt,
                    attempts,
                    sanitize_log_text(str(exc)),
                )
                if attempt >= attempts:
                    break
                delay = min(0.25 * (2 ** (attempt - 1)) + random.uniform(0.0, 0.1), 5.0)
                self._sleep(delay)
        raise DatabaseUnavailableError(
            f"Database operation failed after {attempts} attempts."
        ) from last_error

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Yield a pooled connection."""
        pool = self._ensure_open()
        with pool.connection(timeout=float(self.config.connect_timeout)) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Acquire a connection, apply statement timeout, commit or roll back atomically."""
        with self.connection() as conn:
            committed = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SET LOCAL statement_timeout = {int(self.config.statement_timeout_ms)}"
                    )
                yield conn
                conn.commit()
                committed = True
            finally:
                if not committed:
                    try:
                        conn.rollback()
                    except Exception as rb_err:
                        self.logger.error(
                            "Rollback encountered an error: %s",
                            sanitize_log_text(str(rb_err)),
                        )

    def run_in_transaction(self, fn: Callable[[Any], T]) -> T:
        """Run a transactional callback with transient connection retry safety."""
        def _attempt() -> T:
            with self.transaction() as conn:
                return fn(conn)

        return self.execute_with_retry(_attempt)


class UTDADatabaseRepository:
    """Durable Phase 1 PostgreSQL persistence operations (schema-qualified)."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self.schema = validate_identifier(db.config.schema, kind="schema")

    def _tbl(self, name: str) -> str:
        return f"{quote_ident(self.schema, kind='schema')}.{quote_ident(name, kind='table')}"

    def upsert_project(
        self,
        *,
        project_id: str,
        project_name: str,
        status: str,
        github_owner: str,
        github_repo: str,
        default_branch: str,
        drive_folder_id: str = "",
        template_version: str = "",
        registry_metadata: dict[str, Any] | None = None,
    ) -> None:
        sql = f"""
            INSERT INTO {self._tbl('projects')} (
                project_id, project_name, status, github_owner, github_repo,
                default_branch, drive_folder_id, template_version, registry_metadata,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
            ON CONFLICT (project_id) DO UPDATE SET
                project_name = EXCLUDED.project_name,
                status = EXCLUDED.status,
                github_owner = EXCLUDED.github_owner,
                github_repo = EXCLUDED.github_repo,
                default_branch = EXCLUDED.default_branch,
                drive_folder_id = EXCLUDED.drive_folder_id,
                template_version = EXCLUDED.template_version,
                registry_metadata = EXCLUDED.registry_metadata,
                updated_at = NOW()
        """
        payload = json.dumps(registry_metadata or {})
        def _op(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        project_id,
                        project_name,
                        status,
                        github_owner,
                        github_repo,
                        default_branch,
                        drive_folder_id,
                        template_version,
                        payload,
                    ),
                )
        self.db.run_in_transaction(_op)

    def record_event_receipt(
        self,
        *,
        delivery_id: str,
        event_name: str,
        repository: str,
        project_id: str | None,
        feature_id: str | None,
        git_sha: str,
        pr_number: int | None = None,
        report_blob_sha: str | None = None,
        payload_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a GitHub event receipt idempotently by delivery_id."""
        receipt_id = str(uuid.uuid4())
        sql = f"""
            INSERT INTO {self._tbl('github_event_receipts')} (
                receipt_id, delivery_id, event_name, repository, project_id,
                feature_id, git_sha, pr_number, report_blob_sha, payload_summary
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING receipt_id
        """
        summary_json = json.dumps(payload_summary or {})

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        receipt_id,
                        delivery_id,
                        event_name,
                        repository,
                        project_id,
                        feature_id,
                        git_sha,
                        pr_number,
                        report_blob_sha,
                        summary_json,
                    ),
                )
                row = cur.fetchone()
                return {
                    "inserted": row is not None,
                    "receipt_id": row[0] if row else None,
                    "delivery_id": delivery_id,
                }

        return self.db.run_in_transaction(_op)

    def correlate_feature_state(
        self,
        *,
        project_id: str,
        feature_id: str,
        pr_number: int | None = None,
        pr_merged_sha: str | None = None,
        report_blob_sha: str | None = None,
        force_status: str | None = None,
    ) -> dict[str, Any]:
        """Correlate PR merge and canonical JSON report in either arrival order."""
        state_id = str(uuid.uuid4())
        sql = f"""
            INSERT INTO {self._tbl('feature_processing_states')} (
                state_id, project_id, feature_id, status,
                pr_number, pr_merged_sha, report_blob_sha, correlated_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s::text IS NOT NULL AND %s::text IS NOT NULL THEN NOW() ELSE NULL END,
                NOW()
            )
            ON CONFLICT (project_id, feature_id) DO UPDATE SET
                pr_number = COALESCE(EXCLUDED.pr_number, {self._tbl('feature_processing_states')}.pr_number),
                pr_merged_sha = COALESCE(EXCLUDED.pr_merged_sha, {self._tbl('feature_processing_states')}.pr_merged_sha),
                report_blob_sha = COALESCE(EXCLUDED.report_blob_sha, {self._tbl('feature_processing_states')}.report_blob_sha),
                status = CASE
                    WHEN %s::text IS NOT NULL THEN %s::text
                    WHEN COALESCE(EXCLUDED.pr_merged_sha, {self._tbl('feature_processing_states')}.pr_merged_sha) IS NOT NULL
                     AND COALESCE(EXCLUDED.report_blob_sha, {self._tbl('feature_processing_states')}.report_blob_sha) IS NOT NULL
                    THEN 'READY'
                    WHEN COALESCE(EXCLUDED.pr_merged_sha, {self._tbl('feature_processing_states')}.pr_merged_sha) IS NOT NULL
                    THEN 'WAITING_FOR_FINAL_REPORT'
                    ELSE 'AWAIT_PR_MERGE'
                END,
                correlated_at = CASE
                    WHEN COALESCE(EXCLUDED.pr_merged_sha, {self._tbl('feature_processing_states')}.pr_merged_sha) IS NOT NULL
                     AND COALESCE(EXCLUDED.report_blob_sha, {self._tbl('feature_processing_states')}.report_blob_sha) IS NOT NULL
                    THEN COALESCE({self._tbl('feature_processing_states')}.correlated_at, NOW())
                    ELSE {self._tbl('feature_processing_states')}.correlated_at
                END,
                updated_at = NOW()
            RETURNING state_id, status, pr_merged_sha, report_blob_sha
        """
        initial_status = force_status or (
            "READY"
            if (pr_merged_sha and report_blob_sha)
            else ("WAITING_FOR_FINAL_REPORT" if pr_merged_sha else "AWAIT_PR_MERGE")
        )

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        state_id,
                        project_id,
                        feature_id,
                        initial_status,
                        pr_number,
                        pr_merged_sha,
                        report_blob_sha,
                        pr_merged_sha,
                        report_blob_sha,
                        force_status,
                        force_status,
                    ),
                )
                row = cur.fetchone()
                return {
                    "state_id": row[0],
                    "status": row[1],
                    "pr_merged_sha": row[2],
                    "report_blob_sha": row[3],
                }

        return self.db.run_in_transaction(_op)

    def acquire_idempotency_key(
        self,
        *,
        idempotency_key: str,
        scope: str,
        project_id: str,
        feature_id: str,
        report_blob_sha: str,
        target_document_id: str,
        lock_ttl_seconds: int = 900,
    ) -> bool:
        """Atomically claim an idempotency key or recover a FAILED/expired lock."""
        tbl = self._tbl("idempotency_keys")
        sql = f"""
            INSERT INTO {tbl} (
                idempotency_key, scope, project_id, feature_id,
                report_blob_sha, target_document_id, status, locked_at, expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, 'IN_PROGRESS',
                NOW(),
                NOW() + (%s::integer * INTERVAL '1 second')
            )
            ON CONFLICT (idempotency_key) DO UPDATE SET
                status = 'IN_PROGRESS',
                locked_at = NOW(),
                expires_at = NOW() + (%s::integer * INTERVAL '1 second')
            WHERE {tbl}.status = 'FAILED'
               OR (
                   {tbl}.status = 'IN_PROGRESS'
                   AND {tbl}.expires_at IS NOT NULL
                   AND {tbl}.expires_at < NOW()
               )
            RETURNING idempotency_key
        """

        def _op(conn: Any) -> bool:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        idempotency_key,
                        scope,
                        project_id,
                        feature_id,
                        report_blob_sha,
                        target_document_id,
                        lock_ttl_seconds,
                        lock_ttl_seconds,
                    ),
                )
                return cur.fetchone() is not None

        return self.db.run_in_transaction(_op)

    def get_feature_state(
        self, *, project_id: str, feature_id: str
    ) -> dict[str, Any] | None:
        """Return current correlation state for (project_id, feature_id)."""
        sql = f"""
            SELECT state_id, status, pr_number, pr_merged_sha, report_blob_sha
            FROM {self._tbl('feature_processing_states')}
            WHERE project_id = %s AND feature_id = %s
        """

        def _op(conn: Any) -> dict[str, Any] | None:
            with conn.cursor() as cur:
                cur.execute(sql, (project_id, feature_id))
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "state_id": row[0],
                    "status": row[1],
                    "pr_number": row[2],
                    "pr_merged_sha": row[3],
                    "report_blob_sha": row[4],
                }

        return self.db.run_in_transaction(_op)

    def next_attempt_number(self, *, idempotency_key: str) -> int:
        """Return the next sequential attempt number for an idempotency key."""
        sql = f"""
            SELECT COALESCE(MAX(attempt_number), 0) + 1
            FROM {self._tbl('processing_attempts')}
            WHERE idempotency_key = %s
        """

        def _op(conn: Any) -> int:
            with conn.cursor() as cur:
                cur.execute(sql, (idempotency_key,))
                row = cur.fetchone()
                return int(row[0]) if row else 1

        return self.db.run_in_transaction(_op)

    def complete_idempotency_key(
        self,
        *,
        idempotency_key: str,
        status: str,
        result_summary: dict[str, Any] | None = None,
    ) -> None:
        sql = f"""
            UPDATE {self._tbl('idempotency_keys')}
            SET status = %s,
                completed_at = NOW(),
                result_summary = %s::jsonb
            WHERE idempotency_key = %s
        """
        summary_json = json.dumps(result_summary or {})

        def _op(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(sql, (status, summary_json, idempotency_key))

        self.db.run_in_transaction(_op)

    def record_processing_attempt(
        self,
        *,
        event_receipt_id: str | None,
        idempotency_key: str,
        attempt_number: int,
        status: str,
        retry_state: str = "NONE",
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> str:
        attempt_id = str(uuid.uuid4())
        sql = f"""
            INSERT INTO {self._tbl('processing_attempts')} (
                attempt_id, event_receipt_id, idempotency_key, attempt_number,
                status, retry_state, error_code, error_summary, started_at, finished_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        """
        safe_error = sanitize_log_text(error_summary) if error_summary else None

        def _op(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        attempt_id,
                        event_receipt_id,
                        idempotency_key,
                        attempt_number,
                        status,
                        retry_state,
                        error_code,
                        safe_error,
                    ),
                )
                return attempt_id

        return self.db.run_in_transaction(_op)

    def record_final_report(
        self,
        *,
        project_id: str,
        feature_id: str,
        feature_name: str,
        change_type: str,
        implementation_status: str,
        report_path: str,
        report_blob_sha: str,
        git_commit_sha: str,
        report_metadata: dict[str, Any],
        evidence_refs: list[Any],
    ) -> dict[str, Any]:
        """Persist validated canonical JSON report metadata idempotently by blob SHA."""
        report_id = str(uuid.uuid4())
        sql = f"""
            INSERT INTO {self._tbl('final_reports')} (
                report_id, project_id, feature_id, feature_name, change_type,
                implementation_status, report_path, report_blob_sha, git_commit_sha,
                report_metadata, evidence_refs
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (project_id, feature_id, report_blob_sha) DO NOTHING
            RETURNING report_id
        """
        meta_json = sanitize_log_text(json.dumps(report_metadata))
        ev_json = sanitize_log_text(json.dumps(evidence_refs))

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        report_id,
                        project_id,
                        feature_id,
                        feature_name,
                        change_type,
                        implementation_status,
                        report_path,
                        report_blob_sha,
                        git_commit_sha,
                        meta_json,
                        ev_json,
                    ),
                )
                row = cur.fetchone()
                return {
                    "inserted": row is not None,
                    "report_id": row[0] if row else None,
                }

        return self.db.run_in_transaction(_op)

    def append_audit_event(
        self,
        *,
        actor: str,
        event_type: str,
        project_id: str | None = None,
        feature_id: str | None = None,
        document_id: str | None = None,
        idempotency_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        sql = f"""
            INSERT INTO {self._tbl('audit_events')} (
                event_id, actor, event_type, project_id, feature_id,
                document_id, idempotency_key, details
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """
        safe_details = sanitize_log_text(json.dumps(details or {}))

        def _op(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        event_id,
                        actor,
                        event_type,
                        project_id,
                        feature_id,
                        document_id,
                        idempotency_key,
                        safe_details,
                    ),
                )
                return event_id

        return self.db.run_in_transaction(_op)

"""PostgreSQL / Supabase database connection manager and persistence store.

Provides pooled connections, pre-commit retry handling for transient failures,
fail-closed CommitOutcomeUnknownError on ambiguous commits with safe stable-ID
reconciliation, atomic fenced idempotency claims/finalization, immutable
request-identity verification, and transaction-local project context scoping.

Security boundary note:
    The transaction-local GUC `utda.current_project_id` set via
    `SELECT set_config('utda.current_project_id', %s, true)` is a
    project-scoping context, NOT an authenticated tenant identity. It protects
    against accidental unscoped queries when combined with application validation
    (and future database RLS policies), but it does not independently defend
    against arbitrary SQL executed under a shared runtime credential.
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
import warnings

from engine.db_config import (
    DatabaseConfig,
    quote_ident,
    validate_identifier,
    validate_project_id,
)
from engine.sanitization import (
    configure_sanitized_logger,
    sanitize_log_text,
    sanitize_value,
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


class CommitOutcomeUnknownError(RuntimeError):
    """Raised when a connection drops during COMMIT and commit outcome is ambiguous."""


class StaleAttemptError(RuntimeError):
    """Raised when a stale, expired, or mismatched attempt tries to finalize a lock."""


class IdempotencyIdentityMismatchError(ValueError):
    """Raised when an existing idempotency_key is reused with different request attributes."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_driver() -> None:
    if not PSYCOPG_AVAILABLE:
        raise RuntimeError(
            "psycopg and psycopg_pool are required. Install with: pip install -r requirements.txt"
        )


def set_transaction_project_context(cur: Any, project_id: str) -> str:
    """Validate and set transaction-local `utda.current_project_id` via set_config.

    Security boundary note: `utda.current_project_id` is a transaction-scoped
    project context (reset automatically on COMMIT or ROLLBACK), not an
    authenticated tenant identity. It guards against accidental unscoped
    queries but cannot alone block arbitrary SQL run by the shared runtime role.
    """
    validated = validate_project_id(project_id)
    cur.execute(
        "SELECT set_config('utda.current_project_id', %s, true)",
        (validated,),
    )
    return validated


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
    """Manages PostgreSQL connection pooling, pre-commit retries, and atomic transactions."""

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
        """Execute an operation with bounded exponential backoff on pre-commit transient errors."""
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
    def transaction(self, *, project_id: str | None = None) -> Iterator[Any]:
        """Acquire a connection, apply statement timeout & optional project GUC, commit/rollback.

        When `project_id` is supplied, validates it before acquiring a connection and sets
        transaction-local `utda.current_project_id` (`is_local=true`) inside the transaction
        before yielding to callers. Explicitly global probes leave `project_id=None`.
        Never retries generically after an ambiguous commit failure; raises
        CommitOutcomeUnknownError instead so callers can reconcile via stable IDs.
        """
        validated_pid = validate_project_id(project_id) if project_id is not None else None
        with self.connection() as conn:
            committed = False
            in_commit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SET LOCAL statement_timeout = {int(self.config.statement_timeout_ms)}"
                    )
                    if validated_pid is not None:
                        set_transaction_project_context(cur, validated_pid)
                yield conn
                in_commit = True
                conn.commit()
                committed = True
            except TRANSIENT_ERRORS as exc:
                if in_commit:
                    self.logger.error(
                        "Ambiguous commit outcome after transient error: %s",
                        sanitize_log_text(str(exc)),
                    )
                    raise CommitOutcomeUnknownError(
                        "Database connection failed during commit; outcome is ambiguous."
                    ) from exc
                raise
            finally:
                if not committed:
                    try:
                        conn.rollback()
                    except Exception as rb_err:
                        self.logger.error(
                            "Rollback encountered an error: %s",
                            sanitize_log_text(str(rb_err)),
                        )

    def run_in_transaction(
        self,
        fn: Callable[[Any], T],
        *,
        project_id: str | None = None,
    ) -> T:
        """Run a transactional callback with pre-commit transient retry safety."""
        validated_pid = validate_project_id(project_id) if project_id is not None else None

        def _attempt() -> T:
            with self.transaction(project_id=validated_pid) as conn:
                return fn(conn)

        return self.execute_with_retry(_attempt)

    def run_readonly(
        self,
        fn: Callable[[Any], T],
        *,
        project_id: str | None = None,
    ) -> T:
        """Execute a read-only reconciliation/lookup query with clean rollback."""
        validated_pid = validate_project_id(project_id) if project_id is not None else None

        def _attempt() -> T:
            with self.connection() as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SET LOCAL statement_timeout = {int(self.config.statement_timeout_ms)}"
                        )
                        if validated_pid is not None:
                            set_transaction_project_context(cur, validated_pid)
                    return fn(conn)
                finally:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

        return self.execute_with_retry(_attempt)

    @contextmanager
    def project_transaction(self, project_id: str) -> Iterator[Any]:
        """Require a validated canonical project_id and open a project-scoped transaction."""
        validated_pid = validate_project_id(project_id)
        with self.transaction(project_id=validated_pid) as conn:
            yield conn

    def run_in_project_transaction(
        self, project_id: str, fn: Callable[[Any], T]
    ) -> T:
        """Require a validated canonical project_id and execute inside a project-scoped tx."""
        validated_pid = validate_project_id(project_id)
        return self.run_in_transaction(fn, project_id=validated_pid)

    def run_project_readonly(
        self, project_id: str, fn: Callable[[Any], T]
    ) -> T:
        """Require a validated canonical project_id and execute a project-scoped read-only lookup."""
        validated_pid = validate_project_id(project_id)
        return self.run_readonly(fn, project_id=validated_pid)


class UTDADatabaseRepository:
    """Durable Phase 1 PostgreSQL persistence operations with active-attempt fencing."""

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
        pid = validate_project_id(project_id)
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
        payload = json.dumps(sanitize_value(registry_metadata or {}))

        def _op(conn: Any) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        pid,
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

        try:
            self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            # Reconcile against persisted project row by stable primary key project_id.
            def _check(conn: Any) -> tuple[Any, ...] | None:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT project_name, status, github_owner, github_repo,
                               default_branch, drive_folder_id, template_version
                        FROM {self._tbl('projects')}
                        WHERE project_id = %s
                        """,
                        (pid,),
                    )
                    return cur.fetchone()

            existing = self.db.run_project_readonly(pid, _check)
            target = (
                project_name,
                status,
                github_owner,
                github_repo,
                default_branch,
                drive_folder_id,
                template_version,
            )
            if existing == target:
                return
            self.db.run_in_project_transaction(pid, _op)

    def record_event_receipt(
        self,
        *,
        delivery_id: str,
        event_name: str,
        repository: str,
        project_id: str,
        feature_id: str | None,
        git_sha: str,
        pr_number: int | None = None,
        report_blob_sha: str | None = None,
        payload_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a GitHub event receipt idempotently and return its canonical receipt_id."""
        pid = validate_project_id(project_id)
        receipt_id = str(uuid.uuid4())
        tbl = self._tbl("github_event_receipts")
        insert_sql = f"""
            INSERT INTO {tbl} (
                receipt_id, delivery_id, event_name, repository, project_id,
                feature_id, git_sha, pr_number, report_blob_sha, payload_summary
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING receipt_id
        """
        select_sql = f"SELECT receipt_id FROM {tbl} WHERE delivery_id = %s"
        summary_json = json.dumps(sanitize_value(payload_summary or {}))

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    insert_sql,
                    (
                        receipt_id,
                        delivery_id,
                        event_name,
                        repository,
                        pid,
                        feature_id,
                        git_sha,
                        pr_number,
                        report_blob_sha,
                        summary_json,
                    ),
                )
                row = cur.fetchone()
                if row is not None:
                    return {
                        "inserted": True,
                        "receipt_id": row[0],
                        "delivery_id": delivery_id,
                    }
                cur.execute(select_sql, (delivery_id,))
                existing = cur.fetchone()
                return {
                    "inserted": False,
                    "receipt_id": existing[0] if existing else None,
                    "delivery_id": delivery_id,
                }

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            def _reconcile(conn: Any) -> str | None:
                with conn.cursor() as cur:
                    cur.execute(select_sql, (delivery_id,))
                    row = cur.fetchone()
                    return row[0] if row else None

            existing_id = self.db.run_project_readonly(pid, _reconcile)
            if existing_id is not None:
                return {
                    "inserted": existing_id == receipt_id,
                    "receipt_id": existing_id,
                    "delivery_id": delivery_id,
                    "reconciled": True,
                }
            return self.db.run_in_project_transaction(pid, _op)

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
        pid = validate_project_id(project_id)
        state_id = str(uuid.uuid4())
        tbl = self._tbl("feature_processing_states")
        sql = f"""
            INSERT INTO {tbl} (
                state_id, project_id, feature_id, status,
                pr_number, pr_merged_sha, report_blob_sha, correlated_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s::text IS NOT NULL AND %s::text IS NOT NULL THEN NOW() ELSE NULL END,
                NOW()
            )
            ON CONFLICT (project_id, feature_id) DO UPDATE SET
                pr_number = COALESCE(EXCLUDED.pr_number, {tbl}.pr_number),
                pr_merged_sha = COALESCE(EXCLUDED.pr_merged_sha, {tbl}.pr_merged_sha),
                report_blob_sha = COALESCE(EXCLUDED.report_blob_sha, {tbl}.report_blob_sha),
                status = CASE
                    WHEN %s::text IS NOT NULL THEN %s::text
                    WHEN COALESCE(EXCLUDED.pr_merged_sha, {tbl}.pr_merged_sha) IS NOT NULL
                     AND COALESCE(EXCLUDED.report_blob_sha, {tbl}.report_blob_sha) IS NOT NULL
                    THEN 'READY'
                    WHEN COALESCE(EXCLUDED.pr_merged_sha, {tbl}.pr_merged_sha) IS NOT NULL
                    THEN 'WAITING_FOR_FINAL_REPORT'
                    ELSE 'AWAIT_PR_MERGE'
                END,
                correlated_at = CASE
                    WHEN COALESCE(EXCLUDED.pr_merged_sha, {tbl}.pr_merged_sha) IS NOT NULL
                     AND COALESCE(EXCLUDED.report_blob_sha, {tbl}.report_blob_sha) IS NOT NULL
                    THEN COALESCE({tbl}.correlated_at, NOW())
                    ELSE {tbl}.correlated_at
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
                        pid,
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

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            current = self.get_feature_state(project_id=pid, feature_id=feature_id)
            if current is not None:
                pr_ok = (pr_merged_sha is None) or (current["pr_merged_sha"] == pr_merged_sha)
                rep_ok = (report_blob_sha is None) or (current["report_blob_sha"] == report_blob_sha)
                status_ok = (force_status is None) or (current["status"] == force_status)
                if pr_ok and rep_ok and status_ok:
                    return {
                        "state_id": current["state_id"],
                        "status": current["status"],
                        "pr_merged_sha": current["pr_merged_sha"],
                        "report_blob_sha": current["report_blob_sha"],
                        "reconciled": True,
                    }
            return self.db.run_in_project_transaction(pid, _op)

    def get_feature_state(
        self, *, project_id: str, feature_id: str
    ) -> dict[str, Any] | None:
        """Return current correlation state for (project_id, feature_id)."""
        pid = validate_project_id(project_id)
        sql = f"""
            SELECT state_id, status, pr_number, pr_merged_sha, report_blob_sha
            FROM {self._tbl('feature_processing_states')}
            WHERE project_id = %s AND feature_id = %s
        """

        def _op(conn: Any) -> dict[str, Any] | None:
            with conn.cursor() as cur:
                cur.execute(sql, (pid, feature_id))
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

        return self.db.run_in_project_transaction(pid, _op)

    def claim_idempotency_and_attempt(
        self,
        *,
        idempotency_key: str,
        scope: str,
        project_id: str,
        feature_id: str,
        report_blob_sha: str,
        target_document_id: str,
        event_receipt_id: str | None,
        lock_ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        """Atomically claim fenced idempotency lock, verify immutable identity, and return attempt_id."""
        pid = validate_project_id(project_id)
        if lock_ttl_seconds <= 0:
            raise ValueError("lock_ttl_seconds must be positive.")

        keys_tbl = self._tbl("idempotency_keys")
        attempts_tbl = self._tbl("processing_attempts")
        attempt_id = str(uuid.uuid4())
        requested_identity = (
            scope,
            pid,
            feature_id,
            report_blob_sha,
            target_document_id,
        )

        insert_seed_sql = f"""
            INSERT INTO {keys_tbl} (
                idempotency_key, scope, project_id, feature_id,
                report_blob_sha, target_document_id, status,
                active_attempt_id, attempt_fence_version, locked_at, expires_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, 'IN_PROGRESS',
                NULL, 0, NOW(),
                NOW() + (%s::integer * INTERVAL '1 second')
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING idempotency_key
        """
        lock_key_sql = f"""
            SELECT scope, project_id, feature_id, report_blob_sha, target_document_id,
                   status, active_attempt_id, attempt_fence_version,
                   (expires_at IS NOT NULL AND expires_at < NOW()) AS is_expired
            FROM {keys_tbl}
            WHERE idempotency_key = %s
            FOR UPDATE
        """
        retire_sql = f"""
            UPDATE {attempts_tbl}
            SET status = 'FAILED',
                retry_state = 'LOCK_EXPIRED',
                error_code = COALESCE(error_code, 'LOCK_EXPIRED'),
                error_summary = COALESCE(error_summary, 'Abandoned prior IN_PROGRESS attempt expired.'),
                finished_at = NOW()
            WHERE idempotency_key = %s
              AND status = 'IN_PROGRESS'
        """
        alloc_sql = f"""
            INSERT INTO {attempts_tbl} (
                attempt_id, event_receipt_id, idempotency_key, attempt_number,
                status, retry_state, started_at
            )
            SELECT
                %s, %s, %s, COALESCE(MAX(attempt_number), 0) + 1,
                'IN_PROGRESS', 'NONE', NOW()
            FROM {attempts_tbl}
            WHERE idempotency_key = %s
            RETURNING attempt_id, attempt_number
        """
        fence_sql = f"""
            UPDATE {keys_tbl}
            SET status = 'IN_PROGRESS',
                active_attempt_id = %s,
                attempt_fence_version = attempt_fence_version + 1,
                locked_at = NOW(),
                expires_at = NOW() + (%s::integer * INTERVAL '1 second')
            WHERE idempotency_key = %s
            RETURNING attempt_fence_version
        """

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    insert_seed_sql,
                    (
                        idempotency_key,
                        scope,
                        pid,
                        feature_id,
                        report_blob_sha,
                        target_document_id,
                        lock_ttl_seconds,
                    ),
                )
                inserted_new = cur.fetchone() is not None

                cur.execute(lock_key_sql, (idempotency_key,))
                key_row = cur.fetchone()
                if key_row is None:
                    if not inserted_new:
                        raise IdempotencyIdentityMismatchError(
                            "Idempotency key identity mismatch for key "
                            f"'{sanitize_log_text(idempotency_key)}': key conflicts with another project."
                        )
                    raise RuntimeError(f"Idempotency key '{idempotency_key}' vanished during claim.")

                (
                    row_scope,
                    row_project_id,
                    row_feature_id,
                    row_blob_sha,
                    row_target_doc_id,
                    row_status,
                    row_active_attempt_id,
                    row_fence_version,
                    row_is_expired,
                ) = key_row

                stored_identity = (
                    row_scope,
                    row_project_id,
                    row_feature_id,
                    row_blob_sha,
                    row_target_doc_id,
                )
                if stored_identity != requested_identity:
                    raise IdempotencyIdentityMismatchError(
                        "Idempotency key identity mismatch for key "
                        f"'{sanitize_log_text(idempotency_key)}': existing request metadata is immutable."
                    )

                if not inserted_new:
                    reclaimable = (row_status == "FAILED") or (
                        row_status == "IN_PROGRESS" and bool(row_is_expired)
                    )
                    if not reclaimable:
                        return {
                            "claimed": False,
                            "attempt_id": None,
                            "attempt_number": 0,
                            "fence_version": int(row_fence_version or 0),
                            "key_status": row_status,
                            "active_attempt_id": row_active_attempt_id,
                        }

                cur.execute(retire_sql, (idempotency_key,))
                cur.execute(
                    alloc_sql,
                    (attempt_id, event_receipt_id, idempotency_key, idempotency_key),
                )
                att_row = cur.fetchone()
                resolved_attempt_id = att_row[0] if att_row else attempt_id
                attempt_no = int(att_row[1]) if att_row else 1

                cur.execute(
                    fence_sql,
                    (resolved_attempt_id, lock_ttl_seconds, idempotency_key),
                )
                fence_row = cur.fetchone()
                fence_version = int(fence_row[0]) if fence_row else int(row_fence_version or 0) + 1
                return {
                    "claimed": True,
                    "attempt_id": resolved_attempt_id,
                    "attempt_number": attempt_no,
                    "fence_version": fence_version,
                    "key_status": "IN_PROGRESS",
                }

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            # Reconcile by stable attempt_id generated for this specific claim invocation.
            def _reconcile(conn: Any) -> dict[str, Any] | None:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT pa.attempt_number, pa.status,
                               ik.status, ik.active_attempt_id, ik.attempt_fence_version
                        FROM {attempts_tbl} pa
                        JOIN {keys_tbl} ik ON ik.idempotency_key = pa.idempotency_key
                        WHERE pa.attempt_id = %s AND pa.idempotency_key = %s
                        """,
                        (attempt_id, idempotency_key),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return {
                        "attempt_number": int(row[0]),
                        "attempt_status": row[1],
                        "key_status": row[2],
                        "active_attempt_id": row[3],
                        "fence_version": int(row[4] or 0),
                    }

            reconciled = self.db.run_project_readonly(pid, _reconcile)
            if reconciled is not None:
                if reconciled["active_attempt_id"] == attempt_id and reconciled["key_status"] == "IN_PROGRESS":
                    return {
                        "claimed": True,
                        "attempt_id": attempt_id,
                        "attempt_number": reconciled["attempt_number"],
                        "fence_version": reconciled["fence_version"],
                        "key_status": "IN_PROGRESS",
                        "reconciled": True,
                    }
                raise StaleAttemptError(
                    f"Claim attempt '{attempt_id}' committed prior to connection drop but was already superseded."
                )
            return self.db.run_in_project_transaction(pid, _op)

    def finalize_processing_attempt(
        self,
        *,
        project_id: str,
        event_receipt_id: str | None,
        idempotency_key: str,
        attempt_id: str,
        attempt_number: int | None = None,
        expected_fence_version: int | None = None,
        attempt_status: str,
        final_key_status: str,
        retry_state: str = "NONE",
        error_code: str | None = None,
        error_summary: str | None = None,
        result_summary: dict[str, Any] | None = None,
    ) -> str:
        """Lock, verify project_id + active_attempt_id fencing token, and atomically finalize."""
        pid = validate_project_id(project_id)
        if not attempt_id or not str(attempt_id).strip():
            raise StaleAttemptError(
                "attempt_id is mandatory to finalize an idempotency processing attempt."
            )

        attempts_tbl = self._tbl("processing_attempts")
        keys_tbl = self._tbl("idempotency_keys")
        lock_key_sql = f"""
            SELECT project_id, status, active_attempt_id, attempt_fence_version,
                   (expires_at IS NOT NULL AND expires_at < NOW()) AS is_expired
            FROM {keys_tbl}
            WHERE idempotency_key = %s
            FOR UPDATE
        """
        update_attempt_sql = f"""
            UPDATE {attempts_tbl}
            SET status = %s,
                retry_state = %s,
                error_code = %s,
                error_summary = %s,
                finished_at = NOW()
            WHERE attempt_id = %s
              AND idempotency_key = %s
              AND status = 'IN_PROGRESS'
            RETURNING attempt_id, attempt_number
        """
        complete_key_sql = f"""
            UPDATE {keys_tbl}
            SET status = %s,
                active_attempt_id = NULL,
                completed_at = NOW(),
                result_summary = %s::jsonb
            WHERE idempotency_key = %s
              AND project_id = %s
              AND status = 'IN_PROGRESS'
              AND active_attempt_id = %s
            RETURNING idempotency_key
        """
        safe_error = sanitize_log_text(error_summary) if error_summary else None
        summary_json = json.dumps(sanitize_value(result_summary or {}))

        def _op(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute(lock_key_sql, (idempotency_key,))
                key_row = cur.fetchone()
                if key_row is None:
                    raise StaleAttemptError(
                        f"Idempotency key '{sanitize_log_text(idempotency_key)}' was not found."
                    )

                row_project_id, key_status, active_attempt_id, row_fence_ver, is_expired = key_row
                if row_project_id != pid:
                    raise IdempotencyIdentityMismatchError(
                        f"Idempotency key '{sanitize_log_text(idempotency_key)}' belongs to another project."
                    )
                if key_status != "IN_PROGRESS":
                    raise StaleAttemptError(
                        f"Cannot finalize attempt '{attempt_id}': key status is '{key_status}', expected 'IN_PROGRESS'."
                    )
                if active_attempt_id != attempt_id:
                    raise StaleAttemptError(
                        f"Stale attempt '{attempt_id}' rejected; active attempt is '{active_attempt_id}'."
                    )
                if expected_fence_version is not None and int(row_fence_ver or 0) != int(expected_fence_version):
                    raise StaleAttemptError(
                        f"Fence version mismatch for '{attempt_id}': expected {expected_fence_version}, found {row_fence_ver}."
                    )
                if is_expired:
                    raise StaleAttemptError(
                        f"Attempt '{attempt_id}' lease expired for key '{sanitize_log_text(idempotency_key)}'."
                    )

                cur.execute(
                    update_attempt_sql,
                    (
                        attempt_status,
                        retry_state,
                        error_code,
                        safe_error,
                        attempt_id,
                        idempotency_key,
                    ),
                )
                att_row = cur.fetchone()
                if att_row is None:
                    raise StaleAttemptError(
                        f"Active attempt row '{attempt_id}' not in IN_PROGRESS state."
                    )
                if attempt_number is not None and int(att_row[1]) != int(attempt_number):
                    raise StaleAttemptError(
                        f"Attempt number mismatch for '{attempt_id}': expected {attempt_number}, found {att_row[1]}."
                    )

                cur.execute(
                    complete_key_sql,
                    (final_key_status, summary_json, idempotency_key, pid, attempt_id),
                )
                if cur.fetchone() is None:
                    raise StaleAttemptError(
                        f"Fencing check failed when finalizing idempotency key '{sanitize_log_text(idempotency_key)}'."
                    )
                return att_row[0]

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            def _reconcile(conn: Any) -> dict[str, Any] | None:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT pa.status, ik.status, ik.active_attempt_id,
                               (ik.expires_at IS NOT NULL AND ik.expires_at < NOW()) AS is_expired
                        FROM {keys_tbl} ik
                        LEFT JOIN {attempts_tbl} pa
                          ON pa.idempotency_key = ik.idempotency_key AND pa.attempt_id = %s
                        WHERE ik.idempotency_key = %s AND ik.project_id = %s
                        """,
                        (attempt_id, idempotency_key, pid),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return {
                        "att_status": row[0],
                        "key_status": row[1],
                        "active_attempt_id": row[2],
                        "is_expired": bool(row[3]),
                    }

            state = self.db.run_project_readonly(pid, _reconcile)
            if state is None:
                raise StaleAttemptError(f"Idempotency key '{idempotency_key}' missing during reconciliation.")
            if (
                state["att_status"] == attempt_status
                and state["key_status"] == final_key_status
                and state["active_attempt_id"] is None
            ):
                return attempt_id
            if (
                state["att_status"] == "IN_PROGRESS"
                and state["key_status"] == "IN_PROGRESS"
                and state["active_attempt_id"] == attempt_id
                and not state["is_expired"]
            ):
                return self.db.run_in_project_transaction(pid, _op)
            raise StaleAttemptError(
                f"Stale attempt '{attempt_id}' rejected after ambiguous commit reconciliation."
            )

    def renew_attempt_lease(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        attempt_id: str,
        expected_fence_version: int | None = None,
        extend_ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        """Atomically extend lease expiry when project_id, ownership, and optional fence match."""
        pid = validate_project_id(project_id)
        if not idempotency_key or not str(idempotency_key).strip():
            raise StaleAttemptError("idempotency_key is required to renew a processing lease.")
        if not attempt_id or not str(attempt_id).strip():
            raise StaleAttemptError("attempt_id is required to renew a processing lease.")
        if isinstance(extend_ttl_seconds, bool) or int(extend_ttl_seconds) <= 0:
            raise ValueError("extend_ttl_seconds must be a positive integer.")
        ttl_seconds = int(extend_ttl_seconds)

        expected_ver: int | None = None
        if expected_fence_version is not None:
            if isinstance(expected_fence_version, bool) or int(expected_fence_version) < 0:
                raise StaleAttemptError("expected_fence_version must be a non-negative integer.")
            expected_ver = int(expected_fence_version)

        keys_tbl = self._tbl("idempotency_keys")
        attempts_tbl = self._tbl("processing_attempts")
        lock_key_sql = f"""
            SELECT project_id, status, active_attempt_id, attempt_fence_version,
                   (expires_at IS NOT NULL AND expires_at < NOW()) AS is_expired
            FROM {keys_tbl}
            WHERE idempotency_key = %s
            FOR UPDATE
        """
        renew_sql = f"""
            UPDATE {keys_tbl}
            SET expires_at = NOW() + (%s::integer * INTERVAL '1 second')
            WHERE idempotency_key = %s
              AND project_id = %s
              AND status = 'IN_PROGRESS'
              AND active_attempt_id = %s
              AND attempt_fence_version = %s
            RETURNING expires_at, attempt_fence_version
        """
        observed_target_exp: list[Any] = []

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(lock_key_sql, (idempotency_key,))
                key_row = cur.fetchone()
                if key_row is None:
                    raise StaleAttemptError(
                        f"Idempotency key '{sanitize_log_text(idempotency_key)}' was not found."
                    )
                row_project_id, key_status, active_attempt_id, row_fence_ver, is_expired = key_row
                if row_project_id != pid:
                    raise IdempotencyIdentityMismatchError(
                        f"Idempotency key '{sanitize_log_text(idempotency_key)}' belongs to another project."
                    )
                if key_status != "IN_PROGRESS":
                    raise StaleAttemptError(
                        f"Cannot renew lease for '{attempt_id}': key status is '{key_status}'."
                    )
                if active_attempt_id != attempt_id:
                    raise StaleAttemptError(
                        f"Stale worker '{attempt_id}' cannot renew; active attempt is '{active_attempt_id}'."
                    )
                effective_fence = int(row_fence_ver or 0)
                if expected_ver is not None and effective_fence != expected_ver:
                    raise StaleAttemptError(
                        f"Stale fence version {expected_ver} cannot renew; current version is {row_fence_ver}."
                    )
                if is_expired:
                    raise StaleAttemptError(
                        f"Cannot renew expired lease for attempt '{attempt_id}'."
                    )

                cur.execute(
                    f"SELECT status FROM {attempts_tbl} WHERE attempt_id = %s AND idempotency_key = %s FOR UPDATE",
                    (attempt_id, idempotency_key),
                )
                att_row = cur.fetchone()
                if att_row is None or att_row[0] != "IN_PROGRESS":
                    raise StaleAttemptError(
                        f"Cannot renew lease: attempt '{attempt_id}' is not IN_PROGRESS."
                    )

                cur.execute(
                    renew_sql,
                    (ttl_seconds, idempotency_key, pid, attempt_id, effective_fence),
                )
                upd = cur.fetchone()
                if upd is None:
                    raise StaleAttemptError(
                        f"Lease renewal rejected for attempt '{attempt_id}'."
                    )
                observed_target_exp[:] = [upd[0]]
                return {
                    "renewed": True,
                    "idempotency_key": idempotency_key,
                    "attempt_id": attempt_id,
                    "fence_version": int(upd[1]),
                    "expires_at": upd[0].isoformat() if hasattr(upd[0], "isoformat") else str(upd[0]),
                }

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            def _reconcile(conn: Any) -> dict[str, Any] | None:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT ik.status, ik.active_attempt_id, ik.attempt_fence_version,
                               ik.expires_at,
                               (ik.expires_at IS NOT NULL AND ik.expires_at < NOW()) AS is_expired,
                               pa.status
                        FROM {keys_tbl} ik
                        LEFT JOIN {attempts_tbl} pa
                          ON pa.idempotency_key = ik.idempotency_key AND pa.attempt_id = %s
                        WHERE ik.idempotency_key = %s AND ik.project_id = %s
                        """,
                        (attempt_id, idempotency_key, pid),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return {
                        "key_status": row[0],
                        "active_attempt_id": row[1],
                        "fence_version": int(row[2] or 0),
                        "expires_at": row[3],
                        "is_expired": bool(row[4]),
                        "att_status": row[5],
                    }

            state = self.db.run_project_readonly(pid, _reconcile)
            if (
                state is None
                or state["key_status"] != "IN_PROGRESS"
                or state["active_attempt_id"] != attempt_id
                or state["att_status"] != "IN_PROGRESS"
                or state["is_expired"]
                or (expected_ver is not None and state["fence_version"] != expected_ver)
            ):
                raise StaleAttemptError(
                    f"Lease renewal for attempt '{attempt_id}' rejected after ambiguous commit reconciliation."
                )
            reconciled_exp = state["expires_at"]
            if observed_target_exp and reconciled_exp is not None and reconciled_exp >= observed_target_exp[0]:
                return {
                    "renewed": True,
                    "idempotency_key": idempotency_key,
                    "attempt_id": attempt_id,
                    "fence_version": state["fence_version"],
                    "expires_at": reconciled_exp.isoformat() if hasattr(reconciled_exp, "isoformat") else str(reconciled_exp),
                    "reconciled": True,
                }
            return self.db.run_in_project_transaction(pid, _op)

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
        """Deprecated: use claim_idempotency_and_attempt() for fenced attempt tracking."""
        warnings.warn(
            "acquire_idempotency_key() is deprecated; use claim_idempotency_and_attempt() "
            "to obtain the fencing attempt_id.",
            DeprecationWarning,
            stacklevel=2,
        )
        res = self.claim_idempotency_and_attempt(
            idempotency_key=idempotency_key,
            scope=scope,
            project_id=project_id,
            feature_id=feature_id,
            report_blob_sha=report_blob_sha,
            target_document_id=target_document_id,
            event_receipt_id=None,
            lock_ttl_seconds=lock_ttl_seconds,
        )
        return bool(res["claimed"])

    def next_attempt_number(self, *, project_id: str, idempotency_key: str) -> int:
        """Deprecated: use claim_idempotency_and_attempt() for lock-serialized allocation."""
        pid = validate_project_id(project_id)
        warnings.warn(
            "next_attempt_number() is deprecated; use claim_idempotency_and_attempt().",
            DeprecationWarning,
            stacklevel=2,
        )
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

        return self.db.run_in_project_transaction(pid, _op)

    def complete_idempotency_key(
        self,
        *,
        project_id: str,
        idempotency_key: str,
        status: str,
        result_summary: dict[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """Deprecated: use finalize_processing_attempt(..., attempt_id=...) instead."""
        pid = validate_project_id(project_id)
        warnings.warn(
            "complete_idempotency_key() is deprecated; use finalize_processing_attempt(..., attempt_id=...).",
            DeprecationWarning,
            stacklevel=2,
        )
        if not attempt_id or not str(attempt_id).strip():
            raise StaleAttemptError(
                "complete_idempotency_key requires attempt_id to prevent bypassing active-attempt fencing."
            )
        self.finalize_processing_attempt(
            project_id=pid,
            event_receipt_id=None,
            idempotency_key=idempotency_key,
            attempt_id=attempt_id,
            attempt_status="SUCCEEDED" if status == "COMPLETED" else "FAILED",
            final_key_status=status,
            result_summary=result_summary,
        )

    def record_processing_attempt(
        self,
        *,
        project_id: str,
        event_receipt_id: str | None,
        idempotency_key: str,
        attempt_number: int,
        status: str,
        retry_state: str = "NONE",
        error_code: str | None = None,
        error_summary: str | None = None,
        attempt_id: str | None = None,
    ) -> str:
        """Deprecated: use claim_idempotency_and_attempt() and finalize_processing_attempt()."""
        pid = validate_project_id(project_id)
        warnings.warn(
            "record_processing_attempt() is deprecated; use claim_idempotency_and_attempt() "
            "and finalize_processing_attempt().",
            DeprecationWarning,
            stacklevel=2,
        )
        keys_tbl = self._tbl("idempotency_keys")
        attempts_tbl = self._tbl("processing_attempts")
        resolved_attempt_id = attempt_id or str(uuid.uuid4())
        safe_error = sanitize_log_text(error_summary) if error_summary else None

        def _op(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT project_id, status, active_attempt_id FROM {keys_tbl} WHERE idempotency_key = %s FOR UPDATE",
                    (idempotency_key,),
                )
                krow = cur.fetchone()
                if krow is None:
                    raise StaleAttemptError(f"Idempotency key '{idempotency_key}' does not exist.")
                kproject_id, kstatus, active_id = krow
                if kproject_id != pid:
                    raise IdempotencyIdentityMismatchError(
                        f"Idempotency key '{sanitize_log_text(idempotency_key)}' belongs to another project."
                    )
                if kstatus == "IN_PROGRESS" and active_id is not None and attempt_id != active_id:
                    raise StaleAttemptError(
                        f"Cannot record attempt without matching active attempt_id '{active_id}'."
                    )
                cur.execute(
                    f"""
                    INSERT INTO {attempts_tbl} (
                        attempt_id, event_receipt_id, idempotency_key, attempt_number,
                        status, retry_state, error_code, error_summary, started_at, finished_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    """,
                    (
                        resolved_attempt_id,
                        event_receipt_id,
                        idempotency_key,
                        attempt_number,
                        status,
                        retry_state,
                        error_code,
                        safe_error,
                    ),
                )
                return resolved_attempt_id

        return self.db.run_in_project_transaction(pid, _op)

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
        """Persist validated canonical JSON report idempotently and return existing report_id."""
        pid = validate_project_id(project_id)
        report_id = str(uuid.uuid4())
        tbl = self._tbl("final_reports")
        insert_sql = f"""
            INSERT INTO {tbl} (
                report_id, project_id, feature_id, feature_name, change_type,
                implementation_status, report_path, report_blob_sha, git_commit_sha,
                report_metadata, evidence_refs
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (project_id, feature_id, report_blob_sha) DO NOTHING
            RETURNING report_id
        """
        select_sql = f"""
            SELECT report_id
            FROM {tbl}
            WHERE project_id = %s AND feature_id = %s AND report_blob_sha = %s
        """
        safe_feature_name = sanitize_log_text(feature_name)
        meta_json = json.dumps(sanitize_value(report_metadata))
        ev_json = json.dumps(sanitize_value(evidence_refs))

        def _op(conn: Any) -> dict[str, Any]:
            with conn.cursor() as cur:
                cur.execute(
                    insert_sql,
                    (
                        report_id,
                        pid,
                        feature_id,
                        safe_feature_name,
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
                if row is not None:
                    return {
                        "inserted": True,
                        "report_id": row[0],
                    }
                cur.execute(select_sql, (pid, feature_id, report_blob_sha))
                existing = cur.fetchone()
                return {
                    "inserted": False,
                    "report_id": existing[0] if existing else None,
                }

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            def _reconcile(conn: Any) -> str | None:
                with conn.cursor() as cur:
                    cur.execute(select_sql, (pid, feature_id, report_blob_sha))
                    row = cur.fetchone()
                    return row[0] if row else None

            existing_id = self.db.run_project_readonly(pid, _reconcile)
            if existing_id is not None:
                return {
                    "inserted": existing_id == report_id,
                    "report_id": existing_id,
                    "reconciled": True,
                }
            return self.db.run_in_project_transaction(pid, _op)

    def append_audit_event(
        self,
        *,
        actor: str,
        event_type: str,
        project_id: str,
        feature_id: str | None = None,
        document_id: str | None = None,
        idempotency_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        pid = validate_project_id(project_id)
        event_id = str(uuid.uuid4())
        tbl = self._tbl("audit_events")
        sql = f"""
            INSERT INTO {tbl} (
                event_id, actor, event_type, project_id, feature_id,
                document_id, idempotency_key, details
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """
        safe_details = json.dumps(sanitize_value(details or {}))

        def _op(conn: Any) -> str:
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    (
                        event_id,
                        actor,
                        event_type,
                        pid,
                        feature_id,
                        document_id,
                        idempotency_key,
                        safe_details,
                    ),
                )
                return event_id

        try:
            return self.db.run_in_project_transaction(pid, _op)
        except CommitOutcomeUnknownError:
            def _check(conn: Any) -> str | None:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT event_id FROM {tbl} WHERE event_id = %s", (event_id,))
                    row = cur.fetchone()
                    return row[0] if row else None

            existing_id = self.db.run_project_readonly(pid, _check)
            if existing_id is not None:
                return existing_id
            return self.db.run_in_project_transaction(pid, _op)

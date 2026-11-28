"""Version-controlled PostgreSQL migration runner for UTDA.

Security invariants:
- UTDA_MIGRATION_DATABASE_URL is mandatory for --apply (never falls back to runtime DSN).
- Verifies CURRENT_USER == 'utda_migrator' before applying migrations.
- Validates TLS on the migration DSN independently.
- Refuses execution through Transaction Pooler connections (port 6543 / transaction).
- Validates and double-quotes schema identifiers.
- Detects modified historical checksums and unknown DB migration versions.
- Executes each migration atomically under a PostgreSQL session advisory lock.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from engine.db_config import (
    DatabaseConfig,
    DatabaseConfigError,
    configure_sanitized_logger,
    quote_ident,
    sanitize_log_text,
    validate_identifier,
)

MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent
REQUIRED_MIGRATION_ROLE = "utda_migrator"


class MigrationError(RuntimeError):
    """Raised when migration validation, identity, locking, or execution fails."""


class MigrationChecksumError(MigrationError):
    """Raised when an already-applied migration SQL file has been modified."""


class UnknownMigrationError(MigrationError):
    """Raised when the database contains migration versions absent from the repo."""


@dataclass(frozen=True)
class MigrationFile:
    version: str
    name: str
    path: Path
    sql: str
    checksum_sha256: str


def discover_migrations(
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
) -> list[MigrationFile]:
    """Discover and validate ordered SQL migration files."""
    discovered: list[MigrationFile] = []
    seen_versions: set[str] = set()
    for path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"Invalid migration filename '{path.name}'. Expected NNNN_description.sql"
            )
        version, name = match.group(1), match.group(2)
        if version in seen_versions:
            raise MigrationError(f"Duplicate migration version '{version}'.")
        seen_versions.add(version)
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        discovered.append(
            MigrationFile(
                version=version,
                name=name,
                path=path,
                sql=sql,
                checksum_sha256=checksum,
            )
        )
    return discovered


def assert_migration_connection_supported(config: DatabaseConfig) -> None:
    """Prevent running DDL migrations over incompatible Transaction Pooler mode."""
    if not config.supports_migrations:
        raise MigrationError(
            "Schema migrations cannot run over Supabase Transaction Pooler (port 6543 / "
            "pool_mode='transaction'). Use Session Pooler (port 5432) or Direct connection."
        )


def _rewrite_schema_prefix(sql: str, schema: str) -> str:
    safe_schema = validate_identifier(schema, kind="schema")
    if safe_schema == "utda":
        return sql
    quoted = quote_ident(safe_schema, kind="schema")
    rewritten = re.sub(r"(?<!')\butda\.", f"{quoted}.", sql)
    rewritten = re.sub(
        r"\bSCHEMA\s+(IF\s+NOT\s+EXISTS\s+)?utda\b",
        lambda m: f"SCHEMA {m.group(1) or ''}{quoted}",
        rewritten,
        flags=re.IGNORECASE,
    )
    rewritten = re.sub(
        r"\bIN\s+SCHEMA\s+utda\b",
        f"IN SCHEMA {quoted}",
        rewritten,
        flags=re.IGNORECASE,
    )
    rewritten = re.sub(
        r"schemaname\s*=\s*'utda'",
        f"schemaname = '{safe_schema}'",
        rewritten,
        flags=re.IGNORECASE,
    )
    rewritten = re.sub(
        r"nspname\s*=\s*'utda'",
        f"nspname = '{safe_schema}'",
        rewritten,
        flags=re.IGNORECASE,
    )
    return rewritten


def _verify_migration_identity(conn: Any, *, allow_bootstrap_role: bool = False) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT CURRENT_USER")
        row = cur.fetchone()
    current_user = str(row[0]) if row else ""
    if not allow_bootstrap_role and current_user != REQUIRED_MIGRATION_ROLE:
        raise MigrationError(
            f"Migration apply requires connected role '{REQUIRED_MIGRATION_ROLE}', "
            f"but connected as '{current_user or '<unknown>'}'."
        )
    return current_user


def _fetch_applied_migrations(conn: Any, schema: str) -> dict[str, tuple[str, str]]:
    safe_schema = validate_identifier(schema, kind="schema")
    quoted_table = f'{quote_ident(safe_schema, kind="schema")}."schema_migrations"'
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = 'schema_migrations'
            )
            """,
            (safe_schema,),
        )
        exists_row = cur.fetchone()
        if not exists_row or not exists_row[0]:
            return {}
        cur.execute(
            f"SELECT version, checksum_sha256, status FROM {quoted_table} ORDER BY version"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def plan_migrations(
    conn: Any,
    schema: str,
    migrations: list[MigrationFile],
) -> list[MigrationFile]:
    """Compare repository migrations against applied DB history, failing on drift."""
    safe_schema = validate_identifier(schema, kind="schema")
    applied = _fetch_applied_migrations(conn, safe_schema)

    repo_versions = {m.version for m in migrations}
    unknown_versions = sorted(set(applied.keys()) - repo_versions)
    if unknown_versions:
        raise UnknownMigrationError(
            "Database contains applied migration version(s) absent from repository: "
            f"{unknown_versions}."
        )

    pending: list[MigrationFile] = []
    for migration in migrations:
        if migration.version in applied:
            recorded_checksum, status = applied[migration.version]
            if status != "APPLIED":
                raise MigrationError(
                    f"Migration {migration.version} has non-applied status '{status}'."
                )
            if recorded_checksum != migration.checksum_sha256:
                raise MigrationChecksumError(
                    f"Checksum mismatch for applied migration {migration.version} ({migration.name})."
                )
        else:
            pending.append(migration)
    return pending


def apply_migrations(
    config: DatabaseConfig,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
    *,
    dry_run: bool = True,
    applied_by: str = "utda-migrator",
    connect_fn: Any | None = None,
    allow_bootstrap_role: bool = False,
) -> dict[str, Any]:
    """Plan or apply ordered SQL migrations atomically using a session advisory lock."""
    assert_migration_connection_supported(config)
    if not dry_run and not config.migration_dsn:
        raise MigrationError(
            "UTDA_MIGRATION_DATABASE_URL is mandatory for migration apply operations; "
            "falling back to runtime UTDA_DATABASE_URL is prohibited."
        )

    safe_schema = validate_identifier(config.schema, kind="schema")
    quoted_table = f'{quote_ident(safe_schema, kind="schema")}."schema_migrations"'
    logger = configure_sanitized_logger("utda.migrations")
    migrations = discover_migrations(migrations_dir)

    if connect_fn is None:
        import psycopg

        target_dsn = config.migration_dsn if config.migration_dsn else config.dsn
        kwargs = config.connect_kwargs(for_migration=bool(config.migration_dsn))
        conn_ctx = psycopg.connect(target_dsn, **kwargs)
    else:
        conn_ctx = connect_fn(config)

    applied_versions: list[str] = []
    connected_user = ""
    lock_key = f"utda_mig:{safe_schema}"
    with conn_ctx as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s::text)::bigint)", (lock_key,))
        conn.commit()
        try:
            if not dry_run:
                connected_user = _verify_migration_identity(
                    conn, allow_bootstrap_role=allow_bootstrap_role
                )
                conn.rollback()

            pending = plan_migrations(conn, safe_schema, migrations)
            if dry_run:
                conn.rollback()
                return {
                    "mode": "plan",
                    "schema": safe_schema,
                    "pending": [f"{m.version}_{m.name}" for m in pending],
                    "applied": [],
                }

            for migration in pending:
                start = time.monotonic()
                rendered_sql = _rewrite_schema_prefix(migration.sql, safe_schema)
                committed = False
                rolled_back = False
                try:
                    with conn.cursor() as cur:
                        cur.execute(rendered_sql)
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        cur.execute(
                            f"""
                            INSERT INTO {quoted_table} (
                                version, name, checksum_sha256, status, applied_by, execution_ms
                            )
                            VALUES (%s, %s, %s, 'APPLIED', %s, %s)
                            """,
                            (
                                migration.version,
                                migration.name,
                                migration.checksum_sha256,
                                applied_by,
                                elapsed_ms,
                            ),
                        )
                    conn.commit()
                    committed = True
                    applied_versions.append(f"{migration.version}_{migration.name}")
                    logger.info(
                        "Applied migration %s_%s (%dms) as %s",
                        migration.version,
                        migration.name,
                        elapsed_ms,
                        connected_user,
                    )
                except Exception as exc:
                    conn.rollback()
                    rolled_back = True
                    raise MigrationError(
                        f"Migration {migration.version}_{migration.name} failed and was rolled back: "
                        f"{sanitize_log_text(str(exc))}"
                    ) from exc
                finally:
                    if not committed and not rolled_back:
                        try:
                            conn.rollback()
                        except Exception as rb_err:
                            logger.warning(
                                "Rollback cleanup encountered an error: %s",
                                sanitize_log_text(str(rb_err)),
                            )
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s::text)::bigint)",
                        (lock_key,),
                    )
                conn.commit()
            except Exception as unlock_err:
                logger.warning(
                    "Failed to release migration advisory lock: %s",
                    sanitize_log_text(str(unlock_err)),
                )

    return {
        "mode": "apply",
        "schema": safe_schema,
        "connected_user": connected_user,
        "pending": [],
        "applied": applied_versions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UTDA PostgreSQL migration runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true", help="List pending migrations without applying")
    group.add_argument("--apply", action="store_true", help="Apply pending migrations")
    parser.add_argument("--actor", default=os.getenv("GITHUB_ACTOR", "utda-migrator"))
    args = parser.parse_args(argv)

    try:
        config = DatabaseConfig.from_migration_env(require_migration_dsn=bool(args.apply))
        result = apply_migrations(
            config,
            dry_run=bool(args.plan),
            applied_by=args.actor,
        )
        print(result)
        return 0
    except (DatabaseConfigError, MigrationError) as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

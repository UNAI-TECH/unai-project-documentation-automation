"""Version-controlled PostgreSQL migration runner for UTDA.

Refuses execution through Transaction Pooler connections (port 6543 /
pool_mode='transaction'), verifies checksums of previously applied migrations,
and applies each pending migration atomically inside a transaction.
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
from urllib.parse import urlparse

from engine.db_config import (
    DatabaseConfig,
    DatabaseConfigError,
    configure_sanitized_logger,
)

MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent


class MigrationError(RuntimeError):
    """Raised when migration validation, locking, or execution fails."""


class MigrationChecksumError(MigrationError):
    """Raised when an already-applied migration SQL file has been modified."""


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
    if schema == "utda":
        return sql
    rewritten = re.sub(r"\butda\.", f"{schema}.", sql)
    rewritten = re.sub(
        r"\bSCHEMA\s+(IF\s+NOT\s+EXISTS\s+)?utda\b",
        lambda m: f"SCHEMA {m.group(1) or ''}{schema}",
        rewritten,
        flags=re.IGNORECASE,
    )
    return rewritten


def _fetch_applied_migrations(conn: Any, schema: str) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = 'schema_migrations'
            )
            """,
            (schema,),
        )
        exists_row = cur.fetchone()
        if not exists_row or not exists_row[0]:
            return {}
        cur.execute(
            f"SELECT version, checksum_sha256, status FROM {schema}.schema_migrations ORDER BY version"
        )
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def plan_migrations(
    conn: Any,
    schema: str,
    migrations: list[MigrationFile],
) -> list[MigrationFile]:
    """Compare discovered migrations against applied history and return pending files."""
    applied = _fetch_applied_migrations(conn, schema)
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
) -> dict[str, Any]:
    """Plan or apply ordered SQL migrations atomically using a session advisory lock."""
    assert_migration_connection_supported(config)
    logger = configure_sanitized_logger("utda.migrations")
    migrations = discover_migrations(migrations_dir)

    if connect_fn is None:
        import psycopg

        target_dsn = config.migration_dsn or config.dsn
        kwargs = config.connect_kwargs(for_migration=True)
        conn_ctx = psycopg.connect(target_dsn, **kwargs)
    else:
        conn_ctx = connect_fn(config)

    applied_versions: list[str] = []
    with conn_ctx as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s::text)::bigint)", (f"utda_mig:{config.schema}",))
        conn.commit()
        try:
            pending = plan_migrations(conn, config.schema, migrations)
            if dry_run:
                return {
                    "mode": "plan",
                    "schema": config.schema,
                    "pending": [f"{m.version}_{m.name}" for m in pending],
                    "applied": [],
                }

            for migration in pending:
                start = time.monotonic()
                rendered_sql = _rewrite_schema_prefix(migration.sql, config.schema)
                try:
                    with conn.cursor() as cur:
                        cur.execute(rendered_sql)
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        cur.execute(
                            f"""
                            INSERT INTO {config.schema}.schema_migrations (
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
                    applied_versions.append(f"{migration.version}_{migration.name}")
                    logger.info("Applied migration %s_%s (%dms)", migration.version, migration.name, elapsed_ms)
                except BaseException as exc:
                    conn.rollback()
                    raise MigrationError(
                        f"Migration {migration.version}_{migration.name} failed and was rolled back."
                    ) from exc
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s::text)::bigint)",
                        (f"utda_mig:{config.schema}",),
                    )
                conn.commit()
            except Exception:
                pass

    return {
        "mode": "apply",
        "schema": config.schema,
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
        config = DatabaseConfig.from_env()
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

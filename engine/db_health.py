"""Database health, connectivity, and Supabase Free Plan quota checks."""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from engine.database import DatabaseManager, DatabaseUnavailableError
from engine.db_config import DatabaseConfig, DatabaseConfigError, sanitize_log_text

SUPABASE_FREE_QUOTA_BYTES = 500 * 1024 * 1024  # 500 MB
WARN_PCT = 70.0
CRITICAL_PCT = 85.0


def check_database_health(db: DatabaseManager) -> dict[str, Any]:
    """Run connectivity, migration-status, and Free-Plan quota checks safely."""
    start = time.monotonic()
    schema = db.config.schema

    def _probe(conn: Any) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

            cur.execute("SELECT pg_database_size(current_database())")
            size_bytes = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = 'schema_migrations'
                )
                """,
                (schema,),
            )
            migrations_initialized = bool(cur.fetchone()[0])

            latest_migration = None
            applied_count = 0
            if migrations_initialized:
                cur.execute(
                    f"SELECT version, COUNT(*) OVER () FROM {schema}.schema_migrations "
                    "WHERE status = 'APPLIED' ORDER BY version DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    latest_migration = row[0]
                    applied_count = int(row[1])

        return {
            "size_bytes": size_bytes,
            "migrations_initialized": migrations_initialized,
            "latest_migration": latest_migration,
            "applied_migrations": applied_count,
        }

    try:
        metrics = db.run_in_transaction(_probe)
    except DatabaseUnavailableError as exc:
        return {
            "status": "unhealthy",
            "error": sanitize_log_text(str(exc)),
            "database": db.config.redacted_summary(),
        }

    latency_ms = round((time.monotonic() - start) * 1000, 2)
    size_bytes = metrics["size_bytes"]
    quota_pct = round((size_bytes / SUPABASE_FREE_QUOTA_BYTES) * 100, 2)

    status = "healthy"
    if quota_pct >= CRITICAL_PCT:
        status = "critical"
    elif quota_pct >= WARN_PCT or not metrics["migrations_initialized"]:
        status = "warning"

    return {
        "status": status,
        "latency_ms": latency_ms,
        "database": db.config.redacted_summary(),
        "storage": {
            "database_size_bytes": size_bytes,
            "database_size_mb": round(size_bytes / (1024 * 1024), 2),
            "free_plan_quota_mb": 500,
            "quota_used_pct": quota_pct,
        },
        "migrations": {
            "initialized": metrics["migrations_initialized"],
            "applied_count": metrics["applied_migrations"],
            "latest_version": metrics["latest_migration"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="UTDA database health check")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args(argv)

    try:
        config = DatabaseConfig.from_env()
    except DatabaseConfigError as exc:
        payload = {"status": "misconfigured", "error": str(exc)}
        print(json.dumps(payload, indent=2) if args.json else payload["error"], file=sys.stderr)
        return 2

    with DatabaseManager(config) as db:
        report = check_database_health(db)

    print(json.dumps(report, indent=2) if args.json else report)
    return 0 if report["status"] in ("healthy", "warning") else 1


if __name__ == "__main__":
    raise SystemExit(main())

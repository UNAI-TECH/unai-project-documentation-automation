"""Environment-based PostgreSQL/Supabase configuration and log sanitization.

Reads secrets exclusively from environment variables, validates SSL/TLS and
connection-mode safety independently for runtime and migration DSNs, and
redacts credentials, tokens, API keys, and sensitive query params from logs.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

from engine.sanitization import (
    API_KEY_RE,
    AWS_KEY_RE,
    BEARER_TOKEN_RE,
    DSN_LOG_RE,
    GITHUB_TOKEN_RE,
    JWT_RE,
    KV_SECRET_RE,
    PRIVATE_KEY_RE,
    SENSITIVE_QUERY_KEYS,
    SUPABASE_KEY_RE,
    SanitizingFilter,
    configure_sanitized_logger,
    redact_dsn,
    sanitize_log_text,
    sanitize_value,
    scrub_secrets,
)

VALID_POOL_MODES = ("session", "transaction", "direct")
SECURE_SSL_MODES = ("require", "verify-ca", "verify-full")
LOCAL_INSECURE_SSL_MODES = ("disable", "allow", "prefer")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
SCHEMA_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PROJECT_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")


class DatabaseConfigError(ValueError):
    """Raised when database configuration is missing, unsafe, or invalid."""


def validate_project_id(project_id: str, *, field_name: str = "project_id") -> str:
    """Strictly validate a canonical UTDA project_id without silent normalization."""
    if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
        raise DatabaseConfigError(
            f"Invalid {field_name}: must be a non-empty canonical identifier matching "
            "^[A-Z0-9][A-Z0-9_-]{2,31}$ without leading/trailing whitespace."
        )
    return project_id


def validate_identifier(name: str, kind: str = "identifier") -> str:
    """Validate that a PostgreSQL schema/table identifier is safe to quote."""
    if not isinstance(name, str) or not SCHEMA_NAME_RE.match(name):
        raise DatabaseConfigError(
            f"Unsafe PostgreSQL {kind}: must match ^[a-z_][a-z0-9_]{{0,62}}$."
        )
    return name


def quote_ident(name: str, kind: str = "identifier") -> str:
    """Validate and double-quote a PostgreSQL identifier."""
    safe = validate_identifier(name, kind=kind)
    return f'"{safe}"'


def _parse_int(raw: str | None, default: int, name: str, minimum: int, maximum: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise DatabaseConfigError(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise DatabaseConfigError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _validate_postgres_dsn(dsn: str, var_name: str):
    value = dsn.strip()
    if not value:
        raise DatabaseConfigError(f"{var_name} is required and cannot be empty.")

    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        raise DatabaseConfigError(
            f"{var_name} must be a PostgreSQL connection URI, not a Supabase HTTP API URL."
        )
    if value.startswith(("eyJ", "sb_publishable_", "sb_secret_")):
        raise DatabaseConfigError(
            f"{var_name} appears to contain an API/JWT key; Supabase anon/service-role keys "
            "cannot be used as PostgreSQL credentials."
        )

    parsed = urlparse(value)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise DatabaseConfigError(
            f"{var_name} must use the 'postgresql://' or 'postgres://' scheme."
        )
    if not parsed.hostname:
        raise DatabaseConfigError(f"{var_name} is missing a database hostname.")
    if not parsed.path or parsed.path == "/":
        raise DatabaseConfigError(f"{var_name} is missing a database name.")
    return parsed


def _infer_pool_mode(explicit_mode: str | None, hostname: str, port: int | None, var_name: str) -> str:
    if explicit_mode and explicit_mode.strip():
        mode = explicit_mode.strip().lower()
        if mode not in VALID_POOL_MODES:
            raise DatabaseConfigError(
                f"{var_name} must be one of {VALID_POOL_MODES}."
            )
        return mode
    if port == 6543:
        return "transaction"
    if "pooler.supabase.com" in hostname.lower():
        return "session"
    return "direct"


def _validate_ssl_settings(
    parsed_dsn: Any,
    env: Mapping[str, str],
    ssl_var: str,
    fallback_sslmode: str | None = None,
) -> tuple[str, str | None]:
    query_ssl = dict(parse_qsl(parsed_dsn.query)).get("sslmode")
    # If the DSN query explicitly sets an sslmode, validate THAT value too—never allow bypass.
    explicit_env_ssl = env.get(ssl_var, "").strip().lower()
    effective_candidates = [c for c in (query_ssl, explicit_env_ssl) if c]
    allow_insecure_local = (
        env.get("UTDA_ALLOW_INSECURE_LOCAL_DB", "").strip().lower() == "true"
    )
    is_local_host = (parsed_dsn.hostname or "").lower() in LOCAL_HOSTS

    if not effective_candidates:
        sslmode = (fallback_sslmode or "require").strip().lower()
    else:
        for candidate in effective_candidates:
            cand = candidate.strip().lower()
            if cand in LOCAL_INSECURE_SSL_MODES:
                if not (allow_insecure_local and is_local_host):
                    raise DatabaseConfigError(
                        f"Unsafe sslmode='{cand}'. TLS ('require', 'verify-ca', or 'verify-full') "
                        "is mandatory for remote/Supabase PostgreSQL connections."
                    )
            elif cand not in SECURE_SSL_MODES:
                raise DatabaseConfigError(
                    f"Unsupported sslmode='{cand}'. Expected one of {SECURE_SSL_MODES}."
                )
        sslmode = (query_ssl or explicit_env_ssl).strip().lower()

    if sslmode in LOCAL_INSECURE_SSL_MODES and not (allow_insecure_local and is_local_host):
        raise DatabaseConfigError(
            f"Unsafe sslmode='{sslmode}'. TLS ('require', 'verify-ca', or 'verify-full') "
            "is mandatory for remote/Supabase PostgreSQL connections."
        )

    sslrootcert = env.get("UTDA_DATABASE_SSLROOTCERT", "").strip() or None
    if sslmode in ("verify-ca", "verify-full") and sslrootcert:
        if not os.path.isfile(sslrootcert):
            raise DatabaseConfigError(
                "UTDA_DATABASE_SSLROOTCERT points to a nonexistent certificate file."
            )
    return sslmode, sslrootcert


@dataclass(frozen=True, repr=False)
class DatabaseConfig:
    """Validated runtime and migration configuration for UTDA PostgreSQL."""

    dsn: str
    migration_dsn: str | None = None
    schema: str = "utda"
    pool_mode: str = "session"
    migration_pool_mode: str = "session"
    pool_size: int = 4
    connect_timeout: int = 10
    sslmode: str = "require"
    migration_sslmode: str = "require"
    sslrootcert: str | None = None
    statement_timeout_ms: int = 15000
    max_retries: int = 3

    def __repr__(self) -> str:
        return (
            "DatabaseConfig("
            f"dsn={redact_dsn(self.dsn)!r}, "
            f"migration_dsn={redact_dsn(self.migration_dsn) if self.migration_dsn else None!r}, "
            f"schema={self.schema!r}, "
            f"pool_mode={self.pool_mode!r}, "
            f"migration_pool_mode={self.migration_pool_mode!r}, "
            f"pool_size={self.pool_size!r}, "
            f"connect_timeout={self.connect_timeout!r}, "
            f"sslmode={self.sslmode!r}, "
            f"migration_sslmode={self.migration_sslmode!r}, "
            f"statement_timeout_ms={self.statement_timeout_ms!r}"
            ")"
        )

    @property
    def supports_migrations(self) -> bool:
        """Return True when the migration DSN (if configured) or active DSN supports DDL."""
        if self.migration_dsn:
            parsed = urlparse(self.migration_dsn)
            return self.migration_pool_mode != "transaction" and parsed.port != 6543
        parsed = urlparse(self.dsn)
        return self.pool_mode != "transaction" and parsed.port != 6543

    def redacted_summary(self) -> dict[str, object]:
        """Return a sanitized metadata dictionary safe for structured logs."""
        parsed = urlparse(self.dsn)
        return {
            "host": parsed.hostname,
            "port": parsed.port or 5432,
            "database": parsed.path.lstrip("/"),
            "schema": self.schema,
            "pool_mode": self.pool_mode,
            "pool_size": self.pool_size,
            "sslmode": self.sslmode,
            "dsn": redact_dsn(self.dsn),
        }

    def connect_kwargs(self, for_migration: bool = False) -> dict[str, object]:
        """Return psycopg connection keyword arguments appropriate for the mode."""
        target_dsn = self.migration_dsn if (for_migration and self.migration_dsn) else self.dsn
        target_ssl = self.migration_sslmode if (for_migration and self.migration_dsn) else self.sslmode
        parsed = urlparse(target_dsn)
        query_params = dict(parse_qsl(parsed.query))

        kwargs: dict[str, object] = {
            "connect_timeout": self.connect_timeout,
        }
        if "sslmode" not in query_params:
            kwargs["sslmode"] = target_ssl
        if self.sslrootcert and "sslrootcert" not in query_params:
            kwargs["sslrootcert"] = self.sslrootcert
        if not for_migration and (self.pool_mode == "transaction" or parsed.port == 6543):
            # Supavisor transaction pooling does not preserve server-side prepared statements.
            kwargs["prepare_threshold"] = None
        return kwargs

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        dsn_var: str = "UTDA_DATABASE_URL",
    ) -> "DatabaseConfig":
        """Build and validate DatabaseConfig from environment variables."""
        env = os.environ if environ is None else environ
        raw_dsn = env.get(dsn_var, "")
        if not raw_dsn or not raw_dsn.strip():
            raise DatabaseConfigError(
                f"Missing required environment variable {dsn_var}. "
                "Configure a PostgreSQL DSN securely in the environment."
            )

        parsed = _validate_postgres_dsn(raw_dsn, dsn_var)
        schema = validate_identifier(
            (env.get("UTDA_DATABASE_SCHEMA") or "utda").strip(), kind="schema"
        )
        pool_mode = _infer_pool_mode(
            env.get("UTDA_DATABASE_POOL_MODE"),
            parsed.hostname or "",
            parsed.port,
            "UTDA_DATABASE_POOL_MODE",
        )
        pool_size = _parse_int(
            env.get("UTDA_DATABASE_POOL_SIZE"), 4, "UTDA_DATABASE_POOL_SIZE", 1, 20
        )
        connect_timeout = _parse_int(
            env.get("UTDA_DATABASE_CONNECT_TIMEOUT"),
            10,
            "UTDA_DATABASE_CONNECT_TIMEOUT",
            1,
            120,
        )
        statement_timeout_ms = _parse_int(
            env.get("UTDA_DATABASE_STATEMENT_TIMEOUT"),
            15000,
            "UTDA_DATABASE_STATEMENT_TIMEOUT",
            1000,
            300000,
        )
        max_retries = _parse_int(
            env.get("UTDA_DATABASE_MAX_RETRIES"), 3, "UTDA_DATABASE_MAX_RETRIES", 0, 10
        )

        sslmode, sslrootcert = _validate_ssl_settings(
            parsed, env, "UTDA_DATABASE_SSLMODE"
        )

        raw_migration_dsn = env.get("UTDA_MIGRATION_DATABASE_URL", "").strip()
        migration_dsn: str | None = None
        migration_pool_mode = pool_mode
        migration_sslmode = sslmode
        if raw_migration_dsn:
            parsed_mig = _validate_postgres_dsn(
                raw_migration_dsn, "UTDA_MIGRATION_DATABASE_URL"
            )
            migration_dsn = raw_migration_dsn
            migration_pool_mode = _infer_pool_mode(
                env.get("UTDA_MIGRATION_POOL_MODE"),
                parsed_mig.hostname or "",
                parsed_mig.port,
                "UTDA_MIGRATION_POOL_MODE",
            )
            migration_sslmode, _ = _validate_ssl_settings(
                parsed_mig,
                env,
                "UTDA_MIGRATION_SSLMODE",
                fallback_sslmode=env.get("UTDA_DATABASE_SSLMODE") or "require",
            )

        return cls(
            dsn=raw_dsn.strip(),
            migration_dsn=migration_dsn,
            schema=schema,
            pool_mode=pool_mode,
            migration_pool_mode=migration_pool_mode,
            pool_size=pool_size,
            connect_timeout=connect_timeout,
            sslmode=sslmode,
            migration_sslmode=migration_sslmode,
            sslrootcert=sslrootcert,
            statement_timeout_ms=statement_timeout_ms,
            max_retries=max_retries,
        )

    @classmethod
    def from_migration_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_migration_dsn: bool = True,
    ) -> "DatabaseConfig":
        """Load and validate configuration specifically for migration operations."""
        env = os.environ if environ is None else environ
        raw_migration_dsn = env.get("UTDA_MIGRATION_DATABASE_URL", "").strip()
        if require_migration_dsn and not raw_migration_dsn:
            raise DatabaseConfigError(
                "UTDA_MIGRATION_DATABASE_URL is mandatory for migration operations; "
                "falling back to runtime UTDA_DATABASE_URL is prohibited."
            )
        if raw_migration_dsn:
            runtime_or_fallback = env.get("UTDA_DATABASE_URL", "").strip() or raw_migration_dsn
            patched_env = dict(env)
            patched_env["UTDA_DATABASE_URL"] = runtime_or_fallback
            return cls.from_env(patched_env)
        return cls.from_env(env)

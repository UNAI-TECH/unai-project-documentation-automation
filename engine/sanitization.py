"""Shared secret sanitization and structured payload redaction utilities.

Kept free of internal `engine.*` imports so config, database, git reader,
report parser, migration runner, and audit modules can import it safely
without circular dependencies.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SENSITIVE_QUERY_KEYS = frozenset(
    {"password", "pass", "pwd", "apikey", "api_key", "token", "secret", "access_token", "jwt"}
)

SENSITIVE_KEY_EXACT = frozenset(
    {
        "password",
        "passwd",
        "pass",
        "pwd",
        "secret",
        "secrets",
        "token",
        "tokens",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "bearer_token",
        "github_token",
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "proxy_authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "client_secret",
        "signing_secret",
        "webhook_secret",
        "session_key",
        "cookie",
        "set_cookie",
        "dsn",
        "database_url",
        "migration_database_url",
        "connection_string",
        "service_role_key",
        "anon_key",
    }
)

SENSITIVE_KEY_SUFFIXES = (
    "_password",
    "_passwd",
    "_pwd",
    "_secret",
    "_secrets",
    "_token",
    "_tokens",
    "_api_key",
    "_apikey",
    "_private_key",
    "_credential",
    "_credentials",
    "_dsn",
)

DSN_LOG_RE = re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)([^@\s]+)(@)", re.IGNORECASE)
URI_QUERY_SECRET_RE = re.compile(
    r"([?&](?:password|pass|pwd|apikey|api_key|token|secret|access_token|jwt)=)([^&\s'\"]+)",
    re.IGNORECASE,
)
KV_SECRET_RE = re.compile(
    r"((?:password|pass|pwd|apikey|api_key|token|secret|access_token)\s*=\s*)([^\s;'\"]+)",
    re.IGNORECASE,
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)
GITHUB_TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
SUPABASE_KEY_RE = re.compile(r"\b(sb_(?:publishable|secret)_[A-Za-z0-9_-]{12,})\b")
API_KEY_RE = re.compile(r"\b(sk-(?:or-v1-)?[A-Za-z0-9_-]{16,})\b")
AWS_KEY_RE = re.compile(r"\b(AKIA[0-9A-Z]{16})\b")
BEARER_TOKEN_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*")
JWT_RE = re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")


def is_sensitive_key(key: str) -> bool:
    """Return True if a dictionary/header/field key likely holds a credential."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    if not normalized:
        return False
    if normalized in SENSITIVE_KEY_EXACT:
        return True
    return normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_dsn(dsn: str) -> str:
    """Return a PostgreSQL DSN with password and sensitive query params masked."""
    if not dsn:
        return "<empty>"
    try:
        parsed = urlparse(dsn)
    except ValueError:
        return "<redacted-invalid-dsn>"

    if not parsed.scheme:
        return "<redacted-non-uri>"

    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, hostpart = netloc.rsplit("@", 1)
        if ":" in userinfo:
            user, _ = userinfo.split(":", 1)
            netloc = f"{user}:***@{hostpart}"
        else:
            netloc = f"***@{hostpart}"

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS:
            query_items.append((key, "***"))
        else:
            query_items.append((key, value))

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            urlencode(query_items),
            "",
        )
    )


def sanitize_log_text(text: str) -> str:
    """Scrub DSN passwords, query secrets, GitHub/AWS/Supabase/API tokens, JWTs, and PEM keys."""
    scrubbed = PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", text)
    scrubbed = DSN_LOG_RE.sub(r"\1***\3", scrubbed)
    scrubbed = URI_QUERY_SECRET_RE.sub(r"\1***", scrubbed)
    scrubbed = GITHUB_TOKEN_RE.sub("[REDACTED_GITHUB_TOKEN]", scrubbed)
    scrubbed = SUPABASE_KEY_RE.sub("[REDACTED_SUPABASE_KEY]", scrubbed)
    scrubbed = API_KEY_RE.sub("[REDACTED_API_KEY]", scrubbed)
    scrubbed = AWS_KEY_RE.sub("[REDACTED_AWS_KEY]", scrubbed)
    scrubbed = BEARER_TOKEN_RE.sub(r"\1[REDACTED_TOKEN]", scrubbed)
    scrubbed = JWT_RE.sub("[REDACTED_JWT]", scrubbed)
    scrubbed = KV_SECRET_RE.sub(r"\1***", scrubbed)
    return scrubbed


scrub_secrets = sanitize_log_text


def sanitize_value(value: Any, *, redact_scalars: bool = False) -> Any:
    """Recursively sanitize strings, nested dicts, lists, and tuples before persistence or logging."""
    if isinstance(value, str):
        return "[REDACTED]" if redact_scalars else sanitize_log_text(value)
    if isinstance(value, (int, float, bool)) and redact_scalars:
        return "[REDACTED]"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            key_str = sanitize_log_text(str(raw_key))
            child_redact = redact_scalars or is_sensitive_key(key_str)
            cleaned[key_str] = sanitize_value(raw_val, redact_scalars=child_redact)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, redact_scalars=redact_scalars) for item in value]
    return value


class SanitizingFilter(logging.Filter):
    """Logging filter that redacts database credentials and tokens before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = sanitize_log_text(rendered)
        record.args = ()
        return True


def configure_sanitized_logger(name: str = "utda.database") -> logging.Logger:
    """Return a logger guarded by SanitizingFilter."""
    logger = logging.getLogger(name)
    if not any(isinstance(f, SanitizingFilter) for f in logger.filters):
        logger.addFilter(SanitizingFilter())
    return logger

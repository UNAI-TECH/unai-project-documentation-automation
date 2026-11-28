"""Configuration loading and validation for Phase 1 UTDA Core Engine."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import jsonschema

from engine.contracts import load_json, validate_against_schema
from engine.db_config import validate_project_id
from engine.sanitization import sanitize_log_text


class ConfigError(ValueError):
    """Raised when registry, matrix, or project configuration is invalid."""


class ProjectIdentityError(ConfigError):
    """Raised when authoritative project resolution or identity validation fails."""

    def __init__(self, message: str, *, code: str = "ERR_PROJECT_IDENTITY") -> None:
        self.code = code
        safe_message = sanitize_log_text(message)
        super().__init__(f"[{code}] {safe_message}")


def is_dry_run(environ: Mapping[str, str] | None = None) -> bool:
    """Return True unless UTDA_DRY_RUN is explicitly set to 'false'."""
    env = os.environ if environ is None else environ
    return env.get("UTDA_DRY_RUN", "true").strip().lower() != "false"


def load_document_registry(utda_root: Path) -> dict[str, Any]:
    """Load config/document_registry.json."""
    path = utda_root / "config" / "document_registry.json"
    if not path.is_file():
        raise ConfigError(f"Missing document registry at {path}")
    return load_json(path)


def load_impact_matrix(utda_root: Path) -> dict[str, Any]:
    """Load config/impact_matrix.json."""
    path = utda_root / "config" / "impact_matrix.json"
    if not path.is_file():
        raise ConfigError(f"Missing impact matrix at {path}")
    return load_json(path)


def load_project_registry(
    registry_path: Path, schemas_dir: Path
) -> dict[str, Any]:
    """Load and validate the central multi-project registry collection."""
    if not registry_path.is_file():
        raise ConfigError(f"Missing project registry at {registry_path}")
    data = load_json(registry_path)
    try:
        validate_against_schema(
            data, "project_registry_collection.schema.json", schemas_dir
        )
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"Invalid project registry: {exc.message}") from exc
    return data


def load_local_project_config(
    repo_dir: Path, schemas_dir: Path
) -> dict[str, Any] | None:
    """Load and validate Repo C's documentation/project-config.json if present."""
    path = repo_dir / "documentation" / "project-config.json"
    if not path.is_file():
        return None
    data = load_json(path)
    try:
        validate_against_schema(data, "project_config_local.schema.json", schemas_dir)
    except jsonschema.ValidationError as exc:
        raise ConfigError(f"Invalid local project-config.json: {exc.message}") from exc
    return data


def _parse_trusted_repository(repository: str | None) -> tuple[str, str]:
    """Validate and split a trusted GitHub 'owner/repo' slug without normalizing whitespace."""
    if not isinstance(repository, str) or not repository:
        raise ProjectIdentityError(
            "Trusted GitHub repository ('owner/repo') is required for project resolution.",
            code="ERR_MISSING_REPOSITORY",
        )
    if repository != repository.strip() or any(ch.isspace() for ch in repository):
        raise ProjectIdentityError(
            f"Invalid repository identifier '{repository}': whitespace is not permitted.",
            code="ERR_INVALID_REPOSITORY",
        )
    if repository.count("/") != 1:
        raise ProjectIdentityError(
            f"Invalid repository identifier '{repository}': expected exact 'owner/repo' format.",
            code="ERR_INVALID_REPOSITORY",
        )
    owner, repo = repository.split("/", 1)
    if not owner or not repo:
        raise ProjectIdentityError(
            f"Invalid repository identifier '{repository}': owner and repo must be non-empty.",
            code="ERR_INVALID_REPOSITORY",
        )
    return owner, repo


def _validate_strict_project_id(raw_project_id: str, *, code: str, field_name: str) -> str:
    try:
        return validate_project_id(raw_project_id, field_name=field_name)
    except ValueError as exc:
        raise ProjectIdentityError(str(exc), code=code) from exc


def resolve_project(
    registry: dict[str, Any],
    *,
    project_id: str | None = None,
    repository: str | None = None,
    local_project_id: str | None = None,
) -> dict[str, Any]:
    """Authoritatively resolve a project exclusively by trusted GitHub 'owner/repo'.

    Caller-supplied UTDA_PROJECT_ID and local Repo C project-config.json IDs can
    confirm—never override—repository ownership and are validated strictly without
    silent whitespace or case normalization.
    """
    owner, repo = _parse_trusted_repository(repository)
    projects = registry.get("projects", [])
    matches: list[dict[str, Any]] = []
    for entry in projects:
        gh = entry.get("github", {})
        reg_owner = str(gh.get("owner", ""))
        reg_repo = str(gh.get("repo", ""))
        if not reg_owner or not reg_repo or any(c.isspace() for c in reg_owner + reg_repo):
            continue
        if reg_owner.lower() == owner.lower() and reg_repo.lower() == repo.lower():
            matches.append(entry)

    if not matches:
        raise ProjectIdentityError(
            f"Repository '{owner}/{repo}' is not registered in Repo B project registry.",
            code="ERR_PROJECT_NOT_REGISTERED",
        )
    if len(matches) > 1:
        raise ProjectIdentityError(
            f"Repository '{owner}/{repo}' matches multiple registry entries.",
            code="ERR_AMBIGUOUS_REPOSITORY",
        )

    entry = matches[0]
    if str(entry.get("status") or "active") == "archived":
        raise ProjectIdentityError(
            f"Project '{entry.get('project_id', '<unknown>')}' for '{owner}/{repo}' is archived.",
            code="ERR_PROJECT_ARCHIVED",
        )

    canonical_id = _validate_strict_project_id(
        entry.get("project_id", ""),
        code="ERR_INVALID_REGISTRY_PROJECT_ID",
        field_name="registry.project_id",
    )

    if project_id is not None:
        supplied = _validate_strict_project_id(
            project_id,
            code="ERR_INVALID_PROJECT_ID",
            field_name="UTDA_PROJECT_ID",
        )
        if supplied != canonical_id:
            raise ProjectIdentityError(
                f"Caller UTDA_PROJECT_ID '{supplied}' does not match canonical project '{canonical_id}' "
                f"for repository '{owner}/{repo}'.",
                code="ERR_PROJECT_ID_MISMATCH",
            )

    if local_project_id is not None:
        local_id = _validate_strict_project_id(
            local_project_id,
            code="ERR_INVALID_LOCAL_PROJECT_ID",
            field_name="project-config.json project_id",
        )
        if local_id != canonical_id:
            raise ProjectIdentityError(
                f"Local documentation/project-config.json project_id '{local_id}' does not match "
                f"canonical project '{canonical_id}' for repository '{owner}/{repo}'.",
                code="ERR_LOCAL_CONFIG_PROJECT_MISMATCH",
            )

    return entry

"""Configuration loading and validation for Phase 1 UTDA Core Engine."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import jsonschema

from engine.contracts import load_json, validate_against_schema


class ConfigError(ValueError):
    """Raised when registry, matrix, or project configuration is invalid."""


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


def resolve_project(
    registry: dict[str, Any],
    *,
    project_id: str | None = None,
    repository: str | None = None,
) -> dict[str, Any]:
    """Find a project entry by project_id or GitHub 'owner/repo'."""
    projects = registry.get("projects", [])
    if project_id:
        for entry in projects:
            if entry.get("project_id") == project_id:
                return entry
    if repository and "/" in repository:
        owner, repo = repository.split("/", 1)
        for entry in projects:
            gh = entry.get("github", {})
            if (
                gh.get("owner", "").lower() == owner.lower()
                and gh.get("repo", "").lower() == repo.lower()
            ):
                return entry
    raise ConfigError(
        f"Project not found in registry (project_id={project_id!r}, repository={repository!r})."
    )

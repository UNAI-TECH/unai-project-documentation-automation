"""Phase 0 architectural constants, mappings, and schema validation contracts.

Does not execute network, OpenRouter, GitHub write, or Google Drive operations.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

AUTHORITY_MODE = "GITHUB_AUTHORITY"
MASTER_RECORD_SCOPE = "BOTH"
PROJECT_MASTER_ID = "MASTER"
COMPANY_MASTER_ID = "COMPANY_MASTER"

# Deterministic bridge from schemas/feature_report.schema.json `change_type` values
# to config/impact_matrix.json `rules[].change_type` keys.
CHANGE_TYPE_TO_IMPACT_MATRIX: dict[str, tuple[str, ...]] = {
    "new_feature": ("functional_behavior",),
    "feature_update": ("functional_behavior",),
    "bug_fix": ("functional_behavior",),
    "refactor": ("api_or_backend",),
    "security_change": ("api_or_backend",),
    "database_change": ("database_schema",),
    "api_change": ("api_or_backend",),
    "ui_change": ("ui_ux",),
    "deployment": ("deployment",),
    "test_execution": ("test_execution",),
    "other": (),
}


def load_json(path: Path) -> Any:
    """Load and parse a UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def build_schema_registry(schemas_dir: Path) -> Registry:
    """Build a referencing.Registry covering all local JSON schemas."""
    registry = Registry()
    for schema_file in sorted(schemas_dir.glob("*.json")):
        schema = load_json(schema_file)
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(schema_file.name, resource)
        if "$id" in schema:
            registry = registry.with_resource(schema["$id"], resource)
    return registry


def validate_against_schema(
    instance: Any, schema_name: str, schemas_dir: Path
) -> None:
    """Validate an in-memory object against a named JSON schema in schemas_dir."""
    schema_path = schemas_dir / schema_name
    schema = load_json(schema_path)
    jsonschema.Draft202012Validator.check_schema(schema)
    registry = build_schema_registry(schemas_dir)
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    validator.validate(instance)

"""Canonical JSON feature report locator and strict schema/evidence validator."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import jsonschema

from engine.contracts import validate_against_schema


@dataclass(frozen=True)
class ParsedReportResult:
    status: str  # VALID | MISSING | INVALID
    report: dict[str, Any] | None = None
    report_path: str | None = None
    errors: tuple[str, ...] = field(default_factory=tuple)


def _validate_evidence_rules(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for idx, test in enumerate(report.get("tests", [])):
        if test.get("result") == "passed" and not str(test.get("evidence", "")).strip():
            errors.append(
                f"tests[{idx}] ('{test.get('name', 'unnamed')}') claims 'passed' without execution evidence."
            )

    dep_status = report.get("deployment_status")
    if dep_status in ("staging", "production"):
        evidence = report.get("evidence", [])
        has_deploy_ref = any(
            "deploy" in str(ev.get("kind", "")).lower()
            or "release" in str(ev.get("kind", "")).lower()
            or "deploy" in str(ev.get("description", "")).lower()
            for ev in evidence
        )
        if not has_deploy_ref:
            errors.append(
                f"deployment_status='{dep_status}' requires explicit deployment evidence."
            )
    return errors


def locate_and_parse_report(
    repo_dir: Path,
    *,
    schemas_dir: Path,
    report_dir: str = "documentation/reports/final",
    feature_id: str | None = None,
    expected_project_id: str | None = None,
    changed_files: tuple[str, ...] = (),
) -> ParsedReportResult:
    """Locate canonical <feature-id>.json report and validate schema and evidence."""
    norm_dir = report_dir.strip("/").replace("\\", "/")
    target_dir = repo_dir / norm_dir

    candidate_path: Path | None = None
    if feature_id:
        explicit = target_dir / f"{feature_id}.json"
        if explicit.is_file():
            candidate_path = explicit
    else:
        changed_jsons = [
            repo_dir / f
            for f in changed_files
            if f.startswith(f"{norm_dir}/") and f.endswith(".json") and (repo_dir / f).is_file()
        ]
        if len(changed_jsons) == 1:
            candidate_path = changed_jsons[0]
        elif len(changed_jsons) > 1:
            return ParsedReportResult(
                status="INVALID",
                errors=("Multiple changed JSON reports found; specify feature_id explicitly.",),
            )
        elif target_dir.is_dir():
            all_jsons = sorted(target_dir.glob("*.json"))
            if len(all_jsons) == 1:
                candidate_path = all_jsons[0]

    if candidate_path is None:
        md_hint = (
            target_dir.is_dir()
            and bool(list(target_dir.glob("*.md")))
        )
        errs = (
            ("Markdown companion report found without canonical .json report.",)
            if md_hint
            else ()
        )
        return ParsedReportResult(status="MISSING", errors=errs)

    rel_path = candidate_path.relative_to(repo_dir).as_posix()
    try:
        payload = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ParsedReportResult(
            status="INVALID",
            report_path=rel_path,
            errors=(f"Failed to parse JSON report {rel_path}: {exc}",),
        )

    try:
        validate_against_schema(payload, "feature_report.schema.json", schemas_dir)
    except jsonschema.ValidationError as exc:
        return ParsedReportResult(
            status="INVALID",
            report_path=rel_path,
            errors=(f"Schema validation error: {exc.message}",),
        )

    errors: list[str] = []
    if feature_id and payload.get("feature_id") != feature_id:
        errors.append(
            f"feature_id mismatch: expected '{feature_id}', found '{payload.get('feature_id')}'."
        )
    if candidate_path.stem != payload.get("feature_id"):
        errors.append(
            f"Filename stem '{candidate_path.stem}' does not match feature_id '{payload.get('feature_id')}'."
        )
    if expected_project_id and payload.get("project_id") != expected_project_id:
        errors.append(
            f"project_id mismatch: expected '{expected_project_id}', found '{payload.get('project_id')}'."
        )
    errors.extend(_validate_evidence_rules(payload))

    if errors:
        return ParsedReportResult(
            status="INVALID",
            report=payload,
            report_path=rel_path,
            errors=tuple(errors),
        )

    return ParsedReportResult(status="VALID", report=payload, report_path=rel_path)

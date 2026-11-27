"""Canonical JSON feature report locator and strict schema/evidence validator."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import jsonschema

from engine.contracts import validate_against_schema
from engine.sanitization import sanitize_log_text


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
    report_candidates: tuple[str, ...] | None = None,
    blob_reader: Callable[[str], str] | None = None,
) -> ParsedReportResult:
    """Locate canonical <feature-id>.json report from committed tree and validate schema/evidence."""
    norm_dir = report_dir.strip("/").replace("\\", "/")
    target_dir = repo_dir / norm_dir

    selected_rel: str | None = None
    if report_candidates is not None:
        # Strict Git-tree mode: only examine committed JSON blobs from the requested commit.
        committed = tuple(
            p for p in report_candidates if p.startswith(f"{norm_dir}/") and p.endswith(".json")
        )
        if feature_id:
            target_rel = f"{norm_dir}/{feature_id}.json"
            if target_rel in committed:
                selected_rel = target_rel
        else:
            committed_set = set(committed)
            changed_jsons = [
                f
                for f in changed_files
                if f.startswith(f"{norm_dir}/") and f.endswith(".json") and f in committed_set
            ]
            if len(changed_jsons) == 1:
                selected_rel = changed_jsons[0]
            elif len(changed_jsons) > 1:
                return ParsedReportResult(
                    status="INVALID",
                    errors=("Multiple changed JSON reports found; specify feature_id explicitly.",),
                )
            elif len(committed) == 1:
                selected_rel = committed[0]
    else:
        if feature_id:
            explicit = target_dir / f"{feature_id}.json"
            if explicit.is_file():
                selected_rel = explicit.relative_to(repo_dir).as_posix()
        else:
            changed_jsons = [
                f
                for f in changed_files
                if f.startswith(f"{norm_dir}/") and f.endswith(".json") and (repo_dir / f).is_file()
            ]
            if len(changed_jsons) == 1:
                selected_rel = changed_jsons[0]
            elif len(changed_jsons) > 1:
                return ParsedReportResult(
                    status="INVALID",
                    errors=("Multiple changed JSON reports found; specify feature_id explicitly.",),
                )
            elif target_dir.is_dir():
                all_jsons = sorted(target_dir.glob("*.json"))
                if len(all_jsons) == 1:
                    selected_rel = all_jsons[0].relative_to(repo_dir).as_posix()

    if selected_rel is None:
        md_hint = target_dir.is_dir() and bool(list(target_dir.glob("*.md")))
        errs = (
            ("Markdown companion report found without canonical .json report.",)
            if md_hint
            else ()
        )
        return ParsedReportResult(status="MISSING", errors=errs)

    try:
        if blob_reader is not None:
            raw_text = blob_reader(selected_rel)
        else:
            raw_text = (repo_dir / selected_rel).read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return ParsedReportResult(
            status="INVALID",
            report_path=selected_rel,
            errors=(sanitize_log_text(f"Failed to parse JSON report {selected_rel}: {exc}"),),
        )

    try:
        validate_against_schema(payload, "feature_report.schema.json", schemas_dir)
    except jsonschema.ValidationError as exc:
        return ParsedReportResult(
            status="INVALID",
            report_path=selected_rel,
            errors=(sanitize_log_text(f"Schema validation error: {exc.message}"),),
        )

    errors: list[str] = []
    if feature_id and payload.get("feature_id") != feature_id:
        errors.append(
            f"feature_id mismatch: expected '{feature_id}', found '{payload.get('feature_id')}'."
        )
    candidate_stem = PurePosixPath(selected_rel).stem
    if candidate_stem != payload.get("feature_id"):
        errors.append(
            f"Filename stem '{candidate_stem}' does not match feature_id '{payload.get('feature_id')}'."
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
            report_path=selected_rel,
            errors=tuple(sanitize_log_text(e) for e in errors),
        )

    return ParsedReportResult(status="VALID", report=payload, report_path=selected_rel)

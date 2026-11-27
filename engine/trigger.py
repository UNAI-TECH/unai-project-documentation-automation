"""Phase 1 event normalization, correlation, and durable state orchestrator.

Respects UTDA_DRY_RUN=true and Phase 1 boundaries: does NOT invoke AI section
generation, DOCX edits, Google Drive writes, provisioning, or PR creation.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

from engine.audit_store import AuditStore, make_event_key, make_idempotency_key
from engine.config_loader import (
    ConfigError,
    is_dry_run,
    load_local_project_config,
    load_project_registry,
    resolve_project,
)
from engine.contracts import AUTHORITY_MODE, MASTER_RECORD_SCOPE
from engine.database import DatabaseManager, UTDADatabaseRepository
from engine.db_config import DatabaseConfig, DatabaseConfigError
from engine.github_reader import (
    GitEvidenceError,
    read_git_blob_text,
    read_git_evidence,
    validate_git_sha,
)
from engine.report_parser import locate_and_parse_report
from engine.sanitization import sanitize_log_text


@dataclass(frozen=True)
class NormalizedEvent:
    event_name: str
    delivery_id: str
    repository: str
    git_sha: str
    pr_number: int | None
    pr_merged: bool
    feature_id: str | None
    project_id: str | None
    report_dir: str
    dry_run: bool
    base_sha: str | None = None


def _read_github_event_payload(env: Mapping[str, str]) -> dict[str, Any]:
    event_path = env.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path:
        return {}
    path = Path(event_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def normalize_event(environ: Mapping[str, str] | None = None) -> NormalizedEvent:
    """Normalize GitHub Actions / webhook env vars into a canonical event object."""
    env = os.environ if environ is None else environ
    payload = _read_github_event_payload(env)

    event_name = env.get("GITHUB_EVENT_NAME", "").strip() or "workflow_dispatch"
    repository = (
        env.get("GITHUB_REPOSITORY", "").strip()
        or payload.get("repository", {}).get("full_name", "")
    )
    git_sha = validate_git_sha(
        env.get("GITHUB_SHA", "").strip() or "HEAD",
        field_name="GITHUB_SHA",
    )

    pr_obj = payload.get("pull_request", {})
    raw_base = (
        env.get("UTDA_BASE_SHA", "").strip()
        or str(pr_obj.get("base", {}).get("sha") or "").strip()
        or str(payload.get("before") or "").strip()
    )
    base_sha: str | None = None
    if raw_base and not re.fullmatch(r"0{40,64}", raw_base):
        base_sha = validate_git_sha(raw_base, allow_head=False, field_name="base_sha")

    raw_pr = env.get("PR_NUMBER", "").strip() or str(pr_obj.get("number") or "").strip()
    pr_number = int(raw_pr) if raw_pr.isdigit() else None

    if event_name == "pull_request":
        if "merged" in pr_obj:
            pr_merged = bool(pr_obj["merged"])
        else:
            pr_merged = env.get("UTDA_PR_MERGED", "").strip().lower() == "true"
    else:
        pr_merged = False

    feature_id = (
        env.get("FEATURE_ID", "").strip()
        or payload.get("inputs", {}).get("feature_id", "").strip()
        or None
    )
    if not feature_id and pr_obj:
        for candidate in (pr_obj.get("title", ""), pr_obj.get("head", {}).get("ref", "")):
            match = re.search(r"\b([A-Z]{2,12}-\d+)\b", str(candidate))
            if match:
                feature_id = match.group(1)
                break
    project_id = env.get("UTDA_PROJECT_ID", "").strip() or None
    report_dir = env.get("UTDA_REPORT_PATH", "").strip() or "documentation/reports/final"

    delivery_id = (
        env.get("UTDA_DELIVERY_ID", "").strip()
        or (
            f"{repository}:{event_name}:{env.get('GITHUB_RUN_ID', 'local')}:"
            f"{env.get('GITHUB_RUN_ATTEMPT', '1')}:{git_sha}"
        )
    )

    return NormalizedEvent(
        event_name=event_name,
        delivery_id=delivery_id,
        repository=repository,
        git_sha=git_sha,
        pr_number=pr_number,
        pr_merged=pr_merged,
        feature_id=feature_id,
        project_id=project_id,
        report_dir=report_dir,
        dry_run=is_dry_run(env),
        base_sha=base_sha,
    )


def process_event(
    event: NormalizedEvent,
    *,
    repo_dir: Path,
    utda_dir: Path,
    audit: AuditStore,
) -> dict[str, Any]:
    """Execute Phase 1 correlation, validation, persistence, and idempotency checks."""
    if event.event_name == "pull_request" and not event.pr_merged:
        result = {
            "status": "IGNORED_UNMERGED_PR",
            "authority_mode": AUTHORITY_MODE,
            "master_record_scope": MASTER_RECORD_SCOPE,
            "dry_run": True,
            "pr_number": event.pr_number,
        }
        audit.export_artifact("audit_record.json", result)
        return result

    schemas_dir = utda_dir / "schemas"
    registry_path = utda_dir / os.getenv("UTDA_REGISTRY_PATH", "config/project_registry.json")
    registry = load_project_registry(registry_path, schemas_dir)
    local_cfg = load_local_project_config(repo_dir, schemas_dir)
    resolved_project_id = event.project_id or (local_cfg or {}).get("project_id")
    project_entry = resolve_project(
        registry,
        project_id=resolved_project_id,
        repository=event.repository,
    )
    project_id = project_entry["project_id"]
    audit.sync_project(project_entry)

    evidence = read_git_evidence(
        repo_dir,
        repository=event.repository,
        git_sha=event.git_sha,
        base_sha=event.base_sha,
        pr_number=event.pr_number,
        report_dir=event.report_dir,
    )
    parsed = locate_and_parse_report(
        repo_dir,
        schemas_dir=schemas_dir,
        report_dir=event.report_dir,
        feature_id=event.feature_id,
        expected_project_id=project_id,
        changed_files=evidence.changed_files,
        report_candidates=evidence.report_candidates,
        blob_reader=lambda rel: read_git_blob_text(repo_dir, rel, git_sha=evidence.git_sha),
    )

    feature_id = (
        (parsed.report or {}).get("feature_id")
        or event.feature_id
        or (f"PR-{event.pr_number}" if event.pr_number else "UNRESOLVED")
    )
    report_blob_sha = (
        evidence.report_blob_shas.get(parsed.report_path or "", "")
        if parsed.report_path
        else ""
    )

    receipt = audit.record_receipt(
        delivery_id=event.delivery_id,
        event_name=event.event_name,
        repository=event.repository,
        project_id=project_id,
        feature_id=feature_id,
        git_sha=evidence.git_sha,
        pr_number=event.pr_number,
        report_blob_sha=report_blob_sha or None,
        payload_summary={
            "dry_run": True,
            "changed_count": len(evidence.changed_files),
            "base_sha": evidence.base_sha,
        },
    )
    if not receipt["inserted"]:
        result = {
            "status": "SKIPPED_DUPLICATE_RECEIPT",
            "project_id": project_id,
            "feature_id": feature_id,
            "delivery_id": event.delivery_id,
            "dry_run": True,
        }
        audit.export_artifact("audit_record.json", result)
        return result

    if parsed.status == "MISSING":
        state = audit.correlate_feature(
            project_id=project_id,
            feature_id=feature_id,
            pr_number=event.pr_number,
            pr_merged_sha=evidence.git_sha if event.event_name == "pull_request" else None,
            force_status="WAITING_FOR_FINAL_REPORT",
        )
        audit.append_audit(
            actor="utda.trigger",
            event_type="WAITING_FOR_FINAL_REPORT",
            project_id=project_id,
            feature_id=feature_id,
            details={"reason": parsed.errors or ["Final JSON report not yet present"]},
        )
        result = {
            "status": state["status"],
            "project_id": project_id,
            "feature_id": feature_id,
            "dry_run": True,
        }
        audit.export_artifact("audit_record.json", result)
        return result

    if parsed.status == "INVALID":
        state = audit.correlate_feature(
            project_id=project_id,
            feature_id=feature_id,
            pr_number=event.pr_number,
            pr_merged_sha=evidence.git_sha if event.event_name == "pull_request" else None,
            force_status="VALIDATION_FAILED",
        )
        audit.append_audit(
            actor="utda.trigger",
            event_type="REPORT_VALIDATION_FAILED",
            project_id=project_id,
            feature_id=feature_id,
            details={"errors": list(parsed.errors), "report_path": parsed.report_path},
        )
        result = {
            "status": "VALIDATION_FAILED",
            "project_id": project_id,
            "feature_id": feature_id,
            "errors": list(parsed.errors),
            "dry_run": True,
        }
        audit.export_artifact("audit_record.json", result)
        return result

    assert parsed.report is not None and parsed.report_path is not None
    audit.record_report(
        project_id=project_id,
        report=parsed.report,
        report_path=parsed.report_path,
        report_blob_sha=report_blob_sha or "local-sha",
        git_commit_sha=evidence.git_sha,
    )

    pr_merged_sha = (
        evidence.git_sha
        if event.event_name in ("pull_request", "workflow_dispatch")
        else None
    )
    corr = audit.correlate_feature(
        project_id=project_id,
        feature_id=feature_id,
        pr_number=event.pr_number,
        pr_merged_sha=pr_merged_sha,
        report_blob_sha=report_blob_sha or "local-sha",
    )

    if corr["status"] != "READY":
        audit.append_audit(
            actor="utda.trigger",
            event_type=corr["status"],
            project_id=project_id,
            feature_id=feature_id,
            details={"report_path": parsed.report_path},
        )
        result = {
            "status": corr["status"],
            "project_id": project_id,
            "feature_id": feature_id,
            "report_path": parsed.report_path,
            "dry_run": True,
        }
        audit.export_artifact("audit_record.json", result)
        return result

    event_key = make_event_key(event.repository, feature_id, report_blob_sha or "local-sha")
    idem_key = make_idempotency_key(
        event.repository, feature_id, report_blob_sha or "local-sha", "PHASE1_CORRELATION"
    )
    claimed, attempt_no, attempt_id = audit.claim_processing_lock(
        idempotency_key=idem_key,
        scope="phase1_correlation",
        project_id=project_id,
        feature_id=feature_id,
        report_blob_sha=report_blob_sha or "local-sha",
        target_document_id="PHASE1_CORRELATION",
        event_receipt_id=receipt["receipt_id"],
    )
    if not claimed or not attempt_id:
        result = {
            "status": "SKIPPED_DUPLICATE",
            "project_id": project_id,
            "feature_id": feature_id,
            "event_key": event_key,
            "idempotency_key": idem_key,
            "dry_run": True,
        }
        audit.export_artifact("audit_record.json", result)
        return result

    finalized = False
    try:
        audit.record_attempt_outcome(
            event_receipt_id=receipt["receipt_id"],
            idempotency_key=idem_key,
            attempt_id=attempt_id,
            attempt_number=attempt_no,
            succeeded=True,
            final_key_status="COMPLETED",
            result_summary={"phase": 1, "dry_run": True, "report_path": parsed.report_path},
        )
        finalized = True
    except Exception as exc:
        if not finalized:
            try:
                audit.record_attempt_outcome(
                    event_receipt_id=receipt["receipt_id"],
                    idempotency_key=idem_key,
                    attempt_id=attempt_id,
                    attempt_number=attempt_no,
                    succeeded=False,
                    final_key_status="FAILED",
                    retry_state="RETRY_SCHEDULED",
                    error_code=type(exc).__name__,
                    error_summary=str(exc),
                )
            except Exception:
                pass
        raise
    audit.append_audit(
        actor="utda.trigger",
        event_type="PHASE1_CORRELATED_DRY_RUN",
        project_id=project_id,
        feature_id=feature_id,
        idempotency_key=idem_key,
        details={"event_key": event_key, "report_path": parsed.report_path},
    )
    result = {
        "status": "DRY_RUN_READY",
        "authority_mode": AUTHORITY_MODE,
        "master_record_scope": MASTER_RECORD_SCOPE,
        "project_id": project_id,
        "feature_id": feature_id,
        "event_key": event_key,
        "idempotency_key": idem_key,
        "report_path": parsed.report_path,
        "dry_run": True,
    }
    audit.export_artifact("audit_record.json", result)
    return result


def main() -> int:
    utda_dir = Path(__file__).resolve().parent.parent
    repo_dir = Path(os.getenv("UTDA_REPO_DIR", str(utda_dir)))
    artifacts_dir = utda_dir / "artifacts"
    try:
        event = normalize_event()
    except GitEvidenceError as exc:
        print(f"Git evidence error: {sanitize_log_text(str(exc))}", file=sys.stderr)
        return 2

    db_manager: DatabaseManager | None = None
    db_repo: UTDADatabaseRepository | None = None
    if os.getenv("UTDA_DATABASE_URL", "").strip():
        try:
            db_cfg = DatabaseConfig.from_env()
            db_manager = DatabaseManager(db_cfg)
            db_manager.open()
            db_repo = UTDADatabaseRepository(db_manager)
        except DatabaseConfigError as exc:
            print(f"Database config error: {sanitize_log_text(str(exc))}", file=sys.stderr)
            return 2

    try:
        audit = AuditStore(repo=db_repo, artifacts_dir=artifacts_dir)
        result = process_event(
            event, repo_dir=repo_dir, utda_dir=utda_dir, audit=audit
        )
        print(json.dumps(result, indent=2))
        return 1 if result["status"] == "VALIDATION_FAILED" else 0
    except (ConfigError, GitEvidenceError) as exc:
        print(f"Configuration/Git error: {sanitize_log_text(str(exc))}", file=sys.stderr)
        return 2
    finally:
        if db_manager is not None:
            db_manager.close()


if __name__ == "__main__":
    raise SystemExit(main())

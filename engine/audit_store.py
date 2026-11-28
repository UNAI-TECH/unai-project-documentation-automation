"""Durable Phase 1 audit, correlation, idempotency, and retry-state store."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from engine.database import UTDADatabaseRepository
from engine.db_config import validate_project_id
from engine.sanitization import sanitize_value, scrub_secrets


def make_event_key(repository: str, feature_id: str, report_blob_sha: str) -> str:
    """Compute deterministic run-level event key."""
    raw = f"{repository}:{feature_id}:{report_blob_sha}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_idempotency_key(
    repository: str,
    feature_id: str,
    report_blob_sha: str,
    target_document_id: str,
) -> str:
    """Compute deterministic per-document idempotency key."""
    raw = f"{repository}:{feature_id}:{report_blob_sha}:{target_document_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuditStore:
    """Coordinates PostgreSQL persistence and sanitized CI artifact output."""

    def __init__(
        self,
        repo: UTDADatabaseRepository | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.repo = repo
        self.artifacts_dir = artifacts_dir

    def sync_project(self, project_entry: dict[str, Any]) -> None:
        pid = validate_project_id(project_entry.get("project_id", ""))  # type: ignore[arg-type]
        if not self.repo:
            return
        gh = project_entry["github"]
        drive = project_entry.get("drive", {})
        self.repo.upsert_project(
            project_id=pid,
            project_name=project_entry["project_name"],
            status=project_entry.get("status", "active"),
            github_owner=gh["owner"],
            github_repo=gh["repo"],
            default_branch=gh["default_branch"],
            drive_folder_id=drive.get("folder_id", ""),
            template_version=project_entry.get("template_version", ""),
            registry_metadata={
                "authority_mode": "GITHUB_AUTHORITY",
                "master_record_scope": "BOTH",
            },
        )

    def record_receipt(
        self,
        *,
        delivery_id: str,
        event_name: str,
        repository: str,
        project_id: str,
        feature_id: str | None,
        git_sha: str,
        pr_number: int | None,
        report_blob_sha: str | None,
        payload_summary: dict[str, Any],
    ) -> dict[str, Any]:
        pid = validate_project_id(project_id)
        safe_summary = sanitize_value(payload_summary)
        if not self.repo:
            return {"inserted": True, "receipt_id": f"local-{delivery_id}", "delivery_id": delivery_id}
        return self.repo.record_event_receipt(
            delivery_id=delivery_id,
            event_name=event_name,
            repository=repository,
            project_id=pid,
            feature_id=feature_id,
            git_sha=git_sha,
            pr_number=pr_number,
            report_blob_sha=report_blob_sha,
            payload_summary=safe_summary,
        )

    def correlate_feature(
        self,
        *,
        project_id: str,
        feature_id: str,
        pr_number: int | None = None,
        pr_merged_sha: str | None = None,
        report_blob_sha: str | None = None,
        force_status: str | None = None,
    ) -> dict[str, Any]:
        pid = validate_project_id(project_id)
        if not self.repo:
            status = force_status or (
                "READY"
                if (pr_merged_sha and report_blob_sha)
                else ("WAITING_FOR_FINAL_REPORT" if pr_merged_sha else "AWAIT_PR_MERGE")
            )
            return {
                "state_id": f"local-{pid}-{feature_id}",
                "status": status,
                "pr_merged_sha": pr_merged_sha,
                "report_blob_sha": report_blob_sha,
            }
        return self.repo.correlate_feature_state(
            project_id=pid,
            feature_id=feature_id,
            pr_number=pr_number,
            pr_merged_sha=pr_merged_sha,
            report_blob_sha=report_blob_sha,
            force_status=force_status,
        )

    def record_report(
        self,
        *,
        project_id: str,
        report: dict[str, Any],
        report_path: str,
        report_blob_sha: str,
        git_commit_sha: str,
    ) -> None:
        pid = validate_project_id(project_id)
        if not self.repo:
            return
        self.repo.record_final_report(
            project_id=pid,
            feature_id=report["feature_id"],
            feature_name=scrub_secrets(report["feature_name"]),
            change_type=report["change_type"],
            implementation_status=report["implementation_status"],
            report_path=report_path,
            report_blob_sha=report_blob_sha,
            git_commit_sha=git_commit_sha,
            report_metadata={"summary": scrub_secrets(report.get("summary", ""))},
            evidence_refs=sanitize_value(report.get("evidence", [])),
        )

    def claim_processing_lock(
        self,
        *,
        idempotency_key: str,
        scope: str,
        project_id: str,
        feature_id: str,
        report_blob_sha: str,
        target_document_id: str,
        event_receipt_id: str | None,
        lock_ttl_seconds: int = 900,
    ) -> tuple[bool, int, str | None]:
        pid = validate_project_id(project_id)
        if not self.repo:
            return True, 1, f"local-{idempotency_key}"
        claimed = self.repo.claim_idempotency_and_attempt(
            idempotency_key=idempotency_key,
            scope=scope,
            project_id=pid,
            feature_id=feature_id,
            report_blob_sha=report_blob_sha,
            target_document_id=target_document_id,
            event_receipt_id=event_receipt_id,
            lock_ttl_seconds=lock_ttl_seconds,
        )
        return bool(claimed["claimed"]), int(claimed["attempt_number"]), claimed.get("attempt_id")

    def record_attempt_outcome(
        self,
        *,
        project_id: str,
        event_receipt_id: str | None,
        idempotency_key: str,
        attempt_id: str,
        attempt_number: int | None = None,
        succeeded: bool,
        final_key_status: str,
        retry_state: str = "NONE",
        error_code: str | None = None,
        error_summary: str | None = None,
        result_summary: dict[str, Any] | None = None,
    ) -> None:
        pid = validate_project_id(project_id)
        if not self.repo:
            return
        self.repo.finalize_processing_attempt(
            project_id=pid,
            event_receipt_id=event_receipt_id,
            idempotency_key=idempotency_key,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            attempt_status="SUCCEEDED" if succeeded else "FAILED",
            final_key_status=final_key_status,
            retry_state=retry_state,
            error_code=error_code,
            error_summary=scrub_secrets(error_summary) if error_summary else None,
            result_summary=sanitize_value(result_summary) if result_summary else None,
        )

    def append_audit(
        self,
        *,
        actor: str,
        event_type: str,
        project_id: str,
        feature_id: str | None = None,
        document_id: str | None = None,
        idempotency_key: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        pid = validate_project_id(project_id)
        if self.repo:
            self.repo.append_audit_event(
                actor=actor,
                event_type=event_type,
                project_id=pid,
                feature_id=feature_id,
                document_id=document_id,
                idempotency_key=idempotency_key,
                details=sanitize_value(details) if details else None,
            )

    def export_artifact(self, filename: str, payload: dict[str, Any]) -> Path | None:
        if not self.artifacts_dir:
            return None
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        out = self.artifacts_dir / filename
        safe_payload = sanitize_value(payload)
        safe_text = scrub_secrets(json.dumps(safe_payload, indent=2, sort_keys=True))
        out.write_text(safe_text + "\n", encoding="utf-8")
        return out

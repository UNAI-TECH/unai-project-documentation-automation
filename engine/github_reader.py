"""GitHub and Git repository evidence collector with strict secret scrubbing."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any

from engine.db_config import sanitize_log_text

SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"), "[REDACTED_JWT]"),
)


def scrub_secrets(text: str) -> str:
    """Scrub common credentials, tokens, and DSN passwords from text."""
    cleaned = sanitize_log_text(text)
    for pattern, replacement in SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


@dataclass(frozen=True)
class EvidenceBundle:
    repository: str
    git_sha: str
    pr_number: int | None
    changed_files: tuple[str, ...]
    commit_messages: tuple[str, ...]
    report_candidates: tuple[str, ...]
    report_blob_shas: dict[str, str]
    pr_title: str = ""
    pr_body: str = ""


def _run_git(repo_dir: Path, args: list[str]) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def compute_blob_sha(repo_dir: Path, rel_path: str) -> str:
    """Return git blob SHA if tracked, or SHA-256 fallback of file bytes."""
    rev = _run_git(repo_dir, ["rev-parse", f"HEAD:{rel_path}"])
    if rev and len(rev) >= 40:
        return rev
    full = repo_dir / rel_path
    if full.is_file():
        return hashlib.sha256(full.read_bytes()).hexdigest()
    return ""


def read_git_evidence(
    repo_dir: Path,
    *,
    repository: str,
    git_sha: str,
    pr_number: int | None = None,
    report_dir: str = "documentation/reports/final",
    pr_title: str = "",
    pr_body: str = "",
    max_files: int = 200,
) -> EvidenceBundle:
    """Collect scrubbed changed files, commits, and report blob SHAs from repo_dir."""
    changed_raw = _run_git(
        repo_dir, ["diff-tree", "--no-commit-id", "--name-only", "-r", git_sha]
    )
    if not changed_raw:
        changed_raw = _run_git(repo_dir, ["ls-tree", "--name-only", "-r", "HEAD"]) or ""
    changed_files = tuple(
        line.strip().replace("\\", "/")
        for line in changed_raw.splitlines()
        if line.strip()
    )[:max_files]

    commits_raw = _run_git(repo_dir, ["log", "-n", "10", "--pretty=format:%h %s"]) or ""
    commit_messages = tuple(
        scrub_secrets(line.strip()) for line in commits_raw.splitlines() if line.strip()
    )

    norm_report_dir = report_dir.strip("/").replace("\\", "/")
    candidates: list[str] = []
    report_root = repo_dir / norm_report_dir
    if report_root.is_dir():
        for p in sorted(report_root.glob("*.json")):
            rel = p.relative_to(repo_dir).as_posix()
            candidates.append(rel)

    blob_shas = {rel: compute_blob_sha(repo_dir, rel) for rel in candidates}

    return EvidenceBundle(
        repository=repository,
        git_sha=git_sha,
        pr_number=pr_number,
        changed_files=changed_files,
        commit_messages=commit_messages,
        report_candidates=tuple(candidates),
        report_blob_shas=blob_shas,
        pr_title=scrub_secrets(pr_title),
        pr_body=scrub_secrets(pr_body),
    )

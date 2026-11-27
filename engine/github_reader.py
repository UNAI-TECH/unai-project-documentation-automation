"""GitHub and Git repository evidence collector with commit-pinned integrity and sanitization."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any

from engine.sanitization import sanitize_log_text, scrub_secrets

DEFAULT_GIT_TIMEOUT_SECONDS = 15.0
_GIT_REF_RE = re.compile(r"^(HEAD|[0-9a-fA-F]{7,64})$")
_ALL_ZERO_SHA_RE = re.compile(r"^0{40,64}$")
_LS_TREE_LINE_RE = re.compile(r"^[0-7]+\s+(blob|tree|commit)\s+([0-9a-fA-F]{40,64})\t(.+)$")


class GitEvidenceError(ValueError):
    """Raised when Git revision validation fails or a Git evidence command errors/times out."""


def validate_git_sha(ref: str, *, allow_head: bool = True, field_name: str = "git_sha") -> str:
    """Validate that a commit reference is a safe SHA hex string or (optionally) HEAD."""
    if not isinstance(ref, str):
        raise GitEvidenceError(f"Invalid {field_name}: expected string commit SHA.")
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith("-"):
        raise GitEvidenceError(f"Invalid {field_name}: unsafe or empty commit reference.")
    if not _GIT_REF_RE.match(cleaned):
        raise GitEvidenceError(
            f"Invalid {field_name} '{sanitize_log_text(cleaned)}': expected HEAD or 7-64 hex SHA."
        )
    if not allow_head and cleaned == "HEAD":
        raise GitEvidenceError(f"Invalid {field_name}: literal HEAD is not permitted here.")
    return cleaned


def validate_repo_rel_path(rel_path: str) -> str:
    """Normalize and validate a repository-relative POSIX path against traversal/flag injection."""
    if not isinstance(rel_path, str):
        raise GitEvidenceError("Invalid repository path: expected string.")
    if "\x00" in rel_path or "\n" in rel_path or "\r" in rel_path:
        raise GitEvidenceError("Invalid repository path: control characters are not permitted.")
    normalized = rel_path.strip().replace("\\", "/").strip("/")
    if not normalized or normalized.startswith("-"):
        raise GitEvidenceError("Invalid repository path: empty or unsafe prefix.")
    parts = PurePosixPath(normalized).parts
    if any(part in ("", ".", "..") for part in parts):
        raise GitEvidenceError(f"Invalid repository path '{sanitize_log_text(rel_path)}'.")
    return "/".join(parts)


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
    base_sha: str | None = None


def _run_git(
    repo_dir: Path,
    args: list[str],
    *,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Execute a git command with explicit timeout and fail-closed error handling."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        cmd_hint = args[0] if args else "git"
        raise GitEvidenceError(
            f"Git command '{cmd_hint}' timed out after {timeout:.1f}s."
        ) from exc
    except OSError as exc:
        raise GitEvidenceError(
            f"Failed to execute git command: {sanitize_log_text(str(exc))}"
        ) from exc

    if proc.returncode != 0:
        detail = sanitize_log_text((proc.stderr or proc.stdout or "").strip())
        cmd_hint = args[0] if args else "git"
        raise GitEvidenceError(
            f"Git command '{cmd_hint}' failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout


def resolve_commit_sha(
    repo_dir: Path,
    git_sha: str,
    *,
    allow_head: bool = True,
    field_name: str = "git_sha",
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Validate and resolve a commit reference to its canonical Git commit SHA."""
    safe_ref = validate_git_sha(git_sha, allow_head=allow_head, field_name=field_name)
    out = _run_git(
        repo_dir,
        ["rev-parse", "--verify", f"{safe_ref}^{{commit}}"],
        timeout=timeout,
    ).strip()
    if not _GIT_REF_RE.match(out) or out == "HEAD":
        raise GitEvidenceError(f"Unable to resolve commit SHA for {safe_ref}.")
    return out


def compute_blob_sha(
    repo_dir: Path,
    rel_path: str,
    *,
    git_sha: str = "HEAD",
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Return the Git blob SHA for rel_path strictly at the supplied commit git_sha (or '')."""
    safe_rel = validate_repo_rel_path(rel_path)
    resolved_sha = resolve_commit_sha(repo_dir, git_sha, timeout=timeout)
    tree_out = _run_git(
        repo_dir,
        ["ls-tree", "-r", resolved_sha, "--", safe_rel],
        timeout=timeout,
    )
    for line in tree_out.splitlines():
        match = _LS_TREE_LINE_RE.match(line.strip())
        if match and match.group(1) == "blob" and match.group(3) == safe_rel:
            return match.group(2)
    return ""


def read_git_blob_text(
    repo_dir: Path,
    rel_path: str,
    *,
    git_sha: str,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> str:
    """Read UTF-8 file content strictly from the requested commit tree."""
    safe_rel = validate_repo_rel_path(rel_path)
    resolved_sha = resolve_commit_sha(repo_dir, git_sha, timeout=timeout)
    return _run_git(repo_dir, ["show", f"{resolved_sha}:{safe_rel}"], timeout=timeout)


def _list_tree_blobs(
    repo_dir: Path,
    resolved_sha: str,
    rel_dir: str,
    *,
    timeout: float,
) -> dict[str, str]:
    """Enumerate tracked blob paths -> blob SHAs under rel_dir for resolved_sha."""
    tree_out = _run_git(
        repo_dir,
        ["ls-tree", "-r", resolved_sha, "--", rel_dir],
        timeout=timeout,
    )
    blobs: dict[str, str] = {}
    for line in tree_out.splitlines():
        match = _LS_TREE_LINE_RE.match(line.rstrip("\r\n"))
        if not match or match.group(1) != "blob":
            continue
        blob_sha, path = match.group(2), match.group(3).replace("\\", "/")
        if PurePosixPath(path).parent.as_posix() == rel_dir:
            blobs[path] = blob_sha
    return blobs


def read_git_evidence(
    repo_dir: Path,
    *,
    repository: str,
    git_sha: str,
    base_sha: str | None = None,
    pr_number: int | None = None,
    report_dir: str = "documentation/reports/final",
    pr_title: str = "",
    pr_body: str = "",
    max_files: int = 200,
    timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> EvidenceBundle:
    """Collect commit-pinned changed files, commits, and report blob SHAs from repo_dir."""
    resolved_sha = resolve_commit_sha(repo_dir, git_sha, timeout=timeout)

    resolved_base: str | None = None
    if base_sha and base_sha.strip() and not _ALL_ZERO_SHA_RE.match(base_sha.strip()):
        resolved_base = resolve_commit_sha(
            repo_dir,
            base_sha,
            allow_head=False,
            field_name="base_sha",
            timeout=timeout,
        )

    if resolved_base:
        diff_range = f"{resolved_base}..{resolved_sha}"
        changed_raw = _run_git(
            repo_dir,
            ["diff", "--name-only", diff_range],
            timeout=timeout,
        )
        commits_raw = _run_git(
            repo_dir,
            ["log", "-n", "20", "--pretty=format:%h %s", diff_range],
            timeout=timeout,
        )
    else:
        changed_raw = _run_git(
            repo_dir,
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", resolved_sha],
            timeout=timeout,
        )
        commits_raw = _run_git(
            repo_dir,
            ["log", "-n", "10", "--pretty=format:%h %s", resolved_sha],
            timeout=timeout,
        )

    changed_files = tuple(
        line.strip().replace("\\", "/")
        for line in changed_raw.splitlines()
        if line.strip()
    )[:max_files]

    commit_messages = tuple(
        scrub_secrets(line.strip()) for line in commits_raw.splitlines() if line.strip()
    )

    norm_report_dir = validate_repo_rel_path(report_dir)
    tree_blobs = _list_tree_blobs(repo_dir, resolved_sha, norm_report_dir, timeout=timeout)
    candidates = sorted(path for path in tree_blobs if path.endswith(".json"))
    blob_shas = {rel: tree_blobs[rel] for rel in candidates}

    return EvidenceBundle(
        repository=repository,
        git_sha=resolved_sha,
        base_sha=resolved_base,
        pr_number=pr_number,
        changed_files=changed_files,
        commit_messages=commit_messages,
        report_candidates=tuple(candidates),
        report_blob_shas=blob_shas,
        pr_title=scrub_secrets(pr_title),
        pr_body=scrub_secrets(pr_body),
    )

"""Illustrative GitHub-to-Drive synchronization pattern (Phase 6 reference only).

Under GITHUB_AUTHORITY, Google Drive is NOT the source of truth:
- DOCX updates are generated, validated, approved, and merged in GitHub first.
- Only merged GitHub DOCX commits are synchronized to Drive on the same file_id.
- Production drive_sync.py must add retries, pre-sync backup, SHA-256 verification,
  out-of-band Drive drift detection, and audit DB recording before use.
"""
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive"]


def drive_service(service_account_info: dict):
    creds = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def sync_merged_docx_to_drive(service, file_id: str, local_merged_docx_path: str):
    """Synchronizes an already-merged GitHub DOCX to the mapped Drive file ID."""
    media = MediaFileUpload(
        local_merged_docx_path,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        resumable=True,
    )
    return service.files().update(
        fileId=file_id,
        media_body=media,
        supportsAllDrives=True,
        fields="id,name,mimeType,modifiedTime,version,md5Checksum",
    ).execute()

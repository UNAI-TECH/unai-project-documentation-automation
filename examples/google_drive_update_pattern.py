"""Illustrative Drive API pattern. Add retries, backup and validation before production."""
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/drive"]

def drive_service(service_account_info: dict):
    creds = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def replace_existing_docx(service, file_id: str, local_docx_path: str):
    """Updates media on the same Drive file ID; does not create a new file."""
    media = MediaFileUpload(
        local_docx_path,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        resumable=True,
    )
    return service.files().update(
        fileId=file_id,
        media_body=media,
        supportsAllDrives=True,
        fields="id,name,mimeType,modifiedTime,version",
    ).execute()

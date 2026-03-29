"""
GDrive / Google Sheets tool wrappers.

Manages the single "Job Applications" GSheet file with 3 tabs:
  - Jobs          : assessed job postings
  - Emails Seen   : processed email log (deduplication)
  - Resume Versions: resume metadata + LLM summary

All writes use the Google Sheets API directly (google-api-python-client).
The GDrive MCP is used for file search and PDF download at runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import (
    GDRIVE_CREDENTIALS,
    GDRIVE_FOLDER,
    GSHEET_FILE_NAME,
    RESUME_FILENAME,
    SHEET_EMAILS,
    SHEET_JOBS,
    SHEET_RESUME,
)

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / ".gdrive_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ---------------------------------------------------------------------------
# Column definitions (1-indexed for Sheets API, 0-indexed for list ops)
# ---------------------------------------------------------------------------

JOBS_HEADERS = [
    "Job Role", "Company", "Job Description Summary", "Site", "URL",
    "Resume Strength", "Strength Explanation", "Pay", "Date Added", "Status",
]

EMAILS_HEADERS = [
    "Gmail Message ID", "Site", "Sender", "Subject", "Time Received",
    "Is AI/ML Alert", "Jobs Extracted", "Summary", "Processed At",
]

RESUME_HEADERS = [
    "Version", "Filename", "GDrive File ID", "File Size (bytes)",
    "Created At", "Modified At", "Detected At", "Short Summary",
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_credentials() -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GDRIVE_CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def _sheets_service():
    return build("sheets", "v4", credentials=_get_credentials())


def _drive_service():
    return build("drive", "v3", credentials=_get_credentials())


# ---------------------------------------------------------------------------
# GSheet bootstrap
# ---------------------------------------------------------------------------

def get_or_create_sheet() -> str:
    """
    Find the 'Job Applications' spreadsheet in the 'Job Application' GDrive folder.
    If it doesn't exist, create it with the 3 required tabs and header rows.
    Returns the spreadsheet ID.
    """
    drive = _drive_service()
    sheets = _sheets_service()

    # Find the parent folder
    folder_res = drive.files().list(
        q=(
            f"name='{GDRIVE_FOLDER}' "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        ),
        fields="files(id)",
    ).execute()
    folders = folder_res.get("files", [])
    if not folders:
        raise FileNotFoundError(f"GDrive folder '{GDRIVE_FOLDER}' not found.")
    folder_id = folders[0]["id"]

    # Check if spreadsheet already exists
    file_res = drive.files().list(
        q=(
            f"name='{GSHEET_FILE_NAME}' "
            f"and '{folder_id}' in parents "
            "and mimeType='application/vnd.google-apps.spreadsheet' "
            "and trashed=false"
        ),
        fields="files(id)",
    ).execute()
    files = file_res.get("files", [])

    if files:
        return files[0]["id"]

    # Create new spreadsheet
    spreadsheet = sheets.spreadsheets().create(body={
        "properties": {"title": GSHEET_FILE_NAME},
        "sheets": [
            {"properties": {"title": SHEET_JOBS}},
            {"properties": {"title": SHEET_EMAILS}},
            {"properties": {"title": SHEET_RESUME}},
        ],
    }).execute()
    spreadsheet_id = spreadsheet["spreadsheetId"]

    # Move to folder
    drive.files().update(
        fileId=spreadsheet_id,
        addParents=folder_id,
        removeParents="root",
        fields="id, parents",
    ).execute()

    # Write header rows
    _write_headers(sheets, spreadsheet_id)

    return spreadsheet_id


def _write_headers(sheets_svc, spreadsheet_id: str) -> None:
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": [
            {"range": f"{SHEET_JOBS}!A1", "values": [JOBS_HEADERS]},
            {"range": f"{SHEET_EMAILS}!A1", "values": [EMAILS_HEADERS]},
            {"range": f"{SHEET_RESUME}!A1", "values": [RESUME_HEADERS]},
        ]},
    ).execute()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_existing_job_urls(spreadsheet_id: str) -> set[str]:
    """Return all URLs already in the Jobs tab (column E)."""
    sheets = _sheets_service()
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_JOBS}!E2:E",
    ).execute()
    rows = res.get("values", [])
    return {row[0].strip() for row in rows if row}


def get_seen_email_ids(spreadsheet_id: str) -> set[str]:
    """Return all Gmail Message IDs already in the Emails Seen tab (column A)."""
    sheets = _sheets_service()
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_EMAILS}!A2:A",
    ).execute()
    rows = res.get("values", [])
    return {row[0].strip() for row in rows if row}


def get_last_resume_version(spreadsheet_id: str) -> dict | None:
    """Return the last row of the Resume Versions tab as a dict, or None."""
    sheets = _sheets_service()
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_RESUME}!A2:H",
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return None
    last = rows[-1]
    # Pad to header length in case trailing cells are empty
    last += [""] * (len(RESUME_HEADERS) - len(last))
    return dict(zip(RESUME_HEADERS, last))


# ---------------------------------------------------------------------------
# Append helpers
# ---------------------------------------------------------------------------

def _append_row(spreadsheet_id: str, sheet_name: str, row: list[Any]) -> None:
    sheets = _sheets_service()
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def append_job(spreadsheet_id: str, job: dict) -> None:
    """Append one row to the Jobs tab."""
    row = [
        job.get("title", ""),
        job.get("company", ""),
        job.get("description_summary", ""),
        job.get("site", ""),
        job.get("url", ""),
        job.get("resume_strength", ""),
        job.get("strength_explanation", ""),
        job.get("pay", ""),
        job.get("date_added", datetime.now().strftime("%Y-%m-%d")),
        "Active",
    ]
    _append_row(spreadsheet_id, SHEET_JOBS, row)


def append_email_seen(spreadsheet_id: str, record: dict) -> None:
    """Append one row to the Emails Seen tab."""
    row = [
        record.get("message_id", ""),
        record.get("site", ""),
        record.get("sender", ""),
        record.get("subject", ""),
        record.get("time_received", ""),
        record.get("is_ai_ml", ""),
        record.get("jobs_extracted", 0),
        record.get("summary", ""),
        datetime.now().isoformat(),
    ]
    _append_row(spreadsheet_id, SHEET_EMAILS, row)


def append_resume_version(spreadsheet_id: str, record: dict) -> None:
    """Append one row to the Resume Versions tab."""
    # Auto-increment version
    sheets = _sheets_service()
    res = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{SHEET_RESUME}!A2:A",
    ).execute()
    existing = res.get("values", [])
    version = len(existing) + 1

    row = [
        version,
        record.get("filename", RESUME_FILENAME),
        record.get("file_id", ""),
        record.get("file_size", ""),
        record.get("created_at", ""),
        record.get("modified_at", ""),
        record.get("detected_at", datetime.now().isoformat()),
        record.get("short_summary", ""),
    ]
    _append_row(spreadsheet_id, SHEET_RESUME, row)


# ---------------------------------------------------------------------------
# GDrive resume helpers
# ---------------------------------------------------------------------------

def find_resume_in_gdrive() -> dict | None:
    """
    Find RESUME_FILENAME in the GDRIVE_FOLDER.
    Returns a dict with id, name, size, createdTime, modifiedTime — or None.
    """
    drive = _drive_service()

    folder_res = drive.files().list(
        q=(
            f"name='{GDRIVE_FOLDER}' "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        ),
        fields="files(id)",
    ).execute()
    folders = folder_res.get("files", [])
    if not folders:
        return None
    folder_id = folders[0]["id"]

    file_res = drive.files().list(
        q=(
            f"name='{RESUME_FILENAME}' "
            f"and '{folder_id}' in parents "
            "and trashed=false"
        ),
        fields="files(id, name, size, createdTime, modifiedTime)",
    ).execute()
    files = file_res.get("files", [])
    return files[0] if files else None


def download_resume_pdf() -> bytes:
    """Download the resume PDF from GDrive and return its bytes."""
    import io
    from googleapiclient.http import MediaIoBaseDownload

    drive = _drive_service()
    meta = find_resume_in_gdrive()
    if not meta:
        raise FileNotFoundError(f"'{RESUME_FILENAME}' not found in GDrive '{GDRIVE_FOLDER}'")

    request = drive.files().get_media(fileId=meta["id"])
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()

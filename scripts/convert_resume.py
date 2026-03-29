"""
Phase 1 — One-time resume conversion script.

Finds the resume PDF in GDrive "Job Application" folder via the GDrive MCP,
downloads it, converts to Markdown, and writes resume.md to the project root.

Usage:
    python scripts/convert_resume.py

Requirements:
    - GDRIVE_CREDENTIALS must be set in .env
    - The resume PDF must exist in Google Drive under "Job Application/"
"""

import asyncio
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import GDRIVE_CREDENTIALS, GDRIVE_FOLDER

try:
    import pypdf
except ImportError:
    import PyPDF2 as pypdf  # fallback alias

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
OUTPUT_PATH = ROOT / "resume.md"


def _get_gdrive_service():
    """Authenticate and return a Google Drive API service object."""
    creds = None
    token_path = ROOT / ".gdrive_token.json"

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GDRIVE_CREDENTIALS, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def _find_resume_pdf(service) -> tuple[str, str]:
    """Search for a PDF file inside the 'Job Application' GDrive folder."""
    # Find the folder first
    folder_result = service.files().list(
        q=f"name='{GDRIVE_FOLDER}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)",
    ).execute()
    folders = folder_result.get("files", [])
    if not folders:
        raise FileNotFoundError(f"GDrive folder '{GDRIVE_FOLDER}' not found.")
    folder_id = folders[0]["id"]

    # Find the specific resume PDF by name
    pdf_result = service.files().list(
        q=(
            f"'{folder_id}' in parents "
            f"and name='RAMOS_Gabriel_C_Resume.pdf' "
            f"and mimeType='application/pdf' "
            f"and trashed=false"
        ),
        fields="files(id, name)",
        orderBy="modifiedTime desc",
    ).execute()
    pdfs = pdf_result.get("files", [])
    if not pdfs:
        raise FileNotFoundError(f"No PDF found in GDrive folder '{GDRIVE_FOLDER}'.")

    # Use the most recently modified PDF
    resume_file = pdfs[0]
    print(f"Found resume: {resume_file['name']} (id: {resume_file['id']})")
    return resume_file["id"], resume_file["name"]


def _download_pdf(service, file_id: str) -> bytes:
    """Download a file from GDrive by ID and return its bytes."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes and format as Markdown."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pages_text = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages_text.append(text.strip())

    raw = "\n\n".join(pages_text)

    # Normalise whitespace
    lines = [line.rstrip() for line in raw.splitlines()]
    # Collapse 3+ blank lines into 2
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return cleaned.strip()


def main() -> None:
    print("Authenticating with Google Drive...")
    service = _get_gdrive_service()

    print(f"Searching for resume PDF in '{GDRIVE_FOLDER}'...")
    file_id, file_name = _find_resume_pdf(service)

    print("Downloading PDF...")
    pdf_bytes = _download_pdf(service, file_id)

    print("Converting PDF to Markdown...")
    markdown = _pdf_bytes_to_markdown(pdf_bytes)

    OUTPUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"resume.md written to {OUTPUT_PATH} ({len(markdown)} chars)")
    print("\nDone! Review resume.md and commit it before proceeding to Phase 2.")


if __name__ == "__main__":
    main()

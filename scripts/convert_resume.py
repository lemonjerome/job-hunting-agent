"""
Phase 1 — Resume conversion + version tracking script.

Finds the resume PDF (set via RESUME_FILENAME env var) in GDrive "Job Hunting" folder,
converts it to text (for LLM use at runtime — never committed to repo),
and logs file metadata + a short LLM summary to the "Resume Versions" GSheet tab.

Run this whenever you update your resume:
    python scripts/convert_resume.py

Version tracking logic:
  - Compares the GDrive file's modifiedTime against the last logged version.
  - Only adds a new row if the file has been modified since the last check.
  - Uses auto-incrementing version numbers.

Note: resume.md is gitignored. The agent reads the resume from GDrive at runtime.
"""

import asyncio
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import RESUME_FILENAME, get_llm
from tools.sheets_tools import (
    append_resume_version,
    download_resume_pdf,
    find_resume_in_gdrive,
    get_last_resume_version,
    get_or_create_sheet,
)
from langchain_core.messages import HumanMessage

try:
    import pypdf
except ImportError:
    try:
        import PyPDF2 as pypdf
    except ImportError:
        pypdf = None


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract plain text from PDF bytes."""
    if pypdf is None:
        raise ImportError("Install pypdf: pip install pypdf2")

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text.strip())
    return "\n\n".join(pages)


async def _llm_summarise(text: str) -> str:
    """Ask the LLM for a 3-bullet summary of the resume."""
    llm = get_llm(temperature=0.0)
    prompt = (
        "In exactly 3 bullet points (use • as bullet), summarise this resume. "
        "Focus on: (1) years of experience and level, "
        "(2) top technical skills, (3) most recent role.\n\n"
        + text[:3000]
    )
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    return response.content.strip()


async def main() -> None:
    print("Connecting to Google services...")
    spreadsheet_id = get_or_create_sheet()

    print(f"Searching for '{RESUME_FILENAME}' in GDrive...")
    meta = find_resume_in_gdrive()
    if not meta:
        print(f"ERROR: '{RESUME_FILENAME}' not found in GDrive 'Job Hunting' folder.")
        sys.exit(1)

    print(f"Found: {meta['name']}  (modified: {meta['modifiedTime']})")

    # -- Check if this version is already logged --
    last = get_last_resume_version(spreadsheet_id)
    if last and last.get("Modified At") == meta["modifiedTime"]:
        print("Resume has not changed since last logged version. Nothing to do.")
        print(f"Last version: v{last['Version']} logged at {last['Detected At']}")
        return

    # -- Download and convert --
    print("Downloading PDF...")
    pdf_bytes = download_resume_pdf()

    print("Extracting text from PDF...")
    resume_text = _pdf_to_text(pdf_bytes)

    # -- Write resume.md locally (gitignored) for local dev convenience --
    resume_md_path = ROOT / "resume.md"
    resume_md_path.write_text(resume_text, encoding="utf-8")
    print(f"resume.md written locally ({len(resume_text)} chars) — gitignored, not committed.")

    # -- LLM summary --
    print("Generating resume summary via LLM...")
    summary = await _llm_summarise(resume_text)
    print(f"\nSummary:\n{summary}\n")

    # -- Log to Resume Versions sheet --
    print("Logging to 'Resume Versions' sheet...")
    append_resume_version(spreadsheet_id, {
        "filename":    meta["name"],
        "file_id":     meta["id"],
        "file_size":   meta.get("size", ""),
        "created_at":  meta.get("createdTime", ""),
        "modified_at": meta["modifiedTime"],
        "short_summary": summary,
    })

    print("Done. New resume version logged to GSheet.")


if __name__ == "__main__":
    asyncio.run(main())

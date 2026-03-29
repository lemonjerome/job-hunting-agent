"""
Job Hunting Agent — Entry point.

Usage:
    python main.py               # full graph run
    python main.py --smoke-test  # LLM connectivity check only
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# LLM smoke test (Phase 1 helper — kept for debugging)
# ---------------------------------------------------------------------------

async def smoke_test_llm() -> None:
    from config import get_llm
    from langchain_core.messages import HumanMessage

    print("=== LLM Smoke Test ===")
    llm = get_llm()

    resume_md = ROOT / "resume.md"
    if resume_md.exists():
        resume_text = resume_md.read_text(encoding="utf-8")[:2000]
        prompt = (
            "You are reviewing a resume. In 3 bullet points, summarise "
            "the candidate's top skills:\n\n" + resume_text
        )
    else:
        prompt = "Reply with exactly: 'Ollama Cloud connection successful.'"

    print(f"Sending prompt to Ollama Cloud ({llm.model}) ...")
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    print("\n--- LLM Response ---")
    print(response.content)
    print("--- End ---\n")


# ---------------------------------------------------------------------------
# Resume version check (runs before the graph on every invocation)
# ---------------------------------------------------------------------------

async def _check_resume_version() -> None:
    """
    Check if the resume PDF in GDrive has been updated since the last logged
    version. If so, download it, generate an LLM summary, and append a new row
    to the Resume Versions sheet tab.

    This replaces the need to manually run scripts/convert_resume.py.
    """
    import io

    try:
        import pypdf
    except ImportError:
        import PyPDF2 as pypdf  # type: ignore[no-redef]

    from langchain_core.messages import HumanMessage

    from config import get_llm
    from tools.sheets_tools import (
        append_resume_version,
        download_resume_pdf,
        find_resume_in_gdrive,
        get_last_resume_version,
        get_or_create_sheet,
    )

    print("[resume_check] Checking resume version...")
    spreadsheet_id = get_or_create_sheet()

    meta = find_resume_in_gdrive()
    if not meta:
        print("[resume_check] Resume PDF not found in GDrive — skipping.")
        return

    last = get_last_resume_version(spreadsheet_id)
    if last and last.get("Modified At") == meta["modifiedTime"]:
        print(f"[resume_check] Resume unchanged (v{last['Version']}) — skipping.")
        return

    print("[resume_check] New or updated resume detected — logging version...")
    pdf_bytes = download_resume_pdf()

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    resume_text = "\n".join(
        (page.extract_text() or "").strip() for page in reader.pages
    )

    llm = get_llm(temperature=0.0)
    summary_resp = await llm.ainvoke([HumanMessage(content=(
        "In exactly 3 bullet points (use • as bullet), summarise this resume. "
        "Focus on: (1) years of experience and level, "
        "(2) top technical skills, (3) most recent role.\n\n"
        + resume_text[:3000]
    ))])

    append_resume_version(spreadsheet_id, {
        "filename":      meta["name"],
        "file_id":       meta["id"],
        "file_size":     meta.get("size", ""),
        "created_at":    meta.get("createdTime", ""),
        "modified_at":   meta["modifiedTime"],
        "short_summary": summary_resp.content.strip(),
    })
    print("[resume_check] Resume version logged.")


# ---------------------------------------------------------------------------
# Full graph run
# ---------------------------------------------------------------------------

async def run_graph() -> None:
    from graph.graph import graph

    print("=" * 60)
    print("Job Hunting Agent — Starting")
    print("=" * 60)

    await _check_resume_version()

    # Initial state — all fields have sensible defaults
    initial_state = {
        "spreadsheet_id":     "",
        "job_urls_by_site":   {},
        "glassdoor_contexts": {},
        "raw_job_listings":   {},
        "assessed_jobs":      [],
        "new_jobs":           [],
        "notified":           False,
        "errors":             [],
    }

    result = await graph.ainvoke(initial_state)

    print("\n" + "=" * 60)
    print("Run complete.")
    print(f"  Assessed jobs : {len(result.get('assessed_jobs', []))}")
    print(f"  New in sheet  : {len(result.get('new_jobs', []))}")
    print(f"  Notified      : {result.get('notified', False)}")

    new_jobs = result.get("new_jobs", [])
    if new_jobs:
        print("\nNew jobs added this run:")
        for job in new_jobs:
            print(f"  [{job.resume_strength:8s}] {job.title} @ {job.company} ({job.site})")

    print("=" * 60)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    if "--smoke-test" in sys.argv:
        await smoke_test_llm()
    else:
        await run_graph()


if __name__ == "__main__":
    asyncio.run(main())

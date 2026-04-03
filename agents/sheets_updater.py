"""
Phase 5 — Sheets Updater Agent

LangGraph node: sheets_updater

Takes the list of assessed jobs from job_screener and writes new entries
to the 'Jobs' tab of the Google Sheet. Skips any job whose URL already
exists in the sheet (deduplication).

Input state:  assessed_jobs, spreadsheet_id
Output state: new_jobs (only the rows actually added this run)
"""

from __future__ import annotations

from graph.state import AssessedJob
from tools.sheets_tools import append_job, ensure_jobs_headers, get_existing_job_urls


async def sheets_updater_node(state: dict) -> dict:
    """
    LangGraph node.

    Appends newly assessed jobs to the Jobs sheet, skipping duplicates.
    Returns new_jobs — the subset of assessed_jobs actually written.
    """
    assessed: list[AssessedJob] = state.get("assessed_jobs", [])
    spreadsheet_id: str = state["spreadsheet_id"]

    if not assessed:
        print("[sheets_updater] No assessed jobs to write.")
        return {"new_jobs": []}

    # Ensure header row is current (adds Location column to existing sheets)
    ensure_jobs_headers(spreadsheet_id)

    # Fresh URL set — re-fetch at write time to catch any concurrent runs
    existing_urls = get_existing_job_urls(spreadsheet_id)

    new_jobs: list[AssessedJob] = []
    skipped = 0

    for job in assessed:
        if job.url in existing_urls:
            skipped += 1
            continue

        append_job(spreadsheet_id, {
            "title":                job.normalized_role or job.title,
            "company":              job.company,
            "description_summary":  job.description_summary,
            "site":                 job.site,
            "url":                  job.url,
            "resume_strength":      job.resume_strength,
            "strength_explanation": job.strength_explanation,
            "pay":                  job.normalized_pay or job.pay,
            "location":             job.normalized_location or job.location,
            "date_added":           job.date_added,
        })

        # Track for next iteration (avoid double-writing if same URL appears twice)
        existing_urls.add(job.url)
        new_jobs.append(job)

        print(
            f"[sheets_updater] Added {job.resume_strength:8s} | "
            f"{job.title!r} @ {job.company} ({job.site})"
        )

    print(
        f"[sheets_updater] Done — {len(new_jobs)} added, {skipped} skipped (already in sheet)."
    )
    return {"new_jobs": new_jobs}

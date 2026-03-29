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
# Full graph run
# ---------------------------------------------------------------------------

async def run_graph() -> None:
    from graph.graph import graph

    print("=" * 60)
    print("Job Hunting Agent — Starting")
    print("=" * 60)

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

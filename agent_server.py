"""
FastAPI server wrapping the LangGraph job-hunting agent.

Used in Cloud Run (event-driven) mode — receives a single email payload
from the Cloud Function trigger and runs the full agent pipeline.

Endpoints:
  POST /process        — process one job alert email
  GET  /health         — health check
  POST /renew-watch    — renew Gmail Watch API subscription (called by Cloud Scheduler)
  POST /batch          — manually trigger a full Gmail search run (same as main.py)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from graph.graph import graph
from graph.state import AgentState

app = FastAPI(title="Job Hunting Agent")


def _base_state() -> dict[str, Any]:
    return {
        "injected_emails": [],
        "spreadsheet_id": "",
        "job_urls_by_site": {},
        "email_contexts": {},
        "raw_job_listings": {},
        "assessed_jobs": [],
        "new_jobs": [],
        "notified": False,
        "errors": [],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/process")
async def process_email(request: Request):
    """
    Receive a single job alert email from the Cloud Function trigger and run
    the full agent pipeline (email screening → scraping → assessment → GSheet + notify).

    Expected JSON body:
      {
        "message_id": str,
        "site": str,          # "linkedin" | "jobstreet" | "glassdoor" | "indeed"
        "sender": str,
        "subject": str,
        "html_body": str,
        "time_received": str  # ISO timestamp (optional)
      }
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    required = {"message_id", "site", "sender", "subject", "html_body"}
    missing = required - set(payload.keys())
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    state = _base_state()
    state["injected_emails"] = [payload]

    try:
        result = await graph.ainvoke(state)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "new_jobs": 0, "notified": False},
        )

    new_jobs = result.get("new_jobs", [])
    return {
        "new_jobs": len(new_jobs),
        "notified": result.get("notified", False),
        "jobs": [
            {
                "role": j.normalized_role or j.title,
                "company": j.company,
                "strength": j.resume_strength,
                "site": j.site,
            }
            for j in new_jobs
        ],
    }


@app.post("/batch")
async def batch_run():
    """
    Trigger a full Gmail search run — same as running main.py directly.
    Useful for manual runs or fallback cron.
    """
    state = _base_state()

    try:
        result = await graph.ainvoke(state)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "new_jobs": 0, "notified": False},
        )

    new_jobs = result.get("new_jobs", [])
    return {
        "new_jobs": len(new_jobs),
        "notified": result.get("notified", False),
    }


@app.post("/renew-watch")
async def renew_watch():
    """
    Renew the Gmail Watch API subscription (expires every 7 days).
    Called daily by Cloud Scheduler.
    """
    try:
        from scripts.setup_gmail_watch import setup_watch
        expiration = setup_watch()
        return {"status": "renewed", "expiration": expiration}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

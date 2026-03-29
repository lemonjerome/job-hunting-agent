"""
Phase 3 — Site Scraper Agent

LangGraph node: scrape_site  (fanned-out per site via Send API)

Visits the specific job posting URLs extracted from emails.
Dispatches to the correct per-site strategy:

  LinkedIn   → Guest API (requests, no browser)
  Jobstreet  → GraphQL API (requests, no browser)
  Indeed     → Playwright + stealth (Cloudflare-aware)
  Glassdoor  → Playwright + stealth → email card fallback if blocked

Input  (via Send): {
    "site"               : str,
    "urls"               : [str, ...],
    "spreadsheet_id"     : str,
    "glassdoor_contexts" : { url: {title, company, ...} }   # Glassdoor only
}
Output (state patch): { "raw_job_listings": { site: [JobData, ...] } }
"""

from __future__ import annotations

import asyncio
import random

from tools.browser_tools import JobData, scrape_job
from tools.sheets_tools import get_existing_job_urls


async def scrape_site_node(state: dict) -> dict:
    """
    LangGraph node — one instance per site, fanned out via Send.
    """
    site: str = state["site"]
    urls: list[str] = state.get("urls", [])
    spreadsheet_id: str = state["spreadsheet_id"]
    glassdoor_contexts: dict = state.get("glassdoor_contexts", {})

    if not urls:
        return {"raw_job_listings": {site: []}}

    # Skip URLs already recorded in the Jobs sheet
    existing_urls = get_existing_job_urls(spreadsheet_id)
    new_urls = [u for u in urls if u not in existing_urls]

    if not new_urls:
        print(f"[scraper:{site}] All {len(urls)} URL(s) already in sheet — skipping.")
        return {"raw_job_listings": {site: []}}

    print(f"[scraper:{site}] Scraping {len(new_urls)} new URL(s).")
    listings: list[JobData] = []

    for url in new_urls:
        # Glassdoor: pass email card context for fallback
        email_ctx = glassdoor_contexts.get(url) if site == "glassdoor" else None

        try:
            job = await scrape_job(site, url, email_ctx)
            listings.append(job)

            if job.blocked and job.source != "email_fallback":
                print(f"[scraper:{site}] BLOCKED — stopping site agent.")
                break

            status = job.source.upper()
            print(
                f"[scraper:{site}] [{status}] "
                f"{job.title or '(no title)'} @ {job.company or '(no company)'}"
            )
        except Exception as e:
            print(f"[scraper:{site}] Error on {url}: {e}")
            listings.append(JobData(url=url, site=site, blocked=True))

        # Inter-request delay — vary by site risk level
        delay = {
            "linkedin":  random.uniform(1.0, 2.0),   # API — light delay
            "jobstreet": random.uniform(1.0, 2.0),   # API — light delay
            "indeed":    random.uniform(8.0, 14.0),  # Cloudflare — long delay
            "glassdoor": random.uniform(10.0, 18.0), # Heavy protection — longest delay
        }.get(site, random.uniform(3.0, 6.0))

        await asyncio.sleep(delay)

    return {"raw_job_listings": {site: listings}}

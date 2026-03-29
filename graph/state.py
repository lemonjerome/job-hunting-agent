"""
LangGraph AgentState definition.

Single shared state dict passed through every node in the graph.
Each node returns a PATCH (dict with only the keys it updates).
LangGraph merges patches via the reducers defined here.

State lifecycle:
  email_screener     → sets spreadsheet_id, job_urls_by_site, glassdoor_contexts
  scrape_site (×N)   → accumulates raw_job_listings per site (merge reducer)
  job_screener       → sets assessed_jobs
  sheets_updater     → sets new_jobs
  email_notifier     → sets notified (bool)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Annotated, Any

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from tools.browser_tools import JobData


# ---------------------------------------------------------------------------
# Assessed job (output of job_screener)
# ---------------------------------------------------------------------------

@dataclass
class AssessedJob:
    # From scraper
    site: str
    url: str
    title: str
    company: str
    location: str
    pay: str
    description: str          # raw text used for assessment
    scrape_source: str        # "api" | "scraped" | "email_fallback"

    # From job_screener
    is_ai_ml: bool = False
    description_summary: str = ""
    resume_strength: str = ""       # WEAK | MODERATE | STRONG
    strength_explanation: str = ""
    date_added: str = ""

    def to_sheet_row(self) -> list[Any]:
        """Return values in Jobs sheet column order."""
        from datetime import datetime
        return [
            self.title,
            self.company,
            self.description_summary,
            self.site,
            self.url,
            self.resume_strength,
            self.strength_explanation,
            self.pay,
            self.date_added or datetime.now().strftime("%Y-%m-%d"),
            "Active",
        ]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Reducer: merge raw_job_listings from parallel scraper nodes
# ---------------------------------------------------------------------------

def _merge_listings(
    existing: dict[str, list[JobData]],
    new: dict[str, list[JobData]],
) -> dict[str, list[JobData]]:
    """Merge dicts by site key — used as LangGraph reducer."""
    merged = dict(existing)
    for site, jobs in new.items():
        merged.setdefault(site, [])
        # Deduplicate by URL
        seen = {j.url for j in merged[site]}
        merged[site].extend(j for j in jobs if j.url not in seen)
    return merged


# ---------------------------------------------------------------------------
# AgentState
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    # Set by email_screener
    spreadsheet_id: str
    job_urls_by_site: dict[str, list[str]]          # site → [url, ...]
    glassdoor_contexts: dict[str, dict]              # url → email card data

    # Accumulated from parallel scrape_site nodes (merge reducer)
    raw_job_listings: Annotated[
        dict[str, list[JobData]],
        _merge_listings,
    ]

    # Set by job_screener
    assessed_jobs: list[AssessedJob]

    # Set by sheets_updater
    new_jobs: list[AssessedJob]                      # only newly added rows

    # Set by email_notifier
    notified: bool

    # Error log (any node can append)
    errors: list[str]

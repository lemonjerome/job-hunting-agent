"""
Phase 7 — Compiled LangGraph StateGraph

Full graph flow:

  START
    │
    ▼
  email_screener
    │
    ├─ (no AI/ML emails) ──────────────────────────► END
    │
    ▼
  scrape_site  ← fanned-out per triggered site via Send
  (linkedin / jobstreet / glassdoor / indeed — in parallel)
    │
    ▼ (fan-in: _merge_listings reducer accumulates raw_job_listings)
  job_screener
    │
    ├─ (no assessed jobs) ─────────────────────────► END
    │
    ▼
  sheets_updater
    │
    ├─ (no new strong jobs) ───────────────────────► END
    │
    ▼
  email_notifier
    │
    ▼
  END
"""

from __future__ import annotations

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send

from graph.nodes import (
    email_notifier_node,
    email_screener_node,
    job_screener_node,
    scrape_site_node,
    sheets_updater_node,
)
from graph.state import AgentState


# ---------------------------------------------------------------------------
# Conditional edge functions
# ---------------------------------------------------------------------------

def _route_after_email_screener(state: AgentState):
    """
    Fan-out to one scrape_site node per triggered site.
    If no sites triggered, go straight to END.
    """
    urls_by_site: dict = state.get("job_urls_by_site", {})
    # Only fan-out for sites that have URLs
    active_sites = {site: urls for site, urls in urls_by_site.items() if urls}

    if not active_sites:
        return END

    # LangGraph Send: each Send() creates a separate node invocation
    return [
        Send("scrape_site", {
            "site":               site,
            "urls":               urls,
            "spreadsheet_id":     state["spreadsheet_id"],
            "glassdoor_contexts": state.get("glassdoor_contexts", {}),
        })
        for site, urls in active_sites.items()
    ]


def _route_after_job_screener(state: AgentState) -> str:
    """Skip to END if no jobs passed the AI/ML + assessment filter."""
    if not state.get("assessed_jobs"):
        return END
    return "sheets_updater"


def _route_after_sheets_updater(state: AgentState) -> str:
    """Skip notification if no new STRONG jobs were added."""
    new_jobs = state.get("new_jobs", [])
    has_strong = any(j.resume_strength == "STRONG" for j in new_jobs)
    return "email_notifier" if has_strong else END


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    # --- Nodes ---
    builder.add_node("email_screener",  email_screener_node)
    builder.add_node("scrape_site",     scrape_site_node)
    builder.add_node("job_screener",    job_screener_node)
    builder.add_node("sheets_updater",  sheets_updater_node)
    builder.add_node("email_notifier",  email_notifier_node)

    # --- Edges ---
    builder.add_edge(START, "email_screener")

    # Fan-out to parallel scrapers (or END if nothing to scrape)
    builder.add_conditional_edges(
        "email_screener",
        _route_after_email_screener,
        # Tell LangGraph about all possible destinations
        ["scrape_site", END],
    )

    # Fan-in: all scrape_site instances converge here
    builder.add_edge("scrape_site", "job_screener")

    builder.add_conditional_edges(
        "job_screener",
        _route_after_job_screener,
        ["sheets_updater", END],
    )

    builder.add_conditional_edges(
        "sheets_updater",
        _route_after_sheets_updater,
        ["email_notifier", END],
    )

    builder.add_edge("email_notifier", END)

    return builder.compile()


# Compiled graph singleton — imported by main.py
graph = build_graph()

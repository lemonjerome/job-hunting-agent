"""
Node wrappers for the LangGraph StateGraph.

Each function here is a thin async wrapper that:
  1. Accepts the full AgentState
  2. Delegates to the corresponding agent module
  3. Returns only the state patch (dict)

The scrape_site node is special — it is invoked once per site via
LangGraph's Send API (fan-out), so it receives a sub-dict, not the full state.
"""

from __future__ import annotations

from agents.email_notifier import email_notifier_node
from agents.email_screener import email_screener_node
from agents.job_screener import job_screener_node
from agents.sheets_updater import sheets_updater_node
from agents.site_scraper import scrape_site_node


# Re-export so graph.py imports from one place
__all__ = [
    "email_screener_node",
    "scrape_site_node",
    "job_screener_node",
    "sheets_updater_node",
    "email_notifier_node",
]

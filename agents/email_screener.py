"""
Phase 2 — Email Screening Agent

LangGraph node: email_screener

Responsibilities:
  1. Query Gmail for job alert emails from the 4 monitored senders
     received in the past EMAIL_LOOKBACK_HOURS hours.
  2. Skip emails already logged in the 'Emails Seen' GSheet tab.
  3. Use the LLM to determine if each new email is an AI/ML job alert
     (not a general recommendation like Jobstreet's "LiNa" emails).
  4. For qualifying emails: extract direct job posting URLs from the HTML body.
  5. Log every processed email to 'Emails Seen' tab regardless of verdict.

Output state fields:
  - job_urls_by_site : dict[site, list[url]] — URLs to scrape
  - spreadsheet_id   : str — GSheet ID (passed downstream)

Stops early (returns empty job_urls_by_site) if no new relevant emails found.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from langchain_core.messages import HumanMessage

from config import get_llm
from tools.gmail_tools import extract_job_urls, search_job_alert_emails
from tools.sheets_tools import (
    append_email_seen,
    get_or_create_sheet,
    get_seen_email_ids,
)


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_IS_AI_ML_PROMPT = """\
You are reviewing a job alert email. Determine if this email is specifically
notifying about NEW AI or Machine Learning job openings.

Answer YES only if the email:
- Is a job alert / job posting notification (not a general profile tip or premium upsell)
- Mentions roles related to AI, ML, Machine Learning, Data Science, NLP, Computer Vision,
  or similar technical AI/ML fields

Answer NO if:
- The email is about non-AI/ML roles (e.g., web developer, front-end, finance, sales)
- The email is a general recommendation, premium upsell, or profile suggestion
- No specific job titles are mentioned

Email subject: {subject}

Email body excerpt (first 1500 chars):
{body_excerpt}

Reply with exactly one word: YES or NO
"""

_SUMMARY_PROMPT = """\
Summarise this job alert email in one sentence.
List the job roles and companies mentioned if possible.
Keep it under 60 words.

Subject: {subject}
Body excerpt: {body_excerpt}

Summary:"""


# ---------------------------------------------------------------------------
# Node function
# ---------------------------------------------------------------------------

async def email_screener_node(state: dict) -> dict:
    """
    LangGraph node. Returns a state patch:
      {
        "spreadsheet_id": str,
        "job_urls_by_site": { site: [url, ...], ... },
      }
    """
    llm = get_llm(temperature=0.0)

    # -- Step 1: bootstrap GSheet --
    spreadsheet_id = get_or_create_sheet()

    # -- Step 2: fetch already-seen email IDs --
    seen_ids = get_seen_email_ids(spreadsheet_id)

    # -- Step 3: search Gmail for new job alert emails --
    emails = search_job_alert_emails(seen_ids)
    if not emails:
        print("[email_screener] No new job alert emails found.")
        return {
            "spreadsheet_id": spreadsheet_id,
            "job_urls_by_site": {},
        }

    print(f"[email_screener] Found {len(emails)} new email(s) to screen.")

    job_urls_by_site: dict[str, list[str]] = {}

    # -- Step 4: screen each email --
    for email in emails:
        site = email["site"]
        subject = email["subject"]
        body_excerpt = email["html_body"][:1500]

        # --- Is it an AI/ML alert? ---
        is_ai_ml_response = await llm.ainvoke([
            HumanMessage(content=_IS_AI_ML_PROMPT.format(
                subject=subject,
                body_excerpt=body_excerpt,
            ))
        ])
        verdict = is_ai_ml_response.content.strip().upper()
        is_ai_ml = verdict.startswith("YES")

        # --- Summary (always generated for logging) ---
        summary_response = await llm.ainvoke([
            HumanMessage(content=_SUMMARY_PROMPT.format(
                subject=subject,
                body_excerpt=body_excerpt,
            ))
        ])
        summary = summary_response.content.strip()

        # --- Extract job URLs if qualifying ---
        extracted_urls: list[str] = []
        if is_ai_ml:
            extracted_urls = extract_job_urls(email)
            if extracted_urls:
                existing = job_urls_by_site.setdefault(site, [])
                # Avoid duplicates across emails from the same site
                for url in extracted_urls:
                    if url not in existing:
                        existing.append(url)
            print(
                f"[email_screener] {site} | AI/ML=YES | "
                f"{len(extracted_urls)} URL(s) extracted | '{subject}'"
            )
        else:
            print(
                f"[email_screener] {site} | AI/ML=NO  | skipped | '{subject}'"
            )

        # --- Log to Emails Seen sheet ---
        append_email_seen(spreadsheet_id, {
            "message_id":    email["message_id"],
            "site":          site,
            "sender":        email["sender"],
            "subject":       subject,
            "time_received": email["time_received"],
            "is_ai_ml":      "YES" if is_ai_ml else "NO",
            "jobs_extracted": len(extracted_urls),
            "summary":       summary,
        })

    if not job_urls_by_site:
        print("[email_screener] No AI/ML job alerts found in new emails.")

    return {
        "spreadsheet_id": spreadsheet_id,
        "job_urls_by_site": job_urls_by_site,
    }

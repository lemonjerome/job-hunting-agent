"""
Phase 2 — Email Screening Agent

LangGraph node: email_screener

Responsibilities:
  1. Query Gmail for job alert emails from the 4 monitored senders
     received in the past EMAIL_LOOKBACK_HOURS hours.
  2. Skip emails already logged in the 'Emails Seen' GSheet tab.
  3. Use the LLM to determine if each email is an AI/ML job alert
     (catches false positives like Jobstreet's "LiNa" recommendations).
  4. For qualifying emails: extract direct job posting URLs from the HTML body.
  5. For Glassdoor emails: also parse email card context (title, company,
     location, salary) to be used as scraper fallback if page is blocked.
  6. Log every processed email to 'Emails Seen' tab regardless of verdict.

Output state fields:
  spreadsheet_id      : str
  job_urls_by_site    : dict[site, list[url]]
  glassdoor_contexts  : dict[url, {title, company, location, pay, rating}]
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from config import get_llm
from tools.browser_tools import parse_glassdoor_email_cards
from tools.gmail_tools import extract_job_urls, search_job_alert_emails
from tools.sheets_tools import (
    append_email_seen,
    get_or_create_sheet,
    get_seen_email_ids,
)


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_IS_AI_ML_PROMPT = """\
You are reviewing a job alert email. Determine if this email is specifically
notifying about NEW AI or Machine Learning job openings.

Answer YES only if the email:
- Is a job alert or job posting notification (not a profile tip or premium upsell)
- Mentions roles related to AI, ML, Machine Learning, Data Science, NLP,
  Computer Vision, LLM, Deep Learning, or similar AI/ML fields

Answer NO if:
- The roles are non-AI/ML (e.g. web developer, front-end, finance, sales)
- The email is a general recommendation, premium upsell, or profile suggestion
- No specific job titles are mentioned

Email subject: {subject}

Email body excerpt (first 1500 chars):
{body_excerpt}

Reply with exactly one word: YES or NO"""

_SUMMARY_PROMPT = """\
Summarise this job alert email in one sentence (max 60 words).
List the job roles and companies mentioned if visible.

Subject: {subject}
Body: {body_excerpt}

Summary:"""


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def email_screener_node(state: dict) -> dict:
    """
    LangGraph node. Returns a state patch:
      {
        "spreadsheet_id"     : str,
        "job_urls_by_site"   : { site: [url, ...] },
        "glassdoor_contexts" : { url: {title, company, location, pay, rating} },
      }
    """
    llm = get_llm(temperature=0.0)

    # -- Bootstrap GSheet --
    spreadsheet_id = get_or_create_sheet()

    # -- Load already-seen email IDs --
    seen_ids = get_seen_email_ids(spreadsheet_id)

    # -- Search Gmail --
    emails = search_job_alert_emails(seen_ids)
    if not emails:
        print("[email_screener] No new job alert emails found.")
        return {
            "spreadsheet_id": spreadsheet_id,
            "job_urls_by_site": {},
            "glassdoor_contexts": {},
        }

    print(f"[email_screener] {len(emails)} new email(s) to screen.")

    job_urls_by_site: dict[str, list[str]] = {}
    glassdoor_contexts: dict[str, dict] = {}

    for email in emails:
        site = email["site"]
        subject = email["subject"]
        body_excerpt = email["html_body"][:1500]

        # --- AI/ML verdict ---
        verdict_resp = await llm.ainvoke([HumanMessage(content=_IS_AI_ML_PROMPT.format(
            subject=subject, body_excerpt=body_excerpt,
        ))])
        is_ai_ml = verdict_resp.content.strip().upper().startswith("YES")

        # --- Summary (always, for logging) ---
        summary_resp = await llm.ainvoke([HumanMessage(content=_SUMMARY_PROMPT.format(
            subject=subject, body_excerpt=body_excerpt,
        ))])
        summary = summary_resp.content.strip()

        extracted_urls: list[str] = []

        if is_ai_ml:
            urls = extract_job_urls(email)

            # Glassdoor: also parse email card context for scraper fallback
            if site == "glassdoor" and urls:
                card_contexts = parse_glassdoor_email_cards(email["html_body"])
                glassdoor_contexts.update(card_contexts)

            # Deduplicate across emails from the same site
            existing = job_urls_by_site.setdefault(site, [])
            for url in urls:
                if url not in existing:
                    existing.append(url)
            extracted_urls = urls

            print(f"[email_screener] {site} | YES | {len(urls)} URL(s) | '{subject}'")
        else:
            print(f"[email_screener] {site} | NO  | skipped | '{subject}'")

        # --- Log to Emails Seen sheet ---
        append_email_seen(spreadsheet_id, {
            "message_id":     email["message_id"],
            "site":           site,
            "sender":         email["sender"],
            "subject":        subject,
            "time_received":  email["time_received"],
            "is_ai_ml":       "YES" if is_ai_ml else "NO",
            "jobs_extracted": len(extracted_urls),
            "summary":        summary,
        })

    if not job_urls_by_site:
        print("[email_screener] No AI/ML alerts found — nothing to scrape.")

    return {
        "spreadsheet_id":    spreadsheet_id,
        "job_urls_by_site":  job_urls_by_site,
        "glassdoor_contexts": glassdoor_contexts,
    }

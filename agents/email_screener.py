"""
Phase 2 — Email Screening Agent

LangGraph node: email_screener

Responsibilities:
  1. Query Gmail for job alert emails from the 4 monitored senders
     received in the past EMAIL_LOOKBACK_HOURS hours.
     — OR — use pre-loaded emails injected by the Cloud Function trigger
     (state["injected_emails"]) to skip the Gmail search entirely.
  2. Skip emails already logged in the 'Emails Seen' GSheet tab.
  3. Use the LLM to determine if each email is an AI/ML job alert
     (catches false positives like Jobstreet's "LiNa" recommendations).
  4. For qualifying emails: parse job card HTML to extract job URLs AND
     email card context (title, company, location, pay) for ALL 4 sites.
     This makes every site scraper resilient with email-data fallback.
  5. Log every processed email to 'Emails Seen' tab regardless of verdict.

Output state fields:
  spreadsheet_id   : str
  job_urls_by_site : dict[site, list[url]]
  email_contexts   : dict[url, {title, company, location, pay, rating}]
"""

from __future__ import annotations

import asyncio

import aiohttp
from langchain_core.messages import HumanMessage

from config import get_llm
from tools.browser_tools import _EMAIL_JOB_LINK_PATTERNS as _JOB_PATTERNS, _NEEDS_REDIRECT, parse_email_job_cards
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

Email body excerpt (first 8000 chars):
{body_excerpt}

Reply with exactly one word: YES or NO"""

_CARD_AI_ML_PROMPT = """\
You are reviewing a job alert email. Determine if ANY of the listed jobs
are related to AI or Machine Learning.

Answer YES if at least one job title involves: AI, ML, Machine Learning,
Data Science, NLP, Computer Vision, LLM, Deep Learning, AI Trainer,
or similar AI/ML fields.

Answer NO only if ALL jobs are clearly unrelated to AI/ML.

Email subject: {subject}

Job titles found in this email:
{titles}

Reply with exactly one word: YES or NO"""

_SUMMARY_PROMPT = """\
Summarise this job alert email in one sentence (max 60 words).
List the job roles and companies mentioned if visible.

Subject: {subject}
Body: {body_excerpt}

Summary:"""


# ---------------------------------------------------------------------------
# Tracking URL resolver
# ---------------------------------------------------------------------------

async def _resolve_redirect(url: str) -> str:
    """
    Follow HTTP redirects and return the final URL.
    Used to resolve Jobstreet (url.jobstreet.com) and Indeed (pagead/clk/dl)
    tracking links to their canonical job page URLs.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return str(resp.url)
    except Exception:
        return url  # return original if resolution fails


async def _resolve_card_urls(cards: dict[str, dict], site: str) -> dict[str, dict]:
    """
    For each tracking URL in cards, follow the redirect and re-key the dict
    with the resolved canonical URL. Card context is preserved.
    Non-job URLs (images, homepages, unsubscribe links) are discarded after
    resolution by checking the final URL against the canonical job URL pattern.
    Runs all resolutions concurrently.
    """
    if not cards:
        return cards

    canonical_pattern = _JOB_PATTERNS.get(site)
    tracking_urls = list(cards.keys())
    resolved_urls = await asyncio.gather(*[_resolve_redirect(u) for u in tracking_urls])

    resolved: dict[str, dict] = {}
    for original, final in zip(tracking_urls, resolved_urls):
        # Use the same pattern that identified these as job links in the email HTML.
        # This handles all URL variants: ph.jobstreet.com, jobstreet.com.ph,
        # cts.indeed.com/v3/, pagead/clk, etc. — without hardcoded string checks.
        if canonical_pattern and not canonical_pattern.search(final):
            if final == original:
                # Resolution FAILED (timeout / geo-block from Cloud Run).
                # Keep the original tracking URL so the scraper can fall back to
                # email card data rather than silently dropping this job.
                resolved[original] = cards[original]
                print(f"[email_screener] redirect failed — keeping tracking URL for fallback: ...{original[-60:]}")
            # else: redirect succeeded but landed on a non-job page
            # (homepage, unsubscribe, logo CDN) — discard.
            continue

        ctx = cards[original]
        resolved[final] = ctx
        if final != original:
            print(f"[email_screener] resolved: ...{original[-40:]} → {final[:80]}")

    return resolved


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def email_screener_node(state: dict) -> dict:
    """
    LangGraph node. Returns a state patch:
      {
        "spreadsheet_id"   : str,
        "job_urls_by_site" : { site: [url, ...] },
        "email_contexts"   : { url: {title, company, location, pay, rating} },
      }
    """
    llm = get_llm(temperature=0.0)

    # -- Bootstrap GSheet --
    spreadsheet_id = get_or_create_sheet()

    # -- Load already-seen email IDs --
    seen_ids = get_seen_email_ids(spreadsheet_id)

    # -- Get emails: injected (Cloud Function) or search Gmail (batch mode) --
    injected: list[dict] = state.get("injected_emails") or []
    if injected:
        # Event-driven mode: email already fetched by Cloud Function
        emails = [e for e in injected if e.get("message_id") not in seen_ids]
        print(f"[email_screener] Event-driven mode — {len(emails)} injected email(s).")
    else:
        emails = search_job_alert_emails(seen_ids)
        if not emails:
            print("[email_screener] No new job alert emails found.")
            return {
                "spreadsheet_id": spreadsheet_id,
                "job_urls_by_site": {},
                "email_contexts": {},
            }
        print(f"[email_screener] {len(emails)} new email(s) to screen.")

    job_urls_by_site: dict[str, list[str]] = {}
    email_contexts: dict[str, dict] = {}

    for email in emails:
        site = email["site"]
        subject = email["subject"]
        body_excerpt = email["html_body"][:8000]

        # --- Parse job cards for ALL sites (unified parser) ---
        # This gives us job URLs AND email card context (title, company, location, pay)
        # for every site, not just Glassdoor. Used as scraper fallback data.
        card_contexts = parse_email_job_cards(email["html_body"], site)

        # For sites that use email click-tracking redirects (Jobstreet, Indeed),
        # resolve tracking URLs to canonical job page URLs before using them.
        if site in _NEEDS_REDIRECT and card_contexts:
            card_contexts = await _resolve_card_urls(card_contexts, site)

        email_contexts.update(card_contexts)

        # --- AI/ML verdict ---
        titles = "\n".join(
            f"- {ctx['title']}" for ctx in card_contexts.values() if ctx.get("title")
        )
        if titles:
            # Card titles available — more reliable signal than raw HTML
            verdict_resp = await llm.ainvoke([HumanMessage(content=_CARD_AI_ML_PROMPT.format(
                subject=subject, titles=titles,
            ))])
        else:
            # No card titles extracted (parser found URLs but no text, or no cards at all)
            # Fall back to full email body for the verdict
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
            if card_contexts:
                # Prefer card-parsed URLs (more reliable than regex extraction)
                urls = list(card_contexts.keys())
            else:
                # Fallback: regex-based extraction for sites where card parsing found nothing
                urls = extract_job_urls(email)

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
        "email_contexts":    email_contexts,
    }

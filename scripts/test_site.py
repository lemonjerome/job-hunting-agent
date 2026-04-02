"""
Per-site end-to-end dry-run test harness.

Finds the most recent job alert email from a given site, runs the full
extraction → scraping → assessment pipeline, and prints results.

Does NOT write to Google Sheets or send notification emails.

Usage:
  python scripts/test_site.py --site linkedin
  python scripts/test_site.py --site jobstreet
  python scripts/test_site.py --site indeed
  python scripts/test_site.py --site glassdoor
  python scripts/test_site.py --site all

Optional flags:
  --max-urls N     Only scrape the first N URLs (default: 3)
  --lookback-days N Search emails from the last N days (default: 7)
  --skip-scrape    Only test email parsing, skip Playwright/API scraping
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.gmail_tools import _get_gmail_service, _identify_site, _extract_html_body, _parse_email_address, _parse_date
from tools.browser_tools import _NEEDS_REDIRECT, parse_email_job_cards, scrape_job
from agents.email_screener import _resolve_card_urls
from agents.job_screener import _normalize_fields, _is_ai_ml, _load_resume
from config import get_llm, EMAIL_SENDERS


SITES = list(EMAIL_SENDERS.keys())


def _search_recent_email(service, site: str, lookback_days: int) -> dict | None:
    """Find the most recent email from a given job site sender."""
    sender_addr = EMAIL_SENDERS.get(site)
    if not sender_addr:
        return None

    query = f"from:{sender_addr} newer_than:{lookback_days}d"
    resp = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
    messages = resp.get("messages", [])
    if not messages:
        return None

    # Take the most recent one
    msg_id = messages[0]["id"]
    full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}

    return {
        "message_id":    msg_id,
        "site":          site,
        "sender":        _parse_email_address(headers.get("From", "")),
        "subject":       headers.get("Subject", ""),
        "time_received": _parse_date(headers.get("Date", "")),
        "html_body":     _extract_html_body(full["payload"]),
    }


async def test_site(site: str, max_urls: int, lookback_days: int, skip_scrape: bool) -> None:
    print(f"\n{'='*60}")
    print(f"  Testing site: {site.upper()}")
    print(f"{'='*60}")

    # -- Find email --
    print(f"\n[1] Searching Gmail for {site} email (last {lookback_days} days)...")
    service = _get_gmail_service()
    email = _search_recent_email(service, site, lookback_days)

    if not email:
        print(f"  ✗ No email found from {EMAIL_SENDERS[site]} in the last {lookback_days} days.")
        return

    print(f"  Subject : {email['subject']}")
    print(f"  Received: {email['time_received']}")
    print(f"  Body len: {len(email['html_body'])} chars")

    # -- Card parsing --
    print(f"\n[2] Parsing email job cards...")
    cards = parse_email_job_cards(email["html_body"], site)

    # Resolve tracking URLs for Jobstreet / Indeed
    if site in _NEEDS_REDIRECT and cards:
        print(f"  Resolving {len(cards)} tracking URL(s)...")
        cards = await _resolve_card_urls(cards, site)

    print(f"  Found {len(cards)} job card(s):")
    for url, ctx in list(cards.items())[:10]:
        print(f"  • [{ctx.get('title','—')}] @ {ctx.get('company','—')} | {ctx.get('location','—')} | {ctx.get('pay','no pay')}")
        print(f"    {url[:90]}")

    if not cards:
        print("  ✗ No job URLs found. Check the email HTML or URL patterns.")
        print("\n  HTML snippet (first 2000 chars):")
        print(email["html_body"][:2000])
        return

    # -- AI/ML verdict --
    print(f"\n[3] AI/ML verdict (LLM)...")
    llm = get_llm(temperature=0.0)
    if cards:
        from langchain_core.messages import HumanMessage
        from agents.email_screener import _CARD_AI_ML_PROMPT
        titles = "\n".join(f"- {ctx['title']}" for ctx in list(cards.values())[:8] if ctx.get("title"))
        resp = await llm.ainvoke([HumanMessage(content=_CARD_AI_ML_PROMPT.format(
            subject=email["subject"], titles=titles,
        ))])
        verdict = resp.content.strip()
        print(f"  AI/ML verdict: {verdict}")

    if skip_scrape:
        print("\n  --skip-scrape set. Skipping scraping and assessment.")
        return

    # -- Scraping --
    urls_to_scrape = list(cards.keys())[:max_urls]
    print(f"\n[4] Scraping {len(urls_to_scrape)} URL(s) (max {max_urls})...")
    resume_text = _load_resume()

    for i, url in enumerate(urls_to_scrape, 1):
        email_ctx = cards.get(url)
        print(f"\n  [{i}/{len(urls_to_scrape)}] Scraping: {url[:80]}")
        try:
            job = await scrape_job(site, url, email_ctx)
        except Exception as e:
            print(f"  ✗ Scraping error: {e}")
            continue

        print(f"  Source  : {job.source}")
        print(f"  Blocked : {job.blocked}")
        print(f"  Title   : {job.title or '(empty)'}")
        print(f"  Company : {job.company or '(empty)'}")
        print(f"  Location: {job.location or '(empty)'}")
        print(f"  Pay     : {job.pay or '(none)'}")
        print(f"  Desc len: {len(job.description)} chars")

        if job.title or job.company:
            # AI normalization
            print(f"\n  [5] AI normalization...")
            norm_role, norm_pay = await _normalize_fields(llm, job)
            print(f"  Normalized role: {norm_role or '(empty)'}")
            print(f"  Normalized pay : {norm_pay or '(empty)'}")

            # AI/ML check
            ai_ml = await _is_ai_ml(llm, job)
            print(f"  Is AI/ML       : {ai_ml}")


def main():
    parser = argparse.ArgumentParser(description="Per-site job agent test harness")
    parser.add_argument("--site", choices=SITES + ["all"], required=True, help="Site to test")
    parser.add_argument("--max-urls", type=int, default=3, help="Max URLs to scrape per site")
    parser.add_argument("--lookback-days", type=int, default=7, help="Search emails from last N days")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip scraping, only test email parsing")
    args = parser.parse_args()

    sites_to_test = SITES if args.site == "all" else [args.site]

    async def run():
        for site in sites_to_test:
            await test_site(site, args.max_urls, args.lookback_days, args.skip_scrape)

    asyncio.run(run())
    print("\nDone. No data was written to Sheets or email sent (dry run).")


if __name__ == "__main__":
    main()

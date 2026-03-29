"""
Phase 3 — Site Scraper Agent

LangGraph node: scrape_site  (fanned-out per site via Send API)

Visits the specific job posting URLs extracted from emails (NOT search pages).
Uses Playwright with playwright-stealth to appear as a real macOS Chrome browser.

Supported sites: linkedin, jobstreet, glassdoor, indeed

Kill switch: on 403 / login-wall redirect → marks site as BLOCKED and stops.

Input  (via Send): { "site": str, "urls": [str, ...], "spreadsheet_id": str }
Output (state patch): { "raw_job_listings": { site: [JobPosting, ...] } }
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from playwright.async_api import async_playwright, Page, Response

from config import STEALTH_USER_AGENT, STEALTH_VIEWPORT
from tools.sheets_tools import get_existing_job_urls

try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None  # graceful no-op if not installed


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JobPosting:
    site: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    pay: str = ""
    description: str = ""        # full raw text for LLM
    blocked: bool = False        # True if login wall hit


# ---------------------------------------------------------------------------
# Stealth browser context factory
# ---------------------------------------------------------------------------

async def _new_stealth_context(playwright):
    """Launch a Chromium browser with stealth settings applied."""
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=STEALTH_USER_AGENT,
        viewport=STEALTH_VIEWPORT,
        locale="en-US",
        timezone_id="Asia/Manila",
        # Mask WebGL vendor/renderer as Apple GPU
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    return browser, context


async def _apply_stealth(page: Page) -> None:
    """Inject JS patches to hide headless indicators."""
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    """)
    if stealth_async:
        await stealth_async(page)


def _is_login_wall(page_url: str, content: str) -> bool:
    """Detect if the browser was redirected to a login page."""
    login_signals = [
        "/login", "/signin", "/sign-in", "/auth",
        "authwall", "checkpoint", "join?",
    ]
    url_lower = page_url.lower()
    return any(sig in url_lower for sig in login_signals)


async def _human_scroll(page: Page) -> None:
    """Simulate slow human-like scrolling."""
    for _ in range(3):
        await page.mouse.wheel(0, random.randint(300, 600))
        await asyncio.sleep(random.uniform(0.8, 1.5))


# ---------------------------------------------------------------------------
# Per-site extractors
# ---------------------------------------------------------------------------

async def _scrape_linkedin(page: Page) -> dict:
    """Extract job details from a LinkedIn job detail page."""
    data: dict = {}
    try:
        # Title
        title_el = await page.query_selector("h1.top-card-layout__title, h1.jobs-unified-top-card__job-title")
        if title_el:
            data["title"] = (await title_el.inner_text()).strip()

        # Company
        company_el = await page.query_selector(
            "a.topcard__org-name-link, "
            "span.jobs-unified-top-card__company-name"
        )
        if company_el:
            data["company"] = (await company_el.inner_text()).strip()

        # Location
        loc_el = await page.query_selector(
            "span.topcard__flavor--bullet, "
            "span.jobs-unified-top-card__bullet"
        )
        if loc_el:
            data["location"] = (await loc_el.inner_text()).strip()

        # Salary (not always present)
        pay_el = await page.query_selector(
            "span.compensation__salary, "
            "div.jobs-unified-top-card__job-insight span"
        )
        if pay_el:
            data["pay"] = (await pay_el.inner_text()).strip()

        # Description
        desc_el = await page.query_selector(
            "div.show-more-less-html__markup, "
            "div.jobs-description__content"
        )
        if desc_el:
            data["description"] = (await desc_el.inner_text()).strip()[:5000]
    except Exception as e:
        data["_error"] = str(e)
    return data


async def _scrape_jobstreet(page: Page) -> dict:
    """Extract job details from a Jobstreet job detail page."""
    data: dict = {}
    try:
        title_el = await page.query_selector("h1[data-automation='job-detail-title'], h1.job-title")
        if title_el:
            data["title"] = (await title_el.inner_text()).strip()

        company_el = await page.query_selector(
            "span[data-automation='job-detail-company'], "
            "a[data-automation='job-detail-company']"
        )
        if company_el:
            data["company"] = (await company_el.inner_text()).strip()

        loc_el = await page.query_selector("span[data-automation='job-detail-location']")
        if loc_el:
            data["location"] = (await loc_el.inner_text()).strip()

        pay_el = await page.query_selector("span[data-automation='job-detail-salary']")
        if pay_el:
            data["pay"] = (await pay_el.inner_text()).strip()

        desc_el = await page.query_selector("div[data-automation='jobAdDetails']")
        if desc_el:
            data["description"] = (await desc_el.inner_text()).strip()[:5000]
    except Exception as e:
        data["_error"] = str(e)
    return data


async def _scrape_glassdoor(page: Page) -> dict:
    """Extract job details from a Glassdoor job detail page."""
    data: dict = {}
    try:
        title_el = await page.query_selector(
            "h1.job-title, "
            "div.JobDetails_jobTitle__Rw_gn, "
            "h1[data-test='jobTitle']"
        )
        if title_el:
            data["title"] = (await title_el.inner_text()).strip()

        company_el = await page.query_selector(
            "div.EmployerProfile_employerName__Fs9CR, "
            "span.EmployerProfile_compactEmployerName__9MGcV, "
            "div[data-test='employerName']"
        )
        if company_el:
            data["company"] = (await company_el.inner_text()).strip()

        loc_el = await page.query_selector(
            "div[data-test='location'], "
            "div.JobDetails_jobDetailsHeader__sLkQX span"
        )
        if loc_el:
            data["location"] = (await loc_el.inner_text()).strip()

        pay_el = await page.query_selector(
            "div[data-test='detailSalary'], "
            "span.JobDetails_salaryEstimate__arV5J"
        )
        if pay_el:
            data["pay"] = (await pay_el.inner_text()).strip()

        desc_el = await page.query_selector(
            "div.JobDetails_jobDescription__uW_fK, "
            "div[data-test='jobDescriptionContent']"
        )
        if desc_el:
            data["description"] = (await desc_el.inner_text()).strip()[:5000]
    except Exception as e:
        data["_error"] = str(e)
    return data


async def _scrape_indeed(page: Page) -> dict:
    """Extract job details from an Indeed job detail page."""
    data: dict = {}
    try:
        title_el = await page.query_selector(
            "h1.jobsearch-JobInfoHeader-title, "
            "h1[data-testid='jobsearch-JobInfoHeader-title']"
        )
        if title_el:
            data["title"] = (await title_el.inner_text()).strip()

        company_el = await page.query_selector(
            "div[data-testid='inlineHeader-companyName'] a, "
            "span[data-testid='inlineHeader-companyName']"
        )
        if company_el:
            data["company"] = (await company_el.inner_text()).strip()

        loc_el = await page.query_selector(
            "div[data-testid='jobsearch-JobInfoHeader-companyLocation'], "
            "div[data-testid='inlineHeader-companyLocation']"
        )
        if loc_el:
            data["location"] = (await loc_el.inner_text()).strip()

        pay_el = await page.query_selector(
            "span[data-testid='attribute_snippet_testid'], "
            "#salaryInfoAndJobType span.attribute_snippet"
        )
        if pay_el:
            data["pay"] = (await pay_el.inner_text()).strip()

        desc_el = await page.query_selector(
            "div#jobDescriptionText, "
            "div[data-testid='jobsearch-JobComponent-description']"
        )
        if desc_el:
            data["description"] = (await desc_el.inner_text()).strip()[:5000]
    except Exception as e:
        data["_error"] = str(e)
    return data


_SITE_EXTRACTORS = {
    "linkedin":  _scrape_linkedin,
    "jobstreet": _scrape_jobstreet,
    "glassdoor": _scrape_glassdoor,
    "indeed":    _scrape_indeed,
}


# ---------------------------------------------------------------------------
# Node function
# ---------------------------------------------------------------------------

async def scrape_site_node(state: dict) -> dict:
    """
    LangGraph node (one instance per site, fanned out via Send).

    state must contain:
      site            : str
      urls            : list[str]   — job posting URLs from emails
      spreadsheet_id  : str
    """
    site: str = state["site"]
    urls: list[str] = state["urls"]
    spreadsheet_id: str = state["spreadsheet_id"]

    if not urls:
        return {"raw_job_listings": {site: []}}

    # Deduplicate against what's already in the sheet
    existing_urls = get_existing_job_urls(spreadsheet_id)
    new_urls = [u for u in urls if u not in existing_urls]

    if not new_urls:
        print(f"[scraper:{site}] All {len(urls)} URL(s) already in sheet — skipping.")
        return {"raw_job_listings": {site: []}}

    print(f"[scraper:{site}] Scraping {len(new_urls)} new job URL(s).")
    listings: list[JobPosting] = []
    extractor = _SITE_EXTRACTORS.get(site)

    async with async_playwright() as pw:
        browser, context = await _new_stealth_context(pw)
        try:
            for url in new_urls:
                page = await context.new_page()
                await _apply_stealth(page)

                try:
                    response: Response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=20_000
                    )

                    # Kill switch: login wall or 403
                    status = response.status if response else 0
                    final_url = page.url
                    content_snippet = await page.content()

                    if status == 403 or _is_login_wall(final_url, content_snippet):
                        print(f"[scraper:{site}] BLOCKED at {url}")
                        listings.append(JobPosting(site=site, url=url, blocked=True))
                        await page.close()
                        break  # stop this site's agent

                    await _human_scroll(page)
                    await asyncio.sleep(random.uniform(1.5, 3.0))

                    job_data = await extractor(page) if extractor else {}

                    listing = JobPosting(
                        site=site,
                        url=url,
                        title=job_data.get("title", ""),
                        company=job_data.get("company", ""),
                        location=job_data.get("location", ""),
                        pay=job_data.get("pay", ""),
                        description=job_data.get("description", ""),
                    )
                    listings.append(listing)
                    print(
                        f"[scraper:{site}] OK — {listing.title or '(no title)'} "
                        f"@ {listing.company or '(no company)'}"
                    )

                except Exception as e:
                    print(f"[scraper:{site}] Error on {url}: {e}")
                    listings.append(JobPosting(site=site, url=url))
                finally:
                    await page.close()

                # Inter-page delay
                await asyncio.sleep(random.uniform(8, 14))

        finally:
            await context.close()
            await browser.close()

    return {"raw_job_listings": {site: listings}}

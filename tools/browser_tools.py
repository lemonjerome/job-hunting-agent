"""
Per-site job page scrapers for the Job Hunting Agent.

Strategy per site (no login required for any):

  LinkedIn   → Guest API (requests + BeautifulSoup, no browser)
               Endpoint: /jobs-guest/jobs/api/jobPosting/{job_id}

  Jobstreet  → GraphQL API (requests, no browser)
               Extracts job_id from URL, calls internal GraphQL endpoint

  Indeed     → Playwright + playwright-stealth
               Cloudflare-aware; uses human timing + stealth patches

  Glassdoor  → Playwright + playwright-stealth (primary)
               Falls back to email card context if login wall / CAPTCHA hit
               Email already contains: title, company, location, salary, rating

All functions are async and return a JobData dict.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from bs4 import BeautifulSoup

from config import STEALTH_USER_AGENT, STEALTH_VIEWPORT

try:
    from playwright.async_api import async_playwright, Page
    from playwright_stealth import stealth_async
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Shared data model
# ---------------------------------------------------------------------------

@dataclass
class JobData:
    url: str
    site: str
    title: str = ""
    company: str = ""
    location: str = ""
    pay: str = ""
    description: str = ""
    source: str = "scraped"     # "scraped" | "api" | "email_fallback"
    blocked: bool = False


# ---------------------------------------------------------------------------
# Shared Playwright helpers
# ---------------------------------------------------------------------------

async def _new_stealth_page(playwright):
    """Launch a stealth Chromium page. Returns (browser, page)."""
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=STEALTH_USER_AGENT,
        viewport=STEALTH_VIEWPORT,
        locale="en-US",
        timezone_id="Asia/Manila",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    page = await context.new_page()
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => false });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    """)
    if PLAYWRIGHT_AVAILABLE:
        await stealth_async(page)
    return browser, context, page


def _is_login_wall(url: str, html: str) -> bool:
    login_signals = ["/login", "/signin", "/sign-in", "/authwall",
                     "checkpoint", "join?", "register", "create-account"]
    return any(s in url.lower() for s in login_signals)


async def _human_scroll(page: Page) -> None:
    for _ in range(3):
        await page.mouse.wheel(0, random.randint(250, 500))
        await asyncio.sleep(random.uniform(0.7, 1.4))


# ---------------------------------------------------------------------------
# LinkedIn — Guest API (no Playwright)
# ---------------------------------------------------------------------------

def _linkedin_job_id(url: str) -> str | None:
    """Extract numeric job ID from a LinkedIn job URL."""
    # Handles: /jobs/view/1234567  and  /comm/jobs/view/1234567
    match = re.search(r"/jobs/(?:view|)?(\d{7,})", url)
    if not match:
        match = re.search(r"currentJobId=(\d+)", url)
    return match.group(1) if match else None


async def scrape_linkedin(url: str) -> JobData:
    """
    Fetch job details via LinkedIn's public guest API endpoint.
    No browser, no login — returns structured HTML that BS4 parses.
    """
    job_id = _linkedin_job_id(url)
    if not job_id:
        return JobData(url=url, site="linkedin", blocked=True)

    api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    headers = {
        "User-Agent": STEALTH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.linkedin.com/jobs/",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (401, 403, 429):
                    return JobData(url=url, site="linkedin", blocked=True)
                html = await resp.text()
    except Exception:
        return JobData(url=url, site="linkedin", blocked=True)

    soup = BeautifulSoup(html, "html.parser")

    def _text(selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    title = (
        _text("h2.top-card-layout__title") or
        _text(".top-card-layout__title") or
        _text("h1")
    )
    company = (
        _text("a.topcard__org-name-link") or
        _text(".topcard__org-name-link") or
        _text("[class*='company']")
    )
    location = (
        _text(".topcard__flavor--bullet") or
        _text("[class*='location']")
    )
    pay = _text("[class*='salary']") or _text("[class*='compensation']")
    description = _text(".show-more-less-html__markup") or _text("[class*='description']")

    return JobData(
        url=url, site="linkedin",
        title=title, company=company, location=location,
        pay=pay, description=description[:5000],
        source="api",
    )


# ---------------------------------------------------------------------------
# Jobstreet — GraphQL API (no Playwright)
# ---------------------------------------------------------------------------

_JOBSTREET_GRAPHQL_URL = "https://ph.jobstreet.com/graphql"

_JOBSTREET_QUERY = """
query GetJobDetail($jobId: String!) {
  jobDetail(jobId: $jobId, jobSeoId: "", candidateId: "", solMetaId: null) {
    header {
      jobTitle
      company { name }
      salary { min max currency type }
    }
    location { location }
    jobDetail {
      jobDescription { html }
    }
  }
}
"""

def _jobstreet_job_id(url: str) -> str | None:
    """Extract job ID from a Jobstreet PH URL."""
    # e.g. /job/12345678  or  ?jobId=12345678
    match = re.search(r"/job/(\d+)", url)
    if not match:
        match = re.search(r"[?&]jobId=(\d+)", url)
    return match.group(1) if match else None


async def scrape_jobstreet(url: str) -> JobData:
    """
    Fetch job details via Jobstreet's internal GraphQL API.
    Returns fully structured data without needing a browser.
    """
    job_id = _jobstreet_job_id(url)
    if not job_id:
        return JobData(url=url, site="jobstreet", blocked=True)

    headers = {
        "User-Agent": STEALTH_USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": "https://ph.jobstreet.com/",
        "Origin": "https://ph.jobstreet.com",
    }
    payload = {
        "operationName": "GetJobDetail",
        "query": _JOBSTREET_QUERY,
        "variables": {"jobId": job_id},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _JOBSTREET_GRAPHQL_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (401, 403, 429):
                    return JobData(url=url, site="jobstreet", blocked=True)
                data = await resp.json()
    except Exception:
        return JobData(url=url, site="jobstreet", blocked=True)

    try:
        detail = data["data"]["jobDetail"]
        header = detail["header"]
        salary = header.get("salary") or {}
        pay = ""
        if salary.get("min"):
            currency = salary.get("currency", "PHP")
            pay = f"{currency} {salary['min']:,}–{salary.get('max', ''):,} / {salary.get('type', 'month')}"

        description_html = (
            detail.get("jobDetail", {})
                  .get("jobDescription", {})
                  .get("html", "")
        )
        description = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)

        return JobData(
            url=url, site="jobstreet",
            title=header.get("jobTitle", ""),
            company=(header.get("company") or {}).get("name", ""),
            location=(detail.get("location") or {}).get("location", ""),
            pay=pay,
            description=description[:5000],
            source="api",
        )
    except (KeyError, TypeError):
        return JobData(url=url, site="jobstreet", blocked=True)


# ---------------------------------------------------------------------------
# Indeed — Playwright + stealth (Cloudflare-aware)
# ---------------------------------------------------------------------------

async def scrape_indeed(url: str) -> JobData:
    """
    Fetch Indeed job page via stealth Playwright.
    Indeed uses Cloudflare — stealth patches + human timing reduce blocks.
    """
    if not PLAYWRIGHT_AVAILABLE:
        return JobData(url=url, site="indeed", blocked=True)

    async with async_playwright() as pw:
        browser, context, page = await _new_stealth_page(pw)
        try:
            # Randomised pre-delay
            await asyncio.sleep(random.uniform(1.5, 3.0))

            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            status = resp.status if resp else 0
            final_url = page.url

            if status == 403 or _is_login_wall(final_url, ""):
                return JobData(url=url, site="indeed", blocked=True)

            await _human_scroll(page)
            await asyncio.sleep(random.uniform(1.0, 2.5))

            def _text(selector: str) -> str:
                return page.locator(selector).first

            async def _get(selector: str) -> str:
                try:
                    el = page.locator(selector).first
                    return (await el.inner_text(timeout=3000)).strip()
                except Exception:
                    return ""

            title = await _get("h1.jobsearch-JobInfoHeader-title") or await _get("h1[data-testid]")
            company = (
                await _get("div[data-testid='inlineHeader-companyName'] a") or
                await _get("span[data-testid='inlineHeader-companyName']")
            )
            location = await _get("div[data-testid='inlineHeader-companyLocation']")
            pay = await _get("span[data-testid='attribute_snippet_testid']")
            description = await _get("div#jobDescriptionText")

            return JobData(
                url=url, site="indeed",
                title=title, company=company, location=location,
                pay=pay, description=description[:5000],
                source="scraped",
            )
        except Exception:
            return JobData(url=url, site="indeed", blocked=True)
        finally:
            await context.close()
            await browser.close()


# ---------------------------------------------------------------------------
# Glassdoor — Playwright + stealth, email-data fallback
# ---------------------------------------------------------------------------

async def scrape_glassdoor(url: str, email_context: dict | None = None) -> JobData:
    """
    Attempt to fetch a Glassdoor job page via stealth Playwright.

    Glassdoor is heavily protected (CAPTCHA, login walls).
    If the page is blocked, falls back to `email_context` — a dict of job
    card data already extracted from the alert email (company, title, etc.).

    email_context keys: title, company, location, pay, rating
    """
    if not PLAYWRIGHT_AVAILABLE:
        return _glassdoor_from_email(url, email_context)

    async with async_playwright() as pw:
        browser, context, page = await _new_stealth_page(pw)
        try:
            await asyncio.sleep(random.uniform(2.0, 4.0))
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            status = resp.status if resp else 0
            final_url = page.url
            page_html = await page.content()

            # Login wall or CAPTCHA detected → use email fallback
            if (status == 403
                    or _is_login_wall(final_url, page_html)
                    or "captcha" in page_html.lower()
                    or "sign in" in page_html.lower()[:3000]):
                print(f"[glassdoor] Blocked — using email fallback for {url}")
                return _glassdoor_from_email(url, email_context)

            await _human_scroll(page)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            async def _get(selector: str) -> str:
                try:
                    return (await page.locator(selector).first.inner_text(timeout=3000)).strip()
                except Exception:
                    return ""

            title = (
                await _get("[data-test='jobTitle']") or
                await _get("h1.job-title") or
                await _get("div.JobDetails_jobTitle__Rw_gn")
            )
            company = (
                await _get("[data-test='employerName']") or
                await _get("div.EmployerProfile_employerName__Fs9CR") or
                await _get("[data-test='employer-short-name']")
            )
            location = (
                await _get("[data-test='location']") or
                await _get("div[data-test='emp-location']")
            )
            pay = (
                await _get("[data-test='detailSalary']") or
                await _get("span.JobDetails_salaryEstimate__arV5J")
            )
            description = await _get("div.JobDetails_jobDescription__uW_fK")

            # If the page rendered but gave us nothing useful, fall back to email
            if not title and not company:
                print(f"[glassdoor] Empty page — using email fallback for {url}")
                return _glassdoor_from_email(url, email_context)

            return JobData(
                url=url, site="glassdoor",
                title=title, company=company, location=location,
                pay=pay, description=description[:5000],
                source="scraped",
            )
        except Exception:
            return _glassdoor_from_email(url, email_context)
        finally:
            await context.close()
            await browser.close()


def _glassdoor_from_email(url: str, ctx: dict | None) -> JobData:
    """Build a JobData from email card context when Glassdoor page is inaccessible."""
    if not ctx:
        return JobData(url=url, site="glassdoor", blocked=True)
    return JobData(
        url=url, site="glassdoor",
        title=ctx.get("title", ""),
        company=ctx.get("company", ""),
        location=ctx.get("location", ""),
        pay=ctx.get("pay", ""),
        description=ctx.get("rating", ""),   # store rating in description field for now
        source="email_fallback",
        blocked=False,
    )


# ---------------------------------------------------------------------------
# Glassdoor email card parser
# ---------------------------------------------------------------------------

def parse_glassdoor_email_cards(html: str) -> dict[str, dict]:
    """
    Parse a Glassdoor job alert email HTML and return a dict mapping
    job URL → card data {title, company, location, pay, rating}.

    Called in email_screener before URLs are passed to the scraper.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, dict] = {}

    # Glassdoor email cards are typically table rows or div blocks
    # Each card has a job link and surrounding company/salary text
    for link in soup.find_all("a", href=True):
        href: str = link["href"]
        # Only job detail links
        if not re.search(r"glassdoor\.com.*(job-listing|GD_JOB|JobId)", href, re.I):
            continue

        # Walk up to find the card container (3-4 levels up usually)
        card_el = link
        for _ in range(5):
            card_el = card_el.parent
            if card_el is None:
                break
            text = card_el.get_text(" ", strip=True)
            if len(text) > 50:
                break

        if card_el is None:
            continue

        card_text = card_el.get_text(" ", strip=True)

        # Extract title from the link itself
        title = link.get_text(strip=True)

        # Try to find company name (often a sibling element)
        company = ""
        company_el = card_el.find(attrs={"data-test": "employer-short-name"})
        if not company_el:
            company_el = card_el.find(class_=re.compile(r"employer|company", re.I))
        if company_el:
            company = company_el.get_text(strip=True)

        # Location
        location = ""
        loc_el = card_el.find(attrs={"data-test": "employer-location"})
        if not loc_el:
            loc_el = card_el.find(class_=re.compile(r"location|loc", re.I))
        if loc_el:
            location = loc_el.get_text(strip=True)

        # Salary — look for currency patterns
        pay = ""
        pay_match = re.search(r"[₱$€£]\s*[\d,]+(?:\s*[-–]\s*[\d,]+)?(?:\s*/\s*\w+)?", card_text)
        if pay_match:
            pay = pay_match.group(0).strip()

        # Rating — look for "X.X ★" patterns
        rating = ""
        rating_match = re.search(r"\b([1-5]\.\d)\b", card_text)
        if rating_match:
            rating = f"{rating_match.group(1)} ★"

        cards[href] = {
            "title": title,
            "company": company,
            "location": location,
            "pay": pay,
            "rating": rating,
        }

    return cards


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

SCRAPERS = {
    "linkedin":  scrape_linkedin,
    "jobstreet": scrape_jobstreet,
    "indeed":    scrape_indeed,
}

async def scrape_job(site: str, url: str, email_context: dict | None = None) -> JobData:
    """
    Dispatch to the correct scraper for the given site.
    Glassdoor always receives email_context for fallback.
    """
    if site == "glassdoor":
        return await scrape_glassdoor(url, email_context)
    scraper = SCRAPERS.get(site)
    if scraper:
        return await scraper(url)
    return JobData(url=url, site=site, blocked=True)

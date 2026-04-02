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
# Unified email job card parser (all 4 sites)
# ---------------------------------------------------------------------------

# Per-site patterns that identify a job detail link in email HTML *or* plain text.
# Includes direct job URLs AND tracking/redirect URLs used by each site's email platform.
_EMAIL_JOB_LINK_PATTERNS: dict[str, re.Pattern] = {
    "linkedin":  re.compile(
        r"linkedin\.com/(comm/)?jobs/view/\d+",
        re.I,
    ),
    # Direct URL OR url.jobstreet.com SendGrid tracking link
    "jobstreet": re.compile(
        r"(ph\.)?jobstreet\.com(\.ph)?/job/\d+|url\.jobstreet\.com",
        re.I,
    ),
    "glassdoor": re.compile(
        r"glassdoor\.com.*(job-listing|GD_JOB|JobId|jobListing)",
        re.I,
    ),
    # Direct viewjob URL, pagead tracking, or cts.indeed.com single-job email links
    "indeed": re.compile(
        r"(click\.indeed\.com|indeed\.com/viewjob|ph\.indeed\.com/viewjob"
        r"|ph\.indeed\.com/pagead/clk|cts\.indeed\.com/v3/)",
        re.I,
    ),
}

# Sites whose email links are tracking redirects needing resolution to canonical URLs
_NEEDS_REDIRECT: set[str] = {"jobstreet", "indeed"}

# Pay/salary regex — matches ₱/$/£/€ + PHP amounts with optional K suffix, ranges, period
_PAY_RE = re.compile(
    r"(?:PHP|₱|\$|£|€)\s*[\d,]+[Kk]?(?:\s*[-–]\s*[\d,]+[Kk]?)?(?:\s*/\s*\w+)?",
    re.I,
)

# Rating — "4.2" or "4.2 ★" in card text
_RATING_RE = re.compile(r"\b([1-5]\.\d)\b")

# Glassdoor link text often concatenates company + rating + title, e.g.:
# "Acme Corp3.9 ★Senior ML Engineer"
# Split on the rating separator to isolate the job title.
_GLASSDOOR_RATING_SPLIT_RE = re.compile(r"[1-5]\.\d\s*★\s*")


def _card_context_from_element(card_el, link) -> dict:
    """Extract {title, company, location, pay, rating} from a card DOM element."""
    card_text = card_el.get_text(" ", strip=True)
    raw_link_text = link.get_text(strip=True)

    # Glassdoor email cards concatenate company + rating + title in the link text,
    # e.g. "Acme Corp3.9 ★Senior ML Engineer (Remote)$124K - $138K..."
    # Split on the rating separator to isolate company (before) and title (after).
    rating = ""
    title = raw_link_text
    company_from_link = ""
    rating_match_in_link = _GLASSDOOR_RATING_SPLIT_RE.search(raw_link_text)
    if rating_match_in_link:
        rating_str = raw_link_text[rating_match_in_link.start():rating_match_in_link.end()]
        rating = rating_str.strip()
        company_from_link = raw_link_text[:rating_match_in_link.start()].strip()
        title = raw_link_text[rating_match_in_link.end():].strip()
        # Title may still have pay appended, strip trailing pay info
        pay_in_title = _PAY_RE.search(title)
        if pay_in_title:
            title = title[:pay_in_title.start()].strip()

    # Company — prefer data-test attrs, fall back to class patterns, then link-parsed value
    company = company_from_link  # may already be set from Glassdoor link text split
    if not company:
        for attr_val in ("employer-short-name", "employer-name", "company-name"):
            el = card_el.find(attrs={"data-test": attr_val})
            if el:
                company = el.get_text(strip=True)
                break
    if not company:
        el = card_el.find(class_=re.compile(r"employer|company|org", re.I))
        if el:
            company = el.get_text(strip=True)

    # Location
    location = ""
    for attr_val in ("employer-location", "location", "job-location"):
        el = card_el.find(attrs={"data-test": attr_val})
        if el:
            location = el.get_text(strip=True)
            break
    if not location:
        el = card_el.find(class_=re.compile(r"location|city|address", re.I))
        if el:
            location = el.get_text(strip=True)

    # Pay — regex over card text
    pay = ""
    pay_match = _PAY_RE.search(card_text)
    if pay_match:
        pay = pay_match.group(0).strip()

    # Rating — already set if parsed from Glassdoor link text, else search card text
    if not rating:
        rating_match = _RATING_RE.search(card_text)
        if rating_match:
            rating = f"{rating_match.group(1)} ★"

    return {"title": title, "company": company, "location": location, "pay": pay, "rating": rating}


def _find_card_container(link, min_text_len: int = 40):
    """Walk up the DOM from a link to find the enclosing card element."""
    card_el = link
    for _ in range(6):
        parent = card_el.parent
        if parent is None:
            break
        text = parent.get_text(" ", strip=True)
        if len(text) >= min_text_len:
            card_el = parent
            break
        card_el = parent
    return card_el


def parse_email_job_cards(html: str, site: str) -> dict[str, dict]:
    """
    Parse a job alert email HTML for any of the 4 supported sites and return
    a dict mapping job URL → card data {title, company, location, pay, rating}.

    Two-pass strategy:
      1. HTML pass  — find <a href> tags, walk DOM for card context.
      2. Plain text fallback — if email arrived as text/plain (wrapped in <pre>),
         extract URLs via regex and parse surrounding lines for card context.

    For sites in _NEEDS_REDIRECT (Jobstreet, Indeed), tracking URLs are returned
    as-is; the caller (email_screener_node) must resolve them to canonical URLs.

    Returns {} if site is unrecognised or no job links are found.
    """
    pattern = _EMAIL_JOB_LINK_PATTERNS.get(site)
    if not pattern:
        return {}

    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, dict] = {}

    # --- Pass 1: HTML <a> tag parsing ---
    for link in soup.find_all("a", href=True):
        href: str = link["href"]
        job_url = _resolve_job_url(href, pattern)
        if not job_url:
            continue
        card_el = _find_card_container(link)
        ctx = _card_context_from_element(card_el, link)
        # Keep first seen, but upgrade if stored title is empty and new one has a title
        # (LinkedIn emails have 3 anchors per job; first is often empty)
        if job_url not in cards or (not cards[job_url].get("title") and ctx.get("title")):
            cards[job_url] = ctx

    # --- Post-pass: fix generic link texts (e.g. Indeed "View job" CTA) ---
    # Indeed single-job emails have "View job" as the link text; the job title
    # is in an <h1> heading with format "Title @ Company". Patch cards that
    # have a generic placeholder title.
    _GENERIC_TITLES = re.compile(r"^(view job|apply now|apply|see job|learn more|introduction)$", re.I)
    if site == "indeed" and cards:
        h1 = soup.find("h1")
        h1_text = h1.get_text(strip=True) if h1 else ""
        for url, ctx in cards.items():
            if _GENERIC_TITLES.match((ctx.get("title") or "").strip()):
                if " @ " in h1_text:
                    parts = h1_text.split(" @ ", 1)
                    ctx["title"] = parts[0].strip()
                    if not ctx.get("company"):
                        ctx["company"] = parts[1].strip()
                elif h1_text:
                    ctx["title"] = h1_text

    if cards:
        return cards

    # --- Pass 2: Plain-text fallback ---
    # Triggered when the email arrived as text/plain (no <a> tags found).
    # _extract_html_body wraps plain text in <pre>...</pre>.
    raw_text = soup.get_text(" ", strip=False)
    plain_cards = _parse_plain_text_email(raw_text, site, pattern)
    return plain_cards


# ---------------------------------------------------------------------------
# Plain-text email parser (LinkedIn, Jobstreet, Indeed often send text/plain)
# ---------------------------------------------------------------------------

_PLAIN_URL_RE = re.compile(r"https?://[^\s\[\]<>\"']+")

# Lines to skip when extracting card context from plain text.
# Matches anywhere in the line (not just full-line) to catch suffixed variants
# like "Apply with resume & profile" or "This company is actively hiring now".
_NOISE_LINES = re.compile(
    r"(this company is actively hiring|actively recruiting|"
    r"apply with resume|easily apply|quickly apply|"
    r"recently posted|view job:|^\s*logo\s*$|"
    r"\d+ school alumni|"
    r"we want to help|we recommend|based on your|"
    r"%%str_to_replace|open tracking|"
    r"job recommendations|match\.|hi gabriel)",
    re.I,
)


def _parse_plain_text_email(text: str, site: str, pattern: re.Pattern) -> dict[str, dict]:
    """
    Extract job URL → card context from a plain-text email body.
    Finds all URLs matching the site pattern and looks at lines above each URL
    to extract title, company, location, and pay.
    """
    lines = text.splitlines()
    cards: dict[str, dict] = {}
    seen: set[str] = set()

    for i, line in enumerate(lines):
        # Find all URLs on this line
        for m in _PLAIN_URL_RE.finditer(line):
            raw_url = m.group(0).rstrip(".,;)\"'>]")
            if not pattern.search(raw_url):
                continue
            if raw_url in seen:
                continue
            seen.add(raw_url)

            # Collect non-noise context lines above this URL (up to 8 lines back)
            ctx_lines: list[str] = []
            for prev_line in lines[max(0, i - 8):i]:
                stripped = prev_line.strip()
                if not stripped:
                    continue
                if _PLAIN_URL_RE.match(stripped) or stripped.startswith("[http"):
                    continue  # skip other URLs / bracketed link lines
                if _NOISE_LINES.search(stripped):
                    continue
                if len(stripped) > 100:
                    continue  # skip long description/snippet lines
                ctx_lines.append(stripped)

            ctx = _plain_text_card_context(ctx_lines, site, text)
            cards[raw_url] = ctx

    return cards


def _plain_text_card_context(ctx_lines: list[str], site: str, full_text: str) -> dict:
    """Parse title / company / location / pay from lines above a job URL."""
    title = company = location = pay = ""

    if site == "linkedin":
        # Format (bottom of ctx_lines, reading upward from URL):
        #   ...
        #   [location]
        #   [company]
        #   [title]
        #   View job: <url>      ← current line
        # ctx_lines is in top-down order so last entries are closest to URL
        useful = ctx_lines  # already filtered
        if len(useful) >= 3:
            title    = useful[-3]
            company  = useful[-2]
            location = useful[-1]
        elif len(useful) == 2:
            title   = useful[-2]
            company = useful[-1]
        elif len(useful) == 1:
            title = useful[-1]

    elif site == "jobstreet":
        # Format:
        #   [title]
        #   [company]
        #   [location]
        #   Recently posted        ← filtered out as noise
        #   [tracking URL]
        useful = [l for l in ctx_lines if l.lower() not in ("recently posted", "logo")]
        if len(useful) >= 3:
            title    = useful[-3]
            company  = useful[-2]
            location = useful[-1]
        elif len(useful) == 2:
            title   = useful[-2]
            company = useful[-1]
        elif len(useful) == 1:
            title = useful[-1]
        pay_m = _PAY_RE.search(" ".join(ctx_lines))
        if pay_m:
            pay = pay_m.group(0).strip()

    elif site == "indeed":
        # Format:
        #   [title]
        #   [company] - [location]      (on one line separated by " - ")
        #   Easily apply               ← filtered out
        #   [short description snippet]
        #   [tracking URL]
        useful = ctx_lines
        if len(useful) >= 2:
            title = useful[-2]
            company_loc = useful[-1]
            if " - " in company_loc:
                parts = company_loc.split(" - ", 1)
                company  = parts[0].strip()
                location = parts[1].strip()
            else:
                company = company_loc
        elif len(useful) == 1:
            title = useful[-1]
        pay_m = _PAY_RE.search(" ".join(ctx_lines))
        if pay_m:
            pay = pay_m.group(0).strip()

    return {"title": title, "company": company, "location": location, "pay": pay, "rating": ""}


def _normalize_job_url(href: str) -> str:
    """
    Strip tracking query params from job URLs where the job ID is in the path.
    Keeps query params for URLs where the job ID IS a query param (e.g. Glassdoor).
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(href)
    # LinkedIn job IDs are always in the path; strip all query params
    if "linkedin.com" in parsed.netloc and re.search(r"/jobs/view/\d+", parsed.path):
        return urlunparse(parsed._replace(query="", fragment=""))
    return href


def _resolve_job_url(href: str, pattern: re.Pattern) -> str | None:
    """
    Return a canonical job URL from href.
    Tries direct match first, then decodes common tracking/redirect query params.
    """
    from urllib.parse import urlparse, parse_qs, unquote

    # Direct match
    if pattern.search(href):
        return _normalize_job_url(href)

    # Try to decode tracking redirect — check common param names
    try:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        param_keys = (
            "url", "u", "target", "redirect", "dest", "link",
            "clickUrl", "destination", "href", "URL",
            "redirect_url", "target_url",
        )
        for key in param_keys:
            if key in params:
                candidate = unquote(params[key][0])
                if pattern.search(candidate):
                    return candidate

        # Brute-force: try every query param value
        for values in params.values():
            candidate = unquote(values[0])
            if pattern.search(candidate):
                return candidate
    except Exception:
        pass

    return None


# Backward-compatible alias (used by email_screener imports)
def parse_glassdoor_email_cards(html: str) -> dict[str, dict]:
    """Deprecated: use parse_email_job_cards(html, 'glassdoor') instead."""
    return parse_email_job_cards(html, "glassdoor")


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
    email_context (from email card parser) is used as fallback data if scraping fails.
    Glassdoor uses it directly; other sites fall back to it only when blocked.
    """
    if site == "glassdoor":
        return await scrape_glassdoor(url, email_context)

    scraper = SCRAPERS.get(site)
    if not scraper:
        return JobData(url=url, site=site, blocked=True)

    job = await scraper(url)

    # For non-Glassdoor sites: if scraping returned blocked/empty AND we have
    # email card context, backfill the missing fields from the email data.
    if email_context and (job.blocked or (not job.title and not job.company)):
        return JobData(
            url=url,
            site=site,
            title=email_context.get("title", ""),
            company=email_context.get("company", ""),
            location=email_context.get("location", ""),
            pay=email_context.get("pay", ""),
            description="",
            source="email_fallback",
            blocked=False,
        )

    return job

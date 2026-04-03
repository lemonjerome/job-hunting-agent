"""
Gmail tool wrappers for the Job Hunting Agent.

Uses the Google Gmail API directly (same credentials as GDrive).
Provides:
  - Searching for job alert emails from the 4 monitored senders
  - Reading full email content (HTML body)
  - Extracting job posting hyperlinks from email HTML
  - Sending self-notification emails
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

from config import EMAIL_LOOKBACK_HOURS, EMAIL_SENDERS, GMAIL_CREDENTIALS, IS_CLOUD_RUN, GMAIL_TOKEN_SECRET, get_secret

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / ".gmail_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# --- Per-site URL patterns that identify a job detail page (not "see all") ---
JOB_URL_PATTERNS: dict[str, re.Pattern] = {
    "linkedin": re.compile(
        r"https?://(www\.)?linkedin\.com/(comm/)?jobs/view/\d+"
    ),
    "jobstreet": re.compile(
        r"https?://((www\.|ph\.)?jobstreet\.com(\.ph)?)/job/\d+"
    ),
    "glassdoor": re.compile(
        r"https?://(www\.)?glassdoor\.(com|sg|co\.uk)/job-listing/.*?jobListingId=\d+"
    ),
    "indeed": re.compile(
        r"https?://(click\.indeed\.com|www\.indeed\.com/viewjob|ph\.indeed\.com/viewjob)"
    ),
}

# Tracking/redirect link patterns used by email platforms for each site.
# When a direct URL match fails, we look for these in the href and decode the
# embedded target URL from common query-param names.
_TRACKING_DOMAINS: dict[str, re.Pattern] = {
    "linkedin":  re.compile(r"https?://[^/]*linkedin\.com/comm/careers-email-tracking", re.I),
    "jobstreet": re.compile(r"https?://[^/]*e\.jobstreet\.com/", re.I),
    "indeed":    re.compile(r"https?://click\.indeed\.com/", re.I),
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_gmail_service():
    creds = None

    if IS_CLOUD_RUN and GMAIL_TOKEN_SECRET:
        # Cloud Run: load token JSON from Secret Manager
        token_json = get_secret(GMAIL_TOKEN_SECRET)
        creds = Credentials.from_authorized_user_info(
            __import__("json").loads(token_json), SCOPES
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
    else:
        # Local: load from file, refresh or re-auth as needed
        if TOKEN_PATH.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, SCOPES)
                creds = flow.run_local_server(port=0)
            TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_job_alert_emails(seen_ids: set[str]) -> list[dict]:
    """
    Search Gmail for job alert emails from the 4 monitored senders
    received in the past EMAIL_LOOKBACK_HOURS hours.

    Returns a list of dicts (one per email) with keys:
      message_id, site, sender, subject, time_received, html_body
    Excludes any email whose message_id is already in seen_ids.
    """
    service = _get_gmail_service()
    results: list[dict] = []

    # Build sender query — Gmail 'from:' OR syntax
    senders_query = " OR ".join(
        f"from:{addr}" for addr in EMAIL_SENDERS.values()
    )
    query = f"({senders_query}) newer_than:{EMAIL_LOOKBACK_HOURS}h"

    response = service.users().messages().list(
        userId="me", q=query, maxResults=50,
    ).execute()
    messages = response.get("messages", [])

    for msg_stub in messages:
        msg_id = msg_stub["id"]
        if msg_id in seen_ids:
            continue

        full = service.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()

        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        sender_addr = _parse_email_address(headers.get("From", ""))
        subject = headers.get("Subject", "")
        date_header = headers.get("Date", "")
        time_received = _parse_date(date_header)

        site = _identify_site(sender_addr)
        if not site:
            continue  # unexpected sender, skip

        html_body = _extract_html_body(full["payload"])

        results.append({
            "message_id": msg_id,
            "site": site,
            "sender": sender_addr,
            "subject": subject,
            "time_received": time_received,
            "html_body": html_body,
        })

    return results


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def extract_job_urls(email: dict) -> list[str]:
    """
    Parse the email HTML body and return all URLs that look like
    direct job detail pages for the email's site.
    Filters out 'see all jobs', 'view more', unsubscribe links, etc.

    For sites that use click-tracking redirects (e.g. e.jobstreet.com),
    the embedded target URL is decoded from common query-param names
    (url, u, target, redirect) and checked against the site pattern.
    """
    html = email.get("html_body", "")
    site = email.get("site", "")
    if not html or site not in JOB_URL_PATTERNS:
        return []

    soup = BeautifulSoup(html, "html.parser")
    pattern = JOB_URL_PATTERNS[site]
    tracking = _TRACKING_DOMAINS.get(site)
    seen: set[str] = set()
    urls: list[str] = []

    for tag in soup.find_all("a", href=True):
        href: str = tag["href"]

        # Direct match
        if pattern.search(href):
            if href not in seen:
                seen.add(href)
                urls.append(href)
            continue

        # Tracking/redirect URL — try to extract the embedded target
        if tracking and tracking.search(href):
            decoded = _decode_tracking_url(href, pattern)
            if decoded and decoded not in seen:
                seen.add(decoded)
                urls.append(decoded)

    return urls


def _decode_tracking_url(href: str, pattern: re.Pattern) -> str | None:
    """
    Given a tracking/redirect URL, attempt to extract the final destination
    URL from common query-parameter names and check it against pattern.
    Returns the decoded URL if it matches, else None.
    """
    from urllib.parse import urlparse, parse_qs, unquote

    try:
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        # Extended set of common redirect/tracking param names
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
        # Brute-force: try every param value (catches unusual param names)
        for values in params.values():
            candidate = unquote(values[0])
            if pattern.search(candidate):
                return candidate
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_email(to: str, subject: str, html_body: str) -> None:
    """Send an email from the authenticated Gmail account."""
    service = _get_gmail_service()
    msg = MIMEMultipart("alternative")
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(
        userId="me", body={"raw": raw},
    ).execute()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _identify_site(sender_addr: str) -> str | None:
    """Map a sender email address to a site key."""
    for site, addr in EMAIL_SENDERS.items():
        if addr.lower() in sender_addr.lower():
            return site
    return None


def _parse_email_address(from_header: str) -> str:
    """Extract bare email address from a From header like 'Name <addr@x.com>'."""
    match = re.search(r"<([^>]+)>", from_header)
    return match.group(1) if match else from_header.strip()


def _parse_date(date_str: str) -> str:
    """Parse a Gmail Date header into an ISO 8601 string."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        from config import PHT
        return dt.astimezone(PHT).isoformat()
    except Exception:
        return date_str


def _extract_html_body(payload: dict) -> str:
    """
    Walk the MIME payload tree and return the best body for parsing:
    prefers text/html over text/plain (multipart/alternative emails send both).

    For multipart messages, collects all text parts first and returns HTML
    if found; otherwise returns the plain-text fallback wrapped in <pre>.
    """
    html_part: str = ""
    plain_part: str = ""

    def _collect(p: dict) -> None:
        nonlocal html_part, plain_part
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data", "")

        if mime == "text/html" and data and not html_part:
            html_part = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        elif mime == "text/plain" and data and not plain_part:
            plain = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            plain_part = f"<pre>{plain}</pre>"
        else:
            for sub in p.get("parts", []):
                _collect(sub)

    _collect(payload)
    return html_part or plain_part

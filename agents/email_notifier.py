"""
Phase 6 — Email Notifier Agent

LangGraph node: email_notifier

Sends a self-notification email when there are newly added STRONG jobs.
Only runs when new_jobs contains at least one STRONG entry.

Email format (per job):
  - Role, company, site, location, pay
  - Skills comparison table: JD Requirement | My Resume | Fit (✓/~/✗)
  - Direct link to job posting
  - Footer: link to Google Sheet + GDrive folder
"""

from __future__ import annotations

from config import GDRIVE_FOLDER, GSHEET_FILE_NAME, SELF_EMAIL
from graph.state import AssessedJob
from tools.gmail_tools import send_email

_SITE_LABELS = {
    "linkedin":  "LinkedIn",
    "jobstreet": "Jobstreet",
    "glassdoor": "Glassdoor",
    "indeed":    "Indeed",
}

_FIT_STYLE = {
    "MATCH":   ("#f6fff8", "#2e7d32", "✓"),
    "PARTIAL": ("#fff8f0", "#e65100", "~"),
    "GAP":     ("#fff0f0", "#c62828", "✗"),
}


def _match_table(match_breakdown: list[dict]) -> str:
    """Render an HTML comparison table from match_breakdown rows."""
    if not match_breakdown:
        return ""

    rows = ""
    for row in match_breakdown:
        fit = row.get("fit", "PARTIAL").upper()
        bg, color, symbol = _FIT_STYLE.get(fit, _FIT_STYLE["PARTIAL"])
        req = row.get("requirement", "")
        resume = row.get("my_resume", "")
        rows += f"""
        <tr style="background:{bg}">
          <td style="padding:6px 8px;border-bottom:1px solid #eee">{req}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee">{resume}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:center;
                     color:{color};font-weight:bold;font-size:15px">{symbol}</td>
        </tr>"""

    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:10px">
      <tr style="background:#f0f0f0">
        <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">JD Requirement</th>
        <th style="padding:6px 8px;text-align:left;border-bottom:2px solid #ddd">My Resume</th>
        <th style="padding:6px 8px;text-align:center;border-bottom:2px solid #ddd">Fit</th>
      </tr>
      {rows}
    </table>"""


def _build_email(
    strong_jobs: list[AssessedJob],
    spreadsheet_id: str,
) -> tuple[str, str]:
    """Return (subject, html_body) for the notification email."""

    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    folder_url = f"https://drive.google.com/drive/search?q={GDRIVE_FOLDER.replace(' ', '+')}"
    count = len(strong_jobs)

    subject = (
        f"[Job Alert] {count} Strong AI/ML Opening{'s' if count > 1 else ''} Found"
    )

    job_rows = ""
    for job in strong_jobs:
        site_label = _SITE_LABELS.get(job.site, job.site.capitalize())
        source_note = " (from email)" if job.scrape_source == "email_fallback" else ""

        display_role = job.normalized_role or job.title
        display_pay  = job.normalized_pay  or job.pay

        pay_row = (
            f"<tr><td style='color:#555;padding:2px 0'>💰 Pay</td>"
            f"<td style='padding:2px 8px'>{display_pay}</td></tr>"
            if display_pay else ""
        )

        comparison_table = _match_table(job.match_breakdown or [])
        summary_text = (
            f"<p style='margin:10px 0 0;font-size:13px;color:#444;font-style:italic'>"
            f"{job.strength_explanation}</p>"
            if job.strength_explanation else ""
        )

        job_rows += f"""
        <div style="
            border:1px solid #d4edda;
            border-radius:8px;
            padding:16px 20px;
            margin-bottom:16px;
            background:#f6fff8;
        ">
            <h3 style="margin:0 0 4px;color:#1a1a1a">{display_role}</h3>
            <p style="margin:0 0 8px;color:#444;font-size:15px">{job.company}</p>
            <table style="font-size:13px;border-collapse:collapse">
                <tr>
                    <td style="color:#555;padding:2px 0">📍 Location</td>
                    <td style="padding:2px 8px">{job.normalized_location or job.location or '—'}</td>
                </tr>
                {pay_row}
                <tr>
                    <td style="color:#555;padding:2px 0">🌐 Source</td>
                    <td style="padding:2px 8px">{site_label}{source_note}</td>
                </tr>
            </table>

            {comparison_table}
            {summary_text}

            <a href="{job.url}"
               style="
                   display:inline-block;
                   margin-top:14px;
                   padding:7px 16px;
                   background:#0a66c2;
                   color:#fff;
                   text-decoration:none;
                   border-radius:5px;
                   font-size:13px;
               ">
                View Job Posting →
            </a>
        </div>
        """

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:660px;margin:auto;color:#1a1a1a">

        <div style="background:#0a66c2;padding:20px 24px;border-radius:8px 8px 0 0">
            <h2 style="margin:0;color:#fff">
                🟢 {count} Strong AI/ML Job Match{'es' if count > 1 else ''}
            </h2>
            <p style="margin:6px 0 0;color:#cde;font-size:14px">
                New openings that match your resume — review and apply
            </p>
        </div>

        <div style="padding:20px 24px;background:#fff;border:1px solid #e0e0e0;border-top:none">
            {job_rows}
        </div>

        <div style="
            padding:16px 24px;
            background:#f5f5f5;
            border:1px solid #e0e0e0;
            border-top:none;
            border-radius:0 0 8px 8px;
            font-size:13px;
            color:#555;
        ">
            <p style="margin:0 0 6px">
                📊 <a href="{sheet_url}" style="color:#0a66c2">
                    Open {GSHEET_FILE_NAME} spreadsheet
                </a>
            </p>
            <p style="margin:0">
                📁 <a href="{folder_url}" style="color:#0a66c2">
                    Open {GDRIVE_FOLDER} folder in Drive
                </a>
            </p>
        </div>

    </div>
    """

    return subject, html_body


async def email_notifier_node(state: dict) -> dict:
    """
    LangGraph node.
    Sends a notification email for newly added STRONG jobs only.
    """
    new_jobs: list[AssessedJob] = state.get("new_jobs", [])
    spreadsheet_id: str = state["spreadsheet_id"]

    strong_jobs = [j for j in new_jobs if j.resume_strength == "STRONG"]

    if not strong_jobs:
        print(
            f"[email_notifier] {len(new_jobs)} new job(s) added but none are STRONG — "
            "no notification sent."
        )
        return {"notified": False}

    subject, html_body = _build_email(strong_jobs, spreadsheet_id)
    send_email(to=SELF_EMAIL, subject=subject, html_body=html_body)

    print(
        f"[email_notifier] Sent notification to {SELF_EMAIL} — "
        f"{len(strong_jobs)} STRONG job(s)."
    )
    return {"notified": True}

"""
Phase 4 — Job Screener Agent

LangGraph node: job_screener

For each job in raw_job_listings:
  1. Skip jobs marked as blocked with no data
  2. Confirm the job is AI/ML related (LLM)
  3. Compare the job description against resume.md (LLM)
     → WEAK / MODERATE / STRONG + explanation
  4. Generate a short description summary (LLM)

Resume is read from GDrive at runtime (never committed — public repo).
Falls back to local resume.md if GDrive read fails (e.g. local dev).

Input state:  raw_job_listings, spreadsheet_id
Output state: assessed_jobs (list[AssessedJob])
"""

from __future__ import annotations

import asyncio
import io
import json
from config import now_pht
from pathlib import Path

from langchain_core.messages import HumanMessage

from config import get_llm
from graph.state import AssessedJob
from tools.browser_tools import JobData
from tools.sheets_tools import download_resume_pdf

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Resume loading
# ---------------------------------------------------------------------------

def _pdf_to_text(pdf_bytes: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    except ImportError:
        import PyPDF2 as pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))

    return "\n".join(
        (page.extract_text() or "").strip()
        for page in reader.pages
    )


def _load_resume() -> str:
    """
    Load resume text. Try GDrive first, fall back to local resume.md.
    Returns plain text (not Markdown).
    """
    # Primary: download PDF from GDrive and extract text
    try:
        pdf_bytes = download_resume_pdf()
        text = _pdf_to_text(pdf_bytes)
        if text.strip():
            print("[job_screener] Resume loaded from GDrive.")
            return text
    except Exception as e:
        print(f"[job_screener] GDrive resume unavailable ({e}), falling back to local.")

    # Fallback: local resume.md (present after running convert_resume.py locally)
    local_md = ROOT / "resume.md"
    if local_md.exists():
        print("[job_screener] Resume loaded from local resume.md.")
        return local_md.read_text(encoding="utf-8")

    raise FileNotFoundError(
        "Resume not found. Run 'python scripts/convert_resume.py' first."
    )


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

_IS_AI_ML_PROMPT = """\
Is the following job posting for an AI or Machine Learning role?

Consider YES for: AI Engineer, ML Engineer, Data Scientist, NLP Engineer,
Computer Vision Engineer, MLOps, LLM Engineer, Deep Learning, AI Research,
Data Engineer (AI-focused), or similar AI/ML technical roles.

Consider NO for: general software engineering, web dev, front-end, QA,
sales, marketing, operations, finance, admin, or non-technical roles.

Job Title: {title}
Company: {company}

Job Description (excerpt):
{description}

Reply with exactly one word: YES or NO"""


_NORMALIZE_PROMPT = """\
Extract clean structured data from this raw job posting info.
Return ONLY valid JSON with these exact keys: role, company, location, pay_range.

Rules:
- role: clean job title only. Remove location info, work mode (WFH/onsite/hybrid), \
company name, parenthetical codes, and trailing suffixes. Example: \
"AI Automation Specialist (dbb) - Permanent WFH/Morning" → "AI Automation Specialist"
- company: company name only, no extra text
- location: city and country, or "Remote", or "Hybrid - <city>". Extract from any field.
- pay_range: salary/pay range if found anywhere (title, location, pay, description). \
Include currency and period (e.g. "PHP 50,000–80,000/month"). Empty string if not found.

Raw title: {raw_title}
Raw company: {raw_company}
Raw location: {raw_location}
Raw pay: {raw_pay}
Description excerpt: {desc_excerpt}

JSON:"""


_ASSESS_PROMPT = """\
You are a career advisor assessing how well a candidate matches a job posting.

Rate the fit as STRONG, MODERATE, or WEAK:
  STRONG   — Meets most requirements; skills and experience clearly match.
  MODERATE — Meets some requirements; relevant experience but notable gaps.
  WEAK     — Significant gaps in required skills or experience level.

Extract 5-8 key requirements from the job description and check each against the resume.

Return ONLY valid JSON with this exact structure:
{{
  "rating": "STRONG|MODERATE|WEAK",
  "match_rows": [
    {{"requirement": "...", "my_resume": "...", "fit": "MATCH|PARTIAL|GAP"}}
  ],
  "summary": "One sentence: overall fit recommendation."
}}

fit values:
  MATCH   — resume clearly satisfies this requirement
  PARTIAL — resume partially meets it or has related experience
  GAP     — requirement not met by resume

---
JOB TITLE: {title}
COMPANY: {company}

JOB DESCRIPTION:
{description}

---
CANDIDATE RESUME:
{resume}
---"""


_SUMMARY_PROMPT = """\
Write a 2-3 sentence summary of this job posting.
Cover: role responsibilities, required skills, and any notable perks or conditions.
Be concise and factual.

Job Title: {title}
Company: {company}

Description:
{description}

Summary:"""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

async def _is_ai_ml(llm, job: JobData) -> bool:
    description_excerpt = job.description[:1500] if job.description else "(no description available)"
    resp = await llm.ainvoke([HumanMessage(content=_IS_AI_ML_PROMPT.format(
        title=job.title,
        company=job.company,
        description=description_excerpt,
    ))])
    return resp.content.strip().upper().startswith("YES")


async def _normalize_fields(llm, job: JobData) -> tuple[str, str, str]:
    """Returns (normalized_role, normalized_pay, normalized_location) extracted by LLM."""
    desc_excerpt = job.description[:500] if job.description else ""
    resp = await llm.ainvoke([HumanMessage(content=_NORMALIZE_PROMPT.format(
        raw_title=job.title,
        raw_company=job.company,
        raw_location=job.location,
        raw_pay=job.pay,
        desc_excerpt=desc_excerpt,
    ))])
    try:
        # Strip markdown fences if present
        text = resp.content.strip().strip("```json").strip("```").strip()
        data = json.loads(text)
        return data.get("role", ""), data.get("pay_range", ""), data.get("location", "")
    except Exception:
        return "", "", ""


async def _assess(llm, job: JobData, resume: str) -> tuple[str, str, list[dict]]:
    """Returns (strength, summary, match_breakdown)."""
    description_excerpt = job.description[:3000] if job.description else "(no description available)"
    resume_excerpt = resume[:3000]

    resp = await llm.ainvoke([HumanMessage(content=_ASSESS_PROMPT.format(
        title=job.title,
        company=job.company,
        description=description_excerpt,
        resume=resume_excerpt,
    ))])

    text = resp.content.strip()

    # Try JSON parse first
    try:
        json_text = text.strip("```json").strip("```").strip()
        data = json.loads(json_text)
        strength = data.get("rating", "MODERATE").upper()
        if strength not in ("WEAK", "MODERATE", "STRONG"):
            strength = "MODERATE"
        summary = data.get("summary", "")
        match_breakdown = data.get("match_rows", [])
        return strength, summary, match_breakdown
    except Exception:
        pass

    # Fallback: old line-based parse
    strength = "MODERATE"
    explanation = text
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("RATING:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("WEAK", "MODERATE", "STRONG"):
                strength = val
        elif line.upper().startswith("EXPLANATION:"):
            explanation = line.split(":", 1)[1].strip()
    return strength, explanation, []


async def _summarise(llm, job: JobData) -> str:
    description_excerpt = job.description[:2000] if job.description else "(no description available)"
    resp = await llm.ainvoke([HumanMessage(content=_SUMMARY_PROMPT.format(
        title=job.title,
        company=job.company,
        description=description_excerpt,
    ))])
    return resp.content.strip()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def job_screener_node(state: dict) -> dict:
    """
    LangGraph node.

    Reads raw_job_listings from state, screens and assesses each job,
    returns assessed_jobs list.
    """
    raw_listings: dict[str, list[JobData]] = state.get("raw_job_listings", {})

    if not raw_listings:
        print("[job_screener] No job listings to screen.")
        return {"assessed_jobs": []}

    llm = get_llm(temperature=0.0)
    resume_text = _load_resume()

    today = now_pht().strftime("%Y-%m-%d")
    assessed: list[AssessedJob] = []

    for site, jobs in raw_listings.items():
        print(f"[job_screener] Screening {len(jobs)} job(s) from {site}.")

        for job in jobs:
            # Skip fully blocked jobs with no usable data
            if job.blocked and not job.title and not job.company:
                print(f"[job_screener] Skip blocked/empty: {job.url}")
                continue

            # Step 1 — AI/ML relevance check
            ai_ml = await _is_ai_ml(llm, job)
            if not ai_ml:
                print(f"[job_screener] NOT AI/ML — skip: {job.title!r} @ {job.company}")
                continue

            # Step 2, 3, 4 — normalize + assess + summarise in parallel
            normalize_task = asyncio.create_task(_normalize_fields(llm, job))
            strength_task  = asyncio.create_task(_assess(llm, job, resume_text))
            summary_task   = asyncio.create_task(_summarise(llm, job))
            normalized_role, normalized_pay, normalized_location = await normalize_task
            strength, explanation, match_breakdown = await strength_task
            summary = await summary_task

            assessed_job = AssessedJob(
                site=job.site,
                url=job.url,
                title=job.title,
                company=job.company,
                location=job.location,
                pay=job.pay,
                description=job.description,
                scrape_source=job.source,
                normalized_role=normalized_role,
                normalized_pay=normalized_pay,
                normalized_location=normalized_location,
                is_ai_ml=True,
                description_summary=summary,
                resume_strength=strength,
                strength_explanation=explanation,
                match_breakdown=match_breakdown,
                date_added=today,
            )
            assessed.append(assessed_job)

            print(
                f"[job_screener] {strength:8s} | {job.title!r} @ {job.company} "
                f"[{job.source}]"
            )

            # Small courtesy delay between LLM calls
            await asyncio.sleep(0.5)

    print(f"[job_screener] Done — {len(assessed)} AI/ML job(s) assessed.")
    return {"assessed_jobs": assessed}

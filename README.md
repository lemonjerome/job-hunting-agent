# Job Hunting Agent

An automated AI/ML job hunting agent that runs 3× daily on GitHub Actions. It monitors job alert emails from LinkedIn, Jobstreet, Glassdoor, and Indeed — extracts direct job posting URLs, visits each page with a stealth browser, assesses fit against your resume, logs results to a Google Sheet, and emails you when a strong match is found.

> Built for the Philippines job market. Focuses exclusively on AI, ML, and Data Science roles.

---

## Features

- **Email screening** — reads Gmail for job alert emails, skips already-processed ones, and uses an LLM to verify the email is actually an AI/ML job alert (filters out general recommendations and upsells)
- **Smart URL extraction** — parses direct job posting URLs from email HTML; handles click-tracking redirects (e.g. `e.jobstreet.com`) and multiple domain formats
- **Glassdoor email fallback** — when Glassdoor's job page blocks scraping, job data (title, company, location, salary, rating) is extracted directly from the email card instead
- **Stealth browser scraping** — visits each job URL using Playwright with `playwright-stealth` (spoofed Mac user-agent, real viewport, human-like scroll timing)
- **Resume matching** — compares each job description against your resume; rates fit as `WEAK`, `MODERATE`, or `STRONG` with a 2-3 sentence explanation
- **Google Sheets logging** — deduplicates and appends new jobs to a `Jobs` tab; tracks processed emails in an `Emails Seen` tab; logs resume versions in a `Resume Versions` tab
- **Email notification** — sends a self-notification email listing all newly found `STRONG` matches
- **Scheduled runs** — runs automatically at 6am, 2pm, and 10pm PHT via GitHub Actions cron

---

## How It Works

```
GitHub Actions (cron: 6am / 2pm / 10pm PHT)
        │
        ▼
 email_screener          ← reads Gmail, LLM filters AI/ML emails, extracts job URLs
        │
        ├─ no AI/ML emails ──► END
        │
        ▼
 scrape_site (parallel)  ← one Playwright scraper per site (LinkedIn / Jobstreet / Glassdoor / Indeed)
        │
        ▼
 job_screener            ← LLM confirms AI/ML relevance, rates resume fit, summarises job
        │
        ├─ no jobs passed ──► END
        │
        ▼
 sheets_updater          ← deduplicates against existing sheet rows, appends new jobs
        │
        ├─ no STRONG matches ──► END
        │
        ▼
 email_notifier          ← sends Gmail notification with STRONG match listings
        │
        ▼
        END
```

Each step is a LangGraph node sharing a single typed state object. Scraper nodes run in parallel using LangGraph's `Send()` API and merge results via a custom reducer.

---

## Tech Stack

| Layer | Tool |
|-------|------|
| Orchestration | [LangGraph](https://github.com/langchain-ai/langgraph) `StateGraph` with `Send()` fan-out |
| LLM | [minimax-m2.7](https://ollama.com) via Ollama Cloud API |
| LangChain binding | `langchain-ollama` (`ChatOllama`) |
| Browser automation | [Playwright](https://playwright.dev) + [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) (headless Chromium) |
| Email (read/send) | Gmail API (`google-api-python-client`) |
| Storage | Google Drive + Sheets API (`google-api-python-client`) |
| HTML parsing | BeautifulSoup4 |
| PDF extraction | pypdf2 |
| Scheduling | GitHub Actions cron |
| Runtime secrets | GitHub Actions Secrets (base64-encoded credential files) |

### LLM

**Model:** `minimax-m2.7` served via [Ollama Cloud](https://ollama.com)

Used for:
- Classifying whether an email is an AI/ML job alert
- Confirming whether a scraped job is AI/ML related
- Assessing resume fit (WEAK / MODERATE / STRONG)
- Summarising job descriptions
- Summarising resume versions

---

## Setup Guide

### Prerequisites

- Python 3.11+
- A [Google Cloud Console](https://console.cloud.google.com) account
- An [Ollama Cloud](https://ollama.com) account (free tier works)
- A Gmail account where you receive job alerts
- Your resume PDF uploaded to Google Drive in a folder called `Job Hunting`

---

### Step 1 — Clone and install

```bash
git clone https://github.com/lemonjerome/job-hunting-agent.git
cd job-hunting-agent
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

---

### Step 2 — Create Google OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a new project (e.g. `job-hunting-agent`)
2. Enable these APIs:
   - **Gmail API**
   - **Google Drive API**
   - **Google Sheets API**
3. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop app**
   - Download the JSON file — it will be named something like `client_secret_272268...apps.json`
4. Rename it to match the pattern in `.env` or note the exact filename

---

### Step 3 — Configure environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_API_KEY=your-key-from-ollama.com
OLLAMA_MODEL=minimax-m2.7

GMAIL_CREDENTIALS=client_secret_YOUR_FILE.json
GDRIVE_CREDENTIALS=client_secret_YOUR_FILE.json

SELF_EMAIL=you@gmail.com
```

Your Ollama API key is in your [Ollama account settings](https://ollama.com).

---

### Step 4 — Authenticate with Google (first run only)

Run this once to generate the OAuth token files. A browser window will open asking you to log in and grant permissions.

```bash
# Authenticate Gmail (generates .gmail_token.json)
python -c "
from tools.gmail_tools import _get_gmail_service
_get_gmail_service()
print('Gmail auth OK')
"

# Authenticate Google Drive/Sheets (generates .gdrive_token.json)
python -c "
from tools.sheets_tools import _get_sheets_service
_get_sheets_service()
print('Drive auth OK')
"
```

After running both, you'll have `.gmail_token.json` and `.gdrive_token.json` in your project root. These are gitignored — never commit them.

---

### Step 5 — Upload your resume

Upload your resume PDF to Google Drive in a folder called **`Job Hunting`**. The filename can be anything — you'll set it as an env var.

Set `RESUME_FILENAME` in your `.env` to match the exact filename (case-sensitive):

```env
RESUME_FILENAME=Your_Name_Resume.pdf
```

Version tracking is automatic — every time the agent runs, it checks whether the GDrive PDF has been modified since the last logged version and logs a new entry if so. No manual script needed.

---

### Step 6 — Test locally

```bash
python main.py
```

Expected output on a run with no new job emails:

```
[email_screener] No new job alert emails found.
Run complete.
  Assessed jobs : 0
  New in sheet  : 0
```

On a run with matching emails:

```
[email_screener] glassdoor | YES | 3 URL(s) | 'New AI jobs matching your search'
[email_screener] jobstreet | YES | 2 URL(s) | 'New jobs for you'
[job_screener] STRONG   | 'ML Engineer' @ TechCorp [scraped]
...
```

You can also run a quick LLM connectivity check without triggering the full graph:

```bash
python main.py --smoke-test
```

---

### Step 7 — Set GitHub Secrets

Before pushing, add these 6 secrets to your GitHub repo under **Settings → Secrets and variables → Actions**:

| Secret name | How to get the value |
|-------------|----------------------|
| `GMAIL_CREDENTIALS_JSON` | `base64 -i client_secret_*.json \| tr -d '\n'` |
| `GDRIVE_TOKEN_JSON` | `base64 -i .gdrive_token.json \| tr -d '\n'` |
| `GMAIL_TOKEN_JSON` | `base64 -i .gmail_token.json \| tr -d '\n'` |
| `OLLAMA_BASE_URL` | `https://ollama.com` |
| `OLLAMA_API_KEY` | your Ollama Cloud API key |
| `SELF_EMAIL` | your Gmail address |
| `RESUME_FILENAME` | exact PDF filename in your Drive (e.g. `John_Doe_Resume.pdf`) |

The first three commands run in your terminal from the project root. Copy the full output (including any trailing `=`) into the secret value field.

---

### Step 8 — Push to GitHub

```bash
git push -u origin main
```

---

### Step 9 — Verify GitHub Actions

1. Go to your repo on GitHub → **Actions** tab
2. You should see the **Job Hunting Agent** workflow listed
3. Trigger a manual test run: click the workflow → **Run workflow** → **Run workflow**
4. Watch the logs — look for `[email_screener]` output in the `Run Job Hunting Agent` step

The workflow will also run automatically on the cron schedule (6am / 2pm / 10pm PHT).

---

## Google Sheets Structure

The script creates and manages a file called `Job Huntings` in your `Job Hunting` Drive folder.

**Jobs tab** — one row per assessed job:
`Job Role | Company | Description Summary | Site | URL | Resume Strength | Explanation | Pay | Date Added | Status`

**Emails Seen tab** — audit log of every processed email:
`Gmail Message ID | Site | Sender | Subject | Time Received | Is AI/ML Alert | Jobs Extracted | Summary | Processed At`

**Resume Versions tab** — tracks resume file changes:
`Version | Filename | GDrive File ID | File Size | Created At | Modified At | Detected At | Short Summary`

---

## Privacy

This is a public repo. The following files are gitignored and never committed:

- `.env` — API keys and email address
- `client_secret_*.json` — Google OAuth credentials
- `.gdrive_token.json` / `.gmail_token.json` — OAuth tokens
- `resume.md` — local resume copy (resume PDF stays in Google Drive only)

In GitHub Actions, credentials are written from base64-encoded secrets at runtime and deleted immediately after the run completes.

---

## Adapting for Yourself

To use this for your own job search:

1. Set `RESUME_FILENAME` in `.env` (and as a GitHub Secret) to your PDF filename
2. Optionally update `EMAIL_SENDERS` in `config.py` if you use different job alert senders
3. Optionally adjust `EMAIL_LOOKBACK_HOURS` (default: 8h, matches the 3×/day schedule)
4. Upload your resume PDF to Google Drive in a `Job Hunting` folder
5. Follow the setup guide above


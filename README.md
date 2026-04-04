# Job Hunting Agent

An event-driven AI/ML job hunting agent that runs automatically whenever a job alert email hits your inbox. It monitors emails from LinkedIn, Jobstreet, Glassdoor, and Indeed — extracts direct job posting URLs, visits each page with a stealth browser, assesses your resume fit using an LLM, logs everything to a Google Sheet, and sends you a comparison table email for strong matches.

> Built for the Philippines job market. Focuses exclusively on AI, ML, and Data Science roles.

---

## How It Works

```
Your Gmail inbox
       │
       │  Job alert email arrives
       ▼
  Gmail Watch API  ──►  Cloud Pub/Sub  ──►  Cloud Function (gmail-trigger)
                                                     │
                                                     │  POST /process
                                                     ▼
                                            Cloud Run (job-agent)
                                                     │
                                    ┌────────────────▼────────────────┐
                                    │         LangGraph Graph          │
                                    │                                  │
                                    │  email_screener                  │
                                    │    ├─ LLM: is this AI/ML?        │
                                    │    ├─ parse job cards (4 sites)  │
                                    │    └─ resolve tracking URLs      │
                                    │           │                      │
                                    │    ┌──────┴──────┐               │
                                    │    ▼  Send() API ▼               │
                                    │  scrape_linkedin  scrape_indeed  │
                                    │  scrape_jobstreet scrape_glassdoor│
                                    │    └──────┬──────┘               │
                                    │           ▼                      │
                                    │  job_screener                    │
                                    │    ├─ LLM: confirm AI/ML         │
                                    │    ├─ LLM: normalize role/pay    │
                                    │    ├─ LLM: assess resume fit      │
                                    │    └─ structured match breakdown  │
                                    │           │                      │
                                    │           ▼                      │
                                    │  sheets_updater                  │
                                    │    └─ deduplicate + append        │
                                    │           │                      │
                                    │           ▼                      │
                                    │  email_notifier                  │
                                    │    └─ send comparison table      │
                                    └──────────────────────────────────┘
                                                     │
                                                     ▼
                                          Google Sheet (Job Hunting)
                                          + Notification email to you
```

Each step is a LangGraph node sharing a typed state object. The four site scrapers run in **parallel** using LangGraph's `Send()` fan-out API and their results are merged by a custom reducer.

---

## Features

### Email Screening
Reads incoming job alert emails from LinkedIn, Jobstreet, Glassdoor, and Indeed. Skips any email already logged in the `Emails Seen` sheet tab to avoid double-processing. Uses the LLM to verify the email is actually an AI/ML job alert — filters out "LiNa" recommendations, premium upsells, profile tips, and unrelated roles. When card titles are available (extracted from the email HTML), the LLM verdict is based on the actual job titles rather than the raw email body — more accurate and immune to HTML noise.

### Smart URL Extraction (All 4 Sites)
Parses job card HTML from emails to extract direct job posting URLs and card context (title, company, location, pay). Each site uses a different strategy:

- **LinkedIn** — anchors on `/jobs/view/{id}` links, strips tracking query params to deduplicate the 3 anchors per job card, upgrades empty-title entries
- **Jobstreet** — resolves `url.jobstreet.com` SendGrid tracking redirects via HTTP to get canonical `ph.jobstreet.com/job/{id}` URLs
- **Glassdoor** — splits the concatenated link text (`"Company4.2★Title$pay"`) on the rating regex to extract clean company and title fields
- **Indeed** — handles `cts.indeed.com/v3/` single-job email format (extracts title from `<h1>`) and `pagead/clk` multi-job tracking URLs; falls back to plain-text email parser for text-only emails

### Stealth Browser Scraping
Visits each job URL using headless Playwright with playwright-stealth: spoofed Mac user-agent, real 1440×900 viewport, randomised human-like scroll timing. Uses guest/public API endpoints where available (LinkedIn guest API, Jobstreet GraphQL) to avoid login walls.

### LLM-Assisted Page Extraction
When a page loads but CSS selectors return empty fields (class names change frequently on LinkedIn, Indeed, and Glassdoor), the raw page text is passed to the LLM with a structured extraction prompt. The LLM extracts title, company, location, pay, and description — and also signals if the page is a login wall or CAPTCHA. This makes scraping resilient to site layout changes without any code changes.

### Email Card Fallback + Description Snippet
When scraping is blocked or returns empty data, the agent falls back to the card context parsed directly from the email HTML (title, company, location, pay, rating). Indeed and Glassdoor emails also include a 1–2 sentence job description preview inside each card — this snippet is extracted and used as the description, giving the LLM assessment real content even without visiting the job page.

For LinkedIn, Indeed, and Glassdoor jobs that reach the sheet via email fallback with no description, JobSpy (`python-jobspy`) searches for the job by title to retrieve the full description.

### LLM Field Normalization (Role, Pay, Location)
Raw scraped titles are often noisy: `"Urgently hiringSenior AI Engineer (AI)Rivington PartnersMandaluyong City..."`. An LLM normalization step reasons over all raw fields (title, company, location, pay, description excerpt) and produces clean `normalized_role`, `normalized_pay`, and `normalized_location` — no site-specific string parsing. The LLM handles all 4 sites identically regardless of email format changes.

### Resume Fit Assessment
Compares each job description against your resume. The LLM returns structured JSON with:
- `rating` — STRONG / MODERATE / WEAK
- `match_rows` — 5–8 key JD requirements, each with your matching resume experience and a MATCH / PARTIAL / GAP verdict
- `summary` — one sentence overall recommendation

### Comparison Table Email
For STRONG matches, sends an HTML email with a color-coded comparison table:

| JD Requirement | My Resume | Fit |
|----------------|-----------|-----|
| LLM experience | Built RAG pipelines with LangGraph | ✓ |
| Python 5yr+ | 4 years Python, ML projects | ~ |
| AWS required | No AWS experience | ✗ |

Green = MATCH, orange = PARTIAL, red = GAP.

### Google Sheets Logging
Automatically creates and manages a `Job Hunting` spreadsheet with 3 tabs:
- **Jobs** — one row per assessed job: normalized role, company, location, pay, fit rating, match breakdown, URL. All dates and timestamps are in Philippine Time (PHT, UTC+8).
- **Emails Seen** — audit log of every processed email (deduplication key)
- **Resume Versions** — tracks resume PDF changes in GDrive with LLM-generated summaries

The sheet header is updated automatically on every write run — adding new columns (like Location) to existing sheets without shifting existing data.

### Event-Driven Architecture
Gmail Watch API publishes inbox events to a Cloud Pub/Sub topic in real time. A Cloud Run Function receives the Pub/Sub push, fetches the email, and posts it to the Cloud Run agent. No polling, no cron delays — jobs are processed within seconds of the email arriving.

The last processed Gmail `historyId` is persisted in Secret Manager after every invocation. This ensures the Cloud Function always knows where to resume — even after a cold start — so no emails are missed when the function scales to zero between job alert batches.

---

## Tech Stack

| Layer | Tool |
|-------|------|
| Orchestration | [LangGraph](https://github.com/langchain-ai/langgraph) `StateGraph` with `Send()` fan-out |
| LLM | `minimax-m2.7` via [Ollama Cloud API](https://ollama.com) |
| Browser automation | [Playwright](https://playwright.dev) + [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) |
| Job description enrichment | [JobSpy](https://github.com/speedyapply/JobSpy) (`python-jobspy`) — searches LinkedIn/Indeed/Glassdoor for descriptions when individual page scraping is blocked |
| Email + Drive | Gmail API + Google Drive API + Sheets API (`google-api-python-client`) |
| HTML parsing | BeautifulSoup4 |
| Agent server | FastAPI + Uvicorn |
| Deployment | Google Cloud Run (Docker) + Cloud Run Functions |
| Push notifications | Gmail Watch API → Cloud Pub/Sub |
| Watch renewal | Cloud Scheduler (daily, midnight PHT) |
| CI/CD | GitHub Actions (push to `main` → build + deploy) |
| Secrets | Google Secret Manager |

---

## Setup Guide

### What You Need Before Starting

- A **Gmail account** where you receive job alert emails from LinkedIn, Jobstreet, Glassdoor, and Indeed
- A **Google Cloud account** (the same Gmail account works — free tier is sufficient)
- An **Ollama Cloud account** at [ollama.com](https://ollama.com) (free tier works)
- Your **resume PDF** ready to upload to Google Drive
- **Python 3.11+** and **Git** installed locally
- **gcloud CLI** installed ([install guide](https://cloud.google.com/sdk/docs/install))

---

### Part 1 — Local Setup

#### Step 1 — Clone and install

```bash
git clone https://github.com/lemonjerome/job-hunting-agent.git
cd job-hunting-agent
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

#### Step 2 — Create your Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project dropdown at the top → **New Project**
3. Name it `job-hunting-agent` → **Create**
4. Wait ~30 seconds, then select the new project from the dropdown

#### Step 3 — Enable APIs

In your new project, go to **APIs & Services → Enable APIs and Services** and enable each of these (search by name, click, then click **Enable**):

- Gmail API
- Google Drive API
- Google Sheets API

#### Step 4 — Create OAuth credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. If prompted to configure the consent screen:
   - Click **Configure Consent Screen** → choose **External** → **Create**
   - Fill in App name (e.g. `Job Hunting Agent`), your email for support and developer contact → **Save and Continue** → **Save and Continue** → **Save and Continue** → **Back to Dashboard**
4. Back on Credentials, click **+ Create Credentials → OAuth client ID** again
   - Application type: **Desktop app**
   - Name: `Job Hunting Agent`
   - Click **Create**
5. Click **Download JSON** — save the file inside your project folder (the filename will look like `client_secret_272268....json`)
6. Add yourself as a test user: go to **OAuth consent screen → Test users → + Add users** → add your Gmail address

#### Step 5 — Configure your environment

Create a `.env` file in the project root:

```bash
cp .env.example .env   # if .env.example exists, else create manually
```

Edit `.env` with these values:

```env
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_API_KEY=your-key-here
OLLAMA_MODEL=minimax-m2.7

GMAIL_CREDENTIALS=client_secret_YOUR_EXACT_FILENAME.json
GDRIVE_CREDENTIALS=client_secret_YOUR_EXACT_FILENAME.json

SELF_EMAIL=youremail@gmail.com
RESUME_FILENAME=Your_Resume_Filename.pdf
```

Your Ollama API key: log in at [ollama.com](https://ollama.com) → click your profile → **API Keys** → copy the key.

#### Step 6 — Authenticate with Google (browser pop-up, one time only)

```bash
# Generates .gmail_token.json — a browser window will open, log in and click Allow
python -c "from tools.gmail_tools import _get_gmail_service; _get_gmail_service(); print('Gmail OK')"

# Generates .gdrive_token.json — same browser flow
python -c "from tools.sheets_tools import _get_credentials; _get_credentials(); print('Drive OK')"
```

Both token files are gitignored. Never commit them.

#### Step 7 — Upload your resume to Google Drive

1. Go to [drive.google.com](https://drive.google.com)
2. Create a folder called exactly **`Job Hunting`** (case sensitive)
3. Upload your resume PDF into that folder
4. Make sure `RESUME_FILENAME` in `.env` exactly matches the PDF filename

#### Step 8 — Test locally

```bash
python main.py
```

Expected output when no new emails:
```
[email_screener] No new job alert emails found.
Run complete. Assessed jobs: 0 | New in sheet: 0
```

To test a specific site's email parsing without writing to the sheet:
```bash
python scripts/test_site.py --site linkedin --skip-scrape
python scripts/test_site.py --site all --max-urls 2
```

---

### Part 2 — Google Cloud Deployment

#### Step 9 — Install and authenticate gcloud

```bash
# Install: https://cloud.google.com/sdk/docs/install
gcloud auth login                    # log in with your Gmail account
gcloud auth application-default login  # sets ADC credentials for Python libraries
gcloud config set project YOUR_PROJECT_ID
```

To find your project ID: [console.cloud.google.com](https://console.cloud.google.com) → project dropdown → your project → the ID is shown below the name.

#### Step 10 — Run the GCP setup script

```bash
export GCP_PROJECT=your-project-id
bash scripts/gcloud_setup.sh
```

This script will:
- Enable all required GCP APIs (Cloud Run, Cloud Functions, Pub/Sub, Secret Manager, Artifact Registry, Cloud Scheduler)
- Create an Artifact Registry Docker repository
- Create the `gmail-job-alerts` Pub/Sub topic and grant Gmail push permissions
- Create the `job-agent-runner` service account with required IAM roles
- Prompt you to enter your secrets (Ollama key, email, resume filename)
- Store your OAuth credential files in Secret Manager
- Register the Gmail Watch

#### Step 11 — Create a GitHub Actions service account key

```bash
gcloud iam service-accounts keys create /tmp/gcp-sa-key.json \
  --iam-account="job-agent-runner@YOUR_PROJECT_ID.iam.gserviceaccount.com"

# Print the base64 value to copy into GitHub
cat /tmp/gcp-sa-key.json | base64 | tr -d '\n'
```

#### Step 12 — Add GitHub Secrets

Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

Add these two secrets:

| Secret name | Value |
|-------------|-------|
| `GCP_SA_KEY` | The base64 output from Step 11 |
| `GCP_PROJECT` | Your GCP project ID (e.g. `job-hunting-agent-491709`) |

#### Step 13 — Push to trigger deployment

```bash
git push origin main
```

Go to **Actions** tab on GitHub and watch the 4-job pipeline:
1. **Build & Push** — builds the Docker image and pushes to Artifact Registry (~8 min first time)
2. **Deploy Cloud Run** — deploys the agent server
3. **Deploy Cloud Function** — deploys the Gmail trigger and wires the Pub/Sub subscription
4. **Summary** — prints both service URLs

#### Step 14 — Register Gmail Watch

After the pipeline succeeds, run this locally once:

```bash
GCP_PROJECT=your-project-id python scripts/setup_gmail_watch.py
```

This tells Gmail to push inbox notifications to your Pub/Sub topic. The watch expires every 7 days — Cloud Scheduler renews it automatically at midnight PHT.

#### Step 15 — Verify the deployment

```bash
# Get your Cloud Run URL
CLOUD_RUN_URL=$(gcloud run services describe job-agent \
  --region us-central1 --format "value(status.url)")

# Health check
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$CLOUD_RUN_URL/health"
# Expected: {"status":"ok"}
```

The system is now live. The next job alert email that hits your inbox will automatically trigger the full pipeline.

---

## Privacy

This is a public repo. The following files are gitignored and never committed:

- `.env` — API keys and personal config
- `client_secret_*.json` — Google OAuth credentials
- `.gdrive_token.json` / `.gmail_token.json` — OAuth tokens
- `resume.md` — local resume text copy (PDF lives in GDrive only)
- `PROJECT_SUMMARY.md` — personal technical notes

Secrets on Cloud Run are loaded from Google Secret Manager at runtime. Nothing sensitive is baked into the Docker image.

---

## Adapting for Yourself

1. Update `RESUME_FILENAME` in `.env` and in Secret Manager to your PDF filename
2. Optionally update `EMAIL_SENDERS` in `config.py` for different job alert senders
3. Optionally adjust `EMAIL_LOOKBACK_HOURS` in `config.py` (default: 8h)
4. Update the LLM prompts in `agents/email_screener.py` and `agents/job_screener.py` to match your target roles
5. Upload your resume PDF to GDrive in a folder named `Job Hunting`

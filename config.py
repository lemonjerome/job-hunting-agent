import os
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv()

# --- Ollama Cloud ---
OLLAMA_BASE_URL: str = os.environ["OLLAMA_BASE_URL"]
OLLAMA_API_KEY: str = os.environ["OLLAMA_API_KEY"]
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "minimax-m2.7")


def get_llm(temperature: float = 0.0) -> ChatOllama:
    """Return a configured ChatOllama instance pointing at the Ollama Cloud API."""
    return ChatOllama(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_MODEL,
        api_key=OLLAMA_API_KEY,
        temperature=temperature,
    )


# --- Gmail MCP ---
GMAIL_CREDENTIALS: str = os.environ["GMAIL_CREDENTIALS"]

# --- GDrive / Sheets MCP ---
GDRIVE_CREDENTIALS: str = os.environ["GDRIVE_CREDENTIALS"]

# --- Notifications ---
SELF_EMAIL: str = os.environ["SELF_EMAIL"]

# --- GDrive targets ---
GDRIVE_FOLDER: str = "Job Application"
GSHEET_FILE_NAME: str = "Job Applications"

# --- GSheet tab names ---
SHEET_JOBS: str = "Jobs"
SHEET_EMAILS: str = "Emails Seen"
SHEET_RESUME: str = "Resume Versions"

# --- Resume ---
RESUME_FILENAME: str = os.environ["RESUME_FILENAME"]

# --- Email senders to monitor ---
EMAIL_SENDERS: dict[str, str] = {
    "linkedin":   "jobalerts-noreply@linkedin.com",
    "jobstreet":  "noreply@e.jobstreet.com",
    "glassdoor":  "noreply@glassdoor.com",
    "indeed":     "donotreply@match.indeed.com",
}

# --- Email screening window (hours) ---
# Runs at 6am / 2pm / 10pm — look back 8h to avoid gaps
EMAIL_LOOKBACK_HOURS: int = 8

# --- Scraping ---
STEALTH_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
STEALTH_VIEWPORT: dict = {"width": 1440, "height": 900}

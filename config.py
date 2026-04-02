import os
from dotenv import load_dotenv
from langchain_ollama import ChatOllama

load_dotenv()

# --- Cloud Run detection ---
# When running on Cloud Run, credentials are fetched from Secret Manager at runtime.
# Locally, they are read from files referenced by env vars.
GCP_PROJECT: str = os.getenv("GCP_PROJECT", "")
GMAIL_TOKEN_SECRET: str = os.getenv("GMAIL_TOKEN_SECRET", "")  # Secret Manager secret name
IS_CLOUD_RUN: bool = bool(os.getenv("K_SERVICE"))  # Cloud Run sets K_SERVICE automatically


def get_secret(secret_name: str) -> str:
    """Fetch a secret value from Secret Manager (used on Cloud Run)."""
    from google.cloud import secretmanager
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{GCP_PROJECT}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


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


# --- Gmail / GDrive credentials ---
# On Cloud Run: token is loaded from Secret Manager at runtime (no local file needed).
# Locally: use the file path from env vars.
GMAIL_CREDENTIALS: str = os.getenv("GMAIL_CREDENTIALS", "")
GDRIVE_CREDENTIALS: str = os.getenv("GDRIVE_CREDENTIALS", "")

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

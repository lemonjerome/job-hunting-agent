import os
from dotenv import load_dotenv

load_dotenv()

# --- Ollama Cloud ---
OLLAMA_BASE_URL: str = os.environ["OLLAMA_BASE_URL"]
OLLAMA_API_KEY: str = os.environ["OLLAMA_API_KEY"]
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "minimax-m2.7")

# --- Gmail MCP ---
GMAIL_CREDENTIALS: str = os.environ["GMAIL_CREDENTIALS"]

# --- GDrive / Sheets MCP ---
GDRIVE_CREDENTIALS: str = os.environ["GDRIVE_CREDENTIALS"]

# --- Notifications ---
SELF_EMAIL: str = os.environ["SELF_EMAIL"]

# --- GDrive targets ---
GDRIVE_FOLDER: str = "Job Application"
SHEET_NAME: str = "Job Applications"

# --- Search configs: (keyword, location) ---
SEARCH_CONFIGS: list[tuple[str, str]] = [
    ("AI", "Philippines"),
    ("ML", "Philippines"),
    ("Artificial Intelligence", "Philippines"),
    ("Machine Learning", "Philippines"),
]

# --- Scraping ---
JOBS_PER_CONFIG: int = 10
STEALTH_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
STEALTH_VIEWPORT: dict = {"width": 1440, "height": 900}

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


# Telegram
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_AUTHORIZED_USER_ID = int(_require("TELEGRAM_AUTHORIZED_USER_ID"))

# LLM
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Security
SESSION_PIN = os.getenv("SESSION_PIN", "")
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))

# Paths
DB_PATH = Path(__file__).parent / "clawdbot.db"

# Default LLM
DEFAULT_LLM = "claude"

# Google Places API (for review monitoring)
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# Review polling interval in minutes
REVIEW_POLL_INTERVAL_MINUTES = int(os.getenv("REVIEW_POLL_INTERVAL_MINUTES", "15"))

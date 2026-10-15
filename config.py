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

# LLM (OpenAI only)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")

# Semantic search embeddings
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "512"))

# Voice note transcription
VOICE_MODEL = os.getenv("VOICE_MODEL", "gpt-4o-mini-transcribe")

# Morning digest (local hour; set DIGEST_ENABLED=0 to turn off)
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "1") not in ("0", "false", "no")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))

# How deep the Gmail backfill goes per account (index the whole recent mailbox)
BACKFILL_MAX_EMAILS = int(os.getenv("BACKFILL_MAX_EMAILS", "2000"))

# Max output tokens per LLM response
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8192"))

# Approximate token budget for conversation history sent to the LLM
HISTORY_TOKEN_BUDGET = int(os.getenv("HISTORY_TOKEN_BUDGET", "30000"))

# Start a fresh conversation after this many minutes of inactivity
SESSION_IDLE_RESET_MINUTES = int(os.getenv("SESSION_IDLE_RESET_MINUTES", "120"))

# User identity for the system prompt (optional)
USER_NAME = os.getenv("USER_NAME", "")

# Security
SESSION_PIN = os.getenv("SESSION_PIN", "")
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))

# Paths
DB_PATH = Path(__file__).parent / "clawdbot.db"

# Google Places API (for review monitoring)
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# Review polling interval in minutes
REVIEW_POLL_INTERVAL_MINUTES = int(os.getenv("REVIEW_POLL_INTERVAL_MINUTES", "15"))

# OneDrive (personal Microsoft account)
ONEDRIVE_CLIENT_ID = os.getenv("ONEDRIVE_CLIENT_ID", "")

# Search index polling interval in minutes
INDEX_POLL_INTERVAL_MINUTES = int(os.getenv("INDEX_POLL_INTERVAL_MINUTES", "10"))

# Default Gmail account label (set on startup if token exists)
DEFAULT_GMAIL_ACCOUNT = os.getenv("DEFAULT_GMAIL_ACCOUNT", "personal")

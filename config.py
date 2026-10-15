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


def _parse_user_ids(raw: str) -> list[int]:
    """'894…' or '894…,930243154' → [894…, 930243154]. Blank/invalid entries are rejected."""
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RuntimeError(f"TELEGRAM_AUTHORIZED_USER_ID: '{part}' is not a numeric Telegram id")
        if int(part) not in ids:
            ids.append(int(part))
    if not ids:
        raise RuntimeError("TELEGRAM_AUTHORIZED_USER_ID must list at least one Telegram user id")
    return ids


# Everyone who may talk to the bot (comma-separated in .env). The FIRST id is the owner:
# proactive messages (digest, alerts, reminders, startup ping, backup failures) go only there.
TELEGRAM_AUTHORIZED_USER_IDS = _parse_user_ids(_require("TELEGRAM_AUTHORIZED_USER_ID"))
TELEGRAM_AUTHORIZED_USER_ID = TELEGRAM_AUTHORIZED_USER_IDS[0]

# LLM (OpenAI only)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")

# Agent loop API: "responses" (default — Responses API: reasoning carries across tool rounds,
# built-in web search) or "chat" (the previous chat.completions loop, kept as a fallback).
OPENAI_API_MODE = os.getenv("OPENAI_API_MODE", "responses").strip().lower()
# Reasoning effort for the Responses loop ("none" | "low" | "medium" | "high"); low keeps chat snappy
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low").strip().lower()
# Built-in web search tool in the Responses loop (set 0 to turn browsing off)
WEB_SEARCH_ENABLED = os.getenv("WEB_SEARCH_ENABLED", "1") not in ("0", "false", "no")

# Image generation (the generate_image tool)
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
IMAGE_QUALITY = os.getenv("IMAGE_QUALITY", "medium")

# Semantic search embeddings
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "512"))

# Voice note transcription
VOICE_MODEL = os.getenv("VOICE_MODEL", "gpt-4o-mini-transcribe")

# Spoken replies (text-to-speech): model, voice, and the longest answer that is read aloud
VOICE_OUT_MODEL = os.getenv("VOICE_OUT_MODEL", "gpt-4o-mini-tts")
VOICE_OUT_VOICE = os.getenv("VOICE_OUT_VOICE", "alloy")
VOICE_OUT_MAX_CHARS = int(os.getenv("VOICE_OUT_MAX_CHARS", "1500"))

# Morning digest (local hour; set DIGEST_ENABLED=0 to turn off)
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "1") not in ("0", "false", "no")
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))

# Optional evening wrap-up (local hour; 0 or blank disables)
EVENING_DIGEST_HOUR = int(os.getenv("EVENING_DIGEST_HOUR") or 0)

# How deep the Gmail backfill goes per account (index the whole recent mailbox)
BACKFILL_MAX_EMAILS = int(os.getenv("BACKFILL_MAX_EMAILS", "2000"))

# OpenAI HTTP timeout per request, and the overall budget for one agent turn (seconds)
OPENAI_TIMEOUT_SECONDS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
TURN_TIMEOUT_SECONDS = float(os.getenv("TURN_TIMEOUT_SECONDS", "180"))

# Read-only tools in one agent round run concurrently, at most this many at once
MAX_PARALLEL_TOOLS = int(os.getenv("MAX_PARALLEL_TOOLS", "4"))

# Prices in USD per 1M tokens, used by /usage. PLACEHOLDERS — check platform.openai.com/pricing
# and update; unknown models are recorded with cost 0 and flagged in /usage.
# Two non-token entries: "per_image" is USD per generated image (the image model's usage row
# stores the image count in completion_tokens), and the TTS model is priced per character —
# its "in" figure is USD per 1M *characters* (the usage row stores characters in prompt_tokens).
PRICES_PER_MTOK: dict[str, dict[str, float]] = {
    "gpt-5.1": {"in": 1.25, "cached": 0.125, "out": 10.0},
    "gpt-5-mini": {"in": 0.25, "cached": 0.025, "out": 2.0},
    "text-embedding-3-small": {"in": 0.02, "cached": 0.02, "out": 0.0},
    "gpt-4o-mini-transcribe": {"in": 1.25, "cached": 1.25, "out": 5.0},
    "whisper-1": {"in": 0.0, "cached": 0.0, "out": 0.0},  # billed per minute, not tokens
    "gpt-image-1": {"per_image": 0.04},                   # per image (1024x1024, medium quality)
    "gpt-4o-mini-tts": {"in": 15.0, "cached": 15.0, "out": 0.0},  # per 1M characters (~$0.015/min)
}

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

# Nightly SQLite backup (local hour; set BACKUP_ENABLED=0 to turn off)
BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "1") not in ("0", "false", "no")
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "3"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR") or (Path(__file__).parent / "backups"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))

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

# iMessage: override the chat.db location (default ~/Library/Messages/chat.db on the Mac).
# Set to a fixture (tests/fixtures) to exercise the reader on Windows.
IMESSAGE_DB_PATH = os.getenv("IMESSAGE_DB_PATH", "")

# iMessage backfill page size (rows per batch while walking the whole chat.db)
IMESSAGE_BATCH_SIZE = int(os.getenv("IMESSAGE_BATCH_SIZE", "2000"))

# Cheap model for internal calls (urgency triage, short drafts, memory extraction, summaries)
OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-5-mini")

# Memory 2.0: extract durable facts from every turn with the fast model (set 0 to turn off;
# the explicit `remember` tool keeps working), and how many memories are picked by relevance
MEMORY_AUTO_EXTRACT = os.getenv("MEMORY_AUTO_EXTRACT", "1") not in ("0", "false", "no")
MEMORY_TOP_K = int(os.getenv("MEMORY_TOP_K", "12"))

# Urgent inbound alerts: poll cadence, quiet hours (local, "23-7" = 23:00–07:00), max item age
ALERTS_ENABLED = os.getenv("ALERTS_ENABLED", "1") not in ("0", "false", "no")
ALERT_POLL_MINUTES = int(os.getenv("ALERT_POLL_MINUTES", "3"))
ALERT_QUIET_HOURS = os.getenv("ALERT_QUIET_HOURS", "23-7")
ALERT_MAX_AGE_HOURS = int(os.getenv("ALERT_MAX_AGE_HOURS", "24"))

# Waiting-on tracker: a sent email/text with no reply after this many days is "waiting"
WAITING_ON_DAYS = int(os.getenv("WAITING_ON_DAYS", "3"))

# Reminders: local hour used for date-only reminders and the "Tomorrow" snooze
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))

# Near-realtime iMessage indexing: check chat.db's mtime this often (seconds; 0 disables)
IMESSAGE_WATCH_SECONDS = int(os.getenv("IMESSAGE_WATCH_SECONDS", "30"))

# Apple Contacts: override the AddressBook store (default ~/Library/Application Support/
# AddressBook/… on the Mac). Point at a fixture to exercise the resolver on Windows.
CONTACTS_DB_PATH = os.getenv("CONTACTS_DB_PATH", "")

# Country calling code assumed for phone numbers stored without one (US/Canada = 1)
CONTACTS_DEFAULT_COUNTRY = os.getenv("CONTACTS_DEFAULT_COUNTRY", "1").lstrip("+")

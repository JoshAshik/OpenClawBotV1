"""
iMessage reader — reads from Mac's Messages database.

Mac only. Reads ~/Library/Messages/chat.db (SQLite).
Requires Full Disk Access for the Python process in System Settings → Privacy.

Setup on Mac Mini:
  1. System Settings → Privacy & Security → Full Disk Access
  2. Add Terminal (or the Python binary) to the list
  3. Restart the bot
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Apple's epoch starts at 2001-01-01
_APPLE_EPOCH_OFFSET = 978307200


def is_available() -> bool:
    """Check if the iMessage database is accessible."""
    return _DB_PATH.exists()


def _apple_time_to_iso(apple_time: int | None) -> str:
    """Convert Apple's nanosecond timestamp to ISO format."""
    if not apple_time:
        return ""
    try:
        # Newer macOS uses nanoseconds
        if apple_time > 1e15:
            unix_ts = (apple_time / 1e9) + _APPLE_EPOCH_OFFSET
        else:
            unix_ts = apple_time + _APPLE_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return ""


def get_recent_messages(limit: int = 500) -> list[dict]:
    """
    Get recent iMessages.
    Returns list of {rowid, text, contact, date, is_from_me}.
    """
    if not is_available():
        raise RuntimeError(f"iMessage database not found at {_DB_PATH}. Mac only.")

    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute("""
            SELECT
                m.ROWID as rowid,
                m.text as text,
                m.is_from_me as is_from_me,
                m.date as date,
                COALESCE(h.id, 'Unknown') as contact
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != ''
            ORDER BY m.date DESC
            LIMIT ?
        """, (limit,))

        messages = []
        for row in cursor.fetchall():
            messages.append({
                "rowid": row["rowid"],
                "text": row["text"],
                "contact": row["contact"],
                "date": _apple_time_to_iso(row["date"]),
                "is_from_me": bool(row["is_from_me"]),
            })

        return messages

    finally:
        conn.close()


def get_messages_from_contact(contact: str, limit: int = 100) -> list[dict]:
    """Get messages from/to a specific contact (phone number or email)."""
    if not is_available():
        raise RuntimeError(f"iMessage database not found at {_DB_PATH}. Mac only.")

    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute("""
            SELECT
                m.ROWID as rowid,
                m.text as text,
                m.is_from_me as is_from_me,
                m.date as date,
                COALESCE(h.id, 'Unknown') as contact
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL AND m.text != ''
              AND h.id LIKE ?
            ORDER BY m.date DESC
            LIMIT ?
        """, (f"%{contact}%", limit))

        return [
            {
                "rowid": row["rowid"],
                "text": row["text"],
                "contact": row["contact"],
                "date": _apple_time_to_iso(row["date"]),
                "is_from_me": bool(row["is_from_me"]),
            }
            for row in cursor.fetchall()
        ]

    finally:
        conn.close()


def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Search iMessage text content."""
    if not is_available():
        raise RuntimeError(f"iMessage database not found at {_DB_PATH}. Mac only.")

    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute("""
            SELECT
                m.ROWID as rowid,
                m.text as text,
                m.is_from_me as is_from_me,
                m.date as date,
                COALESCE(h.id, 'Unknown') as contact
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text LIKE ?
            ORDER BY m.date DESC
            LIMIT ?
        """, (f"%{query}%", limit))

        return [
            {
                "rowid": row["rowid"],
                "text": row["text"],
                "contact": row["contact"],
                "date": _apple_time_to_iso(row["date"]),
                "is_from_me": bool(row["is_from_me"]),
            }
            for row in cursor.fetchall()
        ]

    finally:
        conn.close()


def list_contacts(limit: int = 50) -> list[dict]:
    """List contacts with most recent message activity."""
    if not is_available():
        raise RuntimeError(f"iMessage database not found at {_DB_PATH}. Mac only.")

    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute("""
            SELECT
                h.id as contact,
                COUNT(m.ROWID) as message_count,
                MAX(m.date) as last_date
            FROM handle h
            JOIN message m ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL
            GROUP BY h.id
            ORDER BY last_date DESC
            LIMIT ?
        """, (limit,))

        return [
            {
                "contact": row["contact"],
                "message_count": row["message_count"],
                "last_date": _apple_time_to_iso(row["last_date"]),
            }
            for row in cursor.fetchall()
        ]

    finally:
        conn.close()

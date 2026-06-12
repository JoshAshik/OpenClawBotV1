"""
WhatsApp Desktop reader — reads from WhatsApp's local storage on Mac.

Mac only. WhatsApp Desktop stores its database in:
  ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/

Modern WhatsApp Desktop (Electron) stores messages in a LevelDB/IndexedDB
format that's harder to read directly. This module uses the ChatStorage.sqlite
database when available, and falls back to monitoring the notification log.

Setup on Mac Mini:
  1. Install WhatsApp Desktop from the Mac App Store
  2. Link it to your phone (QR code)
  3. Grant Full Disk Access to Python in System Settings
  4. Messages will be read-only (no sending)
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# WhatsApp Desktop paths on macOS
_WA_CONTAINER = Path.home() / "Library" / "Group Containers" / "group.net.whatsapp.WhatsApp.shared"
_WA_CHAT_DB = _WA_CONTAINER / "ChatStorage.sqlite"

# Alternative: newer Electron-based WhatsApp
_WA_ELECTRON = Path.home() / "Library" / "Application Support" / "WhatsApp" / "IndexedDB"


def is_available() -> bool:
    """Check if WhatsApp Desktop database is accessible."""
    return _WA_CHAT_DB.exists()


def _find_db_path() -> Path | None:
    """Find the WhatsApp chat database."""
    if _WA_CHAT_DB.exists():
        return _WA_CHAT_DB

    # Check for alternative locations
    alt_paths = [
        _WA_CONTAINER / "ChatStorage.sqlite",
        _WA_CONTAINER / "Message" / "Message.sqlite",
    ]
    for p in alt_paths:
        if p.exists():
            return p

    return None


def get_recent_messages(limit: int = 500) -> list[dict]:
    """
    Get recent WhatsApp messages.
    Returns list of {id, text, contact, date, is_from_me}.
    """
    db_path = _find_db_path()
    if db_path is None:
        raise RuntimeError(
            "WhatsApp Desktop database not found. "
            "Make sure WhatsApp Desktop is installed and linked on this Mac."
        )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        # WhatsApp's ChatStorage.sqlite schema
        cursor = conn.execute("""
            SELECT
                ZWAMESSAGE.Z_PK as id,
                ZWAMESSAGE.ZTEXT as text,
                ZWAMESSAGE.ZISFROMME as is_from_me,
                ZWAMESSAGE.ZMESSAGEDATE as date,
                ZWACHATSESSION.ZCONTACTJID as contact_jid,
                ZWACHATSESSION.ZPARTNERNAME as contact_name
            FROM ZWAMESSAGE
            LEFT JOIN ZWACHATSESSION
                ON ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
            WHERE ZWAMESSAGE.ZTEXT IS NOT NULL AND ZWAMESSAGE.ZTEXT != ''
            ORDER BY ZWAMESSAGE.ZMESSAGEDATE DESC
            LIMIT ?
        """, (limit,))

        messages = []
        for row in cursor.fetchall():
            contact = row["contact_name"] or row["contact_jid"] or "Unknown"
            # WhatsApp uses Apple epoch (seconds since 2001-01-01)
            date_str = _wa_time_to_iso(row["date"])

            messages.append({
                "id": str(row["id"]),
                "text": row["text"],
                "contact": contact,
                "date": date_str,
                "is_from_me": bool(row["is_from_me"]),
            })

        return messages

    except sqlite3.OperationalError as e:
        # Schema might differ between WhatsApp versions
        logger.error(f"WhatsApp DB query failed (schema mismatch?): {e}")
        raise RuntimeError(
            f"WhatsApp database schema not recognized: {e}. "
            "The WhatsApp Desktop version may have changed its storage format."
        )
    finally:
        conn.close()


def _wa_time_to_iso(wa_time: float | None) -> str:
    """Convert WhatsApp's timestamp to ISO format."""
    if not wa_time:
        return ""
    try:
        # WhatsApp on Mac uses Apple epoch (978307200 offset)
        unix_ts = wa_time + 978307200
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return ""


def get_messages_from_contact(contact: str, limit: int = 100) -> list[dict]:
    """Get messages from a specific contact."""
    db_path = _find_db_path()
    if db_path is None:
        raise RuntimeError("WhatsApp Desktop database not found.")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute("""
            SELECT
                ZWAMESSAGE.Z_PK as id,
                ZWAMESSAGE.ZTEXT as text,
                ZWAMESSAGE.ZISFROMME as is_from_me,
                ZWAMESSAGE.ZMESSAGEDATE as date,
                ZWACHATSESSION.ZPARTNERNAME as contact_name
            FROM ZWAMESSAGE
            LEFT JOIN ZWACHATSESSION
                ON ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
            WHERE ZWAMESSAGE.ZTEXT IS NOT NULL
              AND (ZWACHATSESSION.ZPARTNERNAME LIKE ? OR ZWACHATSESSION.ZCONTACTJID LIKE ?)
            ORDER BY ZWAMESSAGE.ZMESSAGEDATE DESC
            LIMIT ?
        """, (f"%{contact}%", f"%{contact}%", limit))

        return [
            {
                "id": str(row["id"]),
                "text": row["text"],
                "contact": row["contact_name"] or "Unknown",
                "date": _wa_time_to_iso(row["date"]),
                "is_from_me": bool(row["is_from_me"]),
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def search_messages(query: str, limit: int = 50) -> list[dict]:
    """Search WhatsApp message text."""
    db_path = _find_db_path()
    if db_path is None:
        raise RuntimeError("WhatsApp Desktop database not found.")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cursor = conn.execute("""
            SELECT
                ZWAMESSAGE.Z_PK as id,
                ZWAMESSAGE.ZTEXT as text,
                ZWAMESSAGE.ZISFROMME as is_from_me,
                ZWAMESSAGE.ZMESSAGEDATE as date,
                ZWACHATSESSION.ZPARTNERNAME as contact_name
            FROM ZWAMESSAGE
            LEFT JOIN ZWACHATSESSION
                ON ZWAMESSAGE.ZCHATSESSION = ZWACHATSESSION.Z_PK
            WHERE ZWAMESSAGE.ZTEXT LIKE ?
            ORDER BY ZWAMESSAGE.ZMESSAGEDATE DESC
            LIMIT ?
        """, (f"%{query}%", limit))

        return [
            {
                "id": str(row["id"]),
                "text": row["text"],
                "contact": row["contact_name"] or "Unknown",
                "date": _wa_time_to_iso(row["date"]),
                "is_from_me": bool(row["is_from_me"]),
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()

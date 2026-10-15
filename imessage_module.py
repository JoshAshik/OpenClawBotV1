"""
iMessage reader — reads the Mac Messages database (~/Library/Messages/chat.db).

Mac only in production; on any platform the `IMESSAGE_DB_PATH` env var (see config.py)
points the reader at another chat.db (used by the synthetic fixture in tests/).

Since macOS Ventura most `message.text` columns are NULL and the body lives in
`message.attributedBody`, an NSAttributedString serialised as a "typedstream".
`decode_attributed_body` pulls the NSString payload out of that blob. Everything
else here is plain SQL over the real schema:

  message              one row per message part (text, attributedBody, date, service,
                       is_from_me, handle_id = sender, associated_message_type = tapback)
  handle               phone number / email per contact (`id`)
  chat                 a conversation: 1:1 (style 45, chat_identifier == handle id) or
                       group (style 43, display_name)
  chat_message_join    message -> chat
  chat_handle_join     chat -> participants
  attachment +
  message_attachment_join   attachment filenames per message

Setup on the Mac: System Settings → Privacy & Security → Full Disk Access → add the
Python binary that runs the bot, then restart it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
# Kept for callers that still read the module constant; prefer db_path().
_DB_PATH = _DEFAULT_DB_PATH

# Apple's epoch starts at 2001-01-01 00:00:00 UTC.
_APPLE_EPOCH_OFFSET = 978307200

# Reactions ("Loved …"), sticker reactions and edits start at 2000.
TAPBACK_THRESHOLD = 2000

# Group chats have style 43; 1:1 chats style 45.
_GROUP_STYLE = 43

# NSAttributedString puts this where an attachment sits in the text.
_OBJECT_REPLACEMENT = "￼"


# ── Location ──────────────────────────────────────────────────────

def db_path() -> Path:
    """chat.db location: IMESSAGE_DB_PATH (env / config) or the Mac default."""
    override = os.environ.get("IMESSAGE_DB_PATH", "")
    if not override:
        try:
            import config
            override = getattr(config, "IMESSAGE_DB_PATH", "") or ""
        except Exception:  # config needs a filled .env; tests may not have one
            override = ""
    return Path(override).expanduser() if override else _DEFAULT_DB_PATH


def is_available() -> bool:
    """True when a chat.db exists where we expect it."""
    return db_path().exists()


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        raise RuntimeError(f"iMessage database not found at {path}. Mac only.")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── typedstream decoding ──────────────────────────────────────────

def _read_typedstream_int(blob: bytes, pos: int) -> tuple[int, int]:
    """
    Read a typedstream integer at `pos`. Values < 128 are one byte; 0x81 is followed
    by a 2-byte little-endian value, 0x82 by 4 bytes, 0x83 by 8 bytes.
    Returns (value, position after the integer).
    """
    marker = blob[pos]
    if marker == 0x81:
        return struct.unpack_from("<H", blob, pos + 1)[0], pos + 3
    if marker == 0x82:
        return struct.unpack_from("<I", blob, pos + 1)[0], pos + 5
    if marker == 0x83:
        return struct.unpack_from("<Q", blob, pos + 1)[0], pos + 9
    return marker, pos + 1


def decode_attributed_body(blob: bytes | None) -> str:
    """
    Extract the NSString payload from an NSAttributedString typedstream blob.

    Layout (as written by Messages.app and read by imessage-exporter et al.):
      … "NSString" <class version> <NSObject ref> \\x84 \\x01 "+" <length> <utf-8 bytes> \\x86 …
    The "+" is the typedstream code for a C string; the length that follows uses the
    integer encoding handled by _read_typedstream_int. Returns "" when nothing usable
    is found. Attachment placeholders (U+FFFC) are stripped.
    """
    if not blob:
        return ""
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    try:
        anchor = blob.find(b"NSString")
        if anchor < 0:
            return ""
        marker = blob.find(b"+", anchor + len(b"NSString"))
        if marker < 0 or marker + 1 >= len(blob):
            return ""
        length, start = _read_typedstream_int(blob, marker + 1)
        if length <= 0 or start + length > len(blob):
            return ""
        text = blob[start:start + length].decode("utf-8", errors="replace")
    except (struct.error, IndexError, ValueError):
        return ""
    return text.replace(_OBJECT_REPLACEMENT, "").strip()


def message_body(text: str | None, attributed_body: bytes | None) -> str:
    """Plain body for a message row: `text` when present, else the decoded blob."""
    if text:
        cleaned = text.replace(_OBJECT_REPLACEMENT, "").strip()
        if cleaned:
            return cleaned
    return decode_attributed_body(attributed_body)


# ── Dates ─────────────────────────────────────────────────────────

def _apple_time_to_iso(apple_time: int | None) -> str:
    """Apple timestamp (ns since 2001; older DBs used seconds) → ISO 8601 UTC."""
    if not apple_time:
        return ""
    try:
        if apple_time > 1e15:  # nanoseconds (macOS 10.13+)
            unix_ts = (apple_time / 1e9) + _APPLE_EPOCH_OFFSET
        else:
            unix_ts = apple_time + _APPLE_EPOCH_OFFSET
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return ""


def iso_to_apple_ns(iso: str) -> int:
    """ISO 8601 → Apple nanoseconds (inverse of _apple_time_to_iso for current DBs)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt.timestamp() - _APPLE_EPOCH_OFFSET) * 1_000_000_000)


# ── Row → dict ────────────────────────────────────────────────────

# One row per message with its (first) chat and the sender handle.
_MESSAGE_SELECT = """
    SELECT
        m.ROWID                   AS rowid,
        m.guid                    AS guid,
        m.text                    AS text,
        m.attributedBody          AS attributed_body,
        m.is_from_me              AS is_from_me,
        m.date                    AS date,
        m.service                 AS service,
        m.handle_id               AS handle_id,
        m.associated_message_type AS associated_message_type,
        m.cache_has_attachments   AS cache_has_attachments,
        h.id                      AS handle,
        c.ROWID                   AS chat_id,
        c.chat_identifier         AS chat_identifier,
        c.display_name            AS display_name,
        c.style                   AS chat_style,
        c.service_name            AS chat_service
    FROM message m
    LEFT JOIN handle h ON h.ROWID = m.handle_id
    LEFT JOIN chat c ON c.ROWID = (
        SELECT cmj.chat_id FROM chat_message_join cmj
        WHERE cmj.message_id = m.ROWID ORDER BY cmj.chat_id LIMIT 1
    )
"""

# Rows that can never yield a body: tapbacks/edits and rows with neither text column.
_USABLE_WHERE = """
    (m.associated_message_type IS NULL OR m.associated_message_type < 2000)
    AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
"""


def _is_group(row) -> bool:
    if row["chat_id"] is None:
        return False
    if row["chat_style"] == _GROUP_STYLE:
        return True
    ident = row["chat_identifier"] or ""
    return ident.startswith("chat") and bool(row["display_name"])


def chat_key_for(chat_id: int | None, handle: str | None) -> str:
    """Stable key for grouping messages into conversations."""
    if chat_id is not None:
        return f"chat:{chat_id}"
    return f"handle:{handle or 'Unknown'}"


def _row_to_message(row, attachments: list[str]) -> dict | None:
    body = message_body(row["text"], row["attributed_body"])
    if not body:
        return None

    is_from_me = bool(row["is_from_me"])
    handle = row["handle"] or ""
    is_group = _is_group(row)
    chat_id = row["chat_id"]
    chat_identifier = row["chat_identifier"] or ""
    display_name = row["display_name"] or ""

    if is_group:
        contact = display_name or chat_identifier
    elif chat_identifier:
        contact = chat_identifier  # 1:1 chat identifier == the other party's handle
    else:
        contact = handle or "Unknown"

    sender = "Me" if is_from_me else (handle or contact)
    service = row["service"] or row["chat_service"] or "iMessage"

    return {
        "rowid": row["rowid"],
        "guid": row["guid"] or "",
        "text": body,
        "sender": sender,
        "contact": contact,
        "is_from_me": is_from_me,
        "date": _apple_time_to_iso(row["date"]),
        "service": service,
        "chat_id": chat_id,
        "chat_key": chat_key_for(chat_id, contact),
        "chat_identifier": chat_identifier,
        "display_name": display_name,
        "is_group": is_group,
        "attachments": attachments,
    }


def _attachments_for(conn: sqlite3.Connection, rowids: list[int]) -> dict[int, list[str]]:
    """message ROWID → attachment filenames (basename of the stored path)."""
    out: dict[int, list[str]] = {}
    for start in range(0, len(rowids), 500):  # stay under SQLite's variable limit
        chunk = rowids[start:start + 500]
        marks = ",".join("?" * len(chunk))
        cur = conn.execute(
            f"""SELECT maj.message_id AS message_id, a.filename AS filename,
                       a.transfer_name AS transfer_name
                FROM message_attachment_join maj
                JOIN attachment a ON a.ROWID = maj.attachment_id
                WHERE maj.message_id IN ({marks})
                ORDER BY maj.message_id, a.ROWID""",
            chunk,
        )
        for r in cur.fetchall():
            name = r["transfer_name"] or (Path(r["filename"]).name if r["filename"] else "")
            if name:
                out.setdefault(r["message_id"], []).append(name)
    return out


def _rows_to_messages(conn: sqlite3.Connection, rows) -> list[dict]:
    rows = list(rows)
    with_attachments = [r["rowid"] for r in rows if r["cache_has_attachments"]]
    attachments = _attachments_for(conn, with_attachments) if with_attachments else {}
    out = []
    for r in rows:
        msg = _row_to_message(r, attachments.get(r["rowid"], []))
        if msg is not None:
            out.append(msg)
    return out


# ── Incremental reads (indexer) ───────────────────────────────────

def read_batch(after_rowid: int, limit: int = 2000) -> tuple[list[dict], int]:
    """
    Next page of usable messages with ROWID > after_rowid, ascending by ROWID.
    Returns (messages, last_rowid_scanned); the caller stores last_rowid_scanned as
    its cursor. last_rowid_scanned == after_rowid means there was nothing new.
    Rows that decode to an empty body are skipped but still advance the cursor.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            _MESSAGE_SELECT + f" WHERE m.ROWID > ? AND {_USABLE_WHERE} ORDER BY m.ROWID LIMIT ?",
            (after_rowid, limit),
        )
        rows = cur.fetchall()
        if not rows:
            # Nothing usable left — jump the cursor past any trailing unusable rows.
            top = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
            return [], max(after_rowid, top)
        messages = _rows_to_messages(conn, rows)
        last = rows[-1]["rowid"]
        if len(rows) < limit:
            top = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
            last = max(last, top)
        return messages, last
    finally:
        conn.close()


def get_messages_since(after_rowid: int, limit: int = 2000) -> list[dict]:
    """Messages with ROWID > after_rowid, ascending (see read_batch for the cursor)."""
    return read_batch(after_rowid, limit)[0]


def max_rowid() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0
    finally:
        conn.close()


def get_chat_context(chat_key: str, before_rowid: int, until_iso: str,
                     gap_minutes: int = 30, max_rows: int = 500) -> list[dict]:
    """
    Earlier messages of one chat that belong to the same conversation window as a
    message at `until_iso`: walks backwards from that time over rows with
    ROWID <= before_rowid, stopping at the first gap longer than `gap_minutes`.
    Lets the indexer rebuild a whole window when new messages append to it.
    """
    if not until_iso:
        return []
    kind, _, ident = chat_key.partition(":")
    if kind == "chat":
        where = "c.ROWID = ?"
        param: object = int(ident)
    else:
        where = "c.ROWID IS NULL AND h.id = ?"
        param = ident

    conn = _connect()
    try:
        cur = conn.execute(
            _MESSAGE_SELECT
            + f" WHERE {where} AND m.ROWID <= ? AND m.date <= ? AND {_USABLE_WHERE}"
            " ORDER BY m.date DESC LIMIT ?",
            (param, before_rowid, iso_to_apple_ns(until_iso), max_rows),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        gap_ns = gap_minutes * 60 * 1_000_000_000
        kept = []
        last_date = iso_to_apple_ns(until_iso)
        for r in rows:
            if last_date - (r["date"] or 0) > gap_ns:
                break
            kept.append(r)
            last_date = r["date"] or last_date
        kept.reverse()
        return _rows_to_messages(conn, kept)
    finally:
        conn.close()


# ── Convenience reads (live search, tools) ────────────────────────

def _newest(conn: sqlite3.Connection, extra_where: str, params: tuple, limit: int) -> list[dict]:
    cur = conn.execute(
        _MESSAGE_SELECT + f" WHERE {_USABLE_WHERE} {extra_where} ORDER BY m.date DESC LIMIT ?",
        (*params, limit),
    )
    return _rows_to_messages(conn, cur.fetchall())


def get_recent_messages(limit: int = 500) -> list[dict]:
    """Most recent usable messages (newest first)."""
    conn = _connect()
    try:
        return _newest(conn, "", (), limit)
    finally:
        conn.close()


def get_messages_from_contact(contact: str, limit: int = 100) -> list[dict]:
    """
    Messages exchanged with a handle (phone/email substring): the 1:1 chat with them
    plus anything they sent in group chats. Newest first.
    """
    conn = _connect()
    try:
        like = f"%{contact}%"
        return _newest(conn, "AND (c.chat_identifier LIKE ? OR h.id LIKE ?)", (like, like), limit)
    finally:
        conn.close()


def get_timeline(handles: list[str] | None = None, chat_key: str | None = None,
                 after_iso: str | None = None, before_iso: str | None = None,
                 limit: int = 40) -> list[dict]:
    """
    The newest `limit` usable messages, returned in chronological order, for either
    one chat (`chat_key` = "chat:<id>" / "handle:<id>") or everything involving any of
    `handles`: their 1:1 chat (both directions) plus what they sent in group chats.
    `after_iso` is inclusive, `before_iso` exclusive. Live read, not the index.
    """
    clauses: list[str] = []
    params: list[object] = []
    if chat_key:
        kind, _, ident = chat_key.partition(":")
        if kind == "chat":
            clauses.append("c.ROWID = ?")
            params.append(int(ident))
        else:
            clauses.append("c.ROWID IS NULL AND h.id = ?")
            params.append(ident)
    elif handles:
        hs = [h for h in handles if h]
        if not hs:
            return []
        marks = ",".join("?" * len(hs))
        clauses.append(f"(c.chat_identifier IN ({marks}) OR h.id IN ({marks}))")
        params.extend(hs)
        params.extend(hs)
    else:
        return []
    if after_iso:
        clauses.append("m.date >= ?")
        params.append(iso_to_apple_ns(after_iso))
    if before_iso:
        clauses.append("m.date < ?")
        params.append(iso_to_apple_ns(before_iso))

    conn = _connect()
    try:
        cur = conn.execute(
            _MESSAGE_SELECT + f" WHERE {_USABLE_WHERE} AND " + " AND ".join(clauses)
            + " ORDER BY m.date DESC LIMIT ?",
            (*params, max(1, limit)),
        )
        msgs = _rows_to_messages(conn, cur.fetchall())
        msgs.reverse()
        return msgs
    finally:
        conn.close()


def search_messages(query: str, limit: int = 50, scan_limit: int = 50_000) -> list[dict]:
    """
    Case-insensitive substring search over message bodies, newest first. Bodies that
    only exist in attributedBody can't be LIKE-matched in SQL, so this decodes the
    newest `scan_limit` usable rows in pages and matches in Python.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []
    conn = _connect()
    try:
        hits: list[dict] = []
        page = 2000
        offset = 0
        while offset < scan_limit and len(hits) < limit:
            cur = conn.execute(
                _MESSAGE_SELECT + f" WHERE {_USABLE_WHERE} ORDER BY m.date DESC LIMIT ? OFFSET ?",
                (page, offset),
            )
            rows = cur.fetchall()
            if not rows:
                break
            for msg in _rows_to_messages(conn, rows):
                if needle in msg["text"].lower():
                    hits.append(msg)
                    if len(hits) >= limit:
                        break
            offset += page
        return hits
    finally:
        conn.close()


def list_contacts(limit: int = 50) -> list[dict]:
    """Handles with the most recent inbound activity."""
    conn = _connect()
    try:
        cur = conn.execute(
            f"""SELECT h.id AS contact, COUNT(m.ROWID) AS message_count, MAX(m.date) AS last_date
                FROM handle h
                JOIN message m ON m.handle_id = h.ROWID
                WHERE {_USABLE_WHERE}
                GROUP BY h.id
                ORDER BY last_date DESC
                LIMIT ?""",
            (limit,),
        )
        return [
            {
                "contact": r["contact"],
                "message_count": r["message_count"],
                "last_date": _apple_time_to_iso(r["last_date"]),
            }
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def list_chats(limit: int = 50) -> list[dict]:
    """Chats (1:1 and group) by last activity, with their participants."""
    conn = _connect()
    try:
        cur = conn.execute(
            """SELECT c.ROWID AS chat_id, c.chat_identifier, c.display_name, c.style,
                      c.service_name, MAX(m.date) AS last_date, COUNT(m.ROWID) AS message_count
               FROM chat c
               JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
               JOIN message m ON m.ROWID = cmj.message_id
               GROUP BY c.ROWID
               ORDER BY last_date DESC
               LIMIT ?""",
            (limit,),
        )
        chats = []
        for r in cur.fetchall():
            parts = conn.execute(
                """SELECT h.id FROM chat_handle_join chj
                   JOIN handle h ON h.ROWID = chj.handle_id
                   WHERE chj.chat_id = ? ORDER BY h.ROWID""",
                (r["chat_id"],),
            ).fetchall()
            is_group = r["style"] == _GROUP_STYLE
            chats.append({
                "chat_id": r["chat_id"],
                "chat_key": chat_key_for(r["chat_id"], None),
                "chat_identifier": r["chat_identifier"] or "",
                "display_name": r["display_name"] or "",
                "is_group": is_group,
                "service": r["service_name"] or "",
                "participants": [p["id"] for p in parts],
                "message_count": r["message_count"],
                "last_date": _apple_time_to_iso(r["last_date"]),
            })
        return chats
    finally:
        conn.close()

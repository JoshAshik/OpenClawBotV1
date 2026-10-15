"""
Synthetic chat.db fixture — a tiny Messages database with the real macOS schema
(table and column names as in ~/Library/Messages/chat.db) so imessage_module can be
exercised on Windows.

    python tests/fixtures/make_chat_db.py out/chat.db

Contents: two 1:1 chats (one iMessage, one SMS), one group chat, rows whose body is
only in `text`, rows whose body is only in `attributedBody` (real typedstream bytes
from `encode_attributed_body`), a tapback and a reaction removal, an attachment, an
empty row, and one message with no chat join. `EXPECTED` documents what a reader
should produce so the tests don't have to repeat it.
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

APPLE_EPOCH_OFFSET = 978307200


# ── typedstream encoder ───────────────────────────────────────────

def encode_typedstream_int(n: int) -> bytes:
    """Typedstream integer: one byte below 128, else 0x81 + u16 LE, else 0x82 + u32 LE."""
    if n < 0:
        raise ValueError("lengths are non-negative")
    if n < 0x80:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\x81" + struct.pack("<H", n)
    return b"\x82" + struct.pack("<I", n)


# Prefix Messages.app writes before the string payload (archiver header, class
# hierarchy for NSAttributedString / NSString, then the "+" C-string marker).
_TS_PREFIX = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
)

# What follows the string: end-of-object, the attribute run ("iI" = two ints: run
# count 1 and the run length), then an NSDictionary with __kIMMessagePartAttributeName.
_TS_SUFFIX_HEAD = b"\x86\x84\x02iI\x01"
_TS_SUFFIX_TAIL = (
    b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d"
    b"__kIMMessagePartAttributeName\x86\x92\x84\x84\x84\x08NSNumber\x00\x84\x84\x07NSValue"
    b"\x00\x94\x84\x01*\x84\x99\x99\x00\x86\x86\x86"
)


def encode_attributed_body(text: str) -> bytes:
    """NSAttributedString typedstream blob carrying `text`, as Messages.app stores it."""
    data = text.encode("utf-8")
    return (
        _TS_PREFIX
        + encode_typedstream_int(len(data)) + data
        + _TS_SUFFIX_HEAD + encode_typedstream_int(len(text))
        + _TS_SUFFIX_TAIL
    )


# ── Schema (real names; a subset of the columns) ──────────────────

SCHEMA = """
CREATE TABLE handle (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    country TEXT,
    service TEXT NOT NULL,
    uncanonicalized_id TEXT,
    person_centric_id TEXT,
    UNIQUE (id, service)
);
CREATE TABLE chat (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    style INTEGER,
    state INTEGER,
    account_id TEXT,
    properties BLOB,
    chat_identifier TEXT,
    service_name TEXT,
    room_name TEXT,
    account_login TEXT,
    is_archived INTEGER DEFAULT 0,
    last_addressed_handle TEXT,
    display_name TEXT,
    group_id TEXT,
    is_filtered INTEGER,
    successful_query INTEGER,
    last_read_message_timestamp INTEGER DEFAULT 0
);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    text TEXT,
    replace INTEGER DEFAULT 0,
    service_center TEXT,
    handle_id INTEGER DEFAULT 0,
    subject TEXT,
    country TEXT,
    attributedBody BLOB,
    version INTEGER DEFAULT 0,
    type INTEGER DEFAULT 0,
    service TEXT,
    account TEXT,
    account_guid TEXT,
    error INTEGER DEFAULT 0,
    date INTEGER,
    date_read INTEGER,
    date_delivered INTEGER,
    is_delivered INTEGER DEFAULT 0,
    is_finished INTEGER DEFAULT 0,
    is_emote INTEGER DEFAULT 0,
    is_from_me INTEGER DEFAULT 0,
    is_empty INTEGER DEFAULT 0,
    is_delayed INTEGER DEFAULT 0,
    is_auto_reply INTEGER DEFAULT 0,
    is_prepared INTEGER DEFAULT 0,
    is_read INTEGER DEFAULT 0,
    is_system_message INTEGER DEFAULT 0,
    is_sent INTEGER DEFAULT 0,
    has_dd_results INTEGER DEFAULT 0,
    is_service_message INTEGER DEFAULT 0,
    is_forward INTEGER DEFAULT 0,
    was_downgraded INTEGER DEFAULT 0,
    is_archive INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0,
    cache_roomnames TEXT,
    was_data_detected INTEGER DEFAULT 0,
    was_deduplicated INTEGER DEFAULT 0,
    is_audio_message INTEGER DEFAULT 0,
    is_played INTEGER DEFAULT 0,
    date_played INTEGER,
    item_type INTEGER DEFAULT 0,
    other_handle INTEGER DEFAULT 0,
    group_title TEXT,
    group_action_type INTEGER DEFAULT 0,
    share_status INTEGER DEFAULT 0,
    share_direction INTEGER DEFAULT 0,
    is_expirable INTEGER DEFAULT 0,
    expire_state INTEGER DEFAULT 0,
    message_action_type INTEGER DEFAULT 0,
    message_source INTEGER DEFAULT 0,
    associated_message_guid TEXT,
    associated_message_type INTEGER DEFAULT 0,
    balloon_bundle_id TEXT,
    payload_data BLOB,
    expressive_send_style_id TEXT,
    associated_message_range_location INTEGER DEFAULT 0,
    associated_message_range_length INTEGER DEFAULT 0,
    time_expressive_send_played INTEGER,
    message_summary_info BLOB,
    ck_sync_state INTEGER DEFAULT 0,
    ck_record_id TEXT,
    ck_record_change_tag TEXT,
    destination_caller_id TEXT,
    is_corrupt INTEGER DEFAULT 0,
    reply_to_guid TEXT,
    sort_id INTEGER,
    is_spam INTEGER DEFAULT 0,
    has_unseen_mention INTEGER DEFAULT 0,
    thread_originator_guid TEXT,
    thread_originator_part TEXT,
    date_retracted INTEGER DEFAULT 0,
    date_edited INTEGER DEFAULT 0,
    part_count INTEGER
);
CREATE TABLE chat_message_join (
    chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
    message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE,
    message_date INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE chat_handle_join (
    chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
    handle_id INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE,
    UNIQUE (chat_id, handle_id)
);
CREATE TABLE attachment (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    guid TEXT UNIQUE NOT NULL,
    created_date INTEGER DEFAULT 0,
    start_date INTEGER DEFAULT 0,
    filename TEXT,
    uti TEXT,
    mime_type TEXT,
    transfer_state INTEGER DEFAULT 0,
    is_outgoing INTEGER DEFAULT 0,
    user_info BLOB,
    transfer_name TEXT,
    total_bytes INTEGER DEFAULT 0,
    is_sticker INTEGER DEFAULT 0,
    hide_attachment INTEGER DEFAULT 0,
    original_guid TEXT UNIQUE NOT NULL
);
CREATE TABLE message_attachment_join (
    message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE,
    attachment_id INTEGER REFERENCES attachment (ROWID) ON DELETE CASCADE,
    UNIQUE (message_id, attachment_id)
);
CREATE INDEX message_idx_date ON message(date);
CREATE INDEX message_idx_handle ON message(handle_id);
CREATE INDEX chat_message_join_idx_message_id ON chat_message_join(message_id);
"""


# ── Data ──────────────────────────────────────────────────────────

MIKE = "+14155550101"
SARA = "+14155550102"
TENANT = "tenant@example.com"
GROUP_NAME = "Unit 4 Repairs"
GROUP_IDENT = "chat123456789012345678"

HANDLES = [  # ROWID, id, service
    (1, MIKE, "iMessage"),
    (2, SARA, "SMS"),
    (3, TENANT, "iMessage"),
]

CHATS = [  # ROWID, guid, style, chat_identifier, service_name, display_name
    (1, f"iMessage;-;{MIKE}", 45, MIKE, "iMessage", None),
    (2, f"SMS;-;{SARA}", 45, SARA, "SMS", None),
    (3, f"iMessage;+;{GROUP_IDENT}", 43, GROUP_IDENT, "iMessage", GROUP_NAME),
]

CHAT_HANDLES = [(1, 1), (2, 2), (3, 1), (3, 3)]

# A body that only exists in attributedBody and nowhere else in the fixture.
ATTRIBUTED_ONLY_PHRASE = "the zebra valve part arrives Monday"

LONG_BODY = (
    "Quick recap from the site visit: the café on the ground floor wants the awning "
    "re-fixed, unit 2's dishwasher hose is cracked, and the roofer quoted £1,250 for "
    "the flashing. Let me know which to approve first 👍"
)


def ts(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> int:
    """UTC calendar time → Apple nanoseconds since 2001-01-01."""
    unix = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp()
    return int((unix - APPLE_EPOCH_OFFSET) * 1_000_000_000)


# ROWID, chat_id (None = no chat_message_join), handle_id, is_from_me, text,
# attributed_text (None = NULL blob), service, date, associated_message_type, attachments
MESSAGES = [
    (1, 1, 1, 0, "Hi, the boiler at 12 Oak is leaking again", None,
     "iMessage", ts(2026, 10, 10, 9, 0), 0, []),
    (2, 1, 1, 1, None, "Can you come Tuesday morning?",
     "iMessage", ts(2026, 10, 10, 9, 5), 0, []),
    (3, 1, 1, 0, None, f"Tuesday works, {ATTRIBUTED_ONLY_PHRASE}",
     "iMessage", ts(2026, 10, 10, 9, 10), 0, []),
    # tapback (Loved) on message 2 — must be skipped
    (4, 1, 1, 0, "Loved “Can you come Tuesday morning?”", None,
     "iMessage", ts(2026, 10, 10, 9, 11), 2000, []),
    # attachment with the U+FFFC placeholder in the body; > 30 min later = new window
    (5, 1, 1, 1, None, "￼Here is the invoice",
     "iMessage", ts(2026, 10, 10, 14, 0), 0,
     [("~/Library/Messages/Attachments/ab/11/invoice_oct.pdf", "invoice_oct.pdf",
       "application/pdf")]),
    # SMS chat
    (6, 2, 2, 0, "Rent paid for October", None,
     "SMS", ts(2026, 10, 11, 10, 0), 0, []),
    (7, 2, 2, 1, None, "Thanks Sara, received",
     "SMS", ts(2026, 10, 11, 10, 2), 0, []),
    # group chat: inbound rows carry the sender handle, my row has handle_id 0
    (8, 3, 1, 0, None, "The hallway light in unit 4 is out",
     "iMessage", ts(2026, 10, 12, 8, 0), 0, []),
    (9, 3, 3, 0, "I can let the electrician in after 3pm", None,
     "iMessage", ts(2026, 10, 12, 8, 3), 0, []),
    (10, 3, 0, 1, None, "Great, I'll book them for Thursday",
     "iMessage", ts(2026, 10, 12, 8, 5), 0, []),
    # neither text nor attributedBody (e.g. a retracted message) — skipped
    (11, 1, 1, 0, None, None,
     "iMessage", ts(2026, 10, 13, 9, 0), 0, []),
    # reaction removal — skipped
    (12, 1, 1, 0, "Removed a heart from “Here is the invoice”", None,
     "iMessage", ts(2026, 10, 13, 9, 1), 3000, []),
    # long body (> 127 bytes) exercises the 0x81 length form; non-ASCII too
    (13, 2, 2, 0, None, LONG_BODY,
     "SMS", ts(2026, 10, 14, 16, 30), 0, []),
    # no chat_message_join row at all → keyed by handle
    (14, None, 3, 0, "Orphan message without a chat row", None,
     "iMessage", ts(2026, 10, 14, 17, 0), 0, []),
]

# ROWIDs a reader must return (in order) and must skip.
EXPECTED_ROWIDS = [1, 2, 3, 5, 6, 7, 8, 9, 10, 13, 14]
SKIPPED_ROWIDS = [4, 11, 12]
MAX_ROWID = 14


def build_chat_db(path: Path | str) -> Path:
    """Create the fixture database at `path` (overwriting) and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)", HANDLES)
        conn.executemany(
            "INSERT INTO chat (ROWID, guid, style, chat_identifier, service_name, display_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            CHATS,
        )
        conn.executemany("INSERT INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
                         CHAT_HANDLES)
        att_rowid = 0
        for (rowid, chat_id, handle_id, is_from_me, text, attributed, service, date,
             assoc_type, attachments) in MESSAGES:
            blob = encode_attributed_body(attributed) if attributed is not None else None
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, attributedBody, service, "
                "date, is_from_me, associated_message_type, cache_has_attachments, is_finished, "
                "is_sent, is_delivered) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1)",
                (rowid, f"p:0/{rowid:08X}-FIXTURE", text, handle_id, blob, service, date,
                 is_from_me, assoc_type, 1 if attachments else 0, is_from_me),
            )
            if chat_id is not None:
                conn.execute(
                    "INSERT INTO chat_message_join (chat_id, message_id, message_date) "
                    "VALUES (?, ?, ?)",
                    (chat_id, rowid, date),
                )
            for filename, transfer_name, mime in attachments:
                att_rowid += 1
                conn.execute(
                    "INSERT INTO attachment (ROWID, guid, filename, transfer_name, mime_type, "
                    "original_guid, created_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (att_rowid, f"at_0_{att_rowid}", filename, transfer_name, mime,
                     f"at_0_{att_rowid}", date),
                )
                conn.execute(
                    "INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (?, ?)",
                    (rowid, att_rowid),
                )
        conn.commit()
    finally:
        conn.close()
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("chat.db")
    print(build_chat_db(out))

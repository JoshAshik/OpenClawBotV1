import aiosqlite
import json
from datetime import datetime, timezone
from config import DB_PATH

_DB: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _DB
    if _DB is None:
        _DB = await aiosqlite.connect(DB_PATH)
        _DB.row_factory = aiosqlite.Row
        await _init_tables(_DB)
    return _DB


async def _init_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            user_id     INTEGER,
            action_type TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            summary     TEXT    NOT NULL,
            detail      TEXT,
            approved    INTEGER
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT    NOT NULL,
            role        TEXT    NOT NULL,
            content     TEXT    NOT NULL,
            llm_used    TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT    NOT NULL,
            action_type TEXT    NOT NULL,
            payload     TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_turns (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT    NOT NULL,
            timestamp     TEXT    NOT NULL,
            role          TEXT    NOT NULL,
            messages_json TEXT    NOT NULL,
            llm_used      TEXT,
            est_tokens    INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_id, id);

        CREATE TABLE IF NOT EXISTS memories (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT    NOT NULL,
            category    TEXT,
            content     TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contacts (
            email       TEXT    PRIMARY KEY,
            name        TEXT,
            count       INTEGER NOT NULL DEFAULT 0,
            last_seen   TEXT
        );

        CREATE TABLE IF NOT EXISTS properties (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            address     TEXT,
            place_id    TEXT    NOT NULL UNIQUE,
            added_at    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seen_reviews (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            place_id    TEXT    NOT NULL,
            review_id   TEXT    NOT NULL,
            author      TEXT,
            rating      INTEGER,
            text        TEXT,
            time        INTEGER,
            seen_at     TEXT    NOT NULL,
            UNIQUE(place_id, review_id)
        );
    """)
    await db.commit()


async def log_audit(
    action_type: str,
    category: str,
    summary: str,
    detail: str | None = None,
    user_id: int | None = None,
    approved: bool | None = None,
) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO audit_log (timestamp, user_id, action_type, category, summary, detail, approved) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            user_id,
            action_type,
            category,
            summary,
            detail,
            int(approved) if approved is not None else None,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def save_conversation(role: str, content: str, llm_used: str | None = None) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO conversations (timestamp, role, content, llm_used) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), role, content, llm_used),
    )
    await db.commit()


async def get_recent_conversations(limit: int = 20) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT role, content, llm_used FROM conversations ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [{"role": r["role"], "content": r["content"], "llm_used": r["llm_used"]} for r in reversed(rows)]


# ── Structured Conversation Turns ─────────────────────────────────
#
# Each turn stores the normalized (provider-neutral) message list it added:
#   {"role": "user",         "content": "<text>"}
#   {"role": "assistant",    "content": [{"type": "text", ...}, {"type": "tool_use", ...}]}
#   {"role": "tool_results", "content": [{"tool_use_id": ..., "content": "..."}]}
# llm.py converts these to the OpenAI wire format on each request.

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


async def save_turn(session_id: str, role: str, messages: list[dict], llm_used: str | None = None) -> None:
    db = await get_db()
    payload = json.dumps(messages, ensure_ascii=False)
    await db.execute(
        "INSERT INTO chat_turns (session_id, timestamp, role, messages_json, llm_used, est_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            datetime.now(timezone.utc).isoformat(),
            role,
            payload,
            llm_used,
            _estimate_tokens(payload),
        ),
    )
    await db.commit()


async def get_turn_messages(session_id: str, token_budget: int) -> list[dict]:
    """
    Return the normalized message list for a session, newest turns first
    until the token budget is filled, then flattened back to chronological order.
    Whole turns are kept or dropped — never split (keeps tool_use/tool_result pairs valid).
    """
    db = await get_db()
    cursor = await db.execute(
        "SELECT messages_json, est_tokens FROM chat_turns WHERE session_id = ? ORDER BY id DESC",
        (session_id,),
    )
    rows = await cursor.fetchall()

    kept: list[list[dict]] = []
    used = 0
    for r in rows:
        if used + r["est_tokens"] > token_budget and kept:
            break
        try:
            kept.append(json.loads(r["messages_json"]))
        except json.JSONDecodeError:
            continue
        used += r["est_tokens"]

    messages: list[dict] = []
    for turn in reversed(kept):
        messages.extend(turn)
    return messages


async def get_last_turn_time(session_id: str) -> datetime | None:
    db = await get_db()
    cursor = await db.execute(
        "SELECT timestamp FROM chat_turns WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row["timestamp"])
    except ValueError:
        return None


# ── Long-Term Memories ────────────────────────────────────────────

async def add_memory(content: str, category: str | None = None) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO memories (created_at, category, content) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), category, content),
    )
    await db.commit()
    return cursor.lastrowid


async def list_memories(limit: int = 50) -> list[dict]:
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, created_at, category, content FROM memories ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {"id": r["id"], "created_at": r["created_at"], "category": r["category"], "content": r["content"]}
        for r in rows
    ]


async def delete_memory(memory_id: int) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    await db.commit()
    return cursor.rowcount > 0


async def create_pending_action(action_type: str, payload: dict) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO pending_actions (created_at, action_type, payload, status) VALUES (?, ?, ?, 'pending')",
        (datetime.now(timezone.utc).isoformat(), action_type, json.dumps(payload)),
    )
    await db.commit()
    return cursor.lastrowid


async def get_pending_action(action_id: int) -> dict | None:
    """Fetch a pending action WITHOUT resolving it (used by the Edit loop)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM pending_actions WHERE id = ? AND status = 'pending'", (action_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "action_type": row["action_type"], "payload": json.loads(row["payload"])}


async def update_pending_action_payload(action_id: int, payload: dict) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "UPDATE pending_actions SET payload = ? WHERE id = ? AND status = 'pending'",
        (json.dumps(payload), action_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def resolve_pending_action(action_id: int, status: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    await db.execute(
        "UPDATE pending_actions SET status = ? WHERE id = ?", (status, action_id)
    )
    await db.commit()
    return {"id": row["id"], "action_type": row["action_type"], "payload": json.loads(row["payload"])}


async def get_setting(key: str, default: str | None = None) -> str | None:
    db = await get_db()
    cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    await db.commit()


# ── Contacts (built from indexed mail) ────────────────────────────

async def replace_contacts(contacts: list[dict]) -> None:
    """Atomically replace the contacts table (full rebuild from the index)."""
    db = await get_db()
    await db.execute("DELETE FROM contacts")
    await db.executemany(
        "INSERT OR REPLACE INTO contacts (email, name, count, last_seen) VALUES (?, ?, ?, ?)",
        [(c["email"].lower(), c.get("name", ""), c.get("count", 1), c.get("last_seen", "")) for c in contacts],
    )
    await db.commit()


async def search_contacts(name_or_email: str, limit: int = 5) -> list[dict]:
    db = await get_db()
    like = f"%{name_or_email.lower()}%"
    cursor = await db.execute(
        "SELECT email, name, count, last_seen FROM contacts "
        "WHERE lower(name) LIKE ? OR email LIKE ? "
        "ORDER BY count DESC LIMIT ?",
        (like, like, limit),
    )
    rows = await cursor.fetchall()
    return [
        {"email": r["email"], "name": r["name"], "count": r["count"], "last_seen": r["last_seen"]}
        for r in rows
    ]


# ── Property & Review Helpers ─────────────────────────────────────

async def add_property(name: str, place_id: str, address: str = "") -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO properties (name, address, place_id, added_at) VALUES (?, ?, ?, ?)",
        (name, address, place_id, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()
    return cursor.lastrowid


async def remove_property(place_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute("DELETE FROM properties WHERE place_id = ?", (place_id,))
    await db.commit()
    return cursor.rowcount > 0


async def list_properties() -> list[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT id, name, address, place_id FROM properties ORDER BY name")
    rows = await cursor.fetchall()
    return [{"id": r["id"], "name": r["name"], "address": r["address"], "place_id": r["place_id"]} for r in rows]


async def is_review_seen(place_id: str, review_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM seen_reviews WHERE place_id = ? AND review_id = ?",
        (place_id, review_id),
    )
    return await cursor.fetchone() is not None


async def mark_review_seen(
    place_id: str, review_id: str, author: str, rating: int, text: str, time_val: int
) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO seen_reviews (place_id, review_id, author, rating, text, time, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (place_id, review_id, author, rating, text, time_val, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def get_recent_reviews(place_id: str | None = None, limit: int = 10) -> list[dict]:
    db = await get_db()
    if place_id:
        cursor = await db.execute(
            "SELECT * FROM seen_reviews WHERE place_id = ? ORDER BY time DESC LIMIT ?",
            (place_id, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM seen_reviews ORDER BY time DESC LIMIT ?", (limit,)
        )
    rows = await cursor.fetchall()
    return [
        {
            "place_id": r["place_id"], "review_id": r["review_id"], "author": r["author"],
            "rating": r["rating"], "text": r["text"], "time": r["time"],
        }
        for r in rows
    ]


async def close_db() -> None:
    global _DB
    if _DB:
        await _DB.close()
        _DB = None

import aiosqlite
import json
from datetime import datetime, timezone
from config import DB_PATH

_DB: aiosqlite.Connection | None = None


_DB_LOCK: tuple | None = None  # (loop, asyncio.Lock) guarding first-open


def open_lock(holder: list) -> "asyncio.Lock":
    """Per-event-loop lock so two coroutines can't both open+initialize the connection
    (the WAL switch needs exclusive access; a double open raised 'database is locked')."""
    import asyncio
    loop = asyncio.get_running_loop()
    if not holder or holder[0] is not loop:
        holder[:] = [loop, asyncio.Lock()]
    return holder[1]


_OPEN_LOCK: list = []


async def get_db() -> aiosqlite.Connection:
    global _DB
    if _DB is not None:
        return _DB
    async with open_lock(_OPEN_LOCK):
        if _DB is None:
            conn = await aiosqlite.connect(DB_PATH)
            conn.row_factory = aiosqlite.Row
            await apply_pragmas(conn)
            await _init_tables(conn)
            _DB = conn
    return _DB


async def apply_pragmas(conn: aiosqlite.Connection) -> None:
    """WAL + busy timeout so the bot and its background jobs (two connections to the
    same file) stop tripping over each other's writes."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA synchronous=NORMAL")


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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            email       TEXT,
            emails      TEXT    NOT NULL DEFAULT '[]',
            phones      TEXT    NOT NULL DEFAULT '[]',
            channels    TEXT    NOT NULL DEFAULT '[]',
            count       INTEGER NOT NULL DEFAULT 0,
            last_seen   TEXT
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT    NOT NULL,
            due_at       TEXT    NOT NULL,
            text         TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending',
            fired_at     TEXT,
            snoozed_from TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);

        CREATE TABLE IF NOT EXISTS llm_usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT    NOT NULL,
            purpose           TEXT    NOT NULL,
            model             TEXT    NOT NULL,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            cached_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL    NOT NULL DEFAULT 0,
            priced            INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);

        CREATE TABLE IF NOT EXISTS alerts_sent (
            source    TEXT NOT NULL,
            source_id TEXT NOT NULL,
            token     TEXT NOT NULL,
            title     TEXT,
            category  TEXT,
            reason    TEXT,
            meta      TEXT,
            sent_at   TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_token ON alerts_sent(token);

        CREATE TABLE IF NOT EXISTS waiting_on_dismissed (
            kind         TEXT NOT NULL,
            key          TEXT NOT NULL,
            dismissed_at TEXT NOT NULL,
            PRIMARY KEY (kind, key)
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
    await _migrate_contacts(db)
    await _migrate_chat_turns(db)
    await db.commit()


async def _migrate_chat_turns(db: aiosqlite.Connection) -> None:
    """Add per-turn usage columns to existing databases (ALTER TABLE is additive and safe)."""
    cursor = await db.execute("PRAGMA table_info(chat_turns)")
    cols = {r[1] for r in await cursor.fetchall()}
    for col, decl in (("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
                      ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
                      ("cost_usd", "REAL NOT NULL DEFAULT 0")):
        if col not in cols:
            await db.execute(f"ALTER TABLE chat_turns ADD COLUMN {col} {decl}")


async def _migrate_contacts(db: aiosqlite.Connection) -> None:
    """contacts is derived data (rebuilt every index cycle), so a schema change just
    drops the old email-keyed table; the next rebuild repopulates it."""
    cursor = await db.execute("PRAGMA table_info(contacts)")
    cols = {r[1] for r in await cursor.fetchall()}
    if "phones" not in cols:
        await db.executescript("""
            DROP TABLE IF EXISTS contacts;
            CREATE TABLE contacts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                email       TEXT,
                emails      TEXT    NOT NULL DEFAULT '[]',
                phones      TEXT    NOT NULL DEFAULT '[]',
                channels    TEXT    NOT NULL DEFAULT '[]',
                count       INTEGER NOT NULL DEFAULT 0,
                last_seen   TEXT
            );
        """)


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


async def save_turn(session_id: str, role: str, messages: list[dict], llm_used: str | None = None,
                    prompt_tokens: int = 0, completion_tokens: int = 0, cost_usd: float = 0.0) -> None:
    db = await get_db()
    payload = json.dumps(messages, ensure_ascii=False)
    await db.execute(
        "INSERT INTO chat_turns (session_id, timestamp, role, messages_json, llm_used, est_tokens, "
        "prompt_tokens, completion_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            datetime.now(timezone.utc).isoformat(),
            role,
            payload,
            llm_used,
            _estimate_tokens(payload),
            int(prompt_tokens or 0),
            int(completion_tokens or 0),
            float(cost_usd or 0.0),
        ),
    )
    await db.commit()


# ── LLM usage / cost ───────────────────────────────────────────────

async def record_llm_usage(purpose: str, model: str, prompt_tokens: int, cached_tokens: int,
                           completion_tokens: int, cost_usd: float, priced: bool = True) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO llm_usage (ts, purpose, model, prompt_tokens, cached_tokens, completion_tokens, "
        "cost_usd, priced) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), purpose, model, int(prompt_tokens), int(cached_tokens),
         int(completion_tokens), float(cost_usd), 1 if priced else 0),
    )
    await db.commit()


async def usage_summary(since_iso: str) -> dict:
    """Totals since a UTC ISO timestamp: overall, by purpose, by model, plus unpriced calls."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt, "
        "COALESCE(SUM(cached_tokens), 0) AS cached, COALESCE(SUM(completion_tokens), 0) AS completion, "
        "COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(1 - priced), 0) AS unpriced "
        "FROM llm_usage WHERE ts >= ?", (since_iso,))
    t = await cursor.fetchone()
    out = {"calls": t["calls"], "prompt": t["prompt"], "cached": t["cached"],
           "completion": t["completion"], "cost": t["cost"], "unpriced": t["unpriced"],
           "by_purpose": [], "by_model": []}
    for key in ("purpose", "model"):
        cursor = await db.execute(
            f"SELECT {key} AS k, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost, "
            "COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens "
            f"FROM llm_usage WHERE ts >= ? GROUP BY {key} ORDER BY cost DESC, calls DESC", (since_iso,))
        out[f"by_{key}"] = [{"key": r["k"], "calls": r["calls"], "cost": r["cost"], "tokens": r["tokens"]}
                            for r in await cursor.fetchall()]
    return out


async def top_turns(since_iso: str, limit: int = 5) -> list[dict]:
    """Most expensive assistant turns since a timestamp (first user line as the label)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, session_id, timestamp, prompt_tokens, completion_tokens, cost_usd FROM chat_turns "
        "WHERE role = 'assistant' AND timestamp >= ? AND cost_usd > 0 ORDER BY cost_usd DESC LIMIT ?",
        (since_iso, limit))
    turns = []
    for r in await cursor.fetchall():
        u = await db.execute(
            "SELECT messages_json FROM chat_turns WHERE session_id = ? AND role = 'user' AND id < ? "
            "ORDER BY id DESC LIMIT 1", (r["session_id"], r["id"]))
        row = await u.fetchone()
        label = ""
        if row:
            try:
                msgs = json.loads(row["messages_json"])
                label = str(msgs[0].get("content", ""))[:60] if msgs else ""
            except (json.JSONDecodeError, AttributeError, IndexError):
                label = ""
        turns.append({"id": r["id"], "timestamp": r["timestamp"], "prompt_tokens": r["prompt_tokens"],
                      "completion_tokens": r["completion_tokens"], "cost_usd": r["cost_usd"], "label": label})
    return turns


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


# ── Reminders (not outbound: they only ever message the owner) ──

def _reminder_row(r) -> dict:
    return {k: r[k] for k in ("id", "created_at", "due_at", "text", "status", "fired_at", "snoozed_from")}


async def add_reminder(text: str, due_at_iso: str) -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO reminders (created_at, due_at, text) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), due_at_iso, text),
    )
    await db.commit()
    return cursor.lastrowid


async def get_reminder(reminder_id: int) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    row = await cursor.fetchone()
    return _reminder_row(row) if row else None


async def list_reminders(status: str | None = "pending", limit: int = 50) -> list[dict]:
    db = await get_db()
    if status:
        cursor = await db.execute(
            "SELECT * FROM reminders WHERE status = ? ORDER BY due_at LIMIT ?", (status, limit))
    else:
        cursor = await db.execute("SELECT * FROM reminders ORDER BY due_at DESC LIMIT ?", (limit,))
    return [_reminder_row(r) for r in await cursor.fetchall()]


async def due_reminders(now_iso: str) -> list[dict]:
    """Pending reminders that are due and have not been fired yet (so none refires)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM reminders WHERE status = 'pending' AND fired_at IS NULL AND due_at <= ? "
        "ORDER BY due_at", (now_iso,))
    return [_reminder_row(r) for r in await cursor.fetchall()]


async def mark_reminder_fired(reminder_id: int, fired_at_iso: str) -> None:
    db = await get_db()
    await db.execute("UPDATE reminders SET fired_at = ? WHERE id = ?", (fired_at_iso, reminder_id))
    await db.commit()


async def mark_reminder(reminder_id: int, status: str) -> bool:
    """status: done | cancelled | pending."""
    db = await get_db()
    cursor = await db.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))
    await db.commit()
    return cursor.rowcount > 0


async def snooze_reminder(reminder_id: int, new_due_iso: str) -> bool:
    """Re-arm: pending again at new_due, remembering the time it was snoozed from."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE reminders SET status = 'pending', snoozed_from = due_at, due_at = ?, fired_at = NULL "
        "WHERE id = ?", (new_due_iso, reminder_id))
    await db.commit()
    return cursor.rowcount > 0


# ── Urgent alerts ──────────────────────────────────────────────────

def _alert_row(r) -> dict:
    try:
        meta = json.loads(r["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {"source": r["source"], "source_id": r["source_id"], "token": r["token"],
            "title": r["title"] or "", "category": r["category"] or "", "reason": r["reason"] or "",
            "meta": meta, "sent_at": r["sent_at"]}


async def alert_was_sent(source: str, source_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM alerts_sent WHERE source = ? AND source_id = ?", (source, source_id))
    return (await cursor.fetchone()) is not None


async def record_alert(source: str, source_id: str, token: str, title: str,
                       category: str, reason: str, meta: dict) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO alerts_sent (source, source_id, token, title, category, reason, meta, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_id, token, title, category, reason, json.dumps(meta or {}),
         datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def get_alert_by_token(token: str) -> dict | None:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM alerts_sent WHERE token = ?", (token,))
    row = await cursor.fetchone()
    return _alert_row(row) if row else None


async def recent_alerts(limit: int = 5) -> list[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM alerts_sent ORDER BY sent_at DESC LIMIT ?", (limit,))
    return [_alert_row(r) for r in await cursor.fetchall()]


# ── Waiting-on tracker ─────────────────────────────────────────────

async def dismiss_waiting(kind: str, key: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO waiting_on_dismissed (kind, key, dismissed_at) VALUES (?, ?, ?)",
        (kind, key, datetime.now(timezone.utc).isoformat()),
    )
    await db.commit()


async def dismissed_waiting() -> set[tuple[str, str]]:
    db = await get_db()
    cursor = await db.execute("SELECT kind, key FROM waiting_on_dismissed")
    return {(r["kind"], r["key"]) for r in await cursor.fetchall()}


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

def _contact_row(c: dict) -> tuple:
    """Accept merged records ({emails, phones, channels}) and legacy ones ({email})."""
    emails = [e.lower() for e in (c.get("emails") or ([c["email"]] if c.get("email") else [])) if e]
    phones = [p for p in (c.get("phones") or []) if p]
    channels = list(c.get("channels") or (["gmail"] if emails and not c.get("channels") else []))
    return (
        c.get("name", "") or "",
        emails[0] if emails else "",
        json.dumps(emails),
        json.dumps(phones),
        json.dumps(channels),
        int(c.get("count", 0) or 0),
        c.get("last_seen", "") or "",
    )


async def replace_contacts(contacts: list[dict]) -> None:
    """Atomically replace the contacts table (full rebuild from the index + Contacts)."""
    db = await get_db()
    await db.execute("DELETE FROM contacts")
    await db.executemany(
        "INSERT INTO contacts (name, email, emails, phones, channels, count, last_seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [_contact_row(c) for c in contacts],
    )
    await db.commit()


def _row_to_contact(r) -> dict:
    def _load(s):
        try:
            return json.loads(s or "[]")
        except json.JSONDecodeError:
            return []
    return {
        "id": r["id"], "name": r["name"] or "", "email": r["email"] or "",
        "emails": _load(r["emails"]), "phones": _load(r["phones"]),
        "channels": _load(r["channels"]), "count": r["count"], "last_seen": r["last_seen"] or "",
    }


async def search_contacts(query: str, limit: int = 5) -> list[dict]:
    """Match on name, any email, or any phone (digits of the query, 4+ of them)."""
    import re

    db = await get_db()
    like = f"%{query.lower()}%"
    where = "lower(name) LIKE ? OR lower(emails) LIKE ?"
    params: list = [like, like]
    digits = re.sub(r"\D", "", query)
    if len(digits) >= 4:
        where += " OR phones LIKE ?"
        params.append(f"%{digits}%")
    cursor = await db.execute(
        f"SELECT * FROM contacts WHERE {where} ORDER BY count DESC, name LIMIT ?",
        (*params, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_contact(r) for r in rows]


async def list_contacts(limit: int = 500) -> list[dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM contacts ORDER BY count DESC, name LIMIT ?", (limit,))
    return [_row_to_contact(r) for r in await cursor.fetchall()]


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

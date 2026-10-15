"""
Waiting-on tracker (Phase 10): things I sent that nobody answered.

Computed from the search index only — no live Gmail or Messages calls:
  - Gmail: a thread whose newest indexed message is from one of my addresses, older
    than WAITING_ON_DAYS, and addressed to a real person (not a noreply address).
  - iMessage: a 1:1 conversation window whose last line is mine and whose last
    activity is older than WAITING_ON_DAYS.

Items the owner dismissed live in waiting_on_dismissed. A Nudge drafts the follow-up
as a normal pending action (reply_email / send_imessage) that goes through the
approval card — nothing here sends anything.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from email.utils import getaddresses

import config
import db
import search_index

logger = logging.getLogger(__name__)

MAX_ITEMS = 20
_ME_LINE_RE = re.compile(r"^\[[^\]]+\] Me: ", re.M)
_NOREPLY_RE = re.compile(r"no.?reply|donotreply|notifications?@|mailer-daemon|bounce", re.I)


def token(item: dict) -> str:
    """Short stable id for callback data (Telegram caps callback_data at 64 bytes)."""
    return hashlib.sha1(f"{item['kind']}|{item['key']}".encode()).hexdigest()[:12]


def _days_between(now: datetime, then: datetime) -> float:
    return (now - then).total_seconds() / 86400


async def _my_addresses() -> set[str]:
    try:
        import llm
        accounts = await llm._get_accounts_cached()
    except Exception:
        return set()
    return {a["email"].lower() for a in accounts if a.get("email")}


def _recipient_addresses(meta: dict, mine: set[str]) -> list[str]:
    out = []
    for field in ("to", "cc"):
        for _, addr in getaddresses([meta.get(field, "") or ""]):
            a = addr.lower()
            if "@" in a and a not in mine and not _NOREPLY_RE.search(a):
                out.append(a)
    return out


async def _email_items(now: datetime, days: int, mine: set[str]) -> list[dict]:
    if not mine:
        return []
    from search_index import _parse_item_date

    conn = await search_index.get_db()
    cursor = await conn.execute("SELECT source_id, title, metadata FROM search_index WHERE source = 'gmail'")
    threads: dict[tuple[str, str], list[dict]] = {}
    for r in await cursor.fetchall():
        try:
            meta = json.loads(r["metadata"] or "{}")
        except json.JSONDecodeError:
            continue
        dt = _parse_item_date(meta)
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        account = meta.get("account") or r["source_id"].split(":", 1)[0]
        tkey = (account, meta.get("thread_id") or r["source_id"])
        threads.setdefault(tkey, []).append({
            "source_id": r["source_id"], "title": r["title"] or "(no subject)",
            "meta": meta, "date": dt,
        })

    items = []
    for (account, thread_id), msgs in threads.items():
        msgs.sort(key=lambda m: m["date"])
        last = msgs[-1]
        from_addrs = {a.lower() for _, a in getaddresses([last["meta"].get("from", "") or ""])}
        if not (from_addrs & mine):
            continue
        age = _days_between(now, last["date"])
        if age < days:
            continue
        recipients = _recipient_addresses(last["meta"], mine)
        if not recipients:
            continue
        email_id = last["source_id"].split(":", 1)[-1]
        items.append({
            "kind": "email", "key": f"{account}:{thread_id}",
            "email_id": email_id, "account": account,
            "to": recipients[0], "subject": last["title"],
            "date": last["date"].isoformat(), "days_waiting": int(age),
        })
    return items


async def _imessage_items(now: datetime, days: int) -> list[dict]:
    conn = await search_index.get_db()
    cursor = await conn.execute(
        "SELECT source_id, content, metadata FROM search_index WHERE source = 'imessage'")
    latest: dict[str, dict] = {}
    for r in await cursor.fetchall():
        try:
            meta = json.loads(r["metadata"] or "{}")
        except json.JSONDecodeError:
            continue
        if meta.get("is_group"):
            continue
        try:
            dt = datetime.fromisoformat(meta.get("date", ""))
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        key = meta.get("chat_key") or r["source_id"].split("|", 1)[0]
        cur = latest.get(key)
        if cur is None or dt > cur["date"]:
            latest[key] = {"date": dt, "content": r["content"] or "", "meta": meta}

    items = []
    for key, w in latest.items():
        lines = [ln for ln in w["content"].splitlines() if ln.strip()]
        if not lines or not _ME_LINE_RE.match(lines[-1]):
            continue
        age = _days_between(now, w["date"])
        if age < days:
            continue
        handle = w["meta"].get("handle") or w["meta"].get("contact") or ""
        items.append({
            "kind": "imessage", "key": key, "chat_key": key,
            "handle": handle, "contact": w["meta"].get("contact") or handle,
            "last_line": lines[-1][:120],
            "date": w["date"].isoformat(), "days_waiting": int(age),
        })
    return items


async def list_waiting(now: datetime | None = None, days: int | None = None,
                       my_addresses: set[str] | None = None) -> list[dict]:
    """Everything still unanswered, oldest first, minus dismissed items, capped at MAX_ITEMS."""
    now = now or datetime.now(timezone.utc)
    days = config.WAITING_ON_DAYS if days is None else days
    mine = my_addresses if my_addresses is not None else await _my_addresses()
    dismissed = await db.dismissed_waiting()

    items = await _email_items(now, days, mine) + await _imessage_items(now, days)
    items = [it for it in items if (it["kind"], it["key"]) not in dismissed]
    items.sort(key=lambda it: (-it["days_waiting"], it["date"]))
    for it in items:
        it["token"] = token(it)
    return items[:MAX_ITEMS]


def describe(item: dict) -> str:
    d = item["days_waiting"]
    if item["kind"] == "email":
        return f"✉️ {item['to']} — \"{item['subject'][:60]}\" ({d}d, {item['account']})"
    return f"💬 {item['contact']} — \"{item['last_line'][:60]}\" ({d}d)"


def format_waiting(items: list[dict], days: int | None = None) -> str:
    days = config.WAITING_ON_DAYS if days is None else days
    if not items:
        return f"Nothing waiting on a reply (no sent email or text unanswered for {days}+ days)."
    return (f"Waiting on a reply ({len(items)}, no answer after {days}+ days):\n"
            + "\n".join(f"- {describe(it)}" for it in items))


async def find_by_token(tok: str) -> dict | None:
    for it in await list_waiting():
        if it["token"] == tok:
            return it
    return None


async def build_nudge_action(item: dict) -> dict:
    """Pending action for a follow-up: reply_email or send_imessage. Never sends."""
    import agent_tools
    import llm

    if item["kind"] == "email":
        body = await llm.draft_followup(item["subject"], item["to"], item["days_waiting"], channel="email")
        return await agent_tools.build_reply_action(item["email_id"], item["account"], body)
    body = await llm.draft_followup(item.get("last_line", ""), item["contact"], item["days_waiting"],
                                    channel="text")
    return agent_tools.build_imessage_action(item["handle"], item["contact"], body)

"""
Urgent inbound alerts (Phase 10).

The alerts job runs a light inbound sync every ALERT_POLL_MINUTES, drains
`indexer.new_items`, keeps only inbound items (not from me, not my own last line),
and triages them: a watch-list hit (memories saved as "ALERT: …" / "always alert …")
is urgent outright; everything else goes to OPENAI_FAST_MODEL in batches with a
strict JSON schema. Urgent items are pushed to Telegram once (alerts_sent dedupes)
with ✍️ Reply / 👀 Seen buttons; during quiet hours they're held and flushed later.

Reply opens a normal approval card (reply_email / send_imessage via the shared
builders). Nothing here sends anything to anyone but the owner.
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
import llm
from telegram_format import tg_retry

logger = logging.getLogger(__name__)

CATEGORIES = ("emergency", "payment", "legal", "moveout", "watchlist", "routine")
CB_PREFIX = "alert_"
BATCH = 10
_ME_LINE_RE = re.compile(r"^\[[^\]]+\] Me: ")
_WATCH_PREFIX_RE = re.compile(r"^\s*(?:ALERT:|always alert(?: me)?(?: about| on| for| when)?)\s*", re.I)
_STOP = {"the", "a", "an", "and", "or", "me", "about", "any", "anything", "from", "if", "when", "emails",
         "email", "texts", "text", "messages", "message", "mentions", "mention"}

# Urgent items received during quiet hours, flushed afterwards (or by the digest).
held: list[tuple[dict, dict]] = []


# ── Quiet hours ────────────────────────────────────────────────────

def quiet_window() -> tuple[int, int] | None:
    raw = (config.ALERT_QUIET_HOURS or "").strip()
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", raw)
    if not m:
        return None
    a, b = int(m.group(1)) % 24, int(m.group(2)) % 24
    return None if a == b else (a, b)


def in_quiet_hours(now: datetime | None = None) -> bool:
    win = quiet_window()
    if win is None:
        return False
    start, end = win
    hour = (now or datetime.now()).astimezone().hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight, e.g. 23-7


# ── Inbound filter ─────────────────────────────────────────────────

def is_inbound(item: dict, mine: set[str]) -> bool:
    meta = item.get("meta") or {}
    if item["source"] == "gmail":
        from email.utils import getaddresses
        froms = {a.lower() for _, a in getaddresses([meta.get("from", "") or ""]) if "@" in a}
        return bool(froms) and not (froms & mine)
    if item["source"] in ("imessage", "whatsapp"):
        return not _ME_LINE_RE.match(item.get("snippet", "") or "")
    return False


def token_for(item: dict) -> str:
    return hashlib.sha1(f"{item['source']}|{item['source_id']}".encode()).hexdigest()[:12]


def sender_label(item: dict) -> str:
    meta = item.get("meta") or {}
    if item["source"] == "gmail":
        return (meta.get("from") or "?")[:60]
    return (meta.get("contact") or meta.get("handle") or "?")[:60]


# ── Watch-list from memories ───────────────────────────────────────

def watch_terms_from_memories(memories: list[dict]) -> list[str]:
    """'ALERT: Unit 4, Mike Plumber' / 'always alert me about leaks' → ['unit 4', 'mike plumber', 'leaks']."""
    terms: list[str] = []
    for m in memories:
        text = (m.get("content") or "").strip()
        if not _WATCH_PREFIX_RE.match(text):
            continue
        rest = _WATCH_PREFIX_RE.sub("", text, count=1)
        for part in re.split(r",|;| or |\bor\b|\n", rest):
            t = part.strip(" .!").lower()
            if t and t not in _STOP and len(t) >= 3:
                terms.append(t)
    return terms


async def watch_terms() -> list[str]:
    try:
        return watch_terms_from_memories(await db.list_memories(limit=100))
    except Exception as e:
        logger.warning(f"watch-list unavailable: {e}")
        return []


def watch_hit(item: dict, terms: list[str]) -> str | None:
    hay = f"{item.get('title', '')} {item.get('snippet', '')} {sender_label(item)}".lower()
    for t in terms:
        if t in hay:
            return t
    return None


# ── Classification ─────────────────────────────────────────────────

_SCHEMA = {
    "name": "urgency_verdicts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {"type": "integer"},
                        "urgent": {"type": "boolean"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "urgent", "category", "reason"],
                },
            }
        },
        "required": ["verdicts"],
    },
}


def _routine(reason: str = "") -> dict:
    return {"urgent": False, "category": "routine", "reason": reason}


async def _classify_with_model(items: list[dict]) -> list[dict]:
    """One fast-model call for up to BATCH items; routine for anything it doesn't return."""
    lines = []
    for i, it in enumerate(items):
        lines.append(f"{i}. [{it['source']}] from {sender_label(it)} — {it.get('title', '')[:100]}\n"
                     f"   {it.get('snippet', '')[:300]}")
    prompt = (
        "You triage inbound email and texts for a landlord / property manager. Mark an item URGENT "
        "only if it needs action today: emergency (leak, flood, no heat/hot water, fire, break-in, "
        "lockout, gas smell), payment (failed/late rent, bounced payment, chargeback), legal "
        "(notice, court, lawyer, inspection order, council/HOA violation), moveout (notice to vacate, "
        "lease termination). Newsletters, receipts, marketing, routine scheduling and chit-chat are "
        "routine. Return a verdict for EVERY index.\n\n" + "\n".join(lines)
    )
    response = await llm.with_retry(lambda: llm._client().chat.completions.create(
        model=config.OPENAI_FAST_MODEL,
        max_completion_tokens=1200,
        response_format={"type": "json_schema", "json_schema": _SCHEMA},
        messages=[{"role": "user", "content": prompt}],
    ), "triage")
    await llm.record_usage("triage", config.OPENAI_FAST_MODEL, getattr(response, "usage", None))
    data = json.loads(response.choices[0].message.content or "{}")
    out = [_routine("no verdict") for _ in items]
    for v in data.get("verdicts", []):
        i = v.get("index")
        if isinstance(i, int) and 0 <= i < len(items):
            cat = v.get("category") if v.get("category") in CATEGORIES else "routine"
            out[i] = {"urgent": bool(v.get("urgent")) and cat != "routine",
                      "category": cat, "reason": (v.get("reason") or "")[:200]}
    return out


async def classify(items: list[dict], terms: list[str] | None = None) -> list[dict]:
    """Verdict per item: watch-list hits first, the rest via the fast model in batches."""
    terms = watch_terms_from_memories([]) if terms is None else terms
    verdicts: list[dict | None] = [None] * len(items)
    pending: list[int] = []
    for i, it in enumerate(items):
        hit = watch_hit(it, terms)
        if hit:
            verdicts[i] = {"urgent": True, "category": "watchlist", "reason": f"watch-list: {hit}"}
        else:
            pending.append(i)
    for start in range(0, len(pending), BATCH):
        idxs = pending[start:start + BATCH]
        try:
            results = await _classify_with_model([items[i] for i in idxs])
        except Exception as e:
            logger.warning(f"urgency triage failed, treating batch as routine: {e}")
            results = [_routine("triage unavailable") for _ in idxs]
        for i, v in zip(idxs, results):
            verdicts[i] = v
    return [v or _routine() for v in verdicts]


# ── Push ───────────────────────────────────────────────────────────

_ICON = {"emergency": "🚨", "payment": "💸", "legal": "⚖️", "moveout": "📦", "watchlist": "👁️", "routine": "•"}


def format_alert(item: dict, verdict: dict) -> str:
    cat = verdict.get("category", "routine")
    src = {"gmail": "Email", "imessage": "Text", "whatsapp": "WhatsApp"}.get(item["source"], item["source"])
    head = f"{_ICON.get(cat, '🚨')} {cat.upper()} · {src} from {sender_label(item)}"
    title = item.get("title", "")
    snippet = (item.get("snippet", "") or "")[:280]
    reason = verdict.get("reason", "")
    parts = [head]
    if title and item["source"] == "gmail":
        parts.append(title[:120])
    if snippet:
        parts.append(snippet)
    if reason:
        parts.append(f"— {reason}")
    return "\n".join(parts)


def buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ Reply", callback_data=f"{CB_PREFIX}reply:{token}"),
        InlineKeyboardButton("👀 Seen", callback_data=f"{CB_PREFIX}seen:{token}"),
    ]])


async def push(bot, item: dict, verdict: dict, chat_id: int | None = None) -> bool:
    chat_id = chat_id or config.TELEGRAM_AUTHORIZED_USER_ID
    tok = token_for(item)
    try:
        await tg_retry(lambda: bot.send_message(chat_id, format_alert(item, verdict), reply_markup=buttons(tok)))
    except Exception as e:
        logger.error(f"alert push failed for {item['source']}:{item['source_id']}: {e}")
        return False
    await db.record_alert(item["source"], item["source_id"], tok, item.get("title", ""),
                          verdict.get("category", ""), verdict.get("reason", ""), item.get("meta") or {})
    return True


async def process(bot, items: list[dict], mine: set[str] | None = None,
                  now: datetime | None = None, chat_id: int | None = None) -> int:
    """Filter → classify → push (or hold in quiet hours). Returns the number pushed."""
    if not items:
        return await flush_held(bot, now, chat_id)
    if mine is None:
        import waiting_on
        mine = await waiting_on._my_addresses()

    candidates = []
    for it in items:
        if not is_inbound(it, mine):
            continue
        if await db.alert_was_sent(it["source"], it["source_id"]):
            continue
        candidates.append(it)
    pushed = 0
    if candidates:
        verdicts = await classify(candidates, await watch_terms())
        for it, v in zip(candidates, verdicts):
            if not v.get("urgent"):
                continue
            if in_quiet_hours(now):
                held.append((it, v))
                continue
            if await push(bot, it, v, chat_id):
                pushed += 1
    pushed += await flush_held(bot, now, chat_id)
    if pushed:
        logger.info(f"Urgent alerts pushed: {pushed}")
    return pushed


async def flush_held(bot, now: datetime | None = None, chat_id: int | None = None,
                     force: bool = False) -> int:
    if not held or (in_quiet_hours(now) and not force):
        return 0
    pushed = 0
    for it, v in list(held):
        held.remove((it, v))
        if await db.alert_was_sent(it["source"], it["source_id"]):
            continue
        if await push(bot, it, v, chat_id):
            pushed += 1
    return pushed


# ── Reply button → approval card ──────────────────────────────────

async def build_reply_action(token: str) -> dict:
    """Pending reply_email / send_imessage action for an alert. Raises with a user-facing message."""
    import agent_tools

    row = await db.get_alert_by_token(token)
    if row is None:
        raise ValueError("That alert is no longer on file.")
    meta = row["meta"] or {}
    if row["source"] == "gmail":
        email_id = row["source_id"].split(":", 1)[-1]
        account = meta.get("account") or row["source_id"].split(":", 1)[0]
        body = await llm.draft_acknowledgement(row["title"], meta.get("from", ""), channel="email")
        return await agent_tools.build_reply_action(email_id, account, body)
    if meta.get("is_group"):
        raise ValueError("Replying to a group chat isn't supported yet — use the Messages app.")
    handle = meta.get("handle") or meta.get("contact")
    if not handle:
        raise ValueError("No handle on file for this text.")
    body = await llm.draft_acknowledgement(row["title"], meta.get("contact", handle), channel="text")
    return agent_tools.build_imessage_action(handle, meta.get("contact", ""), body)

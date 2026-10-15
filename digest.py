"""
Digest 2.0 (Phase 10): the morning briefing and the optional evening wrap-up.

Each section is gathered by its own function and wrapped so one failing source
(Gmail token expired, calendar not available) never kills the digest. `run()`
composes the sections with the fast model, sends the text, then the waiting-on
cards, then any urgent alerts held during quiet hours.

Calendar is an optional source: `calendar_module` (the Mac EventKit task) just has
to expose `is_available()` and `list_events(after, before)` returning
[{start, end, title, location}] with ISO strings; until it exists the section is
skipped silently.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import config
import db

logger = logging.getLogger(__name__)

Section = tuple[str, str]

HEADERS = {"morning": "☀️ **Morning digest**", "evening": "🌙 **Evening wrap-up**"}


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_local_tz()).strftime("%H:%M")
    except (TypeError, ValueError):
        return (iso or "")[11:16]


async def _safe(name: str, coro):
    try:
        return await coro
    except Exception as e:
        logger.warning(f"Digest section '{name}' failed: {e}")
        return None


# ── Sections ───────────────────────────────────────────────────────

async def section_calendar(now: datetime) -> Section | None:
    try:
        import calendar_module  # noqa: F401 — provided by the Mac EventKit task
    except ImportError:
        return None
    if not getattr(calendar_module, "is_available", lambda: False)():
        return None
    local = now.astimezone(_local_tz())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    events = await asyncio.to_thread(calendar_module.list_events, start.isoformat(), end.isoformat())
    if not events:
        return ("TODAY'S CALENDAR", "nothing scheduled")
    lines = []
    for ev in events:
        when = _fmt_time(ev.get("start", ""))
        loc = f" @ {ev['location']}" if ev.get("location") else ""
        lines.append(f"- {when} {ev.get('title', '(untitled)')}{loc}")
    return ("TODAY'S CALENDAR", "\n".join(lines))


async def section_reminders(now: datetime, kind: str) -> Section | None:
    import reminders

    pending = await db.list_reminders("pending", limit=100)
    local_now = now.astimezone(_local_tz())
    if kind == "evening":
        day_start = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        title = "REMINDERS DUE TOMORROW"
    else:
        day_start = None  # everything overdue counts
        day_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        title = "REMINDERS (due today or overdue)"
    lines = []
    for r in pending:
        try:
            due = datetime.fromisoformat(r["due_at"]).astimezone(_local_tz())
        except (TypeError, ValueError):
            continue
        if due >= day_end or (day_start is not None and due < day_start):
            continue
        tag = " (overdue)" if due < local_now and kind == "morning" else ""
        lines.append(f"- {reminders.describe(r)}{tag}")
    return (title, "\n".join(lines)) if lines else None


async def section_alerts(now: datetime, kind: str) -> Section | None:
    import alerts

    lines = []
    if alerts.held:
        lines.append(f"- {len(alerts.held)} urgent item(s) held during quiet hours — being sent with this digest")
    if kind == "evening":
        cutoff = now.astimezone(_local_tz()).replace(
            hour=config.DIGEST_HOUR, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        title = "URGENT ALERTS SINCE THIS MORNING"
    else:
        cutoff = now - timedelta(hours=24)
        title = "URGENT ALERTS (last 24h)"
    for a in await db.recent_alerts(limit=10):
        try:
            sent = datetime.fromisoformat(a["sent_at"])
        except (TypeError, ValueError):
            continue
        if sent >= cutoff:
            lines.append(f"- {a['category']}: {a['title'][:70]}")
    return (title, "\n".join(lines)) if lines else None


async def section_waiting() -> tuple[Section | None, list[dict]]:
    import waiting_on

    items = await waiting_on.list_waiting()
    if not items:
        return None, []
    text = "\n".join(f"- {waiting_on.describe(it)}" for it in items[:8])
    return (f"WAITING ON (sent by me, no reply after {config.WAITING_ON_DAYS}+ days)", text), items


async def section_unread() -> Section | None:
    import gmail_module
    import llm

    blocks = []
    counts = []
    for a in await llm._get_accounts_cached():
        try:
            unread = await asyncio.to_thread(
                gmail_module.list_emails, max_results=15,
                query="is:unread newer_than:1d", account=a["label"],
            )
        except Exception:
            blocks.append(f"[{a['label']}] unavailable — Gmail needs reconnecting (/addaccount {a['label']})")
            continue
        n = f"{len(unread)}+" if len(unread) >= 15 else str(len(unread))
        counts.append(f"{a['label']}: {n} unread")
        if unread:
            blocks.append(f"[{a['label']}]\n" + "\n".join(
                f"- {e['from'][:45]}: {e['subject'][:70]}" for e in unread))
    if not blocks and not counts:
        return None
    head = ("Inbox: " + ", ".join(counts) + "\n") if counts else ""
    return ("UNREAD (last 24h)", head + "\n".join(blocks))


async def section_reviews(now: datetime) -> Section | None:
    cutoff = now.timestamp() - 86400
    recent = [r for r in await db.get_recent_reviews(limit=20) if (r.get("time") or 0) > cutoff]
    if not recent:
        return None
    lines = [f"- {r['rating']}★ by {r['author']}: {(r['text'] or '')[:100]}" for r in recent]
    return ("NEW REVIEWS (24h)", "\n".join(lines))


async def section_pending() -> Section | None:
    conn = await db.get_db()
    cursor = await conn.execute(
        "SELECT id, action_type, payload FROM pending_actions WHERE status = 'pending' ORDER BY id DESC LIMIT 5"
    )
    rows = await cursor.fetchall()
    if not rows:
        return None
    lines = []
    for r in rows:
        p = json.loads(r["payload"])
        what = p.get("subject") or p.get("body", "")[:60]
        lines.append(f"- {r['action_type']} to {p.get('to_name') or p.get('to', '?')}: {what[:60]}")
    return ("PENDING DRAFTS (still awaiting your Approve/Reject)", "\n".join(lines))


# ── Assembly ───────────────────────────────────────────────────────

async def build_sections(kind: str = "morning", now: datetime | None = None
                         ) -> tuple[list[Section], list[dict]]:
    """Ordered (title, text) sections for the digest, plus the waiting-on items for cards."""
    now = now or _now()
    waiting_items: list[dict] = []
    sections: list[Section] = []

    async def _waiting():
        nonlocal waiting_items
        sec, waiting_items = await section_waiting()
        return sec

    if kind == "evening":
        plan = [
            ("waiting", _waiting()),
            ("reminders", section_reminders(now, "evening")),
            ("alerts", section_alerts(now, "evening")),
            ("pending", section_pending()),
        ]
    else:
        plan = [
            ("calendar", section_calendar(now)),
            ("reminders", section_reminders(now, "morning")),
            ("alerts", section_alerts(now, "morning")),
            ("waiting", _waiting()),
            ("unread", section_unread()),
            ("reviews", section_reviews(now)),
            ("pending", section_pending()),
        ]
    for name, coro in plan:
        sec = await _safe(name, coro)
        if sec:
            sections.append(sec)
    return sections, waiting_items


def render(sections: list[Section]) -> str:
    return "\n\n".join(f"{title}:\n{text}" for title, text in sections)


async def run(bot, kind: str = "morning", chat_id: int | None = None, now: datetime | None = None,
              waiting_cards=None) -> bool:
    """Compose and send the digest. Returns False (and sends nothing) when there's nothing to say."""
    import alerts
    import llm
    from telegram.error import BadRequest
    from telegram_format import markdown_to_telegram_html, tg_retry

    chat_id = chat_id or config.TELEGRAM_AUTHORIZED_USER_ID
    sections, waiting = await build_sections(kind, now)
    if not sections:
        logger.info(f"{kind} digest: nothing to report, staying quiet")
        return False

    raw = render(sections)
    try:
        text = await llm.compose_digest(raw, kind)
    except Exception as e:
        logger.error(f"Digest compose failed: {e}")
        text = raw
    body = f"{HEADERS.get(kind, HEADERS['morning'])}\n\n{text}"
    try:
        await tg_retry(lambda: bot.send_message(chat_id, markdown_to_telegram_html(body),
                                                parse_mode="HTML", disable_web_page_preview=True))
    except BadRequest:
        await tg_retry(lambda: bot.send_message(chat_id, body))

    if waiting and waiting_cards is not None:
        try:
            await waiting_cards(bot, chat_id, waiting, limit=5)
        except Exception as e:
            logger.warning(f"Digest waiting cards failed: {e}")

    try:
        await alerts.flush_held(bot, chat_id=chat_id, force=True)
    except Exception as e:
        logger.warning(f"Digest alert flush failed: {e}")
    return True

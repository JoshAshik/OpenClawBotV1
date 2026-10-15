"""
Reminders (Phase 10): "remind me Thursday to chase the deposit".

The model turns the user's words into an absolute time (the system prompt carries the
current local date/time); `parse_when` validates it. A bot job calls `fire_due` every
minute, which sends "⏰ Reminder: …" to the owner with Snooze 1h / Tomorrow / Done
buttons. Reminders are NOT outbound actions — they only ever message the owner — so
they execute directly and never go through the approval flow.

Times are stored as UTC ISO strings and shown in local time.
"""

import logging
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
import db
from telegram_format import tg_retry

logger = logging.getLogger(__name__)

PAST_GRACE = timedelta(seconds=60)

# Callback data: rem_<kind>:<id>
CB_PREFIX = "rem_"
SNOOZE_KINDS = ("snooze1h", "tomorrow")


def _local_tz():
    return datetime.now().astimezone().tzinfo


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_when(when: str, now: datetime | None = None) -> datetime:
    """
    ISO date/time → aware UTC datetime. A naive time is local; a date alone means
    REMINDER_HOUR local that day. Raises ValueError for garbage or past times.
    """
    raw = (when or "").strip()
    if not raw:
        raise ValueError("no time given")
    now = now or now_utc()
    try:
        if len(raw) == 10:  # YYYY-MM-DD
            d = datetime.fromisoformat(raw)
            dt = d.replace(hour=config.REMINDER_HOUR, minute=0, second=0, microsecond=0,
                           tzinfo=_local_tz())
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_local_tz())
    except ValueError as e:
        raise ValueError(f"'{raw}' is not an ISO date/time (use YYYY-MM-DDTHH:MM)") from e
    dt = dt.astimezone(timezone.utc)
    if dt < now - PAST_GRACE:
        raise ValueError(f"{fmt_local(dt)} is in the past (now is {fmt_local(now)})")
    return dt


def fmt_local(dt: datetime | str | None) -> str:
    if not dt:
        return "?"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    return dt.astimezone(_local_tz()).strftime("%a %Y-%m-%d %H:%M")


def snooze_target(kind: str, now: datetime | None = None) -> datetime:
    """'snooze1h' → now + 1h; 'tomorrow' → next day at REMINDER_HOUR local. UTC result."""
    now = now or now_utc()
    if kind == "snooze1h":
        return now + timedelta(hours=1)
    if kind == "tomorrow":
        local = now.astimezone(_local_tz())
        target = (local + timedelta(days=1)).replace(
            hour=config.REMINDER_HOUR, minute=0, second=0, microsecond=0)
        return target.astimezone(timezone.utc)
    raise ValueError(f"unknown snooze kind {kind}")


def buttons(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏳ Snooze 1h", callback_data=f"{CB_PREFIX}snooze1h:{reminder_id}"),
        InlineKeyboardButton("🌅 Tomorrow", callback_data=f"{CB_PREFIX}tomorrow:{reminder_id}"),
        InlineKeyboardButton("✅ Done", callback_data=f"{CB_PREFIX}done:{reminder_id}"),
    ]])


def describe(r: dict) -> str:
    """One-line listing entry: '#3 · Fri 2026-10-17 09:00 — chase the deposit (fired)'."""
    tag = ""
    if r.get("status") == "pending" and r.get("fired_at"):
        tag = " (fired, awaiting Done/Snooze)"
    elif r.get("snoozed_from"):
        tag = " (snoozed)"
    return f"#{r['id']} · {fmt_local(r['due_at'])} — {r['text']}{tag}"


async def fire_due(bot, chat_id: int | None = None, now: datetime | None = None) -> int:
    """Send every due reminder once. Returns how many were sent."""
    now = now or now_utc()
    chat_id = chat_id or config.TELEGRAM_AUTHORIZED_USER_ID
    sent = 0
    for r in await db.due_reminders(now.isoformat()):
        try:
            await tg_retry(lambda: bot.send_message(
                chat_id, f"⏰ Reminder: {r['text']}", reply_markup=buttons(r["id"])))
        except Exception as e:
            logger.error(f"Reminder #{r['id']} send failed: {e}")
            continue
        await db.mark_reminder_fired(r["id"], now.isoformat())
        sent += 1
    if sent:
        logger.info(f"Reminders fired: {sent}")
    return sent


async def apply_button(kind: str, reminder_id: int, now: datetime | None = None) -> str:
    """Handle a button press; returns the text the card should now show."""
    r = await db.get_reminder(reminder_id)
    if r is None:
        return "That reminder no longer exists."
    if r["status"] != "pending":
        return f"Reminder #{reminder_id} is already {r['status']}: {r['text']}"
    if kind == "done":
        await db.mark_reminder(reminder_id, "done")
        return f"✅ Done: {r['text']}"
    if kind in SNOOZE_KINDS:
        target = snooze_target(kind, now)
        await db.snooze_reminder(reminder_id, target.isoformat())
        return f"⏳ Snoozed until {fmt_local(target)}: {r['text']}"
    return f"Unknown action '{kind}'."

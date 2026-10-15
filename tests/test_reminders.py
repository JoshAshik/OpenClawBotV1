"""Reminders: DB helpers, tools, the firing job, and the snooze/done buttons. No network."""

from datetime import datetime, timedelta, timezone

import pytest

import agent_tools
import db
import reminders
from tests.conftest import run_async

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


class FakeBot:
    def __init__(self, fail_ids=()):
        self.sent = []
        self.fail_ids = set(fail_ids)

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if any(f"rem_done:{i}" in str(reply_markup.to_dict()) for i in self.fail_ids):
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, reply_markup))


def _tool(name, args):
    return run_async(agent_tools.execute_tool(name, args))


# ── Time parsing ───────────────────────────────────────────────────

def test_parse_when_date_only_means_reminder_hour_local(monkeypatch):
    import config
    monkeypatch.setattr(config, "REMINDER_HOUR", 9)
    dt = reminders.parse_when("2026-10-17", now=NOW)
    local = dt.astimezone(datetime.now().astimezone().tzinfo)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 10, 17, 9, 0)
    assert dt.tzinfo == timezone.utc


def test_parse_when_naive_is_local_and_aware_is_kept():
    naive = reminders.parse_when("2026-10-17T09:30", now=NOW)
    local = naive.astimezone(datetime.now().astimezone().tzinfo)
    assert (local.hour, local.minute) == (9, 30)
    aware = reminders.parse_when("2026-10-17T09:30:00+02:00", now=NOW)
    assert aware == datetime(2026, 10, 17, 7, 30, tzinfo=timezone.utc)
    zulu = reminders.parse_when("2026-10-17T09:30:00Z", now=NOW)
    assert zulu == datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["", "next thursday", "2026-13-01", "2026-10-15T13:00:00Z"])
def test_parse_when_rejects_garbage_and_past(bad):
    with pytest.raises(ValueError):
        reminders.parse_when(bad, now=NOW)


def test_parse_when_allows_tiny_grace():
    assert reminders.parse_when((NOW - timedelta(seconds=30)).isoformat(), now=NOW)


def test_snooze_targets(monkeypatch):
    import config
    monkeypatch.setattr(config, "REMINDER_HOUR", 9)
    assert reminders.snooze_target("snooze1h", NOW) == NOW + timedelta(hours=1)
    tmrw = reminders.snooze_target("tomorrow", NOW).astimezone(datetime.now().astimezone().tzinfo)
    local_now = NOW.astimezone(datetime.now().astimezone().tzinfo)
    assert tmrw.date() == (local_now + timedelta(days=1)).date()
    assert (tmrw.hour, tmrw.minute) == (9, 0)
    with pytest.raises(ValueError):
        reminders.snooze_target("never", NOW)


# ── DB helpers ─────────────────────────────────────────────────────

def test_db_roundtrip_due_mark_snooze(index_db):
    async def go():
        a = await db.add_reminder("chase deposit", (NOW + timedelta(hours=1)).isoformat())
        b = await db.add_reminder("call roofer", (NOW - timedelta(minutes=5)).isoformat())
        c = await db.add_reminder("cancelled one", (NOW - timedelta(minutes=5)).isoformat())
        await db.mark_reminder(c, "cancelled")
        due = [r["id"] for r in await db.due_reminders(NOW.isoformat())]
        await db.mark_reminder_fired(b, NOW.isoformat())
        due_after_fire = await db.due_reminders(NOW.isoformat())
        pending = [r["id"] for r in await db.list_reminders("pending")]
        await db.snooze_reminder(b, (NOW + timedelta(hours=1)).isoformat())
        snoozed = await db.get_reminder(b)
        everything = await db.list_reminders(None)
        return a, b, due, due_after_fire, pending, snoozed, everything

    a, b, due, due_after_fire, pending, snoozed, everything = run_async(go())
    assert due == [b]                                   # future and cancelled excluded
    assert due_after_fire == []                         # fired → never refires
    assert pending == [b, a]                            # ordered by due_at
    assert snoozed["status"] == "pending" and snoozed["fired_at"] is None
    assert snoozed["snoozed_from"] == (NOW - timedelta(minutes=5)).isoformat()
    assert snoozed["due_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert len(everything) == 3


# ── Tools ──────────────────────────────────────────────────────────

def test_set_reminder_tool_sets_and_returns_entity(index_db, monkeypatch):
    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT10:15")
    out = _tool("set_reminder", {"when": future, "text": "chase the deposit"})
    assert out["result"].startswith("Reminder #1 set for ")
    assert "10:15: chase the deposit" in out["result"]
    assert out["entities"] == [{"type": "reminder", "id": "1",
                                "label": out["result"].split("set for ", 1)[1].rstrip(".")[:80]}]
    listed = _tool("list_reminders", {})
    assert "#1 ·" in listed["result"] and "chase the deposit" in listed["result"]
    assert listed["entities"][0]["type"] == "reminder"


def test_set_reminder_tool_rejects_past_and_blank(index_db):
    out = _tool("set_reminder", {"when": "2020-01-01T09:00", "text": "too late"})
    assert "in the past" in out["result"] and "entities" not in out
    out = _tool("set_reminder", {"when": "soonish", "text": "x"})
    assert "not an ISO date/time" in out["result"]
    out = _tool("set_reminder", {"when": "2099-01-01", "text": "  "})
    assert "needs `text`" in out["result"]
    assert run_async(db.list_reminders("pending")) == []


def test_cancel_reminder_tool(index_db):
    _tool("set_reminder", {"when": "2099-01-01", "text": "far future"})
    assert _tool("cancel_reminder", {"id": 1})["result"] == "Cancelled reminder #1: far future"
    assert "No pending reminder #1" in _tool("cancel_reminder", {"id": 1})["result"]
    assert "No pending reminder #99" in _tool("cancel_reminder", {"id": 99})["result"]
    assert "numeric id" in _tool("cancel_reminder", {"id": "x"})["result"]
    assert _tool("list_reminders", {})["result"] == "No pending reminders."


# ── Firing job + buttons ───────────────────────────────────────────

def test_fire_due_sends_once_and_only_due(index_db):
    bot = FakeBot()

    async def go():
        due = await db.add_reminder("call roofer", (NOW - timedelta(minutes=1)).isoformat())
        later = await db.add_reminder("later", (NOW + timedelta(hours=2)).isoformat())
        first = await reminders.fire_due(bot, chat_id=42, now=NOW)
        second = await reminders.fire_due(bot, chat_id=42, now=NOW)        # must not refire
        third = await reminders.fire_due(bot, chat_id=42, now=NOW + timedelta(hours=3))
        return due, later, first, second, third

    due, later, first, second, third = run_async(go())
    assert (first, second, third) == (1, 0, 1)
    assert [t for _, t, _ in bot.sent] == ["⏰ Reminder: call roofer", "⏰ Reminder: later"]
    assert bot.sent[0][0] == 42
    datas = [b.callback_data for row in bot.sent[0][2].inline_keyboard for b in row]
    assert datas == [f"rem_snooze1h:{due}", f"rem_tomorrow:{due}", f"rem_done:{due}"]


def test_fire_due_keeps_unsent_on_telegram_error(index_db):
    async def go():
        rid = await db.add_reminder("flaky", (NOW - timedelta(minutes=1)).isoformat())
        n = await reminders.fire_due(FakeBot(fail_ids=[rid]), chat_id=1, now=NOW)
        still_due = await db.due_reminders(NOW.isoformat())
        return n, [r["id"] for r in still_due]

    n, still = run_async(go())
    assert n == 0 and still == [1]           # not marked fired → retried next minute


def test_buttons_snooze_and_done(index_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "REMINDER_HOUR", 9)

    async def go():
        rid = await db.add_reminder("chase deposit", (NOW - timedelta(minutes=1)).isoformat())
        await reminders.fire_due(FakeBot(), chat_id=1, now=NOW)
        s1 = await reminders.apply_button("snooze1h", rid, now=NOW)
        r1 = await db.get_reminder(rid)
        refire_now = await reminders.fire_due(FakeBot(), chat_id=1, now=NOW)
        refire_later = await reminders.fire_due(FakeBot(), chat_id=1, now=NOW + timedelta(hours=1, minutes=1))
        s2 = await reminders.apply_button("tomorrow", rid, now=NOW)
        r2 = await db.get_reminder(rid)
        d = await reminders.apply_button("done", rid, now=NOW)
        again = await reminders.apply_button("done", rid, now=NOW)
        missing = await reminders.apply_button("done", 999, now=NOW)
        return s1, r1, refire_now, refire_later, s2, r2, d, again, missing

    s1, r1, refire_now, refire_later, s2, r2, d, again, missing = run_async(go())
    assert s1.startswith("⏳ Snoozed until ") and "chase deposit" in s1
    assert r1["due_at"] == (NOW + timedelta(hours=1)).isoformat() and r1["fired_at"] is None
    assert refire_now == 0 and refire_later == 1          # re-armed reminder fires again at the new time
    assert "09:00" in s2 and r2["status"] == "pending"
    assert d == "✅ Done: chase deposit"
    assert again.startswith("Reminder #1 is already done")
    assert missing == "That reminder no longer exists."


def test_describe_marks_fired_and_snoozed():
    base = {"id": 7, "due_at": NOW.isoformat(), "text": "x", "status": "pending",
            "fired_at": None, "snoozed_from": None}
    assert reminders.describe(base).startswith("#7 · ") and reminders.describe(base).endswith("— x")
    assert reminders.describe({**base, "fired_at": NOW.isoformat()}).endswith("(fired, awaiting Done/Snooze)")
    assert reminders.describe({**base, "snoozed_from": NOW.isoformat()}).endswith("(snoozed)")

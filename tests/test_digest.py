"""Digest 2.0: section gathering, resilience, optional calendar, and send order. No network."""

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import alerts
import db
import digest
import llm
import search_index
import waiting_on
from tests.conftest import run_async

ME = "me@example.com"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup, kw.get("parse_mode")))


@pytest.fixture
def seeded(index_db, monkeypatch):
    """Reminders, a waiting-on thread, held + recent alerts, unread mail, a review, a pending card."""
    import gmail_module

    now = datetime.now(timezone.utc)
    alerts.held.clear()
    monkeypatch.setitem(sys.modules, "calendar_module", None)      # no calendar yet
    monkeypatch.setattr(waiting_on, "_my_addresses", _coro_factory({ME}))

    async def accounts():
        return [{"label": "personal", "email": ME}, {"label": "work", "email": "w@example.com"}]

    def list_emails(max_results=15, query="", account=None):
        if account == "work":
            raise RuntimeError("invalid_grant")
        return [{"from": "Pat <pat@x.com>", "subject": "Rent question"},
                {"from": "news@x.com", "subject": "Deals"}]

    async def reviews(limit=20):
        return [{"rating": 2, "author": "Ann", "text": "Slow repairs", "time": now.timestamp() - 3600},
                {"rating": 5, "author": "Old", "text": "great", "time": now.timestamp() - 90000}]

    monkeypatch.setattr(llm, "_get_accounts_cached", accounts)
    monkeypatch.setattr(gmail_module, "list_emails", list_emails)
    monkeypatch.setattr(db, "get_recent_reviews", reviews)

    local = now.astimezone()
    tomorrow_10 = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)

    async def go():
        await db.add_reminder("overdue thing", (now - timedelta(days=2)).isoformat())
        await db.add_reminder("due soon", (now + timedelta(minutes=10)).isoformat())
        await db.add_reminder("tomorrow thing", tomorrow_10.astimezone(timezone.utc).isoformat())
        await db.add_reminder("far future", (now + timedelta(days=5)).isoformat())
        await search_index.index_item("gmail", "personal:a2", "Re: Roof flashing", "body", {
            "from": f"Josh <{ME}>", "to": "roofer@x.com", "account": "personal", "thread_id": "t1",
            "date": (now - timedelta(days=6)).isoformat()})
        await db.record_alert("gmail", "personal:z9", "tokz", "Leak in unit 2", "emergency", "water", {})
        await db.create_pending_action("send_email", {"to": "pat@x.com", "subject": "Lease renewal"})
    run_async(go())
    alerts.held.append(({"source": "gmail", "source_id": "personal:held1", "title": "Held leak",
                         "snippet": "water", "meta": {"from": "Pat <pat@x.com>"}},
                        {"urgent": True, "category": "emergency", "reason": "held"}))
    yield now
    alerts.held.clear()


def _coro_factory(value):
    async def f(*a, **k):
        return value
    return f


def _titles(sections):
    return [t for t, _ in sections]


# ── Sections ───────────────────────────────────────────────────────

def test_morning_sections_in_order(seeded):
    sections, waiting = run_async(digest.build_sections("morning", now=seeded))
    assert _titles(sections) == [
        "REMINDERS (due today or overdue)", "URGENT ALERTS (last 24h)",
        "WAITING ON (sent by me, no reply after 3+ days)", "UNREAD (last 24h)",
        "NEW REVIEWS (24h)", "PENDING DRAFTS (still awaiting your Approve/Reject)",
    ]
    text = dict(sections)
    rem = text["REMINDERS (due today or overdue)"]
    assert "overdue thing (overdue)" in rem and "due soon" in rem
    assert "far future" not in rem and "tomorrow thing" not in rem
    al = text["URGENT ALERTS (last 24h)"]
    assert "1 urgent item(s) held during quiet hours" in al and "emergency: Leak in unit 2" in al
    assert "roofer@x.com" in text["WAITING ON (sent by me, no reply after 3+ days)"]
    assert len(waiting) == 1 and waiting[0]["to"] == "roofer@x.com"
    un = text["UNREAD (last 24h)"]
    assert un.startswith("Inbox: personal: 2 unread\n")
    assert "[personal]\n- Pat <pat@x.com>: Rent question" in un
    assert "[work] unavailable — Gmail needs reconnecting (/addaccount work)" in un
    assert text["NEW REVIEWS (24h)"] == "- 2★ by Ann: Slow repairs"
    assert text["PENDING DRAFTS (still awaiting your Approve/Reject)"] == "- send_email to pat@x.com: Lease renewal"
    assert "TODAY'S CALENDAR" not in _titles(sections)


def test_evening_sections(seeded):
    sections, _ = run_async(digest.build_sections("evening", now=seeded))
    titles = _titles(sections)
    assert titles[0].startswith("WAITING ON")
    assert "REMINDERS DUE TOMORROW" in titles and "URGENT ALERTS SINCE THIS MORNING" in titles
    assert titles[-1].startswith("PENDING DRAFTS")
    assert not any(t.startswith(("UNREAD", "NEW REVIEWS", "TODAY'S CALENDAR")) for t in titles)
    rem = dict(sections)["REMINDERS DUE TOMORROW"]
    assert "tomorrow thing" in rem and "due soon" not in rem and "overdue" not in rem


def test_failing_section_is_skipped(seeded, monkeypatch):
    async def boom(now):
        raise RuntimeError("reviews api down")

    monkeypatch.setattr(digest, "section_reviews", boom)
    sections, _ = run_async(digest.build_sections("morning", now=seeded))
    titles = _titles(sections)
    assert "NEW REVIEWS (24h)" not in titles and "UNREAD (last 24h)" in titles and len(titles) == 5


def test_calendar_section_when_module_present(seeded, monkeypatch):
    local = seeded.astimezone()
    fake = SimpleNamespace(
        is_available=lambda: True,
        list_events=lambda a, b: [
            {"start": local.replace(hour=9, minute=0).isoformat(), "end": "", "title": "Inspection",
             "location": "12 Oak"},
            {"start": local.replace(hour=14, minute=30).isoformat(), "end": "", "title": "Call roofer"},
        ],
    )
    monkeypatch.setitem(sys.modules, "calendar_module", fake)
    sections, _ = run_async(digest.build_sections("morning", now=seeded))
    assert _titles(sections)[0] == "TODAY'S CALENDAR"
    assert dict(sections)["TODAY'S CALENDAR"] == "- 09:00 Inspection @ 12 Oak\n- 14:30 Call roofer"

    fake.is_available = lambda: False
    sections, _ = run_async(digest.build_sections("morning", now=seeded))
    assert "TODAY'S CALENDAR" not in _titles(sections)


def test_render():
    assert digest.render([("A", "- x"), ("B", "- y")]) == "A:\n- x\n\nB:\n- y"


# ── run(): compose → text → cards → held alerts ───────────────────

def test_run_sends_in_order(seeded, monkeypatch):
    composed = {}

    async def fake_compose(raw, kind="morning"):
        composed["raw"], composed["kind"] = raw, kind
        return "**SUMMARY** line"

    monkeypatch.setattr(llm, "compose_digest", fake_compose)
    bot = FakeBot()
    cards = []

    async def fake_cards(b, chat_id, items, limit=5):
        cards.append((chat_id, [it["to"] for it in items], limit))
        await b.send_message(chat_id, "CARD")

    ok = run_async(digest.run(bot, "morning", chat_id=9, now=seeded, waiting_cards=fake_cards))
    assert ok and composed["kind"] == "morning" and "WAITING ON" in composed["raw"]
    texts = [t for _, t, _, _ in bot.sent]
    assert texts[0].startswith("☀️ <b>Morning digest</b>") and bot.sent[0][3] == "HTML"
    assert texts[1] == "CARD" and cards == [(9, ["roofer@x.com"], 5)]
    assert texts[2].startswith("🚨 EMERGENCY · Email from Pat") and alerts.held == []
    assert len(bot.sent) == 3


def test_run_compose_failure_falls_back_to_raw(seeded, monkeypatch):
    async def boom(raw, kind="morning"):
        raise RuntimeError("api")

    monkeypatch.setattr(llm, "compose_digest", boom)
    bot = FakeBot()
    run_async(digest.run(bot, "evening", chat_id=9, now=seeded))
    assert bot.sent[0][1].startswith("🌙 <b>Evening wrap-up</b>")
    assert "WAITING ON" in bot.sent[0][1]


def test_run_stays_quiet_when_empty(index_db, monkeypatch):
    monkeypatch.setitem(sys.modules, "calendar_module", None)
    monkeypatch.setattr(waiting_on, "_my_addresses", _coro_factory(set()))
    monkeypatch.setattr(llm, "_get_accounts_cached", _coro_factory([]))
    monkeypatch.setattr(db, "get_recent_reviews", _coro_factory([]))
    alerts.held.clear()
    bot = FakeBot()
    assert run_async(digest.run(bot, "morning", chat_id=9)) is False
    assert bot.sent == []

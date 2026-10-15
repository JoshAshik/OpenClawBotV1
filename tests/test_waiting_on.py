"""Waiting-on tracker: detection from the index, dismissal, Nudge actions, cards. No network."""

from datetime import datetime, timezone

import pytest

import agent_tools
import db
import search_index
import waiting_on
from tests.conftest import run_async

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ME = "me@example.com"
MINE = {ME}


def _mail(source_id, thread, subject, frm, to, date, account="personal"):
    return search_index.index_item("gmail", source_id, subject, "body", {
        "from": frm, "to": to, "date": date, "account": account, "thread_id": thread,
    })


def _window(chat_key, content, date, contact="Mike Plumber", handle="+14155550101", is_group=False):
    return search_index.index_item("imessage", f"{chat_key}|{date}", f"iMessage with {contact}", content, {
        "contact": contact, "handle": handle, "chat_key": chat_key, "is_group": is_group, "date": date,
    })


@pytest.fixture
def seeded(index_db):
    async def go():
        # T1: they wrote, I replied 6 days ago, silence since → waiting
        await _mail("personal:a1", "t1", "Roof flashing", "Roofer <roofer@x.com>", ME, "2026-10-01T10:00:00+00:00")
        await _mail("personal:a2", "t1", "Re: Roof flashing", f"Josh <{ME}>", "roofer@x.com", "2026-10-09T10:00:00+00:00")
        # T2: I wrote, they replied → not waiting
        await _mail("personal:b1", "t2", "Lease renewal", f"Josh <{ME}>", "pat@x.com", "2026-10-08T10:00:00+00:00")
        await _mail("personal:b2", "t2", "Re: Lease renewal", "Pat <pat@x.com>", ME, "2026-10-10T10:00:00+00:00")
        # T3: mine but too recent
        await _mail("personal:c1", "t3", "Quick question", f"Josh <{ME}>", "sam@x.com", "2026-10-14T10:00:00+00:00")
        # T4: mine to a noreply address
        await _mail("personal:d1", "t4", "Statement", f"Josh <{ME}>", "noreply@bank.com", "2026-10-05T10:00:00+00:00")
        # T5: mine, 15 days, work account → waiting (oldest)
        await _mail("work:e1", "t5", "Deposit return", f"Josh <{ME}>", "Tenant <tenant@x.com>, Me <me@example.com>",
                    "2026-09-30T10:00:00+00:00", account="work")
        # W1: my text unanswered for 7 days → waiting
        await _window("handle:+14155550101",
                      "[2026-10-08 09:00] Mike Plumber: Boiler done\n[2026-10-08 09:10] Me: Can you send the invoice?",
                      "2026-10-08T09:10:00+00:00")
        # W2: they had the last word
        await _window("handle:+14155550102", "[2026-10-01 09:00] Me: Rent?\n[2026-10-01 09:05] Sara Lee: Sent!",
                      "2026-10-01T09:05:00+00:00", contact="Sara Lee", handle="+14155550102")
        # W3: mine but recent
        await _window("handle:+14155550103", "[2026-10-14 09:00] Me: Hi?", "2026-10-14T09:00:00+00:00",
                      contact="Recent", handle="+14155550103")
        # W4: group chat ending with me → ignored
        await _window("chat:9", "[2026-10-01 09:00] Me: all good?", "2026-10-01T09:00:00+00:00",
                      contact="Unit 4", handle="", is_group=True)
    run_async(go())
    return index_db


def _list(**kw):
    return run_async(waiting_on.list_waiting(now=NOW, my_addresses=MINE, **kw))


# ── Detection ──────────────────────────────────────────────────────

def test_list_waiting_detects_and_orders(seeded):
    items = _list()
    assert [(it["kind"], it["key"], it["days_waiting"]) for it in items] == [
        ("email", "work:t5", 15), ("imessage", "handle:+14155550101", 7), ("email", "personal:t1", 6),
    ]
    t5, w1, t1 = items
    assert t5["to"] == "tenant@x.com" and t5["email_id"] == "e1" and t5["account"] == "work"
    assert t1["subject"] == "Re: Roof flashing" and t1["to"] == "roofer@x.com"
    assert w1["contact"] == "Mike Plumber" and w1["handle"] == "+14155550101"
    assert w1["last_line"].endswith("Me: Can you send the invoice?")
    assert all(len(it["token"]) == 12 for it in items)


def test_days_threshold_and_no_addresses(seeded):
    assert [it["key"] for it in _list(days=10)] == ["work:t5"]
    assert [it["key"] for it in _list(days=1)][-1] == "personal:t3"   # recent email now counts
    no_mail = run_async(waiting_on.list_waiting(now=NOW, my_addresses=set()))
    assert [it["kind"] for it in no_mail] == ["imessage"]              # can't judge email without my address


def test_my_addresses_come_from_connected_accounts(seeded, monkeypatch):
    import llm

    async def fake_accounts():
        return [{"label": "personal", "email": "Me@Example.com"}, {"label": "work", "email": ""}]

    monkeypatch.setattr(llm, "_get_accounts_cached", fake_accounts)
    assert run_async(waiting_on._my_addresses()) == {ME}
    items = run_async(waiting_on.list_waiting(now=NOW))
    assert "work:t5" in [it["key"] for it in items]


def test_dismiss_hides_and_token_lookup(seeded, monkeypatch):
    monkeypatch.setattr(waiting_on, "_my_addresses", lambda: _coro(MINE))
    monkeypatch.setattr(waiting_on, "datetime", _FrozenDatetime)
    items = _list()
    run_async(db.dismiss_waiting("email", "personal:t1"))
    after = _list()
    assert [it["key"] for it in after] == ["work:t5", "handle:+14155550101"]
    tok = after[1]["token"]
    assert tok == items[1]["token"]                                   # stable across runs
    found = run_async(waiting_on.find_by_token(tok))
    assert found and found["key"] == "handle:+14155550101"
    assert run_async(waiting_on.find_by_token("nope")) is None


async def _coro(v):
    return v


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


# ── Formatting ─────────────────────────────────────────────────────

def test_describe_and_format(seeded):
    items = _list()
    text = waiting_on.format_waiting(items, days=3)
    assert text.startswith("Waiting on a reply (3, no answer after 3+ days):")
    assert '✉️ tenant@x.com — "Deposit return" (15d, work)' in text
    assert '💬 Mike Plumber — "[2026-10-08 09:10] Me: Can you send the invoice?" (7d)' in text
    assert waiting_on.format_waiting([], days=3).startswith("Nothing waiting")


# ── Nudge builds pending actions, never sends ─────────────────────

@pytest.fixture
def drafting(monkeypatch):
    import llm

    async def fake_draft(topic, to, days, channel="email"):
        return f"[{channel}] following up on {topic} after {days}d"

    def fake_meta(fn, email_id, account, **kw):
        return ({"reply_to": "", "from": "Roofer <roofer@x.com>", "subject": "Roof flashing"}, account or "personal")

    monkeypatch.setattr(llm, "draft_followup", fake_draft)
    monkeypatch.setattr(agent_tools, "_gmail_with_fallback", fake_meta)


def test_build_nudge_action_email_and_text(seeded, drafting):
    t5, w1, t1 = _list()
    email_action = run_async(waiting_on.build_nudge_action(t1))
    assert email_action["type"] == "reply_email"
    assert email_action["payload"]["email_id"] == "a2" and email_action["payload"]["account"] == "personal"
    assert email_action["payload"]["to"] == "Roofer <roofer@x.com>"
    assert email_action["payload"]["subject"] == "Re: Roof flashing"
    assert email_action["payload"]["body"] == "[email] following up on Re: Roof flashing after 6d"
    assert "*To:* Roofer" in email_action["detail"]

    text_action = run_async(waiting_on.build_nudge_action(w1))
    assert text_action["type"] == "send_imessage"
    assert text_action["payload"] == {"to": "+14155550101", "to_name": "Mike Plumber",
                                      "body": "[text] following up on [2026-10-08 09:10] Me: Can you send the invoice? after 7d",
                                      "service": "iMessage"}


def test_nudge_item_goes_through_request_approval(seeded, drafting, monkeypatch):
    import bot

    approvals = []

    async def fake_request_approval(context, chat_id, user_id, **kw):
        approvals.append((chat_id, user_id, kw))

    monkeypatch.setattr(bot, "request_approval", fake_request_approval)
    t5, w1, t1 = _list()
    text = run_async(bot.nudge_item(None, 42, 7, t1))
    assert text.startswith("✍️ Follow-up drafted for ✉️ roofer@x.com")
    assert len(approvals) == 1
    chat_id, user_id, kw = approvals[0]
    assert (chat_id, user_id, kw["action_type"]) == (42, 7, "reply_email")
    assert kw["payload"]["email_id"] == "a2"

    async def boom(item):
        raise RuntimeError("gmail offline")

    monkeypatch.setattr(waiting_on, "build_nudge_action", boom)
    assert run_async(bot.nudge_item(None, 42, 7, t1)).startswith("Couldn't draft the follow-up: gmail offline")
    assert len(approvals) == 1


# ── Tool + cards ───────────────────────────────────────────────────

def test_list_waiting_on_tool(seeded, monkeypatch):
    monkeypatch.setattr(waiting_on, "_my_addresses", lambda: _coro(MINE))
    monkeypatch.setattr(waiting_on, "datetime", _FrozenDatetime)
    out = run_async(agent_tools.execute_tool("list_waiting_on", {}))
    assert out["result"].startswith("Waiting on a reply (3")
    assert out["entities"][0] == {"type": "email", "id": "e1", "account": "work",
                                  "label": "Deposit return (waiting 15d)"}
    assert out["entities"][1]["type"] == "imessage_chat" and out["entities"][1]["id"] == "handle:+14155550101"


def test_waiting_cards_and_button_data(seeded):
    import bot

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            self.sent.append((chat_id, text, reply_markup))

    fake = FakeBot()
    items = _list()
    n = run_async(bot._send_waiting_cards(fake, 1, items, limit=2))
    assert n == 2 and len(fake.sent) == 2
    assert fake.sent[0][1].startswith("✉️ tenant@x.com")
    datas = [b.callback_data for row in fake.sent[0][2].inline_keyboard for b in row]
    tok = items[0]["token"]
    assert datas == [f"wait_nudge:{tok}", f"wait_dismiss:{tok}"]
    assert all(len(d.encode()) <= 64 for d in datas)

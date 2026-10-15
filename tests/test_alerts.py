"""Urgent alerts: new-item queue, inbound filter, watch-list, triage, push/hold, Reply. No network."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agent_tools
import alerts
import db
import indexer
import llm
from tests.conftest import run_async
from tests.fixtures import make_chat_db as fx

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
ME = "me@example.com"
REAL_IN_QUIET_HOURS = alerts.in_quiet_hours   # captured before the autouse stub replaces it


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


def _item(source="gmail", source_id="personal:m1", title="Boiler leaking", snippet="Water everywhere",
          **meta):
    base = {"from": "Pat <pat@x.com>", "account": "personal", "date": NOW.isoformat()} if source == "gmail" \
        else {"contact": "Mike Plumber", "handle": "+14155550101", "chat_key": "handle:+14155550101",
              "is_group": False, "date": NOW.isoformat()}
    base.update(meta)
    return {"source": source, "source_id": source_id, "title": title, "snippet": snippet,
            "meta": base, "indexed_at": NOW.isoformat()}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(indexer, "_now", lambda: NOW)
    indexer.new_items.clear()
    alerts.held.clear()
    monkeypatch.setattr(alerts, "in_quiet_hours", lambda now=None: False)


# ── Queue ──────────────────────────────────────────────────────────

def test_email_indexing_queues_only_fresh_items(index_db, monkeypatch):
    import gmail_module

    mail = {
        "fresh": {"subject": "Leak in unit 2", "body": "There is water coming through the ceiling",
                  "from": "Pat <pat@x.com>", "to": ME, "date": (NOW - timedelta(hours=1)).isoformat(),
                  "thread_id": "t1", "pdf_texts": [], "attachments": []},
        "old": {"subject": "Old newsletter", "body": "zzz", "from": "news@x.com", "to": ME,
                "date": (NOW - timedelta(hours=48)).isoformat(), "thread_id": "t2",
                "pdf_texts": [], "attachments": []},
    }
    monkeypatch.setattr(gmail_module, "get_email_with_pdf_text", lambda mid, account=None: mail[mid])

    async def go():
        a = await indexer._index_email_by_id("fresh", "personal", "personal")
        b = await indexer._index_email_by_id("old", "personal", "personal")
        again = await indexer._index_email_by_id("fresh", "personal", "personal")
        return a, b, again

    assert run_async(go()) == (True, True, False)
    queued = indexer.drain_new_items()
    assert [q["source_id"] for q in queued] == ["personal:fresh"]
    assert queued[0]["title"] == "Leak in unit 2" and queued[0]["snippet"].startswith("There is water")
    assert queued[0]["meta"]["from"] == "Pat <pat@x.com>"
    assert indexer.new_items == []


def test_window_indexing_queues_last_line_and_caps(chat_db, index_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "ALERT_MAX_AGE_HOURS", 24 * 7)

    run_async(indexer.index_imessage())
    queued = indexer.drain_new_items()
    assert queued and all(q["source"] == "imessage" for q in queued)
    by_contact = {q["meta"]["contact"]: q for q in queued}
    assert by_contact[fx.SARA]["snippet"].endswith(fx.LONG_BODY[-40:])   # newest line only
    assert "\n" not in by_contact[fx.SARA]["snippet"]

    monkeypatch.setattr(indexer, "NEW_ITEMS_CAP", 3)
    for i in range(5):
        indexer.note_new_item("gmail", f"p:{i}", "t", "b", {"date": NOW.isoformat()})
    assert [q["source_id"] for q in indexer.drain_new_items()] == ["p:2", "p:3", "p:4"]
    assert indexer.note_new_item("gmail", "x", "t", "b", {}) is False          # no date → never queued


# ── Filters and watch-list ─────────────────────────────────────────

def test_is_inbound():
    assert alerts.is_inbound(_item(), {ME})
    assert not alerts.is_inbound(_item(**{"from": f"Josh <{ME}>"}), {ME})
    assert not alerts.is_inbound(_item(**{"from": ""}), {ME})
    assert alerts.is_inbound(_item("imessage", "handle:+1|x", snippet="[2026-10-15 09:00] Mike Plumber: hi"), set())
    assert not alerts.is_inbound(_item("imessage", "handle:+1|x", snippet="[2026-10-15 09:00] Me: hi"), set())
    assert not alerts.is_inbound({"source": "onedrive", "source_id": "f", "meta": {}}, set())


def test_watch_terms_from_memories():
    mems = [
        {"content": "ALERT: Unit 4, Mike Plumber; no hot water"},
        {"content": "always alert me about leaks or the roof"},
        {"content": "Always alert when 12 Oak St emails"},
        {"content": "sign emails as Josh"},
    ]
    assert alerts.watch_terms_from_memories(mems) == [
        "unit 4", "mike plumber", "no hot water", "leaks", "the roof", "12 oak st emails"]
    assert alerts.watch_hit(_item(title="Re: Unit 4 heating"), ["unit 4"]) == "unit 4"
    assert alerts.watch_hit(_item(), ["unit 4"]) is None


def test_classify_uses_watchlist_before_model(monkeypatch):
    seen = []

    async def fake_model(items):
        seen.append([it["source_id"] for it in items])
        return [{"urgent": True, "category": "emergency", "reason": "flooding"} for _ in items]

    monkeypatch.setattr(alerts, "_classify_with_model", fake_model)
    items = [_item(source_id="p:1", title="Unit 4 door"), _item(source_id="p:2", title="Flood")]
    verdicts = run_async(alerts.classify(items, ["unit 4"]))
    assert verdicts[0] == {"urgent": True, "category": "watchlist", "reason": "watch-list: unit 4"}
    assert verdicts[1]["category"] == "emergency"
    assert seen == [["p:2"]]


def test_classify_model_failure_is_routine(monkeypatch):
    async def boom(items):
        raise RuntimeError("api down")

    monkeypatch.setattr(alerts, "_classify_with_model", boom)
    v = run_async(alerts.classify([_item()], []))
    assert v == [{"urgent": False, "category": "routine", "reason": "triage unavailable"}]


def test_classify_with_model_parses_strict_json(monkeypatch):
    captured = {}

    class FakeCompletions:
        async def create(self, **kw):
            captured.update(kw)
            content = json.dumps({"verdicts": [
                {"index": 0, "urgent": True, "category": "payment", "reason": "rent bounced"},
                {"index": 1, "urgent": True, "category": "routine", "reason": "contradiction"},
                {"index": 7, "urgent": True, "category": "legal", "reason": "out of range"},
            ]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    items = [_item(source_id="p:1"), _item(source_id="p:2"), _item(source_id="p:3")]
    out = run_async(alerts._classify_with_model(items))
    assert out[0] == {"urgent": True, "category": "payment", "reason": "rent bounced"}
    assert out[1]["urgent"] is False                      # routine can never be urgent
    assert out[2] == {"urgent": False, "category": "routine", "reason": "no verdict"}
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    import config
    assert captured["model"] == config.OPENAI_FAST_MODEL


# ── Process: push once, hold in quiet hours ───────────────────────

@pytest.fixture
def triage(monkeypatch):
    async def fake_model(items):
        return [{"urgent": "leak" in it["title"].lower() or "leak" in it["snippet"].lower(),
                 "category": "emergency", "reason": "water"} for it in items]

    async def no_terms():
        return []

    monkeypatch.setattr(alerts, "_classify_with_model", fake_model)
    monkeypatch.setattr(alerts, "watch_terms", no_terms)


def test_process_pushes_urgent_inbound_once(index_db, triage):
    bot = FakeBot()
    items = [
        _item(source_id="personal:leak", title="Leak in unit 2"),                       # urgent
        _item(source_id="personal:news", title="Newsletter", snippet="deals"),           # routine
        _item(source_id="personal:mine", title="Leak follow-up", **{"from": f"Me <{ME}>"}),  # outbound
        _item("imessage", "handle:+14155550101|2026", title="iMessage with Mike",
              snippet="[2026-10-15 11:50] Mike Plumber: big leak at 12 Oak"),           # urgent text
    ]
    n = run_async(alerts.process(bot, items, mine={ME}, chat_id=5))
    assert n == 2
    texts = [t for _, t, _ in bot.sent]
    assert texts[0].startswith("🚨 EMERGENCY · Email from Pat <pat@x.com>\nLeak in unit 2\nWater everywhere\n— water")
    assert texts[1].startswith("🚨 EMERGENCY · Text from Mike Plumber\n[2026-10-15 11:50] Mike Plumber: big leak")
    tok = alerts.token_for(items[0])
    datas = [b.callback_data for row in bot.sent[0][2].inline_keyboard for b in row]
    assert datas == [f"alert_reply:{tok}", f"alert_seen:{tok}"]
    assert all(len(d.encode()) <= 64 for d in datas)
    # dedupe: same items again push nothing
    assert run_async(alerts.process(bot, items, mine={ME}, chat_id=5)) == 0
    recent = run_async(db.recent_alerts())
    assert {r["source_id"] for r in recent} == {"personal:leak", "handle:+14155550101|2026"}
    assert run_async(db.get_alert_by_token(tok))["meta"]["account"] == "personal"


def test_quiet_hours_hold_then_flush(index_db, triage, monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(alerts, "in_quiet_hours", lambda now=None: True)
    assert run_async(alerts.process(bot, [_item(source_id="p:leak", title="leak")], mine={ME}, chat_id=5)) == 0
    assert len(alerts.held) == 1 and bot.sent == []
    assert run_async(alerts.flush_held(bot, chat_id=5)) == 0             # still quiet → stays held
    monkeypatch.setattr(alerts, "in_quiet_hours", lambda now=None: False)
    assert run_async(alerts.process(bot, [], mine={ME}, chat_id=5)) == 1   # next poll flushes
    assert alerts.held == [] and len(bot.sent) == 1
    # force flush (digest) works during quiet hours
    monkeypatch.setattr(alerts, "in_quiet_hours", lambda now=None: True)
    alerts.held.append((_item(source_id="p:leak2", title="leak2"), {"urgent": True, "category": "emergency", "reason": ""}))
    assert run_async(alerts.flush_held(bot, chat_id=5, force=True)) == 1


def test_quiet_window_parsing_and_wraparound(monkeypatch):
    import config
    monkeypatch.setattr(config, "ALERT_QUIET_HOURS", "23-7")
    real = REAL_IN_QUIET_HOURS

    def local(h, m=0):  # naive local wall time → aware, with the zone valid on that date
        return datetime(2026, 1, 1, h, m).astimezone()

    assert real(local(23, 30))
    assert real(local(3))
    assert not real(local(7))
    assert not real(local(12))
    monkeypatch.setattr(config, "ALERT_QUIET_HOURS", "9-17")
    assert real(local(12)) and not real(local(20))
    monkeypatch.setattr(config, "ALERT_QUIET_HOURS", "")
    assert alerts.quiet_window() is None and not real(local(3))


# ── Reply button → approval card ──────────────────────────────────

@pytest.fixture
def drafting(monkeypatch):
    async def fake_ack(topic, to, channel="email"):
        return f"[{channel}] seen: {topic}"

    def fake_meta(fn, email_id, account, **kw):
        return ({"reply_to": "", "from": "Pat <pat@x.com>", "subject": "Boiler leaking"}, account)

    monkeypatch.setattr(llm, "draft_acknowledgement", fake_ack)
    monkeypatch.setattr(agent_tools, "_gmail_with_fallback", fake_meta)


def test_build_reply_action_email_text_group_unknown(index_db, drafting):
    async def go():
        await db.record_alert("gmail", "personal:m9", "tokmail", "Boiler leaking", "emergency", "r",
                              {"from": "Pat <pat@x.com>", "account": "personal"})
        await db.record_alert("imessage", "handle:+14155550101|x", "toktext", "iMessage with Mike",
                              "emergency", "r", {"contact": "Mike Plumber", "handle": "+14155550101"})
        await db.record_alert("imessage", "chat:3|x", "tokgroup", "group", "emergency", "r",
                              {"contact": "Unit 4", "is_group": True})
        mail = await alerts.build_reply_action("tokmail")
        text = await alerts.build_reply_action("toktext")
        return mail, text

    mail, text = run_async(go())
    assert mail["type"] == "reply_email"
    assert mail["payload"]["email_id"] == "m9" and mail["payload"]["account"] == "personal"
    assert mail["payload"]["subject"] == "Re: Boiler leaking"
    assert mail["payload"]["body"] == "[email] seen: Boiler leaking"
    assert text["type"] == "send_imessage"
    assert text["payload"]["to"] == "+14155550101" and text["payload"]["to_name"] == "Mike Plumber"
    assert text["payload"]["body"] == "[text] seen: iMessage with Mike"
    with pytest.raises(ValueError, match="group chat"):
        run_async(alerts.build_reply_action("tokgroup"))
    with pytest.raises(ValueError, match="no longer on file"):
        run_async(alerts.build_reply_action("nope"))


def test_alert_reply_goes_through_request_approval(index_db, drafting, monkeypatch):
    import bot

    approvals = []

    async def fake_request_approval(context, chat_id, user_id, **kw):
        approvals.append((chat_id, user_id, kw))

    monkeypatch.setattr(bot, "request_approval", fake_request_approval)
    run_async(db.record_alert("gmail", "work:m1", "tok1", "Rent bounced", "payment", "r",
                              {"from": "Pat <pat@x.com>", "account": "work"}))
    assert run_async(bot.alert_reply(None, 42, 7, "tok1")) == "✍️ Reply drafted — approve it on the card below."
    assert approvals[0][:2] == (42, 7) and approvals[0][2]["action_type"] == "reply_email"
    assert approvals[0][2]["payload"]["account"] == "work"
    assert run_async(bot.alert_reply(None, 42, 7, "missing")).startswith("Couldn't draft a reply: That alert")
    assert len(approvals) == 1


# ── Toggle ─────────────────────────────────────────────────────────

def test_alerts_toggle_and_job_skip(index_db, monkeypatch):
    import bot

    async def go():
        default = await bot.alerts_enabled()
        await db.set_setting(bot.ALERTS_SETTING, "0")
        off = await bot.alerts_enabled()
        indexer.note_new_item("gmail", "p:1", "t", "b", {"date": NOW.isoformat()})
        await bot._alerts_job(SimpleNamespace(bot=FakeBot()))
        drained = indexer.new_items == []
        await db.set_setting(bot.ALERTS_SETTING, "1")
        return default, off, drained, await bot.alerts_enabled()

    import config
    assert run_async(go()) == (config.ALERTS_ENABLED, False, True, True)

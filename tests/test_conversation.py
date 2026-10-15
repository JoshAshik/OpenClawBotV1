"""get_conversation tool, the live timeline reader, and the chat.db watcher. No network."""

import asyncio
import threading
import time

import pytest

import agent_tools
import imessage_module as im
import indexer
from tests.conftest import run_async
from tests.fixtures import make_chat_db as fx
from tests.fixtures import make_contacts_db as cfx

MIKE_ROW = {"name": "Mike Plumber", "emails": [cfx.MIKE_EMAIL], "phones": [fx.MIKE],
            "channels": ["contacts", "imessage"], "count": 40, "last_seen": "2026-10-10"}
MIKE2_ROW = {"name": "Mike Painter", "emails": [], "phones": ["+14155550177"],
             "channels": ["imessage"], "count": 3, "last_seen": "2026-01-01"}
TENANT_ROW = {"name": "Pat Tenant", "emails": [fx.TENANT], "phones": [],
              "channels": ["gmail", "imessage"], "count": 9, "last_seen": "2026-09-01"}


@pytest.fixture
def seeded(chat_db, index_db, tmp_path, monkeypatch):
    """chat.db fixture + temp bot DB with contacts + Apple Contacts fixture for sender names."""
    import contacts_module as cm
    import db

    path = cfx.build_contacts_db(tmp_path / "AddressBook-v22.abcddb")
    monkeypatch.setenv("CONTACTS_DB_PATH", str(path))
    monkeypatch.setattr(cm, "_cached", None)
    run_async(db.replace_contacts([MIKE_ROW, MIKE2_ROW, TENANT_ROW]))
    return chat_db


def _tool(args: dict) -> dict:
    return run_async(agent_tools.execute_tool("get_conversation", args))


def _rowids(msgs):
    return [m["rowid"] for m in msgs]


# ── Reader ─────────────────────────────────────────────────────────

def test_get_timeline_by_handles_is_chronological_and_includes_group_sends(chat_db):
    msgs = im.get_timeline(handles=[fx.MIKE])
    assert _rowids(msgs) == [1, 2, 3, 5, 8]          # 1:1 both ways + Mike's group message
    assert msgs[1]["is_from_me"] and msgs[1]["text"].startswith("Can you come")
    assert _rowids(im.get_timeline(handles=[fx.MIKE], limit=3)) == [3, 5, 8]   # newest 3, in order
    assert _rowids(im.get_timeline(handles=[fx.MIKE], after_iso="2026-10-10T12:00:00+00:00")) == [5, 8]
    assert _rowids(im.get_timeline(handles=[fx.MIKE], before_iso="2026-10-10T12:00:00+00:00")) == [1, 2, 3]
    assert _rowids(im.get_timeline(handles=[fx.MIKE, fx.TENANT], after_iso="2026-10-12T00:00:00+00:00")) == [8, 9, 14]


def test_get_timeline_by_chat_key(chat_db):
    assert _rowids(im.get_timeline(chat_key="chat:3")) == [8, 9, 10]
    assert _rowids(im.get_timeline(chat_key=f"handle:{fx.TENANT}")) == [14]   # orphan, no chat row
    assert im.get_timeline() == [] and im.get_timeline(handles=[]) == []


# ── Tool ───────────────────────────────────────────────────────────

def test_tool_by_handle_resolves_names_and_returns_entities(seeded):
    out = _tool({"contact": "(415) 555-0101"})
    text = out["result"]
    assert text.startswith("iMessage/SMS conversation with Mike Plumber — 5 message(s)")
    assert "] Me: Can you come Tuesday morning?" in text
    assert "] Mike Plumber: Hi, the boiler" in text               # sender named via Contacts
    assert "[attachments: invoice_oct.pdf]" in text
    assert out["entities"] == [{"type": "imessage_chat", "id": f"handle:{fx.MIKE}", "label": "Mike Plumber"}]


def test_tool_by_name_and_ambiguity(seeded):
    out = _tool({"contact": "mike plumber", "limit": 2})
    assert out["result"].startswith("iMessage/SMS conversation with Mike Plumber — 2 message(s)")
    assert "newest 2" in out["result"]
    amb = _tool({"contact": "Mike"})
    assert "Several people match" in amb["result"] and "Mike Painter" in amb["result"]
    assert "entities" not in amb


def test_tool_group_by_display_name(seeded):
    out = _tool({"contact": "unit 4"})
    assert out["result"].startswith(f"iMessage group chat with {fx.GROUP_NAME} — 3 message(s)")
    assert "] Mike Plumber: The hallway light" in out["result"]
    assert "] Me: Great, I'll book" in out["result"]
    assert out["entities"][0] == {"type": "imessage_chat", "id": "chat:3", "label": fx.GROUP_NAME}


def test_tool_email_handle_contact_and_date_filters(seeded):
    out = _tool({"contact": "Pat Tenant", "after": "2026-10-13"})
    assert "1 message(s)" in out["result"] and "Orphan message" in out["result"]
    assert "(filtered from 2026-10-13)" in out["result"]
    none = _tool({"contact": fx.MIKE, "after": "2026-11-01"})
    assert none["result"].startswith("No messages found") and none["entities"]


def test_tool_unknown_and_missing_contact(seeded):
    assert "No contact or group chat matching" in _tool({"contact": "Zed Nobody"})["result"]
    assert "needs a contact" in _tool({"contact": ""})["result"]


def test_tool_when_imessage_unavailable(index_db, tmp_path, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DB_PATH", str(tmp_path / "nope.db"))
    assert "isn't available" in _tool({"contact": "Mike"})["result"]


def test_tool_whatsapp_unavailable_off_mac(index_db):
    out = _tool({"contact": "Mike", "source": "whatsapp"})
    assert "WhatsApp isn't available" in out["result"]


# ── Watcher ────────────────────────────────────────────────────────

def test_watcher_indexes_only_on_change(chat_db, monkeypatch):
    mtimes = {"now": (1, 1)}
    calls = []

    async def fake_index(*a, **k):
        calls.append(1)
        return 2

    monkeypatch.setattr(indexer, "_chat_db_mtimes", lambda: mtimes["now"])
    monkeypatch.setattr(indexer, "index_imessage", fake_index)
    monkeypatch.setattr(indexer, "_watch_mtimes", None)

    async def go():
        r = [await indexer.watch_imessage_once()]           # baseline only
        r.append(await indexer.watch_imessage_once())       # unchanged
        mtimes["now"] = (1, 2)                              # WAL grew
        r.append(await indexer.watch_imessage_once())
        r.append(await indexer.watch_imessage_once())       # unchanged again
        mtimes["now"] = (3, 2)
        async with indexer._imessage_lock:                  # a full pass is running
            r.append(await indexer.watch_imessage_once())
        r.append(await indexer.watch_imessage_once())       # retried after release
        return r

    assert run_async(go()) == [0, 0, 2, 0, 0, 2]
    assert len(calls) == 2


def test_watcher_noop_without_chat_db(tmp_path, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.setattr(indexer, "_watch_mtimes", None)
    assert run_async(indexer.watch_imessage_once()) == 0
    assert indexer._watch_mtimes is None


def test_index_imessage_runs_serialized(chat_db, index_db, monkeypatch):
    state = {"active": 0, "max": 0}
    guard = threading.Lock()
    real = im.read_batch

    def slow_read_batch(after, limit=2000):
        with guard:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        try:
            time.sleep(0.15)
            return real(after, limit)
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(im, "read_batch", slow_read_batch)

    async def go():
        return await asyncio.gather(indexer.index_imessage(), indexer.index_imessage())

    first, second = run_async(go())
    assert first > 0 and second == 0          # second pass found nothing new
    assert state["max"] == 1                  # never overlapped

"""
iMessage reader + conversation-window tests against the synthetic chat.db fixture.
No network: embeddings are stubbed and the bot DB is a temp file (see conftest.py).
"""

import pytest

import imessage_module as im
from tests.conftest import run_async
from tests.fixtures import make_chat_db as fx
from tests.fixtures.make_chat_db import encode_attributed_body, encode_typedstream_int


# ── typedstream decoder ───────────────────────────────────────────

@pytest.mark.parametrize("text", [
    pytest.param("hi", id="short"),
    pytest.param("Can you come Tuesday morning?", id="sentence"),
    pytest.param("x" * 127, id="len127-single-byte"),
    pytest.param("x" * 128, id="len128-0x81-u16"),
    pytest.param("é" * 100, id="200-bytes-from-100-chars"),
    pytest.param("Roofer quoted £1,250 👍 — ok?", id="multibyte-emoji"),
    pytest.param(fx.LONG_BODY, id="long-body"),
    pytest.param("a" * 70_000, id="len70000-0x82-u32"),
])
def test_decoder_round_trip(text):
    assert im.decode_attributed_body(encode_attributed_body(text)) == text


def test_typedstream_int_forms():
    assert encode_typedstream_int(5) == b"\x05"
    assert encode_typedstream_int(127) == b"\x7f"
    assert encode_typedstream_int(128) == b"\x81\x80\x00"
    assert encode_typedstream_int(300) == b"\x81\x2c\x01"
    assert encode_typedstream_int(70_000) == b"\x82\x70\x11\x01\x00"
    for n in (5, 127, 128, 300, 70_000):
        blob = encode_typedstream_int(n)
        assert im._read_typedstream_int(blob, 0) == (n, len(blob))


def test_decoder_strips_attachment_placeholder_and_memoryview():
    blob = encode_attributed_body("￼Here is the invoice")
    assert im.decode_attributed_body(blob) == "Here is the invoice"
    assert im.decode_attributed_body(memoryview(blob)) == "Here is the invoice"


@pytest.mark.parametrize("blob", [None, b"", b"garbage", b"NSString", b"NSString+",
                                  b"NSString\x01\x94\x84\x01+\x81\xff"])
def test_decoder_returns_empty_for_unusable_blobs(blob):
    assert im.decode_attributed_body(blob) == ""


def test_message_body_prefers_text_then_blob():
    blob = encode_attributed_body("from blob")
    assert im.message_body("from text", blob) == "from text"
    assert im.message_body(None, blob) == "from blob"
    assert im.message_body("", blob) == "from blob"
    assert im.message_body("￼", blob) == "from blob"
    assert im.message_body(None, None) == ""


def test_apple_time_round_trip():
    iso = im._apple_time_to_iso(fx.ts(2026, 10, 10, 9, 0))
    assert iso == "2026-10-10T09:00:00+00:00"
    assert im.iso_to_apple_ns(iso) == fx.ts(2026, 10, 10, 9, 0)
    # older DBs stored seconds
    assert im._apple_time_to_iso(600_000_000).startswith("2020-01-06")
    assert im._apple_time_to_iso(None) == ""


# ── reading the fixture ───────────────────────────────────────────

def test_fixture_is_available_via_env(chat_db):
    assert im.db_path() == chat_db
    assert im.is_available()


def test_tapbacks_empty_rows_skipped(chat_db):
    msgs = im.get_messages_since(0)
    rowids = [m["rowid"] for m in msgs]
    assert rowids == fx.EXPECTED_ROWIDS
    assert not set(rowids) & set(fx.SKIPPED_ROWIDS)
    assert not any(m["text"].startswith("Loved") for m in msgs)


def test_attributed_body_rows_decoded(chat_db):
    by_id = {m["rowid"]: m for m in im.get_messages_since(0)}
    assert by_id[1]["text"] == "Hi, the boiler at 12 Oak is leaking again"
    assert fx.ATTRIBUTED_ONLY_PHRASE in by_id[3]["text"]
    assert by_id[13]["text"] == fx.LONG_BODY
    # dates, service, attachments
    assert by_id[1]["date"] == "2026-10-10T09:00:00+00:00"
    assert by_id[6]["service"] == "SMS" and by_id[1]["service"] == "iMessage"
    assert by_id[5]["text"] == "Here is the invoice"
    assert by_id[5]["attachments"] == ["invoice_oct.pdf"]
    assert by_id[1]["attachments"] == []


def test_one_to_one_chat_fields(chat_db):
    by_id = {m["rowid"]: m for m in im.get_messages_since(0)}
    inbound, outbound = by_id[1], by_id[2]
    assert inbound["sender"] == fx.MIKE and not inbound["is_from_me"]
    assert outbound["sender"] == "Me" and outbound["is_from_me"]
    for m in (inbound, outbound):
        assert m["contact"] == fx.MIKE
        assert m["chat_id"] == 1 and m["chat_key"] == "chat:1"
        assert m["chat_identifier"] == fx.MIKE
        assert m["is_group"] is False
    orphan = by_id[14]
    assert orphan["chat_id"] is None
    assert orphan["chat_key"] == f"handle:{fx.TENANT}"
    assert orphan["contact"] == fx.TENANT


def test_group_chat_fields(chat_db):
    by_id = {m["rowid"]: m for m in im.get_messages_since(0)}
    group = [by_id[8], by_id[9], by_id[10]]
    for m in group:
        assert m["is_group"] is True
        assert m["chat_id"] == 3 and m["chat_key"] == "chat:3"
        assert m["display_name"] == fx.GROUP_NAME
        assert m["contact"] == fx.GROUP_NAME
        assert m["chat_identifier"] == fx.GROUP_IDENT
    assert by_id[8]["sender"] == fx.MIKE
    assert by_id[9]["sender"] == fx.TENANT
    assert by_id[10]["sender"] == "Me"  # handle_id 0 on my own group messages


def test_cursor_pagination(chat_db):
    cursor = 0
    seen: list[int] = []
    pages = 0
    while True:
        msgs, last = im.read_batch(cursor, limit=4)
        if last <= cursor:
            break
        assert last > cursor
        assert all(m["rowid"] > cursor for m in msgs)
        seen.extend(m["rowid"] for m in msgs)
        cursor = last
        pages += 1
        assert pages < 20, "cursor did not advance"
    assert seen == fx.EXPECTED_ROWIDS           # every usable row exactly once, ascending
    assert cursor == fx.MAX_ROWID               # cursor ends past the trailing skipped rows
    assert im.read_batch(cursor, limit=4) == ([], cursor)
    assert im.max_rowid() == fx.MAX_ROWID


def test_get_chat_context_walks_back_within_gap(chat_db):
    by_id = {m["rowid"]: m for m in im.get_messages_since(0)}
    # new message 3 arrived; cursor was at 2 → context is rows 1 and 2 (same window)
    ctx = im.get_chat_context("chat:1", before_rowid=2, until_iso=by_id[3]["date"])
    assert [m["rowid"] for m in ctx] == [1, 2]
    # row 5 is > 30 min after row 3 → no context
    assert im.get_chat_context("chat:1", before_rowid=4, until_iso=by_id[5]["date"]) == []
    # handle-keyed orphan chat
    assert im.get_chat_context(f"handle:{fx.TENANT}", 14, by_id[14]["date"])[0]["rowid"] == 14


def test_convenience_readers(chat_db):
    recent = im.get_recent_messages(limit=3)
    assert [m["rowid"] for m in recent] == [14, 13, 10]  # newest first

    hits = im.search_messages("ZEBRA VALVE")
    assert [m["rowid"] for m in hits] == [3]
    assert im.search_messages("   ") == []

    mike = im.get_messages_from_contact("5550101")
    assert sorted(m["rowid"] for m in mike) == [1, 2, 3, 5, 8]  # 1:1 chat + group row

    contacts = {c["contact"]: c for c in im.list_contacts()}
    assert contacts[fx.MIKE]["message_count"] == 5  # rows 1,2,3,5,8 (tapbacks excluded)
    assert contacts[fx.SARA]["last_date"].startswith("2026-10-14")

    chats = {c["chat_id"]: c for c in im.list_chats()}
    assert chats[3]["is_group"] and chats[3]["display_name"] == fx.GROUP_NAME
    assert chats[3]["participants"] == [fx.MIKE, fx.TENANT]
    assert chats[1]["participants"] == [fx.MIKE] and not chats[1]["is_group"]


def test_unavailable_without_db(tmp_path, monkeypatch):
    monkeypatch.setenv("IMESSAGE_DB_PATH", str(tmp_path / "missing.db"))
    assert not im.is_available()
    with pytest.raises(RuntimeError):
        im.get_recent_messages()


# ── conversation windows (indexer) ────────────────────────────────

def test_window_building_keyed_by_chat(chat_db):
    import indexer

    msgs = im.get_messages_since(0)
    docs = indexer.build_conversation_windows(msgs, "iMessage")
    by_key = {}
    for d in docs:
        by_key.setdefault(d["metadata"]["chat_key"], []).append(d)

    # chat 1 splits at the > 30 min gap; group chat stays one window
    assert len(by_key["chat:1"]) == 2
    assert len(by_key["chat:3"]) == 1
    assert len(by_key["chat:2"]) == 2
    assert len(by_key[f"handle:{fx.TENANT}"]) == 1

    first = by_key["chat:1"][0]
    assert first["source_id"] == "chat:1|2026-10-10T09:00:00+00:00"
    assert first["title"].startswith(f"iMessage with {fx.MIKE}")
    assert first["metadata"]["messages"] == 3
    assert f"{fx.MIKE}: Hi, the boiler" in first["content"]
    assert "Me: Can you come Tuesday morning?" in first["content"]
    assert fx.ATTRIBUTED_ONLY_PHRASE in first["content"]
    assert "Loved" not in first["content"]

    second = by_key["chat:1"][1]
    assert "[attachments: invoice_oct.pdf]" in second["content"]
    assert second["metadata"]["attachments"] == ["invoice_oct.pdf"]

    group = by_key["chat:3"][0]
    assert group["title"].startswith(f'iMessage group "{fx.GROUP_NAME}"')
    assert group["metadata"]["is_group"] is True
    assert group["metadata"]["senders"] == [fx.MIKE, fx.TENANT, "Me"]
    assert f"{fx.TENANT}: I can let the electrician" in group["content"]

    sms = by_key["chat:2"][0]
    assert sms["metadata"]["service"] == "SMS"

    # stable across runs → needs_reindex can compare content hashes
    again = indexer.build_conversation_windows(list(reversed(msgs)), "iMessage")
    assert [d["source_id"] for d in again] == [d["source_id"] for d in docs]
    assert [d["content"] for d in again] == [d["content"] for d in docs]


def test_window_name_resolution_hook(chat_db):
    import indexer

    names = {fx.MIKE: "Mike Plumber", fx.TENANT: "Pat Tenant"}
    docs = indexer.build_conversation_windows(
        im.get_messages_since(0), "iMessage", resolve_name=lambda h: names.get(h, h))
    titles = [d["title"] for d in docs]
    assert any(t.startswith("iMessage with Mike Plumber") for t in titles)
    assert any(t.startswith("iMessage with Pat Tenant") for t in titles)
    group = next(d for d in docs if d["metadata"]["is_group"])
    assert "Mike Plumber: The hallway light" in group["content"]
    assert "Pat Tenant: I can let" in group["content"]
    assert group["title"].startswith(f'iMessage group "{fx.GROUP_NAME}"')  # group keeps its name


def test_window_dedupes_context_overlap(chat_db):
    import indexer

    msgs = im.get_messages_since(0)
    docs = indexer.build_conversation_windows(msgs + msgs[:3], "iMessage")
    first = next(d for d in docs if d["source_id"].startswith("chat:1|"))
    assert first["metadata"]["messages"] == 3


def test_legacy_contact_only_messages_still_window(chat_db):
    """WhatsApp dicts have no chat_key; they fall back to the contact."""
    import indexer

    msgs = [
        {"text": "hi", "contact": "+1555", "date": "2026-10-01T10:00:00+00:00", "is_from_me": False},
        {"text": "yo", "contact": "+1555", "date": "2026-10-01T10:05:00+00:00", "is_from_me": True},
    ]
    docs = indexer._build_conversation_windows(msgs, "WhatsApp")
    assert len(docs) == 1
    assert docs[0]["source_id"] == "handle:+1555|2026-10-01T10:00:00+00:00"
    assert docs[0]["title"].startswith("WhatsApp with +1555")
    assert "+1555: hi" in docs[0]["content"] and "Me: yo" in docs[0]["content"]


def test_index_imessage_cursor_and_reindex(chat_db, index_db):
    """
    End to end on a temp bot DB: index in pages of 4, cursor lands on MAX_ROWID, the
    search index holds the windows, a second run is a no-op, and a window that grows
    (new row appended to chat 1's last window) re-indexes under the same source_id.
    """
    import sqlite3

    import db
    import indexer
    import search_index

    async def first_run():
        n = await indexer.index_imessage(limit=4)
        cursor = await db.get_setting(indexer.IMESSAGE_CURSOR_KEY)
        hits = await search_index.search("zebra valve", source_filter="imessage", limit=5)
        stats = await search_index.get_index_stats()
        return n, cursor, hits, stats

    n, cursor, hits, stats = run_async(first_run())
    assert cursor == str(fx.MAX_ROWID)
    assert n >= 6  # 6 distinct windows; boundary windows may index twice as they grow
    assert hits and hits[0]["source_id"] == "chat:1|2026-10-10T09:00:00+00:00"

    async def second_run():
        return await indexer.index_imessage(limit=4)

    assert run_async(second_run()) == 0

    # append a reply to chat 1's afternoon window (row 5 at 14:00 → 14:10)
    conn = sqlite3.connect(chat_db)
    conn.execute(
        "INSERT INTO message (ROWID, guid, text, handle_id, date, service, is_from_me) "
        "VALUES (15, 'p:0/15', 'Got it, paying the invoice today', 1, ?, 'iMessage', 0)",
        (fx.ts(2026, 10, 10, 14, 10),),
    )
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 15)")
    conn.commit()
    conn.close()

    async def third_run():
        n = await indexer.index_imessage(limit=4)
        cursor = await db.get_setting(indexer.IMESSAGE_CURSOR_KEY)
        item = await search_index.get_item("imessage", "chat:1|2026-10-10T14:00:00+00:00")
        content = await search_index.get_full_content("imessage", "chat:1|2026-10-10T14:00:00+00:00")
        return n, cursor, item, content

    n, cursor, item, content = run_async(third_run())
    assert n == 1 and cursor == "15"
    assert item is not None
    assert "Here is the invoice" in content and "paying the invoice today" in content

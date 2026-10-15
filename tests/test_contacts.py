"""Apple Contacts reader, phone normalizer, resolver, and the merged contacts rebuild."""

import json
import sqlite3

import pytest

import contacts_module as cm
from tests.conftest import run_async
from tests.fixtures import make_chat_db as chat_fx
from tests.fixtures import make_contacts_db as fx


@pytest.fixture
def contacts_db(tmp_path, monkeypatch):
    path = fx.build_contacts_db(tmp_path / "AddressBook-v22.abcddb")
    monkeypatch.setenv("CONTACTS_DB_PATH", str(path))
    monkeypatch.setenv("CONTACTS_DEFAULT_COUNTRY", "1")
    monkeypatch.setattr(cm, "_cached", None)
    return path


# ── Normalization ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("(415) 555-0101", "+14155550101"),
    ("415.555.0101", "+14155550101"),
    ("1 415 555 0101", "+14155550101"),
    ("+1 415-555-0101", "+14155550101"),
    ("+14155550101", "+14155550101"),
    ("0044 20 7946 0958", "+442079460958"),
    ("+44 (0)20 7946 0958", "+4402079460958"),     # explicit + is taken literally
    ("415-555-0199 x12", "+14155550199"),          # extension dropped
    ("555-0101", "+15550101"),                     # 7 digits: country code prepended
    ("911", None),
    ("", None),
    (None, None),
])
def test_normalize_phone_us_default(raw, expected):
    assert cm.normalize_phone(raw, default_country="1") == expected


def test_normalize_phone_other_default_country():
    assert cm.normalize_phone("020 7946 0958", default_country="44") == "+440207946 0958".replace(" ", "")
    assert cm.normalize_phone("+1 415 555 0101", default_country="44") == "+14155550101"


@pytest.mark.parametrize("handle, expected", [
    ("+14155550101", "+14155550101"),
    ("(415) 555 0101", "+14155550101"),
    ("Tenant@Example.com ", "tenant@example.com"),
    ("14155550101@s.whatsapp.net", "+14155550101"),
    ("14155550101@c.us", "+14155550101"),
    ("Mike Plumber", None),                        # a display name is not a handle
    ("", None),
])
def test_normalize_handle(handle, expected):
    assert cm.normalize_handle(handle) == expected


# ── Store reading ──────────────────────────────────────────────────

def test_unavailable_without_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTACTS_DB_PATH", str(tmp_path / "missing.abcddb"))
    monkeypatch.setattr(cm, "_cached", None)
    assert not cm.is_available()
    assert cm.get_resolver() is None
    assert cm.resolve_name("+14155550101") == "+14155550101"
    assert cm.resolver_fn()("x") == "x"


def test_load_contacts_from_fixture(contacts_db):
    assert cm.is_available()
    people = {p["name"]: p for p in cm.load_contacts()}
    assert set(people) == {fx.MIKE_NAME, fx.SARA_NAME, fx.ORG_NAME}  # EMPTY_NAME dropped
    assert people[fx.MIKE_NAME]["phones"] == [fx.MIKE_PHONE_E164]
    assert people[fx.MIKE_NAME]["emails"] == [fx.MIKE_EMAIL]
    assert sorted(people[fx.SARA_NAME]["phones"]) == sorted([fx.SARA_PHONE_E164, fx.SARA_WORK_E164])
    assert people[fx.SARA_NAME]["emails"] == [fx.SARA_EMAIL.lower()]
    assert people[fx.ORG_NAME]["organization"] == fx.ORG_NAME
    assert people[fx.ORG_NAME]["first"] == ""


def test_directory_override_globs_stores(tmp_path, monkeypatch):
    src = tmp_path / "Sources" / "ABCD" / "AddressBook-v22.abcddb"
    src.parent.mkdir(parents=True)
    fx.build_contacts_db(src)
    monkeypatch.setenv("CONTACTS_DB_PATH", str(tmp_path))
    monkeypatch.setattr(cm, "_cached", None)
    assert cm.db_paths() == [src]
    assert len(cm.load_contacts()) == 3


def test_unreadable_store_raises(tmp_path, monkeypatch):
    bad = tmp_path / "AddressBook-v22.abcddb"
    sqlite3.connect(bad).close()  # exists, but has none of the tables
    monkeypatch.setenv("CONTACTS_DB_PATH", str(bad))
    monkeypatch.setattr(cm, "_cached", None)
    with pytest.raises(RuntimeError):
        cm.load_contacts()
    assert cm.get_resolver() is None  # logged, not raised


# ── Resolver ───────────────────────────────────────────────────────

def test_resolver_hits_and_misses(contacts_db):
    r = cm.get_resolver()
    assert r is not None and len(r) == 3
    assert r.resolve("+14155550101") == fx.MIKE_NAME
    assert r.resolve("415-555-0101") == fx.MIKE_NAME          # loose SMS formatting
    assert r.resolve("14155550102@s.whatsapp.net") == fx.SARA_NAME
    assert r.resolve("+14155550199") == fx.SARA_NAME           # second number
    assert r.resolve("SARA.LEE@example.com") == fx.SARA_NAME
    assert r.resolve("+18005550100") == fx.ORG_NAME
    assert r.resolve("+14155559999") == "+14155559999"         # unknown → unchanged
    assert r.resolve("") == ""
    assert r.lookup("+14155550101")["emails"] == [fx.MIKE_EMAIL]


def test_resolver_is_cached_until_forced(contacts_db):
    first = cm.get_resolver()
    assert cm.get_resolver() is first
    assert cm.get_resolver(force=True) is not first
    assert cm.get_resolver(max_age_seconds=0) is not first


# ── Windows get names ──────────────────────────────────────────────

def test_imessage_windows_titled_by_contact_name(chat_db, contacts_db):
    import imessage_module as im
    import indexer

    docs = indexer.build_conversation_windows(
        im.get_messages_since(0), "iMessage", cm.resolver_fn())
    titles = [d["title"] for d in docs]
    assert any(t.startswith(f"iMessage with {fx.MIKE_NAME}") for t in titles)
    mike = next(d for d in docs if d["metadata"]["contact"] == fx.MIKE_NAME)
    assert mike["metadata"]["handle"] == chat_fx.MIKE          # raw handle kept
    assert mike["metadata"]["handles"] == [chat_fx.MIKE]
    group = next(d for d in docs if d["metadata"]["is_group"])
    assert f"{fx.MIKE_NAME}: " in group["content"]
    assert chat_fx.MIKE in group["metadata"]["handles"]
    # unknown handles stay raw
    assert any(d["metadata"]["contact"] == chat_fx.TENANT for d in docs)


def test_index_imessage_uses_contacts_by_default(chat_db, contacts_db, index_db):
    import indexer
    import search_index

    async def go():
        n = await indexer.index_imessage()
        conn = await search_index.get_db()
        cur = await conn.execute("SELECT title FROM search_index WHERE source = 'imessage'")
        return n, [r["title"] for r in await cur.fetchall()]

    n, titles = run_async(go())
    assert n > 0
    assert any(fx.MIKE_NAME in t for t in titles)


# ── Merge + rebuild ────────────────────────────────────────────────

def test_merge_contact_records_bridges_sources():
    from indexer import merge_contact_records

    apple = {"name": "Mike Plumber", "emails": ["mike@plumbing.example"],
             "phones": ["+14155550101"], "channels": ["contacts"], "count": 0,
             "last_seen": "", "prio": 3}
    gmail = {"name": "mike plumbing", "emails": ["mike@plumbing.example"], "phones": [],
             "channels": ["gmail"], "count": 12, "last_seen": "2026-09-01", "prio": 2}
    imsg = {"name": "", "emails": [], "phones": ["+14155550101"], "channels": ["imessage"],
            "count": 30, "last_seen": "2026-10-10", "prio": 1}
    other = {"name": "Pat", "emails": ["pat@example.com"], "phones": [],
             "channels": ["gmail"], "count": 2, "last_seen": "2026-01-01", "prio": 2}

    people = merge_contact_records([apple, gmail, imsg, other])
    assert len(people) == 2
    mike = next(p for p in people if "+14155550101" in p["phones"])
    assert mike["name"] == "Mike Plumber"                      # Contacts name wins
    assert mike["emails"] == ["mike@plumbing.example"]
    assert sorted(mike["channels"]) == ["contacts", "gmail", "imessage"]
    assert mike["count"] == 42
    assert mike["last_seen"] == "2026-10-10"
    assert "prio" not in mike


def test_merge_bridging_record_joins_two_people():
    from indexer import merge_contact_records

    a = {"name": "", "emails": ["a@x.com"], "phones": [], "channels": ["gmail"], "count": 1,
         "last_seen": "", "prio": 2}
    b = {"name": "", "emails": [], "phones": ["+15550001111"], "channels": ["imessage"],
         "count": 1, "last_seen": "", "prio": 1}
    bridge = {"name": "A B", "emails": ["a@x.com"], "phones": ["+15550001111"],
              "channels": ["contacts"], "count": 0, "last_seen": "", "prio": 3}
    people = merge_contact_records([a, b, bridge])
    assert len(people) == 1
    assert people[0]["name"] == "A B"
    assert people[0]["count"] == 2


def test_rebuild_contacts_merges_everything(chat_db, contacts_db, index_db):
    import db
    import indexer
    import search_index

    async def go():
        # Gmail metadata as the indexer stores it; Mike mails from his Contacts address.
        await search_index.index_item(
            "gmail", "personal:m1", "Invoice", "body",
            {"from": f"Mike P <{fx.MIKE_EMAIL}>", "to": "me@example.com",
             "date": "2026-09-01T10:00:00+00:00", "account": "personal"})
        await search_index.index_item(
            "gmail", "personal:m2", "Lease", "body",
            {"from": "Pat Tenant <tenant@example.com>", "to": "me@example.com",
             "date": "2026-09-02T10:00:00+00:00", "account": "personal"})
        await indexer.index_imessage()
        n = await indexer.rebuild_contacts()
        mike = await db.search_contacts("mike")
        tenant = await db.search_contacts("tenant@example")
        by_phone = await db.search_contacts("415 555 0101")
        sara = await db.search_contacts("Sara")
        return n, mike, tenant, by_phone, sara

    n, mike, tenant, by_phone, sara = run_async(go())
    assert n >= 4
    assert len(mike) == 1
    m = mike[0]
    assert m["name"] == fx.MIKE_NAME
    assert m["email"] == fx.MIKE_EMAIL and m["emails"] == [fx.MIKE_EMAIL]
    assert m["phones"] == [fx.MIKE_PHONE_E164]
    assert set(m["channels"]) == {"contacts", "gmail", "imessage"}
    assert m["count"] > 1 and m["last_seen"].startswith("2026")
    assert by_phone and by_phone[0]["name"] == fx.MIKE_NAME

    # The tenant is not in Contacts: email from Gmail + the same email as an iMessage handle.
    assert len(tenant) == 1
    assert set(tenant[0]["channels"]) == {"gmail", "imessage"}
    assert tenant[0]["name"] == "Pat Tenant"

    # Sara is in Contacts with two numbers; one is an iMessage handle in the fixture.
    assert sara and sorted(sara[0]["phones"]) == sorted([fx.SARA_PHONE_E164, fx.SARA_WORK_E164])


def test_contacts_table_migrates_from_email_keyed_schema(tmp_path, monkeypatch):
    import config
    import db

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE contacts (email TEXT PRIMARY KEY, name TEXT,
                               count INTEGER NOT NULL DEFAULT 0, last_seen TEXT);
        INSERT INTO contacts VALUES ('old@example.com', 'Old', 3, '');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_DB", None)

    async def go():
        await db.replace_contacts([{"email": "New@Example.com", "name": "New", "count": 1}])
        rows = await db.list_contacts()
        return rows

    rows = run_async(go())
    assert [r["email"] for r in rows] == ["new@example.com"]   # old table dropped, legacy record accepted
    assert rows[0]["emails"] == ["new@example.com"] and rows[0]["channels"] == ["gmail"]
    assert json.loads(sqlite3.connect(path).execute(
        "SELECT phones FROM contacts").fetchone()[0]) == []

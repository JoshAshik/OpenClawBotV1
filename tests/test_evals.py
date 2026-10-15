"""Eval harness: mining from chat_turns, redaction, merging, scoring, offline run. No network."""

import json

import pytest

import db
import search_index
from evals import extract, run
from tests.conftest import run_async

MID = "19eb846f87f11b0c"


def _assistant(tool_uses=(), text=""):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "id": f"t{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(tool_uses)]
    return [{"role": "assistant", "content": content},
            {"role": "tool_results", "content": [{"tool_use_id": "t0", "content": "..."}]}]


@pytest.fixture
def turns_db(index_db):
    async def go():
        # 1. search then read → mined (query ≥ 4 words)
        await db.save_turn("s1", "user", [{"role": "user", "content": "find the tuition invoice from last month"}])
        await db.save_turn("s1", "assistant", _assistant([
            ("search_everything", {"query": "tuition invoice"}),
            ("read_indexed_document", {"source": "gmail", "source_id": f"personal:{MID}"}),
        ]))
        # 2. no tool touched a specific id → skipped
        await db.save_turn("s1", "user", [{"role": "user", "content": "what is in my inbox today please"}])
        await db.save_turn("s1", "assistant", _assistant([("list_recent_emails", {})]))
        # 3. too short → skipped
        await db.save_turn("s1", "user", [{"role": "user", "content": "lease please"}])
        await db.save_turn("s1", "assistant", _assistant([("read_email", {"email_id": "abc", "account": "work"})]))
        # 4. search first, then reply with account → gmail work:xyz; phone redacted; link cited
        await db.save_turn("s2", "user", [{"role": "user", "content": "reply to the roofer at +1 (415) 555-0199 saying yes"}])
        await db.save_turn("s2", "assistant", _assistant(
            [("search_everything", {"query": "roofer"}),
             ("reply_email", {"email_id": "xyz", "account": "work", "body": "yes"})],
            text="Done — [open in Gmail](https://mail.google.com/mail/#all/deadbeef1234)"))
        # 5. duplicate query of #1 in another session → deduped
        await db.save_turn("s3", "user", [{"role": "user", "content": "Find the tuition invoice from last month"}])
        await db.save_turn("s3", "assistant", _assistant([("read_email", {"email_id": f"personal:{MID}"})]))
        # 6. context-only follow-up: starts with reply_email → not a retrieval case
        await db.save_turn("s3", "user", [{"role": "user", "content": "draft a one line reply to it please"}])
        await db.save_turn("s3", "assistant", _assistant([("reply_email", {"email_id": f"personal:{MID}", "body": "ok"})]))
    run_async(go())
    return index_db


def test_mine_cases(turns_db):
    cases = extract.mine_cases(turns_db)
    assert [c["query"] for c in cases] == [
        "find the tuition invoice from last month",
        "reply to the roofer at [phone] saying yes",
    ]
    assert cases[0]["expected"] == [{"source": "gmail", "source_id": f"personal:{MID}"}]
    assert cases[0]["tool"] == "search_everything" and cases[0]["origin"] == "mined"
    assert {e["source_id"] for e in cases[1]["expected"]} == {"work:xyz", "deadbeef1234"}
    assert all(e["source"] == "gmail" for e in cases[1]["expected"])


def test_redact():
    assert extract.redact("call me on 415-555-0199 or +44 20 7946 0958 re sk-abcdefghijklmnop") == \
        "call me on [phone] or [phone] re [key]"
    assert extract.redact("invoice 123456789012 from john@x.com") == "invoice [number] from [email]"
    assert extract.redact("unit 4 lease 2026") == "unit 4 lease 2026"


def test_merge_keeps_manual_and_replaces_mined(tmp_path):
    path = tmp_path / "cases.jsonl"
    manual = {"query": "the plumber invoice", "expected": [{"source": "gmail", "source_id": "p:1"}], "origin": "manual"}
    old_mined = {"query": "old mined one", "expected": [{"source": "gmail", "source_id": "p:2"}], "origin": "mined"}
    clash = {"query": "The Plumber Invoice", "expected": [{"source": "gmail", "source_id": "p:9"}], "origin": "mined"}
    new_mined = {"query": "new mined one", "expected": [{"source": "gmail", "source_id": "p:3"}], "origin": "mined"}
    extract.write_cases([manual, old_mined], path)
    merged = extract.merge_cases(extract.load_cases(path), [clash, new_mined])
    assert merged == [manual, new_mined]                       # manual wins the clash, old mined dropped
    extract.write_cases(merged, path)
    assert extract.load_cases(path) == [manual, new_mined]
    assert extract.load_cases(tmp_path / "missing.jsonl") == []


def test_score_and_summary():
    exp = [{"source": "gmail", "source_id": f"personal:{MID}"}]
    r = lambda *ids: [{"source": "gmail", "source_id": i} for i in ids]
    assert run.score(r(MID, "x"), exp) == {"rank": 1, "hit1": True, "hit5": True, "rr": 1.0}
    assert run.score(r("a", "b", f"work:{MID}"), exp) == {"rank": 3, "hit1": False, "hit5": True, "rr": pytest.approx(1 / 3)}
    assert run.score(r("a", "b", "c", "d", "e", MID), exp)["hit5"] is False
    assert run.score([], exp) == {"rank": None, "hit1": False, "hit5": False, "rr": 0.0}
    assert run.score([{"source": "imessage", "source_id": MID}], exp)["rank"] is None   # source must match
    rows = [run.score(r(MID), exp), run.score(r("a", MID), exp), run.score([], exp)]
    s = run.summarize(rows)
    assert s == {"n": 3, "hit1": pytest.approx(1 / 3), "hit5": pytest.approx(2 / 3), "mrr": pytest.approx(0.5)}
    assert run.summarize([]) == {"n": 0, "hit1": 0.0, "hit5": 0.0, "mrr": 0.0}
    assert "hit@5=67%" in run.format_summary(s)


def test_run_offline_against_index(index_db, monkeypatch):
    async def fake_search(query, limit=10, **kw):
        hits = {"tuition": [f"personal:{MID}", "personal:other"], "roof": ["work:zzz"]}
        for k, ids in hits.items():
            if k in query:
                return [{"source": "gmail", "source_id": i, "title": "t"} for i in ids]
        return []

    monkeypatch.setattr(search_index, "hybrid_search", fake_search)
    cases = [
        {"query": "tuition invoice", "expected": [{"source": "gmail", "source_id": MID}], "origin": "mined"},
        {"query": "roof quote", "expected": [{"source": "gmail", "source_id": "work:nope"}], "origin": "manual"},
        {"query": "nothing here", "expected": [{"source": "gmail", "source_id": "x"}], "origin": "manual"},
    ]
    rows = run_async(run.run_offline(cases))
    assert [r["rank"] for r in rows] == [1, None, None]
    table = run.format_table(rows)
    assert "yes" in table and "NO" in table and "tuition invoice" in table
    assert run.summarize(rows)["hit5"] == pytest.approx(1 / 3)


def test_main_writes_last_run(tmp_path, monkeypatch, index_db):
    async def fake_search(query, limit=10, **kw):
        return [{"source": "gmail", "source_id": "p:1", "title": "t"}]

    monkeypatch.setattr(search_index, "hybrid_search", fake_search)
    monkeypatch.setattr(run, "LAST_RUN_PATH", tmp_path / "last_run.json")
    cases_path = tmp_path / "cases.jsonl"
    extract.write_cases([{"query": "anything at all here", "expected": [{"source": "gmail", "source_id": "p:1"}],
                          "origin": "manual"}], cases_path)
    assert run.main(["--cases", str(cases_path)]) == 0
    report = json.loads((tmp_path / "last_run.json").read_text(encoding="utf-8"))
    assert report["offline"]["summary"]["hit1"] == 1.0 and "commit" in report
    assert run.main(["--cases", str(tmp_path / "none.jsonl")]) == 1

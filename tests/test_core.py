"""Core helpers: Telegram renderer, FTS sanitizer, message conversion, history budget, revise schema."""

import json
import sqlite3
from types import SimpleNamespace

import aiosqlite
import httpx
import openai
import pytest

import db
import llm
import search_index
from telegram_format import markdown_to_telegram_html as md
from tests.conftest import run_async


# ── markdown → Telegram HTML ───────────────────────────────────────

def test_md_inline_formatting_and_escaping():
    assert md("**bold** and *it* and _it2_ and ~~gone~~") == "<b>bold</b> and <i>it</i> and <i>it2</i> and <s>gone</s>"
    assert md("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert md("use `x < y && z`") == "use <code>x &lt; y &amp;&amp; z</code>"
    assert md("[open in Gmail](https://mail.google.com/mail/#all/abc?x=1&y=2)") == \
        '<a href="https://mail.google.com/mail/#all/abc?x=1&amp;y=2">open in Gmail</a>'
    assert md("2 * 3 * 4 and snake_case_name") == "2 * 3 * 4 and snake_case_name"   # no false italics


def test_md_blocks():
    out = md("```python\nif a < b:\n    print('<')\n```\nafter")
    assert out == "<pre>if a &lt; b:\n    print(&#x27;&lt;&#x27;)</pre>\nafter"
    assert md("# Title\n- one\n* two\n  + nested") == "<b>Title</b>\n• one\n• two\n  • nested"
    assert md("> quoted <line>\n> more\nplain") == "<blockquote>quoted &lt;line&gt;\nmore</blockquote>\nplain"
    assert md("") == ""


def test_md_malformed_input_stays_balanced():
    out = md("**unclosed bold and `unclosed code and <tag> & stuff")
    assert "<tag>" not in out and "&lt;tag&gt;" in out and "&amp;" in out
    assert out.count("<b>") == out.count("</b>")
    assert out.count("<code>") == out.count("</code>")


# ── FTS5 sanitizer ─────────────────────────────────────────────────

NASTY = 'find "the lease" for 12-oak AND john@example.com (urgent) OR NOT * -'


def test_fts_terms_strip_operators_and_quotes():
    terms = search_index._fts_terms(NASTY)
    assert '"' not in "".join(terms)
    assert "john@example.com" in terms and "12-oak" in terms and "(urgent)" in terms


def test_fts_quoted_expression_is_accepted_by_fts5():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(title, content)")
    conn.execute("INSERT INTO t VALUES ('Lease for 12-oak', 'john@example.com sent the lease (urgent)')")
    terms = search_index._fts_terms(NASTY)
    for expr in (" ".join(f'"{t}"' for t in terms), " OR ".join(f'"{t}"' for t in terms)):
        conn.execute("SELECT * FROM t WHERE t MATCH ?", (expr,)).fetchall()      # must not raise
    assert conn.execute("SELECT count(*) FROM t WHERE t MATCH ?",
                        (" OR ".join(f'"{t}"' for t in ["lease", "nothing-else"]),)).fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM t WHERE t MATCH ?", (NASTY,)).fetchall()     # raw query would break


def test_search_falls_back_to_like_on_syntax_error(index_db, monkeypatch):
    async def go():
        await search_index.index_item("gmail", "p:1", "Roof quote", "the roofer quoted 1250 for flashing", {})
        normal = await search_index.search("roofer flashing")

        async def boom(*a, **k):
            raise aiosqlite.OperationalError("fts5: syntax error")

        monkeypatch.setattr(search_index, "_run_match", boom)
        fallback = await search_index.search("roofer flashing")
        empty = await search_index.search('"" ')
        return normal, fallback, empty

    normal, fallback, empty = run_async(go())
    assert [r["source_id"] for r in normal] == ["p:1"] and normal[0]["rank"] != 0
    assert [r["source_id"] for r in fallback] == ["p:1"] and fallback[0]["rank"] == 0   # LIKE path
    assert empty == []


# ── provider-neutral turns → OpenAI wire format ───────────────────

def test_to_openai_messages():
    turns = [
        {"role": "user", "content": "find the lease"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Looking…"},
            {"type": "tool_use", "id": "c1", "name": "search_everything", "input": {"query": "lease"}},
            {"type": "tool_use", "id": "c2", "name": "read_email", "input": {"email_id": "x"}},
        ]},
        {"role": "tool_results", "content": [
            {"tool_use_id": "c1", "content": "r1"}, {"tool_use_id": "c2", "content": "r2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Here it is."}]},
        {"role": "assistant", "content": []},
    ]
    wire = llm._to_openai_messages(turns)
    assert wire[0] == {"role": "user", "content": "find the lease"}
    assert wire[1]["role"] == "assistant" and wire[1]["content"] == "Looking…"
    assert [tc["id"] for tc in wire[1]["tool_calls"]] == ["c1", "c2"]
    assert wire[1]["tool_calls"][0]["function"] == {"name": "search_everything", "arguments": '{"query": "lease"}'}
    assert wire[2] == {"role": "tool", "tool_call_id": "c1", "content": "r1"}
    assert wire[3] == {"role": "tool", "tool_call_id": "c2", "content": "r2"}
    assert wire[4] == {"role": "assistant", "content": "Here it is."}
    assert wire[5] == {"role": "assistant", "content": None} and "tool_calls" not in wire[5]


# ── token-budgeted history keeps whole turns ──────────────────────

def test_get_turn_messages_drops_whole_oldest_turns(index_db):
    big = "lease " * 400  # ~2000 chars → sizeable est_tokens

    async def go():
        await db.save_turn("s", "user", [{"role": "user", "content": big}])
        await db.save_turn("s", "assistant", [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a1", "name": "search_everything", "input": {}}]},
            {"role": "tool_results", "content": [{"tool_use_id": "a1", "content": big}]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
        ])
        await db.save_turn("s", "user", [{"role": "user", "content": "and the deposit?"}])
        await db.save_turn("s", "assistant", [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "b1", "name": "read_email", "input": {}}]},
            {"role": "tool_results", "content": [{"tool_use_id": "b1", "content": "small"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "new answer"}]},
        ])
        conn = await db.get_db()
        cur = await conn.execute("SELECT est_tokens FROM chat_turns WHERE session_id = 's' ORDER BY id")
        est = [r["est_tokens"] for r in await cur.fetchall()]
        everything = await db.get_turn_messages("s", 1_000_000)
        tight = await db.get_turn_messages("s", est[-1] + est[-2])      # exactly the newest two turns fit
        tiny = await db.get_turn_messages("s", 1)
        return everything, tight, tiny

    everything, tight, tiny = run_async(go())
    assert len(everything) == 8 and everything[0]["content"] == big
    assert [m["role"] for m in tight] == ["user", "assistant", "tool_results", "assistant"]
    assert tight[0]["content"] == "and the deposit?"                      # oldest turns dropped whole
    assert len(tiny) == 3 and tiny[0]["role"] == "assistant"               # newest turn always kept
    for msgs in (everything, tight, tiny):
        for i, m in enumerate(msgs):
            if m["role"] == "tool_results":
                prev = msgs[i - 1]
                assert prev["role"] == "assistant"
                assert {b["id"] for b in prev["content"] if b["type"] == "tool_use"} == \
                    {r["tool_use_id"] for r in m["content"]}


# ── revise_draft: strict schema + locked recipient ────────────────

def test_schema_for_each_action_type():
    for action_type, fields in llm._EDITABLE_FIELDS.items():
        s = llm.schema_for(action_type)
        assert s["strict"] is True and s["schema"]["additionalProperties"] is False
        assert set(s["schema"]["properties"]) == set(fields) and s["schema"]["required"] == list(fields)
        assert all(v == {"type": "string"} for v in s["schema"]["properties"].values())
    assert "to" not in llm.schema_for("reply_email")["schema"]["properties"]
    assert set(llm.schema_for("send_imessage")["schema"]["properties"]) == {"body"}
    assert llm.schema_for("unknown")["schema"]["required"] == ["body"]


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)   # each: JSON dict | Exception
        self.formats = []

    async def create(self, **kw):
        self.formats.append(kw["response_format"]["type"])
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(r)))],
                               usage=None)


def _fake_client(monkeypatch, replies):
    fc = FakeCompletions(replies)
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=fc)))
    return fc


def test_revise_cannot_change_recipient(index_db, monkeypatch):
    fc = _fake_client(monkeypatch, [{"to": "evil@x.com", "subject": "pwned", "body": "shorter body",
                                     "cc": "", "bcc": "boss@x.com", "email_id": "zzz"}])
    payload = {"email_id": "m1", "to": "pat@x.com", "subject": "Re: Lease", "body": "long body",
               "cc": "", "bcc": "", "account": "personal"}
    out = run_async(llm.revise_draft("reply_email", payload, "make it shorter"))
    assert out["to"] == "pat@x.com" and out["subject"] == "Re: Lease" and out["email_id"] == "m1"
    assert out["body"] == "shorter body" and out["bcc"] == "boss@x.com"      # editable fields applied
    assert fc.formats == ["json_schema"]
    fc2 = _fake_client(monkeypatch, [{"body": "new text", "to": "+19999999999"}])
    text = run_async(llm.revise_draft("send_imessage", {"to": "+1", "to_name": "Mike", "body": "hi", "service": "SMS"},
                                      "x"))
    assert text == {"to": "+1", "to_name": "Mike", "body": "new text", "service": "SMS"}
    assert fc2.formats == ["json_schema"]


def test_revise_falls_back_when_schema_rejected(index_db, monkeypatch):
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.openai.com/v1/x"))
    bad = openai.BadRequestError("schema unsupported", response=resp, body=None)
    fc = _fake_client(monkeypatch, [bad, {"body": "fallback body"}])
    out = run_async(llm.revise_draft("send_imessage", {"to": "+1", "body": "hi", "service": "iMessage"}, "x"))
    assert out == {"to": "+1", "body": "fallback body", "service": "iMessage"}
    assert fc.formats == ["json_schema", "json_object"]


def test_revise_ignores_non_string_and_bad_json(index_db, monkeypatch):
    _fake_client(monkeypatch, [{"body": 123, "cc": None}])
    out = run_async(llm.revise_draft("reply_email", {"to": "a", "body": "keep", "cc": "c"}, "x"))
    assert out == {"to": "a", "body": "keep", "cc": "c"}

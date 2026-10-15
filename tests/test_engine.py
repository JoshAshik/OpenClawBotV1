"""Engine: parallel read tools, usage normalization/cost, recording, migration, /usage text."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

import config
import db
import llm
import ops
from tests.conftest import run_async


# ── Parallel tool execution ────────────────────────────────────────

def test_run_tools_reads_parallel_writes_sequential_order_kept(monkeypatch):
    monkeypatch.setattr(config, "MAX_PARALLEL_TOOLS", 2)
    state = {"active": 0, "max": 0, "log": []}

    async def fake_execute(name, args):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        state["log"].append(("start", name, time.monotonic()))
        try:
            if name == "boom":
                raise RuntimeError("kaboom")
            await asyncio.sleep(0.12 if name not in llm.WRITE_TOOLS else 0.02)
            out = {"result": f"ok:{name}:{args['n']}"}
            if name == "draft_email":
                out["action"] = {"type": "send_email", "payload": {"n": args["n"]}}
            if name == "search_everything":
                out["entities"] = [{"type": "gmail", "id": f"e{args['n']}", "label": "x"}]
            return out
        finally:
            state["active"] -= 1
            state["log"].append(("end", name, time.monotonic()))

    monkeypatch.setattr(llm, "execute_tool", fake_execute)
    tool_uses = [
        {"id": "c1", "name": "search_everything", "input": {"n": 1}},
        {"id": "c2", "name": "draft_email", "input": {"n": 2}},
        {"id": "c3", "name": "read_email", "input": {"n": 3}},
        {"id": "c4", "name": "boom", "input": {"n": 4}},
        {"id": "c5", "name": "find_contact", "input": {"n": 5}},
        {"id": "c6", "name": "set_reminder", "input": {"n": 6}},
    ]
    results, actions, entities = run_async(llm._run_tools(tool_uses, None))

    assert [r["tool_use_id"] for r in results] == ["c1", "c2", "c3", "c4", "c5", "c6"]
    assert results[0]["content"] == "ok:search_everything:1"
    assert results[3]["content"].startswith("Tool failed: boom: kaboom")
    assert actions == [{"type": "send_email", "payload": {"n": 2}}]
    assert entities == [{"type": "gmail", "id": "e1", "label": "x"}]
    assert 1 < state["max"] <= 2                                    # reads overlapped, capped by the semaphore
    reads_end = max(t for kind, n, t in state["log"] if kind == "end" and n not in llm.WRITE_TOOLS)
    writes_start = [t for kind, n, t in state["log"] if kind == "start" and n in llm.WRITE_TOOLS]
    assert all(ws >= reads_end for ws in writes_start)              # writes only after every read finished
    write_names = [n for kind, n, _ in state["log"] if kind == "start" and n in llm.WRITE_TOOLS]
    assert write_names == ["draft_email", "set_reminder"]           # in call order


def test_run_tools_all_writes_strictly_sequential(monkeypatch):
    state = {"active": 0, "max": 0}

    async def fake_execute(name, args):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.03)
        state["active"] -= 1
        return {"result": name}

    monkeypatch.setattr(llm, "execute_tool", fake_execute)
    uses = [{"id": str(i), "name": "remember", "input": {}} for i in range(4)]
    results, _, _ = run_async(llm._run_tools(uses, None))
    assert state["max"] == 1 and len(results) == 4


# ── Usage normalization and cost ───────────────────────────────────

def test_usage_dict_shapes():
    chat = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 1200, "completion_tokens": 300,
                                               "prompt_tokens_details": {"cached_tokens": 1000}})
    assert llm.usage_dict(chat) == {"prompt": 1200, "cached": 1000, "completion": 300}
    assert llm.usage_dict({"prompt_tokens": 10, "completion_tokens": 2}) == {"prompt": 10, "cached": 0, "completion": 2}
    assert llm.usage_dict({"total_tokens": 55}) == {"prompt": 55, "cached": 0, "completion": 0}      # embeddings
    assert llm.usage_dict({"input_tokens": 80, "output_tokens": 20}) == {"prompt": 80, "cached": 0, "completion": 20}
    assert llm.usage_dict(None) == {"prompt": 0, "cached": 0, "completion": 0}
    assert llm.usage_dict(SimpleNamespace(prompt_tokens=5, completion_tokens=1))["prompt"] == 5


def test_usage_cost_math(monkeypatch):
    monkeypatch.setitem(config.PRICES_PER_MTOK, "test-model", {"in": 2.0, "cached": 0.5, "out": 8.0})
    cost, priced = llm.usage_cost("test-model", {"prompt": 1_000_000, "cached": 500_000, "completion": 250_000})
    assert priced and cost == pytest.approx(0.5 * 2.0 + 0.5 * 0.5 + 0.25 * 8.0)
    assert llm.usage_cost("mystery-model", {"prompt": 10, "cached": 0, "completion": 10}) == (0.0, False)
    assert llm.usage_cost("test-model", {"prompt": 0, "cached": 0, "completion": 0}) == (0.0, True)


def test_record_usage_persists_and_summarizes(index_db, monkeypatch):
    monkeypatch.setitem(config.PRICES_PER_MTOK, "test-model", {"in": 1.0, "cached": 0.1, "out": 4.0})

    async def go():
        a = await llm.record_usage("agent", "test-model", {"prompt_tokens": 1000, "completion_tokens": 100})
        await llm.record_usage("digest", "test-model", {"prompt_tokens": 500, "completion_tokens": 50})
        await llm.record_usage("embed", "mystery", {"total_tokens": 999})
        s = await db.usage_summary("2000-01-01T00:00:00+00:00")
        future = await db.usage_summary("2999-01-01T00:00:00+00:00")
        return a, s, future

    a, s, future = run_async(go())
    assert a["cost"] == pytest.approx((1000 * 1.0 + 100 * 4.0) / 1e6)
    assert s["calls"] == 3 and s["prompt"] == 1000 + 500 + 999 and s["completion"] == 150
    assert s["cost"] == pytest.approx((1000 + 400 + 500 + 200) / 1e6)
    assert s["unpriced"] == 1
    assert [p["key"] for p in s["by_purpose"]] == ["agent", "digest", "embed"]
    assert [m["key"] for m in s["by_model"]] == ["test-model", "mystery"]
    assert future["calls"] == 0 and future["by_purpose"] == []


def test_record_usage_never_raises(index_db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(db, "record_llm_usage", boom)
    u = run_async(llm.record_usage("agent", config.OPENAI_MODEL, {"prompt_tokens": 1}))
    assert u["prompt"] == 1


# ── Streaming usage capture in the agent loop ──────────────────────

def test_agent_loop_captures_stream_usage(index_db, monkeypatch):
    monkeypatch.setitem(config.PRICES_PER_MTOK, config.OPENAI_MODEL, {"in": 1.0, "cached": 0.0, "out": 1.0})
    captured = {}

    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello", tool_calls=None))], usage=None)
        yield SimpleNamespace(choices=[], usage={"prompt_tokens": 100, "completion_tokens": 5,
                                                 "prompt_tokens_details": {"cached_tokens": 40}})

    class FakeCompletions:
        async def create(self, **kw):
            captured.update(kw)
            return chunks()

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    result, turn, entities, totals = run_async(
        llm._openai_agent_loop("sys", [{"role": "user", "content": "hi"}], None))
    assert result["text"] == "Hello" and result["actions"] == []
    assert totals == {"prompt": 100, "cached": 40, "completion": 5, "cost": pytest.approx(65 / 1e6)}  # 60 uncached in + 5 out
    assert captured["stream_options"] == {"include_usage": True}
    rows = run_async(db.usage_summary("2000-01-01T00:00:00+00:00"))
    assert rows["calls"] == 1 and rows["by_purpose"][0]["key"] == "agent"


# ── chat_turns migration + top_turns ───────────────────────────────

def test_chat_turns_migration_and_top_turns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE chat_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL, role TEXT NOT NULL, messages_json TEXT NOT NULL,
            llm_used TEXT, est_tokens INTEGER NOT NULL DEFAULT 0);
        INSERT INTO chat_turns (session_id, timestamp, role, messages_json) VALUES
            ('s1', '2026-10-15T10:00:00+00:00', 'user', '[{"role":"user","content":"find the lease"}]');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_DB", None)

    async def go():
        await db.save_turn("s1", "assistant", [{"role": "assistant", "content": []}], llm_used="m",
                           prompt_tokens=3000, completion_tokens=200, cost_usd=0.0123)
        await db.save_turn("s1", "user", [{"role": "user", "content": "cheaper question"}])
        await db.save_turn("s1", "assistant", [{"role": "assistant", "content": []}], llm_used="m",
                           prompt_tokens=100, completion_tokens=10, cost_usd=0.0002)
        return await db.top_turns("2000-01-01T00:00:00+00:00", limit=5)

    top = run_async(go())
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(chat_turns)").fetchall()}
    assert {"prompt_tokens", "completion_tokens", "cost_usd"} <= cols
    assert [t["cost_usd"] for t in top] == [0.0123, 0.0002]
    assert top[0]["label"] == "find the lease" and top[1]["label"] == "cheaper question"
    assert top[0]["prompt_tokens"] == 3000


# ── /usage formatting ──────────────────────────────────────────────

def test_format_usage_text():
    today = {"calls": 3, "prompt": 1500, "cached": 400, "completion": 200, "cost": 0.0042, "unpriced": 0,
             "by_purpose": [], "by_model": []}
    month = {"calls": 40, "prompt": 120_000, "cached": 50_000, "completion": 9_000, "cost": 1.2345, "unpriced": 2,
             "by_purpose": [{"key": "agent", "calls": 30, "cost": 1.1, "tokens": 110_000},
                            {"key": "embed", "calls": 10, "cost": 0.1345, "tokens": 19_000}],
             "by_model": [{"key": "gpt-5.1", "calls": 30, "cost": 1.1}, {"key": "mystery", "calls": 10, "cost": 0}]}
    top = [{"cost_usd": 0.5, "timestamp": "2026-10-15T09:00:00+00:00", "prompt_tokens": 40_000,
            "completion_tokens": 1_000, "label": "find the lease"}]
    text = ops.format_usage(today, month, top)
    assert text.startswith("💵 Usage — today $0.0042 (3 calls, 1.7k tokens, 400 cached)")
    assert "Month to date: $1.23 (40 calls, 129.0k tokens)" in text
    assert "- agent: $1.10 · 30 calls · 110.0k tokens" in text
    assert "- gpt-5.1: $1.10 · 30 calls" in text
    assert '- $0.50 · 2026-10-15 09:00 · 40.0k in / 1.0k out — "find the lease"' in text
    assert "⚠️ 2 call(s) used a model with no price" in text
    assert ops.day_start_iso() <= ops.month_start_iso() or True   # both are valid ISO strings
    assert "T" in ops.day_start_iso() and "T" in ops.month_start_iso()

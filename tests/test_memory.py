"""
Memory 2.0: automatic extraction + dedupe/merge, relevance-based retrieval, episodic session
summaries, tool-result compaction, and the MEMORY_AUTO_EXTRACT switch.

No network: the fast-model call (memory._fast_json) and embeddings.embed_texts are replaced
with deterministic stand-ins. Vectors are 4-dim unit vectors keyed by phrases in the text, so
cosine similarity between two texts is predictable.
"""

import asyncio
import json

import numpy as np
import pytest

import config
import db
import embeddings
import llm
import memory
from tests.conftest import run_async


# ── Deterministic embeddings ───────────────────────────────────────
#
# Each known phrase maps to a basis direction; a text's vector is the normalized sum of the
# directions of the phrases it contains. Unknown text gets its own orthogonal-ish hash vector.

_AXES = {
    "sign": 0, "josh": 0,          # signature preference
    "plumber": 1, "mike": 1,       # Mike the plumber
    "oak": 2, "12 oak": 2,         # the Oak St property
    "tone": 3, "formal": 3,        # tone preference
}


def _vec(text: str) -> np.ndarray:
    v = np.zeros(8, dtype=np.float32)
    low = text.lower()
    hit = False
    for phrase, axis in _AXES.items():
        if phrase in low:
            v[axis] = 1.0
            hit = True
    if not hit:
        v[4 + (hash(low) % 4)] = 1.0
    if "near-dup" in low:           # tilt slightly off its axis: sim ≈ 0.86 with the pure one
        v[7] += 0.6
    return v / np.linalg.norm(v)


@pytest.fixture
def mem(index_db, monkeypatch):
    """Temp DB, fake embeddings, fake fast model with a scripted reply, background → inline."""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", True)
    calls = {"embed": [], "fast": []}
    script = {"facts": [], "merge": None, "summary": "Talked about things.", "entities": ["X"]}

    async def fake_embed(texts):
        calls["embed"].append(list(texts))
        return [_vec(t) for t in texts]

    async def fake_fast(prompt, schema, purpose, max_tokens=600):
        calls["fast"].append((purpose, prompt))
        if purpose == "memory":
            return {"facts": script["facts"]}
        if purpose == "memory_merge":
            return {"content": script["merge"] or "MERGED"}
        if purpose == "summary":
            return {"summary": script["summary"], "entities": script["entities"]}
        return {}

    async def inline(coro):
        await coro

    monkeypatch.setattr(embeddings, "embed_texts", fake_embed)
    monkeypatch.setattr(memory, "_fast_json", fake_fast)
    monkeypatch.setattr(llm, "_background", inline)
    return {"calls": calls, "script": script}


# ── 1. Extraction: store / dedupe / merge ──────────────────────────

def test_extraction_stores_new_facts_with_source_auto(mem):
    mem["script"]["facts"] = [
        {"content": "The user signs emails as Josh.", "category": "preference"},
        {"content": "Mike is the plumber for the Oak St property.", "category": "person"},
    ]

    async def run():
        out = await memory.extract_and_store("sign my emails Josh; Mike the plumber", "ok")
        return out, await db.list_memories(), await db.load_memory_embeddings()

    out, mems, vecs = run_async(run())
    assert [o["action"] for o in out] == ["add", "add"]
    assert {m["source"] for m in mems} == {"auto"}
    assert {m["category"] for m in mems} == {"preference", "person"}
    assert len(vecs) == 2                                  # both embedded on insert
    # the extractor was shown the known memories and both halves of the exchange
    purpose, prompt = mem["calls"]["fast"][0]
    assert purpose == "memory" and "USER SAID" in prompt and "ASSISTANT ANSWERED" in prompt


def test_extraction_skips_near_identical_fact(mem):
    async def run():
        await memory.store_memory("The user signs emails as Josh.", "preference")
        mem["script"]["facts"] = [{"content": "User signs their emails as Josh", "category": "preference"}]
        out = await memory.extract_and_store("x", "y")
        return out, await db.list_memories()

    out, mems = run_async(run())
    assert out[0]["action"] == "skip" and out[0]["score"] >= memory.SIM_SKIP
    assert len(mems) == 1 and mems[0]["source"] == "explicit"   # untouched


def test_extraction_merges_similar_fact_keeping_newer_detail(mem):
    mem["script"]["merge"] = "Mike (555-0100) is the plumber for Oak St."

    async def run():
        old_id = await memory.store_memory("Mike is the plumber.", "person")
        mem["script"]["facts"] = [{"content": "near-dup: Mike the plumber's number is 555-0100",
                                   "category": "person"}]
        out = await memory.extract_and_store("x", "y")
        return old_id, out, await db.list_memories(), await db.load_memory_embeddings()

    old_id, out, mems, vecs = run_async(run())
    assert out[0]["action"] == "merge" and out[0]["id"] == old_id
    assert memory.SIM_MERGE <= out[0]["score"] < memory.SIM_SKIP
    assert len(mems) == 1 and mems[0]["content"] == "Mike (555-0100) is the plumber for Oak St."
    assert mems[0]["source"] == "explicit"                  # merge keeps the row, rewrites content
    assert [v[0] for v in vecs] == [old_id]                 # re-embedded, no orphan vector
    merge_prompt = [p for (purpose, p) in mem["calls"]["fast"] if purpose == "memory_merge"][0]
    assert "OLDER: Mike is the plumber." in merge_prompt and "555-0100" in merge_prompt


def test_extraction_never_raises_and_caps_at_three(mem):
    mem["script"]["facts"] = [{"content": f"fact {i} unique-{i}", "category": "other"} for i in range(6)]
    out = run_async(memory.extract_and_store("hello", "world"))
    assert len(out) == memory.MAX_FACTS

    async def boom(*a, **k):
        raise RuntimeError("model down")

    import pytest as _p
    with _p.MonkeyPatch.context() as mp:
        mp.setattr(memory, "_fast_json", boom)
        assert run_async(memory.extract_and_store("hello", "world")) == []


def test_auto_extract_off_disables_extraction(mem, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    mem["script"]["facts"] = [{"content": "The user likes formal tone.", "category": "preference"}]
    assert run_async(memory.extract_and_store("be formal", "sure")) == []
    assert run_async(db.list_memories()) == []
    assert mem["calls"]["fast"] == []


def test_agent_chat_schedules_extraction_only_when_enabled(mem, monkeypatch):
    """agent_chat → _background(extract_and_store) after the turn is saved; off when disabled."""
    scheduled = []

    async def fake_loop(system, messages, progress):
        return {"text": "done", "actions": []}, [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}], [], \
            {"prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}

    async def fake_extract(user_message, answer):
        scheduled.append((user_message, answer))
        return []

    monkeypatch.setattr(llm, "_openai_agent_loop", fake_loop)
    monkeypatch.setattr(llm, "_get_accounts_cached", _no_accounts)
    monkeypatch.setattr(memory, "extract_and_store", fake_extract)

    run_async(llm.agent_chat("remember I prefer formal tone"))
    assert scheduled == [("remember I prefer formal tone", "done")]

    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    run_async(llm.agent_chat("another"))
    assert len(scheduled) == 1


async def _no_accounts():
    return []


# ── 2. Relevance-based retrieval ───────────────────────────────────

def test_select_memories_picks_by_similarity_and_always_includes_newest(mem):
    async def run():
        ids = {}
        ids["sign"] = await memory.store_memory("The user signs emails as Josh.", "preference")
        ids["plumber"] = await memory.store_memory("Mike is the plumber.", "person")
        ids["oak"] = await memory.store_memory("12 Oak St is the duplex.", "property")
        ids["tone"] = await memory.store_memory("The user prefers a formal tone.", "preference")
        # filler: unrelated memories, the last one is the newest
        for i in range(6):
            ids[f"f{i}"] = await memory.store_memory(f"filler fact number {i}", "other")
        picked = await memory.select_memories("who is the plumber for 12 oak?", top_k=2, newest=1)
        return ids, picked

    ids, picked = run_async(run())
    picked_ids = [m["id"] for m in picked]
    assert picked_ids[:2] in ([ids["plumber"], ids["oak"]], [ids["oak"], ids["plumber"]])   # top-2 by similarity
    assert picked_ids[-1] == ids["f5"] and picked[-1]["score"] is None                      # newest appended
    assert ids["sign"] not in picked_ids and ids["tone"] not in picked_ids
    assert picked[0]["score"] > 0.5


def test_select_memories_falls_back_to_newest_without_embeddings(mem, monkeypatch):
    async def run():
        for i in range(3):
            await db.add_memory(f"m{i}")
        monkeypatch.setattr(config, "OPENAI_API_KEY", "")           # embeddings disabled
        return await memory.select_memories("anything")

    picked = run_async(run())
    assert [m["content"] for m in picked] == ["m2", "m1", "m0"]


def test_select_memories_embeds_explicit_memories_lazily(mem):
    """Rows saved before Memory 2.0 (no vector) get embedded on first retrieval."""
    async def run():
        await db.add_memory("Mike is the plumber.", "person")       # raw insert, no vector
        picked = await memory.select_memories("plumber")
        return picked, await db.load_memory_embeddings()

    picked, vecs = run_async(run())
    assert [m["content"] for m in picked] == ["Mike is the plumber."] and len(vecs) == 1


def test_system_prompt_uses_relevant_memories_and_caps_length(mem, monkeypatch):
    monkeypatch.setattr(llm, "_get_accounts_cached", _no_accounts)
    monkeypatch.setattr(memory, "TOP_K", 2)

    async def run():
        await memory.store_memory("Mike is the plumber.", "person")
        await memory.store_memory("The user prefers a formal tone.", "preference")
        await memory.store_memory("long " * 600, "other")           # newest, > 2000 chars alone
        return await llm.build_system_prompt("who is the plumber?")

    prompt = run_async(run())
    block = prompt.split("MEMORY (")[1].split("\n\n")[0]
    assert "Mike is the plumber." in block
    assert block.index("Mike is the plumber.") < block.index("long long")   # relevance first, newest after
    assert len(block) <= memory.PROMPT_CHAR_CAP + 200 and block.rstrip().endswith("…")


# ── 3. Episodic session summaries ──────────────────────────────────

def _turn(role, text):
    if role == "user":
        return [{"role": "user", "content": text}]
    return [{"role": "assistant", "content": [{"type": "text", "text": text}]}]


def test_summary_written_when_session_is_reset(mem, monkeypatch):
    mem["script"]["summary"] = "Josh asked about the Oak St lease; the assistant found it expires in May."
    mem["script"]["entities"] = ["Oak St", "lease"]

    async def run():
        first = await llm.get_current_session()
        await db.save_turn(first, "user", _turn("user", "when does the oak st lease end?"))
        await db.save_turn(first, "assistant", _turn("assistant", "It ends in May."))
        second = await llm.start_new_session()                      # /new
        return first, second, await db.list_session_summaries(limit=5)

    first, second, summaries = run_async(run())
    assert first != second
    assert len(summaries) == 1 and summaries[0]["session_id"] == first
    assert summaries[0]["summary"].startswith("Josh asked about the Oak St lease")
    assert summaries[0]["entities"] == ["Oak St", "lease"]
    purpose, prompt = [c for c in mem["calls"]["fast"] if c[0] == "summary"][0]
    assert "User: when does the oak st lease end?" in prompt and "Assistant: It ends in May." in prompt


def test_summary_written_on_idle_reset_and_not_for_empty_session(mem, monkeypatch):
    async def run():
        first = await llm.get_current_session()
        # No turns yet → a reset writes nothing
        await llm.start_new_session()
        none_yet = await db.list_session_summaries()
        sid = await llm.get_current_session()
        await db.save_turn(sid, "user", _turn("user", "hi"))
        await db.save_turn(sid, "assistant", _turn("assistant", "hello"))
        monkeypatch.setattr(config, "SESSION_IDLE_RESET_MINUTES", -1)   # everything counts as idle
        new = await llm.get_current_session()
        return first, none_yet, sid, new, await db.list_session_summaries()

    first, none_yet, sid, new, summaries = run_async(run())
    assert none_yet == []
    assert new != sid and [s["session_id"] for s in summaries] == [sid]


def test_summary_written_when_budget_drops_turns_and_only_once(mem, monkeypatch):
    monkeypatch.setattr(llm, "_get_accounts_cached", _no_accounts)
    seen_history = []

    async def fake_loop(system, messages, progress):
        seen_history.append((system, messages))
        return {"text": "ok", "actions": []}, _turn("assistant", "ok"), [], \
            {"prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}

    monkeypatch.setattr(llm, "_openai_agent_loop", fake_loop)
    mem["script"]["summary"] = "Earlier the user asked about rent at 12 Oak."
    mem["script"]["entities"] = ["12 Oak"]

    async def run():
        sid = await llm.get_current_session()
        await db.save_turn(sid, "user", _turn("user", "old question about 12 oak rent " * 20))
        await db.save_turn(sid, "assistant", _turn("assistant", "old answer " * 40))
        await db.save_turn(sid, "user", _turn("user", "newer question"))
        await db.save_turn(sid, "assistant", _turn("assistant", "newer answer"))
        est = [r["est_tokens"] for r in await (await (await db.get_db()).execute(
            "SELECT est_tokens FROM chat_turns ORDER BY id")).fetchall()]
        monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", est[-1] + est[-2] + 2)   # newest two fit
        await llm.agent_chat("third question")
        first_pass = await db.list_session_summaries()
        est = [r["est_tokens"] for r in await (await (await db.get_db()).execute(
            "SELECT est_tokens FROM chat_turns ORDER BY id")).fetchall()]
        monkeypatch.setattr(config, "HISTORY_TOKEN_BUDGET", sum(est[2:]) + 2)     # same two turns drop again
        await llm.agent_chat("fourth question")
        return first_pass, await db.list_session_summaries()

    first_pass, second_pass = run_async(run())
    assert len(first_pass) == 1 and first_pass[0]["summary"].startswith("Earlier the user asked")
    assert len(second_pass) == 1                                   # summarized_through stops a repeat
    summary_calls = [c for c in mem["calls"]["fast"] if c[0] == "summary"]
    assert len(summary_calls) == 1 and "old question about 12 oak" in summary_calls[0][1]
    # The dropped turns are not in the history sent to the model, and the summary is in the prompt
    system, messages = seen_history[-1]
    assert not any("old question" in str(m.get("content")) for m in messages)
    assert "EARLIER CONVERSATIONS" in system and "Earlier the user asked about rent at 12 Oak." in system


def test_summaries_injected_three_most_recent(mem, monkeypatch):
    monkeypatch.setattr(llm, "_get_accounts_cached", _no_accounts)

    async def run():
        for i in range(5):
            await db.add_session_summary(f"s{i}", f"Summary number {i}.", [f"E{i}"])
        return await llm.build_system_prompt("hello")

    prompt = run_async(run())
    block = prompt.split("EARLIER CONVERSATIONS")[1]
    assert "Summary number 4." in block and "Summary number 2." in block
    assert "Summary number 1." not in block and "entities: E4" in block
    assert block.index("Summary number 2.") < block.index("Summary number 4.")   # oldest first


def test_transcript_flattens_tools_and_truncates():
    turns = [{"id": 1, "messages": [{"role": "user", "content": "find the lease"}]},
             {"id": 2, "messages": [
                 {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "search_everything",
                                                    "input": {"q": "lease"}}]},
                 {"role": "tool_results", "content": [{"tool_use_id": "c1", "content": "SECRET BIG BLOB"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": "Found it."}]}]}]
    text = memory.transcript(turns)
    assert text == "User: find the lease\nAssistant used: search_everything\nAssistant: Found it."
    assert "SECRET" not in text                                  # tool payloads stay out of summaries
    assert len(memory.transcript(turns * 500, limit=100)) == 101


# ── 4. Tool-result compaction ──────────────────────────────────────

def _tool_turn(i: int, size: int):
    return [
        {"role": "user", "content": f"q{i}"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": f"call{i}", "name": "search_everything",
                                           "input": {"q": f"q{i}"}}]},
        {"role": "tool_results", "content": [{"tool_use_id": f"call{i}", "content": "x" * size}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]},
    ]


def test_compaction_truncates_only_old_tool_results_and_keeps_pairing():
    history = _tool_turn(1, 1000) + _tool_turn(2, 1000) + _tool_turn(3, 1000) + _tool_turn(4, 100)
    out = llm.compact_history(history)

    assert len(out) == len(history)                                   # nothing removed
    assert [m["role"] for m in out] == [m["role"] for m in history]
    results = [m for m in out if m["role"] == "tool_results"]
    assert [r["content"][0]["tool_use_id"] for r in results] == ["call1", "call2", "call3", "call4"]
    assert results[0]["content"][0]["content"] == "x" * 400 + llm.COMPACT_MARKER     # old → truncated
    assert results[1]["content"][0]["content"] == "x" * 400 + llm.COMPACT_MARKER
    assert results[2]["content"][0]["content"] == "x" * 1000                          # 2 most recent turns intact
    assert results[3]["content"][0]["content"] == "x" * 100
    assert history[0 + 2]["content"][0]["content"] == "x" * 1000                      # input untouched
    # wire conversion still pairs every tool call with its result
    wire = llm._to_openai_messages(out)
    calls = [tc["id"] for m in wire if m.get("tool_calls") for tc in m["tool_calls"]]
    answers = [m["tool_call_id"] for m in wire if m["role"] == "tool"]
    assert calls == answers == ["call1", "call2", "call3", "call4"]


def test_compaction_leaves_short_histories_alone():
    history = _tool_turn(1, 5000) + _tool_turn(2, 5000)
    assert llm.compact_history(history) == history
    assert llm.compact_history([]) == []
    old_short = _tool_turn(1, 50) + _tool_turn(2, 10) + _tool_turn(3, 10)
    assert llm.compact_history(old_short) == old_short                 # under 400 chars: no marker


def test_agent_chat_sends_compacted_history_but_stores_full(mem, monkeypatch):
    monkeypatch.setattr(llm, "_get_accounts_cached", _no_accounts)
    sent = []

    async def fake_loop(system, messages, progress):
        sent.append(messages)
        return {"text": "ok", "actions": []}, [], [], {"prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}

    monkeypatch.setattr(llm, "_openai_agent_loop", fake_loop)

    async def run():
        sid = await llm.get_current_session()
        for i in (1, 2, 3):
            t = _tool_turn(i, 900)
            await db.save_turn(sid, "user", t[:1])
            await db.save_turn(sid, "assistant", t[1:])
        await llm.agent_chat("q4")
        return await db.get_turn_messages(sid, 10_000_000)

    stored = run_async(run())
    sent_results = [m for m in sent[0] if m["role"] == "tool_results"]
    assert sent_results[0]["content"][0]["content"].endswith(llm.COMPACT_MARKER)
    assert sent_results[-1]["content"][0]["content"] == "x" * 900
    stored_results = [m for m in stored if m["role"] == "tool_results"]
    assert all(r["content"][0]["content"] == "x" * 900 for r in stored_results)   # DB keeps full results


# ── DB helpers ─────────────────────────────────────────────────────

def test_get_turn_messages_with_dropped_reports_oldest_turns(index_db):
    async def run():
        for i in range(4):
            await db.save_turn("s", "user", [{"role": "user", "content": f"m{i} " * 50}])
        est = [r["est_tokens"] for r in await (await (await db.get_db()).execute(
            "SELECT est_tokens FROM chat_turns ORDER BY id")).fetchall()]
        kept, dropped = await db.get_turn_messages_with_dropped("s", est[-1] + est[-2])
        return kept, dropped

    kept, dropped = run_async(run())
    assert [m["content"][:2] for m in kept] == ["m2", "m3"]
    assert [d["messages"][0]["content"][:2] for d in dropped] == ["m0", "m1"]
    assert dropped[0]["id"] < dropped[1]["id"] and dropped[0]["role"] == "user"


def test_memories_source_column_migration(tmp_path, monkeypatch):
    """A pre-Memory-2.0 database (memories without `source`) gets the column on open."""
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                               category TEXT, content TEXT NOT NULL);
        INSERT INTO memories (created_at, category, content) VALUES ('2026-01-01', NULL, 'legacy');
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "_DB", None)
    listing = run_async(db.list_memories())
    assert listing == [{"id": 1, "created_at": "2026-01-01", "category": None, "content": "legacy",
                        "source": "explicit"}]


def test_memory_listing_by_source_and_forget_removes_vector(index_db):
    async def run():
        mid = await db.add_memory("old row")
        await db.store_memory_embedding(mid, np.ones(4, dtype=np.float32).tobytes(), "m", 4)
        auto = await db.add_memory("auto row", "person", source="auto")
        listing = await db.list_memories()
        only_auto = await db.list_memories(source="auto")
        ok = await db.delete_memory(mid)
        return listing, only_auto, ok, await db.load_memory_embeddings(), await db.memories_without_embedding()

    listing, only_auto, ok, vecs, missing = run_async(run())
    assert [(m["content"], m["source"]) for m in listing] == [("auto row", "auto"), ("old row", "explicit")]
    assert [m["content"] for m in only_auto] == ["auto row"]
    assert ok and vecs == [] and [m["content"] for m in missing] == ["auto row"]

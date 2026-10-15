"""
Memory 2.0 — the long-term / episodic memory pipeline behind the agent.

Three layers, all cheap (fast model + embeddings) and never on the reply's critical path:

1. **Automatic extraction** — after every agent turn, `extract_and_store` asks the fast model
   (strict JSON schema) for 0–3 durable facts in the user's message + answer, then dedupes
   each against the existing memories by cosine similarity of their embeddings:
   ≥ SIM_SKIP → already known, drop; SIM_MERGE..SIM_SKIP → rewrite the existing memory with
   the newer detail; below → store as a new 'auto' memory. The explicit `remember` tool
   stays (source 'explicit').
2. **Relevance retrieval** — `select_memories(query)` returns the TOP_K memories closest to
   the current user message plus the NEWEST most recent ones, so the prompt carries what
   matters now instead of just the latest 30.
3. **Episodic summaries** — `summarize_turns` condenses turns that fell out of the history
   budget or belong to a session being closed into a `session_summaries` row (≤ 120 words +
   key entities); the prompt shows the 3 most recent under "EARLIER CONVERSATIONS".

Every OpenAI call goes through `llm.with_retry` / `llm.record_usage` (purposes "memory",
"memory_merge", "summary"); embeddings reuse `embeddings.embed_texts`. Nothing here is
outbound, so none of it touches the approval flow.
"""

import json
import logging

import numpy as np

import config
import db
import embeddings

logger = logging.getLogger(__name__)

TOP_K = 12          # memories picked by similarity to the current message
NEWEST = 5          # most recent memories always included
PROMPT_CHAR_CAP = 2000
SIM_SKIP = 0.92     # cosine ≥ this: the fact is already stored
SIM_MERGE = 0.80    # cosine in [SIM_MERGE, SIM_SKIP): rewrite the existing memory
MAX_FACTS = 3
SUMMARY_WORDS = 120
SUMMARIES_IN_PROMPT = 3
_TRANSCRIPT_CHARS = 12000

CATEGORIES = ["preference", "person", "property", "instruction", "other"]

_EXTRACT_SCHEMA = {
    "name": "memory_facts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "content": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                    },
                    "required": ["content", "category"],
                },
            }
        },
        "required": ["facts"],
    },
}

_MERGE_SCHEMA = {
    "name": "memory_merge",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    },
}

_SUMMARY_SCHEMA = {
    "name": "session_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "entities"],
    },
}


# ── Fast-model JSON call (the one seam tests stub) ────────────────

async def _fast_json(prompt: str, schema: dict, purpose: str, max_tokens: int = 600) -> dict:
    """One strict-JSON call to the fast model; returns the parsed object ({} on bad JSON)."""
    import llm  # lazy: llm imports memory

    response = await llm.with_retry(lambda: llm._client().chat.completions.create(
        model=config.OPENAI_FAST_MODEL,
        max_completion_tokens=max_tokens,
        response_format={"type": "json_schema", "json_schema": schema},
        messages=[{"role": "user", "content": prompt}],
    ), purpose)
    await llm.record_usage(purpose, config.OPENAI_FAST_MODEL, getattr(response, "usage", None))
    try:
        data = json.loads(response.choices[0].message.content or "{}")
    except (json.JSONDecodeError, AttributeError, IndexError):
        data = {}
    return data if isinstance(data, dict) else {}


# ── Embeddings over memories ──────────────────────────────────────

def _available() -> bool:
    return embeddings.is_enabled()


async def _embed(texts: list[str]) -> list[np.ndarray]:
    return await embeddings.embed_texts(texts)


async def embed_memory(memory_id: int, content: str) -> None:
    """Best effort: store the vector for one memory (retrieval falls back without it)."""
    if not _available():
        return
    try:
        vec = (await _embed([content]))[0]
        await db.store_memory_embedding(memory_id, vec.astype(np.float32).tobytes(),
                                        config.EMBEDDING_MODEL, len(vec))
    except Exception as e:
        logger.warning(f"embed_memory #{memory_id} failed: {e}")


async def ensure_embeddings(limit: int = 200) -> int:
    """Embed memories that have no vector yet (explicit saves, pre-existing rows). Returns count."""
    if not _available():
        return 0
    missing = await db.memories_without_embedding(limit)
    if not missing:
        return 0
    try:
        vectors = await _embed([m["content"] for m in missing])
    except Exception as e:
        logger.warning(f"ensure_embeddings failed: {e}")
        return 0
    for m, vec in zip(missing, vectors):
        await db.store_memory_embedding(m["id"], vec.astype(np.float32).tobytes(),
                                        config.EMBEDDING_MODEL, len(vec))
    return len(missing)


async def _matrix() -> tuple[list[int], np.ndarray | None]:
    ids, vecs = [], []
    for memory_id, dim, blob in await db.load_memory_embeddings():
        v = np.frombuffer(blob, dtype=np.float32)
        if len(v) != dim:
            continue
        ids.append(memory_id)
        vecs.append(v)
    return ids, (np.vstack(vecs) if vecs else None)


async def store_memory(content: str, category: str | None = None, source: str = "explicit") -> int:
    """Insert a memory and embed it (used by the `remember` tool and the extractor)."""
    memory_id = await db.add_memory(content, category, source)
    await embed_memory(memory_id, content)
    return memory_id


# ── 1. Automatic extraction ───────────────────────────────────────

async def _extract_facts(user_message: str, answer: str, known: list[dict]) -> list[dict]:
    known_text = "\n".join(f"- {m['content']}" for m in known[:40]) or "(none)"
    prompt = (
        "You maintain the long-term memory of a personal assistant for a landlord / property "
        f"manager. From the exchange below, extract at most {MAX_FACTS} DURABLE facts worth "
        "remembering across future conversations: the user's preferences (tone, signature, "
        "how they like things done), people (names, roles, relationships, contact details), "
        "properties (addresses, units, nicknames, tenants, vendors), and standing instructions "
        "(\"always…\", \"never…\", \"from now on…\"). Each fact is one short self-contained "
        "sentence in the third person (\"The user …\"). Do NOT extract one-off tasks, questions, "
        "things that will be stale in a week, email contents the assistant merely reported, or "
        "anything already covered by the KNOWN list. Return an empty list when nothing qualifies "
        "— that is the common case.\n\n"
        f"KNOWN (already remembered):\n{known_text}\n\n"
        f"USER SAID:\n{user_message[:3000]}\n\n"
        f"ASSISTANT ANSWERED:\n{answer[:2000]}"
    )
    data = await _fast_json(prompt, _EXTRACT_SCHEMA, "memory", max_tokens=400)
    facts = []
    for f in data.get("facts", []) or []:
        if not isinstance(f, dict):
            continue
        content = (f.get("content") or "").strip()
        if not content:
            continue
        cat = f.get("category") if f.get("category") in CATEGORIES else "other"
        facts.append({"content": content[:400], "category": cat})
    return facts[:MAX_FACTS]


async def _merge_text(existing: str, new: str) -> str:
    prompt = (
        "Two memory notes about the same fact. Rewrite them as ONE short sentence that keeps "
        "every detail, preferring the NEWER note wherever they disagree. Return only the sentence.\n\n"
        f"OLDER: {existing}\nNEWER: {new}"
    )
    data = await _fast_json(prompt, _MERGE_SCHEMA, "memory_merge", max_tokens=200)
    merged = (data.get("content") or "").strip()
    return merged[:400] or new


async def _dedupe_and_store(fact: dict) -> dict:
    """Decide what to do with one extracted fact. Returns {"action": skip|merge|add, "id": ...}."""
    content, category = fact["content"], fact.get("category")
    if not _available():
        memory_id = await db.add_memory(content, category, "auto")
        return {"action": "add", "id": memory_id}

    await ensure_embeddings()
    ids, matrix = await _matrix()
    try:
        qvec = (await _embed([content]))[0]
    except Exception as e:
        logger.warning(f"memory dedupe embed failed, storing anyway: {e}")
        memory_id = await db.add_memory(content, category, "auto")
        return {"action": "add", "id": memory_id}

    if matrix is not None and len(ids):
        scores = matrix @ qvec
        best = int(np.argmax(scores))
        best_score = float(scores[best])
        if best_score >= SIM_SKIP:
            return {"action": "skip", "id": ids[best], "score": best_score}
        if best_score >= SIM_MERGE:
            existing = await db.get_memory(ids[best])
            if existing:
                merged = await _merge_text(existing["content"], content)
                await db.update_memory(existing["id"], merged, category)
                await embed_memory(existing["id"], merged)
                return {"action": "merge", "id": existing["id"], "score": best_score}

    memory_id = await db.add_memory(content, category, "auto")
    await db.store_memory_embedding(memory_id, qvec.astype(np.float32).tobytes(),
                                    config.EMBEDDING_MODEL, len(qvec))
    return {"action": "add", "id": memory_id}


async def extract_and_store(user_message: str, answer: str) -> list[dict]:
    """Post-turn hook: extract durable facts and store/merge them. Never raises."""
    if not config.MEMORY_AUTO_EXTRACT:
        return []
    if not (user_message or "").strip():
        return []
    try:
        known = await db.list_memories(limit=60)
        facts = await _extract_facts(user_message, answer or "", known)
        outcomes = []
        for fact in facts:
            outcome = await _dedupe_and_store(fact)
            outcome["content"] = fact["content"]
            outcomes.append(outcome)
        if outcomes:
            logger.info("memory: " + ", ".join(f"{o['action']} #{o['id']}" for o in outcomes))
        return outcomes
    except Exception as e:
        logger.warning(f"memory extraction failed: {e}")
        return []


# ── 2. Relevance-based retrieval ──────────────────────────────────

async def select_memories(query: str, top_k: int = TOP_K, newest: int = NEWEST) -> list[dict]:
    """
    The memories to show the model for this message: the `top_k` closest by cosine
    similarity to `query` (relevance order) followed by the `newest` most recent ones not
    already picked. Without embeddings (no key, API failure, nothing embedded yet) it
    degrades to the newest 30, which is what the prompt used to carry.
    """
    fallback_n = 30
    if not query or not query.strip() or not _available():
        return await db.list_memories(limit=fallback_n)

    try:
        await ensure_embeddings()
        ids, matrix = await _matrix()
        if matrix is None or not ids:
            return await db.list_memories(limit=fallback_n)
        qvec = (await _embed([query]))[0]
    except Exception as e:
        logger.warning(f"memory retrieval degraded to newest: {e}")
        return await db.list_memories(limit=fallback_n)

    scores = matrix @ qvec
    order = np.argsort(-scores)[:top_k]
    picked_ids = [ids[i] for i in order]
    score_by_id = {ids[i]: float(scores[i]) for i in order}

    by_id: dict[int, dict] = {}
    for m in await db.list_memories(limit=1000):
        by_id[m["id"]] = m
    out = [dict(by_id[i], score=score_by_id[i]) for i in picked_ids if i in by_id]
    seen = {m["id"] for m in out}
    for m in await db.list_memories(limit=newest):
        if m["id"] not in seen:
            out.append(dict(m, score=None))
            seen.add(m["id"])
    return out


def format_memories(memories: list[dict], cap: int = PROMPT_CHAR_CAP) -> str:
    lines = [f"  #{m['id']}: {m['content']}" for m in memories]
    text = "\n".join(lines)
    if len(text) > cap:
        text = text[:cap] + "\n  …"
    return text


# ── 3. Episodic session summaries ─────────────────────────────────

def _through_key(session_id: str) -> str:
    return f"summarized_through:{session_id}"


def transcript(turns: list[dict], limit: int = _TRANSCRIPT_CHARS) -> str:
    """Flatten stored turns into a plain transcript (tool calls by name only)."""
    lines = []
    for t in turns:
        for m in t.get("messages", []):
            role = m.get("role")
            if role == "user":
                lines.append(f"User: {str(m.get('content', ''))[:1500]}")
            elif role == "assistant":
                texts, tools = [], []
                for b in m.get("content", []) or []:
                    if b.get("type") == "text" and b.get("text"):
                        texts.append(b["text"])
                    elif b.get("type") == "tool_use":
                        tools.append(b.get("name", "tool"))
                if tools:
                    lines.append(f"Assistant used: {', '.join(tools)}")
                if texts:
                    lines.append("Assistant: " + "\n".join(texts)[:1500])
    text = "\n".join(lines)
    if len(text) > limit:
        text = "…" + text[-limit:]
    return text


async def _summarize_text(text: str) -> tuple[str, list[str]]:
    prompt = (
        f"Summarize this conversation between a landlord / property manager and their assistant "
        f"in at most {SUMMARY_WORDS} words, written so a future conversation can pick up where it "
        "left off: what was asked, what was found or decided, what is still open. Then list the "
        "key entities (people, properties, companies, documents) as short strings. Never invent "
        "details.\n\n" + text
    )
    data = await _fast_json(prompt, _SUMMARY_SCHEMA, "summary", max_tokens=500)
    summary = (data.get("summary") or "").strip()
    words = summary.split()
    if len(words) > SUMMARY_WORDS + 20:
        summary = " ".join(words[:SUMMARY_WORDS]) + "…"
    entities = [str(e)[:80] for e in (data.get("entities") or []) if str(e).strip()][:12]
    return summary, entities


async def summarize_turns(session_id: str, turns: list[dict]) -> int | None:
    """
    Summarize the given stored turns of a session (those with an id above the session's
    `summarized_through` mark) into one session_summaries row. Returns the row id or None
    when there was nothing new to summarize. Never raises.
    """
    try:
        through = int(await db.get_setting(_through_key(session_id), "0") or 0)
        fresh = [t for t in turns if int(t.get("id", 0)) > through]
        if not fresh:
            return None
        text = transcript(fresh)
        if not text.strip():
            await db.set_setting(_through_key(session_id), str(max(int(t["id"]) for t in fresh)))
            return None
        summary, entities = await _summarize_text(text)
        if not summary:
            return None
        row_id = await db.add_session_summary(session_id, summary, entities)
        await db.set_setting(_through_key(session_id), str(max(int(t["id"]) for t in fresh)))
        logger.info(f"memory: summarized {len(fresh)} turns of {session_id} → summary #{row_id}")
        return row_id
    except Exception as e:
        logger.warning(f"session summary failed for {session_id}: {e}")
        return None


async def close_session(session_id: str) -> int | None:
    """Session closed (idle reset or /new): summarize whatever of it was not summarized yet."""
    try:
        through = int(await db.get_setting(_through_key(session_id), "0") or 0)
        turns = await db.get_session_turns(session_id, after_id=through)
    except Exception as e:
        logger.warning(f"close_session {session_id}: {e}")
        return None
    return await summarize_turns(session_id, turns)


def format_summaries(summaries: list[dict]) -> str:
    lines = []
    for s in reversed(summaries):  # oldest first reads naturally
        day = (s.get("created_at") or "")[:10]
        ents = ", ".join(s.get("entities") or [])
        line = f"  [{day}] {s['summary']}"
        if ents:
            line += f" (entities: {ents})"
        lines.append(line)
    return "\n".join(lines)

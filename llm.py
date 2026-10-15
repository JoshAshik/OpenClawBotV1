"""
LLM agent — routes natural language to tool calls automatically.

OpenAI function calling with streaming over the Responses API (config.OPENAI_API_MODE
"responses": reasoning state chained across tool rounds with previous_response_id, built-in
web search) or the older chat.completions loop ("chat", kept as a reversible fallback).
Read operations execute immediately; write operations return pending actions for the
approval flow in bot.py.

Conversation state is structured (Phase 2): every turn's tool calls and full
tool results are persisted in chat_turns as provider-neutral messages, then
converted to the OpenAI wire format per request. Sessions auto-reset after
idle time; /new forces a fresh one.

Memory 2.0 (see memory.py): the system prompt carries the memories most relevant
to the current message plus the newest, and the 3 most recent session summaries;
after each turn durable facts are extracted in the background; turns that fall out
of the history budget (and sessions being closed) are summarized; tool results
older than the two most recent turns are compacted before they are sent.
"""

import asyncio
import base64
import io
import json
import logging
import re
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

import openai

import config
import db
import gmail_module
import memory
from agent_tools import execute_tool, get_openai_tools, get_responses_tools

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10

RAN_OUT_TEXT = "I ran out of tool steps before finishing — say \"continue\" and I'll pick up where I left off."

# Tools that change state or produce approval actions: always run sequentially, in call order.
WRITE_TOOLS = frozenset({
    "draft_email", "reply_email", "send_imessage", "remember", "set_reminder", "cancel_reminder",
})

_openai: openai.AsyncOpenAI | None = None


# ── Usage / cost accounting ───────────────────────────────────────

def usage_dict(usage) -> dict:
    """Normalize an OpenAI usage object (chat, embeddings, transcription) or dict to ints."""
    if usage is None:
        return {"prompt": 0, "cached": 0, "completion": 0}
    if isinstance(usage, dict):
        d = usage
    elif hasattr(usage, "model_dump"):
        d = usage.model_dump()
    else:
        d = dict(vars(usage))
    prompt = d.get("prompt_tokens") or d.get("input_tokens") or 0
    completion = d.get("completion_tokens") or d.get("output_tokens") or 0
    details = d.get("prompt_tokens_details") or d.get("input_tokens_details") or {}
    cached = details.get("cached_tokens") or 0 if isinstance(details, dict) else 0
    if not prompt and not completion and d.get("total_tokens"):
        prompt = d["total_tokens"]  # embeddings report only a total
    out = {"prompt": int(prompt or 0), "cached": int(cached or 0), "completion": int(completion or 0)}
    if d.get("images"):
        # Image generation is priced per image: the count rides in completion_tokens (see config).
        out["images"] = int(d["images"])
        out["completion"] = out["images"]
    return out


def usage_cost(model: str, u: dict) -> tuple[float, bool]:
    """(USD, priced?) for a normalized usage dict under config.PRICES_PER_MTOK."""
    price = config.PRICES_PER_MTOK.get(model)
    if price is None:
        return 0.0, False
    if "per_image" in price:
        return round(u.get("images", u["completion"]) * price["per_image"], 8), True
    uncached = max(u["prompt"] - u["cached"], 0)
    cost = (uncached * price.get("in", 0.0) + u["cached"] * price.get("cached", 0.0)
            + u["completion"] * price.get("out", 0.0)) / 1_000_000
    return round(cost, 8), True


async def record_usage(purpose: str, model: str, usage) -> dict:
    """Persist one API call's usage. Never raises; returns the normalized dict + cost."""
    u = usage_dict(usage)
    cost, priced = usage_cost(model, u)
    u["cost"] = cost
    try:
        await db.record_llm_usage(purpose, model, u["prompt"], u["cached"], u["completion"], cost, priced)
    except Exception as e:
        logger.debug(f"record_usage failed: {e}")
    return u


def _client() -> openai.AsyncOpenAI:
    global _openai
    if _openai is None:
        # max_retries=0: with_retry below owns retries (and logs them), not the SDK.
        _openai = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY,
                                     timeout=config.OPENAI_TIMEOUT_SECONDS, max_retries=0)
    return _openai


# ── Retries ───────────────────────────────────────────────────────

RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 0.5
_TRANSIENT = (openai.APIConnectionError, openai.APITimeoutError)


def is_retryable(err: BaseException) -> bool:
    """429 and 5xx status errors, plus connection/timeout errors. Other 4xx are not."""
    if isinstance(err, openai.APIStatusError):
        return err.status_code == 429 or err.status_code >= 500
    return isinstance(err, _TRANSIENT)


def retry_after_seconds(err: BaseException) -> float | None:
    resp = getattr(err, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


async def with_retry(factory, purpose: str = "openai", attempts: int = RETRY_ATTEMPTS,
                     base_delay: float = RETRY_BASE_DELAY):
    """
    Await `factory()` with exponential backoff (0.5s, 1s, 2s, …) on rate limits,
    5xx and connection/timeout errors, honouring Retry-After. Non-retryable errors
    and the final failure propagate unchanged.
    """
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as e:
            if not is_retryable(e) or attempt == attempts:
                raise
            wait = retry_after_seconds(e)
            wait = delay if wait is None else min(max(wait, 0.0), 30.0)
            logger.warning(f"{purpose}: {type(e).__name__} (attempt {attempt}/{attempts}), "
                           f"retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
            delay *= 2


# ── Background work (memory extraction, summaries) ────────────────

_BACKGROUND: set[asyncio.Task] = set()


async def _background(coro) -> None:
    """Fire-and-forget on the running loop; the task set keeps a reference until it finishes.
    Tests replace this with an awaiting version to make the side effects synchronous."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


# ── Sessions ──────────────────────────────────────────────────────

async def get_current_session(force_new: bool = False) -> str:
    """Return the active session id, starting a fresh one on /new or after idle timeout.
    A session that is replaced gets summarized in the background (memory.close_session)."""
    session_id = await db.get_setting("current_session_id")

    if not force_new and session_id:
        last = await db.get_last_turn_time(session_id)
        if last is not None:
            idle = datetime.now(timezone.utc) - last
            if idle > timedelta(minutes=config.SESSION_IDLE_RESET_MINUTES):
                force_new = True
        # A session with no turns yet stays current.

    if force_new or not session_id:
        previous = session_id
        base = datetime.now(timezone.utc).strftime("s%Y%m%d-%H%M%S")
        session_id = base
        n = 1
        while session_id == previous or await db.get_last_turn_time(session_id) is not None:
            session_id = f"{base}-{n}"  # resets within the same second must not share an id
            n += 1
        await db.set_setting("current_session_id", session_id)
        await db.set_setting("working_set", "[]")
        if previous and previous != session_id:
            await _background(memory.close_session(previous))

    return session_id


async def start_new_session() -> str:
    return await get_current_session(force_new=True)


# ── Working set (recently referenced items) ───────────────────────

async def _update_working_set(entities: list[dict]) -> None:
    if not entities:
        return
    try:
        raw = await db.get_setting("working_set", "[]")
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        items = []

    seen = {(it.get("type"), it.get("id")) for it in items}
    for e in entities:
        key = (e.get("type"), e.get("id"))
        if key in seen:
            # Move refreshed items to the end (most recent)
            items = [it for it in items if (it.get("type"), it.get("id")) != key]
        items.append(e)
        seen.add(key)

    items = items[-10:]
    await db.set_setting("working_set", json.dumps(items, ensure_ascii=False))


async def _get_working_set() -> list[dict]:
    try:
        raw = await db.get_setting("working_set", "[]")
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


# ── System prompt ─────────────────────────────────────────────────

# Gmail account info requires network calls — cache for 15 minutes.
_accounts_cache: tuple[float, list[dict]] = (0.0, [])


async def _get_accounts_cached() -> list[dict]:
    global _accounts_cache
    fetched_at, accounts = _accounts_cache
    if time.monotonic() - fetched_at < 900 and accounts:
        return accounts
    try:
        accounts = await asyncio.to_thread(gmail_module.get_connected_accounts)
    except Exception as e:
        logger.warning(f"Could not list Gmail accounts for prompt: {e}")
        accounts = []
    _accounts_cache = (time.monotonic(), accounts)
    return accounts


async def build_system_prompt(user_message: str = "") -> str:
    """The dynamic system prompt. `user_message` steers which memories are injected
    (top-K by similarity + the newest); without it the newest memories are used."""
    now = datetime.now().astimezone()
    accounts = await _get_accounts_cached()
    active = gmail_module.get_active_account()
    memories = await memory.select_memories(user_message, top_k=config.MEMORY_TOP_K)
    summaries = await db.list_session_summaries(limit=memory.SUMMARIES_IN_PROMPT)
    working_set = await _get_working_set()

    parts = []

    owner = f"{config.USER_NAME}'s" if config.USER_NAME else "the user's"
    web_search_on = config.OPENAI_API_MODE != "chat" and config.WEB_SEARCH_ENABLED
    parts.append(
        f"You are Clawdbot, {owner} personal AI assistant for property management, chatting via Telegram."
    )
    parts.append(
        f"Current date/time: {now.strftime('%A, %Y-%m-%d %H:%M')} ({now.tzname()}). "
        "Resolve relative dates like 'yesterday' or 'last week' against this."
    )

    if accounts:
        acct_lines = []
        for a in accounts:
            marker = " [active]" if a["label"] == active else ""
            email = f" — {a['email']}" if a.get("email") else ""
            acct_lines.append(f"  {a['label']}{email}{marker}")
        parts.append("Connected Gmail accounts:\n" + "\n".join(acct_lines) +
                     "\nReads auto-detect the right account. Sends use the active account unless another is specified.")

    parts.append(
        "HOW TO WORK:\n"
        "1. Start with search_everything for questions about emails, documents, PDFs, or messages. "
        "It matches meaning as well as keywords, and takes optional sender/after/before/source filters "
        "and a limit (up to 15). If a search misses, retry once with different words or a higher limit. "
        "NEVER invent an email ID — you only know IDs from tool results.\n"
        "2. Use read_email / read_pdf_from_email with an exact email_id when you need full, fresh content. "
        "read_indexed_document works for indexed copies.\n"
        "3. Use list_recent_emails only when the user asks what's new in their inbox.\n"
        "4. Replying to an existing email → reply_email (threads correctly). New email → draft_email. "
        "When the user asks you to draft, write, send, or reply, ALWAYS call the tool — never paste a "
        "draft into chat, because only the tool produces the approval card the user sends from. "
        "NEVER say an email was sent; say it's ready for their approval.\n"
        "5. When the user states a lasting preference or fact (signature, tone, a contact, a property nickname), "
        "save it with remember.\n"
        "6. Property reviews → check_reviews / list_properties.\n"
        "7. When your answer centers on one specific email, include its gmail_link from the tool result "
        "as a markdown link like [open in Gmail](url) so the user can jump to it.\n"
        "8. When the user names a person without an email address, resolve it with find_contact. "
        "One clear match → use it (the approval card shows the address). Multiple plausible matches → ask which.\n"
        "9. Attachments: to attach a file to a draft/reply, pass attachments=[{source, id, filename}] "
        "using ids from search/read results (an email holding the file, or a OneDrive file id).\n"
        "10. Texting: when the user says text / message / iMessage someone, call send_imessage "
        "(it accepts a name and resolves it; if told the name is ambiguous, ask which). Email vs "
        "text follows what the user asked for; if they just say 'let Mike know', prefer the "
        "channel find_contact shows you've used with them. Texts are short and conversational, "
        "no signature. Like email, a text is only sent after the user approves the card — never "
        "say it was sent. WhatsApp is read-only: you can search it but not send.\n"
        "11. 'What did X say' / 'what's the latest from X' / 'show my texts with X' → get_conversation "
        "(live timeline for one person or group chat, with date filters). Use search_everything "
        "instead when the question is about a topic across people or sources.\n"
        "12. 'Remind me …' → set_reminder with an ABSOLUTE local time worked out from the current "
        "date/time above (e.g. 'Thursday 9am' → that Thursday's date at 09:00; a bare day means 09:00). "
        "Confirm the resolved date and time in your reply. list_reminders / cancel_reminder manage them.\n"
        "13. 'Who hasn't replied' / 'what am I waiting on' → list_waiting_on; to chase one, draft the "
        "follow-up with reply_email (email) or send_imessage (text) — it still needs approval.\n"
        "14. Urgent inbound email/texts are pushed to the user automatically. When they ask to always be "
        "alerted about a contact, property, or topic, save it with remember as 'ALERT: <terms, comma-"
        "separated>' (e.g. 'ALERT: Unit 4, Mike Plumber, no hot water') — that is the watch-list.\n"
        + (
            "15. The web: for news, prices, regulations, how-tos, businesses, or anything not in the "
            "user's own data, use the built-in web search and cite sources as markdown links. If the "
            "user wants a picture that already exists online, give its link — never download or "
            "embed it.\n"
            if web_search_on else
            "15. You have no web access in this mode; say so when asked about current events.\n"
        )
        + "16. To CREATE a new picture (an illustration, flyer concept, diagram, mockup), call "
        "generate_image. The image is sent to the user as a photo automatically — reply with one "
        "short line, don't describe it at length."
    )

    signatures = []
    for a in accounts:
        sig = await db.get_setting(f"signature:{a['label']}")
        if sig:
            signatures.append(f"  [{a['label']}]\n{sig}")
    if signatures:
        parts.append(
            "SIGNATURES — end every email body from these accounts with the matching signature, "
            "exactly as written:\n" + "\n".join(signatures)
        )

    parts.append(
        "STYLE:\n"
        "- Replies render in Telegram. Use markdown sparingly: **bold**, - bullets, `code`. "
        "No headers, no tables, no long preambles.\n"
        "- Be concise. Lead with the answer, then supporting detail.\n"
        "- Email drafts: professional and brief unless told otherwise. Apply any signature or tone "
        "preferences from MEMORY."
    )

    if memories:
        parts.append("MEMORY (long-term facts about the user, most relevant to this message first):\n"
                     + memory.format_memories(memories))

    if summaries:
        parts.append(
            "EARLIER CONVERSATIONS (summaries of recent sessions; use them to resolve follow-ups "
            "like 'that email' or 'the plumber from last time', and re-read sources before acting):\n"
            + memory.format_summaries(summaries)
        )

    if working_set:
        ws_lines = []
        for it in reversed(working_set):
            acct = f" account={it['account']}" if it.get("account") else ""
            ws_lines.append(f"  [{it.get('type')}] id={it.get('id')}{acct} — {it.get('label', '')}")
        parts.append(
            "RECENTLY REFERENCED (real ids from this conversation — usable directly in tools):\n"
            + "\n".join(ws_lines)
        )

    return "\n\n".join(parts)


# ── Progress callbacks (implemented by bot.py's status message) ───

async def _notify(progress, method: str, *args) -> None:
    """Invoke a progress callback, never letting UI errors break the agent."""
    if progress is None:
        return
    fn = getattr(progress, method, None)
    if fn is None:
        return
    try:
        await fn(*args)
    except Exception:
        logger.debug(f"progress.{method} failed", exc_info=True)


# ── Normalized message format → OpenAI wire format ────────────────

def _to_openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            text_parts = [b["text"] for b in m.get("content", []) if b.get("type") == "text" and b.get("text")]
            tool_calls = [
                {
                    "id": b["id"], "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                }
                for b in m.get("content", []) if b.get("type") == "tool_use"
            ]
            msg: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        elif role == "tool_results":
            for r in m.get("content", []):
                out.append({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]})
    return out


# ── Tool-result compaction ────────────────────────────────────────

COMPACT_KEEP_TURNS = 2       # the most recent user-initiated turns keep full tool results
COMPACT_MAX_CHARS = 400
COMPACT_MARKER = "\n[truncated — ask again to re-read]"


def compact_history(messages: list[dict], keep_turns: int = COMPACT_KEEP_TURNS,
                    max_chars: int = COMPACT_MAX_CHARS) -> list[dict]:
    """
    Shrink tool_results content that is older than the `keep_turns` most recent turns
    (a turn starts at each user message) to `max_chars` plus a marker, so the history
    budget holds more conversation and less stale data. Returns a new list: no message is
    removed, no tool_use_id changes, and the stored turns are never touched.
    """
    user_idx = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_idx) <= keep_turns:
        return list(messages)
    cutoff = user_idx[-keep_turns]
    out = []
    for i, m in enumerate(messages):
        if i < cutoff and m.get("role") == "tool_results":
            content = []
            for r in m.get("content", []):
                text = r.get("content") or ""
                if isinstance(text, str) and len(text) > max_chars:
                    r = dict(r, content=text[:max_chars] + COMPACT_MARKER)
                content.append(r)
            m = dict(m, content=content)
        out.append(m)
    return out


def _to_responses_input(messages: list[dict]) -> list[dict]:
    """Provider-neutral turns → Responses API `input` items. Tool-call ids are kept as the
    function_call / function_call_output call_id so stored history stays valid."""
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            for b in m.get("content", []):
                if b.get("type") == "text" and b.get("text"):
                    out.append({"role": "assistant", "content": b["text"]})
                elif b.get("type") == "tool_use":
                    out.append({
                        "type": "function_call", "call_id": b["id"], "name": b["name"],
                        "arguments": json.dumps(b.get("input", {})),
                    })
        elif role == "tool_results":
            for r in m.get("content", []):
                content = r["content"]
                out.append({"type": "function_call_output", "call_id": r["tool_use_id"],
                            "output": content if isinstance(content, str) else json.dumps(content)})
    return out


# ── Web-search citations ──────────────────────────────────────────

def _annotation_dict(a) -> dict | None:
    """Normalize a url_citation annotation (object or dict) to a plain dict, else None."""
    if a is None:
        return None
    if isinstance(a, dict):
        d = a
    elif hasattr(a, "model_dump"):
        d = a.model_dump()
    else:
        d = dict(vars(a))
    if d.get("type") != "url_citation" or not d.get("url"):
        return None
    return {"type": "url_citation", "url": d["url"], "title": d.get("title") or "",
            "start_index": d.get("start_index"), "end_index": d.get("end_index")}


def _citation_label(url: str, title: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host or (title or "source")[:40]


def render_citations(text: str, annotations: list[dict]) -> str:
    """Turn url_citation annotations into markdown links in the answer text, so
    telegram_format renders them as <a>. Each source appears once: inline right after the cited
    span when the indices are usable, otherwise in a trailing "Sources:" line. URLs the model
    already wrote into the text are left alone."""
    if not text or not annotations:
        return text
    seen: set[str] = set()
    unique: list[dict] = []
    for a in annotations:
        a = _annotation_dict(a)
        if a and a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)
    inline, footer = [], []
    for a in unique:
        if a["url"] in text:
            continue
        end = a.get("end_index")
        if isinstance(end, int) and 0 < end <= len(text):
            inline.append((end, a))
        else:
            footer.append(a)
    for end, a in sorted(inline, key=lambda x: x[0], reverse=True):
        while end < len(text) and not text[end].isspace():  # finish the word / sentence
            end += 1
        link = f" ([{_citation_label(a['url'], a['title'])}]({a['url']}))"
        text = text[:end] + link + text[end:]
    if footer:
        text += "\n\nSources: " + " · ".join(
            f"[{_citation_label(a['url'], a['title'])}]({a['url']})" for a in footer)
    return text


# ── Agent entry point ─────────────────────────────────────────────

async def agent_chat(user_message: str, progress=None) -> dict:
    """
    Run the agent loop with streaming. Returns:
      {"text": "...", "actions": [...], "files": [...]}
    where actions is a list of write-actions needing user approval (may be empty) and files
    are generated attachments to send after the text ({kind, bytes, filename, caption}).
    """
    session_id = await get_current_session()
    history, dropped = await db.get_turn_messages_with_dropped(session_id, config.HISTORY_TOKEN_BUDGET)
    if dropped:
        await _background(memory.summarize_turns(session_id, dropped))
    history = compact_history(history)
    system = await build_system_prompt(user_message)

    user_turn = [{"role": "user", "content": user_message}]
    messages = history + user_turn

    result, turn_messages, entities, totals = await _openai_agent_loop(system, messages, progress)

    await db.save_turn(session_id, "user", user_turn)
    if turn_messages:
        await db.save_turn(session_id, "assistant", turn_messages, llm_used=config.OPENAI_MODEL,
                           prompt_tokens=totals["prompt"], completion_tokens=totals["completion"],
                           cost_usd=totals["cost"])
    await _update_working_set(entities)
    if config.MEMORY_AUTO_EXTRACT:
        await _background(memory.extract_and_store(user_message, result.get("text", "")))

    return result


async def _run_one_tool(tu: dict, progress) -> dict:
    await _notify(progress, "on_tool", tu["name"], tu["input"])
    logger.info(f"Agent calling tool: {tu['name']}({json.dumps(tu['input'])[:200]})")
    try:
        return await execute_tool(tu["name"], tu["input"])
    except Exception as e:  # execute_tool catches its own errors; this is the last resort
        logger.exception(f"Tool crashed: {tu['name']}")
        return {"result": f"Tool failed: {tu['name']}: {e}"}


async def _run_tools(tool_uses: list[dict], progress, files: list[dict] | None = None
                     ) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Execute one round's tool calls. Read-only tools run concurrently (at most
    config.MAX_PARALLEL_TOOLS at once); WRITE_TOOLS run afterwards, one at a time, in
    call order. Results are returned in the ORIGINAL call order so tool_use /
    tool_results pairing never changes. tool_uses: [{"id", "name", "input"}].
    Returns (results [{"tool_use_id","content"}], pending_actions, entities); any `files`
    a tool produced (generated images) are appended to the optional `files` sink.
    """
    outcomes: list[dict | None] = [None] * len(tool_uses)
    read_idx = [i for i, tu in enumerate(tool_uses) if tu["name"] not in WRITE_TOOLS]
    write_idx = [i for i, tu in enumerate(tool_uses) if tu["name"] in WRITE_TOOLS]

    if read_idx:
        sem = asyncio.Semaphore(max(1, config.MAX_PARALLEL_TOOLS))

        async def _guarded(i: int) -> dict:
            async with sem:
                return await _run_one_tool(tool_uses[i], progress)

        gathered = await asyncio.gather(*(_guarded(i) for i in read_idx), return_exceptions=True)
        for i, res in zip(read_idx, gathered):
            if isinstance(res, BaseException):
                res = {"result": f"Tool failed: {tool_uses[i]['name']}: {res}"}
            outcomes[i] = res

    for i in write_idx:
        outcomes[i] = await _run_one_tool(tool_uses[i], progress)

    results, actions, entities = [], [], []
    for tu, outcome in zip(tool_uses, outcomes):
        outcome = outcome or {"result": "Tool produced no result."}
        if outcome.get("action"):
            actions.append(outcome["action"])
        if outcome.get("entities"):
            entities.extend(outcome["entities"])
        if files is not None and outcome.get("files"):
            files.extend(outcome["files"])
        results.append({"tool_use_id": tu["id"], "content": outcome.get("result", "")})
    return results, actions, entities


# ── OpenAI Agent Loop (streaming) ─────────────────────────────────

async def _openai_agent_loop(system: str, messages: list[dict], progress
                             ) -> tuple[dict, list[dict], list[dict], dict]:
    """Dispatch on config.OPENAI_API_MODE: "chat" → the chat.completions loop, anything else →
    the Responses API loop. Both return (result, turn_norm, entities, totals)."""
    if config.OPENAI_API_MODE == "chat":
        return await _chat_agent_loop(system, messages, progress)
    return await _responses_agent_loop(system, messages, progress)


def _parse_tool_args(raw: str) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _responses_kwargs(system: str, input_items: list[dict], tools: list[dict],
                      previous_response_id: str | None) -> dict:
    kwargs = dict(
        model=config.OPENAI_MODEL,
        instructions=system,
        input=input_items,
        tools=tools,
        max_output_tokens=config.LLM_MAX_TOKENS,
        store=True,  # required for previous_response_id chaining
        stream=True,
    )
    if config.OPENAI_REASONING_EFFORT:
        kwargs["reasoning"] = {"effort": config.OPENAI_REASONING_EFFORT}
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    return kwargs


async def _responses_agent_loop(system: str, messages: list[dict], progress
                                ) -> tuple[dict, list[dict], list[dict], dict]:
    """
    Responses API loop. Round 1 sends the whole provider-neutral history as `input`; later
    rounds send only the function_call_output items with previous_response_id, so the
    model's reasoning state carries across tool rounds. Text deltas stream to progress.on_text;
    function calls are read from the completed response; usage comes from its `usage`.
    """
    client = _client()
    tools = get_responses_tools()
    if config.WEB_SEARCH_ENABLED:
        tools.append({"type": "web_search"})

    turn_norm: list[dict] = []
    pending_actions: list[dict] = []
    all_entities: list[dict] = []
    files: list[dict] = []
    final_text = RAN_OUT_TEXT
    totals = {"prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}

    input_items = _to_responses_input(messages)
    previous_id: str | None = None

    for _ in range(MAX_TOOL_ROUNDS):
        streamed = ""
        annotations: list[dict] = []
        completed = None
        kwargs = _responses_kwargs(system, input_items, tools, previous_id)
        stream = await with_retry(lambda: client.responses.create(**kwargs), "agent")
        async for event in stream:
            et = getattr(event, "type", "")
            if et == "response.output_text.delta":
                streamed += event.delta or ""
                await _notify(progress, "on_text", streamed)
            elif et == "response.output_text.annotation.added":
                a = _annotation_dict(getattr(event, "annotation", None))
                if a:
                    annotations.append(a)
            elif et == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", "") == "web_search_call":
                    action = getattr(item, "action", None)
                    query = getattr(action, "query", None) if action is not None else None
                    await _notify(progress, "on_tool", "web_search", {"query": query or ""})
            elif et in ("response.completed", "response.incomplete"):
                completed = event.response
                if et == "response.incomplete":
                    reason = getattr(getattr(completed, "incomplete_details", None), "reason", "?")
                    logger.warning(f"Responses stream incomplete: {reason}")
            elif et == "response.failed":
                err = getattr(getattr(event, "response", None), "error", None)
                raise RuntimeError(f"Responses API failure: {getattr(err, 'message', err) or 'unknown'}")
            elif et == "error":
                raise RuntimeError(f"Responses API error: {getattr(event, 'message', event)}")

        tool_uses: list[dict] = []
        if completed is not None:
            previous_id = getattr(completed, "id", None) or previous_id
            usage = getattr(completed, "usage", None)
            if usage:
                u = await record_usage("agent", config.OPENAI_MODEL, usage)
                for k in ("prompt", "cached", "completion", "cost"):
                    totals[k] += u[k]
            for item in getattr(completed, "output", None) or []:
                itype = getattr(item, "type", "")
                if itype == "function_call":
                    tool_uses.append({"id": item.call_id, "name": item.name,
                                      "input": _parse_tool_args(getattr(item, "arguments", "") or "")})
                elif itype == "message" and not streamed:
                    for part in getattr(item, "content", None) or []:
                        if getattr(part, "type", "") == "output_text":
                            streamed += part.text or ""
                            if not annotations:
                                annotations.extend(
                                    a for a in map(_annotation_dict, getattr(part, "annotations", None) or []) if a)

        norm_content = (
            [{"type": "text", "text": streamed}] if streamed else []
        ) + [{"type": "tool_use", **tu} for tu in tool_uses]
        if norm_content:
            turn_norm.append({"role": "assistant", "content": norm_content})

        if not tool_uses:
            final_text = render_citations(streamed.strip(), annotations)
            break

        results, actions, entities = await _run_tools(tool_uses, progress, files=files)
        pending_actions.extend(actions)
        all_entities.extend(entities)
        turn_norm.append({"role": "tool_results", "content": results})
        input_items = [{"type": "function_call_output", "call_id": r["tool_use_id"], "output": r["content"]}
                       for r in results]
        if previous_id is None:  # no completed event: resend everything rather than chain
            input_items = _to_responses_input(messages + turn_norm)
    else:
        turn_norm.append({"role": "assistant", "content": [{"type": "text", "text": final_text}]})

    return ({"text": final_text, "actions": pending_actions, "files": files},
            turn_norm, all_entities, totals)


async def _chat_agent_loop(system: str, messages: list[dict], progress
                           ) -> tuple[dict, list[dict], list[dict], dict]:
    """The pre-Responses chat.completions loop (OPENAI_API_MODE=chat). No web search."""
    client = _client()
    wire_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)
    oai_tools = get_openai_tools()

    turn_norm: list[dict] = []
    pending_actions: list[dict] = []
    all_entities: list[dict] = []
    files: list[dict] = []
    final_text = RAN_OUT_TEXT
    totals = {"prompt": 0, "cached": 0, "completion": 0, "cost": 0.0}

    for _ in range(MAX_TOOL_ROUNDS):
        streamed = ""
        calls_acc: dict[int, dict] = {}
        stream = await with_retry(lambda: client.chat.completions.create(
            model=config.OPENAI_MODEL,
            max_completion_tokens=config.LLM_MAX_TOKENS,
            messages=wire_messages,
            tools=oai_tools,
            stream=True,
            stream_options={"include_usage": True},
        ), "agent")
        async for chunk in stream:
            if getattr(chunk, "usage", None):  # final chunk carries the round's usage
                u = await record_usage("agent", config.OPENAI_MODEL, chunk.usage)
                for k in ("prompt", "cached", "completion", "cost"):
                    totals[k] += u[k]
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                streamed += delta.content
                await _notify(progress, "on_text", streamed)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    acc = calls_acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function and tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        acc["args"] += tc.function.arguments

        tool_uses = []
        for idx in sorted(calls_acc):
            acc = calls_acc[idx]
            try:
                parsed = json.loads(acc["args"]) if acc["args"] else {}
            except json.JSONDecodeError:
                parsed = {}
            tool_uses.append({"id": acc["id"], "name": acc["name"], "input": parsed})

        norm_content = (
            [{"type": "text", "text": streamed}] if streamed else []
        ) + [{"type": "tool_use", **tu} for tu in tool_uses]
        if norm_content:
            turn_norm.append({"role": "assistant", "content": norm_content})

        if not tool_uses:
            final_text = streamed.strip()
            break

        wire_messages.append({
            "role": "assistant",
            "content": streamed or None,
            "tool_calls": [
                {"id": tu["id"], "type": "function",
                 "function": {"name": tu["name"], "arguments": json.dumps(tu["input"])}}
                for tu in tool_uses
            ],
        })

        results, actions, entities = await _run_tools(tool_uses, progress, files=files)
        pending_actions.extend(actions)
        all_entities.extend(entities)
        turn_norm.append({"role": "tool_results", "content": results})
        for r in results:
            wire_messages.append({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]})
    else:
        turn_norm.append({"role": "assistant", "content": [{"type": "text", "text": final_text}]})

    return ({"text": final_text, "actions": pending_actions, "files": files},
            turn_norm, all_entities, totals)


# ── Draft revision (Approve / Edit / Reject loop) ────────────────

# Which payload fields the Edit loop may change. reply_email recomputes
# to/subject from the original thread at send time, so only these are editable.
_EDITABLE_FIELDS = {
    "send_email": ("to", "subject", "body", "cc", "bcc"),
    "reply_email": ("body", "cc", "bcc"),
    "send_imessage": ("body",),  # recipient and service are locked once resolved
}


def schema_for(action_type: str) -> dict:
    """Strict JSON schema for a revised draft: exactly the editable fields, all strings.
    Recipient/thread fields are absent, so the model structurally cannot change them."""
    fields = _EDITABLE_FIELDS.get(action_type, ("body",))
    return {
        "name": f"revised_{action_type}",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {k: {"type": "string"} for k in fields},
            "required": list(fields),
        },
    }


async def revise_draft(action_type: str, payload: dict, instructions: str) -> dict:
    """Apply freeform edit instructions to a pending draft; returns the new payload.
    Uses a strict json_schema; falls back to json_object if the API rejects the schema.
    Only _EDITABLE_FIELDS are ever copied back, whatever the model returns."""
    fields = _EDITABLE_FIELDS.get(action_type, ("body",))
    editable = {k: payload.get(k, "") for k in fields}

    kind = "text message" if action_type == "send_imessage" else "email draft"
    prompt = (
        f"You are revising a {kind}. Apply the user's instructions and return ONLY a JSON "
        f"object with exactly these keys: {', '.join(fields)}. Keep everything the instructions "
        "don't ask to change. Body is plain text.\n\n"
        f"Current draft:\n{json.dumps(editable, ensure_ascii=False)}\n\n"
        f"User's instructions: {instructions}"
    )

    def _call(response_format: dict):
        return _client().chat.completions.create(
            model=config.OPENAI_MODEL,
            max_completion_tokens=2048,
            response_format=response_format,
            messages=[{"role": "user", "content": prompt}],
        )

    try:
        response = await with_retry(
            lambda: _call({"type": "json_schema", "json_schema": schema_for(action_type)}), "revise")
    except openai.BadRequestError as e:
        logger.warning(f"revise_draft: strict schema rejected ({e}); falling back to json_object")
        response = await with_retry(lambda: _call({"type": "json_object"}), "revise")
    await record_usage("revise", config.OPENAI_MODEL, getattr(response, "usage", None))
    try:
        data = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        data = {}

    new_payload = dict(payload)
    for k in fields:
        if isinstance(data.get(k), str):
            new_payload[k] = data[k]
    return new_payload


# ── Voice, vision, digest helpers (Phase 5) ───────────────────────

async def transcribe_voice(data: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe a Telegram voice note. Falls back to whisper-1 on format errors."""
    model = config.VOICE_MODEL
    try:
        result = await with_retry(lambda: _client().audio.transcriptions.create(
            model=model, file=(filename, io.BytesIO(data))
        ), "voice")
    except openai.BadRequestError:
        model = "whisper-1"
        result = await with_retry(lambda: _client().audio.transcriptions.create(
            model=model, file=(filename, io.BytesIO(data))
        ), "voice")
    await record_usage("voice", model, getattr(result, "usage", None))
    return (result.text or "").strip()


async def describe_image(data: bytes, mime: str) -> str:
    """Extract text (OCR) and a thorough description from an image."""
    b64 = base64.b64encode(data).decode()
    response = await with_retry(lambda: _client().chat.completions.create(
        model=config.OPENAI_MODEL,
        max_completion_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": (
                    "Extract everything useful from this image for an assistant to act on: "
                    "transcribe all visible text exactly, then briefly describe what the image shows."
                )},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    ), "vision")
    await record_usage("vision", config.OPENAI_MODEL, getattr(response, "usage", None))
    return (response.choices[0].message.content or "").strip()


_IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536"}
_IMAGE_SIZE_ALIASES = {"square": "1024x1024", "landscape": "1536x1024", "wide": "1536x1024",
                       "portrait": "1024x1536", "tall": "1024x1536"}


def image_size(raw: str | None) -> str:
    """Normalize a requested size ('landscape', '1536x1024', …) to one the image API accepts."""
    key = (raw or "").strip().lower()
    key = _IMAGE_SIZE_ALIASES.get(key, key)
    return key if key in _IMAGE_SIZES else "1024x1024"


async def generate_image(prompt: str, size: str | None = None) -> bytes:
    """Generate one PNG with the image model (not outbound: the picture only goes back to the
    owner). Usage is recorded per image under purpose "image"."""
    size = image_size(size)
    response = await with_retry(lambda: _client().images.generate(
        model=config.IMAGE_MODEL, prompt=prompt, n=1, size=size, quality=config.IMAGE_QUALITY,
    ), "image")
    data = response.data[0]
    if not getattr(data, "b64_json", None):
        raise RuntimeError("image API returned no image data")
    png = base64.b64decode(data.b64_json)
    await record_usage("image", config.IMAGE_MODEL, {"images": 1})
    return png


# ── Spoken replies (TTS) ──────────────────────────────────────────

VOICE_MODES = ("auto", "on", "off")
VOICE_SETTING = "voice_reply_mode"

_SAY_IT_RE = re.compile(
    r"\b(say (it|that|this)|read (it|that|this) (out|aloud|to me)|out loud|speak (it|that|this)"
    r"|(reply|answer|respond|tell me) (by|with|in|via) voice|voice (reply|note|message) please)\b",
    re.IGNORECASE,
)


async def voice_mode() -> str:
    mode = (await db.get_setting(VOICE_SETTING) or "auto").strip().lower()
    return mode if mode in VOICE_MODES else "auto"


def wants_spoken_reply(user_text: str) -> bool:
    """Did the user ask for this particular answer out loud ("say it", "read it to me")?"""
    return bool(_SAY_IT_RE.search(user_text or ""))


def should_speak(mode: str, incoming_voice: bool, say_it: bool = False) -> bool:
    """The voice-reply decision: an explicit 'say it' always speaks; /voice on always speaks;
    /voice off never speaks otherwise; auto speaks only when the user sent a voice note."""
    if say_it:
        return True
    if mode == "on":
        return True
    if mode == "off":
        return False
    return bool(incoming_voice)


def strip_markdown(text: str) -> str:
    """Plain text for TTS: no code fences, links become their label, emphasis/headers/bullets
    dropped, bare URLs removed."""
    t = text or ""
    t = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", lambda m: m.group(1).strip("\n"), t, flags=re.DOTALL)
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\((?:https?://[^)\s]+)\)", r"\1", t)
    t = re.sub(r"<[^<>\n]{1,80}>", "", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"~~(.+?)~~", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)
    t = re.sub(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    lines = []
    for line in t.split("\n"):
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"^\s*[-*+•]\s+", "", line)
        lines.append(line.rstrip())
    t = "\n".join(lines)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def speech_text(text: str, max_chars: int | None = None) -> str:
    """Markdown-stripped answer, cut at a sentence boundary to at most max_chars."""
    limit = max_chars or config.VOICE_OUT_MAX_CHARS
    plain = strip_markdown(text)
    if len(plain) <= limit:
        return plain
    cut = plain[:limit]
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    if boundary >= limit // 2:
        cut = cut[:boundary + 1]
    else:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


async def synthesize_speech(text: str) -> bytes:
    """Speak an answer with the TTS model; returns Ogg/Opus bytes for a Telegram voice note.
    Usage is recorded under "tts" with the character count in prompt_tokens (see config)."""
    spoken = speech_text(text)
    if not spoken:
        return b""
    response = await with_retry(lambda: _client().audio.speech.create(
        model=config.VOICE_OUT_MODEL, voice=config.VOICE_OUT_VOICE, input=spoken,
        response_format="opus",
    ), "tts")
    data = await response.aread() if hasattr(response, "aread") else response.content
    await record_usage("tts", config.VOICE_OUT_MODEL, {"prompt_tokens": len(spoken)})
    return data


async def draft_followup(topic: str, to: str, days: int, channel: str = "email") -> str:
    """A short, polite nudge for something unanswered. Falls back to a stock line on error."""
    kind = "text message" if channel == "text" else "email reply"
    fallback = ("Hi — just following up on this, let me know when you get a chance. Thanks!"
                if channel == "email" else "Hey, just checking in on this when you have a sec — thanks!")
    try:
        response = await with_retry(lambda: _client().chat.completions.create(
            model=config.OPENAI_MODEL,
            max_completion_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a brief, friendly follow-up {kind} (2–3 sentences, no subject line, "
                    f"no signature) to {to}, who hasn't replied for {days} days about: "
                    f"\"{topic}\". Ask for a quick update without sounding pushy. Return only the text."
                ),
            }],
        ), "followup")
        await record_usage("followup", config.OPENAI_MODEL, getattr(response, "usage", None))
        text = (response.choices[0].message.content or "").strip()
        return text or fallback
    except Exception as e:
        logger.warning(f"draft_followup failed, using fallback: {e}")
        return fallback


async def draft_acknowledgement(topic: str, to: str, channel: str = "email") -> str:
    """Short 'got it, on it' reply to an urgent inbound item (fast model; stock line on error)."""
    kind = "text message" if channel == "text" else "email reply"
    fallback = ("Thanks for letting me know — I've seen this and I'm on it. I'll follow up shortly."
                if channel == "email" else "Got it, thanks — on it now, will update you shortly.")
    try:
        response = await with_retry(lambda: _client().chat.completions.create(
            model=config.OPENAI_FAST_MODEL,
            max_completion_tokens=160,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a brief, calm {kind} (1–2 sentences, no subject line, no signature) to {to} "
                    f"acknowledging their urgent message about: \"{topic}\". Say it's been seen and is "
                    "being handled, and that they'll hear back soon. Return only the text."
                ),
            }],
        ), "ack")
        await record_usage("ack", config.OPENAI_FAST_MODEL, getattr(response, "usage", None))
        return (response.choices[0].message.content or "").strip() or fallback
    except Exception as e:
        logger.warning(f"draft_acknowledgement failed, using fallback: {e}")
        return fallback


async def compose_digest(raw_sections: str, kind: str = "morning") -> str:
    """Turn raw digest sections into a short briefing (morning) or wrap-up (evening)."""
    if kind == "evening":
        brief = (
            "Write a short evening wrap-up for Telegram from the data below: what is still "
            "waiting on a reply, what's due tomorrow, any urgent alerts since this morning, and "
            "drafts still awaiting approval. Under 100 words."
        )
    else:
        brief = (
            "Write a short morning briefing for Telegram from the data below. If there is a "
            "calendar section, open with today's schedule in one or two lines. Then what needs "
            "action today (overdue reminders, urgent alerts, people waiting on a reply, unread "
            "that matters), then one line for the ignorable rest. Mention pending drafts and "
            "new reviews if present. Under 180 words."
        )
    response = await with_retry(lambda: _client().chat.completions.create(
        model=config.OPENAI_FAST_MODEL,
        max_completion_tokens=1200,
        messages=[{
            "role": "user",
            "content": (
                brief + " Use markdown sparingly (**bold**, - bullets). No greeting fluff beyond "
                "one short opening line. Never invent items that aren't in the data.\n\n"
                + raw_sections
            ),
        }],
    ), "digest")
    await record_usage("digest", config.OPENAI_FAST_MODEL, getattr(response, "usage", None))
    return (response.choices[0].message.content or "").strip()


# ── Legacy simple chat (kept for backward compat) ────────────────

async def chat(user_message: str, conversation_history: list[dict] | None = None) -> str:
    result = await agent_chat(user_message)
    return result["text"]

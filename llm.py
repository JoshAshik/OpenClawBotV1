"""
LLM agent — routes natural language to tool calls automatically.

OpenAI function calling with streaming. Read operations execute immediately;
write operations return pending actions for the approval flow in bot.py.

Conversation state is structured (Phase 2): every turn's tool calls and full
tool results are persisted in chat_turns as provider-neutral messages, then
converted to the OpenAI wire format per request. Sessions auto-reset after
idle time; /new forces a fresh one.
"""

import asyncio
import base64
import io
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import openai

import config
import db
import gmail_module
from agent_tools import execute_tool, get_openai_tools

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10

RAN_OUT_TEXT = "I ran out of tool steps before finishing — say \"continue\" and I'll pick up where I left off."

_openai: openai.AsyncOpenAI | None = None


def _client() -> openai.AsyncOpenAI:
    global _openai
    if _openai is None:
        _openai = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    return _openai


# ── Sessions ──────────────────────────────────────────────────────

async def get_current_session(force_new: bool = False) -> str:
    """Return the active session id, starting a fresh one on /new or after idle timeout."""
    session_id = await db.get_setting("current_session_id")

    if not force_new and session_id:
        last = await db.get_last_turn_time(session_id)
        if last is not None:
            idle = datetime.now(timezone.utc) - last
            if idle > timedelta(minutes=config.SESSION_IDLE_RESET_MINUTES):
                force_new = True
        # A session with no turns yet stays current.

    if force_new or not session_id:
        session_id = datetime.now(timezone.utc).strftime("s%Y%m%d-%H%M%S")
        await db.set_setting("current_session_id", session_id)
        await db.set_setting("working_set", "[]")

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


async def build_system_prompt() -> str:
    now = datetime.now().astimezone()
    accounts = await _get_accounts_cached()
    active = gmail_module.get_active_account()
    memories = await db.list_memories(limit=30)
    working_set = await _get_working_set()

    parts = []

    owner = f"{config.USER_NAME}'s" if config.USER_NAME else "the user's"
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
        "using ids from search/read results (an email holding the file, or a OneDrive file id)."
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
        mem_lines = [f"  #{m['id']}: {m['content']}" for m in memories]
        mem_text = "\n".join(mem_lines)
        if len(mem_text) > 2000:
            mem_text = mem_text[:2000] + "\n  …"
        parts.append("MEMORY (long-term facts you saved earlier):\n" + mem_text)

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


# ── Agent entry point ─────────────────────────────────────────────

async def agent_chat(user_message: str, progress=None) -> dict:
    """
    Run the agent loop with streaming. Returns:
      {"text": "...", "actions": [...]}
    where actions is a list of write-actions needing user approval (may be empty).
    """
    session_id = await get_current_session()
    system = await build_system_prompt()
    history = await db.get_turn_messages(session_id, config.HISTORY_TOKEN_BUDGET)

    user_turn = [{"role": "user", "content": user_message}]
    messages = history + user_turn

    result, turn_messages, entities = await _openai_agent_loop(system, messages, progress)

    await db.save_turn(session_id, "user", user_turn)
    if turn_messages:
        await db.save_turn(session_id, "assistant", turn_messages, llm_used=config.OPENAI_MODEL)
    await _update_working_set(entities)

    return result


async def _run_tools(tool_uses: list[dict], progress) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Execute tool calls sequentially. tool_uses: [{"id", "name", "input"}].
    Returns (results [{"tool_use_id","content"}], pending_actions, entities).
    """
    results, actions, entities = [], [], []
    for tu in tool_uses:
        await _notify(progress, "on_tool", tu["name"], tu["input"])
        logger.info(f"Agent calling tool: {tu['name']}({json.dumps(tu['input'])[:200]})")
        outcome = await execute_tool(tu["name"], tu["input"])
        if outcome.get("action"):
            actions.append(outcome["action"])
        if outcome.get("entities"):
            entities.extend(outcome["entities"])
        results.append({"tool_use_id": tu["id"], "content": outcome["result"]})
    return results, actions, entities


# ── OpenAI Agent Loop (streaming) ─────────────────────────────────

async def _openai_agent_loop(system: str, messages: list[dict], progress) -> tuple[dict, list[dict], list[dict]]:
    client = _client()
    wire_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)
    oai_tools = get_openai_tools()

    turn_norm: list[dict] = []
    pending_actions: list[dict] = []
    all_entities: list[dict] = []
    final_text = RAN_OUT_TEXT

    for _ in range(MAX_TOOL_ROUNDS):
        streamed = ""
        calls_acc: dict[int, dict] = {}
        stream = await client.chat.completions.create(
            model=config.OPENAI_MODEL,
            max_completion_tokens=config.LLM_MAX_TOKENS,
            messages=wire_messages,
            tools=oai_tools,
            stream=True,
        )
        async for chunk in stream:
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

        results, actions, entities = await _run_tools(tool_uses, progress)
        pending_actions.extend(actions)
        all_entities.extend(entities)
        turn_norm.append({"role": "tool_results", "content": results})
        for r in results:
            wire_messages.append({"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]})
    else:
        turn_norm.append({"role": "assistant", "content": [{"type": "text", "text": final_text}]})

    return {"text": final_text, "actions": pending_actions}, turn_norm, all_entities


# ── Draft revision (Approve / Edit / Reject loop) ────────────────

# Which payload fields the Edit loop may change. reply_email recomputes
# to/subject from the original thread at send time, so only these are editable.
_EDITABLE_FIELDS = {
    "send_email": ("to", "subject", "body", "cc", "bcc"),
    "reply_email": ("body", "cc", "bcc"),
}


async def revise_draft(action_type: str, payload: dict, instructions: str) -> dict:
    """Apply freeform edit instructions to a pending draft; returns the new payload."""
    fields = _EDITABLE_FIELDS.get(action_type, ("body",))
    editable = {k: payload.get(k, "") for k in fields}

    prompt = (
        "You are revising an email draft. Apply the user's instructions and return ONLY a JSON "
        f"object with exactly these keys: {', '.join(fields)}. Keep everything the instructions "
        "don't ask to change. Body is plain text.\n\n"
        f"Current draft:\n{json.dumps(editable, ensure_ascii=False)}\n\n"
        f"User's instructions: {instructions}"
    )
    response = await _client().chat.completions.create(
        model=config.OPENAI_MODEL,
        max_completion_tokens=2048,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(response.choices[0].message.content or "{}")

    new_payload = dict(payload)
    for k in fields:
        if isinstance(data.get(k), str):
            new_payload[k] = data[k]
    return new_payload


# ── Voice, vision, digest helpers (Phase 5) ───────────────────────

async def transcribe_voice(data: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe a Telegram voice note. Falls back to whisper-1 on format errors."""
    try:
        result = await _client().audio.transcriptions.create(
            model=config.VOICE_MODEL, file=(filename, io.BytesIO(data))
        )
    except openai.BadRequestError:
        result = await _client().audio.transcriptions.create(
            model="whisper-1", file=(filename, io.BytesIO(data))
        )
    return (result.text or "").strip()


async def describe_image(data: bytes, mime: str) -> str:
    """Extract text (OCR) and a thorough description from an image."""
    b64 = base64.b64encode(data).decode()
    response = await _client().chat.completions.create(
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
    )
    return (response.choices[0].message.content or "").strip()


async def compose_digest(raw_sections: str) -> str:
    """Turn raw digest inputs into a short, friendly morning briefing."""
    response = await _client().chat.completions.create(
        model=config.OPENAI_MODEL,
        max_completion_tokens=1200,
        messages=[{
            "role": "user",
            "content": (
                "Write a short morning briefing for Telegram from the data below. "
                "Lead with anything that needs action today. Group unread email into "
                "'needs attention' vs one line for the ignorable rest. Mention pending drafts "
                "and new reviews if present. Use markdown sparingly (**bold**, - bullets). "
                "Under 150 words. No greeting fluff beyond one short opening line.\n\n"
                + raw_sections
            ),
        }],
    )
    return (response.choices[0].message.content or "").strip()


# ── Legacy simple chat (kept for backward compat) ────────────────

async def chat(user_message: str, conversation_history: list[dict] | None = None) -> str:
    result = await agent_chat(user_message)
    return result["text"]

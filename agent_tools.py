"""
Agent tools — functions the AI can call autonomously based on natural language.

Each tool has:
  - A schema (name, description, parameters) for the LLM
  - An async execute() function that runs the actual logic
  - A category: "read" (auto-execute) or "write" (needs approval)

The LLM sees the schemas and decides which tools to call.
"""

import json
import logging

import db
import gmail_module
import search_index

logger = logging.getLogger(__name__)


# ── Tool Schemas (sent to the LLM) ───────────────────────────────

_ATTACHMENTS_SCHEMA = {
    "type": "array",
    "description": (
        "Files to attach, pulled from another email or OneDrive. "
        "Each item: source ('email' or 'onedrive'), id (the email_id or OneDrive file id), "
        "filename (exact attachment/file name)."
    ),
    "items": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["email", "onedrive"]},
            "id": {"type": "string"},
            "filename": {"type": "string"},
        },
        "required": ["source", "id", "filename"],
    },
}


def action_detail(action_type: str, payload: dict) -> str:
    """Render the approval-card body for an action. Single source of truth —
    used for the initial card and for re-renders after the Edit loop."""
    if action_type in ("send_email", "reply_email"):
        acct = f" (from: {payload['account']})" if payload.get("account") else ""
        lines = [f"*To:* {payload.get('to', '?')}{acct}"]
        if payload.get("cc"):
            lines.append(f"*Cc:* {payload['cc']}")
        if payload.get("bcc"):
            lines.append(f"*Bcc:* {payload['bcc']}")
        lines.append(f"*Subject:* {payload.get('subject', '')}")
        if action_type == "reply_email":
            lines.append("_(threaded reply)_")
        atts = payload.get("attachments") or []
        if atts:
            lines.append("*Attach:* " + ", ".join(a.get("filename", "?") for a in atts))
        if payload.get("html"):
            lines.append("_(HTML formatting)_")
        return "\n".join(lines) + f"\n\n{payload.get('body', '')}"
    if action_type == "send_imessage":
        return (f"*To:* {message_recipient_label(payload)} via {payload.get('service', 'iMessage')}"
                f"\n\n{payload.get('body', '')}")
    return json.dumps(payload, indent=2)[:1500]


def message_recipient_label(payload: dict) -> str:
    """'Mike Plumber (+14155550101)' or just the handle when the name is unknown."""
    to, name = payload.get("to", "?"), payload.get("to_name", "")
    return f"{name} ({to})" if name and name != to else to


TOOL_SCHEMAS = [
    {
        "name": "search_everything",
        "description": (
            "Search across ALL sources — emails (including PDF attachment text), OneDrive files, "
            "iMessage and WhatsApp conversations. Hybrid keyword + semantic search, so natural "
            "descriptions work too ('the plumber invoice'). Use this as the FIRST step for any "
            "question about a document, email, file, or conversation. "
            "If the first search misses, retry with different words or a higher limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look for — keywords or a natural description",
                },
                "source": {
                    "type": "string",
                    "enum": ["gmail", "onedrive", "imessage", "whatsapp"],
                    "description": "Restrict to one source (omit to search everything)",
                },
                "sender": {
                    "type": "string",
                    "description": "Only items from this sender/contact (name or email substring)",
                },
                "after": {
                    "type": "string",
                    "description": "Only items dated on/after this date, YYYY-MM-DD",
                },
                "before": {
                    "type": "string",
                    "description": "Only items dated on/before this date, YYYY-MM-DD",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5, up to 15 for broader coverage)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_recent_emails",
        "description": "List recent emails from Gmail inbox. Use when the user asks about recent emails, inbox, or what's new.",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of emails to return (default 10, max 25)"},
                "account": {"type": "string", "description": "Gmail account label (e.g. 'personal', 'work'). Omit for active account."},
            },
        },
    },
    {
        "name": "search_emails",
        "description": "Search Gmail with a specific query. Supports Gmail syntax like from:, subject:, has:attachment, is:unread, newer_than:.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "account": {"type": "string", "description": "Gmail account label. Omit for active."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_pdfs",
        "description": "Search Gmail for emails with PDF attachments and extract their text. Great for finding leases, invoices, contracts, reports.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in PDF emails"},
                "account": {"type": "string", "description": "Gmail account label. Omit for active."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_email",
        "description": (
            "Read the FULL current content of a specific email directly from Gmail (fresher than the index). "
            "Use the exact email_id from search or list results. Includes attachment names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The Gmail message ID (from search/list results)"},
                "account": {"type": "string", "description": "Gmail account label. Omit to auto-detect."},
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "read_pdf_from_email",
        "description": (
            "Read an email AND extract the text of its PDF attachments. "
            "Use when the answer is likely inside an attached PDF (lease, invoice, contract, report)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "The Gmail message ID"},
                "account": {"type": "string", "description": "Gmail account label. Omit to auto-detect."},
            },
            "required": ["email_id"],
        },
    },
    {
        "name": "draft_email",
        "description": (
            "Draft a NEW email. The draft is shown to the user for approval before sending — "
            "NEVER claim it was sent; say it's ready for approval. "
            "To reply to an existing email, use reply_email instead so the reply stays in-thread."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body text"},
                "cc": {"type": "string", "description": "Optional CC addresses (comma-separated)"},
                "bcc": {"type": "string", "description": "Optional BCC addresses (comma-separated)"},
                "html": {"type": "boolean", "description": "Send as formatted HTML (body must then be HTML)"},
                "attachments": _ATTACHMENTS_SCHEMA,
                "account": {"type": "string", "description": "Gmail account label to send from. Omit for active account."},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "reply_email",
        "description": (
            "Reply to an existing email, keeping the reply in the same thread. "
            "Use the email_id of the message being replied to. The reply is shown to the user "
            "for approval before sending — NEVER claim it was sent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "Gmail message ID of the email being replied to"},
                "body": {"type": "string", "description": "The reply text (no quoted original needed — just the reply)"},
                "cc": {"type": "string", "description": "Optional CC addresses (comma-separated)"},
                "bcc": {"type": "string", "description": "Optional BCC addresses (comma-separated)"},
                "attachments": _ATTACHMENTS_SCHEMA,
                "account": {"type": "string", "description": "Gmail account label. Omit to auto-detect from the email."},
            },
            "required": ["email_id", "body"],
        },
    },
    {
        "name": "send_imessage",
        "description": (
            "Send a text message (iMessage, or SMS) from the user's Mac. Use when the user asks "
            "to text, message, or iMessage someone. `to` may be a phone number, an iMessage "
            "email handle, or a person's name (resolved from Contacts and past messages; if the "
            "name is ambiguous you'll be told to ask which). The message is shown to the user "
            "for approval before anything is sent — NEVER say it was sent; say it's ready for "
            "their approval. WhatsApp sending is not available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Phone number, iMessage email handle, or the person's name"},
                "body": {"type": "string", "description": "The message text (plain, conversational — no signature)"},
                "service": {"type": "string", "enum": ["iMessage", "SMS"], "description": "Defaults to iMessage"},
            },
            "required": ["to", "body"],
        },
    },
    {
        "name": "get_conversation",
        "description": (
            "Pull up the live message timeline with one person or one group chat (iMessage/SMS, "
            "or WhatsApp) — 'what did Mike say', 'latest from the tenant', 'my texts with Sara "
            "last week'. Complete and in order, straight from the Messages database, with dates. "
            "For topic searches across people or sources use search_everything instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "Person's name, phone number, email handle, or a group chat's name"},
                "after": {"type": "string", "description": "Only messages on/after this date, YYYY-MM-DD"},
                "before": {"type": "string", "description": "Only messages on/before this date, YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Newest N messages (default 40, max 200)"},
                "source": {"type": "string", "enum": ["imessage", "whatsapp"], "description": "Default imessage"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "find_contact",
        "description": (
            "Look up a person's email address and phone number(s) from Contacts and past "
            "correspondence (email, iMessage, WhatsApp). Use whenever the user names a person "
            "without giving an address or number ('email John about the rent', 'text Mike'). "
            "Returns matches ranked by how often you've interacted, with the channels each "
            "person has been reached on, so you can pick email vs. message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The person's name, or a partial email address or phone number"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Save a lasting fact to long-term memory: user preferences (signature, tone), "
            "frequent contacts, property nicknames, standing instructions. "
            "Use when the user states something worth remembering across conversations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The fact to remember, phrased so it's useful later"},
                "category": {"type": "string", "description": "Optional: 'preference', 'contact', 'property', or 'fact'"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "list_waiting_on",
        "description": (
            "What the user is still waiting on: emails and texts they sent that got no reply "
            "for a few days ('who hasn't replied?', 'what am I waiting on?'). Returns ids so "
            "you can draft a nudge with reply_email or send_imessage if asked."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_reminder",
        "description": (
            "Set a reminder the bot will send to the user at a specific time ('remind me Thursday "
            "to chase the deposit'). Compute `when` as an ABSOLUTE local date/time from the current "
            "date/time in your instructions — never pass relative words. The reminder is delivered "
            "as a Telegram message with Snooze / Done buttons; it is not sent to anyone else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "ISO local date/time, e.g. 2026-10-17T09:00 (a date alone means 09:00)"},
                "text": {"type": "string", "description": "What to remind about, phrased as a short imperative"},
            },
            "required": ["when", "text"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List pending reminders with their ids and times.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by id (from list_reminders or a set_reminder result).",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Reminder id"}},
            "required": ["id"],
        },
    },
    {
        "name": "check_reviews",
        "description": "Check all monitored properties for new Google Reviews. Use when user asks about reviews, ratings, or tenant feedback.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_properties",
        "description": "List all properties currently being monitored for reviews.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "search_onedrive",
        "description": "Search OneDrive files by name or content. Use when user asks about files, documents, or specific file names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "File search query"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_onedrive_file",
        "description": "Download and read the text content of a OneDrive file (PDF or text). Use after searching OneDrive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "OneDrive file ID"},
                "filename": {"type": "string", "description": "The filename (needed to determine file type)"},
            },
            "required": ["file_id", "filename"],
        },
    },
    {
        "name": "read_indexed_document",
        "description": (
            "Read the FULL text of an already-indexed document (email, PDF, message, file). "
            "Use this when search_everything returned a result and you need the complete content — "
            "it's faster and more reliable than going back to Gmail/OneDrive. "
            "Pass the source and source_id exactly as shown in search results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "The source type: 'gmail', 'onedrive', 'imessage', or 'whatsapp'"},
                "source_id": {"type": "string", "description": "The source ID from search results (e.g. 'personal:abc123')"},
            },
            "required": ["source", "source_id"],
        },
    },
    {
        "name": "get_index_stats",
        "description": "Get the number of indexed items per source (Gmail, OneDrive, iMessage, WhatsApp).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Helpers ────────────────────────────────────────────────────────

def _parse_filter_date(raw: str | None, end_of_day: bool = False):
    """Parse a YYYY-MM-DD filter into an aware datetime (None if absent/bad)."""
    from datetime import datetime, timedelta, timezone

    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end_of_day and len(raw.strip()) == 10:
        dt += timedelta(days=1)  # date-only "before" is inclusive
    return dt


def _gmail_link(source_id: str) -> str:
    """Deep link into Gmail. source_id may be 'account:msgid' or a bare msgid."""
    bare = source_id.split(":", 1)[-1]
    return f"https://mail.google.com/mail/#all/{bare}"


def _clean_account(args: dict) -> str | None:
    """Extract account from args, treating empty/whitespace strings as None."""
    account = args.get("account")
    if isinstance(account, str):
        account = account.strip() or None
    return account

def _resolve_email_id(args: dict) -> tuple[str, str | None]:
    """
    Extract the real Gmail message ID and account from tool args.
    Handles three cases:
      1. email_id="personal:abc123"         → ("abc123", "personal")
      2. email_id="abc123", account="work"  → ("abc123", "work")
      3. email_id="abc123"                  → ("abc123", None)  (uses active)
    """
    raw_id = args.get("email_id", "").strip()
    account = args.get("account")
    if isinstance(account, str):
        account = account.strip() or None

    # Only split on ":" if the prefix is a known account label
    known_labels = {a["label"] for a in gmail_module.get_connected_accounts()}
    if ":" in raw_id:
        prefix, rest = raw_id.split(":", 1)
        if prefix in known_labels:
            account = account or prefix
            raw_id = rest

    return raw_id, account


def _gmail_with_fallback(fn, email_id: str, account: str | None, **kwargs):
    """
    Try fn(email_id, account=account) — on 404 or auth error, retry with
    every other connected Gmail account before giving up.
    Returns (result, account_label_that_worked).
    """
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    accounts_to_try = [account] if account else [gmail_module.get_active_account()]

    all_accounts = [a["label"] for a in gmail_module.get_connected_accounts()]
    for a in all_accounts:
        if a not in accounts_to_try:
            accounts_to_try.append(a)

    errors = {}
    for acct in accounts_to_try:
        try:
            return fn(email_id, account=acct, **kwargs), acct
        except HttpError as e:
            if e.resp.status in (400, 404):
                errors[acct] = "not found" if e.resp.status == 404 else "invalid ID"
                continue
            raise
        except (RefreshError, RuntimeError) as e:
            errors[acct] = f"auth expired — re-connect with /addaccount {acct}"
            logger.warning(f"Gmail account '{acct}' auth failed: {e}")
            continue

    detail = "; ".join(f"{a}: {msg}" for a, msg in errors.items())
    raise RuntimeError(f"Email {email_id} not found ({detail})")


async def _fuzzy_index_lookup(source: str, source_id: str) -> str | None:
    """
    Try partial matches when exact source_id lookup fails.
    e.g. if source_id="personal:abc123", also try "work:abc123", "abc123", etc.
    """
    import aiosqlite

    db = await search_index.get_db()

    # Try LIKE match on the email_id portion
    bare_id = source_id.split(":")[-1] if ":" in source_id else source_id
    cursor = await db.execute(
        "SELECT content FROM search_index WHERE source = ? AND source_id LIKE ?",
        (source, f"%{bare_id}%"),
    )
    row = await cursor.fetchone()
    if row and row["content"]:
        return row["content"]

    # Try any account prefix with same bare ID
    for acc in gmail_module.get_connected_accounts():
        alt_id = f"{acc['label']}:{bare_id}"
        content = await search_index.get_full_content(source, alt_id)
        if content:
            return content

    return None


async def build_reply_action(email_id: str, account: str | None, body: str,
                             cc: str = "", bcc: str = "", attachments: list | None = None) -> dict:
    """
    Pending reply_email action for an existing message (threading metadata fetched
    live). Shared by the reply_email tool and the waiting-on Nudge. Raises if the
    message can't be found in any connected account.
    """
    import asyncio

    meta, used_acct = await asyncio.to_thread(
        _gmail_with_fallback, gmail_module.get_message_meta, email_id, account
    )
    to = meta["reply_to"] or meta["from"]
    subject = meta["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    payload = {
        "email_id": email_id, "body": body, "account": used_acct,
        "to": to, "subject": subject, "cc": cc, "bcc": bcc,
        "attachments": attachments or [],
    }
    return {
        "type": "reply_email",
        "payload": payload,
        "summary": f"Reply to {to}",
        "detail": action_detail("reply_email", payload),
    }


def build_imessage_action(to: str, to_name: str, body: str, service: str = "iMessage") -> dict:
    """Pending send_imessage action. Shared by the send_imessage tool and the Nudge."""
    payload = {"to": to, "to_name": to_name or "", "body": body,
               "service": "SMS" if (service or "").upper() == "SMS" else "iMessage"}
    return {
        "type": "send_imessage",
        "payload": payload,
        "summary": f"Text to {payload['to_name'] or payload['to']}",
        "detail": action_detail("send_imessage", payload),
    }


async def resolve_message_recipient(to_raw: str) -> dict:
    """
    Turn `to` (handle or name) into {to: handle, to_name, note} or {error: text}.

    A phone/email handle is normalized and named from the contacts table if known.
    A name must match exactly one person who has a phone (or an email handle already
    seen on iMessage); several plausible people → error asking which; none → error
    asking for the number. The approval card is the user's confirmation step.
    """
    from contacts_module import normalize_handle

    handle = normalize_handle(to_raw)
    if handle:
        name = ""
        for m in await db.search_contacts(handle, limit=5):
            if handle in m.get("phones", []) or handle in m.get("emails", []):
                name = m.get("name") or ""
                break
        return {"to": handle, "to_name": name}

    matches = await db.search_contacts(to_raw, limit=8)
    candidates = []
    for m in matches:
        if m.get("phones"):
            candidates.append((m, m["phones"][0], m["phones"][1:]))
        elif "imessage" in (m.get("channels") or []) and m.get("emails"):
            candidates.append((m, m["emails"][0], []))
    if len(candidates) == 1:
        m, to, alternates = candidates[0]
        note = f"(Other numbers for them: {', '.join(alternates)}.)" if alternates else ""
        return {"to": to, "to_name": m.get("name") or to_raw, "note": note}
    if len(candidates) > 1:
        options = "; ".join(f"{m.get('name') or '?'} {to}" for m, to, _ in candidates)
        return {"error": f"Several people match '{to_raw}': {options}. Ask the user which one "
                         "(or which number), then call send_imessage with that number."}
    if matches:
        names = ", ".join(m.get("name") or m.get("email") or "?" for m in matches[:3])
        return {"error": f"Found {names} but no phone number or iMessage handle on file for "
                         f"'{to_raw}'. Ask the user for the number."}
    return {"error": f"No contact matching '{to_raw}'. Ask the user for the phone number."}


async def resolve_conversation_target(contact: str, groups: bool = True) -> dict:
    """
    Who/what `contact` refers to for get_conversation:
      {handles: [...], chat_key, name}            a person (handle or unique name match)
      {chat_key, name, is_group: True}            a group chat by display name
      {error: text}                               ambiguous or unknown
    """
    import asyncio

    from contacts_module import normalize_handle

    raw = contact.strip()
    needle = raw.lower()

    key = normalize_handle(raw)
    if key:
        name = ""
        for m in await db.search_contacts(key, limit=5):
            if key in m.get("phones", []) or key in m.get("emails", []):
                name = m.get("name") or ""
                break
        handles = list(dict.fromkeys([key, raw]))
        return {"handles": handles, "chat_key": f"handle:{key}", "name": name or key}

    if groups:
        try:
            import imessage_module
            chats = await asyncio.to_thread(imessage_module.list_chats, 200)
        except Exception:
            chats = []
        hits = [c for c in chats if c.get("is_group") and needle in (c.get("display_name") or "").lower()]
        exact = [c for c in hits if (c.get("display_name") or "").lower() == needle]
        if len(exact) == 1 or len(hits) == 1:
            c = (exact or hits)[0]
            return {"chat_key": c["chat_key"], "name": c["display_name"], "is_group": True}
        if len(hits) > 1:
            names = ", ".join(f"\"{c['display_name']}\"" for c in hits[:5])
            return {"error": f"Several group chats match '{raw}': {names}. Ask the user which one."}

    matches = [m for m in await db.search_contacts(raw, limit=8) if m.get("phones") or m.get("emails")]
    exact = [m for m in matches if (m.get("name") or "").lower() == needle]
    if len(exact) == 1 or len(matches) == 1:
        m = (exact or matches)[0]
        handles = list(m.get("phones", [])) + list(m.get("emails", []))
        return {"handles": handles, "chat_key": f"handle:{handles[0]}", "name": m.get("name") or raw}
    if len(matches) > 1:
        names = "; ".join(f"{m.get('name') or '?'} ({', '.join((m.get('phones') or m.get('emails'))[:1])})"
                          for m in matches[:5])
        return {"error": f"Several people match '{raw}': {names}. Ask the user which one."}
    return {"error": f"No contact or group chat matching '{raw}'. Ask for their number, or try "
                     "search_everything with a topic instead."}


def _fmt_local(iso: str) -> str:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return (iso or "")[:16].replace("T", " ")


def format_timeline(msgs: list[dict], resolve, label: str, name: str, limit: int,
                    span_note: str = "") -> str:
    lines = []
    for m in msgs:
        who = "Me" if m.get("is_from_me") else resolve(m.get("sender") or m.get("contact") or "?")
        line = f"[{_fmt_local(m.get('date', ''))}] {who}: {m.get('text', '')}"
        atts = m.get("attachments") or []
        if atts:
            line += f" [attachments: {', '.join(atts)}]"
        lines.append(line)
    header = (f"{label} with {name} — {len(msgs)} message(s), "
              f"{_fmt_local(msgs[0].get('date', ''))} → {_fmt_local(msgs[-1].get('date', ''))}{span_note}")
    if len(msgs) >= limit:
        header += f" (newest {limit}; raise limit or narrow the dates for more)"
    return header + "\n" + "\n".join(lines)


async def get_conversation(args: dict) -> dict:
    """get_conversation tool: live timeline for one person or group chat."""
    import asyncio

    contact = (args.get("contact") or "").strip()
    if not contact:
        return {"result": "get_conversation needs a contact: a name, number, email handle, or group chat name."}
    source = (args.get("source") or "imessage").lower()
    try:
        limit = max(1, min(int(args.get("limit") or 40), 200))
    except (TypeError, ValueError):
        limit = 40
    after = _parse_filter_date(args.get("after"))
    before = _parse_filter_date(args.get("before"), end_of_day=True)
    span_note = ""
    if after or before:
        span_note = " (filtered" + (f" from {args.get('after')}" if after else "") \
                    + (f" to {args.get('before')}" if before else "") + ")"

    if source == "whatsapp":
        return await _whatsapp_conversation(contact, after, before, limit, span_note)

    import contacts_module
    import imessage_module
    if not imessage_module.is_available():
        return {"result": "iMessage isn't available on this machine (Mac only). Try search_everything."}

    target = await resolve_conversation_target(contact)
    if target.get("error"):
        return {"result": target["error"]}

    msgs = await asyncio.to_thread(
        imessage_module.get_timeline,
        None if target.get("is_group") else target.get("handles"),
        target["chat_key"] if target.get("is_group") else None,
        after.isoformat() if after else None,
        before.isoformat() if before else None,
        limit,
    )
    label = "iMessage group chat" if target.get("is_group") else "iMessage/SMS conversation"
    name = target["name"]
    entities = [{"type": "imessage_chat", "id": target["chat_key"], "label": name[:80]}]
    if not msgs:
        return {"result": f"No messages found for {label} with {name}{span_note}.", "entities": entities}
    resolve = await asyncio.to_thread(contacts_module.resolver_fn)
    return {"result": format_timeline(msgs, resolve, label, name, limit, span_note),
            "entities": entities}


async def _whatsapp_conversation(contact: str, after, before, limit: int, span_note: str) -> dict:
    import asyncio

    import contacts_module
    import whatsapp_module
    if not whatsapp_module.is_available():
        return {"result": "WhatsApp isn't available on this machine (Mac only, WhatsApp app linked). "
                          "Try search_everything with source=whatsapp for indexed history."}
    try:
        msgs = await asyncio.to_thread(whatsapp_module.get_messages_from_contact, contact,
                                       limit=max(limit * 3, 200))
    except Exception as e:
        return {"result": f"WhatsApp read failed: {e}"}
    kept = []
    for m in msgs:  # newest first from the reader
        dt = _parse_filter_date(m.get("date", ""))
        if after and (dt is None or dt < after):
            continue
        if before and (dt is None or dt >= before):
            continue
        kept.append(m)
        if len(kept) >= limit:
            break
    kept.reverse()
    name = kept[0].get("contact") if kept else contact
    entities = [{"type": "whatsapp_chat", "id": f"handle:{name}", "label": str(name)[:80]}]
    if not kept:
        return {"result": f"No WhatsApp messages found with {contact}{span_note}.", "entities": entities}
    resolve = await asyncio.to_thread(contacts_module.resolver_fn)
    return {"result": format_timeline(kept, resolve, "WhatsApp conversation", name, limit, span_note),
            "entities": entities}


async def _index_fallback(email_id: str, account: str | None) -> str | None:
    """Try to retrieve email/PDF content from the search index when Gmail is unreachable."""
    # Try exact matches first: "account:msg_id"
    candidates = []
    if account:
        candidates.append(f"{account}:{email_id}")
    for acc in gmail_module.get_connected_accounts():
        sid = f"{acc['label']}:{email_id}"
        if sid not in candidates:
            candidates.append(sid)
    candidates.append(email_id)

    for source_id in candidates:
        content = await search_index.get_full_content("gmail", source_id)
        if content:
            return f"(Retrieved from index)\n\n{content[:4000]}"

    # Fuzzy: LIKE match on the bare email ID
    content = await _fuzzy_index_lookup("gmail", email_id)
    if content:
        return f"(Retrieved from index)\n\n{content[:4000]}"

    return None


# ── Tool Execution ────────────────────────────────────────────────

async def execute_tool(name: str, args: dict) -> dict:
    """
    Execute a tool and return the result.
    Returns {"result": str, "action": dict|absent, "entities": list|absent}.
    - action: write operations needing user approval (send/reply email).
    - entities: referenced items (emails, docs) for the conversation working set.
    Sync Gmail/OneDrive calls run in a thread so the bot never blocks.
    """
    import asyncio

    try:
        if name == "search_everything":
            after = _parse_filter_date(args.get("after"))
            before = _parse_filter_date(args.get("before"), end_of_day=True)
            limit = min(int(args.get("limit") or 5), 15)
            results = await search_index.hybrid_search(
                args["query"],
                source_filter=args.get("source"),
                sender=args.get("sender"),
                after=after,
                before=before,
                limit=limit,
            )
            if results:
                # Enrich top results with full content so the LLM can answer directly
                for r in results[:3]:
                    full = await search_index.get_full_content(r["source"], r["source_id"])
                    if full:
                        r["full_content"] = full[:2000]
            entities = [
                {"type": r["source"], "id": r["source_id"], "label": (r["title"] or "")[:80]}
                for r in results[:5]
            ]
            return {"result": _format_search_results(results), "entities": entities}

        elif name == "list_recent_emails":
            account = _clean_account(args)
            count = min(args.get("count", 10), 25)
            emails = await asyncio.to_thread(gmail_module.list_emails, max_results=count, account=account)
            active = account or gmail_module.get_active_account()
            return {"result": _format_email_list(emails, account=active),
                    "entities": _email_entities(emails, active)}

        elif name == "search_emails":
            account = _clean_account(args)
            emails = await asyncio.to_thread(
                gmail_module.search_emails, args["query"], max_results=10, account=account
            )
            active = account or gmail_module.get_active_account()
            return {"result": _format_email_list(emails, account=active),
                    "entities": _email_entities(emails, active)}

        elif name == "read_email":
            email_id, account = _resolve_email_id(args)
            try:
                email, used_acct = await asyncio.to_thread(
                    _gmail_with_fallback, gmail_module.read_email, email_id, account
                )
                try:
                    await asyncio.to_thread(gmail_module.mark_as_read, email_id, account=used_acct)
                except Exception:
                    pass
                entity = {"type": "email", "id": email_id, "account": used_acct,
                          "label": f"{email.get('subject', '')[:60]} (from {email.get('from', '')[:40]})"}
                return {"result": _format_full_email(email), "entities": [entity]}
            except RuntimeError:
                # Gmail failed — fall back to indexed content
                content = await _index_fallback(email_id, account)
                if content:
                    return {"result": content}
                return {"result": f"Could not read email {email_id}. The account token may be expired — use /addaccount to reconnect."}

        elif name == "read_pdf_from_email":
            email_id, account = _resolve_email_id(args)
            try:
                result, used_acct = await asyncio.to_thread(
                    _gmail_with_fallback, gmail_module.get_email_with_pdf_text, email_id, account
                )
                entity = {"type": "email", "id": email_id, "account": used_acct,
                          "label": f"{result.get('subject', '')[:60]} (has PDF)"}
                return {"result": _format_email_with_pdfs(result), "entities": [entity]}
            except RuntimeError:
                # Gmail failed — fall back to indexed content (includes PDF text)
                content = await _index_fallback(email_id, account)
                if content:
                    return {"result": content}
                return {"result": f"Could not read PDF from email {email_id}. The account token may be expired — use /addaccount to reconnect."}

        elif name == "search_pdfs":
            account = _clean_account(args)
            results = await asyncio.to_thread(
                gmail_module.search_pdfs, args["query"], max_results=5, account=account
            )
            return {"result": _format_pdf_results(results)}

        elif name == "draft_email":
            # WRITE action — returned for approval instead of executing
            account = _clean_account(args) or gmail_module.get_active_account()
            payload = {
                "to": args["to"], "subject": args["subject"], "body": args["body"],
                "cc": args.get("cc", ""), "bcc": args.get("bcc", ""),
                "html": bool(args.get("html")), "attachments": args.get("attachments") or [],
                "account": account,
            }
            return {
                "result": f"Draft prepared: To={args['to']}, Subject={args['subject']}. Awaiting user approval.",
                "action": {
                    "type": "send_email",
                    "payload": payload,
                    "summary": f"Email to {args['to']}",
                    "detail": action_detail("send_email", payload),
                },
            }

        elif name == "reply_email":
            email_id, account = _resolve_email_id(args)
            try:
                action = await build_reply_action(
                    email_id, account, args["body"],
                    cc=args.get("cc", ""), bcc=args.get("bcc", ""),
                    attachments=args.get("attachments") or [],
                )
            except Exception as e:
                logger.warning(f"reply_email meta lookup failed for {email_id}: {e}")
                return {"result": f"Could not find the email to reply to ({email_id}): {e}"}
            p = action["payload"]
            return {
                "result": f"Reply prepared to {p['to']}, subject '{p['subject']}'. Awaiting user approval.",
                "action": action,
                "entities": [{"type": "email", "id": email_id, "account": p["account"],
                              "label": f"{p['subject'][:60]} (replying)"}],
            }

        elif name == "send_imessage":
            # WRITE action — resolve the recipient, then hand back for approval. Never sends.
            to_raw = (args.get("to") or "").strip()
            body = (args.get("body") or "").strip()
            service = "SMS" if str(args.get("service") or "").upper() == "SMS" else "iMessage"
            if not to_raw or not body:
                return {"result": "send_imessage needs both `to` and `body`."}

            resolved = await resolve_message_recipient(to_raw)
            if resolved.get("error"):
                return {"result": resolved["error"]}

            action = build_imessage_action(resolved["to"], resolved.get("to_name", ""), body, service)
            label = message_recipient_label(action["payload"])
            note = f" {resolved['note']}" if resolved.get("note") else ""
            return {
                "result": f"Message prepared to {label} via {service}. Awaiting user approval.{note}",
                "action": action,
            }

        elif name == "list_waiting_on":
            import waiting_on
            items = await waiting_on.list_waiting()
            entities = []
            for it in items[:8]:
                if it["kind"] == "email":
                    entities.append({"type": "email", "id": it["email_id"], "account": it["account"],
                                     "label": f"{it['subject'][:50]} (waiting {it['days_waiting']}d)"})
                else:
                    entities.append({"type": "imessage_chat", "id": it["chat_key"],
                                     "label": f"{it['contact']} (waiting {it['days_waiting']}d)"})
            return {"result": waiting_on.format_waiting(items), "entities": entities}

        elif name == "get_conversation":
            return await get_conversation(args)

        elif name == "find_contact":
            matches = await db.search_contacts(args["name"], limit=5)
            if not matches:
                return {"result": f"No contact matching '{args['name']}' in Contacts or past "
                                  "correspondence. Ask the user for the email address or number."}
            lines = []
            for m in matches:
                nm = m["name"] or "(no name)"
                parts = []
                if m.get("emails"):
                    parts.append("email " + ", ".join(m["emails"][:3]))
                if m.get("phones"):
                    parts.append("phone " + ", ".join(m["phones"][:3]))
                channels = ", ".join(m.get("channels") or []) or "unknown"
                last = f", last {m['last_seen'][:10]}" if m.get("last_seen") else ""
                lines.append(f"- {nm}: {'; '.join(parts) or 'no address'} — channels: {channels}; "
                             f"{m['count']} interactions{last}")
            return {"result": "Contact matches (most frequent first):\n" + "\n".join(lines)}

        elif name == "remember":
            memory_id = await db.add_memory(args["fact"], args.get("category"))
            return {"result": f"Saved to memory (#{memory_id}): {args['fact']}"}

        elif name == "set_reminder":
            import reminders
            text = (args.get("text") or "").strip()
            if not text:
                return {"result": "set_reminder needs `text` (what to remind about)."}
            try:
                due = reminders.parse_when(args.get("when", ""))
            except ValueError as e:
                return {"result": f"Can't set that reminder: {e}. Pass an absolute ISO local time."}
            rid = await db.add_reminder(text, due.isoformat())
            label = f"{reminders.fmt_local(due)}: {text}"
            return {"result": f"Reminder #{rid} set for {label}.",
                    "entities": [{"type": "reminder", "id": str(rid), "label": label[:80]}]}

        elif name == "list_reminders":
            import reminders
            pending = await db.list_reminders("pending", limit=30)
            if not pending:
                return {"result": "No pending reminders."}
            return {"result": "Pending reminders:\n" + "\n".join(reminders.describe(r) for r in pending),
                    "entities": [{"type": "reminder", "id": str(r["id"]),
                                  "label": f"{reminders.fmt_local(r['due_at'])}: {r['text']}"[:80]}
                                 for r in pending[:5]]}

        elif name == "cancel_reminder":
            try:
                rid = int(args.get("id"))
            except (TypeError, ValueError):
                return {"result": "cancel_reminder needs a numeric id."}
            r = await db.get_reminder(rid)
            if r is None or r["status"] != "pending":
                return {"result": f"No pending reminder #{rid}."}
            await db.mark_reminder(rid, "cancelled")
            return {"result": f"Cancelled reminder #{rid}: {r['text']}"}

        elif name == "check_reviews":
            import reviews
            alerts = await reviews.poll_all_properties()
            if not alerts:
                return {"result": "No new reviews found across any monitored properties."}
            lines = []
            for a in alerts:
                stars = reviews.rating_stars(a["rating"])
                lines.append(f"{stars} {a['property_name']} — by {a['author']}: {a.get('text', '')[:200]}")
            return {"result": "\n".join(lines)}

        elif name == "list_properties":
            props = await db.list_properties()
            if not props:
                return {"result": "No properties being monitored."}
            lines = [f"- {p['name']} ({p['address']})" for p in props]
            return {"result": "\n".join(lines)}

        elif name == "search_onedrive":
            try:
                from onedrive_module import search_files
                files = await asyncio.to_thread(search_files, args["query"])
                if not files:
                    return {"result": f"No OneDrive files matching '{args['query']}'."}
                lines = [f"- {f['name']} (ID: {f['id']}, {f['size']//1024}KB)" for f in files[:10]]
                return {"result": "\n".join(lines)}
            except Exception as e:
                return {"result": f"OneDrive not available: {e}"}

        elif name == "read_onedrive_file":
            try:
                from onedrive_module import download_file_text
                text = await asyncio.to_thread(download_file_text, args["file_id"], args["filename"])
                if not text:
                    return {"result": "No text could be extracted from this file."}
                return {"result": f"Content of {args['filename']}:\n\n{text[:3000]}"}
            except Exception as e:
                return {"result": f"Failed to read file: {e}"}

        elif name == "read_indexed_document":
            source = args["source"]
            source_id = args["source_id"]
            content = await search_index.get_full_content(source, source_id)
            if not content:
                # Fuzzy fallback: search for partial match on source_id
                content = await _fuzzy_index_lookup(source, source_id)
            if not content:
                logger.info(f"read_indexed_document: no content for {source}:{source_id}")
                return {"result": f"No indexed content found for {source}:{source_id}. Try read_email or read_pdf_from_email instead."}
            return {"result": content[:4000]}

        elif name == "get_index_stats":
            stats = await search_index.get_index_stats()
            lines = [f"- {k}: {v} items" for k, v in stats.items()]
            return {"result": "\n".join(lines)}

        else:
            return {"result": f"Unknown tool: {name}"}

    except Exception as e:
        logger.exception(f"Tool execution failed: {name}")
        return {"result": f"Error executing {name}: {e}"}


# ── Formatting Helpers ────────────────────────────────────────────

def _email_entities(emails: list[dict], account: str | None) -> list[dict]:
    return [
        {"type": "email", "id": em["id"], "account": account,
         "label": f"{em.get('subject', '')[:60]} (from {em.get('from', '')[:40]})"}
        for em in emails[:5]
    ]


def _format_search_results(results: list[dict]) -> str:
    if not results:
        return ("No results found. Try different keywords, a semantic description, "
                "or a higher limit (up to 15).")
    lines = []
    for r in results:
        meta = r.get("metadata", {})
        source = r["source"]
        source_id = r["source_id"]

        extra = ""
        if meta.get("from"):
            extra = f" (from: {meta['from'][:40]})"
        elif meta.get("contact"):
            extra = f" (contact: {meta['contact']})"
        if meta.get("date"):
            extra += f" [{meta['date'][:16]}]"

        entry = f"[{source}] {r['title']}{extra}\n  source_id: {source_id}"
        if source == "gmail":
            entry += f"\n  gmail_link: {_gmail_link(source_id)}"
        entry += f"\n  {r['snippet']}"
        if r.get("full_content"):
            entry += f"\n  --- Full content ---\n  {r['full_content']}"
        else:
            entry += f"\n  → Use read_indexed_document(source=\"{source}\", source_id=\"{source_id}\") for full text"
        lines.append(entry)
    return "\n\n".join(lines)


def _format_email_list(emails: list[dict], account: str | None = None) -> str:
    if not emails:
        return "No emails found."
    acc_note = f" (account: {account})" if account else ""
    lines = []
    for em in emails:
        unread = " [NEW]" if em.get("unread") else ""
        lines.append(
            f"email_id: {em['id']}{acc_note}\n  From: {em['from'][:50]}\n"
            f"  Subject: {em['subject']}{unread}\n  Date: {em['date']}\n"
            f"  gmail_link: {_gmail_link(em['id'])}"
        )
    return "\n\n".join(lines)


def _format_full_email(email: dict) -> str:
    atts = email.get("attachments", [])
    att_str = ""
    if atts:
        att_names = [a["filename"] for a in atts]
        att_str = f"\nAttachments: {', '.join(att_names)}"

    body = email.get("body", "")[:2000]
    return (
        f"From: {email['from']}\nTo: {email['to']}\n"
        f"Subject: {email['subject']}\nDate: {email['date']}"
        f"{att_str}\n\n{body}"
    )


def _format_email_with_pdfs(result: dict) -> str:
    base = _format_full_email(result)
    pdf_texts = result.get("pdf_texts", [])
    if not pdf_texts:
        return base + "\n\n(No PDF attachments)"
    parts = [base]
    for pdf in pdf_texts:
        parts.append(f"\n--- PDF: {pdf['filename']} ---\n{pdf['text'][:2000]}")
    return "\n".join(parts)


def _format_pdf_results(results: list[dict]) -> str:
    if not results:
        return "No PDFs found."
    parts = []
    for r in results:
        parts.append(
            f"Email: {r['email_subject']} (from {r['email_from']})\n"
            f"PDF: {r['filename']}\n"
            f"Content preview: {r['text'][:500]}"
        )
    return "\n\n---\n\n".join(parts)


# ── Convert to OpenAI format ─────────────────────────────────────

def get_openai_tools() -> list[dict]:
    """Convert our tool schemas to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOL_SCHEMAS
    ]

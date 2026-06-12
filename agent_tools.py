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

TOOL_SCHEMAS = [
    {
        "name": "search_everything",
        "description": (
            "Search across ALL sources — emails, PDFs, OneDrive files, iMessages, WhatsApp messages. "
            "Use this as the first step when the user asks about any document, conversation, file, or message. "
            "Returns ranked results with snippets."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — use keywords from what the user is looking for",
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
        "name": "draft_email",
        "description": (
            "Draft an email to send. ALWAYS use this when the user wants to send, reply, or write an email. "
            "The draft will be shown to the user for approval before sending — NEVER claim it was sent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body text"},
            },
            "required": ["to", "subject", "body"],
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
            return fn(email_id, account=acct, **kwargs)
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
    Returns {"result": ..., "action": None} for read operations.
    Returns {"result": ..., "action": {...}} for write operations needing approval.
    """
    try:
        if name == "search_everything":
            results = await search_index.search(args["query"], limit=5)
            if results:
                # Enrich top results with full content so the LLM can answer directly
                for r in results[:3]:
                    full = await search_index.get_full_content(r["source"], r["source_id"])
                    if full:
                        r["full_content"] = full[:2000]
            return {"result": _format_search_results(results)}

        elif name == "list_recent_emails":
            account = _clean_account(args)
            count = min(args.get("count", 10), 25)
            emails = gmail_module.list_emails(max_results=count, account=account)
            active = account or gmail_module.get_active_account()
            return {"result": _format_email_list(emails, account=active)}

        elif name == "search_emails":
            account = _clean_account(args)
            emails = gmail_module.search_emails(args["query"], max_results=10, account=account)
            active = account or gmail_module.get_active_account()
            return {"result": _format_email_list(emails, account=active)}

        elif name == "read_email":
            email_id, account = _resolve_email_id(args)
            try:
                email = _gmail_with_fallback(gmail_module.read_email, email_id, account)
                try:
                    gmail_module.mark_as_read(email_id, account=account)
                except Exception:
                    pass
                return {"result": _format_full_email(email)}
            except RuntimeError:
                # Gmail failed — fall back to indexed content
                content = await _index_fallback(email_id, account)
                if content:
                    return {"result": content}
                return {"result": f"Could not read email {email_id}. The work account token may be expired — use /addaccount work to reconnect."}

        elif name == "read_pdf_from_email":
            email_id, account = _resolve_email_id(args)
            try:
                result = _gmail_with_fallback(gmail_module.get_email_with_pdf_text, email_id, account)
                return {"result": _format_email_with_pdfs(result)}
            except RuntimeError:
                # Gmail failed — fall back to indexed content (includes PDF text)
                content = await _index_fallback(email_id, account)
                if content:
                    return {"result": content}
                return {"result": f"Could not read PDF from email {email_id}. The work account token may be expired — use /addaccount work to reconnect."}

        elif name == "search_pdfs":
            account = _clean_account(args)
            results = gmail_module.search_pdfs(args["query"], max_results=5, account=account)
            return {"result": _format_pdf_results(results)}

        elif name == "draft_email":
            # This is a WRITE action — return it for approval instead of executing
            return {
                "result": f"Draft prepared: To={args['to']}, Subject={args['subject']}",
                "action": {
                    "type": "send_email",
                    "payload": {"to": args["to"], "subject": args["subject"], "body": args["body"]},
                    "summary": f"Email to {args['to']}",
                    "detail": f"*To:* {args['to']}\n*Subject:* {args['subject']}\n\n{args['body']}",
                },
            }

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
                files = search_files(args["query"])
                if not files:
                    return {"result": f"No OneDrive files matching '{args['query']}'."}
                lines = [f"- {f['name']} (ID: {f['id']}, {f['size']//1024}KB)" for f in files[:10]]
                return {"result": "\n".join(lines)}
            except Exception as e:
                return {"result": f"OneDrive not available: {e}"}

        elif name == "read_onedrive_file":
            try:
                from onedrive_module import download_file_text
                text = download_file_text(args["file_id"], args["filename"])
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

def _format_search_results(results: list[dict]) -> str:
    if not results:
        return "No results found in the index."
    lines = []
    for r in results:
        meta = r.get("metadata", {})
        source = r["source"]
        source_id = r["source_id"]

        extra = ""
        if meta.get("from"):
            extra = f" (from: {meta['from'][:30]})"
        elif meta.get("contact"):
            extra = f" (contact: {meta['contact']})"

        entry = f"[{source}] {r['title']}{extra}\n  {r['snippet']}"
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
        lines.append(f"email_id: {em['id']}{acc_note}\n  From: {em['from'][:50]}\n  Subject: {em['subject']}{unread}\n  Date: {em['date']}")
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

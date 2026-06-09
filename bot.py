"""
Telegram bot — the primary interface for Clawdbot.

Commands:
  /start          — Welcome + auth check
  /pin <code>     — Unlock session (if PIN configured)
  /lock           — Lock session manually
  /switch <llm>   — Switch between claude / chatgpt
  /status         — Current state
  /audit          — Recent audit log entries
  /emails         — List recent inbox emails
  /search <query> — Search emails (Gmail query syntax)
  /read <id>      — Read a specific email by ID
  /draft          — AI-draft a reply or new email (approval required)
  /send           — Send an email (approval required)
  /properties     — List monitored properties
  /addproperty    — Search & add a property to monitor
  /removeproperty — Stop monitoring a property
  /reviews        — Show recent reviews across all properties
  /checkreviews   — Manually trigger a review check now
  /help           — List commands

All free-text messages are routed to the active LLM for conversation.

Outbound actions (send email, send message) go through the approval flow:
  1. Bot presents a preview with Approve / Edit / Reject buttons
  2. Only on Approve does the action execute
  3. Everything is audit-logged
"""

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import gmail_module
import llm
import reviews
import security
from security import ActionCategory, auth_required

logger = logging.getLogger(__name__)


# ── Command Handlers ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not security.is_authorized(user.id):
        await db.log_audit("unauthorized_access", "security",
                           f"Blocked /start from {user.id} ({user.username})",
                           user_id=user.id, approved=False)
        return

    if not security.is_session_unlocked():
        await update.message.reply_text(
            "Clawdbot is locked. Send your PIN with /pin <code> to unlock."
        )
        return

    await db.log_audit("session_start", "security", "Bot started", user_id=user.id, approved=True)
    await update.message.reply_text(
        "Clawdbot online.\n\n"
        f"LLM: {llm.get_active_llm()}\n"
        f"Session: {'unlocked' if security.is_session_unlocked() else 'locked'}\n\n"
        "Type /help for commands, or just send me a message."
    )


@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Clawdbot Commands*\n\n"
        "*General*\n"
        "/start — Wake up & status\n"
        "/pin `<code>` — Unlock session\n"
        "/lock — Lock session\n"
        "/switch `<claude|chatgpt>` — Switch LLM\n"
        "/status — Current state\n"
        "/audit — Recent audit log\n"
        "/help — This message\n\n"
        "*Email*\n"
        "/accounts — List connected Gmail accounts\n"
        "/addaccount `<label>` — Connect a Gmail (e.g. personal, work)\n"
        "/useaccount `<label>` — Switch active account\n"
        "/emails — Recent inbox (10)\n"
        "/emails `<N>` — Recent inbox (N)\n"
        "/search `<query>` — Search emails\n"
        "/read `<id>` — Read full email\n"
        "/draft `<to> | <subject> | <body>` — Draft email for approval\n"
        "/send `<to> | <subject> | <body>` — Send email (requires approval)\n"
        "/aidraft `<instructions>` — AI drafts an email for you\n\n"
        "*Reviews*\n"
        "/properties — List monitored properties\n"
        "/addproperty `<name>` — Search & add a property\n"
        "/removeproperty `<place_id>` — Stop monitoring\n"
        "/reviews — Recent reviews (all properties)\n"
        "/checkreviews — Manual review check now\n\n"
        "_Just type normally to chat with the AI._",
        parse_mode="Markdown",
    )


async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not security.is_authorized(user.id):
        return

    if not config.SESSION_PIN:
        await update.message.reply_text("No PIN configured. Session is always unlocked.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /pin <code>")
        return

    attempt = " ".join(context.args)
    if attempt == config.SESSION_PIN:
        security.unlock_session()
        await db.log_audit("session_unlock", "security", "Session unlocked via PIN",
                           user_id=user.id, approved=True)
        await update.message.reply_text("Session unlocked.")
    else:
        await db.log_audit("failed_pin", "security", "Incorrect PIN attempt",
                           user_id=user.id, approved=False)
        await update.message.reply_text("Incorrect PIN.")


@auth_required
async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    security.lock_session()
    await db.log_audit("session_lock", "security", "Session locked manually",
                       user_id=update.effective_user.id, approved=True)
    await update.message.reply_text("Session locked. Use /pin to unlock.")


@auth_required
async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            f"Current LLM: *{llm.get_active_llm()}*\n\nUsage: /switch `claude` or /switch `chatgpt`",
            parse_mode="Markdown",
        )
        return

    result = llm.switch_llm(context.args[0])
    await db.log_audit("llm_switch", "config", result, user_id=update.effective_user.id, approved=True)
    await update.message.reply_text(result)


@auth_required
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rate_ok = security.check_rate_limit()
    used = len(security._outbound_timestamps)
    await update.message.reply_text(
        f"*Clawdbot Status*\n\n"
        f"LLM: `{llm.get_active_llm()}`\n"
        f"Session: {'unlocked' if security.is_session_unlocked() else 'LOCKED'}\n"
        f"Outbound this hour: {used}/{config.RATE_LIMIT_PER_HOUR}\n"
        f"Rate limit: {'OK' if rate_ok else 'EXCEEDED'}",
        parse_mode="Markdown",
    )


@auth_required
async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = await db.get_db()
    cursor = await conn.execute(
        "SELECT timestamp, action_type, summary, approved FROM audit_log ORDER BY id DESC LIMIT 10"
    )
    rows = await cursor.fetchall()
    if not rows:
        await update.message.reply_text("No audit log entries yet.")
        return

    lines = ["*Recent Audit Log*\n"]
    for r in rows:
        ts = r["timestamp"][:16].replace("T", " ")
        status = ""
        if r["approved"] is not None:
            status = " [OK]" if r["approved"] else " [DENIED]"
        lines.append(f"`{ts}` {r['action_type']}{status}\n  {r['summary']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Account Management ────────────────────────────────────────────

@auth_required
async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all connected Gmail accounts."""
    accounts = gmail_module.get_connected_accounts()
    active = gmail_module.get_active_account()

    if not accounts:
        await update.message.reply_text(
            "No Gmail accounts connected.\n\n"
            "Use /addaccount `personal` or /addaccount `work` to connect one.",
            parse_mode="Markdown",
        )
        return

    lines = ["*Connected Gmail Accounts*\n"]
    for acc in accounts:
        marker = " (active)" if acc["label"] == active else ""
        email_str = f" — {acc['email']}" if acc["email"] else ""
        lines.append(f"  `{acc['label']}`{email_str}{marker}")

    lines.append(f"\nSwitch with: /useaccount `<label>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_addaccount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Connect a new Gmail account via OAuth."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /addaccount `<label>`\n\n"
            "Examples:\n"
            "  /addaccount personal\n"
            "  /addaccount work\n\n"
            "A browser window will open for Google sign-in.\n"
            "Make sure the email is added as a test user in Google Cloud Console.",
            parse_mode="Markdown",
        )
        return

    label = context.args[0].lower().strip()
    if not label.isalnum():
        await update.message.reply_text("Label must be alphanumeric (e.g. personal, work, rental).")
        return

    await update.message.reply_text(
        f"Opening browser for Google sign-in...\n"
        f"Sign in with the email you want to label as *{label}*.",
        parse_mode="Markdown",
    )

    try:
        email = gmail_module.connect_account(label)
    except FileNotFoundError as e:
        await update.message.reply_text(str(e))
        return
    except Exception as e:
        logger.exception("OAuth flow failed")
        await update.message.reply_text(f"Connection failed: {e}")
        return

    await db.log_audit("add_gmail_account", "config",
                       f"Connected Gmail account: {label} ({email})",
                       user_id=update.effective_user.id, approved=True)
    await update.message.reply_text(
        f"Connected *{label}* account: `{email}`\n\n"
        f"Active account: *{gmail_module.get_active_account()}*\n"
        f"Use /useaccount `{label}` to switch.",
        parse_mode="Markdown",
    )


@auth_required
async def cmd_useaccount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the active Gmail account."""
    if not context.args:
        active = gmail_module.get_active_account()
        await update.message.reply_text(
            f"Active account: *{active or 'none'}*\n\n"
            f"Usage: /useaccount `<label>`\n"
            f"See /accounts for available labels.",
            parse_mode="Markdown",
        )
        return

    label = context.args[0].lower().strip()
    if gmail_module.set_active_account(label):
        await db.log_audit("switch_gmail_account", "config",
                           f"Switched to Gmail account: {label}",
                           user_id=update.effective_user.id, approved=True)
        await update.message.reply_text(f"Switched to *{label}* account.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"No account with label '{label}'. Use /accounts to see connected accounts."
        )


# ── Email Commands ────────────────────────────────────────────────

def _truncate(text: str, max_len: int = 300) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def _active_label() -> str:
    """Return display string for the current active account."""
    active = gmail_module.get_active_account()
    return f" [{active}]" if active else ""


@auth_required
async def cmd_emails(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    count = 10
    if context.args:
        try:
            count = min(int(context.args[0]), 25)
        except ValueError:
            pass

    try:
        emails = gmail_module.list_emails(max_results=count)
    except FileNotFoundError as e:
        await update.message.reply_text(f"Gmail setup needed: {e}")
        return
    except Exception as e:
        logger.exception("Gmail list failed")
        await update.message.reply_text(f"Gmail error: {e}")
        return

    if not emails:
        await update.message.reply_text("No emails found.")
        return

    await db.log_audit("list_emails", "read", f"Listed {len(emails)} emails",
                       user_id=update.effective_user.id, approved=True)

    lines = [f"*Inbox{_active_label()} ({len(emails)} emails)*\n"]
    for i, em in enumerate(emails, 1):
        unread = " NEW" if em["unread"] else ""
        lines.append(
            f"{i}. {em['from'][:40]}\n"
            f"   {em['subject'][:60]}{unread}\n"
            f"   `{em['id']}`"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /search `<query>`\n\n"
            "Examples:\n"
            "  /search from:tenant@email.com\n"
            "  /search subject:maintenance\n"
            "  /search is:unread\n"
            "  /search newer_than:2d",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args)
    try:
        emails = gmail_module.search_emails(query, max_results=10)
    except Exception as e:
        logger.exception("Gmail search failed")
        await update.message.reply_text(f"Search error: {e}")
        return

    await db.log_audit("search_emails", "read", f"Searched: {query} ({len(emails)} results)",
                       user_id=update.effective_user.id, approved=True)

    if not emails:
        await update.message.reply_text(f"No results for: {query}")
        return

    lines = [f"*Search: {query}* ({len(emails)} results)\n"]
    for i, em in enumerate(emails, 1):
        lines.append(
            f"{i}. {em['from'][:40]}\n"
            f"   {em['subject'][:60]}\n"
            f"   `{em['id']}`"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /read `<email_id>`\nGet IDs from /emails or /search")
        return

    msg_id = context.args[0]
    try:
        email = gmail_module.read_email(msg_id)
        gmail_module.mark_as_read(msg_id)
    except Exception as e:
        logger.exception("Gmail read failed")
        await update.message.reply_text(f"Read error: {e}")
        return

    await db.log_audit("read_email", "read", f"Read email {msg_id}: {email['subject'][:50]}",
                       user_id=update.effective_user.id, approved=True)

    body = _truncate(email["body"], 3000)
    text = (
        f"*From:* {email['from']}\n"
        f"*To:* {email['to']}\n"
    )
    if email.get("cc"):
        text += f"*CC:* {email['cc']}\n"
    text += (
        f"*Subject:* {email['subject']}\n"
        f"*Date:* {email['date']}\n\n"
        f"{body}"
    )

    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i + 4000])
    else:
        await update.message.reply_text(text)


@auth_required
async def cmd_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send email with approval. Format: /send to | subject | body"""
    if not context.args:
        await update.message.reply_text(
            "Usage: /send `to@email.com | Subject line | Email body text`\n\n"
            "Separate to, subject, and body with `|`",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|", 2)]
    if len(parts) < 3:
        await update.message.reply_text("Need 3 parts separated by |: to | subject | body")
        return

    to, subject, body = parts

    detail = (
        f"*To:* {to}\n"
        f"*Subject:* {subject}\n\n"
        f"{body}"
    )

    await request_approval(
        update, context,
        action_type="send_email",
        category=ActionCategory.WRITE,
        summary=f"Email to {to}",
        detail=detail,
        payload={"to": to, "subject": subject, "body": body},
    )


@auth_required
async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a draft in Gmail. Format: /draft to | subject | body"""
    if not context.args:
        await update.message.reply_text(
            "Usage: /draft `to@email.com | Subject line | Email body text`\n\n"
            "Saves to Gmail drafts (no approval needed — it doesn't send).",
            parse_mode="Markdown",
        )
        return

    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|", 2)]
    if len(parts) < 3:
        await update.message.reply_text("Need 3 parts separated by |: to | subject | body")
        return

    to, subject, body = parts
    try:
        result = gmail_module.create_draft(to, subject, body)
    except Exception as e:
        logger.exception("Draft creation failed")
        await update.message.reply_text(f"Draft error: {e}")
        return

    await db.log_audit("create_draft", "write", f"Draft created: {subject} -> {to}",
                       user_id=update.effective_user.id, approved=True)
    await update.message.reply_text(
        f"Draft saved.\n\n*To:* {to}\n*Subject:* {subject}\n*Draft ID:* `{result['draft_id']}`",
        parse_mode="Markdown",
    )


@auth_required
async def cmd_aidraft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Have the AI draft an email based on instructions, then present for approval."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /aidraft `<instructions>`\n\n"
            "Examples:\n"
            '  /aidraft reply to tenant about maintenance request being scheduled for Monday\n'
            '  /aidraft email john@example.com about the lease renewal for unit 4B\n'
            '  /aidraft follow up on the plumbing issue at 123 Oak St',
            parse_mode="Markdown",
        )
        return

    instructions = " ".join(context.args)

    prompt = (
        f"Draft an email based on these instructions: {instructions}\n\n"
        "Respond in EXACTLY this format (no markdown, no extra text):\n"
        "TO: <email address>\n"
        "SUBJECT: <subject line>\n"
        "BODY:\n<email body>\n\n"
        "If no email address is given, use UNKNOWN for TO. "
        "Keep it professional and concise."
    )

    try:
        history = await db.get_recent_conversations(limit=10)
        response = await llm.chat(prompt, history)
    except Exception as e:
        logger.exception("AI draft failed")
        await update.message.reply_text(f"AI draft error: {e}")
        return

    to, subject, body = _parse_ai_draft(response)

    detail = (
        f"*To:* {to}\n"
        f"*Subject:* {subject}\n\n"
        f"{body}"
    )

    await request_approval(
        update, context,
        action_type="send_email",
        category=ActionCategory.WRITE,
        summary=f"AI-drafted email to {to}",
        detail=detail,
        payload={"to": to, "subject": subject, "body": body},
    )


def _parse_ai_draft(text: str) -> tuple[str, str, str]:
    to = "UNKNOWN"
    subject = "(no subject)"
    body = text

    lines = text.strip().split("\n")
    body_start = 0

    for i, line in enumerate(lines):
        if line.upper().startswith("TO:"):
            to = line[3:].strip()
        elif line.upper().startswith("SUBJECT:"):
            subject = line[8:].strip()
        elif line.upper().startswith("BODY:"):
            body_start = i + 1
            break

    if body_start > 0:
        body = "\n".join(lines[body_start:]).strip()

    return to, subject, body


# ── Review Commands ───────────────────────────────────────────────

@auth_required
async def cmd_properties(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    props = await db.list_properties()
    if not props:
        await update.message.reply_text(
            "No properties being monitored.\n\nUse /addproperty `<name>` to add one.",
            parse_mode="Markdown",
        )
        return

    lines = [f"*Monitored Properties ({len(props)})*\n"]
    for i, p in enumerate(props, 1):
        lines.append(
            f"{i}. *{p['name']}*\n"
            f"   {p['address']}\n"
            f"   Place ID: `{p['place_id']}`"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_addproperty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /addproperty `<property name or address>`\n\n"
            "Example: /addproperty Sunset Apartments 123 Oak St",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"Searching for: {query}...")

    try:
        results = await reviews.search_places(query)
    except RuntimeError as e:
        await update.message.reply_text(str(e))
        return
    except Exception as e:
        logger.exception("Place search failed")
        await update.message.reply_text(f"Search error: {e}")
        return

    if not results:
        await update.message.reply_text("No places found. Try a more specific name or address.")
        return

    # Store search results in user context for selection
    context.user_data["place_search_results"] = results

    lines = ["*Search Results* — reply with the number to add:\n"]
    for i, r in enumerate(results, 1):
        rating_str = f" ({r['rating']}/5, {r['review_count']} reviews)" if r["rating"] else ""
        lines.append(f"{i}. *{r['name']}*{rating_str}\n   {r['address']}\n   `{r['place_id']}`")

    lines.append("\nReply with /pick `<number>` to add, or /addproperty to search again.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick a place from search results to add as a monitored property."""
    results = context.user_data.get("place_search_results")
    if not results:
        await update.message.reply_text("No search results. Use /addproperty first.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /pick `<number>`")
        return

    try:
        idx = int(context.args[0]) - 1
        if idx < 0 or idx >= len(results):
            raise ValueError()
    except ValueError:
        await update.message.reply_text(f"Pick a number between 1 and {len(results)}")
        return

    place = results[idx]

    # Check if already monitored
    existing = await db.list_properties()
    if any(p["place_id"] == place["place_id"] for p in existing):
        await update.message.reply_text(f"*{place['name']}* is already being monitored.", parse_mode="Markdown")
        return

    try:
        await db.add_property(place["name"], place["place_id"], place["address"])
        # Seed existing reviews so we only alert on truly new ones
        seeded = await reviews.seed_existing_reviews(place["place_id"])

        await db.log_audit("add_property", "config",
                           f"Added property: {place['name']} ({place['place_id']})",
                           user_id=update.effective_user.id, approved=True)

        await update.message.reply_text(
            f"Added *{place['name']}*\n"
            f"{place['address']}\n\n"
            f"Seeded {seeded} existing reviews (won't trigger alerts).\n"
            f"New reviews will be checked every {config.REVIEW_POLL_INTERVAL_MINUTES} minutes.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Failed to add property")
        await update.message.reply_text(f"Error adding property: {e}")

    context.user_data.pop("place_search_results", None)


@auth_required
async def cmd_removeproperty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Usage: /removeproperty `<place_id>`\n\nGet place IDs from /properties",
            parse_mode="Markdown",
        )
        return

    place_id = context.args[0]
    removed = await db.remove_property(place_id)
    if removed:
        await db.log_audit("remove_property", "config", f"Removed property: {place_id}",
                           user_id=update.effective_user.id, approved=True)
        await update.message.reply_text("Property removed.")
    else:
        await update.message.reply_text("Property not found. Check /properties for IDs.")


@auth_required
async def cmd_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent reviews across all monitored properties."""
    recent = await db.get_recent_reviews(limit=10)
    if not recent:
        await update.message.reply_text("No reviews recorded yet. Add properties with /addproperty first.")
        return

    # Get property names for display
    props = await db.list_properties()
    name_map = {p["place_id"]: p["name"] for p in props}

    lines = ["*Recent Reviews*\n"]
    for r in recent:
        prop_name = name_map.get(r["place_id"], r["place_id"][:12])
        stars = reviews.rating_stars(r["rating"])
        text_preview = (r["text"][:100] + "...") if len(r["text"]) > 100 else r["text"]
        lines.append(
            f"{stars} — *{prop_name}*\n"
            f"   by {r['author']}\n"
            f"   {text_preview}"
        )

    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_checkreviews(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger a review check across all properties."""
    props = await db.list_properties()
    if not props:
        await update.message.reply_text("No properties being monitored. Use /addproperty first.")
        return

    await update.message.reply_text(f"Checking {len(props)} properties for new reviews...")

    try:
        alerts = await reviews.poll_all_properties()
    except Exception as e:
        logger.exception("Manual review check failed")
        await update.message.reply_text(f"Check failed: {e}")
        return

    if not alerts:
        await update.message.reply_text("No new reviews found.")
        return

    for alert in alerts:
        stars = reviews.rating_stars(alert["rating"])
        text = alert["text"][:500] if alert["text"] else "(no text)"
        msg = (
            f"*New Review* — {alert['property_name']}\n\n"
            f"{stars}\n"
            f"by {alert['author']} ({alert['relative_time']})\n\n"
            f"{text}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    await db.log_audit("manual_review_check", "read",
                       f"Found {len(alerts)} new reviews across {len(props)} properties",
                       user_id=update.effective_user.id, approved=True)


# ── Background Review Polling ─────────────────────────────────────

async def _background_review_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called by APScheduler on interval. Checks all properties and sends alerts."""
    try:
        alerts = await reviews.poll_all_properties()
    except Exception as e:
        logger.error(f"Background review poll failed: {e}")
        return

    if not alerts:
        return

    chat_id = config.TELEGRAM_AUTHORIZED_USER_ID
    for alert in alerts:
        stars = reviews.rating_stars(alert["rating"])
        text = alert["text"][:500] if alert["text"] else "(no text)"
        msg = (
            f"*New Review Alert* — {alert['property_name']}\n\n"
            f"{stars}\n"
            f"by {alert['author']} ({alert['relative_time']})\n\n"
            f"{text}"
        )
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send review alert: {e}")

    await db.log_audit("auto_review_check", "read",
                       f"Background poll: {len(alerts)} new reviews found")


# ── Approval Flow ─────────────────────────────────────────────────

async def request_approval(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action_type: str,
    category: ActionCategory,
    summary: str,
    detail: str,
    payload: dict,
) -> None:
    """Present an outbound action for user approval with inline buttons."""
    action_id = await db.create_pending_action(action_type, payload)

    await db.log_audit(
        action_type=f"{action_type}_pending",
        category=category.value,
        summary=summary,
        detail=detail,
        user_id=update.effective_user.id,
    )

    buttons = [
        [
            InlineKeyboardButton("Approve", callback_data=f"approve:{action_id}"),
            InlineKeyboardButton("Reject", callback_data=f"reject:{action_id}"),
        ]
    ]

    if category == ActionCategory.DANGEROUS:
        preview = f"**DANGEROUS ACTION**\n\n*{summary}*\n\n{detail}\n\n_This requires confirmation._"
    else:
        preview = f"*Draft — {summary}*\n\n{detail}\n\n_Approve to send, Reject to discard._"

    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process Approve/Reject button presses."""
    query = update.callback_query
    await query.answer()

    user = query.from_user
    if not security.is_authorized(user.id):
        await query.edit_message_text("Unauthorized.")
        return

    data = query.data
    if ":" not in data:
        return

    decision, action_id_str = data.split(":", 1)
    try:
        action_id = int(action_id_str)
    except ValueError:
        return

    if decision == "approve":
        if not security.check_rate_limit():
            await query.edit_message_text("Rate limit exceeded. Try again later.")
            await db.log_audit("rate_limit", "security", "Outbound blocked by rate limit",
                               user_id=user.id, approved=False)
            return

        action = await db.resolve_pending_action(action_id, "approved")
        if action is None:
            await query.edit_message_text("Action not found or already resolved.")
            return

        security.record_outbound()
        await db.log_audit(
            action_type=f"{action['action_type']}_approved",
            category="write",
            summary=f"Approved: {action['action_type']} #{action_id}",
            detail=json.dumps(action["payload"]),
            user_id=user.id,
            approved=True,
        )

        result = await execute_approved_action(action)
        await query.edit_message_text(f"Approved and executed.\n\n{result}")

    elif decision == "reject":
        action = await db.resolve_pending_action(action_id, "rejected")
        if action is None:
            await query.edit_message_text("Action not found or already resolved.")
            return

        await db.log_audit(
            action_type=f"{action['action_type']}_rejected",
            category="write",
            summary=f"Rejected: {action['action_type']} #{action_id}",
            user_id=user.id,
            approved=False,
        )
        await query.edit_message_text("Rejected. Nothing was sent.")


async def execute_approved_action(action: dict) -> str:
    """
    Dispatch approved actions to their handlers.
    New action types (send_email, send_imessage, etc.) get added here in later phases.
    """
    action_type = action["action_type"]
    payload = action["payload"]

    if action_type == "send_email":
        try:
            result = gmail_module.send_email(
                to=payload["to"],
                subject=payload["subject"],
                body=payload["body"],
            )
            return f"Email sent to {payload['to']}.\nMessage ID: {result['message_id']}"
        except Exception as e:
            return f"Send failed: {e}"
    elif action_type == "send_imessage":
        return "iMessage sending not yet implemented (Phase 5)."
    elif action_type == "send_whatsapp":
        return "WhatsApp sending not yet implemented (Phase 6)."
    else:
        return f"Unknown action type: {action_type}"


# ── Free-Text Chat ────────────────────────────────────────────────

@auth_required
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route free-text messages to the active LLM."""
    user_text = update.message.text
    if not user_text:
        return

    await db.log_audit("chat", "read", f"User message ({len(user_text)} chars)",
                       user_id=update.effective_user.id, approved=True)

    history = await db.get_recent_conversations(limit=20)

    try:
        response = await llm.chat(user_text, history)
    except Exception as e:
        logger.exception("LLM call failed")
        await update.message.reply_text(f"LLM error: {type(e).__name__}: {e}")
        return

    max_len = 4000
    if len(response) > max_len:
        for i in range(0, len(response), max_len):
            await update.message.reply_text(response[i : i + max_len])
    else:
        await update.message.reply_text(response)


# ── Bot Setup ─────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("addaccount", cmd_addaccount))
    app.add_handler(CommandHandler("useaccount", cmd_useaccount))
    app.add_handler(CommandHandler("emails", cmd_emails))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("draft", cmd_draft))
    app.add_handler(CommandHandler("aidraft", cmd_aidraft))
    app.add_handler(CommandHandler("properties", cmd_properties))
    app.add_handler(CommandHandler("addproperty", cmd_addproperty))
    app.add_handler(CommandHandler("pick", cmd_pick))
    app.add_handler(CommandHandler("removeproperty", cmd_removeproperty))
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    app.add_handler(CommandHandler("checkreviews", cmd_checkreviews))
    app.add_handler(CallbackQueryHandler(handle_approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Background review polling
    if config.GOOGLE_PLACES_API_KEY:
        interval = config.REVIEW_POLL_INTERVAL_MINUTES * 60
        app.job_queue.run_repeating(
            _background_review_poll,
            interval=interval,
            first=30,  # first check 30s after startup
            name="review_poll",
        )

    return app

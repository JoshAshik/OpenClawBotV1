"""
Telegram bot — the primary interface for Clawdbot.

Natural language is the main interface — the user types plain English and
the LLM agent decides which tools to call (search emails, read PDFs,
check reviews, etc.). Slash commands still work as shortcuts.

Outbound actions (send email, send message) go through the approval flow:
  1. Bot presents a preview with Approve / Reject buttons
  2. Only on Approve does the action execute
  3. Everything is audit-logged
"""

import asyncio
import io
import json
import logging
import time
import uuid
from datetime import datetime, time as dtime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import agent_tools
import alerts
import config
import db
import digest
import gmail_module
import indexer
import llm
import ops
import reminders
import reviews
import waiting_on
import search_index
import security
from security import ActionCategory, auth_required
from telegram_format import markdown_to_telegram_html, tg_retry

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096

# Friendly labels for the live status message while tools run.
_TOOL_LABELS = {
    "search_everything": "🔍 Searching everything",
    "search_emails": "📧 Searching email",
    "list_recent_emails": "📥 Checking the inbox",
    "read_email": "📖 Reading an email",
    "read_pdf_from_email": "📄 Reading a PDF attachment",
    "search_pdfs": "📄 Searching PDFs",
    "draft_email": "✍️ Drafting an email",
    "reply_email": "✍️ Drafting a reply",
    "check_reviews": "⭐ Checking reviews",
    "list_properties": "🏠 Listing properties",
    "search_onedrive": "📁 Searching OneDrive",
    "read_onedrive_file": "📁 Reading a OneDrive file",
    "read_indexed_document": "📖 Reading a document",
    "get_index_stats": "📊 Checking the index",
    "find_contact": "👤 Looking up a contact",
    "send_imessage": "💬 Drafting a text message",
    "get_conversation": "💬 Pulling up the conversation",
    "list_waiting_on": "⏳ Checking what's unanswered",
    "set_reminder": "⏰ Setting a reminder",
    "list_reminders": "⏰ Checking reminders",
    "cancel_reminder": "⏰ Cancelling a reminder",
    "remember": "💾 Saving to memory",
    "generate_image": "🎨 Generating an image",
    "web_search": "🌐 Searching the web",
}


def _tool_label(name: str, args: dict) -> str:
    label = _TOOL_LABELS.get(name, f"⚙️ {name}")
    query = args.get("query") if isinstance(args, dict) else None
    if query:
        label += f': "{str(query)[:60]}"'
    return label


class TurnTimeout(Exception):
    """An agent turn exceeded TURN_TIMEOUT_SECONDS and was cancelled."""


# One lock per user: a second message while a turn runs waits its turn instead of
# running concurrently (and the user is told so).
_TURN_LOCKS: dict[int, asyncio.Lock] = {}
BUSY_TEXT = "⏳ Still working on your last message — I'll take this one next."


class StatusMessage:
    """
    One Telegram message that lives through an agent turn:
    starts as "Thinking…", shows each tool step, streams the answer text,
    and finally becomes the formatted answer (ChatGPT-app feel).
    """

    MIN_EDIT_INTERVAL = 1.5  # Telegram edit rate limit safety

    def __init__(self, bot, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._message = None
        self._steps: list[str] = []
        self._stream_text = ""
        self._last_edit = 0.0
        self._last_rendered = ""

    async def start(self) -> None:
        try:
            await self._bot.send_chat_action(chat_id=self._chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        self._message = await tg_retry(
            lambda: self._bot.send_message(chat_id=self._chat_id, text="🤔 Thinking…"))

    # -- callbacks invoked by llm.py --

    async def on_tool(self, name: str, args: dict) -> None:
        if self._steps:
            self._steps[-1] = self._steps[-1].replace("⏳ ", "✅ ", 1)
        self._steps.append("⏳ " + _tool_label(name, args))
        self._stream_text = ""  # narration before a tool call is superseded
        await self._render(force=True)

    async def on_text(self, text: str) -> None:
        self._stream_text = text
        await self._render()

    # -- rendering --

    def _compose_progress(self) -> str:
        parts = list(self._steps)
        snippet = self._stream_text.strip()
        if snippet:
            if len(snippet) > 3000:
                snippet = snippet[-3000:]
            parts.append(snippet + " ▌")
        elif not parts:
            parts.append("🤔 Thinking…")
        return "\n".join(parts)[:TELEGRAM_MAX_LEN - 10]

    async def _render(self, force: bool = False) -> None:
        if self._message is None:
            return
        now = time.monotonic()
        if not force and now - self._last_edit < self.MIN_EDIT_INTERVAL:
            return
        text = self._compose_progress()
        if text == self._last_rendered:
            return
        self._last_edit = now
        self._last_rendered = text
        try:
            await tg_retry(lambda: self._message.edit_text(text))
        except BadRequest:
            pass  # a different BadRequest (e.g. too long) — harmless for a progress edit
        except Exception:
            logger.debug("status edit failed", exc_info=True)

    async def _edit_final(self, chunk: str) -> None:
        """Edit the status message into the final answer, HTML first, plain fallback."""
        try:
            await tg_retry(lambda: self._message.edit_text(
                markdown_to_telegram_html(chunk), parse_mode="HTML",
                disable_web_page_preview=True,
            ))
        except BadRequest:
            try:
                await tg_retry(lambda: self._message.edit_text(chunk))
            except BadRequest:
                pass

    async def finish(self, final_text: str) -> None:
        if self._message is None:
            return
        final_text = final_text.strip() or "(no reply)"
        chunks = [final_text[i:i + 4000] for i in range(0, len(final_text), 4000)]
        await self._edit_final(chunks[0])
        for chunk in chunks[1:]:
            try:
                await tg_retry(lambda: self._bot.send_message(
                    chat_id=self._chat_id, text=markdown_to_telegram_html(chunk),
                    parse_mode="HTML", disable_web_page_preview=True,
                ))
            except BadRequest:
                await tg_retry(lambda: self._bot.send_message(chat_id=self._chat_id, text=chunk))

    async def fail(self, error: Exception, retry_key: str) -> None:
        if self._message is None:
            return
        buttons = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔄 Retry", callback_data=f"retry:{retry_key}")]]
        )
        if isinstance(error, TurnTimeout):
            msg = (f"That took too long (over {int(config.TURN_TIMEOUT_SECONDS)}s) and was stopped. "
                   "Nothing was sent.")
        else:
            msg = f"Something went wrong ({type(error).__name__}). Nothing was sent."
        try:
            await tg_retry(lambda: self._message.edit_text(msg, reply_markup=buttons))
        except Exception:
            logger.debug("error render failed", exc_info=True)


# Last user prompts, keyed for the Retry button.
_RETRY_CACHE: dict[str, str] = {}

# Active "Edit draft" sessions: user_id -> {action_id, chat_id, message_id}.
# While set, the user's next free-text message is edit instructions, not chat.
_EDIT_STATE: dict[int, dict] = {}


def _action_summary(action_type: str, payload: dict) -> str:
    if action_type == "reply_email":
        return f"Reply to {payload.get('to', '?')}"
    if action_type == "send_email":
        return f"Email to {payload.get('to', '?')}"
    if action_type == "send_imessage":
        return f"Text to {payload.get('to_name') or payload.get('to', '?')}"
    return action_type


async def _handle_draft_edit(context: ContextTypes.DEFAULT_TYPE, user_id: int, instructions: str) -> None:
    """Apply freeform edit instructions to the pending draft and refresh its card."""
    state = _EDIT_STATE.get(user_id)
    if state is None:
        return
    chat_id = state["chat_id"]

    action = await db.get_pending_action(state["action_id"])
    if action is None:
        _EDIT_STATE.pop(user_id, None)
        await context.bot.send_message(chat_id, "That draft was already resolved — nothing to edit.")
        return

    note = await context.bot.send_message(chat_id, "✏️ Revising the draft…")
    try:
        new_payload = await llm.revise_draft(action["action_type"], action["payload"], instructions)
        await db.update_pending_action_payload(action["id"], new_payload)
    except Exception as e:
        logger.exception("Draft revision failed")
        await note.edit_text(f"Couldn't revise the draft ({type(e).__name__}). Try again, or /cancel.")
        return

    card = _card_text(_action_summary(action["action_type"], new_payload),
                      action["action_type"], new_payload, revised=True)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=state["message_id"], text=card,
            parse_mode="Markdown", reply_markup=_approval_buttons(action["id"]),
        )
    except BadRequest:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=state["message_id"], text=card,
                reply_markup=_approval_buttons(action["id"]),
            )
        except BadRequest:
            pass

    _EDIT_STATE.pop(user_id, None)
    try:
        await note.edit_text("Updated the draft ⤴ — Approve, Edit again, or Reject.")
    except BadRequest:
        pass


async def _run_agent_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, user_text: str,
                          incoming_voice: bool = False) -> None:
    """Shared free-text pipeline, one turn at a time per user (see _TURN_LOCKS).
    incoming_voice marks a transcribed voice note, which /voice auto answers by voice."""
    lock = _TURN_LOCKS.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        try:
            await tg_retry(lambda: context.bot.send_message(chat_id, BUSY_TEXT))
        except Exception:
            pass
    async with lock:
        await _run_agent_turn_locked(context, chat_id, user_id, user_text, incoming_voice)


async def _send_files(bot, chat_id: int, files: list[dict]) -> None:
    """Send generated attachments (today: photos from generate_image) after the answer."""
    for f in files or []:
        data = f.get("bytes")
        if not data:
            continue
        caption = (f.get("caption") or "")[:1024] or None
        name = f.get("filename") or "image.png"

        def _buf():  # a fresh buffer per attempt, so a flood-control retry doesn't send an empty file
            b = io.BytesIO(data)
            b.name = name
            return b

        try:
            if f.get("kind", "photo") == "photo":
                await tg_retry(lambda: bot.send_photo(chat_id=chat_id, photo=_buf(), caption=caption))
            else:
                await tg_retry(lambda: bot.send_document(chat_id=chat_id, document=_buf(), caption=caption))
        except Exception:
            logger.exception("Sending a generated file failed")


async def _send_voice_reply(bot, chat_id: int, text: str) -> None:
    """Speak the final answer as a Telegram voice note (after the text, never instead of it)."""
    try:
        audio = await llm.synthesize_speech(text)
        if not audio:
            return
        def _buf():
            b = io.BytesIO(audio)
            b.name = "reply.ogg"
            return b

        await tg_retry(lambda: bot.send_voice(chat_id=chat_id, voice=_buf()))
    except Exception:
        logger.exception("Voice reply failed")


async def _run_agent_turn_locked(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int,
                                 user_text: str, incoming_voice: bool = False) -> None:
    """status message → agent (bounded by TURN_TIMEOUT_SECONDS) → final render → files →
    voice note (per /voice) → approvals."""
    await db.log_audit("chat", "read", f"User message ({len(user_text)} chars)",
                       user_id=user_id, approved=True)

    status = StatusMessage(context.bot, chat_id)
    await status.start()

    try:
        result = await asyncio.wait_for(
            llm.agent_chat(user_text, progress=status), timeout=config.TURN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(f"Agent turn timed out after {config.TURN_TIMEOUT_SECONDS}s")
        key = uuid.uuid4().hex[:12]
        _RETRY_CACHE[key] = user_text
        await status.fail(TurnTimeout(), key)
        return
    except Exception as e:
        logger.exception("LLM agent call failed")
        key = uuid.uuid4().hex[:12]
        _RETRY_CACHE[key] = user_text
        while len(_RETRY_CACHE) > 20:
            _RETRY_CACHE.pop(next(iter(_RETRY_CACHE)))
        await status.fail(e, key)
        return

    await status.finish(result["text"])
    await _send_files(context.bot, chat_id, result.get("files") or [])

    if result.get("text") and result["text"] != llm.RAN_OUT_TEXT:
        try:
            mode = await llm.voice_mode()
        except Exception:
            mode = "auto"
        if llm.should_speak(mode, incoming_voice, llm.wants_spoken_reply(user_text)):
            await _send_voice_reply(context.bot, chat_id, result["text"])

    for action in result.get("actions", []):
        await request_approval(
            context, chat_id, user_id,
            action_type=action["type"],
            category=ActionCategory.WRITE,
            summary=action["summary"],
            payload=action["payload"],
        )


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
        f"Model: {config.OPENAI_MODEL}\n"
        f"Session: {'unlocked' if security.is_session_unlocked() else 'locked'}\n\n"
        "Type /help for commands, or just send me a message."
    )


@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Clawdbot — Just talk to me!*\n\n"
        "Type naturally — or send a *voice note*, *photo*, or *PDF*:\n"
        "  \"Show me my latest emails\"\n"
        "  \"Find the lease for 123 Oak St\"\n"
        "  \"Any new reviews on my properties?\"\n"
        "  \"Reply to John's email saying I'll be there Monday\"\n\n"
        "I'll search emails, read PDFs, check reviews, browse the web, make images "
        "(\"draw a flyer for the open house\"), and more — automatically. Say \"say it\" "
        "to hear an answer out loud. Sending anything always requires your approval, "
        "and every draft card has an ✏️ Edit button for revisions.\n\n"
        "*Conversation*\n"
        "/new — Start a fresh conversation\n"
        "/memories — Long-term memories (saved + auto-learned) and recent conversation summaries\n"
        "/summaries — Summaries of earlier conversations\n"
        "/reminders — Pending reminders (\"remind me Friday to…\" sets one)\n"
        "/waiting — Sent emails/texts with no reply, with Nudge buttons\n"
        "/alerts [on|off] — Urgent inbound alerts status, watch-list, last alerts\n"
        "/forget `<id>` — Delete a memory\n"
        "/voice [on|off|auto] — Spoken replies: always, never, or only to voice notes\n"
        "/cancel — Cancel an in-progress draft edit\n\n"
        "*Email*\n"
        "/signature `<account> <text>` — Set a per-account signature\n\n"
        "*Shortcut commands* (optional)\n"
        "/accounts — List connected Gmail accounts\n"
        "/addaccount `<label>` — Connect a Gmail account\n"
        "/useaccount `<label>` — Switch active account\n"
        "/addproperty `<name>` — Add a property to monitor\n"
        "/connectonedrive — Connect personal OneDrive\n"
        "/reindex — Trigger full reindex now\n\n"
        "*Admin*\n"
        "/pin `<code>` — Unlock session\n"
        "/lock — Lock session\n"
        "/status — Current state\n"
        "/usage — OpenAI spend today / month, by purpose and model\n"
        "/doctor — Health checks (tokens, OpenAI, data sources, jobs)\n"
        "/update — Pull the latest code and restart\n"
        "/restart — Restart the bot\n"
        "/audit — Recent audit log\n"
        "/help — This message",
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
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rate_ok = security.check_rate_limit()
    used = len(security._outbound_timestamps)
    last_index = ops.fmt_ts(await db.get_setting(ops.LAST_INDEX_KEY))
    last_backfill = ops.fmt_ts(await db.get_setting(ops.LAST_BACKFILL_KEY))
    last_backup = ops.fmt_ts(await db.get_setting(ops.LAST_BACKUP_KEY))
    started = ops.STARTED_AT.astimezone().strftime("%Y-%m-%d %H:%M")
    try:
        today = await db.usage_summary(ops.day_start_iso())
        spend = f"${today['cost']:.2f} ({today['calls']} calls)"
    except Exception:
        spend = "n/a"
    await update.message.reply_text(
        f"*Clawdbot Status*\n\n"
        f"Model: `{config.OPENAI_MODEL}` (fast: `{config.OPENAI_FAST_MODEL}`, "
        f"API: {config.OPENAI_API_MODE}, reasoning: {config.OPENAI_REASONING_EFFORT})\n"
        f"Spend today: {spend} — /usage for the breakdown\n"
        f"Uptime: {ops.uptime_str()} (since {started})\n"
        f"Session: {'unlocked' if security.is_session_unlocked() else 'LOCKED'}\n"
        f"Outbound this hour: {used}/{config.RATE_LIMIT_PER_HOUR}\n"
        f"Rate limit: {'OK' if rate_ok else 'EXCEEDED'}\n\n"
        f"Last index: {last_index}\n"
        f"Last backfill: {last_backfill}\n"
        f"Last backup: {last_backup}\n\n"
        f"/doctor for full health checks",
        parse_mode="Markdown",
    )


@auth_required
async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today = await db.usage_summary(ops.day_start_iso())
    month = await db.usage_summary(ops.month_start_iso())
    top = await db.top_turns(ops.month_start_iso(), limit=5)
    await update.message.reply_text(ops.format_usage(today, month, top)[:TELEGRAM_MAX_LEN])


# ── Ops: doctor / update / restart ───────────────────────────────

@auth_required
async def cmd_doctor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("🩺 Running checks…")
    checks = await ops.run_doctor(context.job_queue)
    await msg.edit_text(ops.format_doctor(checks)[:TELEGRAM_MAX_LEN])


def _ops_confirm_buttons(op: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, go ahead", callback_data=f"ops_{op}:confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"ops_{op}:cancel"),
    ]])


@auth_required
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Update Clawdbot? This runs `git pull --ff-only` and `pip install -r requirements.txt`, "
        "then restarts the bot (the supervisor brings it back up).",
        parse_mode="Markdown",
        reply_markup=_ops_confirm_buttons("update"),
    )


@auth_required
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Restart Clawdbot now? The bot exits and the supervisor (launchd) starts it again.",
        reply_markup=_ops_confirm_buttons("restart"),
    )


async def handle_ops_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm/cancel buttons for /update and /restart. Not outbound — no pending_actions."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not security.is_authorized(user.id):
        await query.edit_message_text("Unauthorized.")
        return
    if not security.is_session_unlocked():
        await query.edit_message_text("Session locked. Send your PIN to unlock.")
        return

    op, _, choice = query.data.partition(":")
    op = op.removeprefix("ops_")
    if choice != "confirm":
        await query.edit_message_text(f"{op.capitalize()} cancelled.")
        return

    if op == "update":
        await query.edit_message_text("⬇️ Updating… (git pull, pip install)")
        ok, report = await ops.run_update()
        await db.log_audit("update", "config", "Update " + ("applied" if ok else "failed"),
                           detail=report, user_id=user.id, approved=ok)
        if not ok:
            await query.edit_message_text(f"❌ Update failed.\n\n{report}"[:TELEGRAM_MAX_LEN])
            return
        await query.edit_message_text(f"✅ Updated. Restarting now…\n\n{report}"[:TELEGRAM_MAX_LEN])
        logger.info("Update applied via /update; restarting")
        ops.request_exit(context.application)

    elif op == "restart":
        await db.log_audit("restart", "config", "Restart requested via /restart",
                           user_id=user.id, approved=True)
        await query.edit_message_text("🔄 Restarting… I'll say hello when I'm back.")
        logger.info("Restart requested via /restart")
        ops.request_exit(context.application)

    else:
        await query.edit_message_text(f"Unknown operation: {op}")


async def _post_init(app: Application) -> None:
    """Startup ping so crash loops are visible from the phone."""
    import platform
    try:
        await app.bot.send_message(
            config.TELEGRAM_AUTHORIZED_USER_ID,
            f"🟢 Clawdbot started on {platform.node()} ({platform.system()}) · "
            f"model {config.OPENAI_MODEL}",
        )
    except Exception as e:
        logger.warning(f"Startup ping failed: {e}")


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

    await request_approval(
        context, update.effective_chat.id, update.effective_user.id,
        action_type="send_email",
        category=ActionCategory.WRITE,
        summary=f"Email to {to}",
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

    await request_approval(
        context, update.effective_chat.id, update.effective_user.id,
        action_type="send_email",
        category=ActionCategory.WRITE,
        summary=f"AI-drafted email to {to}",
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


# ── Attachment & PDF Commands ─────────────────────────────────────

@auth_required
async def cmd_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List attachments on a specific email."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /attachments `<email_id>`\nGet IDs from /emails or /search",
            parse_mode="Markdown",
        )
        return

    msg_id = context.args[0]
    try:
        email = gmail_module.read_email(msg_id)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    attachments = email.get("attachments", [])
    if not attachments:
        await update.message.reply_text(
            f"*{email['subject']}*\n\nNo attachments on this email.",
            parse_mode="Markdown",
        )
        return

    lines = [f"*Attachments on:* {email['subject']}\n"]
    for i, att in enumerate(attachments, 1):
        size_kb = att["size"] / 1024
        is_pdf = att["mime_type"] == "application/pdf" or att["filename"].lower().endswith(".pdf")
        pdf_tag = " [PDF]" if is_pdf else ""
        lines.append(f"{i}. `{att['filename']}`{pdf_tag} ({size_kb:.1f} KB)")

    if any(a["filename"].lower().endswith(".pdf") for a in attachments):
        lines.append(f"\nUse /readpdf `{msg_id}` to extract PDF text.")

    await db.log_audit("list_attachments", "read",
                       f"Listed {len(attachments)} attachments on {msg_id}",
                       user_id=update.effective_user.id, approved=True)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_readpdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Extract text from all PDF attachments on an email."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /readpdf `<email_id>`\n\n"
            "Extracts text from all PDF attachments on the email.\n"
            "Get email IDs from /emails or /search",
            parse_mode="Markdown",
        )
        return

    msg_id = context.args[0]
    await update.message.reply_text("Extracting PDF text...")

    try:
        result = gmail_module.get_email_with_pdf_text(msg_id)
    except Exception as e:
        logger.exception("PDF extraction failed")
        await update.message.reply_text(f"Error: {e}")
        return

    pdf_texts = result.get("pdf_texts", [])
    if not pdf_texts:
        await update.message.reply_text(
            f"*{result['subject']}*\n\nNo PDF attachments found on this email.",
            parse_mode="Markdown",
        )
        return

    await db.log_audit("read_pdf", "read",
                       f"Extracted {len(pdf_texts)} PDFs from {msg_id}",
                       user_id=update.effective_user.id, approved=True)

    for pdf in pdf_texts:
        header = f"*PDF: {pdf['filename']}*\n\n"
        text = pdf["text"]

        # Split into chunks if needed (Telegram max 4096)
        full = header + text
        if len(full) > 4000:
            await update.message.reply_text(header + text[:3900] + "\n\n_(truncated)_", parse_mode="Markdown")
        else:
            await update.message.reply_text(full, parse_mode="Markdown")


@auth_required
async def cmd_searchpdfs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search Gmail for emails with PDF attachments and extract their text."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /searchpdfs `<query>`\n\n"
            "Examples:\n"
            "  /searchpdfs lease agreement\n"
            "  /searchpdfs from:tenant invoice\n"
            "  /searchpdfs maintenance report 2026",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"Searching for PDFs matching: {query}...")

    try:
        results = gmail_module.search_pdfs(query, max_results=5)
    except Exception as e:
        logger.exception("PDF search failed")
        await update.message.reply_text(f"Search error: {e}")
        return

    if not results:
        await update.message.reply_text("No PDF attachments found matching that query.")
        return

    await db.log_audit("search_pdfs", "read",
                       f"PDF search: {query} ({len(results)} results)",
                       user_id=update.effective_user.id, approved=True)

    for r in results:
        text_preview = r["text"][:1500]
        if len(r["text"]) > 1500:
            text_preview += "\n\n_(truncated — use /readpdf for full text)_"

        msg = (
            f"*{r['filename']}*\n"
            f"From: {r['email_from']}\n"
            f"Subject: {r['email_subject']}\n"
            f"Date: {r['email_date']}\n"
            f"Email ID: `{r['email_id']}`\n\n"
            f"{text_preview}"
        )

        if len(msg) > 4000:
            msg = msg[:4000] + "\n..."
        await update.message.reply_text(msg, parse_mode="Markdown")


# ── Unified Search Commands ───────────────────────────────────────

SOURCE_ICONS = {
    "gmail": "Email",
    "onedrive": "OneDrive",
    "imessage": "iMessage",
    "whatsapp": "WhatsApp",
}


@auth_required
async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search across all indexed sources."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /find `<query>`\n\n"
            "Examples:\n"
            "  /find lease agreement unit 4B\n"
            "  /find plumbing issue 123 Oak\n"
            "  /find invoice from contractor\n\n"
            "Searches emails, PDFs, OneDrive files, iMessages, and WhatsApp.\n"
            "Use /findlive for real-time search (slower).",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args)

    try:
        results = await search_index.search(query, limit=10)
    except Exception as e:
        logger.exception("Search failed")
        await update.message.reply_text(f"Search error: {e}")
        return

    if not results:
        await update.message.reply_text(
            f"No results for: {query}\n\nTry /findlive for a live search, or /reindex to update the index."
        )
        return

    await db.log_audit("search", "read", f"Search: {query} ({len(results)} results)",
                       user_id=update.effective_user.id, approved=True)

    lines = [f"*Search: {query}* ({len(results)} results)\n"]
    for i, r in enumerate(results, 1):
        source_label = SOURCE_ICONS.get(r["source"], r["source"])
        meta = r.get("metadata", {})
        date_str = meta.get("date", meta.get("modified", ""))[:10]
        extra = ""
        if meta.get("from"):
            extra = f"\n   From: {meta['from'][:40]}"
        elif meta.get("contact"):
            extra = f"\n   Contact: {meta['contact']}"
        elif meta.get("filename"):
            extra = f"\n   File: {meta['filename']}"

        lines.append(
            f"{i}. [{source_label}] *{r['title'][:60]}*{extra}\n"
            f"   {r['snippet']}\n"
            f"   `{r['source']}:{r['source_id'][:20]}`"
        )
        if date_str:
            lines[-1] += f" | {date_str}"

    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_findlive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Live search across all sources (hits APIs in real time)."""
    if not context.args:
        await update.message.reply_text("Usage: /findlive `<query>`", parse_mode="Markdown")
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"Searching live across all sources for: {query}...")

    results = []

    # Gmail live search (all accounts)
    accounts = gmail_module.get_connected_accounts()
    for acc in accounts:
        try:
            emails = gmail_module.search_emails(query, max_results=5, account=acc["label"])
            for em in emails:
                results.append({
                    "source": f"Gmail ({acc['label']})",
                    "title": em["subject"],
                    "snippet": em["snippet"][:200],
                    "id": em["id"],
                    "date": em["date"],
                })
        except Exception as e:
            logger.warning(f"Live Gmail search failed ({acc['label']}): {e}")

    # OneDrive live search
    try:
        from onedrive_module import search_files, is_connected
        if is_connected():
            files = search_files(query)
            for f in files[:5]:
                results.append({
                    "source": "OneDrive",
                    "title": f["name"],
                    "snippet": f["path"],
                    "id": f["id"],
                    "date": f.get("modified", ""),
                })
    except Exception as e:
        logger.warning(f"Live OneDrive search failed: {e}")

    # iMessage live search
    try:
        from imessage_module import search_messages, is_available
        if is_available():
            msgs = search_messages(query, limit=5)
            for m in msgs:
                results.append({
                    "source": "iMessage",
                    "title": f"Message with {m['contact']}",
                    "snippet": m["text"][:200],
                    "id": str(m["rowid"]),
                    "date": m["date"],
                })
    except Exception as e:
        logger.warning(f"Live iMessage search failed: {e}")

    # WhatsApp live search
    try:
        from whatsapp_module import search_messages as wa_search, is_available as wa_available
        if wa_available():
            msgs = wa_search(query, limit=5)
            for m in msgs:
                results.append({
                    "source": "WhatsApp",
                    "title": f"Chat with {m['contact']}",
                    "snippet": m["text"][:200],
                    "id": m["id"],
                    "date": m["date"],
                })
    except Exception as e:
        logger.warning(f"Live WhatsApp search failed: {e}")

    if not results:
        await update.message.reply_text(f"No live results for: {query}")
        return

    await db.log_audit("search_live", "read", f"Live search: {query} ({len(results)} results)",
                       user_id=update.effective_user.id, approved=True)

    lines = [f"*Live Search: {query}* ({len(results)} results)\n"]
    for i, r in enumerate(results, 1):
        date_str = r.get("date", "")[:10]
        lines.append(
            f"{i}. [{r['source']}] *{r['title'][:60]}*\n"
            f"   {r['snippet'][:150]}"
        )
        if date_str:
            lines[-1] += f"\n   {date_str}"

    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_indexstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = await search_index.get_index_stats()
    lines = ["*Search Index Stats*\n"]
    for source, count in sorted(stats.items()):
        if source == "total":
            continue
        icon = SOURCE_ICONS.get(source, source)
        lines.append(f"  {icon}: {count} items")
    lines.append(f"\n*Total: {stats.get('total', 0)} items*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_reindex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Starting full reindex across all sources...")

    try:
        results = await indexer.full_reindex()
    except Exception as e:
        logger.exception("Reindex failed")
        await update.message.reply_text(f"Reindex error: {e}")
        return

    total = sum(results.values())
    lines = [f"*Reindex complete: {total} new items*\n"]
    for source, count in results.items():
        icon = SOURCE_ICONS.get(source, source)
        lines.append(f"  {icon}: {count} new")

    await db.log_audit("reindex", "read", f"Full reindex: {total} items",
                       user_id=update.effective_user.id, approved=True)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── OneDrive Commands ─────────────────────────────────────────────

@auth_required
async def cmd_connectonedrive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        from onedrive_module import get_auth_url, exchange_code
    except ImportError:
        await update.message.reply_text("OneDrive module not available.")
        return

    if context.args and len(context.args[0]) > 10:
        # User is providing the auth code
        code = context.args[0]
        try:
            name = exchange_code(code)
            await db.log_audit("connect_onedrive", "config", f"Connected OneDrive: {name}",
                               user_id=update.effective_user.id, approved=True)
            await update.message.reply_text(f"OneDrive connected as: *{name}*", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Connection failed: {e}")
        return

    try:
        url = get_auth_url()
    except RuntimeError as e:
        await update.message.reply_text(str(e))
        return

    await update.message.reply_text(
        "*Connect OneDrive*\n\n"
        f"1. Open this URL in your browser:\n`{url}`\n\n"
        "2. Sign in with your personal Microsoft account\n"
        "3. After approval, you'll be redirected to localhost\n"
        "4. Copy the `code=` value from the URL\n"
        "5. Send: /connectonedrive `<code>`",
        parse_mode="Markdown",
    )


@auth_required
async def cmd_odfiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        from onedrive_module import list_files
    except ImportError:
        await update.message.reply_text("OneDrive module not available.")
        return

    folder = "/"
    if context.args:
        folder = " ".join(context.args)

    try:
        files = list_files(folder_path=folder)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    if not files:
        await update.message.reply_text("No files found.")
        return

    lines = [f"*OneDrive: {folder}* ({len(files)} files)\n"]
    for i, f in enumerate(files[:20], 1):
        size_kb = f["size"] / 1024
        is_pdf = f["name"].lower().endswith(".pdf")
        tag = " [PDF]" if is_pdf else ""
        lines.append(f"{i}. `{f['name']}`{tag} ({size_kb:.0f} KB)\n   ID: `{f['id'][:20]}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_odsearch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /odsearch `<query>`", parse_mode="Markdown")
        return

    try:
        from onedrive_module import search_files
    except ImportError:
        await update.message.reply_text("OneDrive module not available.")
        return

    query = " ".join(context.args)
    try:
        files = search_files(query)
    except Exception as e:
        await update.message.reply_text(f"Search error: {e}")
        return

    if not files:
        await update.message.reply_text(f"No files matching: {query}")
        return

    lines = [f"*OneDrive search: {query}* ({len(files)} results)\n"]
    for i, f in enumerate(files[:10], 1):
        lines.append(f"{i}. `{f['name']}` ({f['size'] / 1024:.0f} KB)\n   ID: `{f['id'][:20]}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@auth_required
async def cmd_odread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /odread `<file_id>`", parse_mode="Markdown")
        return

    try:
        from onedrive_module import download_file_text
    except ImportError:
        await update.message.reply_text("OneDrive module not available.")
        return

    file_id = context.args[0]
    filename = context.args[1] if len(context.args) > 1 else "file.pdf"

    await update.message.reply_text("Downloading and extracting text...")
    try:
        text = download_file_text(file_id, filename)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    if not text:
        await update.message.reply_text("No text could be extracted from this file.")
        return

    header = f"*{filename}*\n\n"
    full = header + text
    if len(full) > 4000:
        await update.message.reply_text(header + text[:3900] + "\n\n_(truncated)_", parse_mode="Markdown")
    else:
        await update.message.reply_text(full, parse_mode="Markdown")


# ── Background Indexer ────────────────────────────────────────────

async def _background_index(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called by job_queue on interval. Indexes new content from all sources."""
    try:
        results = await indexer.incremental_index()
        total = sum(results.values())
        if total > 0:
            logger.info(f"Background index: {total} new items")
        await ops.mark_now(ops.LAST_INDEX_KEY)
    except Exception as e:
        logger.error(f"Background index failed: {e}")


async def _background_backfill(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deep-index mailbox history + embed anything missing (daily, resumable)."""
    try:
        indexed = await indexer.backfill_all_gmail_accounts()
        import embeddings
        embedded = await embeddings.embed_missing(limit=3000)
        if indexed or embedded:
            logger.info(f"Backfill: {indexed} emails indexed, {embedded} items embedded")
        await ops.mark_now(ops.LAST_BACKFILL_KEY)
    except Exception as e:
        logger.error(f"Backfill failed: {e}")


async def _reminders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every minute: send due reminders to the owner (see reminders.fire_due)."""
    try:
        await reminders.fire_due(context.bot)
    except Exception as e:
        logger.error(f"Reminder job failed: {e}")


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Snooze 1h / Tomorrow / Done buttons on a fired reminder."""
    query = update.callback_query
    await query.answer()
    if not security.is_authorized(query.from_user.id):
        await query.edit_message_text("Unauthorized.")
        return
    kind, _, rid_str = query.data.removeprefix(reminders.CB_PREFIX).partition(":")
    try:
        rid = int(rid_str)
    except ValueError:
        return
    text = await reminders.apply_button(kind, rid)
    try:
        await query.edit_message_text(text)
    except BadRequest:
        pass


@auth_required
async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = await db.list_reminders("pending", limit=30)
    if not pending:
        await update.message.reply_text(
            "No pending reminders. Say something like \"remind me Friday at 9 to chase the deposit\"."
        )
        return
    lines = ["⏰ Pending reminders\n"] + [reminders.describe(r) for r in pending]
    lines.append("\nCancel one by telling me, e.g. \"cancel reminder 3\".")
    await update.message.reply_text("\n".join(lines))


ALERTS_SETTING = "alerts_enabled"


async def alerts_enabled() -> bool:
    raw = await db.get_setting(ALERTS_SETTING)
    if raw is None:
        return config.ALERTS_ENABLED
    return raw == "1"


async def _alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every ALERT_POLL_MINUTES: light inbound sync, then triage whatever is new."""
    if not await alerts_enabled():
        indexer.drain_new_items()
        return
    try:
        for a in await llm._get_accounts_cached():
            try:
                await indexer.index_gmail(max_emails=50, account=a["label"], query="newer_than:1d")
            except Exception as e:
                logger.debug(f"alerts: gmail sync skipped for {a['label']}: {e}")
        await indexer.index_imessage()
        await alerts.process(context.bot, indexer.drain_new_items())
    except Exception as e:
        logger.error(f"Alerts job failed: {e}")


async def alert_reply(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, token: str) -> str:
    """Draft an acknowledgement and present it as an approval card. Never sends."""
    try:
        action = await alerts.build_reply_action(token)
    except Exception as e:
        return f"Couldn't draft a reply: {e}"
    await request_approval(
        context, chat_id, user_id,
        action_type=action["type"], category=ActionCategory.WRITE,
        summary=action["summary"], payload=action["payload"],
    )
    return "✍️ Reply drafted — approve it on the card below."


async def handle_alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """✍️ Reply / 👀 Seen buttons on an urgent alert."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not security.is_authorized(user.id):
        await query.edit_message_text("Unauthorized.")
        return
    op, _, token = query.data.removeprefix(alerts.CB_PREFIX).partition(":")
    original = query.message.text or ""
    if op == "seen":
        try:
            await query.edit_message_text(original + "\n\n👀 Seen.")
        except BadRequest:
            pass
        return
    if op == "reply":
        text = await alert_reply(context, query.message.chat_id, user.id, token)
        try:
            await query.edit_message_text(original + f"\n\n{text}")
        except BadRequest:
            pass


@auth_required
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/alerts [on|off] — toggle urgent alerts and show the last few."""
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        await db.set_setting(ALERTS_SETTING, "1" if arg == "on" else "0")
        await db.log_audit("alerts_toggle", "config", f"Urgent alerts turned {arg}",
                           user_id=update.effective_user.id, approved=True)
    enabled = await alerts_enabled()
    quiet = config.ALERT_QUIET_HOURS or "none"
    terms = await alerts.watch_terms()
    lines = [
        f"🚨 Urgent alerts: {'ON' if enabled else 'OFF'} (every {config.ALERT_POLL_MINUTES} min, "
        f"quiet hours {quiet}, model {config.OPENAI_FAST_MODEL})",
        "Watch-list: " + (", ".join(terms) if terms else "none — say \"always alert me about …\""),
        "Toggle with /alerts on or /alerts off.",
    ]
    recent = await db.recent_alerts(limit=5)
    if recent:
        lines.append("\nLast alerts:")
        for r in recent:
            lines.append(f"- {r['sent_at'][:16].replace('T', ' ')} {r['category']}: {r['title'][:60]}")
    if alerts.held:
        lines.append(f"\n{len(alerts.held)} urgent item(s) held for quiet hours.")
    await update.message.reply_text("\n".join(lines))


def _waiting_buttons(item: dict) -> InlineKeyboardMarkup:
    tok = item["token"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ Nudge", callback_data=f"wait_nudge:{tok}"),
        InlineKeyboardButton("🙈 Dismiss", callback_data=f"wait_dismiss:{tok}"),
    ]])


async def _send_waiting_cards(bot, chat_id: int, items: list[dict], limit: int = 5) -> int:
    sent = 0
    for it in items[:limit]:
        try:
            await tg_retry(lambda: bot.send_message(chat_id, waiting_on.describe(it), reply_markup=_waiting_buttons(it)))
            sent += 1
        except Exception as e:
            logger.warning(f"waiting-on card failed: {e}")
    return sent


async def nudge_item(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, item: dict) -> str:
    """Draft the follow-up and present it as a normal approval card. Never sends."""
    try:
        action = await waiting_on.build_nudge_action(item)
    except Exception as e:
        logger.warning(f"nudge draft failed: {e}")
        return f"Couldn't draft the follow-up: {e}"
    await request_approval(
        context, chat_id, user_id,
        action_type=action["type"], category=ActionCategory.WRITE,
        summary=action["summary"], payload=action["payload"],
    )
    return f"✍️ Follow-up drafted for {waiting_on.describe(item)} — approve it on the card below."


async def handle_waiting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nudge / Dismiss buttons on a waiting-on item."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not security.is_authorized(user.id):
        await query.edit_message_text("Unauthorized.")
        return
    op, _, tok = query.data.removeprefix("wait_").partition(":")
    item = await waiting_on.find_by_token(tok)
    if item is None:
        await query.edit_message_text("That item is no longer waiting (replied, dismissed, or re-indexed).")
        return
    if op == "dismiss":
        await db.dismiss_waiting(item["kind"], item["key"])
        await query.edit_message_text(f"🙈 Dismissed: {waiting_on.describe(item)}")
        return
    if op == "nudge":
        text = await nudge_item(context, query.message.chat_id, user.id, item)
        try:
            await query.edit_message_text(text)
        except BadRequest:
            pass


@auth_required
async def cmd_waiting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    items = await waiting_on.list_waiting()
    if not items:
        await update.message.reply_text(waiting_on.format_waiting(items))
        return
    await update.message.reply_text(
        f"⏳ Waiting on a reply ({len(items)}, no answer after {config.WAITING_ON_DAYS}+ days). "
        "Nudge drafts a follow-up for your approval; Dismiss hides it."
    )
    await _send_waiting_cards(context.bot, update.effective_chat.id, items, limit=10)


async def _imessage_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every IMESSAGE_WATCH_SECONDS: index new iMessage rows as soon as chat.db changes."""
    try:
        await indexer.watch_imessage_once()
    except Exception as e:
        logger.error(f"iMessage watch failed: {e}")


async def _background_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nightly SQLite online backup of clawdbot.db (see ops.backup_database)."""
    try:
        await ops.backup_database()
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        try:
            await context.bot.send_message(
                config.TELEGRAM_AUTHORIZED_USER_ID, f"⚠️ Nightly database backup failed: {e}"
            )
        except Exception:
            pass


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

def _approval_buttons(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{action_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{action_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{action_id}"),
    ]])


def _card_text(summary: str, action_type: str, payload: dict, revised: bool = False) -> str:
    detail = agent_tools.action_detail(action_type, payload)
    tag = "Draft (revised)" if revised else "Draft"
    return (f"*{tag} — {summary}*\n\n{detail}\n\n"
            "_Approve to send · Edit to revise · Reject to discard._")


async def request_approval(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    action_type: str,
    category: ActionCategory,
    summary: str,
    payload: dict,
) -> None:
    """Present an outbound action for approval with Approve / Edit / Reject."""
    action_id = await db.create_pending_action(action_type, payload)

    await db.log_audit(
        action_type=f"{action_type}_pending",
        category=category.value,
        summary=summary,
        detail=agent_tools.action_detail(action_type, payload),
        user_id=user_id,
    )

    preview = _card_text(summary, action_type, payload)
    try:
        await tg_retry(lambda: context.bot.send_message(
            chat_id=chat_id,
            text=preview,
            parse_mode="Markdown",
            reply_markup=_approval_buttons(action_id),
        ))
    except BadRequest:
        # Draft content broke Markdown parsing — send plain.
        await tg_retry(lambda: context.bot.send_message(
            chat_id=chat_id, text=preview, reply_markup=_approval_buttons(action_id)
        ))


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process Approve/Reject/Retry button presses."""
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

    if decision == "retry":
        user_text = _RETRY_CACHE.pop(action_id_str, None)
        if not user_text:
            await query.edit_message_text("Nothing to retry (too old).")
            return
        try:
            await query.edit_message_text("Retrying…")
        except BadRequest:
            pass
        await _run_agent_turn(context, query.message.chat_id, user.id, user_text)
        return

    try:
        action_id = int(action_id_str)
    except ValueError:
        return

    if decision == "edit":
        action = await db.get_pending_action(action_id)
        if action is None:
            await query.edit_message_text("This draft was already resolved.")
            return
        _EDIT_STATE[user.id] = {
            "action_id": action_id,
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
        }
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ Tell me what to change (e.g. \"shorter, and mention the deadline\"). /cancel to leave it as is.",
        )
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


def _resolve_one_attachment(spec: dict, default_account: str | None) -> dict:
    """Fetch attachment bytes from an email or OneDrive (runs in a thread)."""
    if spec.get("source") == "onedrive":
        from onedrive_module import download_file
        data = download_file(spec["id"])
    else:
        raw_id = spec["id"]
        att_account = default_account
        if ":" in raw_id:
            prefix, rest = raw_id.split(":", 1)
            if prefix in {a["label"] for a in gmail_module.get_connected_accounts()}:
                att_account, raw_id = prefix, rest
        data = gmail_module.get_attachment_bytes(raw_id, spec["filename"], account=att_account)

    if len(data) > 20 * 1024 * 1024:
        raise RuntimeError(f"'{spec['filename']}' is too large to email ({len(data) // (1024 * 1024)}MB)")
    return {"filename": spec["filename"], "data": data}


async def _resolve_attachments(specs: list[dict] | None, account: str | None) -> list[dict]:
    return [
        await asyncio.to_thread(_resolve_one_attachment, spec, account)
        for spec in (specs or [])
    ]


async def execute_approved_action(action: dict) -> str:
    """Dispatch approved actions to their handlers."""
    action_type = action["action_type"]
    payload = action["payload"]

    if action_type == "send_email":
        try:
            attachments = await _resolve_attachments(payload.get("attachments"), payload.get("account"))
            result = await asyncio.to_thread(
                gmail_module.send_email,
                to=payload["to"],
                subject=payload["subject"],
                body=payload["body"],
                account=payload.get("account"),
                cc=payload.get("cc", ""),
                bcc=payload.get("bcc", ""),
                html=bool(payload.get("html")),
                attachments=attachments,
            )
            att_note = f" with {len(attachments)} attachment(s)" if attachments else ""
            return f"Email sent to {payload['to']}{att_note}.\nMessage ID: {result['message_id']}"
        except Exception as e:
            return f"Send failed: {e}"
    elif action_type == "reply_email":
        try:
            attachments = await _resolve_attachments(payload.get("attachments"), payload.get("account"))
            result = await asyncio.to_thread(
                gmail_module.reply_to_email,
                payload["email_id"],
                payload["body"],
                account=payload.get("account"),
                cc=payload.get("cc", ""),
                bcc=payload.get("bcc", ""),
                attachments=attachments,
            )
            att_note = f" with {len(attachments)} attachment(s)" if attachments else ""
            return f"Reply sent to {result['to']} (in-thread){att_note}.\nMessage ID: {result['message_id']}"
        except Exception as e:
            return f"Reply failed: {e}"
    elif action_type == "send_imessage":
        import imessage_send
        label = agent_tools.message_recipient_label(payload)
        service = imessage_send.normalize_service(payload.get("service"))
        if not imessage_send.is_available():
            await db.log_audit("send_imessage", "write",
                               f"Not sent to {label}: iMessage sending needs the Mac", approved=False)
            return (f"iMessage sending needs the Mac (Messages.app). Nothing was sent to {label}; "
                    "approve again once Clawdbot runs on the Mac mini.")
        try:
            await asyncio.to_thread(
                imessage_send.send_message, payload["to"], payload["body"], service
            )
            await db.log_audit("send_imessage", "write", f"{service} sent to {label}",
                               detail=payload.get("body", "")[:500], approved=True)
            return f"{service} sent to {label}."
        except Exception as e:
            await db.log_audit("send_imessage", "write", f"{service} to {label} failed: {e}",
                               approved=False)
            return f"{service} send failed: {e}"
    else:
        return f"Unknown action type: {action_type}"


# ── Free-Text Chat ────────────────────────────────────────────────

@auth_required
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route free-text messages to the LLM agent (auto tool-calling)."""
    user_text = update.message.text
    if not user_text:
        return
    if update.effective_user.id in _EDIT_STATE:
        await _handle_draft_edit(context, update.effective_user.id, user_text)
        return
    await _run_agent_turn(
        context, update.effective_chat.id, update.effective_user.id, user_text
    )


@auth_required
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _EDIT_STATE.pop(update.effective_user.id, None):
        await update.message.reply_text("Edit cancelled — the draft is unchanged.")
    else:
        await update.message.reply_text("Nothing to cancel.")


@auth_required
async def cmd_signature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set per-account email signatures: /signature <account> <text> | clear | (no args to view)."""
    text = update.message.text or ""
    parts = text.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""

    if not rest.strip():
        accounts = await llm._get_accounts_cached()
        lines = ["*Signatures* — set with /signature `<account> <text>`, clear with /signature `<account> clear`\n"]
        for a in accounts:
            sig = await db.get_setting(f"signature:{a['label']}")
            lines.append(f"*{a['label']}:*\n{sig}" if sig else f"*{a['label']}:* (none)")
        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        return

    sub = rest.split(None, 1)
    label = sub[0].strip()
    body = sub[1].strip() if len(sub) > 1 else ""
    if not body:
        await update.message.reply_text("Usage: /signature <account> <signature text> — or /signature <account> clear")
        return
    if body.lower() == "clear":
        await db.set_setting(f"signature:{label}", "")
        await update.message.reply_text(f"Signature for '{label}' cleared.")
        return
    await db.set_setting(f"signature:{label}", body)
    await update.message.reply_text(f"Signature for '{label}' saved:\n\n{body}\n\nIt'll be applied to drafts from that account.")


# ── Conversation & Memory Commands ────────────────────────────────

@auth_required
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a fresh conversation (clears short-term context, keeps memories)."""
    await llm.start_new_session()
    await update.message.reply_text("✨ Fresh conversation started. Long-term memories are kept.")


def _memory_lines(memories: list[dict]) -> list[str]:
    lines = []
    for m in memories:
        cat = f" ({m['category']})" if m["category"] else ""
        lines.append(f"`#{m['id']}`{cat} {_md_escape(m['content'])}")
    return lines


def _md_escape(text: str) -> str:
    """Neutralize the few characters that break Telegram's legacy Markdown mode."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "'")


@auth_required
async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/voice [on|off|auto] — spoken replies always / never / only to voice notes (persisted)."""
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in llm.VOICE_MODES:
        await db.set_setting(llm.VOICE_SETTING, arg)
        mode = arg
    elif arg:
        await update.message.reply_text("Usage: /voice on | off | auto")
        return
    else:
        mode = await llm.voice_mode()
    desc = {
        "on": "every answer also comes as a voice note",
        "off": "answers are text only (\"say it\" still reads one aloud)",
        "auto": "voice notes get a spoken answer; typed messages get text",
    }[mode]
    await update.message.reply_text(
        f"🔊 Voice replies: *{mode}* — {desc}.\nVoice: `{config.VOICE_OUT_VOICE}` ({config.VOICE_OUT_MODEL})",
        parse_mode="Markdown",
    )


@auth_required
async def cmd_memories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    explicit = await db.list_memories(limit=30, source="explicit")
    auto = await db.list_memories(limit=30, source="auto")
    summaries = await db.list_session_summaries(limit=3)
    if not explicit and not auto and not summaries:
        await update.message.reply_text(
            "No memories yet. Tell me things like \"remember to sign my emails as Josh\" — "
            "I also pick up durable facts from our conversations automatically."
        )
        return
    lines = ["*Long-term memory*"]
    if explicit:
        lines.append("\n*Saved on request*")
        lines.extend(_memory_lines(explicit))
    if auto:
        lines.append("\n*Learned from conversations*")
        lines.extend(_memory_lines(auto))
    if summaries:
        lines.append("\n*Recent conversations*")
        for sm in summaries:
            lines.append(f"`{sm['created_at'][:10]}` {_md_escape(sm['summary'][:300])}")
        lines.append("More with /summaries")
    lines.append("\nForget a memory with /forget `<id>`")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_summaries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Episodic memory: what earlier conversations were about (memory.summarize_turns)."""
    summaries = await db.list_session_summaries(limit=10)
    if not summaries:
        await update.message.reply_text(
            "No conversation summaries yet — one is written when a conversation ends "
            "(/new or idle reset) or grows past the history budget."
        )
        return
    lines = ["*Earlier conversations*\n"]
    for sm in summaries:
        ents = ", ".join(sm["entities"][:6])
        lines.append(f"`{sm['created_at'][:16].replace('T', ' ')}` {_md_escape(sm['summary'])}"
                     + (f"\n  _{_md_escape(ents)}_" if ents else ""))
    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /forget <memory id> — see ids with /memories")
        return
    try:
        memory_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Memory id must be a number — see /memories")
        return
    if await db.delete_memory(memory_id):
        await update.message.reply_text(f"Forgotten memory #{memory_id}.")
    else:
        await update.message.reply_text(f"No memory #{memory_id} found.")


# ── Voice, Photos & Documents (Phase 5) ──────────────────────────

_MEDIA_MAX_BYTES = 20 * 1024 * 1024


@auth_required
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram voice note / audio → transcription → normal agent flow."""
    voice = update.message.voice or update.message.audio
    if voice is None:
        return
    if (voice.file_size or 0) > _MEDIA_MAX_BYTES:
        await update.message.reply_text("That audio is too large (20MB max).")
        return

    note = await update.message.reply_text("🎤 Transcribing…")
    try:
        tg_file = await context.bot.get_file(voice.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        filename = getattr(voice, "file_name", None) or "voice.ogg"
        transcript = await llm.transcribe_voice(data, filename)
    except Exception as e:
        logger.exception("Voice transcription failed")
        await note.edit_text(f"Couldn't transcribe that ({type(e).__name__}).")
        return

    if not transcript:
        await note.edit_text("I couldn't hear anything in that recording.")
        return

    try:
        await note.edit_text(f"🎤 “{transcript}”")
    except BadRequest:
        pass

    if update.effective_user.id in _EDIT_STATE:
        await _handle_draft_edit(context, update.effective_user.id, transcript)
        return
    await _run_agent_turn(context, update.effective_chat.id, update.effective_user.id, transcript,
                          incoming_voice=True)


def _extract_pdf_bytes(data: bytes) -> str:
    import io
    from PyPDF2 import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text and text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


@auth_required
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photos and documents (PDF/text) → extract → answer + index for later search."""
    msg = update.message
    caption = (msg.caption or "").strip()

    if msg.photo:
        file_id = msg.photo[-1].file_id
        unique_id = msg.photo[-1].file_unique_id
        filename, mime = "photo.jpg", "image/jpeg"
    elif msg.document:
        doc = msg.document
        if (doc.file_size or 0) > _MEDIA_MAX_BYTES:
            await msg.reply_text("That file is too large (20MB max).")
            return
        file_id, unique_id = doc.file_id, doc.file_unique_id
        filename = doc.file_name or "file"
        mime = doc.mime_type or ""
    else:
        return

    note = await msg.reply_text("🖼 Looking…" if mime.startswith("image/") else "📄 Reading…")
    try:
        tg_file = await context.bot.get_file(file_id)
        data = bytes(await tg_file.download_as_bytearray())

        if mime.startswith("image/"):
            extracted = await llm.describe_image(data, mime)
            label = f"photo '{filename}'"
        elif mime == "application/pdf" or filename.lower().endswith(".pdf"):
            extracted = await asyncio.to_thread(_extract_pdf_bytes, data)
            label = f"PDF '{filename}'"
        elif mime.startswith("text/") or filename.lower().endswith((".txt", ".csv", ".md", ".log")):
            extracted = data.decode("utf-8", errors="replace")
            label = f"file '{filename}'"
        else:
            await note.edit_text(
                f"I can read photos, PDFs, and text files — '{filename}' "
                f"({mime or 'unknown type'}) isn't supported yet."
            )
            return
    except Exception as e:
        logger.exception("Media extraction failed")
        await note.edit_text(f"Couldn't read that ({type(e).__name__}).")
        return

    extracted = (extracted or "").strip()
    if not extracted:
        await note.edit_text("I couldn't extract anything readable from that.")
        return

    # Index the upload so it's findable later via search_everything.
    try:
        import embeddings
        await search_index.index_item(
            "telegram", unique_id, filename, extracted,
            {"date": datetime.now(timezone.utc).isoformat(), "from": "telegram upload"},
        )
        await embeddings.embed_item("telegram", unique_id, filename, extracted)
    except Exception:
        logger.debug("Telegram media indexing failed", exc_info=True)

    try:
        await note.delete()
    except Exception:
        pass

    question = caption or "Summarize what you received and note anything that needs action."
    agent_message = (
        f"[The user sent a {label} via Telegram. Extracted content follows.]\n"
        f"{extracted[:8000]}\n\n[User's message about it:] {question}"
    )
    await _run_agent_turn(context, update.effective_chat.id, update.effective_user.id, agent_message)


# ── Morning Digest (Phase 5) ─────────────────────────────────────

async def _daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Morning briefing (see digest.py): sections → fast-model summary → cards → held alerts."""
    try:
        await digest.run(context.bot, "morning", waiting_cards=_send_waiting_cards)
    except Exception as e:
        logger.error(f"Morning digest failed: {e}")


async def _evening_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await digest.run(context.bot, "evening", waiting_cards=_send_waiting_cards)
    except Exception as e:
        logger.error(f"Evening digest failed: {e}")


@auth_required
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/digest [evening] — run the briefing now (preview)."""
    kind = "evening" if context.args and context.args[0].lower().startswith("eve") else "morning"
    sent = await digest.run(context.bot, kind, chat_id=update.effective_chat.id,
                            waiting_cards=_send_waiting_cards)
    if not sent:
        await update.message.reply_text(f"Nothing to report for the {kind} digest right now.")


# ── Bot Setup ─────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pin", cmd_pin))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("memories", cmd_memories))
    app.add_handler(CommandHandler("summaries", cmd_summaries))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("waiting", cmd_waiting))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("signature", cmd_signature))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("addaccount", cmd_addaccount))
    app.add_handler(CommandHandler("useaccount", cmd_useaccount))
    app.add_handler(CommandHandler("emails", cmd_emails))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("read", cmd_read))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(CommandHandler("draft", cmd_draft))
    app.add_handler(CommandHandler("aidraft", cmd_aidraft))
    app.add_handler(CommandHandler("attachments", cmd_attachments))
    app.add_handler(CommandHandler("readpdf", cmd_readpdf))
    app.add_handler(CommandHandler("searchpdfs", cmd_searchpdfs))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("findlive", cmd_findlive))
    app.add_handler(CommandHandler("indexstats", cmd_indexstats))
    app.add_handler(CommandHandler("reindex", cmd_reindex))
    app.add_handler(CommandHandler("connectonedrive", cmd_connectonedrive))
    app.add_handler(CommandHandler("odfiles", cmd_odfiles))
    app.add_handler(CommandHandler("odsearch", cmd_odsearch))
    app.add_handler(CommandHandler("odread", cmd_odread))
    app.add_handler(CommandHandler("properties", cmd_properties))
    app.add_handler(CommandHandler("addproperty", cmd_addproperty))
    app.add_handler(CommandHandler("pick", cmd_pick))
    app.add_handler(CommandHandler("removeproperty", cmd_removeproperty))
    app.add_handler(CommandHandler("reviews", cmd_reviews))
    app.add_handler(CommandHandler("checkreviews", cmd_checkreviews))
    app.add_handler(CallbackQueryHandler(handle_ops_callback, pattern=r"^ops_"))
    app.add_handler(CallbackQueryHandler(handle_reminder_callback, pattern=r"^rem_"))
    app.add_handler(CallbackQueryHandler(handle_waiting_callback, pattern=r"^wait_"))
    app.add_handler(CallbackQueryHandler(handle_alert_callback, pattern=r"^alert_"))
    app.add_handler(CallbackQueryHandler(handle_approval_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_media))

    # Background search indexer
    index_interval = config.INDEX_POLL_INTERVAL_MINUTES * 60
    app.job_queue.run_repeating(
        _background_index,
        interval=index_interval,
        first=60,  # first index 60s after startup
        name="search_indexer",
    )

    # Urgent inbound alerts: light sync + triage every few minutes
    if config.ALERT_POLL_MINUTES > 0:
        app.job_queue.run_repeating(
            _alerts_job, interval=config.ALERT_POLL_MINUTES * 60, first=90, name="alerts",
        )

    # Reminders: fire anything due, once a minute
    app.job_queue.run_repeating(_reminders_job, interval=60, first=20, name="reminders")

    # Near-realtime iMessage indexing: cheap mtime check on chat.db, index only on change
    if config.IMESSAGE_WATCH_SECONDS > 0:
        app.job_queue.run_repeating(
            _imessage_watch,
            interval=config.IMESSAGE_WATCH_SECONDS,
            first=config.IMESSAGE_WATCH_SECONDS,
            name="imessage_watch",
        )

    # Deep mailbox backfill + embedding catch-up: shortly after startup, then daily
    app.job_queue.run_repeating(
        _background_backfill,
        interval=24 * 3600,
        first=120,
        name="mailbox_backfill",
    )

    # Nightly SQLite backup at the configured local hour
    if config.BACKUP_ENABLED:
        app.job_queue.run_daily(
            _background_backup,
            time=dtime(hour=config.BACKUP_HOUR, tzinfo=datetime.now().astimezone().tzinfo),
            name="db_backup",
        )

    # Morning digest at the configured local hour
    if config.DIGEST_ENABLED:
        local_tz = datetime.now().astimezone().tzinfo
        app.job_queue.run_daily(
            _daily_digest,
            time=dtime(hour=config.DIGEST_HOUR, tzinfo=local_tz),
            name="daily_digest",
        )
        if config.EVENING_DIGEST_HOUR:
            app.job_queue.run_daily(
                _evening_digest,
                time=dtime(hour=config.EVENING_DIGEST_HOUR, tzinfo=local_tz),
                name="evening_digest",
            )

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

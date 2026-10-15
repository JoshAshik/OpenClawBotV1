# Clawdbot

A personal Telegram assistant for property management. Talk to it in plain English (or send a
voice note, photo, or PDF) — it searches your email and files, drafts and sends replies with your
approval, watches Google Reviews, and briefs you every morning.

## Features

- **AI Chat** — Natural-language assistant powered by OpenAI (streaming replies, tool use, long-term memory)
- **Voice, Photos & PDFs** — Send a voice note, photo, or document; it's transcribed/read, answered, and indexed
- **Gmail Integration** — Read, search, draft, reply in-thread, and send (cc/bcc/attachments) across multiple accounts
- **Hybrid Search** — Keyword + semantic search over email, PDFs, files, and messages, with sender/date filters
- **Message Timelines** (Mac) — "What did Mike say about the boiler?" pulls the live iMessage/SMS thread with a person or group chat; new texts are indexed within seconds
- **iMessage / SMS Sending** (Mac) — "Text Mike that the plumber comes Tuesday" resolves the contact and sends through Messages.app after approval; WhatsApp is search-only
- **Approval Flow** — Every outbound email or text gets an Approve / ✏️ Edit / Reject card; Edit revises the draft in place
- **Reminders** — "Remind me Thursday to chase the deposit" fires a Telegram message at the time with Snooze 1h / Tomorrow / Done buttons
- **Waiting-on Tracker** — Sent emails and texts with no reply after a few days show up in the digest and `/waiting`, with a Nudge button that drafts the follow-up for approval
- **Urgent Alerts** — New inbound email and texts are triaged by a cheap model every few minutes; emergencies, payment and legal issues reach you within minutes with a Reply button (drafts go through approval); a watch-list of contacts/topics is one "always alert me about…" away
- **Morning Digest & Evening Wrap-up** — Today's calendar (on the Mac), reminders due, urgent alerts, who you're waiting on, unread that matters, new reviews, pending drafts — composed into a short briefing; optional evening recap
- **Google Reviews Monitoring** — Track reviews across properties with automatic polling and Telegram alerts
- **Security** — Single-user authorization, optional session PIN lock, rate limiting, full audit logging

## Setup

### Prerequisites

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An OpenAI API key (used for chat, semantic-search embeddings, voice transcription, and vision)

### Installation

```bash
git clone <repo-url>
cd Clawdbot
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
```

### Configuration

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Required:

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_AUTHORIZED_USER_ID` | Numeric Telegram user id(s) allowed to use the bot, comma-separated. The first is the owner: digests, alerts and reminders go only there |
| `OPENAI_API_KEY` | OpenAI API key |

Optional (defaults in parentheses):

| Variable | Description |
|---|---|
| `OPENAI_MODEL` | Chat model (`gpt-5.1`) |
| `OPENAI_FAST_MODEL` | Cheap model for triage and short drafts (`gpt-5-mini`) |
| `MAX_PARALLEL_TOOLS` | Read-only tools run concurrently per agent round, up to this many (4) |
| `OPENAI_TIMEOUT_SECONDS` / `TURN_TIMEOUT_SECONDS` | HTTP timeout per OpenAI request / budget for one whole agent turn (60 / 180) |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | Semantic-search embeddings (`text-embedding-3-small` / 512) |
| `VOICE_MODEL` | Voice transcription (`gpt-4o-mini-transcribe`, auto-falls back to `whisper-1`) |
| `LLM_MAX_TOKENS` | Max tokens per reply (8192) |
| `HISTORY_TOKEN_BUDGET` | Conversation context sent to the model, in tokens (30000) |
| `SESSION_IDLE_RESET_MINUTES` | Start a fresh conversation after this much idle time (120) |
| `USER_NAME` | Your name, used in the assistant's prompt |
| `DEFAULT_GMAIL_ACCOUNT` | Gmail account label activated at startup (`personal`) |
| `BACKFILL_MAX_EMAILS` | How deep the mailbox history backfill goes, per account (2000) |
| `INDEX_POLL_INTERVAL_MINUTES` | Incremental indexing cadence (10) |
| `IMESSAGE_DB_PATH` / `IMESSAGE_BATCH_SIZE` | Override the Mac Messages DB location / backfill page size (default / 2000) |
| `IMESSAGE_WATCH_SECONDS` | How often to check chat.db for new texts and index them (30; 0 disables) |
| `CONTACTS_DB_PATH` / `CONTACTS_DEFAULT_COUNTRY` | Override the Apple Contacts store / country code for bare numbers (default / 1) |
| `DIGEST_ENABLED` / `DIGEST_HOUR` | Morning digest on/off and local hour (1 / 8) |
| `EVENING_DIGEST_HOUR` | Local hour for the evening wrap-up (0 = off) |
| `REMINDER_HOUR` | Local hour for date-only reminders and the Tomorrow snooze (9) |
| `WAITING_ON_DAYS` | Days without a reply before a sent email/text counts as waiting (3) |
| `ALERTS_ENABLED` / `ALERT_POLL_MINUTES` / `ALERT_QUIET_HOURS` / `ALERT_MAX_AGE_HOURS` | Urgent alerts on/off, poll cadence, local quiet window, max item age (1 / 3 / `23-7` / 24) |
| `BACKUP_ENABLED` / `BACKUP_HOUR` / `BACKUP_DIR` / `BACKUP_KEEP` | Nightly SQLite backup (1 / 3 / `./backups` / 14) |
| `SESSION_PIN` | PIN required after each restart (blank = disabled) |
| `RATE_LIMIT_PER_HOUR` | Max outbound sends per hour (20) |
| `ONEDRIVE_CLIENT_ID` | Azure app client id, enables OneDrive search/attachments |
| `GOOGLE_PLACES_API_KEY` / `REVIEW_POLL_INTERVAL_MINUTES` | Review monitoring (off / 15) |

### Gmail Setup (Optional)

1. Create a project in [Google Cloud Console](https://console.cloud.google.com)
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app) and download as `credentials.json`
4. Add your email(s) as test users in the OAuth consent screen
5. Place `credentials.json` in the project root
6. Use `/addaccount <label>` in Telegram to connect each account (e.g. `personal`, `work`)

If Gmail ever shows "auth expired" errors (tokens get revoked after long inactivity), just run
`/addaccount <label>` again for that account.

### Google Reviews Setup (Optional)

1. Enable the Places API (New) in Google Cloud Console
2. Create an API key restricted to the Places API
3. Add `GOOGLE_PLACES_API_KEY` to `.env`

## Usage

```bash
python main.py
```

**Just talk to it.** Send text, a voice note, a photo, or a PDF. The assistant decides which tools
to use — searching your mail and files, reading PDFs, resolving contacts, checking reviews — and
streams its progress live ("🔍 Searching…", "📖 Reading an email…") before the answer.

**Sending email always requires your approval.** Asking it to draft or reply produces a card with
**✅ Approve / ✏️ Edit / ❌ Reject**. Edit takes freeform instructions ("shorter, cc the owner") —
typed or spoken — and revises the same card in place. Replies stay in the original thread, and an
edit can never change who a reply goes to.

**On first run:** indexing starts about a minute after startup, and a deep mailbox backfill
(up to `BACKFILL_MAX_EMAILS` per account, plus semantic embeddings and the contacts table) runs a
couple of minutes in — older mail becomes searchable as it progresses. If the digest is enabled,
a morning briefing arrives at `DIGEST_HOUR` when there's something worth saying.

**iMessage / WhatsApp:** the indexers read the Mac databases (`~/Library/Messages/chat.db`,
WhatsApp Desktop storage) and are inert on Windows — run the bot on a Mac to activate them.
See Phase 6 of [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md).

### Telegram Commands

Everything works through natural language; commands are shortcuts and admin controls.

**Conversation & memory**

| Command | Description |
|---|---|
| `/new` | Start a fresh conversation (keeps long-term memories) |
| `/memories` | List long-term memories the AI has saved |
| `/reminders` | Pending reminders (set them in plain English: "remind me Friday at 9 to…") |
| `/waiting` | Sent emails/texts with no reply, each with Nudge / Dismiss buttons |
| `/alerts [on\|off]` | Urgent-alert status, watch-list, last alerts; toggle on/off |
| `/digest [evening]` | Send the morning briefing or evening wrap-up now |
| `/forget <id>` | Delete a memory |
| `/cancel` | Cancel an in-progress draft edit |

**Email**

| Command | Description |
|---|---|
| `/signature <account> <text>` | Set a per-account signature (`/signature <account> clear` to remove) |
| `/accounts` | List connected Gmail accounts |
| `/addaccount <label>` | Connect (or reconnect) a Gmail account |
| `/useaccount <label>` | Switch the active account |
| `/emails [N]` | List recent inbox emails |
| `/search <query>` | Search Gmail (Gmail query syntax) |
| `/read <id>` | Read a specific email |
| `/attachments <id>` | List an email's attachments |
| `/readpdf <id>` | Extract text from an email's PDF attachments |
| `/searchpdfs <query>` | Search emails that have PDF attachments |
| `/send <to> \| <subject> \| <body>` | Send email (requires approval) |
| `/draft <to> \| <subject> \| <body>` | Save a draft in Gmail |
| `/aidraft <instructions>` | AI drafts an email for approval |

**Search & files**

| Command | Description |
|---|---|
| `/find <query>` | Search the unified index directly |
| `/findlive <query>` | Search live sources (slower, fresher) |
| `/indexstats` | Items indexed per source |
| `/reindex` | Trigger a full reindex + backfill now |
| `/connectonedrive` | Connect a personal OneDrive |
| `/odfiles` / `/odsearch <q>` / `/odread <id>` | Browse / search / read OneDrive files |

**Properties & reviews**

| Command | Description |
|---|---|
| `/properties` | List monitored properties |
| `/addproperty <name>` | Search and add a property (then `/pick <n>`) |
| `/removeproperty <place_id>` | Stop monitoring a property |
| `/reviews` | Show recent reviews |
| `/checkreviews` | Manually trigger a review check |

**Admin**

| Command | Description |
|---|---|
| `/start` | Wake up and show status |
| `/status` | Model, uptime, session, rate limit, last index/backfill/backup |
| `/usage` | OpenAI spend today and month-to-date, by purpose and model, most expensive turns |
| `/doctor` | Health checks: Gmail tokens, OpenAI, Mac data sources, disk, jobs |
| `/update` | `git pull` + `pip install`, then restart (confirm button) |
| `/restart` | Restart the bot (confirm button; needs a supervisor such as launchd) |
| `/pin <code>` / `/lock` | Unlock / lock the session |
| `/audit` | Recent audit log |
| `/help` | Command reference |

## Project Structure

```
main.py             — Entry point
bot.py              — Telegram handlers, streaming status UX, approval/Edit flow, digest
llm.py              — Streaming OpenAI agent loop, sessions, system prompt, memory helpers
agent_tools.py      — Tool schemas + execution (search, read, draft/reply, contacts, remember)
search_index.py     — SQLite FTS5 index + hybrid (keyword+semantic) retrieval
embeddings.py       — OpenAI embeddings stored in SQLite, numpy similarity search
indexer.py          — Background indexing: Gmail incremental + backfill, OneDrive,
                      iMessage/WhatsApp conversation windows (Mac), contacts rebuild
gmail_module.py     — Gmail OAuth (multi-account), read/send/reply/attachments
onedrive_module.py  — Microsoft Graph: OneDrive search/download
imessage_module.py  — Reads the Mac Messages database (Mac only)
whatsapp_module.py  — Reads WhatsApp Desktop storage (Mac only, read-only)
contacts_module.py  — Apple Contacts reader: phone/email → name for message windows (Mac only)
imessage_send.py    — Sends an approved iMessage/SMS through Messages.app via osascript (Mac only)
reviews.py          — Google Reviews polling via Places API
telegram_format.py  — Markdown → Telegram HTML rendering
security.py         — Auth, session PIN, rate limiting
reminders.py        — Reminders: time parsing, minutely firing job, snooze/done buttons
waiting_on.py       — Waiting-on tracker: unanswered sent mail/texts, Nudge drafts via approval card
alerts.py           — Urgent inbound triage (watch-list + fast model), 🚨 cards with Reply/Seen, quiet hours
digest.py           — Morning briefing / evening wrap-up sections, composed by the fast model
ops.py              — /doctor health checks, nightly DB backup, /update and /restart
db.py               — SQLite: turns, memories, contacts, pending actions, audit log
config.py           — Environment variable loading
deploy/             — macOS LaunchAgent template + MAC_SETUP.md (24/7 on a Mac mini)
evals/              — Retrieval/agent eval set mined from real queries; `python -m evals.run`
```

Runtime data lives next to the code: `clawdbot.db` (index, conversations, memories, audit),
`token_<label>.json` (Gmail OAuth per account), `onedrive_token.json`, `clawdbot.log` (rotates at
10 MB × 5), and nightly backups in `backups/`.

## Costs

Every OpenAI call is recorded with its token usage; `/usage` turns that into dollars using the
price table `PRICES_PER_MTOK` in `config.py`. The values shipped there are placeholders — check
[OpenAI's pricing page](https://platform.openai.com/docs/pricing) and update them, and add any
model you switch to (unknown models are counted at $0 and flagged in `/usage`).

## Running 24/7 on a Mac

See [deploy/MAC_SETUP.md](deploy/MAC_SETUP.md): Python + venv, copying the live files, Gmail
tokens that don't expire, the Full Disk Access grant for Messages/WhatsApp/Contacts, and the
launchd agent that keeps the bot alive across crashes, `/update`, and reboots.

## Roadmap

[IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) tracks the build-out. Phases 0–5 (engine, streaming UX,
structured memory, hybrid search, email production, voice/photos/digest) are complete.
[UPGRADE_PLAN.md](UPGRADE_PLAN.md) continues with Phases 7–12: the Mac mini deployment, real
iMessage/WhatsApp search and iMessage sending, proactivity, and property-management features.

## License

Private use.

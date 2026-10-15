# Clawdbot

A personal Telegram assistant for property management. Talk to it in plain English (or send a
voice note, photo, or PDF) — it searches your email and files, drafts and sends replies with your
approval, watches Google Reviews, and briefs you every morning.

## Features

- **AI Chat** — Natural-language assistant powered by OpenAI (streaming replies, tool use, long-term memory)
- **Voice, Photos & PDFs** — Send a voice note, photo, or document; it's transcribed/read, answered, and indexed
- **Gmail Integration** — Read, search, draft, reply in-thread, and send (cc/bcc/attachments) across multiple accounts
- **Hybrid Search** — Keyword + semantic search over email, PDFs, files, and messages, with sender/date filters
- **Approval Flow** — Every outbound email gets an Approve / ✏️ Edit / Reject card; Edit revises the draft in place
- **Morning Digest** — Daily triaged briefing: unread that matters, new reviews, pending drafts
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
| `TELEGRAM_AUTHORIZED_USER_ID` | Your numeric Telegram user ID (only this user can use the bot) |
| `OPENAI_API_KEY` | OpenAI API key |

Optional (defaults in parentheses):

| Variable | Description |
|---|---|
| `OPENAI_MODEL` | Chat model (`gpt-5.1`) |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | Semantic-search embeddings (`text-embedding-3-small` / 512) |
| `VOICE_MODEL` | Voice transcription (`gpt-4o-mini-transcribe`, auto-falls back to `whisper-1`) |
| `LLM_MAX_TOKENS` | Max tokens per reply (8192) |
| `HISTORY_TOKEN_BUDGET` | Conversation context sent to the model, in tokens (30000) |
| `SESSION_IDLE_RESET_MINUTES` | Start a fresh conversation after this much idle time (120) |
| `USER_NAME` | Your name, used in the assistant's prompt |
| `DEFAULT_GMAIL_ACCOUNT` | Gmail account label activated at startup (`personal`) |
| `BACKFILL_MAX_EMAILS` | How deep the mailbox history backfill goes, per account (2000) |
| `INDEX_POLL_INTERVAL_MINUTES` | Incremental indexing cadence (10) |
| `DIGEST_ENABLED` / `DIGEST_HOUR` | Morning digest on/off and local hour (1 / 8) |
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
| `/status` | Model, session, and rate-limit state |
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
whatsapp_module.py  — Reads WhatsApp Desktop storage (Mac only)
reviews.py          — Google Reviews polling via Places API
telegram_format.py  — Markdown → Telegram HTML rendering
security.py         — Auth, session PIN, rate limiting
db.py               — SQLite: turns, memories, contacts, pending actions, audit log
config.py           — Environment variable loading
```

Runtime data lives next to the code: `clawdbot.db` (index, conversations, memories, audit),
`token_<label>.json` (Gmail OAuth per account), `onedrive_token.json`, `clawdbot.log`.

## Roadmap

[IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) tracks the build-out. Phases 0–5 (engine, streaming UX,
structured memory, hybrid search, email production, voice/photos/digest) are complete; Phase 6
(running on a Mac so iMessage/WhatsApp indexing goes live) is open.

## License

Private use.

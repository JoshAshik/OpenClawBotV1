# Clawdbot

A personal Telegram bot for property management. Chat with AI, manage Gmail, and monitor Google Reviews — all from Telegram.

## Features

- **AI Chat** — Converse with Claude or ChatGPT, switchable on the fly (`/switch claude` or `/switch chatgpt`)
- **Gmail Integration** — Read, search, draft, and send emails with multi-account OAuth support
- **Google Reviews Monitoring** — Track reviews across properties with automatic polling and Telegram alerts
- **Approval Flow** — Outbound actions (sending emails, messages) require explicit approval before executing
- **Security** — Single-user authorization, optional session PIN lock, rate limiting, full audit logging

## Setup

### Prerequisites

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- At least one LLM API key (Anthropic and/or OpenAI)

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

Edit `.env` with your credentials:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather |
| `TELEGRAM_AUTHORIZED_USER_ID` | Yes | Your numeric Telegram user ID |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (for Claude) |
| `OPENAI_API_KEY` | No | OpenAI API key (for ChatGPT) |
| `SESSION_PIN` | No | PIN required after each restart |
| `RATE_LIMIT_PER_HOUR` | No | Max outbound actions per hour (default: 20) |
| `GOOGLE_PLACES_API_KEY` | No | Google Places API key (for review monitoring) |

### Gmail Setup (Optional)

1. Create a project in [Google Cloud Console](https://console.cloud.google.com)
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app) and download as `credentials.json`
4. Add your email(s) as test users in the OAuth consent screen
5. Place `credentials.json` in the project root
6. Use `/addaccount <label>` in Telegram to connect each account

### Google Reviews Setup (Optional)

1. Enable the Places API (New) in Google Cloud Console
2. Create an API key restricted to the Places API
3. Add `GOOGLE_PLACES_API_KEY` to `.env`

## Usage

```bash
python main.py
```

### Telegram Commands

| Command | Description |
|---|---|
| `/start` | Wake up and show status |
| `/pin <code>` | Unlock session |
| `/lock` | Lock session |
| `/switch <claude\|chatgpt>` | Switch LLM |
| `/status` | Current bot state |
| `/audit` | Recent audit log |
| `/emails [N]` | List recent inbox emails |
| `/search <query>` | Search emails (Gmail query syntax) |
| `/read <id>` | Read a specific email |
| `/draft <to> \| <subject> \| <body>` | Save a draft in Gmail |
| `/send <to> \| <subject> \| <body>` | Send email (requires approval) |
| `/aidraft <instructions>` | AI drafts an email for approval |
| `/accounts` | List connected Gmail accounts |
| `/addaccount <label>` | Connect a Gmail account |
| `/useaccount <label>` | Switch active Gmail account |
| `/properties` | List monitored properties |
| `/addproperty <name>` | Search and add a property |
| `/removeproperty <place_id>` | Stop monitoring a property |
| `/reviews` | Show recent reviews |
| `/checkreviews` | Manually trigger a review check |

Send any free-text message to chat with the active AI.

## Project Structure

```
main.py           — Entry point
bot.py            — Telegram command handlers and approval flow
llm.py            — LLM abstraction (Claude / ChatGPT)
security.py       — Auth, session PIN, rate limiting
db.py             — SQLite database (audit log, conversations, pending actions)
gmail_module.py   — Gmail OAuth and email operations
reviews.py        — Google Reviews polling via Places API
config.py         — Environment variable loading
```

## License

Private use.

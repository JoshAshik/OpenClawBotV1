# AGENTS.md

Guidance for AI-assisted work on Clawdbot — a single-user Telegram assistant bot
(python-telegram-bot v21, SQLite, OpenAI) for property management.

## Running & verifying

- `python main.py` — long-running polling bot; needs a filled `.env`. Use the venv:
  `.venv\Scripts\python.exe`.
- Tests: `pip install -r requirements-dev.txt` once, then `pytest -q` (no network; the iMessage
  tests build a synthetic `chat.db` from `tests/fixtures/make_chat_db.py` and point the reader at
  it with `IMESSAGE_DB_PATH`, and index tests use a temp bot DB with embeddings stubbed). Also run
  `python -m py_compile <files>` and the import/build check: `python -c "import bot; bot.build_app()"`.
- End-to-end checks call `llm.agent_chat(...)` directly (no Telegram needed) — this hits the real
  OpenAI API and the user's real `clawdbot.db`; keep such tests small and cheap.
- Dev box is Windows (Git Bash shell; beware cp1252 console output — pass `-X utf8` to Python).

## Hard constraints

- **OpenAI-only, by owner decision.** Never add Anthropic/Codex SDK calls or a model switcher.
  Models come from `config.OPENAI_MODEL` / `EMBEDDING_MODEL` / `VOICE_MODEL`.
- **Approval-flow invariant:** anything outbound (send/reply email, future iMessage/WhatsApp sends)
  must be returned from `agent_tools.execute_tool` as a pending `action`, surfaced as an
  Approve / ✏️ Edit / ❌ Reject card, and executed only in `bot.execute_approved_action`.
  Tools never send directly, and the model must never claim an email was sent.
- The draft **Edit loop may not change a reply's recipient or thread** — editable fields per action
  type are locked in `llm._EDITABLE_FIELDS`.
- All sync google-api / OneDrive calls must run through `asyncio.to_thread` — the bot, the
  background indexer, and the job queue share one event loop.
- The single authorized user is enforced by `security.auth_required`; keep it on every handler.

## Architecture (follow a message through)

1. **bot.py** — Telegram handlers. Free text/voice/photo/PDF → `_run_agent_turn`, which drives a
   `StatusMessage` (one Telegram message that shows tool steps, streams the answer, then becomes
   the final HTML-rendered reply via `telegram_format.markdown_to_telegram_html`).
2. **llm.py** — streaming OpenAI agent loop (`agent_chat`). Builds a dynamic system prompt
   (date/time, Gmail accounts, memories, signatures, working set of recently referenced ids),
   replays token-budgeted history, runs up to 10 tool rounds, persists the turn.
3. **agent_tools.py** — tool schemas + execution. Read tools return `result` (+ `entities` for the
   working set); write tools return `action` payloads. `action_detail()` renders approval cards.
4. **search_index.py / embeddings.py / indexer.py** — `hybrid_search` = FTS5 BM25 (sanitized
   queries, LIKE fallback) + embedding cosine (numpy over 512-dim vectors stored in SQLite),
   merged by reciprocal-rank fusion with a recency boost. `indexer` runs incremental Gmail sync
   (`newer_than:7d`, every `INDEX_POLL_INTERVAL_MINUTES`), a resumable daily mailbox backfill,
   conversation-window indexing for iMessage/WhatsApp, contact rebuilds, and inline embedding.
5. **gmail_module.py** — multi-account OAuth (`token_<label>.json` per account). Replies must keep
   using the RFC `Message-ID` header + `threadId` (recipient-side threading breaks otherwise).
6. **ops.py** — always-on plumbing: `/doctor` health checks, the nightly SQLite online backup,
   `/update` (`git pull --ff-only` + pip) and `/restart` (exit 0; launchd's `KeepAlive` restarts
   the bot — see `deploy/`). Background jobs record `last_index_at` / `last_backfill_at` /
   `last_backup_at` in `settings`. These are not outbound actions and bypass the approval flow,
   but every command is still `auth_required` with a confirm button.

## Conversation storage

`chat_turns.messages_json` holds provider-neutral messages:
`{role: user, content: str}` · `{role: assistant, content: [text | tool_use blocks]}` ·
`{role: tool_results, content: [{tool_use_id, content}]}` — converted to the OpenAI wire format in
`llm._to_openai_messages`. When trimming history, drop **whole turns** only, never split a
tool_use from its tool_results.

## Environment quirks to expect

- Gmail OAuth tokens go stale (`invalid_grant`) after inactivity: live mail ops, backfill, and the
  digest's unread section then degrade gracefully. The fix is the user running `/addaccount <label>`
  in Telegram — code changes can't fix it.
- iMessage/WhatsApp modules read Mac-only databases and are intentionally inert on Windows
  (`is_available()` gates). `IMESSAGE_DB_PATH` (env/config) points `imessage_module` at any
  chat.db, which is how the tests run on Windows. `UPGRADE_PLAN.md` Phases 7–12 cover the Mac move.
- `imessage_module` decodes `message.attributedBody` (typedstream) because `message.text` is
  usually NULL on current macOS; the indexer keeps a ROWID cursor in `settings.imessage_last_rowid`
  and keys conversation windows by chat (`chat:<id>` / `handle:<id>`), so group threads stay together.
- `clawdbot.db`, `token_*.json`, `credentials.json`, `.env`, `clawdbot.log` are live user data in
  the repo directory — don't commit, truncate, or overwrite them.

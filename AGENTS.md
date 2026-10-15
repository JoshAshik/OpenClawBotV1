# AGENTS.md

Guidance for AI-assisted work on Clawdbot — a single-user Telegram assistant bot
(python-telegram-bot v21, SQLite, OpenAI) for property management.

## Running & verifying

- `python main.py` — long-running polling bot; needs a filled `.env`. Use the venv:
  `.venv\Scripts\python.exe`.
- Tests: `pip install -r requirements-dev.txt` once, then `pytest -q` (no network; the iMessage/Contacts
  tests build a synthetic `chat.db` from `tests/fixtures/make_chat_db.py` and point the reader at
  it with `IMESSAGE_DB_PATH`, and index tests use a temp bot DB with embeddings stubbed). Also run
  `python -m py_compile <files>` and the import/build check: `python -c "import bot; bot.build_app()"`.
- Evals: `python -X utf8 -m evals.run` scores retrieval (hit@1/5, MRR) for the cases in
  `evals/cases.jsonl` against the live index (a few cents of embeddings); `--live` also drives the
  real agent on a handful of cases and reports the cost. Re-run after search/index/prompt changes;
  `python -m evals.extract` re-mines cases from real conversations (read-only, manual cases kept).
- End-to-end checks call `llm.agent_chat(...)` directly (no Telegram needed) — this hits the real
  OpenAI API and the user's real `clawdbot.db`; keep such tests small and cheap.
- Dev box is Windows (Git Bash shell; beware cp1252 console output — pass `-X utf8` to Python).

## Hard constraints

- **OpenAI-only, by owner decision.** Never add Anthropic/Codex SDK calls or a model switcher.
  Models come from `config.OPENAI_MODEL` / `EMBEDDING_MODEL` / `VOICE_MODEL`.
- **Approval-flow invariant:** anything outbound (send/reply email, `send_imessage`) must be
  returned from `agent_tools.execute_tool` as a pending `action`, surfaced as an
  Approve / ✏️ Edit / ❌ Reject card, and executed only in `bot.execute_approved_action`.
  Tools never send directly, and the model must never claim an email or text was sent.
  iMessage sends go through `imessage_send.send_message` (osascript, body passed as argv, Mac
  only — needs Automation → Messages granted once). **WhatsApp is read-only by owner decision:**
  never add a `send_whatsapp` tool or action.
- The draft **Edit loop may not change a reply's recipient or thread** — editable fields per action
  type are locked in `llm._EDITABLE_FIELDS`.
- All sync google-api / OneDrive calls must run through `asyncio.to_thread` — the bot, the
  background indexer, and the job queue share one event loop.
- Authorization is enforced by `security.auth_required` (keep it on every handler) against
  `config.TELEGRAM_AUTHORIZED_USER_IDS` — a comma-separated list in `.env`. The first id is the
  owner (`config.TELEGRAM_AUTHORIZED_USER_ID`): every proactive push targets that id only.

## Architecture (follow a message through)

1. **bot.py** — Telegram handlers. Free text/voice/photo/PDF → `_run_agent_turn`, which drives a
   `StatusMessage` (one Telegram message that shows tool steps, streams the answer, then becomes
   the final HTML-rendered reply via `telegram_format.markdown_to_telegram_html`).
2. **llm.py** — streaming OpenAI agent loop (`agent_chat`). Builds a dynamic system prompt
   (date/time, Gmail accounts, memories, session summaries, signatures, working set of recently
   referenced ids), replays token-budgeted history, runs up to 10 tool rounds, persists the turn.
   `build_system_prompt(user_message)` takes the user's message so memories can be picked by
   relevance; `compact_history` is applied to the replayed history (see 15).
   `_openai_agent_loop` dispatches on `config.OPENAI_API_MODE`: **`responses`** (default) is
   `_responses_agent_loop` — `client.responses.create(stream=True)` with the system prompt as
   `instructions`, the stored turns mapped by `_to_responses_input` (user/assistant text,
   `function_call` / `function_call_output` items keyed by the stored tool_use id), our
   function tools from `agent_tools.get_responses_tools()` (strict where every property is
   required) plus the built-in `web_search` tool, `reasoning.effort` from
   `OPENAI_REASONING_EFFORT`, and `previous_response_id` chaining so later rounds send only the
   `function_call_output` items and reasoning state carries across tool rounds. Text deltas
   stream to `on_text`, function calls are read from the completed response, usage from its
   `usage`, and `url_citation` annotations become markdown links via `render_citations`.
   **`chat`** is the old chat.completions loop (`_chat_agent_loop`, `_to_openai_messages`), kept
   as a reversible fallback with no web search. Both return the same
   `(result, turn_norm, entities, totals)`; `result` is `{text, actions, files}`.
3. **agent_tools.py** — tool schemas + execution. Read tools return `result` (+ `entities` for the
   working set); write tools return `action` payloads. `action_detail()` renders approval cards.
   `generate_image` (read tool, not outbound) calls `llm.generate_image` (gpt-image-1) and returns
   `files` = `[{kind: photo, bytes, filename, caption}]`, which `_run_tools` collects into the
   loop's `files` sink and `bot._send_files` sends as Telegram photos after the answer. Web
   images are only ever returned as links (prompt rule), never downloaded.
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
7. **contacts_module.py** — Apple Contacts reader (Mac only; `CONTACTS_DB_PATH` points it at a
   fixture on Windows). Normalizes phones/emails/WhatsApp JIDs with `normalize_handle`, caches a
   `ContactResolver` for an hour, and hands `resolve` to the indexer as `resolve_name` so message
   windows are titled by person. `indexer.rebuild_contacts` merges Apple Contacts + Gmail headers +
   indexed message handles into one `contacts` row per person (emails, phones, channels JSON).
8. **Message tools** — `get_conversation` (agent_tools) reads a live timeline via
   `imessage_module.get_timeline`, not the index; `indexer.watch_imessage_once` is the 30 s
   chat.db-mtime watcher behind the `imessage_watch` job, serialized with the main indexer by
   `indexer._imessage_lock` (always call `index_imessage`, never `_index_imessage_unlocked`).
9. **reminders.py** — `set_reminder` / `/reminders` / the minutely `reminders` job. Reminders are
   **not outbound** (they only message the owner), so by design they execute directly and skip
   the approval flow; buttons use callback prefix `rem_`. Times are UTC in the DB, local in text.
10. **waiting_on.py** — "sent by me, no reply" detection from the index (Gmail threads + 1:1
    iMessage windows), dismissals in `waiting_on_dismissed`, and `build_nudge_action`, which
    returns a pending reply_email / send_imessage action for `bot.request_approval` — the Nudge
    button is just another way to open an approval card. `agent_tools.build_reply_action` /
    `build_imessage_action` are the shared payload builders; use them for any new follow-up path.
11. **alerts.py** — urgent inbound triage. `indexer.note_new_item` feeds `indexer.new_items`;
    the `alerts` job drains it, filters to inbound, dedupes with `alerts_sent`, applies the
    watch-list (memories beginning "ALERT:" / "always alert" — the system prompt tells the
    model to store them that way) and asks `config.OPENAI_FAST_MODEL` (strict JSON schema) for
    the rest. ✍️ Reply builds a pending action via the shared builders — approval card as usual.
    Quiet hours hold alerts in `alerts.held`; the digest force-flushes them.
12. **digest.py** — morning briefing / evening wrap-up: `build_sections(kind)` runs each source
    through `_safe` so a dead Gmail token or missing calendar only drops that section;
    `run()` composes with the fast model (`llm.compose_digest(raw, kind)`), sends, then the
    waiting-on cards, then `alerts.flush_held(force=True)`. Calendar is an optional import of
    `calendar_module` with `is_available()` / `list_events(after_iso, before_iso)`.
13. **Engine notes** — `llm._run_tools` runs read tools in parallel (semaphore
    `MAX_PARALLEL_TOOLS`) and `WRITE_TOOLS` sequentially, preserving call order; add any new
    state-changing tool to `llm.WRITE_TOOLS`. Every OpenAI call must go through
    `llm.record_usage(purpose, model, response.usage)` (Responses stream: the
    `response.completed` event's `usage`; chat stream: pass `stream_options={"include_usage":
    True}` and read the final chunk's `usage`); costs come from `config.PRICES_PER_MTOK`, which
    the owner keeps in sync with OpenAI pricing. Non-token entries: `per_image` (purpose
    `image`, count in completion_tokens) and the TTS model priced per character (purpose `tts`,
    characters in prompt_tokens).
    **Spoken replies:** `bot._run_agent_turn(..., incoming_voice=True)` marks transcribed voice
    notes; after the text reply, `llm.should_speak(mode, incoming_voice, say_it)` (mode from
    `/voice on|off|auto` in `settings.voice_reply_mode`, `say_it` from `llm.wants_spoken_reply`)
    decides whether `llm.synthesize_speech` (markdown stripped, ≤ `VOICE_OUT_MAX_CHARS`, Ogg/Opus
    from `VOICE_OUT_MODEL`) is sent as a Telegram voice note. Not outbound — owner only.
14. **Resilience rules** — every OpenAI call goes through `llm.with_retry(lambda: ..., purpose)`;
    every Telegram send/edit outside a handler's direct reply goes through
    `telegram_format.tg_retry`; agent turns run under `bot._TURN_LOCKS[user]` with
    `TURN_TIMEOUT_SECONDS`; the DB is WAL (`db.apply_pragmas`) — never set journal_mode elsewhere.
15. **memory.py — the memory pipeline (Memory 2.0).** Three layers, all on the fast model and
    `embeddings.embed_texts`, none on the reply's critical path (`llm._background` =
    fire-and-forget task; tests patch it to await inline):
    - *Extraction:* after `save_turn`, `memory.extract_and_store(user_message, answer)` asks
      `OPENAI_FAST_MODEL` (strict json_schema) for 0–3 durable facts and dedupes each against
      `memory_embeddings` by cosine: ≥ 0.92 skip, 0.80–0.92 merge (short LLM rewrite keeps the
      newer detail, row updated in place), else insert with `memories.source='auto'`. The
      `remember` tool still exists (`source='explicit'`, stored via `memory.store_memory` so it
      is embedded too). `MEMORY_AUTO_EXTRACT=0` turns extraction off.
    - *Retrieval:* `memory.select_memories(query)` = top `MEMORY_TOP_K` (12) by similarity to the
      current message + the 5 newest, 2000-char cap; falls back to the newest 30 without
      embeddings. Memories without a vector are embedded lazily (`ensure_embeddings`).
    - *Episodic:* `memory.summarize_turns` writes a `session_summaries` row (≤ 120 words + key
      entities) for turns that `db.get_turn_messages_with_dropped` dropped from the budget and,
      via `memory.close_session`, for a session replaced by `/new` or the idle reset.
      `settings.summarized_through:<session>` stops re-summarizing the same turns. The prompt
      shows the 3 newest under "EARLIER CONVERSATIONS"; `/memories` and `/summaries` list them.
    - *Compaction rule:* `llm.compact_history` truncates `tool_results` content older than the
      2 most recent user turns to 400 chars + `[truncated — ask again to re-read]` **in the
      copy sent to the model only**: no message is removed, no `tool_use_id` changes, and
      `chat_turns` keeps the full results. Memory work is not outbound — no approval flow.

## Conversation storage

`chat_turns.messages_json` holds provider-neutral messages:
`{role: user, content: str}` · `{role: assistant, content: [text | tool_use blocks]}` ·
`{role: tool_results, content: [{tool_use_id, content}]}` — converted per request to Responses
`input` items by `llm._to_responses_input` (the tool_use id becomes `call_id` on both the
`function_call` and its `function_call_output`) or to the chat wire format by
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
- The `contacts` table is derived data: `db._migrate_contacts` drops and recreates it when the schema
  changes, and every index cycle rebuilds it, so never store hand-entered data there.
- `clawdbot.db`, `token_*.json`, `credentials.json`, `.env`, `clawdbot.log` are live user data in
  the repo directory — don't commit, truncate, or overwrite them.

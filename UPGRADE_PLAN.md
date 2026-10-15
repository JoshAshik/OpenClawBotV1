# Clawdbot — Mac mini upgrade plan (Phases 7–12)

Continues `IMPROVEMENT_PLAN.md` (Phases 0–5 done, Phase 6 open). Target: Clawdbot runs 24/7 on a
Mac mini, message search and iMessage sending finally work, and the bot becomes proactive and
property-management-aware. Written 2026-10-15.

**Decisions from the owner (2026-10-15):** the Mac mini has a display (one-time setup is done at
the machine); WhatsApp is **read-only**; after messaging, **proactivity** comes first; calendar is
**Apple Calendar** (EventKit). Telegram stays the only chat surface.

## Where things stand

- **The whole Phase 0–5 rebuild is uncommitted.** 12 modified files (+2,350 / −444 lines) and 5
  untracked files (`embeddings.py`, `telegram_format.py`, `CLAUDE.md`, `AGENTS.md`,
  `IMPROVEMENT_PLAN.md`). Last commit is "PDF Reading Fix". Nothing moves to the Mac until this
  is committed.
- `clawdbot.log` (10.8 MB, 71k lines): 1,654 `invalid_grant` (Gmail tokens expired), 1,642
  iMessage and 2,463 WhatsApp failures — all "Mac-only path not found". Index has zero messages.
- Dev box is Windows, Python 3.14 venv. The code uses `pathlib` and `asyncio.to_thread`
  throughout, so nothing is Windows-specific; the Mac-only modules are gated by `is_available()`.
- Known defects in the Mac modules, found by reading them against current macOS:
  1. `imessage_module` reads only `message.text`. Since macOS Ventura most rows have `text = NULL`
     and the body lives in the `attributedBody` blob (NSAttributedString typedstream). On a
     current Mac the reader would return a small fraction of messages.
  2. Handles are raw phone numbers / emails; there is no name resolution, so windows are titled
     "iMessage with +14155551234" and `find_contact` knows nothing about phones.
  3. Group chats are ignored (`handle_id` is the *sender*, not the chat). Reactions/tapbacks
     (`associated_message_type` 2000+) would be indexed as text ("Loved ...").
  4. `whatsapp_module` targets the native Mac App Store app's `ChatStorage.sqlite`; the schema is
     plausible but has never been run against a real DB, and group-chat sender names
     (`ZWAGROUPMEMBER`) aren't handled.
  5. Both readers re-scan "last 500" every poll instead of keeping a ROWID / Z_PK cursor.
- `/addaccount` runs `InstalledAppFlow.run_local_server`, which opens a browser *on the machine
  running the bot*. Fine on a Mac with a display: run it once while sitting at the Mac.
- The repeated `invalid_grant` is almost certainly the Google OAuth consent screen being in
  **Testing** status: refresh tokens for Testing apps expire after 7 days. Flipping the app to
  **In production** (unverified is fine for a personal app; Google shows a one-time warning)
  makes tokens last indefinitely. No code change can fix this.

---

## Phase 7 — Land it and move it (deployment)   ~1–2 days

- [x] **Commit the rebuild** on `main` (104a0c0). `requirements.lock` generated from the
      dependency closure of `requirements.txt` (not a raw freeze — the venv still had a stale
      `anthropic` package).
- [ ] **Mac install** — steps written up in `deploy/MAC_SETUP.md`; to be done at the Mac:
      Python 3.14, venv, `pip install -r requirements.lock`, copy `.env`, `credentials.json`,
      `token_*.json`, `clawdbot.db` by hand (gitignored, never commit).
- [ ] **launchd LaunchAgent** — template at `deploy/com.clawdbot.bot.plist` (`RunAtLoad`,
      `KeepAlive`, `ThrottleInterval` 10, `ProcessType Interactive`, logs to
      `~/Library/Logs/clawdbot/`); install commands in `MAC_SETUP.md` §7. Auto-login, `pmset`,
      restart-after-power-failure in §1. To be done at the Mac.
- [x] **`/doctor` command** (`ops.run_doctor`): Gmail token per account (refreshes, flags
      `invalid_grant` with the Testing-status hint), OpenAI reachable, chat.db / WhatsApp /
      Contacts DB opened read-only (➖ on Windows, permission-denied → "grant Full Disk Access"),
      disk free, DB/log/backup-dir sizes, last index/backfill/backup timestamps, job queue next
      runs. Verified on Windows: Gmail ❌ (expired tokens, expected), OpenAI ✅, Mac sources ➖.
- [ ] **Full Disk Access / Automation grants** — `MAC_SETUP.md` §6 (grant to the `readlink -f`
      target of `.venv/bin/python`). To be done at the Mac.
- [ ] **Gmail token longevity** — flip the OAuth app to "In production", then `/addaccount` at
      the Mac (`MAC_SETUP.md` §5). `/doctor` now prints the Testing-status hint on
      `invalid_grant`.
- [ ] **One-time macOS setup at the display** — `MAC_SETUP.md` §1 (Messages sign-in + Text
      Message Forwarding, WhatsApp link, Calendar sync).
- [x] **Ops basics:** `RotatingFileHandler` (10 MB × 5, `clawdbot.log` stays the active file);
      nightly SQLite online backup (`ops.backup_database`, `BACKUP_HOUR` 03:00 → `backups/`,
      `quick_check` verified, keeps `BACKUP_KEEP`=14, Telegram alert on failure); `/update`
      (confirm button → `git pull --ff-only` → `pip install` → exit 0 for launchd) and `/restart`
      (confirm → exit 0); "🟢 Clawdbot started" ping in `post_init`; `/status` shows uptime,
      model, last index/backfill/backup; the index and backfill jobs record their timestamps.
- **Check:** bot survives a reboot without touching the Mac; `/doctor` is all green; Gmail still
  works 8+ days after connecting.

## Phase 8 — Messages for real (the moat)   ~3–5 days

- [x] **iMessage reader rewrite** (`imessage_module.py`) — done on Windows against the synthetic
      fixture `tests/fixtures/make_chat_db.py` (`pytest -q`, 32 tests); needs a run against the
      real chat.db at the Mac:
      - [x] Decode `attributedBody` (typedstream → the NSString payload); `text` wins when present.
      - [x] Join `chat_message_join` → `chat` for group chats: `chat.display_name` /
        `chat_identifier`, sender from `handle` per message; `is_from_me` rows have `handle_id = 0`.
      - [x] Skip tapbacks (`associated_message_type >= 2000`), carry `service` (iMessage/SMS) and
        attachment filenames (`message_attachment_join` → `attachment`).
      - [x] Incremental cursor: `settings.imessage_last_rowid` via `read_batch(after_rowid, limit)`;
        first run walks the whole DB in pages of `IMESSAGE_BATCH_SIZE` (2000); a new batch is
        widened with the earlier messages of each chat it touches (`get_chat_context`) so an open
        window re-indexes whole under the same `source_id`.
      - [x] Conversation windows keyed by *chat* (`chat:<id>`, `handle:<id>` for chat-less rows);
        titles use `display_name` for groups and the handle for 1:1 — `resolve_name(handle)` on
        `indexer.build_conversation_windows` is the hook for the Contacts task below.
- [x] **Contacts name resolution** (`contacts_module.py`): reads the root store and every
      `Sources/*/AddressBook-v22.abcddb` (`ZABCDRECORD`, `ZABCDPHONENUMBER`, `ZABCDEMAILADDRESS`),
      normalizes phones to E.164 (`CONTACTS_DEFAULT_COUNTRY`), lower-cases emails, folds WhatsApp
      JIDs, and caches a resolver for an hour. iMessage and WhatsApp windows are titled by name
      with the raw `handle`/`handles` kept in metadata; `rebuild_contacts` merges Apple Contacts +
      Gmail headers + indexed message handles into one `contacts` row per person (emails, phones,
      channels, count, last_seen), and `find_contact` returns phones and channels. Fixture in
      `tests/fixtures/make_contacts_db.py`, 32 tests. Verified on Windows only; the real store
      needs Full Disk Access on the Mac (`/doctor` checks it).
- [ ] **WhatsApp reader hardening:** probe the schema on first run on the real Mac and log the
      exact table/column set; add `ZWAGROUPMEMBER` for group senders, media placeholders, a Z_PK
      cursor, and a clear Telegram warning (once) if the schema doesn't match.
- [x] **`send_imessage` tool behind the approval flow:** `agent_tools.resolve_message_recipient`
      turns a name or handle into one E.164/email handle (ambiguous → asks which, no action);
      the card shows "Name (+1 415…) via iMessage"; `_EDITABLE_FIELDS["send_imessage"] =
      ("body",)`; `execute_approved_action` → `imessage_send.send_message` (`osascript` with the
      body as an argv item, 30 s timeout, Automation-permission hint on -1743) in a thread, audit
      logged; shares `RATE_LIMIT_PER_HOUR`; on Windows it reports "needs the Mac" and sends
      nothing. 14 tests. **The live send is unverified until the Mac** (needs Automation →
      Messages granted once at the display). Group-chat sends (by `chat id`) as a follow-up.
- [x] WhatsApp stays **read-only** by owner decision — no `send_whatsapp` tool; the stub in
      `execute_approved_action` is gone and the system prompt says WhatsApp can't be sent to.
- [x] **`get_conversation` tool:** `imessage_module.get_timeline` reads the newest N messages
      for a person (their 1:1 chat both ways plus what they sent in groups) or a group chat by
      display name, with after/before filters, straight from chat.db; senders are named through
      Contacts; the result carries an `imessage_chat` entity for the working set. Ambiguous
      names ask back. WhatsApp path uses the read-only reader (unverified until the Mac).
- [x] **Near-realtime indexing:** `imessage_watch` job every `IMESSAGE_WATCH_SECONDS` (30; 0
      disables) stats chat.db and chat.db-wal and runs `index_imessage` only when an mtime moved;
      an `asyncio.Lock` serializes it with the 10-minute indexer, and a change seen while a pass
      runs is retried next tick. Silent when idle. This is what Phase 10 alerts hook into.
- **Check:** the Phase 6 check — a WhatsApp/iMessage question ChatGPT structurally cannot answer,
  answered correctly, plus "text Mike that the plumber comes Tuesday" → card → approve → sent.

## Phase 9 — Engine and reliability   ~2–3 days

- [x] **Parallel tool execution:** `llm._run_tools` runs a round's read tools concurrently
      under a `MAX_PARALLEL_TOOLS` (4) semaphore, then the `WRITE_TOOLS` (draft/reply/send,
      remember, reminders) one at a time, and emits results in the original call order so
      tool_use/tool_results pairing is untouched; a crashing tool becomes a "Tool failed" result.
- [ ] **Responses API migration** for the agent loop: native for GPT-5.x, reasoning state carries
      across tool rounds (better multi-step tool use), server-side prompt caching. Keep the
      provider-neutral `chat_turns` format; only `_to_openai_messages` and the loop change.
      Expose `OPENAI_REASONING_EFFORT` (default `low` for chat latency) and `OPENAI_FAST_MODEL`
      (`gpt-5-mini`-class) for internal calls: draft revision, digest, image OCR, urgency triage.
- [x] **Strict structured outputs:** `llm.revise_draft` sends a strict `json_schema` built from
      `_EDITABLE_FIELDS` (`llm.schema_for`), so a reply/text revision cannot even return a
      recipient; falls back to `json_object` if the API rejects the schema. Urgency triage was
      already strict (`alerts._SCHEMA`); the digest stays free text by design.
- [x] **Usage and cost:** every OpenAI call records to `llm_usage` via `llm.record_usage`
      (streaming agent rounds use `stream_options.include_usage`; embeddings, voice, vision,
      revise, digest, triage, follow-ups all covered); agent turns also store
      prompt/completion tokens and cost on `chat_turns` (guarded ALTER migration). Prices live
      in `config.PRICES_PER_MTOK` (placeholders — **update from OpenAI's pricing page**; unknown
      models are flagged). `/usage` shows today and month-to-date by purpose and model plus the
      five most expensive turns; `/status` shows today's spend. 9 tests.
- [x] **Eval set:** `evals/extract.py` mines `chat_turns` read-only into `evals/cases.jsonl`
      (query → expected source_id, only turns that opened with a search/read; emails, phones
      and key-like tokens redacted; `manual` cases preserved); `python -m evals.run` scores
      hit@1 / hit@5 / MRR against the live index and `--live` drives the real agent on a few
      cases reporting cost. **Baseline 2026-10-15 (commit 7032c05 + working tree), offline,
      3 mined cases: hit@1 33%, hit@5 67%, MRR 0.40.** Hits: "most recent email about rent /
      invoice" (rank 5), "who sent the tuition invoice" (rank 1). Miss: "receipts from AI
      companies" — the agent only found it by rewriting the query with vendor names, so plain
      hybrid search doesn't surface it; a candidate for query expansion. Only 7 real turns
      exist yet — add manual cases (see evals/README.md) before treating the number as a trend.
- [x] **Unit tests (pytest, no network):** 175 across `tests/`: `telegram_format` (inline,
      blocks, escaping, malformed input), the FTS5 sanitizer (quoted MATCH accepted by a real
      FTS5 table, LIKE fallback on syntax error), `_to_openai_messages`, the token-budgeted
      history (whole turns only, tool_use/tool_results never split), the `_EDITABLE_FIELDS`
      lock, the typedstream decoder, phone normalization, windows, reminders, alerts, digest,
      engine, robustness, evals. `CLAUDE.md` says how to run them.
- [x] **Robustness:** `llm.with_retry` (0.5/1/2/4 s backoff, Retry-After honoured, 429/5xx/
      connection/timeout only) wraps every OpenAI call incl. the agent stream, embeddings and
      triage; both OpenAI clients get `OPENAI_TIMEOUT_SECONDS` and SDK retries off. A turn is
      bounded by `TURN_TIMEOUT_SECONDS` (friendly error + Retry) and a per-user lock
      serializes turns ("Still working on your last message…"). `telegram_format.tg_retry`
      handles flood control (RetryAfter) and "not modified" on status edits, approval cards
      and every job send. Both SQLite connections run `journal_mode=WAL`, `busy_timeout=5000`,
      `synchronous=NORMAL`; backups verified while another connection holds a write lock.
      12 tests.
- **Check:** eval hit rate ≥ 9/10; `/usage` matches the OpenAI dashboard within a few percent.

## Phase 10 — Proactivity   ~2–3 days

- [x] **Reminders tool:** `set_reminder(when, text)` / `list_reminders` / `cancel_reminder`
      (direct-execute: a reminder only messages the owner, so no approval card); `reminders`
      table (UTC `due_at`, `fired_at` guards against refiring, `snoozed_from`); the `reminders`
      job fires due ones every minute as "⏰ Reminder: …" with Snooze 1h / Tomorrow
      (`REMINDER_HOUR`, default 09:00) / Done buttons; `/reminders` lists; the system prompt
      makes the model pass an absolute local time. 16 tests.
- [x] **Waiting-on tracker:** `waiting_on.list_waiting` finds, from the index alone, Gmail
      threads whose newest message is mine (to a real person, not noreply) and 1:1 iMessage
      windows ending with "Me:", both older than `WAITING_ON_DAYS` (3). Surfaces in the digest
      (section + up to 5 cards) and `/waiting`, with ✍️ Nudge (LLM-drafted follow-up presented
      as a reply_email / send_imessage approval card — never sent directly) and 🙈 Dismiss
      (`waiting_on_dismissed`). `list_waiting_on` tool for the model. 9 tests.
- [x] **Urgent alerts:** `indexer.new_items` queues every freshly indexed email / message window
      (≤ `ALERT_MAX_AGE_HOURS` old, so backfills never alert); the `alerts` job every
      `ALERT_POLL_MINUTES` (3) runs a light inbound sync (Gmail `newer_than:1d`, iMessage) and
      `alerts.process`: inbound only, deduped via `alerts_sent`, watch-list hits (memories saved
      as "ALERT: …") urgent outright, the rest triaged by `OPENAI_FAST_MODEL` with a strict JSON
      schema (emergency / payment / legal / moveout / routine). Pushed as 🚨 cards with ✍️ Reply
      (acknowledgement drafted → reply_email / send_imessage approval card) and 👀 Seen; held
      during `ALERT_QUIET_HOURS` (23-7) and flushed by the next poll or the digest. `/alerts
      [on|off]`. 13 tests. **Live classification quality is unverified** — tune the triage
      prompt once real mail flows on the Mac.
- [ ] **Apple Calendar** (`calendar_module.py`, Mac only, via `pyobjc-framework-EventKit`):
      request full calendar access once (TCC prompt at the display), then `list_events(after,
      before, calendar?)` reads every iCloud/local calendar, and `create_event(title, start, end,
      location?, notes?, calendar?)` returns an **action** so the approval card shows the event
      before it's written. Events created by the bot get a "via Clawdbot" note. Digest opens
      with today's schedule; "when am I free Thursday afternoon?" answered from events. Inert on
      Windows like the other Mac modules. AppleScript (`tell application "Calendar"`) as the
      fallback if EventKit access is refused.
- [x] **Digest 2.0:** `digest.py` gathers each section in isolation (one failure never kills the
      briefing): today's calendar (optional `calendar_module.list_events` hook — **fills in once
      the Mac EventKit module exists**), reminders due/overdue, urgent alerts (held + last 24h),
      waiting-on, unread per account with inbox counts, new reviews, pending cards; composed by
      the fast model, then waiting-on cards, then held alerts flushed. Optional evening wrap-up
      at `EVENING_DIGEST_HOUR` (waiting-on, due tomorrow, alerts since morning, pending);
      `/digest [evening]` previews on demand. 8 tests.
- **Check:** "remind me Thursday to chase the deposit" fires on Thursday; an urgent tenant text
  reaches Telegram within 3 minutes.

## Phase 11 — Property-management brain   ~3–4 days

- [ ] **Property knowledge base:** extend `properties` (address, units, tenants, owner, vendors,
      lease start/end, rent, notes) with `get_property` / `update_property` tools; the model links
      emails/messages to properties by address and nickname (memories already hold nicknames).
- [ ] **Unified directory:** Gmail contacts + Apple Contacts + WhatsApp partners → one person
      with email, phones, and preferred channel. "Message Mike" picks iMessage vs email vs
      WhatsApp automatically; the approval card says which.
- [ ] **Maintenance tickets:** `open_ticket` / `list_tickets` / `close_ticket`; a ticket links the
      originating email/message, property, vendor, status; digest lists tickets open > N days.
- [ ] **Lease and rent dates:** one-time LLM extraction from indexed lease PDFs into
      `properties`; automatic reminders 60/30 days before expiry; "did unit 2 pay?" answered from
      indexed bank/portal emails.
- [ ] **Reviews:** new-review alert card gets a "Draft reply" button (reply text to paste; the
      Business Profile API needs separate verification, so posting is out of scope).
- [ ] **Telegram files as attachments:** documents/photos sent to the bot are stored so "attach
      the photo I just sent" works in a draft.
- **Check:** "who's the tenant at 12 Oak, when does their lease end, and have they paid this
  month?" answered in one turn.

## Phase 12 — Always-on hygiene   ~1 day

- [ ] Secrets: `.env`/tokens outside any iCloud-synced folder, `chmod 600`; optional Keychain via
      `keyring` for the API keys.
- [ ] Remote admin: SSH + Tailscale to reach the Mac; `/restart`, `/update`, `/logs 50`.
- [ ] Watchdog: in-bot heartbeat; if Telegram polling has errored for > 5 min, exit so launchd
      restarts; weekly `VACUUM` + integrity check in the backup job.
- [ ] Audit-log retention (keep 1 year), `/lock` auto-arm after N idle hours (optional).

---

## Suggested order

**7 → 8 → 10 → 9 → 11 → 12.** Phases 7 and 8 turn the moat on; Phase 10 (proactivity) is the
owner's top pick after that. Two Phase 9 items are cheap enough to pull forward into Phase 8's
work: parallel tool execution and SQLite WAL mode (the message backfill will hammer the DB).

What can be built on Windows before the move (everything with a mockable data source): the
typedstream decoder and iMessage/Contacts readers against copied `chat.db` /
`AddressBook-v22.abcddb` fixtures, reminders, waiting-on tracker, urgent triage, cost tracking,
tests, eval set. What needs the Mac: launchd, TCC permissions, WhatsApp schema probe, `osascript`
sending, EventKit, and the end-to-end checks.

## Still open (not blocking)

1. **macOS version** on the mini — EventKit full-access API and the `attributedBody` format both
   changed at macOS 14; the plan assumes 14 or newer.
2. **Google OAuth consent screen:** is it in "Testing"? If so, flip to "In production" before
   reconnecting accounts, or tokens will keep dying after 7 days.
3. **Budget:** keep `gpt-5.1` as the default chat model, or go bigger for quality / smaller for
   cost? A monthly ceiling would set the alert threshold in `/usage`.
4. **Where the Mac lives:** if it's on a different network than you usually are, Tailscale (Phase
   12) moves up so `/update` and SSH work from anywhere.

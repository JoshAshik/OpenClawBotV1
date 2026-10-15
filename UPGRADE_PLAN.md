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

- [ ] **iMessage reader rewrite** (`imessage_module.py`):
      - Decode `attributedBody` (typedstream → the NSString payload) with `text` as fallback.
      - Join `chat_message_join` → `chat` for group chats: `chat.display_name` / `chat_identifier`,
        sender from `handle` per message; `is_from_me` rows have `handle_id = 0`.
      - Skip tapbacks (`associated_message_type >= 2000`), carry `service` (iMessage/SMS) and
        attachment filenames (`message_attachment_join` → `attachment`).
      - Incremental cursor: `settings.imessage_last_rowid`; initial backfill walks the whole DB in
        pages (tens of thousands of rows is fine — embeddings cost cents).
      - Conversation windows keyed by *chat*, not handle, so group threads stay together.
- [ ] **Contacts name resolution** (`contacts_module.py`, Mac only): read the Contacts store
      (`~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb`: `ZABCDRECORD`,
      `ZABCDPHONENUMBER`, `ZABCDEMAILADDRESS`), normalize phones to E.164, map handle → name.
      Used for window titles ("iMessage with Mike Plumber"), the `contacts` table (merged with
      Gmail-derived contacts: one row per person with email + phones + channels), and
      `find_contact` (returns phone and which channel they usually use). Rebuild hourly.
- [ ] **WhatsApp reader hardening:** probe the schema on first run on the real Mac and log the
      exact table/column set; add `ZWAGROUPMEMBER` for group senders, media placeholders, a Z_PK
      cursor, and a clear Telegram warning (once) if the schema doesn't match.
- [ ] **`send_imessage` tool behind the approval flow:** tool returns an `action` (`to` resolved
      via contacts to a handle, `body`, `service`); the approval card shows "Name (+1 415…)";
      `_EDITABLE_FIELDS["send_imessage"] = ("body",)`; `execute_approved_action` runs
      `osascript` (`tell application "Messages" … send … to participant …`) in a thread with a
      timeout; shares `RATE_LIMIT_PER_HOUR`. Group-chat sends (by `chat id`) as a follow-up.
- WhatsApp stays **read-only** by owner decision — no `send_whatsapp` tool; the existing stub in
  `execute_approved_action` is removed so the model can't be tempted to use it.
- [ ] **`get_conversation` tool:** live timeline for one contact/chat with `after/before/limit`
      straight from chat.db / ChatStorage, not just index windows — "what did Mike say last week
      about the boiler" gets the complete exchange with timestamps.
- [ ] **Near-realtime indexing:** poll `chat.db-wal` mtime every 30 s (cheap) and index new rows
      immediately; this is also what Phase 10 alerts hook into.
- **Check:** the Phase 6 check — a WhatsApp/iMessage question ChatGPT structurally cannot answer,
  answered correctly, plus "text Mike that the plumber comes Tuesday" → card → approve → sent.

## Phase 9 — Engine and reliability   ~2–3 days

- [ ] **Parallel tool execution:** when a round returns several read tools, run them with
      `asyncio.gather` (results kept in call order); write tools stay sequential.
- [ ] **Responses API migration** for the agent loop: native for GPT-5.x, reasoning state carries
      across tool rounds (better multi-step tool use), server-side prompt caching. Keep the
      provider-neutral `chat_turns` format; only `_to_openai_messages` and the loop change.
      Expose `OPENAI_REASONING_EFFORT` (default `low` for chat latency) and `OPENAI_FAST_MODEL`
      (`gpt-5-mini`-class) for internal calls: draft revision, digest, image OCR, urgency triage.
- [ ] **Strict structured outputs** (json_schema) for `revise_draft` and digest/urgency triage.
- [ ] **Usage and cost:** store `usage` per request (prompt / cached / completion tokens) on
      `chat_turns`; `/usage` shows today and month-to-date in $ from a price table in `config`.
      (Cross-cutting item from the old plan, never done.)
- [ ] **Eval set:** `evals/cases.jsonl` built from real past queries (query → expected
      source_id / expected tool); `python -m evals` reports hit rate against the live index. Run
      after each phase.
- [ ] **Unit tests (pytest, no network):** `telegram_format`, FTS sanitizer, conversation
      windows, `_to_openai_messages`, the `_EDITABLE_FIELDS` invariant, typedstream decoder, phone
      normalization. Update `CLAUDE.md` ("there is no test suite" → how to run it).
- [ ] **Robustness:** retry/backoff on OpenAI 429/5xx and Telegram `RetryAfter`; a per-turn
      timeout; a concurrency guard (second message while a turn runs → "still working on the
      last one" or queue); SQLite WAL mode + `busy_timeout` for `clawdbot.db` under indexer load.
- **Check:** eval hit rate ≥ 9/10; `/usage` matches the OpenAI dashboard within a few percent.

## Phase 10 — Proactivity   ~2–3 days

- [ ] **Reminders tool:** `set_reminder(when_iso, text)` → `reminders` table → job queue fires a
      Telegram message with Snooze 1h / Tomorrow / Done buttons; `/reminders` lists; model
      resolves "Friday 9am" against the prompt's current time.
- [ ] **Waiting-on tracker:** sent emails (and sent iMessages) with no reply after N days
      surface in the digest with a "Nudge" button that drafts a follow-up (approval card).
- [ ] **Urgent alerts:** incremental inbound sync every 2–3 min; the fast model classifies new
      inbound (email, iMessage, WhatsApp) as urgent (emergency repair, payment failure, legal,
      move-out) vs. routine; urgent → immediate alert with a Reply button; routine waits for the
      digest. Watch-list of contacts/keywords in memory ("always alert me about Unit 4").
- [ ] **Apple Calendar** (`calendar_module.py`, Mac only, via `pyobjc-framework-EventKit`):
      request full calendar access once (TCC prompt at the display), then `list_events(after,
      before, calendar?)` reads every iCloud/local calendar, and `create_event(title, start, end,
      location?, notes?, calendar?)` returns an **action** so the approval card shows the event
      before it's written. Events created by the bot get a "via Clawdbot" note. Digest opens
      with today's schedule; "when am I free Thursday afternoon?" answered from events. Inert on
      Windows like the other Mac modules. AppleScript (`tell application "Calendar"`) as the
      fallback if EventKit access is refused.
- [ ] **Digest 2.0:** today's calendar, waiting-on, reminders due, open tickets, new reviews,
      unread that matters; optional short evening wrap-up.
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

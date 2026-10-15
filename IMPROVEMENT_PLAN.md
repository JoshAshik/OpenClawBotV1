# Clawdbot → ChatGPT-app parity plan

Goal: make talking to Clawdbot feel as good as the ChatGPT app for email search + drafting,
while keeping (and actually delivering) its unique edge — WhatsApp/iMessage search and
property-review monitoring behind an approval flow.

> **Status (2026-08-02): Phases 0–5 complete; Phase 6 open.** The bot is OpenAI-only (Claude
> support removed by owner decision). Outstanding to-dos that aren't code: reconnect Gmail
> (`/addaccount personal` / `work` — both tokens expired), send one real voice note + photo as a
> live check, and decide the Phase 6 Mac deployment so message search goes live.

## Diagnosis — why it felt worse (pre-rebuild, 2026-08-02)

Evidence from the code and `clawdbot.log` at the time; items below are fixed unless noted:

1. **Year-old models, small budgets.** `llm.py:98` pins `claude-sonnet-4-20250514`, `llm.py:154` pins
   `gpt-4o`, both with `max_tokens=2048` and only 6 tool rounds. The ChatGPT app is running current
   models — this alone is most of the "dialogue quality" gap.
2. **The agent can't actually open an email.** `read_email` and `read_pdf_from_email` are implemented
   in `agent_tools.py` (`execute_tool`, lines 313/329) but are **missing from `TOOL_SCHEMAS`**, so the
   LLM can never call them. It only ever sees indexed snippets (≤4,000 chars). ChatGPT's Gmail
   connector reads full messages live.
3. **No reply capability.** `gmail_module.reply_to_email` exists but no tool exposes it — every
   "reply" becomes a brand-new email with no thread. (Also a latent bug: it sets `In-Reply-To` to the
   Gmail API id, not the RFC `Message-ID` header, so recipient-side threading breaks anyway.)
4. **Fragile conversation memory.** Follow-ups rely on a hack: tool output is truncated to 300 chars
   and appended to saved text as `[Tool data: …]` (`llm.py:82-85`), history is "last 20 rows"
   (`bot.py:1412`). "Reply to that one", "make it shorter", "the second email" often lose the referent.
   There is also no `/new` — one global conversation forever, so topics bleed together.
5. **Silence while working, plain-text answers.** No typing indicator, no status updates, no
   streaming — the user stares at nothing for the whole tool loop. Replies are sent without
   `parse_mode`, so no bold/lists/links. ChatGPT streams instantly and renders rich text.
6. **Search misses things.** Index coverage is ~100 recent emails per account (358 items total in the
   DB) with no backfill, so anything older simply can't be found. Search is keyword-only BM25 — no
   semantic match ("plumber invoice" won't find "Mike's Plumbing bill"), no sender/date filters, no
   recency boost. Raw user text goes straight into FTS5 `MATCH`, which throws syntax errors on `@`,
   quotes, and hyphens. The system prompt doesn't even include today's date, so "emails from last
   week" can't resolve reliably.
7. **The differentiator doesn't work here.** The log shows **821 consecutive failures each** for
   iMessage and WhatsApp indexing — both modules are Mac-only and this deployment is Windows. The
   index contains zero message items. The reason to prefer Clawdbot currently doesn't function.
8. **The bot blocks itself.** Gmail calls are synchronous and the background indexer runs on the same
   event loop, so the bot goes unresponsive during indexing cycles.
9. **Draft UX is one-shot.** Approve/Reject only — no Edit loop, no cc/bcc, no signature, no
   attachments, and the pending action doesn't carry an account, so sends always go from the active
   account even when the thread lives in the other one.
10. **Text-only input.** No voice notes, no photos/PDFs — the two things that make the ChatGPT phone
    app pleasant on the go.

---

## Phase 0 — Engine tune-up (quick wins, ~a day)

Biggest quality-per-line-changed. No architecture changes.

- [x] Upgrade models: `claude-sonnet-4-20250514` → `claude-sonnet-5` (default; best agentic tool
      use), and update the OpenAI path to the current model for `/switch`. Raise `max_tokens` to
      8192, `MAX_TOOL_ROUNDS` to 10.
- [x] Rebuild the system prompt: inject **current date/time**, user name + email addresses,
      connected account labels + which is active, email signature, drafting style rules, and
      "format for Telegram" guidance.
- [x] Add `read_email` and `read_pdf_from_email` to `TOOL_SCHEMAS` (they already work — they're just
      invisible to the model).
- [x] Add a `reply_email` tool wired to `gmail_module.reply_to_email`; fix threading to use the RFC
      `Message-ID` header for `In-Reply-To`/`References`, quote the original below the reply.
- [x] Sanitize FTS5 queries (quote each term, strip operators) with a `LIKE` fallback on syntax error.
- [x] Wrap sync Gmail/index work in `asyncio.to_thread` so background indexing stops freezing the bot.

**Check:** "find the lease for X and reply to the sender saying Y" works end-to-end in one turn,
threaded correctly, while an index cycle runs.

## Phase 1 — Feel: responsiveness + rendering

This is the "interface" half of the complaint. Telegram can't stream tokens, but it can feel live.

- [x] Immediate `typing` chat action on every message; post a status message within ~1s
      ("Searching your email…") and edit it as each tool runs; replace it with the final answer.
- [x] Optional true streaming: use the streaming API and progressively edit the answer message
      (throttled to ~1 edit/sec to respect Telegram limits).
- [x] Render answers as Telegram HTML (markdown → HTML converter with proper escaping; fall back to
      plain text if parsing fails). Bold, lists, and links make answers read like ChatGPT's.
- [x] Friendly errors: catch tool/LLM failures into a short human message with a Retry button
      (no more raw tracebacks in chat).
- [x] `/new` command to reset conversation context, plus auto-reset after ~2h idle.

**Check:** every message gets visible feedback in under a second; answers are formatted; a failed
tool never shows a stack trace.

## Phase 2 — Conversation memory done right

The "dialogue" half. Kill the `[Tool data: …]` hack.

- [x] Store turns structurally: a `turns` table keeping each assistant turn's tool calls + full
      results as JSON; rebuild proper `tool_use`/`tool_result` message arrays for the model so
      follow-ups resolve exactly ("the second one", "reply to it", "shorten it").
- [x] Working-set pinning: keep the last few referenced email ids / thread ids / contacts (with
      account labels) injected into the system prompt each turn.
- [x] Long-term memory: a `memories` table (signature, tone preferences, frequent contacts,
      property nicknames) + a `remember` tool; inject into the system prompt. This mirrors the
      ChatGPT memory feature.
- [x] Budget history by tokens (~30k) instead of "last 20 rows".

**Check:** draft → "make it warmer" → "actually address it to his partner too" → approve, all
without re-searching or losing the draft.

## Phase 3 — Search that finds the thing

- [x] **Backfill the whole mailbox** — paged id listing (500/page, `BACKFILL_MAX_EMAILS`=2000 per
      account, resumable daily job) plus `newer_than:7d` incremental sync instead of "list 50 recent".
      *Runs automatically once Gmail is reconnected (tokens currently expired).*
- [x] **Hybrid retrieval:** OpenAI `text-embedding-3-small` (512-dim, normalized float32 in SQLite,
      numpy brute-force — no native extension needed), merged with FTS5 BM25 via reciprocal-rank
      fusion + recency boost. Verified: "receipt from the AI company" finds AI-vendor receipts with
      zero keyword overlap.
- [x] Richer `search_everything`: `source`, `sender`, `after`/`before` filters and `limit` up to 15
      (the model retries with a higher limit instead of a "show more" button).
- [x] Messages indexed in **conversation windows** (per contact, 30-minute chunks) so results carry
      the exchange, not one stranded line; emails carry `thread_id` in metadata. (Full thread-level
      email grouping deferred until the Mac deployment makes message sources live.)
- [x] Gmail deep links in every email tool result; the agent is instructed to include
      "[open in Gmail](…)" when an answer centers on one email — verified end-to-end.

**Check:** 10 real "find that email/doc" queries from memory — target 9/10 first-try hits,
including ones older than 6 months. *(Run this after reconnecting Gmail so the backfill can
index history — today's index only holds the 358 most recent.)*

## Phase 4 — Email production parity

- [x] Draft cards with **Approve / ✏️ Edit / Reject**: Edit takes freeform instructions (typed or
      voice), the LLM revises the payload, and the same card updates in place. Reply edits can't
      change the recipient/thread (structurally locked). /cancel aborts an edit.
- [x] Pending actions carry the **account**; replies auto-select the account that received the
      thread; sends execute from that account. *(Landed with Phase 0–2.)*
- [x] cc/bcc, per-account signatures (`/signature`, injected into the drafting prompt), attachments
      pulled from an email or OneDrive at send time, optional HTML body with plain-text fallback.
- [x] Contact resolution: contacts table rebuilt from indexed mail headers (frequency-ranked,
      noreply-filtered — 137 contacts from the current index) + a `find_contact` tool; the approval
      card showing the resolved address is the confirmation step.

**Check:** "find who sent the tuition invoice and draft them an email asking about a payment plan,
cc me" → verified: one search, one correct draft card (to/cc/subject/account all right). The
send-with-attachment leg needs Gmail reconnected to verify live.

## Phase 5 — Phone-native input + proactivity

- [x] **Voice notes in:** Telegram voice/audio → `gpt-4o-mini-transcribe` (whisper-1 fallback) →
      shown as "🎤 …" → normal agent flow. Voice also works for draft-edit instructions.
- [x] **Photos/PDFs in:** photos → vision extraction (OCR + description); PDFs/text files → text
      extraction; the content is answered about AND indexed+embedded (source `telegram`) so it's
      findable later.
- [x] Morning digest at `DIGEST_HOUR` (default 8am local): unread triaged by the LLM into
      needs-attention vs ignorable, new reviews, pending draft cards. Stays silent when there's
      nothing to say; degrades gracefully while Gmail is disconnected.

**Check:** send a voice note asking about an email while walking; get a formatted answer; 8am digest
arrives daily. *(API paths verified with synthetic audio/images; do one real voice note + photo
from the phone after starting the bot.)*

## Phase 6 — The moat: make message search real

Reality: iMessage/WhatsApp indexing has never succeeded on this Windows box (821 failures each;
Mac-only code paths). Decide the deployment, then ship it:

- **Option A (recommended): run Clawdbot on the Mac Mini** the modules were written for. Zero new
  code; Windows stays the dev box.
- **Option B: split deployment** — a tiny "source agent" on the Mac that reads `chat.db` /
  `ChatStorage.sqlite` and pushes new items into the shared index over HTTP/rsync; the main bot runs
  anywhere.
- **Option C (only if no Mac): WhatsApp bridge on Windows** (whatsmeow/Baileys sidecar — more moving
  parts, ToS gray area). There is **no** Windows path for iMessage.

Then:
- [ ] Message-aware answers: "what did the tenant say on WhatsApp about the leak?" returns the
      conversation window with dates.
- [ ] (Mac) iMessage **sending** via AppleScript behind the existing approval flow — fills in the
      `send_imessage` stub in `bot.py:1392`.

**Check:** a WhatsApp/iMessage question ChatGPT structurally cannot answer, answered correctly.

---

## Cross-cutting

- LLM: OpenAI-only by owner decision (2026-08-02) — Claude support and `/switch` removed;
  model set via `OPENAI_MODEL` (default `gpt-5.1`), embeddings via `EMBEDDING_MODEL`.
- Build a tiny eval set from the real conversations in the DB; re-run after each phase.
- Log tokens/cost per turn; show in `/status`.

## Suggested order

Phase 0 → 1 → 2 close the felt gap ("interface and dialogue"). Phase 3 → 4 close the capability gap
("search and produce emails"). Phase 5 adds the phone features people love ChatGPT for, and Phase 6
delivers the reason Clawdbot exists at all. If time is scarce: **0, 1, and the backfill+hybrid items
of 3 are the 80/20.**

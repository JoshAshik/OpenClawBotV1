# Evals

A small, rerunnable check that Clawdbot still finds the right thing. `cases.jsonl` holds one
case per line:

```json
{"query": "find the tuition invoice from last month", "expected": [{"source": "gmail", "source_id": "personal:19eb846f87f11b0c"}], "tool": "search_everything", "origin": "mined"}
```

- `query` — what the user typed (phone numbers and key-like tokens are redacted).
- `expected` — the item(s) a correct answer must surface. Gmail ids may carry an account prefix;
  scoring compares the bare id.
- `origin` — `mined` (extracted from real conversations) or `manual` (hand-written).

## Where cases come from

```bash
python -X utf8 -m evals.extract
```

reads `chat_turns` in the live `clawdbot.db` **read-only** and emits a case for every user message
whose answer read or cited a specific indexed item (via `read_email`, `read_pdf_from_email`,
`read_indexed_document`, `reply_email`, or a Gmail deep link). Queries shorter than four words are
skipped. Re-running replaces the mined cases and keeps every `manual` one.

## Adding a manual case

Append a line with `"origin": "manual"`. Find the id with `/find` in Telegram or straight from the
index:

```bash
python -X utf8 -c "import asyncio, search_index as s; print(asyncio.run(s.hybrid_search('tuition invoice', limit=3)))"
```

Good manual cases are the ones you actually ask: "the plumber's invoice from the spring", "Sara's
text about the deposit", "the lease for 12 Oak". Aim for 10–20 covering both accounts and (once the
Mac is live) messages.

## Running

```bash
python -X utf8 -m evals.run              # offline: hybrid_search only, ~a few cents of embeddings
python -X utf8 -m evals.run --live       # + the real agent on up to 5 cases (spends money, says so)
```

Offline reports per-case rank plus hit@1, hit@5 and MRR. Live reports whether the agent's tool
calls touched an expected id and what the run cost (from `llm_usage`); it runs in an isolated
session so it never bleeds into your real conversation. Both write `evals/last_run.json` with
the commit hash, so runs can be compared across changes.

**Target (from UPGRADE_PLAN.md):** 9/10 first-try hits — hit@5 ≥ 90% offline. Re-run after any
change to `search_index`, `embeddings`, the indexer, or the system prompt.

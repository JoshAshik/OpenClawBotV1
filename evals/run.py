"""
Run the eval set.

    python -X utf8 -m evals.run                  # offline: hybrid_search only (a few cents of embeddings)
    python -X utf8 -m evals.run --live --limit 5 # also drive the real agent on N cases (spends money)

Offline mode scores hit@1, hit@5 and MRR of the expected ids in hybrid_search results
against the live index. Live mode runs llm.agent_chat in an isolated session and
reports whether the agent's tool calls touched the expected ids plus the OpenAI cost
of the run (from llm_usage). Results go to evals/last_run.json.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from evals.extract import CASES_PATH, bare_id, load_cases

LAST_RUN_PATH = Path(__file__).resolve().parent / "last_run.json"
ROOT = Path(__file__).resolve().parent.parent


# ── Scoring (pure) ─────────────────────────────────────────────────

def _keys(items: list[dict]) -> list[tuple[str, str]]:
    return [(it["source"], bare_id(it["source"], it["source_id"])) for it in items]


def score(results: list[dict], expected: list[dict]) -> dict:
    """results: ranked [{source, source_id}], expected: [{source, source_id}]."""
    want = set(_keys(expected))
    rank = None
    for i, key in enumerate(_keys(results), start=1):
        if key in want:
            rank = i
            break
    return {"rank": rank, "hit1": rank == 1, "hit5": rank is not None and rank <= 5,
            "rr": (1.0 / rank) if rank else 0.0}


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "hit1": 0.0, "hit5": 0.0, "mrr": 0.0}
    return {
        "n": n,
        "hit1": sum(1 for r in rows if r["hit1"]) / n,
        "hit5": sum(1 for r in rows if r["hit5"]) / n,
        "mrr": sum(r["rr"] for r in rows) / n,
    }


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def format_table(rows: list[dict]) -> str:
    lines = [f"{'rank':>4}  {'hit@5':<5}  query → expected"]
    for r in rows:
        rank = "-" if r["rank"] is None else str(r["rank"])
        exp = ", ".join(bare_id(e["source"], e["source_id"])[:14] for e in r["expected"])
        lines.append(f"{rank:>4}  {'yes' if r['hit5'] else 'NO':<5}  {r['query'][:70]} → {exp}")
    return "\n".join(lines)


def format_summary(s: dict, label: str = "offline") -> str:
    if not s["n"]:
        return f"{label}: no cases"
    return (f"{label}: n={s['n']}  hit@1={s['hit1']:.0%}  hit@5={s['hit5']:.0%}  MRR={s['mrr']:.2f}"
            f"  (target: 9/10 first-try hits ⇒ hit@5 ≥ 90%)")


# ── Offline: retrieval only ───────────────────────────────────────

async def run_offline(cases: list[dict], limit: int = 10) -> list[dict]:
    import search_index

    rows = []
    for c in cases:
        try:
            results = await search_index.hybrid_search(c["query"], limit=limit)
        except Exception as e:
            results = []
            print(f"  search failed for {c['query'][:50]!r}: {e}", file=sys.stderr)
        sc = score(results, c["expected"])
        rows.append({**sc, "query": c["query"], "expected": c["expected"], "origin": c.get("origin", "")})
    return rows


# ── Live: drive the agent ─────────────────────────────────────────

async def run_live(cases: list[dict]) -> tuple[list[dict], float]:
    """Runs the real agent in an isolated session. Returns (rows, cost_usd)."""
    import db
    import llm

    start_iso = datetime.now(timezone.utc).isoformat()
    touched: list[tuple[str, str]] = []
    real_execute = llm.execute_tool

    async def spy(name, args):
        a = args or {}
        if name in ("read_email", "read_pdf_from_email", "reply_email") and a.get("email_id"):
            touched.append(("gmail", str(a["email_id"])))
        elif name == "read_indexed_document":
            touched.append((str(a.get("source", "")), str(a.get("source_id", ""))))
        out = await real_execute(name, a)
        for e in out.get("entities") or []:
            if e.get("type") in ("gmail", "email", "imessage", "whatsapp", "onedrive"):
                touched.append(("gmail" if e["type"] == "email" else e["type"], str(e.get("id", ""))))
        return out

    llm.execute_tool = spy
    await llm.start_new_session()  # keep eval turns out of the owner's conversation
    rows = []
    try:
        for c in cases:
            touched.clear()
            try:
                result = await llm.agent_chat(c["query"])
                text = result.get("text", "")
            except Exception as e:
                text = f"ERROR: {e}"
            want = set(_keys(c["expected"]))
            got = {(s, bare_id(s, i)) for s, i in touched}
            rows.append({"query": c["query"], "expected": c["expected"], "touched": bool(want & got),
                         "tools_touched": sorted(f"{s}:{i}" for s, i in got)[:10], "answer": text[:300]})
    finally:
        llm.execute_tool = real_execute
        await llm.start_new_session()
    usage = await db.usage_summary(start_iso)
    return rows, float(usage["cost"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the Clawdbot eval set")
    ap.add_argument("--live", action="store_true", help="also drive the real agent (spends money)")
    ap.add_argument("--limit", type=int, default=5, help="max cases for --live")
    ap.add_argument("--cases", default=str(CASES_PATH))
    args = ap.parse_args(argv)

    cases = load_cases(args.cases)
    if not cases:
        print(f"No cases in {args.cases}. Run `python -m evals.extract` or add manual cases.")
        return 1

    async def _go():
        import db
        import search_index
        try:
            print(f"Offline retrieval eval on {len(cases)} case(s) — embedding each query costs a few cents.")
            rows = await run_offline(cases)
            summary = summarize(rows)
            print(format_table(rows))
            print(format_summary(summary))
            report = {"timestamp": datetime.now(timezone.utc).isoformat(), "commit": git_commit(),
                      "offline": {"summary": summary, "rows": rows}}
            if args.live:
                sel = cases[: args.limit]
                print(f"\n⚠️  --live: running the REAL agent on {len(sel)} case(s). This spends OpenAI money.")
                live_rows, cost = await run_live(sel)
                hits = sum(1 for r in live_rows if r["touched"])
                for r in live_rows:
                    print(f"  {'✓' if r['touched'] else '✗'} {r['query'][:70]}")
                print(f"live: {hits}/{len(live_rows)} touched an expected id · cost ${cost:.4f}")
                report["live"] = {"touched": hits, "n": len(live_rows), "cost_usd": cost, "rows": live_rows}
            LAST_RUN_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\nWritten {LAST_RUN_PATH.name} (commit {report['commit']}).")
        finally:
            await search_index.close_db()
            await db.close_db()

    asyncio.run(_go())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

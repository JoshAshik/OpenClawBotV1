"""
Mine retrieval eval cases from the owner's real conversations (read-only).

A case is a user message that the assistant answered by reading or citing a specific
indexed item: the expected ids come from read_email / read_pdf_from_email /
read_indexed_document / reply_email tool inputs and from Gmail deep links in the
answer. Queries are redacted (phone numbers, key-like tokens) and must be at least
four words. Hand-written cases ("origin": "manual") in cases.jsonl survive re-runs.

    python -X utf8 -m evals.extract            # mine clawdbot.db → evals/cases.jsonl
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = Path(__file__).resolve().parent / "cases.jsonl"

READ_TOOLS = {"read_email", "read_pdf_from_email", "read_indexed_document", "reply_email"}
# A turn only counts as a retrieval case if the assistant STARTED by searching/reading;
# a turn that opens with reply_email/draft_email resolved its target from earlier
# context ("reply to it"), which offline retrieval can't be expected to reproduce.
RETRIEVAL_FIRST = {"search_everything", "search_emails", "search_pdfs", "read_email",
                   "read_pdf_from_email", "read_indexed_document", "list_recent_emails",
                   "get_conversation"}
MIN_WORDS = 4

_GMAIL_LINK_RE = re.compile(r"mail\.google\.com/mail/[^\s)\]]*?#all/([0-9a-fA-F]{8,})")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_KEY_RE = re.compile(r"\b(?:sk|rk|pk|ghp|xox[abp])-[A-Za-z0-9_-]{10,}\b")
_LONG_DIGITS_RE = re.compile(r"\b\d{9,}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def redact(text: str) -> str:
    text = _KEY_RE.sub("[key]", text)
    text = _EMAIL_RE.sub("[email]", text)
    text = _LONG_DIGITS_RE.sub("[number]", text)   # bare account/reference numbers first
    text = _PHONE_RE.sub("[phone]", text)          # then punctuated phone numbers
    return " ".join(text.split())


def bare_id(source: str, source_id: str) -> str:
    """Gmail ids may carry an account prefix ('personal:abc'); compare on the bare id."""
    if source == "gmail" and ":" in source_id:
        return source_id.split(":", 1)[1]
    return source_id


def _expected_from_turn(msgs: list[dict]) -> tuple[list[dict], str]:
    expected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    first_tool = ""

    def _add(source: str, source_id: str) -> None:
        if not source or not source_id:
            return
        key = (source, bare_id(source, source_id))
        if key not in seen:
            seen.add(key)
            expected.append({"source": source, "source_id": source_id})

    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for b in m.get("content", []) or []:
            if b.get("type") == "tool_use":
                name, inp = b.get("name", ""), b.get("input", {}) or {}
                first_tool = first_tool or name
                if name in ("read_email", "read_pdf_from_email", "reply_email"):
                    eid = str(inp.get("email_id", "")).strip()
                    acct = str(inp.get("account", "") or "").strip()
                    if eid and ":" not in eid and acct:
                        eid = f"{acct}:{eid}"
                    _add("gmail", eid)
                elif name == "read_indexed_document":
                    _add(str(inp.get("source", "")), str(inp.get("source_id", "")))
            elif b.get("type") == "text":
                for mid in _GMAIL_LINK_RE.findall(b.get("text", "") or ""):
                    _add("gmail", mid)
    return expected, first_tool


def mine_cases(db_path: Path | str) -> list[dict]:
    """Read chat_turns from a bot DB (opened read-only) and return mined cases."""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, session_id, role, messages_json FROM chat_turns ORDER BY id").fetchall()
    finally:
        conn.close()

    cases: list[dict] = []
    pending: tuple[str, str] | None = None  # (session_id, user text) awaiting its assistant turn
    for r in rows:
        try:
            msgs = json.loads(r["messages_json"])
        except json.JSONDecodeError:
            continue
        if r["role"] == "user":
            text = msgs[0].get("content", "") if msgs and isinstance(msgs[0].get("content"), str) else ""
            pending = (r["session_id"], text)
            continue
        if r["role"] != "assistant" or pending is None or pending[0] != r["session_id"]:
            continue
        query = pending[1]
        pending = None
        expected, first_tool = _expected_from_turn(msgs)
        if not expected or first_tool not in RETRIEVAL_FIRST or len(query.split()) < MIN_WORDS:
            continue
        cases.append({"query": redact(query), "expected": expected, "tool": first_tool, "origin": "mined"})
    return dedupe(cases)


def dedupe(cases: list[dict]) -> list[dict]:
    out, seen = [], set()
    for c in cases:
        key = c["query"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(c)
    return out


def load_cases(path: Path | str = CASES_PATH) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def merge_cases(existing: list[dict], mined: list[dict]) -> list[dict]:
    """Manual cases are kept verbatim (and win on a query clash); mined ones are replaced."""
    manual = [c for c in existing if c.get("origin") == "manual"]
    manual_keys = {c["query"].strip().lower() for c in manual}
    fresh = [c for c in mined if c["query"].strip().lower() not in manual_keys]
    return manual + fresh


def write_cases(cases: list[dict], path: Path | str = CASES_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    db_path = Path(argv[0]) if argv else ROOT / "clawdbot.db"
    mined = mine_cases(db_path)
    merged = merge_cases(load_cases(), mined)
    write_cases(merged)
    manual = sum(1 for c in merged if c.get("origin") == "manual")
    print(f"{len(mined)} mined case(s) from {db_path.name}; {manual} manual kept → {CASES_PATH} ({len(merged)} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

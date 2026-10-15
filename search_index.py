"""
Unified search index — SQLite FTS5 full-text search across all sources.

Sources: gmail, onedrive, imessage, whatsapp
Each item is indexed with source, source_id, title, content, and metadata.
FTS5 provides fast full-text search with ranking.
"""

import json
import hashlib
import aiosqlite
from datetime import datetime, timezone
from config import DB_PATH

_DB: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _DB
    if _DB is None:
        _DB = await aiosqlite.connect(DB_PATH)
        _DB.row_factory = aiosqlite.Row
        await _init_search_tables(_DB)
    return _DB


async def _init_search_tables(db: aiosqlite.Connection) -> None:
    await db.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            source,
            source_id,
            title,
            content,
            metadata,
            tokenize='porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS indexed_items (
            source      TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            indexed_at  TEXT NOT NULL,
            content_hash TEXT,
            title       TEXT,
            PRIMARY KEY (source, source_id)
        );
    """)
    await db.commit()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def is_indexed(source: str, source_id: str) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "SELECT 1 FROM indexed_items WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    return await cursor.fetchone() is not None


async def needs_reindex(source: str, source_id: str, content: str) -> bool:
    """Check if content has changed since last index."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT content_hash FROM indexed_items WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return True
    return row["content_hash"] != _content_hash(content)


async def index_item(
    source: str,
    source_id: str,
    title: str,
    content: str,
    metadata: dict | None = None,
) -> None:
    """Add or update an item in the search index."""
    db = await get_db()
    meta_json = json.dumps(metadata or {})
    content_h = _content_hash(content)

    # Check if already indexed with same content
    cursor = await db.execute(
        "SELECT content_hash FROM indexed_items WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    existing = await cursor.fetchone()

    if existing:
        if existing["content_hash"] == content_h:
            return  # No change
        # Remove old FTS entry before re-indexing
        await db.execute(
            "DELETE FROM search_index WHERE source = ? AND source_id = ?",
            (source, source_id),
        )

    # Insert into FTS5
    await db.execute(
        "INSERT INTO search_index (source, source_id, title, content, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (source, source_id, title, content, meta_json),
    )

    # Track in indexed_items
    await db.execute(
        "INSERT INTO indexed_items (source, source_id, indexed_at, content_hash, title) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source, source_id) DO UPDATE SET indexed_at = ?, content_hash = ?, title = ?",
        (
            source, source_id, datetime.now(timezone.utc).isoformat(), content_h, title,
            datetime.now(timezone.utc).isoformat(), content_h, title,
        ),
    )
    await db.commit()


async def remove_item(source: str, source_id: str) -> None:
    db = await get_db()
    await db.execute(
        "DELETE FROM search_index WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    await db.execute(
        "DELETE FROM indexed_items WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    await db.commit()


def _fts_terms(query: str) -> list[str]:
    """Split a raw user query into quotable terms, stripping FTS5 operators."""
    terms = []
    for tok in query.split():
        tok = tok.replace('"', "").strip()
        if tok:
            terms.append(tok)
    return terms


async def _run_match(db, match_expr: str, source_filter: str | None, limit: int):
    if source_filter:
        cursor = await db.execute(
            "SELECT source, source_id, title, snippet(search_index, 3, '**', '**', '...', 40) as snippet, "
            "metadata, rank FROM search_index "
            "WHERE search_index MATCH ? AND source = ? "
            "ORDER BY rank LIMIT ?",
            (match_expr, source_filter, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT source, source_id, title, snippet(search_index, 3, '**', '**', '...', 40) as snippet, "
            "metadata, rank FROM search_index "
            "WHERE search_index MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match_expr, limit),
        )
    return await cursor.fetchall()


async def _like_fallback(db, terms: list[str], source_filter: str | None, limit: int):
    """Plain LIKE scan for when FTS5 can't parse the query at all."""
    conds = []
    params: list = []
    for t in terms:
        conds.append("(title LIKE ? OR content LIKE ?)")
        like = f"%{t}%"
        params.extend([like, like])
    where = " AND ".join(conds) if conds else "1=1"
    if source_filter:
        where += " AND source = ?"
        params.append(source_filter)
    params.append(limit)
    cursor = await db.execute(
        f"SELECT source, source_id, title, substr(content, 1, 200) as snippet, metadata, 0 as rank "
        f"FROM search_index WHERE {where} LIMIT ?",
        params,
    )
    return await cursor.fetchall()


async def search(
    query: str,
    source_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Full-text search across all indexed sources, ranked by BM25.

    The raw user query is never passed to MATCH directly — terms are quoted so
    characters like @, quotes, and hyphens can't cause FTS5 syntax errors.
    Falls back to OR-matching (better recall), then to a LIKE scan.
    """
    db = await get_db()
    terms = _fts_terms(query)
    if not terms:
        return []

    quoted = [f'"{t}"' for t in terms]
    rows = []
    try:
        rows = await _run_match(db, " ".join(quoted), source_filter, limit)
        if not rows and len(quoted) > 1:
            rows = await _run_match(db, " OR ".join(quoted), source_filter, limit)
    except aiosqlite.OperationalError:
        rows = []
    if not rows:
        try:
            rows = await _like_fallback(db, terms, source_filter, limit)
        except aiosqlite.OperationalError:
            rows = []

    results = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r["metadata"])
        except (json.JSONDecodeError, TypeError):
            pass

        results.append({
            "source": r["source"],
            "source_id": r["source_id"],
            "title": r["title"],
            "snippet": r["snippet"],
            "metadata": meta,
            "rank": r["rank"],
        })

    return results


async def get_item(source: str, source_id: str) -> dict | None:
    """Fetch one indexed item's title/snippet/metadata (for semantic-only hits)."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT source, source_id, title, substr(content, 1, 200) as snippet, metadata "
        "FROM search_index WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    r = await cursor.fetchone()
    if r is None:
        return None
    meta = {}
    try:
        meta = json.loads(r["metadata"])
    except (json.JSONDecodeError, TypeError):
        pass
    return {"source": r["source"], "source_id": r["source_id"], "title": r["title"],
            "snippet": r["snippet"], "metadata": meta, "rank": 0}


def _parse_item_date(meta: dict) -> datetime | None:
    """Best-effort parse of an item's date from metadata (RFC 2822 or ISO)."""
    raw = meta.get("date", "")
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _passes_filters(item: dict, sender: str | None,
                    after: datetime | None, before: datetime | None) -> bool:
    meta = item.get("metadata", {})
    if sender:
        haystack = f"{meta.get('from', '')} {meta.get('contact', '')}".lower()
        if sender.lower() not in haystack:
            return False
    if after or before:
        dt = _parse_item_date(meta)
        if dt is None:
            return False
        if after and dt < after:
            return False
        if before and dt > before:
            return False
    return True


async def hybrid_search(
    query: str,
    source_filter: str | None = None,
    sender: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Hybrid retrieval: FTS5 BM25 + semantic embeddings merged with reciprocal
    rank fusion, plus a small recency boost. Optional sender/date post-filters.
    """
    import embeddings

    pool = max(limit * 3, 30)
    fts_results = await search(query, source_filter=source_filter, limit=pool)
    sem_results = await embeddings.semantic_search(query, limit=pool)

    K = 60  # standard RRF constant
    scored: dict[tuple[str, str], dict] = {}

    for rank, item in enumerate(fts_results):
        key = (item["source"], item["source_id"])
        scored[key] = {"item": item, "score": 1.0 / (K + rank)}

    for rank, (source, source_id, _cos) in enumerate(sem_results):
        if source_filter and source != source_filter:
            continue
        key = (source, source_id)
        if key in scored:
            scored[key]["score"] += 1.0 / (K + rank)
        else:
            item = await get_item(source, source_id)
            if item is not None:
                scored[key] = {"item": item, "score": 1.0 / (K + rank)}

    # Small recency boost: fresh items win ties, can't leapfrog strong matches.
    now = datetime.now(timezone.utc)
    for entry in scored.values():
        dt = _parse_item_date(entry["item"].get("metadata", {}))
        if dt is not None:
            age_days = max(0.0, (now - dt).total_seconds() / 86400)
            entry["score"] += 0.005 * (0.5 ** (age_days / 180))

    ranked = sorted(scored.values(), key=lambda e: -e["score"])
    out = []
    for entry in ranked:
        if _passes_filters(entry["item"], sender, after, before):
            out.append(entry["item"])
        if len(out) >= limit:
            break
    return out


async def get_full_content(source: str, source_id: str) -> str | None:
    """Retrieve the full indexed content for an item."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT content FROM search_index WHERE source = ? AND source_id = ?",
        (source, source_id),
    )
    row = await cursor.fetchone()
    return row["content"] if row else None


async def get_index_stats() -> dict:
    """Return counts per source and total."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT source, COUNT(*) as count FROM indexed_items GROUP BY source"
    )
    rows = await cursor.fetchall()
    stats = {r["source"]: r["count"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats


async def close_db() -> None:
    global _DB
    if _DB:
        await _DB.close()
        _DB = None

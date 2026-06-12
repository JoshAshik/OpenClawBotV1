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


async def search(
    query: str,
    source_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Full-text search across all indexed sources.
    Returns results ranked by relevance (BM25).
    """
    db = await get_db()

    if source_filter:
        cursor = await db.execute(
            "SELECT source, source_id, title, snippet(search_index, 3, '**', '**', '...', 40) as snippet, "
            "metadata, rank FROM search_index "
            "WHERE search_index MATCH ? AND source = ? "
            "ORDER BY rank LIMIT ?",
            (query, source_filter, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT source, source_id, title, snippet(search_index, 3, '**', '**', '...', 40) as snippet, "
            "metadata, rank FROM search_index "
            "WHERE search_index MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        )

    rows = await cursor.fetchall()
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

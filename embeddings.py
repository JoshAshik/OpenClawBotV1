"""
Semantic search embeddings — OpenAI text-embedding-3-small over the search index.

Vectors are stored normalized (float32 BLOBs) in the same SQLite database and
searched with a brute-force numpy dot product. At personal-mailbox scale
(a few thousand items × 512 dims) that's a few milliseconds per query with an
in-memory matrix cache — no native vector extension needed.
"""

import logging
from datetime import datetime, timezone

import numpy as np
import openai

import config
import search_index

logger = logging.getLogger(__name__)

_client: openai.AsyncOpenAI | None = None

# In-memory matrix cache: rebuilt lazily whenever new vectors are stored.
_cache_ids: list[tuple[str, str]] = []
_cache_matrix: np.ndarray | None = None
_cache_dirty = True


def is_enabled() -> bool:
    return bool(config.OPENAI_API_KEY)


def _get_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY,
                                     timeout=config.OPENAI_TIMEOUT_SECONDS, max_retries=0)
    return _client


async def _init_table() -> None:
    db = await search_index.get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS embeddings (
            source      TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            model       TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            vector      BLOB NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )"""
    )
    await db.commit()


async def embed_texts(texts: list[str]) -> list[np.ndarray]:
    """Embed a batch of texts, returning normalized float32 vectors."""
    import llm  # lazy: llm → agent_tools → search_index would otherwise cycle at import time

    response = await llm.with_retry(lambda: _get_client().embeddings.create(
        model=config.EMBEDDING_MODEL,
        input=[t[:6000] or " " for t in texts],
        dimensions=config.EMBEDDING_DIM,
    ), "embed")
    try:
        await llm.record_usage("embed", config.EMBEDDING_MODEL, getattr(response, "usage", None))
    except Exception:
        pass
    vectors = []
    for item in response.data:
        v = np.asarray(item.embedding, dtype=np.float32)
        norm = np.linalg.norm(v)
        if norm > 0:
            v = v / norm
        vectors.append(v)
    return vectors


async def store_embedding(source: str, source_id: str, vector: np.ndarray) -> None:
    global _cache_dirty
    await _init_table()
    db = await search_index.get_db()
    await db.execute(
        "INSERT INTO embeddings (source, source_id, model, dim, vector, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, source_id) DO UPDATE SET model=?, dim=?, vector=?, updated_at=?",
        (
            source, source_id, config.EMBEDDING_MODEL, len(vector), vector.tobytes(),
            datetime.now(timezone.utc).isoformat(),
            config.EMBEDDING_MODEL, len(vector), vector.tobytes(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db.commit()
    _cache_dirty = True


async def embed_item(source: str, source_id: str, title: str, content: str) -> None:
    """Embed one indexed item (called from the indexer right after index_item)."""
    if not is_enabled():
        return
    try:
        vectors = await embed_texts([f"{title}\n{content}"])
        await store_embedding(source, source_id, vectors[0])
    except Exception as e:
        logger.warning(f"embed_item failed for {source}:{source_id}: {e}")


async def embed_missing(limit: int = 300) -> int:
    """Embed indexed items that don't have a vector yet. Returns count embedded."""
    if not is_enabled():
        return 0
    await _init_table()
    db = await search_index.get_db()
    cursor = await db.execute(
        """SELECT si.source, si.source_id, si.title, si.content
           FROM search_index si
           LEFT JOIN embeddings e ON e.source = si.source AND e.source_id = si.source_id
           WHERE e.source_id IS NULL
           LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return 0

    done = 0
    batch_size = 64
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        try:
            vectors = await embed_texts([f"{r['title']}\n{r['content']}" for r in batch])
        except Exception as e:
            logger.warning(f"embed_missing batch failed: {e}")
            break
        for row, vec in zip(batch, vectors):
            await store_embedding(row["source"], row["source_id"], vec)
            done += 1

    if done:
        logger.info(f"Embedded {done} items for semantic search")
    return done


async def _load_cache() -> None:
    global _cache_ids, _cache_matrix, _cache_dirty
    await _init_table()
    db = await search_index.get_db()
    cursor = await db.execute("SELECT source, source_id, dim, vector FROM embeddings")
    rows = await cursor.fetchall()

    ids = []
    vecs = []
    for r in rows:
        v = np.frombuffer(r["vector"], dtype=np.float32)
        if len(v) != r["dim"]:
            continue
        ids.append((r["source"], r["source_id"]))
        vecs.append(v)

    _cache_ids = ids
    _cache_matrix = np.vstack(vecs) if vecs else None
    _cache_dirty = False


async def semantic_search(query: str, limit: int = 30) -> list[tuple[str, str, float]]:
    """Return [(source, source_id, cosine_score)] for the closest indexed items."""
    if not is_enabled():
        return []
    if _cache_dirty or _cache_matrix is None:
        await _load_cache()
    if _cache_matrix is None or not len(_cache_ids):
        return []

    try:
        qvec = (await embed_texts([query]))[0]
    except Exception as e:
        logger.warning(f"query embedding failed: {e}")
        return []

    scores = _cache_matrix @ qvec
    top = np.argsort(-scores)[:limit]
    return [(_cache_ids[i][0], _cache_ids[i][1], float(scores[i])) for i in top]

"""
Background indexer — pulls content from all sources into the search index.

Each source has its own index_* function that can be called independently.
The full_reindex function runs them all.
A background job calls incremental_index periodically.
"""

import asyncio
import logging

import gmail_module
import search_index

logger = logging.getLogger(__name__)

# Log unavailable sources once, not every poll cycle.
_unavailable_logged: set[str] = set()


def _log_unavailable_once(source: str, reason: str) -> None:
    if source not in _unavailable_logged:
        _unavailable_logged.add(source)
        logger.info(f"{source} indexing disabled: {reason}")


# ── Gmail Indexer ─────────────────────────────────────────────────

async def _index_email_by_id(msg_id: str, account: str | None, account_label: str) -> bool:
    """Fetch one email (body + PDF text) and index + embed it. Returns True if indexed."""
    import embeddings

    source_id = f"{account_label}:{msg_id}"
    if await search_index.is_indexed("gmail", source_id):
        return False

    try:
        full = await asyncio.to_thread(
            gmail_module.get_email_with_pdf_text, msg_id, account=account
        )
    except Exception as e:
        logger.warning(f"Gmail index skip {msg_id}: {e}")
        return False

    parts = [full.get("body", "")]
    for pdf in full.get("pdf_texts", []):
        parts.append(f"\n[PDF: {pdf['filename']}]\n{pdf['text']}")
    content = "\n".join(parts)

    title = full.get("subject", "(no subject)")
    metadata = {
        "from": full.get("from", ""),
        "to": full.get("to", ""),
        "date": full.get("date", ""),
        "account": account_label,
        "thread_id": full.get("thread_id", ""),
        "has_pdf": len(full.get("pdf_texts", [])) > 0,
        "attachments": [a["filename"] for a in full.get("attachments", [])],
    }

    await search_index.index_item("gmail", source_id, title, content, metadata)
    await embeddings.embed_item("gmail", source_id, title, content)
    return True


async def index_gmail(max_emails: int = 100, account: str | None = None,
                      query: str = "newer_than:7d") -> int:
    """
    Incremental Gmail sync: everything from the last 7 days (catches bursts
    bigger than a fixed 'N most recent' window), skipping already-indexed mail.
    """
    try:
        ids = await asyncio.to_thread(
            gmail_module.list_email_ids, max_results=max_emails, query=query, account=account
        )
    except Exception as e:
        logger.error(f"Gmail index failed (list): {e}")
        return 0

    account_label = account or gmail_module.get_active_account() or "default"
    indexed = 0
    for msg_id in ids:
        if await _index_email_by_id(msg_id, account, account_label):
            indexed += 1

    if indexed:
        logger.info(f"Gmail indexer: {indexed} new items indexed (account: {account_label})")
    return indexed


async def index_all_gmail_accounts() -> int:
    """Index recent emails from all connected Gmail accounts."""
    accounts = gmail_module.get_connected_accounts()
    total = 0
    for acc in accounts:
        try:
            count = await index_gmail(max_emails=100, account=acc["label"])
            total += count
        except Exception as e:
            logger.error(f"Gmail index failed for {acc['label']}: {e}")
    return total


async def backfill_gmail(account: str, max_emails: int | None = None,
                         batch_pause: float = 0.5) -> int:
    """
    Deep-index the mailbox history (Phase 3): page through up to max_emails
    ids (newest first) and index everything not yet in the search index.
    Safe to re-run — already-indexed mail is skipped cheaply, so each run
    continues where the last left off.
    """
    import config

    max_emails = max_emails or config.BACKFILL_MAX_EMAILS
    try:
        ids = await asyncio.to_thread(
            gmail_module.list_email_ids, max_results=max_emails, account=account
        )
    except Exception as e:
        logger.warning(f"Gmail backfill unavailable for '{account}': {e}")
        return 0

    indexed = 0
    for i, msg_id in enumerate(ids):
        if await _index_email_by_id(msg_id, account, account):
            indexed += 1
            if indexed % 25 == 0:
                logger.info(f"Gmail backfill ({account}): {indexed} indexed, {i + 1}/{len(ids)} scanned")
                await asyncio.sleep(batch_pause)  # be gentle with the API

    logger.info(f"Gmail backfill ({account}) done: {indexed} new items ({len(ids)} scanned)")
    return indexed


async def backfill_all_gmail_accounts() -> int:
    total = 0
    for acc in gmail_module.get_connected_accounts():
        total += await backfill_gmail(acc["label"])
    return total


# ── OneDrive Indexer ──────────────────────────────────────────────

async def index_onedrive() -> int:
    """Index files from OneDrive. Requires onedrive_module."""
    try:
        from onedrive_module import is_connected, list_files, download_file_text
    except ImportError:
        return 0

    if not is_connected():
        _log_unavailable_once("OneDrive", "not connected (use /connectonedrive)")
        return 0

    indexed = 0
    try:
        files = await asyncio.to_thread(list_files)
    except Exception as e:
        logger.error(f"OneDrive index failed (list): {e}")
        return 0

    for f in files:
        source_id = f["id"]

        if await search_index.is_indexed("onedrive", source_id):
            continue

        try:
            text = await asyncio.to_thread(download_file_text, f["id"], f["name"])
        except Exception as e:
            logger.warning(f"OneDrive index skip {f['name']}: {e}")
            continue

        if not text or not text.strip():
            continue

        metadata = {
            "filename": f["name"],
            "path": f.get("path", ""),
            "size": f.get("size", 0),
            "modified": f.get("modified", ""),
        }

        await search_index.index_item("onedrive", source_id, f["name"], text, metadata)
        indexed += 1

    logger.info(f"OneDrive indexer: {indexed} new items indexed")
    return indexed


# ── Message Indexers (iMessage / WhatsApp) ────────────────────────
#
# Messages are indexed as CONVERSATION WINDOWS, not single lines (Phase 3):
# per contact, messages within 30 minutes of each other form one document,
# so a search hit carries the surrounding exchange instead of one stranded line.

_WINDOW_GAP_MINUTES = 30


def _build_conversation_windows(messages: list[dict], label: str) -> list[dict]:
    """Group flat messages into windows: [{source_id, title, content, metadata}]."""
    from datetime import datetime, timedelta

    def _parse(d: str):
        try:
            return datetime.fromisoformat(d)
        except (ValueError, TypeError):
            return None

    usable = []
    for m in messages:
        dt = _parse(m.get("date", ""))
        if dt is not None and m.get("text"):
            usable.append((m.get("contact") or "Unknown", dt, m))
    usable.sort(key=lambda x: (x[0], x[1]))

    windows = []
    current = None
    for contact, dt, m in usable:
        if (current is None or current["contact"] != contact
                or dt - current["last_dt"] > timedelta(minutes=_WINDOW_GAP_MINUTES)):
            if current is not None:
                windows.append(current)
            current = {
                "contact": contact, "start_dt": dt, "last_dt": dt, "lines": [], "count": 0,
            }
        who = "Me" if m.get("is_from_me") else contact
        current["lines"].append(f"[{dt.strftime('%Y-%m-%d %H:%M')}] {who}: {m['text']}")
        current["last_dt"] = dt
        current["count"] += 1
    if current is not None:
        windows.append(current)

    docs = []
    for w in windows:
        docs.append({
            "source_id": f"{w['contact']}|{w['start_dt'].isoformat()}",
            "title": f"{label} with {w['contact']} ({w['start_dt'].strftime('%Y-%m-%d %H:%M')})",
            "content": "\n".join(w["lines"]),
            "metadata": {
                "contact": w["contact"],
                "date": w["last_dt"].isoformat(),
                "messages": w["count"],
            },
        })
    return docs


async def _index_message_windows(source: str, docs: list[dict]) -> int:
    """Index conversation windows; open windows re-index as new messages append."""
    import embeddings

    indexed = 0
    for doc in docs:
        # index_item no-ops on unchanged content, updates when a window grew.
        if not await search_index.needs_reindex(source, doc["source_id"], doc["content"]):
            continue
        await search_index.index_item(
            source, doc["source_id"], doc["title"], doc["content"], doc["metadata"]
        )
        await embeddings.embed_item(source, doc["source_id"], doc["title"], doc["content"])
        indexed += 1
    return indexed


async def index_imessage(limit: int = 500) -> int:
    """Index recent iMessage conversations as windows. Mac only."""
    try:
        from imessage_module import is_available, get_recent_messages
    except ImportError:
        return 0

    if not is_available():
        _log_unavailable_once("iMessage", "chat.db not found (Mac only)")
        return 0

    try:
        messages = await asyncio.to_thread(get_recent_messages, limit=limit)
    except Exception as e:
        logger.error(f"iMessage index failed: {e}")
        return 0

    docs = _build_conversation_windows(messages, "iMessage")
    indexed = await _index_message_windows("imessage", docs)
    if indexed:
        logger.info(f"iMessage indexer: {indexed} conversation windows indexed")
    return indexed


async def index_whatsapp(limit: int = 500) -> int:
    """Index recent WhatsApp messages as windows. Mac only (WhatsApp Desktop)."""
    try:
        from whatsapp_module import is_available, get_recent_messages
    except ImportError:
        return 0

    if not is_available():
        _log_unavailable_once("WhatsApp", "ChatStorage.sqlite not found (Mac only)")
        return 0

    try:
        messages = await asyncio.to_thread(get_recent_messages, limit=limit)
    except Exception as e:
        logger.error(f"WhatsApp index failed: {e}")
        return 0

    docs = _build_conversation_windows(messages, "WhatsApp")
    indexed = await _index_message_windows("whatsapp", docs)
    if indexed:
        logger.info(f"WhatsApp indexer: {indexed} conversation windows indexed")
    return indexed


# ── Contacts (rebuilt from indexed mail) ──────────────────────────

_NOREPLY_RE = None


def _is_noreply(addr: str) -> bool:
    global _NOREPLY_RE
    if _NOREPLY_RE is None:
        import re
        _NOREPLY_RE = re.compile(r"no.?reply|donotreply|notifications?@|mailer-daemon|bounce", re.I)
    return bool(_NOREPLY_RE.search(addr))


async def rebuild_contacts() -> int:
    """Aggregate name → address mappings from indexed mail headers (Phase 4)."""
    import json
    from email.utils import getaddresses

    import db
    from search_index import _parse_item_date

    conn = await search_index.get_db()
    cursor = await conn.execute("SELECT metadata FROM search_index WHERE source = 'gmail'")
    rows = await cursor.fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        try:
            meta = json.loads(r["metadata"])
        except (json.JSONDecodeError, TypeError):
            continue
        dt = _parse_item_date(meta)
        seen = dt.isoformat() if dt else ""
        for field in ("from", "to"):
            for name, addr in getaddresses([meta.get(field, "") or ""]):
                if "@" not in addr or _is_noreply(addr):
                    continue
                entry = agg.setdefault(addr.lower(), {"email": addr.lower(), "name": "",
                                                      "count": 0, "last_seen": ""})
                entry["count"] += 1
                if name and len(name) > len(entry["name"]):
                    entry["name"] = name
                if seen > entry["last_seen"]:
                    entry["last_seen"] = seen

    await db.replace_contacts(list(agg.values()))
    logger.debug(f"Contacts rebuilt: {len(agg)} addresses")
    return len(agg)


# ── Orchestration ─────────────────────────────────────────────────

async def incremental_index() -> dict:
    """Run incremental index across all available sources. Called by background job."""
    import embeddings

    results = {}

    results["gmail"] = await index_all_gmail_accounts()

    results["onedrive"] = await index_onedrive()
    results["imessage"] = await index_imessage()
    results["whatsapp"] = await index_whatsapp()

    # Catch anything indexed before embeddings existed (or whose embed failed).
    try:
        results["embedded"] = await embeddings.embed_missing(limit=300)
    except Exception as e:
        logger.warning(f"embed_missing failed: {e}")

    try:
        await rebuild_contacts()
    except Exception as e:
        logger.warning(f"rebuild_contacts failed: {e}")

    total = sum(results.values())
    if total > 0:
        logger.info(f"Incremental index complete: {total} new items ({results})")

    return results


async def full_reindex() -> dict:
    """Full reindex — deep backfill of Gmail history plus all other sources."""
    import embeddings

    results = {}
    results["gmail_backfill"] = await backfill_all_gmail_accounts()

    results["onedrive"] = await index_onedrive()
    results["imessage"] = await index_imessage(limit=2000)
    results["whatsapp"] = await index_whatsapp(limit=2000)

    try:
        results["embedded"] = await embeddings.embed_missing(limit=2000)
    except Exception as e:
        logger.warning(f"embed_missing failed: {e}")

    total = sum(results.values())
    logger.info(f"Full reindex complete: {total} items ({results})")
    return results

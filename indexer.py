"""
Background indexer — pulls content from all sources into the search index.

Each source has its own index_* function that can be called independently.
The full_reindex function runs them all.
A background job calls incremental_index periodically.
"""

import logging

import gmail_module
import search_index

logger = logging.getLogger(__name__)


# ── Gmail Indexer ─────────────────────────────────────────────────

async def index_gmail(max_emails: int = 100, account: str | None = None) -> int:
    """Index recent emails + PDF attachment text from Gmail."""
    indexed = 0

    try:
        emails = gmail_module.list_emails(max_results=max_emails, account=account)
    except Exception as e:
        logger.error(f"Gmail index failed (list): {e}")
        return 0

    account_label = account or gmail_module.get_active_account() or "default"

    for em in emails:
        source_id = f"{account_label}:{em['id']}"

        # Skip if already indexed
        if await search_index.is_indexed("gmail", source_id):
            continue

        try:
            full = gmail_module.get_email_with_pdf_text(em["id"], account=account)
        except Exception as e:
            logger.warning(f"Gmail index skip {em['id']}: {e}")
            continue

        # Build content: email body + all PDF text
        parts = [full.get("body", "")]
        for pdf in full.get("pdf_texts", []):
            parts.append(f"\n[PDF: {pdf['filename']}]\n{pdf['text']}")
        content = "\n".join(parts)

        title = f"{em.get('subject', '(no subject)')}"
        metadata = {
            "from": em.get("from", ""),
            "to": em.get("to", ""),
            "date": em.get("date", ""),
            "account": account_label,
            "has_pdf": len(full.get("pdf_texts", [])) > 0,
            "attachments": [a["filename"] for a in full.get("attachments", [])],
        }

        await search_index.index_item("gmail", source_id, title, content, metadata)
        indexed += 1

    logger.info(f"Gmail indexer: {indexed} new items indexed (account: {account_label})")
    return indexed


async def index_all_gmail_accounts() -> int:
    """Index emails from all connected Gmail accounts."""
    accounts = gmail_module.get_connected_accounts()
    total = 0
    for acc in accounts:
        try:
            count = await index_gmail(max_emails=50, account=acc["label"])
            total += count
        except Exception as e:
            logger.error(f"Gmail index failed for {acc['label']}: {e}")
    return total


# ── OneDrive Indexer ──────────────────────────────────────────────

async def index_onedrive() -> int:
    """Index files from OneDrive. Requires onedrive_module."""
    try:
        from onedrive_module import list_files, download_file_text
    except ImportError:
        return 0

    indexed = 0
    try:
        files = list_files()
    except Exception as e:
        logger.error(f"OneDrive index failed (list): {e}")
        return 0

    for f in files:
        source_id = f["id"]

        if await search_index.is_indexed("onedrive", source_id):
            continue

        try:
            text = download_file_text(f["id"], f["name"])
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


# ── iMessage Indexer ──────────────────────────────────────────────

async def index_imessage(limit: int = 500) -> int:
    """Index recent iMessage conversations. Mac only."""
    try:
        from imessage_module import get_recent_messages
    except ImportError:
        return 0

    indexed = 0
    try:
        messages = get_recent_messages(limit=limit)
    except Exception as e:
        logger.error(f"iMessage index failed: {e}")
        return 0

    for msg in messages:
        source_id = str(msg["rowid"])

        if await search_index.is_indexed("imessage", source_id):
            continue

        title = f"iMessage with {msg['contact']}"
        metadata = {
            "contact": msg["contact"],
            "date": msg["date"],
            "is_from_me": msg["is_from_me"],
        }

        await search_index.index_item("imessage", source_id, title, msg["text"], metadata)
        indexed += 1

    logger.info(f"iMessage indexer: {indexed} new items indexed")
    return indexed


# ── WhatsApp Indexer ──────────────────────────────────────────────

async def index_whatsapp(limit: int = 500) -> int:
    """Index recent WhatsApp messages. Mac only (WhatsApp Desktop)."""
    try:
        from whatsapp_module import get_recent_messages
    except ImportError:
        return 0

    indexed = 0
    try:
        messages = get_recent_messages(limit=limit)
    except Exception as e:
        logger.error(f"WhatsApp index failed: {e}")
        return 0

    for msg in messages:
        source_id = msg["id"]

        if await search_index.is_indexed("whatsapp", source_id):
            continue

        title = f"WhatsApp: {msg['contact']}"
        metadata = {
            "contact": msg["contact"],
            "date": msg["date"],
            "is_from_me": msg.get("is_from_me", False),
        }

        await search_index.index_item("whatsapp", source_id, title, msg["text"], metadata)
        indexed += 1

    logger.info(f"WhatsApp indexer: {indexed} new items indexed")
    return indexed


# ── Orchestration ─────────────────────────────────────────────────

async def incremental_index() -> dict:
    """Run incremental index across all available sources. Called by background job."""
    results = {}

    results["gmail"] = await index_all_gmail_accounts()

    results["onedrive"] = await index_onedrive()
    results["imessage"] = await index_imessage()
    results["whatsapp"] = await index_whatsapp()

    total = sum(results.values())
    if total > 0:
        logger.info(f"Incremental index complete: {total} new items ({results})")

    return results


async def full_reindex() -> dict:
    """Full reindex — same as incremental but with higher limits."""
    results = {}

    accounts = gmail_module.get_connected_accounts()
    gmail_total = 0
    for acc in accounts:
        try:
            gmail_total += await index_gmail(max_emails=200, account=acc["label"])
        except Exception as e:
            logger.error(f"Full reindex Gmail {acc['label']}: {e}")
    results["gmail"] = gmail_total

    results["onedrive"] = await index_onedrive()
    results["imessage"] = await index_imessage(limit=2000)
    results["whatsapp"] = await index_whatsapp(limit=2000)

    total = sum(results.values())
    logger.info(f"Full reindex complete: {total} items ({results})")
    return results

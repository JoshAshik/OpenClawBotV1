"""
Shared pytest fixtures. No network: nothing here talks to OpenAI, Telegram or Google.

`config` needs TELEGRAM_* to import, so placeholders are set when the dev box has no
`.env`; they never reach a real API because the tests only touch SQLite.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_AUTHORIZED_USER_ID", "1")


@pytest.fixture
def chat_db(tmp_path, monkeypatch) -> Path:
    """A synthetic chat.db that imessage_module is pointed at via IMESSAGE_DB_PATH."""
    from tests.fixtures.make_chat_db import build_chat_db

    path = build_chat_db(tmp_path / "chat.db")
    monkeypatch.setenv("IMESSAGE_DB_PATH", str(path))
    return path


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    """
    Point the bot's own SQLite (settings + search index) at a temp file so index tests
    never touch the live clawdbot.db, and stub out the embeddings API call.
    """
    import config
    import db
    import embeddings
    import search_index

    path = tmp_path / "clawdbot-test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(search_index, "DB_PATH", path)
    monkeypatch.setattr(db, "_DB", None)
    monkeypatch.setattr(search_index, "_DB", None)

    async def _no_embed(*args, **kwargs):
        return None

    monkeypatch.setattr(embeddings, "embed_item", _no_embed)
    yield path


def run_async(coro):
    """Run a coroutine and close the module-level aiosqlite connections on the same loop."""
    import asyncio

    async def _wrapped():
        import db
        import search_index
        try:
            return await coro
        finally:
            await search_index.close_db()
            await db.close_db()

    return asyncio.run(_wrapped())

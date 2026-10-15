"""
Clawdbot — Personal assistant for property management.

Entry point. Starts the Telegram bot and background services.
"""

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import db
import search_index
from bot import build_app

# clawdbot.log stays the active file; rotated copies are clawdbot.log.1 … .5 (10 MB each).
_LOG_PATH = Path(__file__).parent / "clawdbot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"),
    ],
)
logger = logging.getLogger("clawdbot")


async def shutdown(app) -> None:
    logger.info("Shutting down...")
    await db.close_db()
    await search_index.close_db()


def main() -> None:
    # Python 3.14 removed auto-creation of event loops — create one explicitly
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    logger.info("Starting Clawdbot...")

    # Auto-set default Gmail account on startup
    import config
    import gmail_module
    if config.DEFAULT_GMAIL_ACCOUNT:
        if gmail_module.set_active_account(config.DEFAULT_GMAIL_ACCOUNT):
            logger.info(f"Gmail account set to: {config.DEFAULT_GMAIL_ACCOUNT}")
        else:
            logger.warning(f"Default Gmail account '{config.DEFAULT_GMAIL_ACCOUNT}' not found (no token file)")

    app = build_app()

    app.post_shutdown = shutdown

    logger.info("Telegram bot polling started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

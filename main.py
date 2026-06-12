"""
Clawdbot — Personal assistant for property management.

Entry point. Starts the Telegram bot and background services.
"""

import asyncio
import logging
import signal
import sys

import db
from bot import build_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("clawdbot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("clawdbot")


async def shutdown(app) -> None:
    logger.info("Shutting down...")
    await db.close_db()


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

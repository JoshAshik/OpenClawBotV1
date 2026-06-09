"""
Security layer for Clawdbot.

Three action categories:
  - READ:      Auto-allowed, logged silently (read email, check reviews)
  - WRITE:     Requires explicit Approve/Reject before execution (send email, send message)
  - DANGEROUS: Requires double confirmation (delete, bulk actions)

Nothing outbound ever fires without the owner pressing Approve.
"""

import time
from collections import deque
from enum import Enum
from functools import wraps

from telegram import Update

import config
import db


class ActionCategory(str, Enum):
    READ = "read"
    WRITE = "write"
    DANGEROUS = "dangerous"


_session_unlocked: bool = not bool(config.SESSION_PIN)

_outbound_timestamps: deque[float] = deque()


def is_authorized(user_id: int) -> bool:
    return user_id == config.TELEGRAM_AUTHORIZED_USER_ID


def is_session_unlocked() -> bool:
    return _session_unlocked


def unlock_session() -> None:
    global _session_unlocked
    _session_unlocked = True


def lock_session() -> None:
    global _session_unlocked
    _session_unlocked = False


def check_rate_limit() -> bool:
    now = time.time()
    cutoff = now - 3600
    while _outbound_timestamps and _outbound_timestamps[0] < cutoff:
        _outbound_timestamps.popleft()
    return len(_outbound_timestamps) < config.RATE_LIMIT_PER_HOUR


def record_outbound() -> None:
    _outbound_timestamps.append(time.time())


def auth_required(handler):
    """Decorator for Telegram handlers: blocks unauthorized users and locked sessions."""

    @wraps(handler)
    async def wrapper(update: Update, context, *args, **kwargs):
        user = update.effective_user
        if user is None or not is_authorized(user.id):
            await db.log_audit(
                action_type="unauthorized_access",
                category="security",
                summary=f"Blocked user {user.id if user else 'unknown'} ({user.username if user else 'N/A'})",
                user_id=user.id if user else None,
                approved=False,
            )
            return

        if not is_session_unlocked():
            await update.message.reply_text("Session locked. Send your PIN to unlock.")
            return

        return await handler(update, context, *args, **kwargs)

    return wrapper

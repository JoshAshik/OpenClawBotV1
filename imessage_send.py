"""
iMessage / SMS sending through Messages.app (Phase 8). Mac only.

Called ONLY from bot.execute_approved_action after the user pressed Approve on a
send_imessage card. Tools never call this module.

Mechanics: `osascript -e <script> <handle> <body> <service>` — the body travels as an
argv item, never interpolated into the AppleScript, so quotes and newlines in the
message can't break or alter the script. First use triggers macOS's Automation prompt
("python wants to control Messages") — allow it once at the display (see
deploy/MAC_SETUP.md §6). SMS needs Text Message Forwarding from the iPhone.
"""

import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"
SEND_TIMEOUT_SECONDS = 30

SERVICES = ("iMessage", "SMS")

# `participant` is the Messages dictionary term since macOS 11; `buddy` was the older one.
APPLESCRIPT = """
on run argv
    set theHandle to item 1 of argv
    set theBody to item 2 of argv
    set theService to item 3 of argv
    tell application "Messages"
        if theService is "SMS" then
            set targetService to 1st account whose service type = SMS
        else
            set targetService to 1st account whose service type = iMessage
        end if
        set targetParticipant to participant theHandle of targetService
        send theBody to targetParticipant
    end tell
end run
""".strip()


def is_available() -> bool:
    """True on a Mac with osascript on PATH (Messages.app itself is assumed)."""
    return IS_MAC and shutil.which("osascript") is not None


def normalize_service(service: str | None) -> str:
    return "SMS" if (service or "").strip().upper() == "SMS" else "iMessage"


def build_command(handle: str, body: str, service: str = "iMessage") -> list[str]:
    """The osascript argv. Body and handle are separate arguments, not script text."""
    if not handle or not handle.strip():
        raise ValueError("recipient handle is empty")
    if not body or not body.strip():
        raise ValueError("message body is empty")
    return ["osascript", "-e", APPLESCRIPT, handle.strip(), body, normalize_service(service)]


def send_message(handle: str, body: str, service: str = "iMessage") -> str:
    """
    Send one message. Returns osascript's stdout (usually empty). Raises RuntimeError
    with Messages' error text on failure, or when not on a Mac.
    """
    if not is_available():
        raise RuntimeError("iMessage sending needs the Mac (Messages.app + osascript)")
    cmd = build_command(handle, body, service)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SEND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Messages did not respond within {SEND_TIMEOUT_SECONDS}s") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"osascript exit {proc.returncode}"
        if "not allowed" in err.lower() or "-1743" in err:
            err += " — allow python to control Messages in System Settings → Privacy → Automation"
        raise RuntimeError(err)
    logger.info(f"iMessage sent via {normalize_service(service)} to {handle}")
    return (proc.stdout or "").strip()

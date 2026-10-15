"""
Operations helpers for an always-on deployment (Phase 7).

  - run_doctor()       → health checks behind /doctor (tokens, OpenAI, Mac data
                          sources, disk, job timestamps, job queue)
  - backup_database()  → SQLite online backup of clawdbot.db, pruned to BACKUP_KEEP
  - run_update()       → git pull --ff-only + pip install, output for /update
  - request_exit()     → stop polling so the supervisor (launchd) restarts the bot

Nothing here is outbound; none of it goes through the approval flow.
"""

import asyncio
import logging
import os
import platform
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import db

logger = logging.getLogger(__name__)

STARTED_AT = datetime.now(timezone.utc)
_PROJECT_DIR = Path(__file__).parent

IS_MAC = sys.platform == "darwin"

# Settings keys written by the background jobs, read by /status and /doctor.
LAST_INDEX_KEY = "last_index_at"
LAST_BACKFILL_KEY = "last_backfill_at"
LAST_BACKUP_KEY = "last_backup_at"

_STATUS_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "na": "➖"}


def uptime_str() -> str:
    secs = int((datetime.now(timezone.utc) - STARTED_AT).total_seconds())
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h {mins}m"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


async def mark_now(key: str) -> None:
    await db.set_setting(key, datetime.now(timezone.utc).isoformat())


async def _age(key: str) -> tuple[str | None, float | None]:
    """(iso timestamp, age in minutes) for a settings timestamp; (None, None) if unset."""
    raw = await db.get_setting(key)
    if not raw:
        return None, None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw, None
    return raw, (datetime.now(timezone.utc) - dt).total_seconds() / 60


def fmt_ts(raw: str | None) -> str:
    """Render a stored UTC ISO timestamp in local time, or 'never'."""
    if not raw:
        return "never"
    try:
        return datetime.fromisoformat(raw).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw


# ── Doctor ─────────────────────────────────────────────────────────

def _check(name: str, status: str, detail: str = "") -> dict:
    return {"name": name, "status": status, "detail": detail}


def _check_gmail_token_sync(token_file: Path) -> dict:
    """Load one token, refresh it if expired. Runs in a thread (network)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    import gmail_module

    label = token_file.stem.replace("token_", "")
    age_days = (time.time() - token_file.stat().st_mtime) / 86400
    try:
        creds = Credentials.from_authorized_user_file(str(token_file), gmail_module.SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        if not creds.valid:
            return _check(f"Gmail '{label}'", "fail", f"token invalid — run /addaccount {label}")
        return _check(f"Gmail '{label}'", "ok", f"token valid (file {age_days:.0f}d old)")
    except RefreshError as e:
        hint = f"re-run /addaccount {label}"
        if "invalid_grant" in str(e):
            hint += ("; if this keeps happening every ~7 days the Google OAuth app is in "
                     "'Testing' — set it to 'In production'")
        return _check(f"Gmail '{label}'", "fail", f"refresh failed ({age_days:.0f}d old) — {hint}")
    except Exception as e:
        return _check(f"Gmail '{label}'", "fail", f"{type(e).__name__}: {str(e)[:120]}")


async def _check_gmail() -> list[dict]:
    tokens = sorted(_PROJECT_DIR.glob("token_*.json"))
    if not tokens:
        return [_check("Gmail", "warn", "no accounts connected — /addaccount <label>")]
    if not (_PROJECT_DIR / "credentials.json").exists():
        return [_check("Gmail", "fail", "credentials.json missing")]
    return [await asyncio.to_thread(_check_gmail_token_sync, t) for t in tokens]


async def _check_openai() -> dict:
    if not config.OPENAI_API_KEY:
        return _check("OpenAI", "fail", "OPENAI_API_KEY not set")
    import llm
    try:
        await asyncio.wait_for(llm._client().models.retrieve(config.OPENAI_MODEL), timeout=15)
        return _check("OpenAI", "ok", f"reachable, model {config.OPENAI_MODEL} available")
    except asyncio.TimeoutError:
        return _check("OpenAI", "fail", "timed out after 15s")
    except Exception as e:
        return _check("OpenAI", "fail", f"{type(e).__name__}: {str(e)[:120]}")


def _probe_sqlite(path: Path, probe_sql: str) -> tuple[str, str]:
    """Open a Mac data store read-only and run a trivial query. Returns (status, detail)."""
    if not path.exists():
        return "fail", f"not found at {path}"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute(probe_sql).fetchone()
        finally:
            conn.close()
        return "ok", f"readable ({path.name})"
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "unable to open" in msg or "authorization" in msg or "permission" in msg.lower():
            return "fail", "permission denied — grant Full Disk Access to the Python binary"
        return "fail", f"{msg[:100]} (schema changed?)"
    except Exception as e:
        return "fail", f"{type(e).__name__}: {str(e)[:100]}"


def _contacts_db_path() -> Path | None:
    base = Path.home() / "Library" / "Application Support" / "AddressBook"
    candidates = [base / "AddressBook-v22.abcddb"]
    candidates += sorted((base / "Sources").glob("*/AddressBook-v22.abcddb")) if (base / "Sources").exists() else []
    for c in candidates:
        if c.exists():
            return c
    return None


def _check_mac_sources_sync() -> list[dict]:
    if not IS_MAC:
        return [
            _check("iMessage (chat.db)", "na", "Mac only"),
            _check("WhatsApp (ChatStorage.sqlite)", "na", "Mac only"),
            _check("Contacts (AddressBook)", "na", "Mac only"),
        ]
    import imessage_module
    import whatsapp_module

    out = []
    status, detail = _probe_sqlite(imessage_module.db_path(), "SELECT ROWID FROM message LIMIT 1")
    out.append(_check("iMessage (chat.db)", status, detail))

    wa_path = whatsapp_module._find_db_path() or whatsapp_module._WA_CHAT_DB
    status, detail = _probe_sqlite(wa_path, "SELECT Z_PK FROM ZWAMESSAGE LIMIT 1")
    if status == "fail" and not wa_path.exists():
        status, detail = "warn", "not found — install and link the native WhatsApp app (read-only source)"
    out.append(_check("WhatsApp (ChatStorage.sqlite)", status, detail))

    cpath = _contacts_db_path()
    if cpath is None:
        out.append(_check("Contacts (AddressBook)", "warn", "AddressBook-v22.abcddb not found"))
    else:
        status, detail = _probe_sqlite(cpath, "SELECT Z_PK FROM ZABCDRECORD LIMIT 1")
        out.append(_check("Contacts (AddressBook)", status, detail))
    return out


def _check_disk() -> dict:
    usage = shutil.disk_usage(_PROJECT_DIR)
    free_gb = usage.free / 1e9
    pct = usage.free / usage.total * 100
    status = "ok" if free_gb > 5 else ("warn" if free_gb > 1 else "fail")
    return _check("Disk", status, f"{free_gb:.1f} GB free ({pct:.0f}%)")


def _check_files() -> list[dict]:
    out = []
    db_size = config.DB_PATH.stat().st_size / 1e6 if config.DB_PATH.exists() else 0
    out.append(_check("Database", "ok" if config.DB_PATH.exists() else "fail",
                      f"{config.DB_PATH.name} {db_size:.1f} MB"))
    log = _PROJECT_DIR / "clawdbot.log"
    if log.exists():
        out.append(_check("Log", "ok", f"{log.stat().st_size / 1e6:.1f} MB (rotates at 10 MB)"))
    if config.BACKUP_ENABLED:
        if config.BACKUP_DIR.exists():
            n = len(list(config.BACKUP_DIR.glob("clawdbot-*.db")))
            out.append(_check("Backup dir", "ok", f"{config.BACKUP_DIR} ({n} backups)"))
        else:
            out.append(_check("Backup dir", "warn", f"{config.BACKUP_DIR} not created yet (first backup pending)"))
    return out


async def _check_timestamps() -> list[dict]:
    out = []
    raw, age = await _age(LAST_INDEX_KEY)
    limit = config.INDEX_POLL_INTERVAL_MINUTES * 3
    if raw is None:
        out.append(_check("Last index", "warn", "never (runs 60s after startup)"))
    else:
        out.append(_check("Last index", "ok" if age is not None and age <= limit else "warn",
                          f"{fmt_ts(raw)} ({age:.0f} min ago)" if age is not None else fmt_ts(raw)))

    raw, age = await _age(LAST_BACKFILL_KEY)
    if raw is None:
        out.append(_check("Last backfill", "warn", "never (runs 2 min after startup, then daily)"))
    else:
        out.append(_check("Last backfill", "ok" if age is not None and age <= 48 * 60 else "warn",
                          fmt_ts(raw)))

    if config.BACKUP_ENABLED:
        raw, age = await _age(LAST_BACKUP_KEY)
        if raw is None:
            out.append(_check("Last backup", "warn", f"never (nightly at {config.BACKUP_HOUR:02d}:00)"))
        else:
            out.append(_check("Last backup", "ok" if age is not None and age <= 48 * 60 else "warn",
                              fmt_ts(raw)))
    return out


def _check_jobs(job_queue) -> dict:
    if job_queue is None:
        return _check("Jobs", "na", "no job queue")
    try:
        jobs = job_queue.jobs()
    except Exception as e:
        return _check("Jobs", "fail", f"{type(e).__name__}: {e}")
    if not jobs:
        return _check("Jobs", "warn", "no scheduled jobs")
    parts = []
    for j in jobs:
        nxt = j.next_t
        when = nxt.astimezone().strftime("%m-%d %H:%M") if nxt else "—"
        parts.append(f"{j.name}→{when}")
    return _check("Jobs", "ok", ", ".join(parts))


async def run_doctor(job_queue=None) -> list[dict]:
    """Run every health check; never raises. Returns [{name, status, detail}]."""
    checks: list[dict] = [
        _check("Runtime", "ok",
               f"Python {platform.python_version()} on {platform.system()} {platform.release()}"),
    ]
    sections = await asyncio.gather(
        _check_gmail(),
        _check_openai(),
        asyncio.to_thread(_check_mac_sources_sync),
        asyncio.to_thread(_check_disk),
        asyncio.to_thread(_check_files),
        _check_timestamps(),
        return_exceptions=True,
    )
    for sec in sections:
        if isinstance(sec, Exception):
            checks.append(_check("Doctor", "fail", f"check crashed: {type(sec).__name__}: {sec}"))
        elif isinstance(sec, list):
            checks.extend(sec)
        else:
            checks.append(sec)
    checks.append(_check_jobs(job_queue))
    return checks


def format_doctor(checks: list[dict]) -> str:
    """Plain text (no parse_mode) so paths and error text never break rendering."""
    counts = {"ok": 0, "warn": 0, "fail": 0}
    lines = []
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
        detail = f" — {c['detail']}" if c["detail"] else ""
        lines.append(f"{_STATUS_ICON.get(c['status'], '•')} {c['name']}{detail}")
    verdict = ("All good" if not counts["fail"] and not counts["warn"]
               else f"{counts['fail']} failing, {counts['warn']} warnings")
    return f"🩺 Clawdbot doctor — {verdict}\n\n" + "\n".join(lines)


# ── Backup ─────────────────────────────────────────────────────────

def _backup_sync() -> tuple[Path, int]:
    """SQLite online backup (consistent even while the bot writes). Returns (path, pruned)."""
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"clawdbot-{stamp}.db"

    src = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # Verify the copy opens and is intact before pruning anything.
    chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        ok = chk.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity check: {ok}")

    backups = sorted(config.BACKUP_DIR.glob("clawdbot-*.db"), key=lambda p: p.stat().st_mtime)
    pruned = 0
    for old in backups[:-config.BACKUP_KEEP] if config.BACKUP_KEEP > 0 else []:
        old.unlink(missing_ok=True)
        pruned += 1
    return dest, pruned


async def backup_database() -> tuple[Path, int]:
    """Run the backup in a thread and record the timestamp. Raises on failure."""
    dest, pruned = await asyncio.to_thread(_backup_sync)
    await mark_now(LAST_BACKUP_KEY)
    logger.info(f"Backup written: {dest} ({dest.stat().st_size / 1e6:.1f} MB), pruned {pruned}")
    return dest, pruned


# ── Update / restart ───────────────────────────────────────────────

async def _run_cmd(*argv: str, timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(_PROJECT_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _tail(text: str, lines: int = 12, width: int = 1200) -> str:
    kept = [ln for ln in text.strip().splitlines() if ln.strip()][-lines:]
    joined = "\n".join(kept)
    return joined[-width:]


async def run_update() -> tuple[bool, str]:
    """git pull --ff-only, then pip install -r requirements.txt. Returns (ok, report)."""
    report = []
    rc, out = await _run_cmd("git", "pull", "--ff-only", timeout=120)
    report.append(f"$ git pull --ff-only\n{_tail(out)}")
    if rc != 0:
        return False, "\n\n".join(report) + "\n\nUpdate aborted (git failed); not restarting."

    rc, out = await _run_cmd(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt",
                             timeout=600)
    report.append(f"$ pip install -r requirements.txt\n{_tail(out) or '(no output)'}")
    if rc != 0:
        return False, "\n\n".join(report) + "\n\nUpdate aborted (pip failed); not restarting."
    return True, "\n\n".join(report)


def request_exit(application, delay: float = 1.5) -> None:
    """Stop polling so main() returns with exit code 0 and the supervisor restarts us.
    A hard exit after 15s guards against a shutdown that hangs."""
    loop = asyncio.get_running_loop()

    def _stop():
        try:
            application.stop_running()
        except Exception:
            logger.exception("stop_running failed; forcing exit")
            os._exit(0)

    loop.call_later(delay, _stop)
    loop.call_later(delay + 15, os._exit, 0)

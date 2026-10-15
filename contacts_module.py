"""
Apple Contacts reader + handle → name resolver (Phase 8).

Mac only: reads the Contacts store(s) read-only —
  ~/Library/Application Support/AddressBook/AddressBook-v22.abcddb
  ~/Library/Application Support/AddressBook/Sources/<uuid>/AddressBook-v22.abcddb
(one per account: iCloud, Google, Exchange …). Needs Full Disk Access for the Python
binary, like chat.db. `CONTACTS_DB_PATH` (env or config) points the reader at one
specific file (a fixture on Windows) and makes `is_available()` true there.

Core-Data schema used (stable since macOS 10.9):
  ZABCDRECORD        Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME
  ZABCDPHONENUMBER   ZOWNER → ZABCDRECORD.Z_PK, ZFULLNUMBER
  ZABCDEMAILADDRESS  ZOWNER → ZABCDRECORD.Z_PK, ZADDRESS

Message handles are phone numbers (E.164 on iMessage, loosely formatted on SMS) or
emails; WhatsApp JIDs look like 14155550101@s.whatsapp.net. `normalize_handle` folds
all of them to a comparable key so one resolver serves every source.
"""

import logging
import os
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BASE = Path.home() / "Library" / "Application Support" / "AddressBook"
_DB_NAME = "AddressBook-v22.abcddb"

_RESOLVER_TTL_SECONDS = 3600

_WA_JID_RE = re.compile(r"^(\d+)@(s\.whatsapp\.net|c\.us)$", re.I)
_NON_DIGIT_RE = re.compile(r"\D+")


# ── Normalization ───────────────────────────────────────────────────

def _default_country() -> str:
    raw = os.environ.get("CONTACTS_DEFAULT_COUNTRY")
    if raw is None:
        try:
            import config
            raw = getattr(config, "CONTACTS_DEFAULT_COUNTRY", "1")
        except Exception:
            raw = "1"
    return (raw or "1").lstrip("+")


def normalize_phone(raw: str | None, default_country: str | None = None) -> str | None:
    """
    Fold a phone number into E.164 ("+14155550101") without a phone library.

    Rules: an explicit "+" or "00" prefix wins; a 10-digit number (or 11 starting with
    "1") under the default country "1" is North American; otherwise the default country
    code is prepended. Fewer than 7 digits (short codes, extensions) → None.
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    country = (default_country or _default_country()).lstrip("+")

    # Drop extensions ("x123", "ext. 5") before counting digits.
    s = re.split(r"(?i)\s*(?:x|ext\.?|extension)\s*\d+$", s)[0]

    explicit = s.startswith("+")
    digits = _NON_DIGIT_RE.sub("", s)
    if not explicit and digits.startswith("00") and len(digits) > 9:
        digits, explicit = digits[2:], True
    if len(digits) < 7:
        return None

    if explicit:
        return "+" + digits
    if country == "1":
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
    if digits.startswith(country) and len(digits) > 10:
        return "+" + digits  # already carries the country code
    return "+" + country + digits


def normalize_handle(handle: str | None) -> str | None:
    """
    Comparable key for any message handle: lower-cased email, E.164 phone, or None.
    Accepts iMessage handles, SMS numbers with punctuation, and WhatsApp JIDs.
    """
    if not handle:
        return None
    h = handle.strip()
    if not h:
        return None
    m = _WA_JID_RE.match(h)
    if m:
        return normalize_phone("+" + m.group(1))
    if "@" in h:
        return h.lower()
    return normalize_phone(h)


def is_phone_like(handle: str | None) -> bool:
    return bool(handle) and "@" not in handle and _NON_DIGIT_RE.sub("", handle) != ""


# ── Store access ───────────────────────────────────────────────────

def db_paths() -> list[Path]:
    """Every AddressBook store to read: the override, or the root store plus all Sources."""
    override = os.environ.get("CONTACTS_DB_PATH", "")
    if not override:
        try:
            import config
            override = getattr(config, "CONTACTS_DB_PATH", "") or ""
        except Exception:
            override = ""
    if override:
        p = Path(override).expanduser()
        return [p] if p.is_file() else sorted(p.glob(f"**/{_DB_NAME}")) if p.is_dir() else []

    paths = []
    root = _DEFAULT_BASE / _DB_NAME
    if root.exists():
        paths.append(root)
    sources = _DEFAULT_BASE / "Sources"
    if sources.exists():
        paths.extend(sorted(sources.glob(f"*/{_DB_NAME}")))
    return paths


def is_available() -> bool:
    return bool(db_paths())


def _display_name(first: str, last: str, org: str, nick: str) -> str:
    full = " ".join(p for p in (first.strip(), last.strip()) if p)
    return full or org.strip() or nick.strip()


def _read_store(path: Path) -> list[dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        people: dict[int, dict] = {}
        for r in conn.execute(
            "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME FROM ZABCDRECORD"
        ):
            name = _display_name(r["ZFIRSTNAME"] or "", r["ZLASTNAME"] or "",
                                 r["ZORGANIZATION"] or "", r["ZNICKNAME"] or "")
            if not name:
                continue
            people[r["Z_PK"]] = {
                "name": name,
                "first": (r["ZFIRSTNAME"] or "").strip(),
                "last": (r["ZLASTNAME"] or "").strip(),
                "organization": (r["ZORGANIZATION"] or "").strip(),
                "phones": [],
                "emails": [],
            }
        for r in conn.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
            p = people.get(r["ZOWNER"])
            num = normalize_phone(r["ZFULLNUMBER"])
            if p is not None and num and num not in p["phones"]:
                p["phones"].append(num)
        for r in conn.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
            p = people.get(r["ZOWNER"])
            addr = (r["ZADDRESS"] or "").strip().lower()
            if p is not None and addr and "@" in addr and addr not in p["emails"]:
                p["emails"].append(addr)
        return [p for p in people.values() if p["phones"] or p["emails"]]
    finally:
        conn.close()


def load_contacts() -> list[dict]:
    """
    All people with at least one phone or email, merged across stores:
    [{name, first, last, organization, phones: [E.164], emails: [lower]}].
    Raises if a store exists but can't be opened (permissions) so callers can report it.
    """
    merged: dict[str, dict] = {}
    for path in db_paths():
        try:
            rows = _read_store(path)
        except sqlite3.OperationalError as e:
            raise RuntimeError(f"Contacts store {path} unreadable: {e}") from e
        for p in rows:
            key = p["name"].casefold()
            cur = merged.get(key)
            if cur is None:
                merged[key] = p
                continue
            for field in ("phones", "emails"):
                for v in p[field]:
                    if v not in cur[field]:
                        cur[field].append(v)
            if not cur["organization"]:
                cur["organization"] = p["organization"]
    return sorted(merged.values(), key=lambda p: p["name"].casefold())


# ── Resolver ───────────────────────────────────────────────────────

class ContactResolver:
    """handle → name lookups over a loaded contact list."""

    def __init__(self, contacts: list[dict]):
        self.contacts = contacts
        self.built_at = time.monotonic()
        self._by_key: dict[str, dict] = {}
        for c in contacts:
            for k in c["phones"] + c["emails"]:
                self._by_key.setdefault(k, c)

    def lookup(self, handle: str | None) -> dict | None:
        key = normalize_handle(handle)
        return self._by_key.get(key) if key else None

    def resolve(self, handle: str | None) -> str:
        """Display name for a handle, or the handle itself when unknown (never raises)."""
        c = self.lookup(handle)
        return c["name"] if c else (handle or "")

    def __len__(self) -> int:
        return len(self.contacts)


def identity(handle: str) -> str:
    return handle


_cached: ContactResolver | None = None


def get_resolver(max_age_seconds: int = _RESOLVER_TTL_SECONDS,
                 force: bool = False) -> ContactResolver | None:
    """
    Cached resolver, rebuilt when older than max_age_seconds. None when no store is
    available or it can't be read (logged once per failure). Sync — call via to_thread.
    """
    global _cached
    if not is_available():
        return None
    if (not force and _cached is not None
            and time.monotonic() - _cached.built_at < max_age_seconds):
        return _cached
    try:
        _cached = ContactResolver(load_contacts())
        logger.info(f"Contacts loaded: {len(_cached)} people with a phone or email")
    except Exception as e:
        logger.warning(f"Contacts unavailable: {e}")
        return _cached  # keep a stale resolver rather than none
    return _cached


def resolve_name(handle: str) -> str:
    """Module-level convenience: cached resolver or identity."""
    r = get_resolver()
    return r.resolve(handle) if r else handle


def resolver_fn():
    """The callable the indexer passes as resolve_name (identity when unavailable)."""
    r = get_resolver()
    return r.resolve if r else identity

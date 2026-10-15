"""
Gmail integration — OAuth 2.0, multi-account support.

Each account gets a label (e.g. "personal", "work") and its own token file.
All accounts share the same credentials.json (same Google Cloud project).
Both emails must be added as test users in the Google Cloud Console.

Setup:
  1. Go to console.cloud.google.com → create project → enable Gmail API
  2. Create OAuth 2.0 credentials (Desktop app) → download as credentials.json
  3. Place credentials.json in the Clawdbot directory
  4. Add both email addresses as test users in OAuth consent screen
  5. Use /addaccount <label> in Telegram to connect each account
"""

import base64
import io
import re
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from PyPDF2 import PdfReader

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]

_PROJECT_DIR = Path(__file__).parent
_CREDENTIALS_PATH = _PROJECT_DIR / "credentials.json"

# {label: gmail service object}
_services: dict[str, object] = {}

# Active account label
_active_account: str | None = None


def _token_path(label: str) -> Path:
    """Each account gets its own token file: token_personal.json, token_work.json, etc."""
    return _PROJECT_DIR / f"token_{label}.json"


def get_connected_accounts() -> list[dict]:
    """Return list of accounts that have token files on disk."""
    accounts = []
    for token_file in _PROJECT_DIR.glob("token_*.json"):
        label = token_file.stem.replace("token_", "")
        # Try to extract email from the token
        email = ""
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if hasattr(creds, "client_id"):
                # Build a temporary service to get the email
                if creds and (creds.valid or (creds.expired and creds.refresh_token)):
                    if creds.expired:
                        creds.refresh(Request())
                    svc = build("gmail", "v1", credentials=creds)
                    profile = svc.users().getProfile(userId="me").execute()
                    email = profile.get("emailAddress", "")
        except Exception:
            pass
        accounts.append({"label": label, "email": email, "token_file": str(token_file)})
    return accounts


def get_active_account() -> str | None:
    return _active_account


def set_active_account(label: str) -> bool:
    """Switch to a different account. Returns False if no token exists for that label."""
    global _active_account
    if not _token_path(label).exists():
        return False
    _active_account = label
    return True


def connect_account(label: str) -> str:
    """Run OAuth flow for a new account. Returns the email address connected."""
    if not _CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {_CREDENTIALS_PATH}. Download OAuth credentials from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    _token_path(label).write_text(creds.to_json())

    # Clear cached service for this label so it rebuilds
    _services.pop(label, None)

    # Get the email address
    svc = _get_service(label)
    profile = svc.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress", "unknown")

    # Auto-set as active if it's the first or only account
    global _active_account
    if _active_account is None:
        _active_account = label

    return email


def _get_service(label: str | None = None):
    """Get Gmail API service for the given account label (or active account)."""
    global _active_account

    if label is None:
        label = _active_account
    if label is None:
        raise RuntimeError("No Gmail account connected. Use /addaccount <label> first.")

    if label in _services:
        return _services[label]

    token_file = _token_path(label)
    if not token_file.exists():
        raise RuntimeError(f"No token for account '{label}'. Use /addaccount {label} to connect.")

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        else:
            raise RuntimeError(
                f"Token for '{label}' is invalid. Use /addaccount {label} to re-authenticate."
            )

    service = build("gmail", "v1", credentials=creds)
    _services[label] = service
    return service


# ── Email Operations (all accept optional account= parameter) ────

def list_emails(max_results: int = 10, query: str = "", account: str | None = None) -> list[dict]:
    service = _get_service(account)
    params = {"userId": "me", "maxResults": max_results}
    if query:
        params["q"] = query

    results = service.users().messages().list(**params).execute()
    messages = results.get("messages", [])

    emails = []
    for msg_stub in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_stub["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        emails.append({
            "id": msg["id"],
            "thread_id": msg.get("threadId", ""),
            "snippet": msg.get("snippet", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "labels": msg.get("labelIds", []),
            "unread": "UNREAD" in msg.get("labelIds", []),
        })

    return emails


def list_email_ids(max_results: int = 2000, query: str = "", account: str | None = None) -> list[str]:
    """
    Page through the mailbox and return up to max_results message ids
    (newest first). Cheap: one list call per 500 ids, no per-message fetch.
    """
    service = _get_service(account)
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        params = {"userId": "me", "maxResults": min(500, max_results - len(ids))}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        results = service.users().messages().list(**params).execute()
        ids.extend(m["id"] for m in results.get("messages", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return ids


def read_email(msg_id: str, account: str | None = None) -> dict:
    service = _get_service(account)
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    body = _extract_body(msg.get("payload", {}))
    attachments = _list_attachments(msg.get("payload", {}))

    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "body": body,
        "labels": msg.get("labelIds", []),
        "attachments": attachments,
    }


def _extract_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            raw = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
            clean = re.sub(r"<[^>]+>", "", raw)
            clean = re.sub(r"\s+", " ", clean).strip()
            return clean

    for part in payload.get("parts", []):
        nested = _extract_body(part)
        if nested:
            return nested

    return "(unable to extract body)"


def create_draft(to: str, subject: str, body: str, account: str | None = None) -> dict:
    service = _get_service(account)
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()

    return {"draft_id": draft["id"], "message_id": draft["message"]["id"]}


def _build_mime(to: str, subject: str, body: str, cc: str = "", bcc: str = "",
                html: bool = False, attachments: list[dict] | None = None):
    """
    Build the outgoing message. attachments: [{"filename": str, "data": bytes}].
    html=True sends a multipart/alternative with a plain-text fallback.
    """
    if html:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(re.sub(r"<[^>]+>", "", body)))
        body_part.attach(MIMEText(body, "html"))
    else:
        body_part = MIMEText(body)

    if attachments:
        message = MIMEMultipart()
        message.attach(body_part)
        for att in attachments:
            part = MIMEApplication(att["data"], Name=att["filename"])
            part["Content-Disposition"] = f'attachment; filename="{att["filename"]}"'
            message.attach(part)
    else:
        message = body_part

    message["to"] = to
    if cc:
        message["cc"] = cc
    if bcc:
        message["bcc"] = bcc
    message["subject"] = subject
    return message


def send_email(to: str, subject: str, body: str, account: str | None = None,
               cc: str = "", bcc: str = "", html: bool = False,
               attachments: list[dict] | None = None) -> dict:
    service = _get_service(account)
    message = _build_mime(to, subject, body, cc=cc, bcc=bcc, html=html, attachments=attachments)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()

    return {"message_id": sent["id"], "labels": sent.get("labelIds", [])}


def get_attachment_bytes(msg_id: str, filename: str, account: str | None = None) -> bytes:
    """Fetch one attachment's raw bytes from an email, matched by filename."""
    email = read_email(msg_id, account=account)
    for att in email.get("attachments", []):
        if att["filename"].lower() == filename.lower():
            return download_attachment(msg_id, att["attachment_id"], account=account)
    available = ", ".join(a["filename"] for a in email.get("attachments", [])) or "none"
    raise RuntimeError(f"No attachment '{filename}' on email {msg_id} (available: {available})")


def get_message_meta(msg_id: str, account: str | None = None) -> dict:
    """Fetch threading metadata for a message: RFC Message-ID, References, thread id, addressing."""
    service = _get_service(account)
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["Subject", "From", "To", "Reply-To", "Message-ID", "References", "Date"],
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "reply_to": headers.get("reply-to", ""),
        "subject": headers.get("subject", "(no subject)"),
        "date": headers.get("date", ""),
        "rfc_message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
    }


def reply_to_email(msg_id: str, body: str, account: str | None = None,
                   cc: str = "", bcc: str = "", html: bool = False,
                   attachments: list[dict] | None = None) -> dict:
    """
    Reply in-thread. Uses the RFC 2822 Message-ID header (not the Gmail API id)
    for In-Reply-To/References so threading works in the recipient's client too,
    plus the Gmail threadId so it threads in the sender's own mailbox.
    """
    meta = get_message_meta(msg_id, account=account)
    service = _get_service(account)

    subject = meta["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    to = meta["reply_to"] or meta["from"]
    message = _build_mime(to, subject, body, cc=cc, bcc=bcc, html=html, attachments=attachments)
    if meta["rfc_message_id"]:
        message["In-Reply-To"] = meta["rfc_message_id"]
        message["References"] = f"{meta['references']} {meta['rfc_message_id']}".strip()

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    send_body: dict = {"raw": raw}
    if meta["thread_id"]:
        send_body["threadId"] = meta["thread_id"]
    sent = service.users().messages().send(userId="me", body=send_body).execute()

    return {"message_id": sent["id"], "to": to, "subject": subject}


def search_emails(query: str, max_results: int = 10, account: str | None = None) -> list[dict]:
    return list_emails(max_results=max_results, query=query, account=account)


def mark_as_read(msg_id: str, account: str | None = None) -> None:
    service = _get_service(account)
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# ── Attachment Handling ───────────────────────────────────────────

def _list_attachments(payload: dict) -> list[dict]:
    """Extract attachment metadata from an email payload."""
    attachments = []
    _walk_parts_for_attachments(payload, attachments)
    return attachments


def _walk_parts_for_attachments(part: dict, out: list[dict]) -> None:
    filename = part.get("filename", "")
    body = part.get("body", {})
    attachment_id = body.get("attachmentId")

    if filename and attachment_id:
        out.append({
            "filename": filename,
            "attachment_id": attachment_id,
            "mime_type": part.get("mimeType", ""),
            "size": body.get("size", 0),
        })

    for sub in part.get("parts", []):
        _walk_parts_for_attachments(sub, out)


def download_attachment(msg_id: str, attachment_id: str, account: str | None = None) -> bytes:
    """Download raw attachment bytes from Gmail."""
    service = _get_service(account)
    att = service.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=attachment_id,
    ).execute()
    data = att.get("data", "")
    return base64.urlsafe_b64decode(data)


def read_pdf_attachment(msg_id: str, attachment_id: str, account: str | None = None) -> str:
    """Download a PDF attachment and extract its text content."""
    raw_bytes = download_attachment(msg_id, attachment_id, account=account)
    reader = PdfReader(io.BytesIO(raw_bytes))

    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text and text.strip():
            pages.append(f"--- Page {i + 1} ---\n{text.strip()}")

    if not pages:
        return "(PDF has no extractable text — may be scanned/image-based)"

    return "\n\n".join(pages)


def get_email_with_pdf_text(msg_id: str, account: str | None = None) -> dict:
    """Read an email and auto-extract text from all PDF attachments."""
    email = read_email(msg_id, account=account)

    pdf_texts = []
    for att in email.get("attachments", []):
        if att["mime_type"] == "application/pdf" or att["filename"].lower().endswith(".pdf"):
            try:
                text = read_pdf_attachment(msg_id, att["attachment_id"], account=account)
                pdf_texts.append({"filename": att["filename"], "text": text})
            except Exception as e:
                pdf_texts.append({"filename": att["filename"], "text": f"(extraction failed: {e})"})

    email["pdf_texts"] = pdf_texts
    return email


def search_pdfs(query: str, max_results: int = 5, account: str | None = None) -> list[dict]:
    """Search for emails with PDF attachments, extract text from each."""
    search_query = f"has:attachment filename:pdf {query}"
    emails = list_emails(max_results=max_results, query=search_query, account=account)

    results = []
    for em in emails:
        full = get_email_with_pdf_text(em["id"], account=account)
        for pdf in full.get("pdf_texts", []):
            results.append({
                "email_id": em["id"],
                "email_subject": em["subject"],
                "email_from": em["from"],
                "email_date": em["date"],
                "filename": pdf["filename"],
                "text": pdf["text"],
            })

    return results

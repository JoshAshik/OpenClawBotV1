"""
OneDrive integration — Microsoft Graph API for personal OneDrive.

Setup:
  1. Go to https://portal.azure.com → Azure Active Directory → App registrations
  2. New registration → name "Clawdbot" → Personal Microsoft accounts only
  3. Add redirect URI: http://localhost:8400 (Mobile and desktop applications)
  4. Copy Application (client) ID → add to .env as ONEDRIVE_CLIENT_ID
  5. Under API permissions → add Microsoft Graph → Files.Read, Files.Read.All
  6. Use /connectonedrive in Telegram to authenticate
"""

import io
import json
import logging
from pathlib import Path

import httpx
from PyPDF2 import PdfReader

import config

logger = logging.getLogger(__name__)

_PROJECT_DIR = Path(__file__).parent
_TOKEN_PATH = _PROJECT_DIR / "onedrive_token.json"

_GRAPH_URL = "https://graph.microsoft.com/v1.0"
_AUTH_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
_SCOPES = ["Files.Read", "Files.Read.All", "offline_access"]

_access_token: str | None = None
_refresh_token: str | None = None


def _get_client_id() -> str:
    client_id = getattr(config, "ONEDRIVE_CLIENT_ID", "") or ""
    if not client_id:
        raise RuntimeError("ONEDRIVE_CLIENT_ID not set in .env")
    return client_id


def is_connected() -> bool:
    return _TOKEN_PATH.exists()


def _load_tokens() -> bool:
    global _access_token, _refresh_token
    if not _TOKEN_PATH.exists():
        return False
    try:
        data = json.loads(_TOKEN_PATH.read_text())
        _access_token = data.get("access_token")
        _refresh_token = data.get("refresh_token")
        return bool(_access_token)
    except Exception:
        return False


def _save_tokens() -> None:
    _TOKEN_PATH.write_text(json.dumps({
        "access_token": _access_token,
        "refresh_token": _refresh_token,
    }))


def get_auth_url() -> str:
    """Generate the OAuth authorization URL. User visits this in browser."""
    client_id = _get_client_id()
    scopes = "%20".join(_SCOPES)
    return (
        f"{_AUTH_URL}/authorize?"
        f"client_id={client_id}&response_type=code&redirect_uri=http://localhost:8400"
        f"&scope={scopes}&response_mode=query"
    )


def exchange_code(code: str) -> str:
    """Exchange authorization code for tokens. Returns the user's display name."""
    global _access_token, _refresh_token
    client_id = _get_client_id()

    with httpx.Client() as client:
        resp = client.post(
            f"{_AUTH_URL}/token",
            data={
                "client_id": client_id,
                "code": code,
                "redirect_uri": "http://localhost:8400",
                "grant_type": "authorization_code",
                "scope": " ".join(_SCOPES),
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _access_token = data["access_token"]
    _refresh_token = data.get("refresh_token", "")
    _save_tokens()

    # Get user name
    with httpx.Client() as client:
        resp = client.get(
            f"{_GRAPH_URL}/me",
            headers={"Authorization": f"Bearer {_access_token}"},
        )
        if resp.is_success:
            return resp.json().get("displayName", "Unknown")
    return "Connected"


def _refresh() -> None:
    """Refresh the access token."""
    global _access_token, _refresh_token
    if not _refresh_token:
        raise RuntimeError("No refresh token. Use /connectonedrive to re-authenticate.")

    client_id = _get_client_id()
    with httpx.Client() as client:
        resp = client.post(
            f"{_AUTH_URL}/token",
            data={
                "client_id": client_id,
                "refresh_token": _refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(_SCOPES),
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _access_token = data["access_token"]
    _refresh_token = data.get("refresh_token", _refresh_token)
    _save_tokens()


def _headers() -> dict:
    if not _access_token:
        if not _load_tokens():
            raise RuntimeError("OneDrive not connected. Use /connectonedrive first.")
    return {"Authorization": f"Bearer {_access_token}"}


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Make a Graph API request with auto-refresh on 401."""
    with httpx.Client() as client:
        resp = client.request(method, url, headers=_headers(), **kwargs)
        if resp.status_code == 401:
            _refresh()
            resp = client.request(method, url, headers=_headers(), **kwargs)
        resp.raise_for_status()
        return resp


# ── File Operations ───────────────────────────────────────────────

def list_files(folder_path: str = "/", file_types: list[str] | None = None) -> list[dict]:
    """
    List files in OneDrive. Default: root folder.
    file_types: filter by extension, e.g. [".pdf", ".docx"]
    """
    if folder_path == "/":
        url = f"{_GRAPH_URL}/me/drive/root/children"
    else:
        url = f"{_GRAPH_URL}/me/drive/root:/{folder_path.strip('/')}:/children"

    all_files = []
    while url:
        resp = _request("GET", url, params={"$top": "200"})
        data = resp.json()

        for item in data.get("value", []):
            if "file" not in item:
                continue  # Skip folders

            name = item["name"]
            if file_types and not any(name.lower().endswith(ext) for ext in file_types):
                continue

            all_files.append({
                "id": item["id"],
                "name": name,
                "path": item.get("parentReference", {}).get("path", "") + "/" + name,
                "size": item.get("size", 0),
                "modified": item.get("lastModifiedDateTime", ""),
                "mime_type": item.get("file", {}).get("mimeType", ""),
                "download_url": item.get("@microsoft.graph.downloadUrl", ""),
            })

        url = data.get("@odata.nextLink")

    return all_files


def list_all_pdfs() -> list[dict]:
    """Search entire OneDrive for PDF files."""
    url = f"{_GRAPH_URL}/me/drive/root/search(q='.pdf')"
    resp = _request("GET", url)
    data = resp.json()

    pdfs = []
    for item in data.get("value", []):
        if "file" not in item:
            continue
        if not item["name"].lower().endswith(".pdf"):
            continue

        pdfs.append({
            "id": item["id"],
            "name": item["name"],
            "path": item.get("parentReference", {}).get("path", "") + "/" + item["name"],
            "size": item.get("size", 0),
            "modified": item.get("lastModifiedDateTime", ""),
            "download_url": item.get("@microsoft.graph.downloadUrl", ""),
        })

    return pdfs


def download_file(file_id: str) -> bytes:
    """Download a file's content by ID."""
    resp = _request("GET", f"{_GRAPH_URL}/me/drive/items/{file_id}/content")
    return resp.content


def download_file_text(file_id: str, filename: str) -> str:
    """Download a file and extract text. Supports PDF and plain text files."""
    raw = download_file(file_id)

    if filename.lower().endswith(".pdf"):
        return _extract_pdf_text(raw)
    elif filename.lower().endswith((".txt", ".csv", ".md", ".log")):
        return raw.decode("utf-8", errors="replace")
    else:
        return ""


def _extract_pdf_text(raw: bytes) -> str:
    reader = PdfReader(io.BytesIO(raw))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text and text.strip():
            pages.append(f"--- Page {i + 1} ---\n{text.strip()}")

    if not pages:
        return "(PDF has no extractable text)"
    return "\n\n".join(pages)


def search_files(query: str) -> list[dict]:
    """Search OneDrive files by name/content."""
    url = f"{_GRAPH_URL}/me/drive/root/search(q='{query}')"
    resp = _request("GET", url)
    data = resp.json()

    results = []
    for item in data.get("value", []):
        if "file" not in item:
            continue
        results.append({
            "id": item["id"],
            "name": item["name"],
            "path": item.get("parentReference", {}).get("path", "") + "/" + item["name"],
            "size": item.get("size", 0),
            "modified": item.get("lastModifiedDateTime", ""),
        })
    return results

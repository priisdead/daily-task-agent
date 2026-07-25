"""Pull recent email from one or more Gmail accounts via the official API
(read-only scope). Each account has its own OAuth token file — see
scripts/gmail_auth.py and GMAIL_TOKEN_FILES in .env."""
import base64
import logging
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from . import config, db

log = logging.getLogger("gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _credentials(token_file: str) -> Credentials:
    token_path = Path(token_file)
    if not token_path.exists():
        raise RuntimeError(
            f"Gmail token not found at {token_path}. Run scripts/gmail_auth.py "
            "on your own machine for this account, then copy the token file to the server."
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return creds


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree and return the best-effort plain-text body."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text
    # fall back to text/html stripped very roughly
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        import re
        html = base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="replace")
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _fetch_account(token_file: str, query: str | None = None, max_pages: int = 1) -> int:
    """Fetch mail for a single account. Returns count stored."""
    service = build("gmail", "v1", credentials=_credentials(token_file), cache_discovery=False)
    account = service.users().getProfile(userId="me").execute().get("emailAddress", token_file)
    refs, page_token = [], None
    for _ in range(max_pages):
        resp = service.users().messages().list(
            userId="me", q=query or config.GMAIL_QUERY, maxResults=100,
            pageToken=page_token,
        ).execute()
        refs += resp.get("messages", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    stored = 0
    for ref in refs:
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="full"
        ).execute()
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        body = _extract_body(msg.get("payload", {}))[:8000]
        sender = headers.get("from", "")
        sender_addr = (re.findall(r"[\w.+-]+@[\w.-]+", sender) or [""])[0].lower()
        if any(pat in sender.lower() for pat in config.IGNORE_SENDERS):
            continue  # automated/noise sender — don't store at all
        direction = (
            "outgoing"
            if "SENT" in msg.get("labelIds", []) or sender_addr in config.OWNER_EMAILS
            else "incoming"
        )
        # Group by the address the mail was originally sent to (so a hub inbox
        # receiving forwards still shows mail under the original business
        # address). Falls back to the connected account.
        to_addr = (re.findall(r"[\w.+-]+@[\w.-]+",
                              headers.get("delivered-to", "") or headers.get("to", ""))
                   or [account])[0].lower()
        if db.save_email(
            gmail_id=msg["id"],
            account=to_addr if direction == "incoming" else account,
            sender=sender,
            subject=headers.get("subject", ""),
            snippet=msg.get("snippet", ""),
            body=body,
            ts=headers.get("date", ""),
            direction=direction,
        ):
            stored += 1
    log.info("gmail[%s]: stored %d new emails", account, stored)
    return stored


def fetch_recent_emails(query: str | None = None, max_pages: int = 1) -> int:
    """Fetch mail across ALL configured accounts. One account failing
    (expired token, network) never blocks the others."""
    total = 0
    for token_file in config.GMAIL_TOKEN_FILES:
        try:
            total += _fetch_account(token_file, query=query, max_pages=max_pages)
        except Exception:
            log.exception("gmail fetch failed for %s — continuing", token_file)
    return total

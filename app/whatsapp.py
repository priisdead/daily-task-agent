"""WhatsApp Cloud API webhook handling: verification, signature check,
payload parsing, and media download."""
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config, db

log = logging.getLogger("whatsapp")


def verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate X-Hub-Signature-256 so only Meta can post to us."""
    if not config.WHATSAPP_APP_SECRET:
        return True  # signature checking disabled (not recommended in production)
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        config.WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


async def download_media(media_id: str) -> str | None:
    """Fetch a media object (image/document/audio) and store it locally.
    Returns the local file path, or None on failure."""
    headers = {"Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            meta = await client.get(f"{config.GRAPH_API_BASE}/{media_id}", headers=headers)
            meta.raise_for_status()
            info = meta.json()
            url = info["url"]
            mime = info.get("mime_type", "application/octet-stream")
            ext = mime.split("/")[-1].split(";")[0] or "bin"
            blob = await client.get(url, headers=headers)
            blob.raise_for_status()
            path = Path(config.MEDIA_DIR) / f"{media_id}.{ext}"
            path.write_bytes(blob.content)
            return str(path)
    except Exception:
        log.exception("media download failed for %s", media_id)
        return None


async def handle_webhook_payload(payload: dict) -> int:
    """Parse a Meta webhook POST body; store every inbound message.
    Returns the number of new messages stored."""
    stored = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Map wa_id -> profile name from the contacts block
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                wa_id = msg.get("id")
                sender = msg.get("from", "")
                msg_type = msg.get("type", "unknown")
                ts_raw = msg.get("timestamp")
                ts = (
                    datetime.fromtimestamp(int(ts_raw), tz=timezone.utc).isoformat()
                    if ts_raw else db.utcnow()
                )

                body, media_path = "", None
                if msg_type == "text":
                    body = msg.get("text", {}).get("body", "")
                elif msg_type in ("image", "document", "video", "audio", "sticker"):
                    media = msg.get(msg_type, {})
                    body = media.get("caption", "") or media.get("filename", "") or f"[{msg_type}]"
                    media_id = media.get("id")
                    if media_id:
                        media_path = await download_media(media_id)
                elif msg_type == "button":
                    body = msg.get("button", {}).get("text", "")
                elif msg_type == "interactive":
                    inter = msg.get("interactive", {})
                    body = (
                        inter.get("button_reply", {}).get("title")
                        or inter.get("list_reply", {}).get("title")
                        or "[interactive]"
                    )
                else:
                    body = f"[{msg_type} message]"

                if db.save_wa_message(
                    wa_id, sender, names.get(sender, ""), body, msg_type, media_path, ts
                ):
                    stored += 1
    return stored

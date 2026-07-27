"""Email + password login with signed session cookies.
No extra dependencies: PBKDF2 from hashlib, HMAC-signed cookie values."""
import base64
import hashlib
import hmac
import os
import time

from . import config

SESSION_COOKIE = "session"
SESSION_TTL = 30 * 24 * 3600   # 30 days


# ── passwords ────────────────────────────────────────────────────────────────

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Returns (salt, hash). PBKDF2-SHA256, 200k iterations."""
    salt = salt or os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return salt, h


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not (password and salt and expected_hash):
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(h, expected_hash)


# ── sessions (HMAC-signed cookie, no server-side storage) ────────────────────

def _secret() -> bytes:
    return (config.SECRET_KEY or config.DASHBOARD_TOKEN or "dev-secret").encode()


def make_session(email: str) -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    payload = f"{email}|{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_session(cookie_value: str | None) -> str | None:
    """Returns the email if the cookie is valid and unexpired, else None."""
    if not cookie_value:
        return None
    try:
        payload = base64.urlsafe_b64decode(cookie_value.encode()).decode()
        email, exp, sig = payload.rsplit("|", 2)
        base = f"{email}|{exp}"
        good = hmac.new(_secret(), base.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        if time.time() > int(exp):
            return None
        return email
    except Exception:
        return None

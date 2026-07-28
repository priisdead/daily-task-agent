"""Central configuration, loaded from environment / .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Meta / WhatsApp
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
# Sending (morning WhatsApp update): the phone_number_id from the Meta app
# dashboard, and YOUR personal WhatsApp number (country code, no +, e.g.
# 9198xxxxxxxx). Leave empty to disable WhatsApp sending.
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WA_NOTIFY_TO = os.getenv("WA_NOTIFY_TO", "917778988358")
# Approved template name for business-initiated messages outside the 24h
# window (optional but recommended). Params: {{1}} open, {{2} high, {{3}} new.
WA_TEMPLATE_NAME = os.getenv("WA_TEMPLATE_NAME", "")
WA_TEMPLATE_LANG = os.getenv("WA_TEMPLATE_LANG", "en")
WA_DIGEST_HOUR = int(os.getenv("WA_DIGEST_HOUR", "9"))  # local hour, daily

# LLM provider: "claude" or "gemini"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "claude").lower()

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Gmail
# Scans BOTH received mail and your sent replies (so task status can update
# automatically when you reply to a client).
GMAIL_QUERY = os.getenv(
    "GMAIL_QUERY",
    "newer_than:1d (in:inbox OR in:sent) -category:{promotions social}",
)
# Your own sending addresses (comma-separated). Mail FROM any of these is
# treated as YOUR reply (moves tasks to in-progress/done) even when it arrives
# via forwarding/BCC into a hub inbox.
OWNER_EMAILS = [
    e.strip().lower()
    for e in os.getenv("OWNER_EMAILS", "").split(",")
    if e.strip()
]

# Senders to ignore completely (substring match, case-insensitive). Extend via
# the IGNORE_SENDERS env var (comma-separated) — e.g. "linkedin.com,quora.com"
_DEFAULT_IGNORES = (
    "noreply,no-reply,no_reply,donotreply,do-not-reply,mailer-daemon,"
    "postmaster@,notifications@,newsletter@,mail-noreply@google.com"
)
IGNORE_SENDERS = [
    s.strip().lower()
    for s in (_DEFAULT_IGNORES + "," + os.getenv("IGNORE_SENDERS", "")).split(",")
    if s.strip()
]

# ── Notifications: daily digest + failure alerts ────────────────────────────
# Sent by plain SMTP. For Gmail: turn on 2-Step Verification, then create an
# App Password (myaccount.google.com/apppasswords) and use it as SMTP_PASS.
# Leave SMTP_USER/SMTP_PASS/NOTIFY_TO empty to disable sending (digest still
# viewable at /digest).
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_TO = os.getenv("NOTIFY_TO", "priyanka@thesolfactory.com")  # comma-separated
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))  # local (TIMEZONE) hour, daily

# App
# Master admin token (Braj & Lokesh) — sees everything, all pages.
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")

# Per-department credentials. Each department gets its own secret token;
# opening the dashboard with that token shows ONLY that department's tasks.
# Format: "logistics:tok1,production:tok2,accounts:tok3,design:tok4"
# (an "admin:tok" entry grants full admin like DASHBOARD_TOKEN).
DEPT_TOKENS: dict[str, str] = {}   # token -> department
for _pair in os.getenv("DEPT_TOKENS", "").replace(";", ",").split(","):
    _pair = _pair.strip()
    _sep = ":" if ":" in _pair else ("=" if "=" in _pair else None)
    if not _sep:
        continue
    _d, _t = _pair.split(_sep, 1)
    _d, _t = _d.strip().lower(), _t.strip()
    if _d and _t:
        DEPT_TOKENS[_t] = _d

DEPARTMENTS = ["admin", "logistics", "production", "accounts", "design",
               "implementation", "qc", "management", "hr"]

# May the AI close tasks? Default NO: when the AI believes a task is
# complete (e.g. owner replied "sent"), it only moves it to in_progress —
# a HUMAN presses Done (with remark). Set "true" to restore auto-closing.
AI_MAY_CLOSE_TASKS = os.getenv("AI_MAY_CLOSE_TASKS", "false").lower() == "true"

# Purchase-order lifecycle (Orders page). Edit to match your workflow.
PO_STATUSES = ["received", "in_production", "ready", "shipped",
               "delivered", "paid", "cancelled"]

# ── Production sheet sync (feeds the Orders pages from the same Google
# Sheet as the CXO Production dashboard). Paste the SAME spreadsheet link/ID
# and AIza API key you use in the dashboard's connect panel. Empty = off.
SHEETS_API_KEY = os.getenv("SHEETS_API_KEY", "")
PROD_SHEET_ID = os.getenv("PROD_SHEET_ID", "")
PROD_SHEET_TAB = os.getenv("PROD_SHEET_TAB", "Sheet1")
SHEET_SYNC_MINUTES = int(os.getenv("SHEET_SYNC_MINUTES", "60"))
SHEET_RISK_DAYS = int(os.getenv("SHEET_RISK_DAYS", "2"))
# Only raise at-risk tasks for meaningful pending quantities and reasonably
# current ship-ready dates (ignore stale rows from weeks ago).
SHEET_RISK_MIN_PENDING = int(os.getenv("SHEET_RISK_MIN_PENDING", "100"))
SHEET_RISK_LOOKBACK_DAYS = int(os.getenv("SHEET_RISK_LOOKBACK_DAYS", "7"))

# ── Email login (RBAC) ───────────────────────────────────────────────────────
# Signs session cookies; set to a long random string in production.
SECRET_KEY = os.getenv("SECRET_KEY", "")
# First admin account, created automatically when no users exist yet.
# Log in with these, then add everyone else on the Users page and change
# this password from there.
INIT_ADMIN_EMAIL = os.getenv("INIT_ADMIN_EMAIL", "")
INIT_ADMIN_PASSWORD = os.getenv("INIT_ADMIN_PASSWORD", "")
# Postgres connection string (e.g. from Neon). When set, all data lives there
# and survives restarts/redeploys. When empty, a local SQLite file is used.
DATABASE_URL = os.getenv("DATABASE_URL", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "agent.db"))
MEDIA_DIR = os.getenv("MEDIA_DIR", str(BASE_DIR / "data" / "media"))
# How often the agent scans for new messages and extracts tasks (minutes)
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")

Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(MEDIA_DIR).mkdir(parents=True, exist_ok=True)

# Gmail token sources — two ways, combinable:
# 1) GMAIL_TOKEN_FILES: comma-separated paths to token files on disk.
# 2) GMAIL_TOKEN_JSON_<NAME> env vars: paste the token file's JSON content
#    directly (handy on Render/Railway); each is written to data/ at startup.
_token_files = [
    p.strip()
    for p in os.getenv("GMAIL_TOKEN_FILES", os.getenv("GMAIL_TOKEN_FILE", "")).split(",")
    if p.strip()
]
_data_dir = Path(DATABASE_PATH).parent
for _key in sorted(os.environ):
    if _key.startswith("GMAIL_TOKEN_JSON_") and os.environ[_key].strip():
        _name = _key[len("GMAIL_TOKEN_JSON_"):].lower() or "token"
        _path = _data_dir / f"gmail_{_name}.json"
        _path.write_text(os.environ[_key])
        if str(_path) not in _token_files:
            _token_files.append(str(_path))
if not _token_files:
    _token_files = [str(BASE_DIR / "data" / "gmail_token.json")]
GMAIL_TOKEN_FILES = _token_files

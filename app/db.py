"""SQLite storage: raw messages (WhatsApp + email) and extracted tasks."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wa_id       TEXT UNIQUE,          -- Meta message id (dedup on webhook retries)
    sender      TEXT,                 -- phone number
    sender_name TEXT,                 -- WhatsApp profile name
    body        TEXT,
    msg_type    TEXT,                 -- text / image / document / audio / ...
    media_path  TEXT,
    ts          TEXT,                 -- ISO timestamp of the message
    received_at TEXT,
    processed   INTEGER DEFAULT 0     -- 1 once a digest run has consumed it
);

CREATE TABLE IF NOT EXISTS emails (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id    TEXT UNIQUE,
    account     TEXT,                 -- which of your inboxes received it
    sender      TEXT,
    subject     TEXT,
    snippet     TEXT,
    body        TEXT,
    ts          TEXT,
    processed   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client      TEXT,                 -- client name (best known)
    contact     TEXT,                 -- phone or email address
    channel     TEXT,                 -- whatsapp / email / both
    request     TEXT,                 -- what the client needs
    deadline    TEXT,                 -- free-text deadline ("Friday", "2026-07-30", or "")
    priority    TEXT,                 -- high / normal / low
    source      TEXT,                 -- short excerpt of the originating message(s)
    status      TEXT DEFAULT 'open',  -- open / done / dropped
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    wa_count    INTEGER,
    email_count INTEGER,
    new_tasks   INTEGER,
    note        TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(SCHEMA)


# ── writes ────────────────────────────────────────────────────────────────────

def save_wa_message(wa_id, sender, sender_name, body, msg_type, media_path, ts) -> bool:
    """Insert a WhatsApp message; returns False if it was a duplicate delivery."""
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO wa_messages (wa_id, sender, sender_name, body, msg_type,"
                " media_path, ts, received_at) VALUES (?,?,?,?,?,?,?,?)",
                (wa_id, sender, sender_name, body, msg_type, media_path, ts, utcnow()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def save_email(gmail_id, account, sender, subject, snippet, body, ts) -> bool:
    with get_db() as db:
        try:
            db.execute(
                "INSERT INTO emails (gmail_id, account, sender, subject, snippet, body, ts)"
                " VALUES (?,?,?,?,?,?,?)",
                (gmail_id, account, sender, subject, snippet, body, ts),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def add_task(t: dict) -> None:
    now = utcnow()
    with get_db() as db:
        db.execute(
            "INSERT INTO tasks (client, contact, channel, request, deadline, priority,"
            " source, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                t.get("client", "Unknown"), t.get("contact", ""), t.get("channel", ""),
                t.get("request", ""), t.get("deadline", ""), t.get("priority", "normal"),
                t.get("source", ""), "open", now, now,
            ),
        )


def set_task_status(task_id: int, status: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow(), task_id),
        )


def mark_processed(table: str, ids: list) -> None:
    if not ids or table not in ("wa_messages", "emails"):
        return
    with get_db() as db:
        q = f"UPDATE {table} SET processed = 1 WHERE id IN ({','.join('?' * len(ids))})"
        db.execute(q, ids)


def record_run(started_at, wa_count, email_count, new_tasks, note="") -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO runs (started_at, finished_at, wa_count, email_count,"
            " new_tasks, note) VALUES (?,?,?,?,?,?)",
            (started_at, utcnow(), wa_count, email_count, new_tasks, note),
        )


# ── reads ─────────────────────────────────────────────────────────────────────

def unprocessed_wa_messages() -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM wa_messages WHERE processed = 0 ORDER BY ts")]


def unprocessed_emails() -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM emails WHERE processed = 0 ORDER BY ts")]


def open_tasks() -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM tasks WHERE status = 'open' ORDER BY client, id")]


def tasks_done_today(today_prefix: str) -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM tasks WHERE status = 'done' AND updated_at LIKE ?"
            " ORDER BY updated_at DESC", (today_prefix + "%",))]


def all_tasks(limit: int = 500) -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]


def all_wa_messages(limit: int = 500) -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM wa_messages ORDER BY id DESC LIMIT ?", (limit,))]


def all_emails(limit: int = 500) -> list:
    with get_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, gmail_id, account, sender, subject, snippet, ts, processed"
            " FROM emails ORDER BY id DESC LIMIT ?", (limit,))]


def last_run() -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

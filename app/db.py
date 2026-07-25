"""Storage layer. Uses Postgres when DATABASE_URL is set (e.g. a free Neon
database — data survives restarts/redeploys), otherwise a local SQLite file."""
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

IS_PG = bool(config.DATABASE_URL)

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3

_ID = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS wa_messages (
    id          {_ID},
    wa_id       TEXT UNIQUE,
    sender      TEXT,
    sender_name TEXT,
    body        TEXT,
    msg_type    TEXT,
    media_path  TEXT,
    ts          TEXT,
    received_at TEXT,
    processed   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS emails (
    id          {_ID},
    gmail_id    TEXT UNIQUE,
    account     TEXT,
    direction   TEXT DEFAULT 'incoming',
    sender      TEXT,
    subject     TEXT,
    snippet     TEXT,
    body        TEXT,
    ts          TEXT,
    processed   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
    id          {_ID},
    client      TEXT,
    contact     TEXT,
    channel     TEXT,
    request     TEXT,
    deadline    TEXT,
    priority    TEXT,
    source      TEXT,
    status      TEXT DEFAULT 'open',
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          {_ID},
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


def _q(sql: str) -> str:
    """Translate '?' placeholders to '%s' for Postgres."""
    return sql.replace("?", "%s") if IS_PG else sql


@contextmanager
def get_db():
    if IS_PG:
        conn = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    else:
        conn = sqlite3.connect(config.DATABASE_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        if IS_PG:
            db.execute(SCHEMA)
        else:
            db.executescript(SCHEMA)
    # migration for databases created before the 'direction' column existed
    try:
        with get_db() as db:
            db.execute("ALTER TABLE emails ADD COLUMN direction TEXT DEFAULT 'incoming'")
    except Exception:
        pass  # column already there


def _rows(result) -> list:
    return [dict(r) for r in result.fetchall()]


# ── writes ────────────────────────────────────────────────────────────────────

def save_wa_message(wa_id, sender, sender_name, body, msg_type, media_path, ts) -> bool:
    """Insert a WhatsApp message; returns False if it was a duplicate delivery."""
    sql = (
        "INSERT INTO wa_messages (wa_id, sender, sender_name, body, msg_type,"
        " media_path, ts, received_at) VALUES (?,?,?,?,?,?,?,?)"
    )
    sql += " ON CONFLICT (wa_id) DO NOTHING" if IS_PG else ""
    if not IS_PG:
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    with get_db() as db:
        cur = db.execute(_q(sql), (wa_id, sender, sender_name, body, msg_type,
                                   media_path, ts, utcnow()))
        return cur.rowcount > 0


def save_email(gmail_id, account, sender, subject, snippet, body, ts,
               direction="incoming") -> bool:
    sql = (
        "INSERT INTO emails (gmail_id, account, direction, sender, subject, snippet,"
        " body, ts) VALUES (?,?,?,?,?,?,?,?)"
    )
    sql += " ON CONFLICT (gmail_id) DO NOTHING" if IS_PG else ""
    if not IS_PG:
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    with get_db() as db:
        cur = db.execute(_q(sql), (gmail_id, account, direction, sender, subject,
                                   snippet, body, ts))
        return cur.rowcount > 0


def add_task(t: dict) -> None:
    now = utcnow()
    with get_db() as db:
        db.execute(
            _q("INSERT INTO tasks (client, contact, channel, request, deadline,"
               " priority, source, status, created_at, updated_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?)"),
            (
                t.get("client", "Unknown"), t.get("contact", ""), t.get("channel", ""),
                t.get("request", ""), t.get("deadline", ""), t.get("priority", "normal"),
                t.get("source", ""), "open", now, now,
            ),
        )


def set_task_status(task_id: int, status: str) -> None:
    with get_db() as db:
        db.execute(
            _q("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?"),
            (status, utcnow(), task_id),
        )


def mark_processed(table: str, ids: list) -> None:
    if not ids or table not in ("wa_messages", "emails"):
        return
    with get_db() as db:
        placeholders = ",".join("?" * len(ids))
        db.execute(_q(f"UPDATE {table} SET processed = 1 WHERE id IN ({placeholders})"), ids)


def record_run(started_at, wa_count, email_count, new_tasks, note="") -> None:
    with get_db() as db:
        db.execute(
            _q("INSERT INTO runs (started_at, finished_at, wa_count, email_count,"
               " new_tasks, note) VALUES (?,?,?,?,?,?)"),
            (started_at, utcnow(), wa_count, email_count, new_tasks, note),
        )


# ── reads ─────────────────────────────────────────────────────────────────────

def unprocessed_wa_messages() -> list:
    with get_db() as db:
        return _rows(db.execute(
            "SELECT * FROM wa_messages WHERE processed = 0 ORDER BY ts"))


def unprocessed_emails() -> list:
    with get_db() as db:
        return _rows(db.execute(
            "SELECT * FROM emails WHERE processed = 0 ORDER BY ts"))


def open_tasks() -> list:
    """Everything still needing attention: open + in-progress."""
    with get_db() as db:
        return _rows(db.execute(
            "SELECT * FROM tasks WHERE status IN ('open', 'in_progress')"
            " ORDER BY client, id"))


def tasks_done_today(today_prefix: str) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM tasks WHERE status = 'done' AND updated_at LIKE ?"
               " ORDER BY updated_at DESC"), (today_prefix + "%",)))


def all_tasks(limit: int = 500) -> list:
    with get_db() as db:
        return _rows(db.execute(_q("SELECT * FROM tasks ORDER BY id DESC LIMIT ?"), (limit,)))


def all_wa_messages(limit: int = 500) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM wa_messages ORDER BY id DESC LIMIT ?"), (limit,)))


def all_emails(limit: int = 500) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT id, gmail_id, account, direction, sender, subject, snippet, ts,"
               " processed FROM emails ORDER BY id DESC LIMIT ?"), (limit,)))


def last_run() -> dict | None:
    with get_db() as db:
        rows = _rows(db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1"))
        return rows[0] if rows else None

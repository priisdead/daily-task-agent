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
    department  TEXT DEFAULT '',
    deadline    TEXT,
    priority    TEXT,
    source      TEXT,
    status      TEXT DEFAULT 'open',
    remark      TEXT DEFAULT '',
    done_by     TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS skipped_msgs (
    id          {_ID},
    sender      TEXT,
    reason      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          {_ID},
    level       TEXT,
    source      TEXT,
    message     TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id          {_ID},
    email       TEXT UNIQUE,
    salt        TEXT,
    pass_hash   TEXT,
    department  TEXT DEFAULT '',
    role        TEXT DEFAULT 'member',
    active      INTEGER DEFAULT 1,
    created_at  TEXT
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
    # migration for databases created before the 'department' column existed
    try:
        with get_db() as db:
            db.execute("ALTER TABLE tasks ADD COLUMN department TEXT DEFAULT ''")
    except Exception:
        pass  # column already there
    # migrations for completion remarks
    for _col in ("remark", "done_by"):
        try:
            with get_db() as db:
                db.execute(f"ALTER TABLE tasks ADD COLUMN {_col} TEXT DEFAULT ''")
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
            _q("INSERT INTO tasks (client, contact, channel, request, department,"
               " deadline, priority, source, status, created_at, updated_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?)"),
            (
                t.get("client", "Unknown"), t.get("contact", ""), t.get("channel", ""),
                t.get("request", ""), t.get("department", ""), t.get("deadline", ""),
                t.get("priority", "normal"), t.get("source", ""), "open", now, now,
            ),
        )


def set_task_department(task_id: int, department: str) -> None:
    with get_db() as db:
        db.execute(
            _q("UPDATE tasks SET department = ?, updated_at = ? WHERE id = ?"),
            (department, utcnow(), task_id),
        )


def set_task_status(task_id: int, status: str, remark: str | None = None,
                    done_by: str | None = None) -> None:
    sets = ["status = ?", "updated_at = ?"]
    vals: list = [status, utcnow()]
    if remark is not None and remark.strip():
        sets.append("remark = ?")
        vals.append(remark.strip()[:500])
    if done_by:
        sets.append("done_by = ?")
        vals.append(done_by[:200])
    vals.append(task_id)
    with get_db() as db:
        db.execute(_q(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?"), vals)


def mark_processed(table: str, ids: list) -> None:
    if not ids or table not in ("wa_messages", "emails"):
        return
    with get_db() as db:
        placeholders = ",".join("?" * len(ids))
        db.execute(_q(f"UPDATE {table} SET processed = 1 WHERE id IN ({placeholders})"), ids)


# ── users (email login / RBAC) ───────────────────────────────────────────────

def get_user(email: str) -> dict | None:
    with get_db() as db:
        rows = _rows(db.execute(
            _q("SELECT * FROM users WHERE lower(email) = ?"), (email.strip().lower(),)))
        return rows[0] if rows else None


def list_users() -> list:
    with get_db() as db:
        return _rows(db.execute("SELECT * FROM users ORDER BY role DESC, email"))


def count_users() -> int:
    with get_db() as db:
        rows = _rows(db.execute("SELECT COUNT(*) AS n FROM users"))
        return rows[0]["n"] if rows else 0


def create_user(email: str, salt: str, pass_hash: str,
                department: str, role: str = "member") -> bool:
    sql = ("INSERT INTO users (email, salt, pass_hash, department, role,"
           " active, created_at) VALUES (?,?,?,?,?,1,?)")
    sql += " ON CONFLICT (email) DO NOTHING" if IS_PG else ""
    if not IS_PG:
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    with get_db() as db:
        cur = db.execute(_q(sql), (email.strip().lower(), salt, pass_hash,
                                   department, role, utcnow()))
        return cur.rowcount > 0


def update_user(email: str, *, department: str | None = None,
                role: str | None = None, active: int | None = None,
                salt: str | None = None, pass_hash: str | None = None) -> None:
    sets, vals = [], []
    for col, val in (("department", department), ("role", role),
                     ("active", active), ("salt", salt), ("pass_hash", pass_hash)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return
    vals.append(email.strip().lower())
    with get_db() as db:
        db.execute(_q(f"UPDATE users SET {', '.join(sets)} WHERE lower(email) = ?"), vals)


def log_event(level: str, source: str, message: str) -> None:
    """Record a health event (error/warn/info) so morning updates can report
    problems. Never raises — health logging must not break the pipeline."""
    try:
        with get_db() as db:
            db.execute(
                _q("INSERT INTO events (level, source, message, created_at)"
                   " VALUES (?,?,?,?)"),
                (level, source, str(message)[:300], utcnow()),
            )
    except Exception:
        pass


def events_since(iso_ts: str, limit: int = 100) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM events WHERE created_at >= ?"
               " ORDER BY id DESC LIMIT ?"), (iso_ts, limit)))


def save_skipped(entries: list) -> None:
    """Persist the AI's 'no task because...' decisions so they can be audited
    on the /skipped page and in the daily digest."""
    now = utcnow()
    with get_db() as db:
        for e in entries:
            db.execute(
                _q("INSERT INTO skipped_msgs (sender, reason, created_at)"
                   " VALUES (?,?,?)"),
                (str(e.get("from", "?"))[:200], str(e.get("why", ""))[:300], now),
            )


def skipped_since(iso_ts: str, limit: int = 200) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM skipped_msgs WHERE created_at >= ?"
               " ORDER BY id DESC LIMIT ?"), (iso_ts, limit)))


def tasks_created_since(iso_ts: str) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM tasks WHERE created_at >= ? ORDER BY id DESC"),
            (iso_ts,)))


def runs_since(iso_ts: str) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM runs WHERE started_at >= ? ORDER BY id DESC"),
            (iso_ts,)))


def reset_processed() -> int:
    """Mark ALL stored mail/WA as unprocessed so the next scan re-extracts
    tasks from everything (used after improving the extraction prompt).
    Existing open tasks are shown to the AI, so they won't be duplicated."""
    with get_db() as db:
        n = db.execute("UPDATE emails SET processed = 0").rowcount or 0
        n += db.execute("UPDATE wa_messages SET processed = 0").rowcount or 0
    return n


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


def pipeline_stats() -> dict:
    """How much of the captured mail/WA has actually been through the AI."""
    out = {}
    with get_db() as db:
        for table in ("emails", "wa_messages"):
            rows = _rows(db.execute(
                f"SELECT processed, COUNT(*) AS n FROM {table} GROUP BY processed"))
            total = sum(r["n"] for r in rows)
            done = sum(r["n"] for r in rows if r["processed"])
            out[table] = {"total": total, "processed": done, "queued": total - done}
        trows = _rows(db.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"))
        out["tasks"] = {r["status"]: r["n"] for r in trows}
        out["tasks"]["total"] = sum(out["tasks"].values())
        out["recent_runs"] = _rows(db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 5"))
    return out


def last_run() -> dict | None:
    with get_db() as db:
        rows = _rows(db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1"))
        return rows[0] if rows else None

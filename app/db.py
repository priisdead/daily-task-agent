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
    scheduled_for TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS production_rows (
    id           {_ID},
    uid          TEXT,
    po_number    TEXT,
    customer     TEXT,
    description  TEXT,
    po_qty       INTEGER DEFAULT 0,
    done_qty     INTEGER DEFAULT 0,
    pending_qty  INTEGER DEFAULT 0,
    ship_ready   TEXT,
    priority     TEXT,
    prod_start   TEXT,
    sheet_status TEXT,
    synced_at    TEXT
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id          {_ID},
    po_number   TEXT UNIQUE,
    client      TEXT,
    status      TEXT DEFAULT 'received',
    notes       TEXT DEFAULT '',
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS tracking_rows (
    id            {_ID},
    po_number     TEXT,
    customer      TEXT,
    po_date       TEXT,
    cargo_ready   TEXT,
    stages_json   TEXT,
    stages_done   INTEGER DEFAULT 0,
    stages_total  INTEGER DEFAULT 0,
    current_stage TEXT,
    current_owner TEXT,
    track_status  TEXT,
    synced_at     TEXT
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
    # migrations for completion remarks + PO linkage + scheduling
    for _col in ("remark", "done_by", "po_number", "scheduled_for"):
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
               " po_number, deadline, priority, source, status, created_at, updated_at)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
            (
                t.get("client", "Unknown"), t.get("contact", ""), t.get("channel", ""),
                t.get("request", ""), t.get("department", ""),
                t.get("po_number", ""), t.get("deadline", ""),
                t.get("priority", "normal"), t.get("source", ""), "open", now, now,
            ),
        )


def set_task_schedule(task_id: int, day_iso: str) -> None:
    """Schedule a task for a date (YYYY-MM-DD) or clear with ''."""
    with get_db() as db:
        db.execute(
            _q("UPDATE tasks SET scheduled_for = ?, updated_at = ? WHERE id = ?"),
            (day_iso, utcnow(), task_id),
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


# ── production sheet rows ────────────────────────────────────────────────────

def replace_production_rows(records: list) -> None:
    """The sheet is the source of truth — replace our copy wholesale.
    Bulk insert on ONE connection: a remote Postgres charges a network
    round-trip per statement, so 320 single INSERTs took ~a minute."""
    now = utcnow()
    rows = [(r["uid"], r["po_number"], r["customer"], r["description"],
             r["po_qty"], r["done_qty"], r["pending_qty"], r["ship_ready"],
             r["priority"], r["prod_start"], r["sheet_status"], now)
            for r in records]
    sql = _q("INSERT INTO production_rows (uid, po_number, customer,"
             " description, po_qty, done_qty, pending_qty, ship_ready,"
             " priority, prod_start, sheet_status, synced_at)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
    with get_db() as db:
        db.execute("DELETE FROM production_rows")
        if not rows:
            return
        cur = db.cursor() if IS_PG else db
        cur.executemany(sql, rows)


def upsert_pos_bulk(pairs: list) -> None:
    """Create many PO records in one connection. pairs = [(po_number, client)]."""
    now = utcnow()
    seen: dict[str, str] = {}
    for po, client in pairs:
        po = (po or "").strip().upper()
        if po and (po not in seen or (client and not seen[po])):
            seen[po] = client or seen.get(po, "")
    if not seen:
        return
    sql = ("INSERT INTO purchase_orders (po_number, client, status, created_at,"
           " updated_at) VALUES (?,?,?,?,?)")
    sql += " ON CONFLICT (po_number) DO NOTHING" if IS_PG else ""
    if not IS_PG:
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    rows = [(po, client, "received", now, now) for po, client in seen.items()]
    with get_db() as db:
        cur = db.cursor() if IS_PG else db
        cur.executemany(_q(sql), rows)


def production_for_po(po_number: str) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM production_rows WHERE po_number = ?"
               " ORDER BY uid"), (po_number.strip().upper(),)))


def replace_tracking_rows(records: list) -> None:
    """The stage-tracking sheet is the source of truth — replace wholesale,
    bulk insert on one connection (same pattern as production_rows)."""
    now = utcnow()
    rows = [(r["po_number"], r["customer"], r["po_date"], r["cargo_ready"],
             r["stages_json"], r["stages_done"], r["stages_total"],
             r["current_stage"], r["current_owner"], r["track_status"], now)
            for r in records]
    sql = _q("INSERT INTO tracking_rows (po_number, customer, po_date,"
             " cargo_ready, stages_json, stages_done, stages_total,"
             " current_stage, current_owner, track_status, synced_at)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?)")
    with get_db() as db:
        db.execute("DELETE FROM tracking_rows")
        if not rows:
            return
        cur = db.cursor() if IS_PG else db
        cur.executemany(sql, rows)


def tracking_all() -> list:
    with get_db() as db:
        return _rows(db.execute(
            "SELECT * FROM tracking_rows ORDER BY po_date, po_number"))


def tracking_for_po(po_number: str) -> dict | None:
    with get_db() as db:
        rows = _rows(db.execute(
            _q("SELECT * FROM tracking_rows WHERE UPPER(po_number) = ?"),
            ((po_number or "").strip().upper(),)))
        return rows[0] if rows else None


def production_all() -> list:
    with get_db() as db:
        return _rows(db.execute(
            "SELECT * FROM production_rows"
            " ORDER BY CASE WHEN ship_ready = '' THEN 1 ELSE 0 END,"
            " ship_ready, po_number, uid"))


def production_last_sync() -> str:
    with get_db() as db:
        rows = _rows(db.execute(
            "SELECT synced_at FROM production_rows LIMIT 1"))
        return rows[0]["synced_at"] if rows else ""


# ── purchase orders ──────────────────────────────────────────────────────────

def upsert_po(po_number: str, client: str = "") -> None:
    """Create the PO record if it doesn't exist yet (first sighting)."""
    po_number = (po_number or "").strip().upper()
    if not po_number:
        return
    now = utcnow()
    sql = ("INSERT INTO purchase_orders (po_number, client, status, created_at,"
           " updated_at) VALUES (?,?,?,?,?)")
    sql += " ON CONFLICT (po_number) DO NOTHING" if IS_PG else ""
    if not IS_PG:
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    with get_db() as db:
        db.execute(_q(sql), (po_number, client or "", "received", now, now))
        # backfill client name if we learn it later
        if client:
            db.execute(
                _q("UPDATE purchase_orders SET client = ? "
                   "WHERE po_number = ? AND (client IS NULL OR client = '')"),
                (client, po_number))


def list_pos() -> list:
    """POs with open/total task counts, newest first."""
    with get_db() as db:
        pos = _rows(db.execute("SELECT * FROM purchase_orders ORDER BY id DESC"))
        counts = _rows(db.execute(
            "SELECT po_number, COUNT(*) AS total,"
            " SUM(CASE WHEN status IN ('open','in_progress') THEN 1 ELSE 0 END) AS open_n"
            " FROM tasks WHERE po_number <> '' GROUP BY po_number"))
    by_po = {c["po_number"]: c for c in counts}
    for p in pos:
        c = by_po.get(p["po_number"], {})
        p["task_total"] = c.get("total", 0) or 0
        p["task_open"] = c.get("open_n", 0) or 0
    return pos


def get_po(po_number: str) -> dict | None:
    with get_db() as db:
        rows = _rows(db.execute(
            _q("SELECT * FROM purchase_orders WHERE po_number = ?"),
            (po_number.strip().upper(),)))
        return rows[0] if rows else None


def update_po(po_number: str, *, status: str | None = None,
              notes: str | None = None, client: str | None = None) -> None:
    sets, vals = ["updated_at = ?"], [utcnow()]
    for col, val in (("status", status), ("notes", notes), ("client", client)):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    vals.append(po_number.strip().upper())
    with get_db() as db:
        db.execute(_q(f"UPDATE purchase_orders SET {', '.join(sets)}"
                      " WHERE po_number = ?"), vals)


def tasks_for_po(po_number: str) -> list:
    with get_db() as db:
        return _rows(db.execute(
            _q("SELECT * FROM tasks WHERE po_number = ? ORDER BY id DESC"),
            (po_number.strip().upper(),)))


def emails_mentioning(text: str, limit: int = 100) -> list:
    like = f"%{text.strip()}%"
    op = "ILIKE" if IS_PG else "LIKE"   # case-insensitive on both engines
    with get_db() as db:
        return _rows(db.execute(
            _q(f"SELECT id, account, direction, sender, subject, snippet, ts"
               f" FROM emails WHERE subject {op} ? OR snippet {op} ? OR body {op} ?"
               f" ORDER BY id DESC LIMIT ?"),
            (like, like, like, limit)))


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


def dedupe_open_tasks() -> int:
    """Merge EXACT duplicate open tasks (same client + same request text +
    same PO after normalisation). Keeps one — preferring the copy a human
    already moved to in_progress, else the oldest — and closes the rest
    with a remark pointing at the kept task, so nothing is deleted and any
    merge can be reopened. Similar-but-not-identical tasks are left alone:
    judging those is human work. Returns how many copies were closed."""
    import re as _re

    def _norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    groups: dict[tuple, list] = {}
    for t in open_tasks():
        key = (_norm(t.get("client", "")), _norm(t.get("request", "")),
               (t.get("po_number") or "").strip().upper())
        if key[1]:  # never group tasks with empty request text
            groups.setdefault(key, []).append(t)

    closed = 0
    now = utcnow()
    with get_db() as db:
        for dupes in groups.values():
            if len(dupes) < 2:
                continue
            # keep: in_progress beats open (work already started), then oldest id
            dupes.sort(key=lambda t: (0 if t["status"] == "in_progress" else 1,
                                      t["id"]))
            keep = dupes[0]
            for extra in dupes[1:]:
                db.execute(
                    _q("UPDATE tasks SET status = 'done', remark = ?,"
                       " done_by = ?, updated_at = ? WHERE id = ?"),
                    (f"auto-merged: duplicate of task #{keep['id']}",
                     "agent (dedup)", now, extra["id"]),
                )
                closed += 1
    if closed:
        log_event("info", "dedup", f"auto-merged {closed} duplicate task(s)")
    return closed


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

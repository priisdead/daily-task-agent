"""Weekly database backup: every table dumped to JSON inside one zip.
Downloadable anytime at /backup (admin); emailed weekly when SMTP is
configured. The data now runs the company — it deserves a copy that lives
outside the database provider."""
import io
import json
import logging
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, db, notify

log = logging.getLogger("backup")

TABLES = ["tasks", "emails", "wa_messages", "purchase_orders",
          "production_rows", "users", "skipped_msgs", "events", "runs"]


def build_backup() -> tuple[str, bytes]:
    """Returns (filename, zip_bytes) with one JSON file per table."""
    stamp = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%Y-%m-%d")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        counts = {}
        for table in TABLES:
            try:
                with db.get_db() as conn:
                    rows = db._rows(conn.execute(f"SELECT * FROM {table}"))  # noqa: S608
            except Exception:
                log.exception("backup: could not dump %s", table)
                rows = []
            counts[table] = len(rows)
            z.writestr(f"{table}.json",
                       json.dumps(rows, ensure_ascii=False, default=str, indent=1))
        z.writestr("MANIFEST.json", json.dumps(
            {"created": datetime.utcnow().isoformat() + "Z",
             "row_counts": counts}, indent=1))
    return f"task-agent-backup-{stamp}.zip", buf.getvalue()


def run_weekly_backup() -> None:
    """Scheduled job: build the backup and email it. If email isn't
    configured, record an event so the reminder shows in the digest."""
    try:
        filename, data = build_backup()
        size_kb = len(data) // 1024
        if notify.configured():
            ok = notify.send_email_attachment(
                f"[Task Agent] Weekly backup {filename}",
                f"Attached: full database backup ({size_kb} KB). "
                f"Keep a copy somewhere safe.\n\nTables: {', '.join(TABLES)}.",
                filename, data,
            )
            db.log_event("info" if ok else "error", "backup",
                         f"weekly backup {'emailed' if ok else 'EMAIL FAILED'} "
                         f"({size_kb} KB)")
        else:
            db.log_event("warn", "backup",
                         f"weekly backup built ({size_kb} KB) but SMTP not "
                         f"configured — download it from /backup")
        log.info("weekly backup done: %s (%d KB)", filename, size_kb)
    except Exception:
        log.exception("weekly backup failed")
        db.log_event("error", "backup", "weekly backup FAILED — see logs")

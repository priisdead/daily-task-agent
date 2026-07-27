"""Daily digest: a plain-language summary of the last 24 hours, emailed every
morning (DIGEST_HOUR, local time) and always viewable at /digest."""
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from . import config, db, notify


def build() -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    now_local = datetime.now(ZoneInfo(config.TIMEZONE))

    stats = db.pipeline_stats()
    new_tasks = db.tasks_created_since(since)
    skipped = db.skipped_since(since)
    runs = db.runs_since(since)
    open_tasks = db.open_tasks()
    high = [t for t in open_tasks if t.get("priority") == "high"]
    with_deadline = [t for t in open_tasks if (t.get("deadline") or "").strip()]
    queued = stats["emails"]["queued"] + stats["wa_messages"]["queued"]

    lines = [
        f"TASK AGENT — DAILY DIGEST · {now_local.strftime('%A, %d %B %Y')}",
        "",
        f"Open tasks: {len(open_tasks)}  ({len(high)} high priority)",
        f"New tasks in last 24h: {len(new_tasks)}",
        f"Mails set aside with a reason: {len(skipped)}",
        f"Scans in last 24h: {len(runs)}",
        f"Backlog waiting for AI: {queued}"
        + ("  <-- ATTENTION: queue is not empty" if queued else ""),
    ]

    problems = _health_lines(since)
    if problems:
        lines += ["", "PROBLEMS IN LAST 24H — agent health:"]
        lines += [f"  x {p}" for p in problems]

    if high:
        lines += ["", "HIGH PRIORITY — needs you first:"]
        for t in high:
            dl = f"  (deadline: {t['deadline']})" if (t.get("deadline") or "").strip() else ""
            lines.append(f"  ! {t['client']}: {t['request']}{dl}")

    if with_deadline:
        lines += ["", "OPEN TASKS WITH A DEADLINE:"]
        for t in with_deadline:
            if t in high:
                continue
            lines.append(f"  - {t['client']}: {t['request']}  (deadline: {t['deadline']})")

    if new_tasks:
        lines += ["", f"NEW TASKS ({len(new_tasks)}):"]
        for t in new_tasks[:25]:
            lines.append(f"  + [{t.get('status', 'open')}] {t['client']}: {t['request']}")
        if len(new_tasks) > 25:
            lines.append(f"  ... and {len(new_tasks) - 25} more on the dashboard")

    if skipped:
        lines += ["", f"SET ASIDE, NO TASK CREATED ({len(skipped)}) — spot-check these:"]
        for s in skipped[:20]:
            lines.append(f"  · {s['sender']}: {s['reason']}")
        if len(skipped) > 20:
            lines.append(f"  ... and {len(skipped) - 20} more on the /skipped page")

    lines += ["", "Dashboard: /?token=...   Skipped audit: /skipped?token=..."]
    return "\n".join(lines)


def send() -> bool:
    """Build and email the digest (or log it when SMTP isn't configured)."""
    return notify.send_email("[Task Agent] Daily digest", build())


# ── Morning WhatsApp update ──────────────────────────────────────────────────

def _health_lines(since: str) -> list:
    """Turn raw health events into short human sentences, deduplicated."""
    events = db.events_since(since)
    counts: dict[str, int] = {}
    for e in events:
        if e.get("level") in ("error", "warn"):
            counts[e["message"]] = counts.get(e["message"], 0) + 1
    lines = []
    for msg, n in list(counts.items())[:6]:
        lines.append(f"{msg}" + (f" (x{n})" if n > 1 else ""))
    return lines


def build_wa() -> str:
    """Short morning update: NUMBERS ONLY (no task contents) plus the
    agent-health report. Full task details live on the dashboard."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    now_local = datetime.now(ZoneInfo(config.TIMEZONE))

    stats = db.pipeline_stats()
    open_tasks = db.open_tasks()
    high = [t for t in open_tasks if t.get("priority") == "high"]
    with_deadline = [t for t in open_tasks if (t.get("deadline") or "").strip()]
    new_tasks = db.tasks_created_since(since)
    in_progress = [t for t in open_tasks if t.get("status") == "in_progress"]
    queued = stats["emails"]["queued"] + stats["wa_messages"]["queued"]
    problems = _health_lines(since)
    if queued:
        problems.insert(0, f"{queued} messages still waiting for the AI")

    lines = [
        f"Good morning! Task Agent — {now_local.strftime('%a, %d %b')}",
        "",
        f"Open tasks: {len(open_tasks)}",
        f"High priority: {len(high)}",
        f"With deadline: {len(with_deadline)}",
        f"In progress: {len(in_progress)}",
        f"New in last 24h: {len(new_tasks)}",
    ]

    if problems:
        lines += ["", "PROBLEMS (last 24h):"]
        lines += [f"  x {p}" for p in problems]
    else:
        lines += ["", "No problems in the last 24h. All systems normal."]
    return "\n".join(lines)


def send_morning() -> bool:
    """The 9AM update: numbers-only summary + health report, delivered to
    BOTH channels — WhatsApp (WA_NOTIFY_TO) and email (NOTIFY_TO). WhatsApp
    tries free-form text first, then the approved template. Returns True if
    at least one channel succeeded."""
    from . import whatsapp

    text = build_wa()

    wa_ok, err = whatsapp.send_text(config.WA_NOTIFY_TO, text)
    if not wa_ok:
        db.log_event("warn", "whatsapp", f"free-form morning message failed: {err}")
        if config.WA_TEMPLATE_NAME:
            open_tasks = db.open_tasks()
            high = sum(1 for t in open_tasks if t.get("priority") == "high")
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            wa_ok, err = whatsapp.send_template(
                config.WA_NOTIFY_TO, config.WA_TEMPLATE_NAME,
                [len(open_tasks), high, len(db.tasks_created_since(since))],
                config.WA_TEMPLATE_LANG,
            )
            if not wa_ok:
                db.log_event("warn", "whatsapp",
                             f"template morning message failed: {err}")

    mail_ok = notify.send_email("[Task Agent] Morning update", text)
    return wa_ok or mail_ok


# kept as an alias so nothing else breaks
send_whatsapp = send_morning

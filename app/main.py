"""FastAPI app: Meta webhook + daily scheduler + task dashboard."""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (HTMLResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.templating import Jinja2Templates

from . import (auth, config, db, demo, digest, extractor, notify, sheets,
               whatsapp)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Internal dedup markers ([KRA:...], [TRK:...]) are plumbing, not something
# a person should read — strip them wherever a task is displayed.
_MARKER_RE = re.compile(r"\[(?:KRA|TRK):[^\]]*\]\s*")


def _clean_request(text) -> str:
    return _MARKER_RE.sub("", str(text or "")).strip()


templates.env.filters["clean"] = _clean_request


class _DemoAwareTemplates:
    """Wraps TemplateResponse so every page knows whether it is a demo view
    without threading a flag through a dozen endpoints."""

    def __init__(self, inner):
        self._inner = inner
        self.env = inner.env
        self.get_template = inner.get_template

    def TemplateResponse(self, request, name, context=None, **kw):
        ctx = dict(context or {})
        ctx.setdefault("demo", _is_demo(request))
        return self._inner.TemplateResponse(request, name, ctx, **kw)


_raw_templates = templates
templates = _DemoAwareTemplates(templates)
scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Seed the first admin account so someone can log in and add the rest.
    if config.INIT_ADMIN_EMAIL and config.INIT_ADMIN_PASSWORD:
        if not db.get_user(config.INIT_ADMIN_EMAIL):
            salt, ph = auth.hash_password(config.INIT_ADMIN_PASSWORD)
            db.create_user(config.INIT_ADMIN_EMAIL, salt, ph, "admin", "admin")
            log.info("seeded initial admin user %s", config.INIT_ADMIN_EMAIL)
    scheduler.add_job(
        extractor.run_digest,
        IntervalTrigger(minutes=config.SCAN_INTERVAL_MINUTES),
        id="scan",
        replace_existing=True,
        max_instances=1,       # never let two scans overlap
        coalesce=True,         # if the host slept, run once, not N times
    )
    # One morning update, numbers only, to BOTH WhatsApp and email.
    # (The detailed, task-by-task digest stays available at /digest.)
    scheduler.add_job(
        digest.send_morning,
        CronTrigger(hour=config.WA_DIGEST_HOUR, minute=0),
        id="morning_update",
        replace_existing=True,
    )
    from . import backup as backup_mod
    scheduler.add_job(
        backup_mod.run_weekly_backup,
        CronTrigger(day_of_week="mon", hour=7, minute=30),
        id="weekly_backup",
        replace_existing=True,
    )
    if sheets.configured():
        scheduler.add_job(
            sheets.sync_production,
            IntervalTrigger(minutes=config.SHEET_SYNC_MINUTES),
            id="sheet_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if sheets.tracking_configured():
        scheduler.add_job(
            sheets.sync_tracking,
            IntervalTrigger(minutes=config.SHEET_SYNC_MINUTES),
            id="tracking_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    log.info(
        "Scheduler started: scanning every %d minutes, digest daily at %02d:00 (%s)%s",
        config.SCAN_INTERVAL_MINUTES, config.DIGEST_HOUR, config.TIMEZONE,
        ", production sheet sync ON" if sheets.configured() else "",
    )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="WhatsApp + Email Task Agent", lifespan=lifespan)

# Task pages are text-heavy HTML; without compression a busy list was going
# out at ~500 KB, which is seconds of transfer on a phone or office link.
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/healthz")
async def healthz():
    """Cheap liveness check — no database work. Point an uptime pinger here
    to stop a free-tier host from sleeping (which also stops the scheduler)."""
    return {"ok": True}


@app.middleware("http")
async def _timing(request: Request, call_next):
    """Measure how long the SERVER took, so slow pages can be attributed to
    the server or to the network instead of guessed at. Read it in the
    browser's Network tab: response header `Server-Timing: app;dur=123`."""
    import time as _t
    t0 = _t.perf_counter()
    response = await call_next(request)
    ms = (_t.perf_counter() - t0) * 1000
    response.headers["Server-Timing"] = f"app;dur={ms:.0f}"
    response.headers["X-Process-Time-Ms"] = f"{ms:.0f}"
    if ms > 1000:
        log.warning("SLOW %s %s took %.0f ms", request.method, request.url.path, ms)
    return response


@app.get("/debug/tables")
async def debug_tables(request: Request, token: str = Query("")):
    """Row counts for every table in the database the app is CURRENTLY using.
    Compare with the old database (or a backup's MANIFEST.json) to confirm a
    migration brought everything across."""
    _demo_guard(request)
    _check_token(request, token)
    counts = await asyncio.to_thread(db.table_counts)
    return {"database": "postgres" if db.IS_PG else "sqlite",
            "counts": counts,
            "note": "compare these numbers with the source database"}


@app.get("/debug/timing")
async def debug_timing(request: Request, token: str = Query("")):
    """Where do the seconds actually go? Times each layer separately so we
    can tell a sleeping database from a slow query from slow rendering."""
    _demo_guard(request)
    _check_token(request, token)
    import time as _t

    def _ms(fn):
        t0 = _t.perf_counter()
        try:
            fn()
            err = None
        except Exception as exc:                       # report, don't raise
            err = str(exc)[:200]
        return round((_t.perf_counter() - t0) * 1000, 1), err

    def _connect_only():
        with db.get_db() as conn:
            conn.execute("SELECT 1")

    # cold-ish first touch, then a warm one: a big gap means the database
    # (or the pool) was asleep and the first query paid the wake-up cost
    first_ms, first_err = _ms(_connect_only)
    warm_ms, _ = _ms(_connect_only)
    bundle_ms, bundle_err = _ms(
        lambda: db.dashboard_data(datetime.utcnow().date().isoformat(), False))
    tasks_ms, _ = _ms(db.open_tasks)
    prod_ms, _ = _ms(db.production_all)

    bundle = await asyncio.to_thread(
        db.dashboard_data, datetime.utcnow().date().isoformat(), False)
    by_client: dict[str, list] = {}
    for t in bundle["open"]:
        by_client.setdefault(t["client"] or "Unknown", []).append(t)
    tmpl = templates.get_template("dashboard.html")
    ctx = {"request": request, "token": token, "dept": "admin",
           "departments": config.DEPARTMENTS, "user_email": "", "q": "", "po": "",
           "merged": "", "view": "client", "n_client": len(bundle["open"]),
           "n_internal": 0, "review": [], "n_review": 0, "n_review_high": 0,
           "closed": "", "date_str": "", "by_client": by_client,
           "open_count": len(bundle["open"]), "progress_count": 0,
           "done_today": bundle["done_today"], "archive": [],
           "n_archive": bundle["n_archive"], "show_archive": False,
           "last_run": bundle["last_run"]}
    t0 = _t.perf_counter()
    html = tmpl.render(**ctx)
    render_ms = round((_t.perf_counter() - t0) * 1000, 1)

    return {
        "database": {
            "backend": "postgres" if db.IS_PG else "sqlite",
            "pool_active": db._pool is not None,
            "pool_fell_back": db._pool_broken,
            "first_query_ms": first_ms,
            "warm_query_ms": warm_ms,
            "wake_up_cost_ms": round(max(0.0, first_ms - warm_ms), 1),
            "error": first_err or bundle_err,
        },
        "queries": {
            "dashboard_bundle_ms": bundle_ms,
            "open_tasks_ms": tasks_ms,
            "production_all_ms": prod_ms,
        },
        "render": {
            "template_ms": render_ms,
            "html_kb": round(len(html.encode()) / 1024, 1),
            "open_tasks": len(bundle["open"]),
        },
        "how_to_read": (
            "wake_up_cost_ms high (>500) = the database was asleep (Neon free "
            "tier suspends after ~5 min idle). warm_query_ms high (>150) = the "
            "app and database are far apart, or the instance is CPU-starved. "
            "template_ms high (>300) = too much HTML. If all of these are small "
            "but the browser still shows seconds, the time is network/host: "
            "compare with the Server-Timing header on the real page."
        ),
    }


# ── Meta webhook ─────────────────────────────────────────────────────────────

@app.get("/webhook")
async def webhook_verify(request: Request):
    """Meta's one-time verification handshake."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == config.WHATSAPP_VERIFY_TOKEN
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def webhook_receive(request: Request):
    raw = await request.body()
    if not whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="bad signature")
    payload = await request.json()
    # Respond to Meta fast; store in the background.
    asyncio.create_task(whatsapp.handle_webhook_payload(payload))
    return {"status": "ok"}


# ── Dashboard ────────────────────────────────────────────────────────────────

def _session_user(request: Request) -> dict | None:
    """The logged-in user (via session cookie), or None."""
    email = auth.read_session(request.cookies.get(auth.SESSION_COOKIE))
    if not email:
        return None
    user = db.get_user(email)
    if user and user.get("active"):
        return user
    return None


def _dept_for(request: Request, token: str = "") -> str:
    """Resolve access to a department. Email login (RBAC) is the ONLY way
    in. Exception: while no user accounts exist yet (fresh setup), the
    legacy ?token= links work so the first admin can be bootstrapped —
    the moment the first account is created, token links stop working."""
    user = _session_user(request)
    if user:
        return "admin" if user.get("role") == "admin" else (user.get("department") or "")
    if db.count_users() == 0:
        if config.DASHBOARD_TOKEN and token == config.DASHBOARD_TOKEN:
            return "admin"
        dept = config.DEPT_TOKENS.get(token)
        if dept in config.DEPARTMENTS:
            return dept
    raise HTTPException(status_code=401, detail="not logged in")


def _check_token(request_or_token, token: str | None = None) -> None:
    """Admin-only gate: mails, whatsapp, skipped, stats, digest, scans."""
    if isinstance(request_or_token, Request):
        if _dept_for(request_or_token, token or "") != "admin":
            raise HTTPException(status_code=403, detail="admin access only")
    else:  # plain token (no request context)
        if (config.DASHBOARD_TOKEN and request_or_token == config.DASHBOARD_TOKEN) or \
           config.DEPT_TOKENS.get(request_or_token) == "admin":
            return
        raise HTTPException(status_code=401, detail="invalid or missing ?token=")


def _matches(t: dict, q: str) -> bool:
    hay = f"{t.get('client','')} {t.get('contact','')} {t.get('request','')}".lower()
    return q in hay


def _is_demo(request: Request) -> bool:
    """Is this a demo login? Demo accounts never see the real database."""
    user = _session_user(request)
    return bool(user) and (user.get("email") or "").lower() in config.DEMO_EMAILS


def _demo_guard(request: Request) -> None:
    """Actions that would touch the real world (scans, backups, user
    management, sheet syncs, real reports) are refused for demo accounts."""
    if _is_demo(request):
        raise HTTPException(
            status_code=403,
            detail="This is a demo account — live actions are disabled. "
                   "Everything you see is generated sample data.")


def _src(request: Request):
    """The data source for this request: the generated demo dataset for demo
    accounts, the real database for everyone else. Both expose the same
    function names, so every page below is written once."""
    return demo if _is_demo(request) else db


def _is_internal(t: dict) -> bool:
    """Internal work comes from our own sheets (Team KRA assignments,
    production risk, stage tracking). Client work comes from mail/WhatsApp."""
    return (t.get("channel") or "") == "sheet"


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, token: str = Query(""), q: str = Query(""), po: str = Query(""), merged: str = Query(""), view: str = Query("client"), closed: str = Query(""), archive: int = Query(0)):
    try:
        dept = _dept_for(request, token)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=302)
    user = _session_user(request)
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz)
    query = q.strip().lower()
    po_filter = po.strip().upper()
    # one connection, and the finished-task archive only when asked for
    src = _src(request)
    bundle = await asyncio.to_thread(
        src.dashboard_data, datetime.utcnow().date().isoformat(), bool(archive))
    tasks = bundle["open"]
    done_today = bundle["done_today"]
    archive_rows = bundle["archive"]
    n_archive = bundle["n_archive"]
    if dept not in ("admin", "management"):
        # department credential: only this department's tasks.
        # (management is oversight — sees every department's tasks,
        # but has no admin pages/controls)
        tasks = [t for t in tasks if (t.get("department") or "") == dept]
        done_today = [t for t in done_today if (t.get("department") or "") == dept]
        archive_rows = [t for t in archive_rows if (t.get("department") or "") == dept]
    if po_filter:
        # Filter by PO number (from production page deep-link)
        tasks = [t for t in tasks if (t.get("po_number") or "").upper() == po_filter]
        done_today = [t for t in done_today if (t.get("po_number") or "").upper() == po_filter]
        archive_rows = [t for t in archive_rows if (t.get("po_number") or "").upper() == po_filter]
    if query:
        tasks = [t for t in tasks if _matches(t, query)]
        done_today = [t for t in done_today if _matches(t, query)]
        archive_rows = [t for t in archive_rows if _matches(t, query)]
    # Everyone works in two sections: client tasks (mail/WhatsApp) and
    # internal tasks (raised by the factory-sheet syncs / assigned from the
    # Team page). A PO deep-link ignores the split so nothing hides.
    n_client = sum(1 for t in tasks if not _is_internal(t))
    n_internal = sum(1 for t in tasks if _is_internal(t))
    review = [t for t in tasks if (t.get("close_at") or "")]
    review.sort(key=lambda t: (0 if t.get("close_conf") == "high" else 1,
                               t.get("close_at") or ""), reverse=False)
    n_review = len(review)
    n_review_high = sum(1 for t in review if t.get("close_conf") == "high")
    if view not in ("client", "internal", "review"):
        view = "client"
    if not po_filter and view != "review":
        want_internal = view == "internal"
        tasks = [t for t in tasks if _is_internal(t) == want_internal]
        done_today = [t for t in done_today if _is_internal(t) == want_internal]
        archive_rows = [t for t in archive_rows if _is_internal(t) == want_internal]
    if view == "review":
        tasks = []          # the review tab renders its own list
    by_client: dict[str, list] = {}
    for t in tasks:
        by_client.setdefault(t["client"] or "Unknown", []).append(t)

    def _group_order(item):
        _, ts = item
        has_high = any(x.get("priority") == "high" for x in ts)
        has_deadline = any((x.get("deadline") or "").strip() for x in ts)
        # urgent clients first, then busiest
        return (0 if has_high else (1 if has_deadline else 2), -len(ts))

    by_client = dict(sorted(by_client.items(), key=_group_order))
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "token": token,
            "dept": dept,
            "departments": config.DEPARTMENTS,
            "user_email": (user or {}).get("email", ""),
            "q": q.strip(),
            "po": po_filter,
            "merged": merged,
            "view": view,
            "n_client": n_client,
            "n_internal": n_internal,
            "review": review,
            "n_review": n_review,
            "n_review_high": n_review_high,
            "closed": closed,
            "date_str": today.strftime("%A, %d %B %Y"),
            "by_client": by_client,
            "open_count": sum(1 for t in tasks if t["status"] == "open"),
            "progress_count": sum(1 for t in tasks if t["status"] == "in_progress"),
            "done_today": done_today,
            "archive": archive_rows,
            "n_archive": n_archive,
            "show_archive": bool(archive),
            "last_run": bundle["last_run"],
        },
    )


@app.get("/mails", response_class=HTMLResponse)
async def mails(request: Request, token: str = Query("")):
    """Email history, one section per inbox."""
    _check_token(request, token)
    emails = _src(request).all_emails()
    by_inbox: dict[str, list] = {}
    for e in emails:
        by_inbox.setdefault(e.get("account") or "unknown inbox", []).append(e)
    return templates.TemplateResponse(
        request, "mails.html", {"token": token, "emails_by_inbox": by_inbox},
    )


@app.get("/whatsapp", response_class=HTMLResponse)
async def whatsapp_page(request: Request, token: str = Query("")):
    """WhatsApp message history."""
    _check_token(request, token)
    return templates.TemplateResponse(
        request, "whatsapp.html",
        {"token": token, "wa_messages": _src(request).all_wa_messages()},
    )


@app.get("/history")
async def history_redirect(token: str = Query("")):
    return RedirectResponse(url=f"/mails?token={token}", status_code=302)


@app.get("/backfill", response_class=HTMLResponse)
async def backfill(request: Request, token: str = Query(""), days: int = Query(30, ge=1, le=90)):
    """One-time seeding from past mail. Runs in the background."""
    _demo_guard(request)
    _check_token(request, token)
    asyncio.get_running_loop().run_in_executor(None, extractor.run_backfill, days)
    return HTMLResponse(
        f"<body style='font-family:sans-serif;padding:40px'>"
        f"<h3>Backfill started — reading the last {days} days of mail.</h3>"
        f"<p>This runs in the background and can take a few minutes "
        f"(batched AI processing). Refresh the <a href='/?token={token}'>Tasks page</a> "
        f"to watch tasks appear.</p></body>"
    )


@app.post("/tasks/{task_id}/assign")
async def task_assign(request: Request, task_id: int, token: str = Query("")):
    """Assign a task to a department (admin only)."""
    if _dept_for(request, token) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    form = await request.form()
    department = str(form.get("department", "")).lower().strip()
    if department in config.DEPARTMENTS:
        _src(request).set_task_department(task_id, department)
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/tasks/create")
async def task_create(request: Request, token: str = Query("")):
    """Manually add a task that didn't come from mail/WhatsApp (admin only)."""
    if _dept_for(request, token) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    form = await request.form()
    text = str(form.get("request", "")).strip()
    if text:
        department = str(form.get("department", "admin")).lower().strip()
        priority = str(form.get("priority", "normal")).lower()
        user = _session_user(request)
        _src(request).add_task({
            "client": str(form.get("client", "")).strip() or "Internal",
            "contact": "",
            "channel": "manual",
            "request": text[:500],
            "department": department if department in config.DEPARTMENTS else "admin",
            "deadline": str(form.get("deadline", "")).strip()[:100],
            "priority": priority if priority in ("high", "normal", "low") else "normal",
            "source": f"added manually by {(user or {}).get('email', 'admin')}",
        })
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/tasks/{task_id}/schedule")
async def task_schedule(request: Request, task_id: int, token: str = Query("")):
    """Schedule a task for a specific date (any logged-in user)."""
    _dept_for(request, token)
    form = await request.form()
    day = str(form.get("date", "")).strip()
    if day:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    _src(request).set_task_schedule(task_id, day)
    back = str(form.get("back", "")) or f"/?token={token}"
    return RedirectResponse(url=back, status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request, token: str = Query(""), date: str = Query("")):
    """Day view: tasks scheduled for, created on, and completed on a date.
    Admin only — department portals show just their own task list."""
    try:
        dept = _dept_for(request, token)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=302)
    if dept != "admin":
        raise HTTPException(status_code=403, detail="admin access only")
    from datetime import timedelta
    from . import report
    tz = ZoneInfo(config.TIMEZONE)
    try:
        day = (datetime.strptime(date, "%Y-%m-%d").date()
               if date else datetime.now(tz).date())
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    all_tasks = _src(request).all_tasks(limit=2000)
    if dept not in ("admin", "management"):
        all_tasks = [t for t in all_tasks if (t.get("department") or "") == dept]

    iso = day.isoformat()
    scheduled = [t for t in all_tasks if (t.get("scheduled_for") or "") == iso]
    created = [t for t in all_tasks
               if report._to_local_date(t.get("created_at") or "") == day]
    completed = [t for t in all_tasks if t.get("status") == "done"
                 and report._to_local_date(t.get("updated_at") or "") == day]
    return templates.TemplateResponse(
        request, "calendar.html",
        {"token": token, "dept": dept, "day": iso,
         "day_label": day.strftime("%A, %d %B %Y"),
         "prev_day": (day - timedelta(days=1)).isoformat(),
         "next_day": (day + timedelta(days=1)).isoformat(),
         "scheduled": scheduled, "created": created, "completed": completed},
    )


@app.post("/tasks/{task_id}/done")
async def task_done(request: Request, task_id: int, token: str = Query("")):
    _dept_for(request, token)  # any valid credential may close its tasks
    form = await request.form()
    user = _session_user(request)
    _src(request).set_task_status(
        task_id, "done",
        remark=str(form.get("remark", "")),
        done_by=(user or {}).get("email", ""),
    )
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/tasks/{task_id}/reopen")
async def task_reopen(request: Request, task_id: int, token: str = Query("")):
    _dept_for(request, token)
    _src(request).set_task_status(task_id, "open")
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/run-now")
async def run_now(request: Request, token: str = Query("")):
    """Manually trigger the digest (useful for testing and mid-day refreshes).
    Fire-and-forget: the scan runs in the background (it can take minutes now
    that batches are paced for the rate limit); if one is already running the
    trigger is simply ignored."""
    _demo_guard(request)
    _check_token(request, token)
    asyncio.get_running_loop().run_in_executor(None, extractor.run_digest)
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.get("/skipped", response_class=HTMLResponse)
async def skipped_page(request: Request, token: str = Query(""), days: int = Query(7, ge=1, le=90)):  # noqa: E501
    """Audit page: every mail the AI decided NOT to turn into a task, with
    its one-line reason. If you disagree with a reason, that mail's request
    can be added by hand on the dashboard — and tell the AI's owner to tune
    the prompt."""
    _check_token(request, token)
    from datetime import datetime, timedelta, timezone as tz
    since = (datetime.now(tz.utc) - timedelta(days=days)).isoformat()
    return templates.TemplateResponse(
        request, "skipped.html",
        {"token": token, "days": days, "rows": _src(request).skipped_since(since, limit=500)},
    )


@app.get("/digest", response_class=PlainTextResponse)
async def digest_preview(request: Request, token: str = Query(""), send: int = Query(0)):
    """Preview today's digest as plain text; add &send=1 to email it now."""
    _check_token(request, token)
    _demo_guard(request)
    text = digest.build()
    if send:
        ok = notify.send_email("[Task Agent] Daily digest (manual)", text)
        text = (
            ("EMAILED OK\n\n" if ok else
             "NOT EMAILED — SMTP not configured or failed (see logs). "
             "Set SMTP_USER / SMTP_PASS / NOTIFY_TO.\n\n")
            + text
        )
    return text


@app.get("/wa-digest", response_class=PlainTextResponse)
async def wa_digest_preview(request: Request, token: str = Query(""), send: int = Query(0)):
    """Preview the numbers-only morning update; add &send=1 to send it now
    to both WhatsApp and email."""
    _check_token(request, token)
    _demo_guard(request)
    text = digest.build_wa()
    if send:
        ok = await asyncio.to_thread(digest.send_morning)
        text = (("SENT (at least one channel OK)\n\n" if ok else
                 "SEND FAILED on both WhatsApp and email — check that "
                 "WHATSAPP_PHONE_NUMBER_ID and SMTP_USER/SMTP_PASS are set; "
                 "details in logs.\n\n") + text)
    return text


@app.get("/reprocess", response_class=HTMLResponse)
async def reprocess(request: Request, token: str = Query("")):
    """Re-run task extraction over EVERY stored mail/WA message (after a
    prompt improvement). Open tasks are passed to the AI so it won't
    duplicate them. Runs in the background."""
    _demo_guard(request)
    _check_token(request, token)
    n = db.reset_processed()
    asyncio.get_running_loop().run_in_executor(None, extractor.run_digest)
    return HTMLResponse(
        f"<body style='font-family:sans-serif;padding:40px'>"
        f"<h3>Re-scanning {n} stored messages with the improved extractor.</h3>"
        f"<p>This runs in the background in batches and can take a few minutes. "
        f"Refresh the <a href='/?token={token}'>Tasks page</a> to watch tasks appear.</p></body>"
    )


@app.get("/stats")
async def stats(request: Request, token: str = Query("")):
    """Pipeline visibility: how many mails/WA msgs are captured, processed,
    still queued for the AI, plus task counts and recent runs."""
    _demo_guard(request)
    _check_token(request, token)
    return db.pipeline_stats()


# ── Orders (purchase orders) ─────────────────────────────────────────────────

def _orders_access(request: Request, token: str) -> str:
    """Orders pages: admin, management (oversight) and implementation
    (their whole job is tracking POs)."""
    dept = _dept_for(request, token)
    if dept not in ("admin", "management", "implementation"):
        raise HTTPException(status_code=403, detail="no access to orders")
    return dept


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, token: str = Query("")):
    dept = _orders_access(request, token)
    return templates.TemplateResponse(
        request, "orders.html",
        {"token": token, "dept": dept, "pos": _src(request).list_pos(),
         "statuses": config.PO_STATUSES},
    )


@app.get("/orders/{po_number}", response_class=HTMLResponse)
async def order_detail(request: Request, po_number: str, token: str = Query("")):
    dept = _orders_access(request, token)
    po = _src(request).get_po(po_number)
    if not po:
        raise HTTPException(status_code=404, detail="unknown PO")
    tracking = _src(request).tracking_for_po(po_number)
    stages = []
    if tracking:
        import json as _json
        try:
            stages = _json.loads(tracking.get("stages_json") or "[]")
        except ValueError:
            stages = []
    return templates.TemplateResponse(
        request, "order_detail.html",
        {"token": token, "dept": dept, "po": po,
         "statuses": config.PO_STATUSES,
         "tasks": _src(request).tasks_for_po(po_number),
         "production": _src(request).production_for_po(po_number),
         "tracking": tracking, "stages": stages,
         "mails": _src(request).emails_mentioning(po["po_number"], limit=50)},
    )


@app.post("/orders/{po_number}/update")
async def order_update(request: Request, po_number: str, token: str = Query("")):
    dept = _orders_access(request, token)
    if dept == "management":
        raise HTTPException(status_code=403, detail="management is view-only")
    form = await request.form()
    status = str(form.get("status", "")).lower().strip()
    notes = form.get("notes")
    _src(request).update_po(
        po_number,
        status=status if status in config.PO_STATUSES else None,
        notes=str(notes)[:1000] if notes is not None else None,
    )
    return RedirectResponse(url=f"/orders/{po_number}?token={token}", status_code=303)


def _production_access(request: Request, token: str) -> str:
    """Production, Team and the hosted CXO dashboard are admin-only —
    department portals show just their own task list."""
    dept = _dept_for(request, token)
    if dept != "admin":
        raise HTTPException(status_code=403, detail="admin access only")
    return dept


@app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, token: str = Query("")):
    """Charts: production completed vs pending, tasks by department,
    14-day task flow, PO pipeline. Admin + management."""
    try:
        dept = _dept_for(request, token)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=302)
    if dept not in ("admin", "management"):
        raise HTTPException(status_code=403, detail="admin or management only")

    from datetime import date as _date, timedelta as _td
    from . import report
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz).date()
    horizon = today + _td(days=config.SHEET_RISK_DAYS)

    # production states
    prod = {"overdue": 0, "risk": 0, "running": 0, "complete": 0}
    for r in _src(request).production_all():
        if r["pending_qty"] <= 0:
            prod["complete"] += 1
            continue
        try:
            ready = _date.fromisoformat(r.get("ship_ready") or "")
        except ValueError:
            prod["running"] += 1
            continue
        if ready < today:
            prod["overdue"] += 1
        elif ready <= horizon:
            prod["risk"] += 1
        else:
            prod["running"] += 1
    prod_total = sum(prod.values()) or 1

    tasks = _src(request).all_tasks(limit=2000)
    open_by_dept: dict[str, int] = {}
    for t in tasks:
        if t.get("status") in ("open", "in_progress"):
            d = t.get("department") or "unassigned"
            open_by_dept[d] = open_by_dept.get(d, 0) + 1
    dept_rows = sorted(open_by_dept.items(), key=lambda kv: -kv[1])
    dept_max = max([n for _, n in dept_rows] or [1])

    # 14-day created vs completed
    days = [today - _td(days=i) for i in range(13, -1, -1)]
    created = {d: 0 for d in days}
    completed = {d: 0 for d in days}
    for t in tasks:
        c = report._to_local_date(t.get("created_at") or "")
        if c in created:
            created[c] += 1
        if t.get("status") == "done":
            u = report._to_local_date(t.get("updated_at") or "")
            if u in completed:
                completed[u] += 1
    # precompute SVG geometry
    W, H, PL, PB, PT = 640, 190, 34, 24, 10
    ymax = max(list(created.values()) + list(completed.values()) + [1])
    step = (W - PL - 8) / (len(days) - 1)
    def _pts(series):
        out = []
        for i, d in enumerate(days):
            x = PL + i * step
            y = PT + (H - PT - PB) * (1 - series[d] / ymax)
            out.append({"x": round(x, 1), "y": round(y, 1),
                        "v": series[d], "label": d.strftime("%d %b")})
        return out
    line_created = _pts(created)
    line_completed = _pts(completed)

    po_counts = {s: 0 for s in config.PO_STATUSES}
    for p in _src(request).list_pos():
        if p["status"] in po_counts:
            po_counts[p["status"]] += 1
    po_total = sum(po_counts.values()) or 1

    # Who is holding the most overdue factory stages (Team KRA sheet).
    # Demo sessions read invented owners from the demo dataset instead.
    _kra_src = _src(request)
    _kra_rows = (_kra_src.team_pastdue() if hasattr(_kra_src, "team_pastdue")
                 else sheets.team_pastdue())
    kra_by_owner: dict[str, dict] = {}
    for r in _kra_rows:
        o = kra_by_owner.setdefault(
            r["owner"], {"owner": r["owner"], "n": 0, "worst": 0, "pos": set()})
        o["n"] += 1
        o["worst"] = max(o["worst"], r["days_late"])
        o["pos"].add(r["po_number"])
    kra_rows = sorted(kra_by_owner.values(), key=lambda o: (-o["n"], -o["worst"]))
    for o in kra_rows:
        o["pos"] = len(o["pos"])
    kra_total = sum(o["n"] for o in kra_rows)
    kra_shown = kra_rows[:12]
    kra_hidden = len(kra_rows) - len(kra_shown)   # never truncate silently
    kra_max = max([o["n"] for o in kra_shown] or [1])

    return templates.TemplateResponse(
        request, "insights.html",
        {"token": token, "dept": dept, "prod": prod, "prod_total": prod_total,
         "dept_rows": dept_rows, "dept_max": dept_max,
         "kra_rows": kra_shown, "kra_max": kra_max, "kra_total": kra_total,
         "kra_hidden": kra_hidden, "kra_people": len(kra_rows),
         "line_created": line_created, "line_completed": line_completed,
         "poly_created": " ".join(f"{p['x']},{p['y']}" for p in line_created),
         "poly_completed": " ".join(f"{p['x']},{p['y']}" for p in line_completed),
         "ymax": ymax, "svg_w": W, "svg_h": H,
         "po_counts": po_counts, "po_total": po_total,
         "days_label": f"{days[0].strftime('%d %b')} – {days[-1].strftime('%d %b')}"},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def cxo_dashboard(request: Request, token: str = Query(""), po: str = Query("")):
    """The CXO Production dashboard, hosted inside the agent behind login.
    Accepts ?po=... to open pre-filtered to one PO (deep link from the
    Production page). Never served to demo accounts — it renders live
    factory data straight from the company's Google Sheet."""
    _demo_guard(request)
    try:
        _production_access(request, token)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse(url="/login", status_code=302)
        raise
    path = Path(__file__).parent / "static" / "cxo_production.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/production", response_class=HTMLResponse)
async def production_page(request: Request, token: str = Query("")):
    """The factory sheet, inside the agent: every line with progress and
    at-risk highlighting. Separate from client tasks by design."""
    try:
        dept = _production_access(request, token)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse(url="/login", status_code=302)
        raise
    from datetime import date as _date, timedelta as _td
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    horizon = today + _td(days=config.SHEET_RISK_DAYS)

    # The factory's own Status column is the truth; quantity columns are
    # often stale. Classify by status FIRST, then by dates. Where numbers
    # contradict the status, flag it instead of raising a false alarm.
    buckets = {"late": [], "soon": [], "running": [], "hold": [], "complete": []}
    for r in _src(request).production_all():
        st = (r.get("sheet_status") or "").strip().lower()
        pct = 0
        if r["po_qty"] > 0:
            pct = min(100, int(round(100 * r["done_qty"] / r["po_qty"])))
        r["pct"] = pct
        r["flag"] = ""
        r["when"] = ""
        if r["pending_qty"] > r["po_qty"] > 0:
            r["flag"] = "numbers don't add up — check sheet"
        if any(w in st for w in ("complete", "done", "shipped", "dispatch")):
            if r["pending_qty"] > 0 and not r["flag"]:
                r["flag"] = (f"sheet says {st or 'complete'} but "
                             f"{r['pending_qty']:,} pcs still show pending")
            r["pct"] = 100 if not r["flag"] else pct
            buckets["complete"].append(r)
            continue
        if "hold" in st or "cancel" in st:
            r["when"] = "on hold" if "hold" in st else "cancelled"
            buckets["hold"].append(r)
            continue
        if r["pending_qty"] <= 0:
            buckets["complete"].append(r)
            continue
        try:
            ready = _date.fromisoformat(r.get("ship_ready") or "")
        except ValueError:
            buckets["running"].append(r)
            continue
        days = (ready - today).days
        if days < 0:
            r["when"] = f"late by {-days} day{'s' if days != -1 else ''}"
            r["days_late"] = -days
            buckets["late"].append(r)
        elif days <= config.SHEET_RISK_DAYS:
            r["when"] = ("ships today" if days == 0
                         else f"ships in {days} day{'s' if days != 1 else ''}")
            buckets["soon"].append(r)
        else:
            r["when"] = f"ships {ready.strftime('%d %b')}"
            buckets["running"].append(r)

    buckets["late"].sort(key=lambda r: -r.get("days_late", 0))
    buckets["soon"].sort(key=lambda r: r.get("ship_ready") or "")
    buckets["running"].sort(key=lambda r: r.get("ship_ready") or "9999")
    return templates.TemplateResponse(
        request, "production.html",
        {"token": token, "dept": dept, "b": buckets,
         "configured": _is_demo(request) or sheets.configured(),
         "last_sync": _src(request).production_last_sync(),
         "total": sum(len(v) for v in buckets.values())},
    )


@app.post("/tasks/{task_id}/close-confirm")
async def close_confirm(request: Request, task_id: int, token: str = Query("")):
    """Human confirms the agent's 'looks done' reading — one click, and the
    agent's evidence is kept as the remark so the record explains itself."""
    dept = _dept_for(request, token)
    form = await request.form()
    user = _session_user(request)
    task = next((t for t in _src(request).open_tasks() if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if dept not in ("admin", "management") and (task.get("department") or "") != dept:
        raise HTTPException(status_code=403, detail="not your department's task")
    typed = str(form.get("remark", "")).strip()
    remark = typed or f"confirmed done — {task.get('close_why') or 'agent flagged complete'}"
    _src(request).set_task_status(task_id, "done", remark=remark[:500],
                       done_by=(user or {}).get("email", "confirmed by human"))
    return RedirectResponse(url=f"/?token={token}&view=review", status_code=303)


@app.post("/tasks/{task_id}/close-dismiss")
async def close_dismiss(request: Request, task_id: int, token: str = Query("")):
    """'Not yet' — keep the task open, drop the suggestion."""
    dept = _dept_for(request, token)
    task = next((t for t in _src(request).open_tasks() if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if dept not in ("admin", "management") and (task.get("department") or "") != dept:
        raise HTTPException(status_code=403, detail="not your department's task")
    _src(request).clear_close_suggestion(task_id)
    return RedirectResponse(url=f"/?token={token}&view=review", status_code=303)


@app.post("/tasks/close-all-high")
async def close_all_high(request: Request, token: str = Query("")):
    """Confirm every HIGH-confidence suggestion at once (admin only).
    Medium-confidence ones always stay one-by-one — that is the point."""
    if _dept_for(request, token) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    user = _session_user(request)
    n = 0
    for t in _src(request).ready_to_close():
        if (t.get("close_conf") or "") != "high":
            continue
        _src(request).set_task_status(
            t["id"], "done",
            remark=f"confirmed done (bulk) — {t.get('close_why') or 'agent flagged complete'}"[:500],
            done_by=(user or {}).get("email", "confirmed by human"))
        n += 1
    return RedirectResponse(url=f"/?token={token}&view=review&closed={n}", status_code=303)


@app.post("/dedupe")
async def dedupe_tasks(request: Request, token: str = Query("")):
    """Merge exact duplicate open tasks (admin only). Copies are closed
    with a remark pointing at the kept task — reversible via Reopen."""
    if _dept_for(request, token) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    merged = await asyncio.to_thread(_src(request).dedupe_open_tasks)
    return RedirectResponse(url=f"/?token={token}&merged={merged}", status_code=303)


def _team_data(owner: str = "", src=None):
    """Past-due stage tasks + per-owner summary, optionally filtered."""
    rows = (src.team_pastdue() if src is not None and hasattr(src, "team_pastdue")
            else sheets.team_pastdue())
    by_owner: dict[str, list] = {}
    for r in rows:
        by_owner.setdefault(r["owner"], []).append(r)
    summary = []
    for o, rs in by_owner.items():
        summary.append({
            "owner": o,
            "n": len(rs),
            "pos": len({x["po_number"] for x in rs}),
            "worst": max(x["days_late"] for x in rs),
            "avg": round(sum(x["days_late"] for x in rs) / len(rs)),
            "oldest": max(rs, key=lambda x: x["days_late"]),
        })
    summary.sort(key=lambda s: (-s["n"], -s["worst"]))
    sel = owner.strip()
    shown = by_owner.get(sel, []) if sel else rows
    return rows, summary, sel, shown


def _kra_marker(po: str, stage: str) -> str:
    """Stable id for an assigned KRA stage task, used to prevent duplicates
    and to show 'assigned' state on the Team page."""
    slug = "".join(c for c in (stage or "").lower() if c.isalnum())[:40]
    return f"[KRA:{po}:{slug}]"


@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request, token: str = Query(""), owner: str = Query(""), assigned: str = Query("")):
    """Team KRA view: who is sitting on which past-due stage, by how many
    days. ?owner=Name gives each person a bookmarkable personal view."""
    try:
        dept = _production_access(request, token)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse(url="/login", status_code=302)
        raise
    src = _src(request)
    rows, summary, sel, shown = _team_data(owner, src)
    # which rows are already assigned as real tasks?
    open_reqs = " ".join((t.get("request") or "") for t in src.open_tasks())
    for r in rows:
        r["assigned"] = _kra_marker(r["po_number"], r["stage"]) in open_reqs
    buckets = {"b13": 0, "b47": 0, "b814": 0, "b15": 0}
    for r in rows:
        d = r["days_late"]
        buckets["b13" if d <= 3 else "b47" if d <= 7 else "b814" if d <= 14 else "b15"] += 1
    return templates.TemplateResponse(
        request, "team.html",
        {"token": token, "dept": dept, "rows": rows, "summary": summary,
         "sel": sel, "shown": shown, "buckets": buckets,
         "departments": config.DEPARTMENTS,
         "assigned_msg": assigned,
         "pos_hit": len({r["po_number"] for r in rows}),
         "worst": max((r["days_late"] for r in rows), default=0),
         "configured": _is_demo(request) or sheets.tracking_configured(),
         "last_sync": src.production_last_sync()},
    )


@app.post("/team/assign")
async def team_assign(request: Request, token: str = Query("")):
    """Admin turns a past-due KRA stage into a real task on a department's
    portal. Deduped by marker — assigning twice does nothing."""
    if _dept_for(request, token) != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    form = await request.form()
    po = str(form.get("po", "")).strip().upper()
    stage = str(form.get("stage", "")).strip()[:80]
    owner_name = str(form.get("owner", "")).strip()[:60]
    customer = str(form.get("customer", "")).strip()[:80]
    due = str(form.get("due", "")).strip()[:10]
    late = str(form.get("late", "")).strip()[:6]
    department = str(form.get("department", "production")).lower().strip()
    if department not in config.DEPARTMENTS:
        department = "production"
    back_owner = str(form.get("back_owner", "")).strip()
    if not (po and stage):
        raise HTTPException(status_code=400, detail="missing po/stage")
    src = _src(request)
    marker = _kra_marker(po, stage)
    already = any(marker in (t.get("request") or "") for t in src.open_tasks())
    if not already:
        user = _session_user(request)
        src.add_task({
            "client": customer or "Production",
            "channel": "sheet",
            "request": (f"{marker} {stage} for {po}"
                        + (f" — {owner_name}" if owner_name else "")
                        + (f", due {due}" if due else "")
                        + (f", {late} days late" if late else "")),
            "department": department,
            "po_number": po,
            "deadline": due,
            "priority": "high",
            "source": f"assigned from Team page by {(user or {}).get('email', 'admin')}",
        })
    from urllib.parse import quote
    back = f"/team?token={token}&assigned={'dup' if already else '1'}"
    if back_owner:
        back += f"&owner={quote(back_owner)}"
    return RedirectResponse(url=back, status_code=303)


@app.get("/team.pdf")
async def team_pdf(request: Request, token: str = Query(""), owner: str = Query("")):
    """Past-due report as PDF, optionally for one person."""
    try:
        _production_access(request, token)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse(url="/login", status_code=302)
        raise
    from . import report
    rows, summary, sel, shown = _team_data(owner, _src(request))
    data = await asyncio.to_thread(report.build_team_pdf, shown, sel)
    name = f"SOL_PastDue_{datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()}"
    if sel:
        name += "_" + "".join(c for c in sel if c.isalnum())
    return Response(
        content=data, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
    )


@app.get("/sync-sheets")
async def sync_sheets_now(request: Request, token: str = Query(""), back: int = Query(0)):
    """Pull the production sheet right now (admin only)."""
    _demo_guard(request)
    _check_token(request, token)
    if not sheets.configured():
        return PlainTextResponse(
            "Production sheet sync is not configured yet.\n"
            "Set SHEETS_API_KEY and PROD_SHEET_ID in Render -> Environment "
            "(the same values from the dashboard's connect panel).")
    if back:
        # fire-and-forget: the button returns instantly, sync runs behind
        asyncio.get_running_loop().run_in_executor(None, sheets.sync_production)
        if sheets.tracking_configured():
            asyncio.get_running_loop().run_in_executor(None, sheets.sync_tracking)
        return RedirectResponse(url=f"/production?token={token}", status_code=303)
    result = await asyncio.to_thread(sheets.sync_production)
    tracking = (await asyncio.to_thread(sheets.sync_tracking)
                if sheets.tracking_configured() else {"skipped": "not configured"})
    return PlainTextResponse(f"Sync result: {result}\nTracking sheet: {tracking}")


@app.get("/backup")
async def backup_download(request: Request, token: str = Query("")):
    """Download a full database backup zip (admin only)."""
    _demo_guard(request)
    _check_token(request, token)
    from . import backup as backup_mod
    filename, data = await asyncio.to_thread(backup_mod.build_backup)
    return Response(
        content=data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/report.pdf")
async def report_pdf(request: Request, token: str = Query(""), date: str = Query("")):
    """Downloadable daily report: every department's tasks for the chosen
    date (admin only). ?date=YYYY-MM-DD, defaults to today."""
    _demo_guard(request)
    _check_token(request, token)
    from . import report
    tz = ZoneInfo(config.TIMEZONE)
    try:
        day = (datetime.strptime(date, "%Y-%m-%d").date()
               if date else datetime.now(tz).date())
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    pdf = await asyncio.to_thread(report.build_daily_pdf, day)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="tasks-{day.isoformat()}.pdf"'},
    )


# ── Email login (RBAC) ───────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = Query("")):
    if _session_user(request):
        return RedirectResponse(url="/", status_code=302)
    demo_user = next((db.get_user(e) for e in sorted(config.DEMO_EMAILS)
                      if db.get_user(e)), None)
    return templates.TemplateResponse(
        request, "login.html",
        {"error": error,
         "demo_available": bool(demo_user and demo_user.get("active"))})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    user = db.get_user(email)
    if not user or not user.get("active") or \
       not auth.verify_password(password, user.get("salt", ""), user.get("pass_hash", "")):
        return RedirectResponse(url="/login?error=Wrong+email+or+password",
                                status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(email),
                    max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
                    secure=(request.url.scheme == "https"))
    return resp


@app.post("/demo-login")
@app.get("/demo-login")
async def demo_login(request: Request):
    """One click into the demo. The password never appears in the page — the
    server signs the visitor in as the demo account directly. Only accounts
    listed in DEMO_EMAILS can be entered this way, and those accounts read
    generated sample data, never the real database."""
    email = next((e for e in sorted(config.DEMO_EMAILS)
                  if (db.get_user(e) or {}).get("active")), None)
    if not email:
        return RedirectResponse(url="/login?error=Demo+is+not+available",
                                status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.make_session(email),
                    max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
                    secure=(request.url.scheme == "https"))
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


# ── Admin: user management (who belongs to which department) ────────────────

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, token: str = Query(""), msg: str = Query("")):
    _check_token(request, token)
    return templates.TemplateResponse(
        request, "users.html",
        {"token": token, "users": _src(request).list_users(),
         "departments": config.DEPARTMENTS, "msg": msg},
    )


@app.post("/users/action")
async def users_action(request: Request, token: str = Query("")):
    _demo_guard(request)
    _check_token(request, token)
    form = await request.form()
    action = str(form.get("action", ""))
    email = str(form.get("email", "")).strip().lower()
    msg = "done"
    if action == "add":
        password = str(form.get("password", ""))
        department = str(form.get("department", "admin")).lower()
        role = "admin" if str(form.get("role", "")) == "admin" else "member"
        if department not in config.DEPARTMENTS:
            department = "admin"
        if not email or "@" not in email or len(password) < 6:
            msg = "need a valid email and a password of 6+ characters"
        elif db.get_user(email):
            msg = f"{email} already exists"
        else:
            salt, ph = auth.hash_password(password)
            db.create_user(email, salt, ph, department, role)
            msg = f"added {email} to {department}"
    elif action == "dept":
        department = str(form.get("department", "")).lower()
        if department in config.DEPARTMENTS:
            db.update_user(email, department=department,
                           role="admin" if department == "admin" else None)
            msg = f"{email} moved to {department}"
    elif action == "password":
        password = str(form.get("password", ""))
        if len(password) >= 6:
            salt, ph = auth.hash_password(password)
            db.update_user(email, salt=salt, pass_hash=ph)
            msg = f"password reset for {email}"
        else:
            msg = "password must be 6+ characters"
    elif action == "toggle":
        u = db.get_user(email)
        if u:
            db.update_user(email, active=0 if u.get("active") else 1)
            msg = f"{email} {'deactivated' if u.get('active') else 'reactivated'}"
    from urllib.parse import quote
    return RedirectResponse(url=f"/users?token={token}&msg={quote(msg)}",
                            status_code=303)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True}

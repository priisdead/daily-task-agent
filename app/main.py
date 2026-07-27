"""FastAPI app: Meta webhook + daily scheduler + task dashboard."""
import asyncio
import logging
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

from . import auth, config, db, digest, extractor, notify, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
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
    scheduler.start()
    log.info(
        "Scheduler started: scanning every %d minutes, digest daily at %02d:00 (%s)",
        config.SCAN_INTERVAL_MINUTES, config.DIGEST_HOUR, config.TIMEZONE,
    )
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="WhatsApp + Email Task Agent", lifespan=lifespan)


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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, token: str = Query(""), q: str = Query("")):
    try:
        dept = _dept_for(request, token)
    except HTTPException:
        return RedirectResponse(url="/login", status_code=302)
    user = _session_user(request)
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz)
    query = q.strip().lower()
    tasks = db.open_tasks()
    done_today = db.tasks_done_today(datetime.utcnow().date().isoformat())
    active_ids = {t["id"] for t in tasks} | {t["id"] for t in done_today}
    archive = [t for t in db.all_tasks() if t["id"] not in active_ids]
    if dept not in ("admin", "management"):
        # department credential: only this department's tasks.
        # (management is oversight — sees every department's tasks,
        # but has no admin pages/controls)
        tasks = [t for t in tasks if (t.get("department") or "") == dept]
        done_today = [t for t in done_today if (t.get("department") or "") == dept]
        archive = [t for t in archive if (t.get("department") or "") == dept]
    if query:
        tasks = [t for t in tasks if _matches(t, query)]
        done_today = [t for t in done_today if _matches(t, query)]
        archive = [t for t in archive if _matches(t, query)]
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
            "date_str": today.strftime("%A, %d %B %Y"),
            "by_client": by_client,
            "open_count": sum(1 for t in tasks if t["status"] == "open"),
            "progress_count": sum(1 for t in tasks if t["status"] == "in_progress"),
            "done_today": done_today,
            "archive": archive,
            "last_run": db.last_run(),
        },
    )


@app.get("/mails", response_class=HTMLResponse)
async def mails(request: Request, token: str = Query("")):
    """Email history, one section per inbox."""
    _check_token(request, token)
    emails = db.all_emails()
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
        {"token": token, "wa_messages": db.all_wa_messages()},
    )


@app.get("/history")
async def history_redirect(token: str = Query("")):
    return RedirectResponse(url=f"/mails?token={token}", status_code=302)


@app.get("/backfill", response_class=HTMLResponse)
async def backfill(request: Request, token: str = Query(""), days: int = Query(30, ge=1, le=90)):
    """One-time seeding from past mail. Runs in the background."""
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
        db.set_task_department(task_id, department)
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
        db.add_task({
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


@app.post("/tasks/{task_id}/done")
async def task_done(request: Request, task_id: int, token: str = Query("")):
    _dept_for(request, token)  # any valid credential may close its tasks
    form = await request.form()
    user = _session_user(request)
    db.set_task_status(
        task_id, "done",
        remark=str(form.get("remark", "")),
        done_by=(user or {}).get("email", ""),
    )
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/tasks/{task_id}/reopen")
async def task_reopen(request: Request, task_id: int, token: str = Query("")):
    _dept_for(request, token)
    db.set_task_status(task_id, "open")
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/run-now")
async def run_now(request: Request, token: str = Query("")):
    """Manually trigger the digest (useful for testing and mid-day refreshes).
    Fire-and-forget: the scan runs in the background (it can take minutes now
    that batches are paced for the rate limit); if one is already running the
    trigger is simply ignored."""
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
        {"token": token, "days": days, "rows": db.skipped_since(since, limit=500)},
    )


@app.get("/digest", response_class=PlainTextResponse)
async def digest_preview(request: Request, token: str = Query(""), send: int = Query(0)):
    """Preview today's digest as plain text; add &send=1 to email it now."""
    _check_token(request, token)
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
    _check_token(request, token)
    return db.pipeline_stats()


@app.get("/report.pdf")
async def report_pdf(request: Request, token: str = Query(""), date: str = Query("")):
    """Downloadable daily report: every department's tasks for the chosen
    date (admin only). ?date=YYYY-MM-DD, defaults to today."""
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
    return templates.TemplateResponse(request, "login.html", {"error": error})


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
        {"token": token, "users": db.list_users(),
         "departments": config.DEPARTMENTS, "msg": msg},
    )


@app.post("/users/action")
async def users_action(request: Request, token: str = Query("")):
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

"""FastAPI app: Meta webhook + daily scheduler + task dashboard."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db, extractor, whatsapp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(
        extractor.run_digest,
        IntervalTrigger(minutes=config.SCAN_INTERVAL_MINUTES),
        id="scan",
        replace_existing=True,
        max_instances=1,       # never let two scans overlap
        coalesce=True,         # if the host slept, run once, not N times
    )
    scheduler.start()
    log.info(
        "Scheduler started: scanning every %d minutes (%s)",
        config.SCAN_INTERVAL_MINUTES, config.TIMEZONE,
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

def _check_token(token: str) -> None:
    if not config.DASHBOARD_TOKEN or token != config.DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing ?token=")


def _matches(t: dict, q: str) -> bool:
    hay = f"{t.get('client','')} {t.get('contact','')} {t.get('request','')}".lower()
    return q in hay


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, token: str = Query(""), q: str = Query("")):
    _check_token(token)
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz)
    query = q.strip().lower()
    tasks = db.open_tasks()
    done_today = db.tasks_done_today(datetime.utcnow().date().isoformat())
    active_ids = {t["id"] for t in tasks} | {t["id"] for t in done_today}
    archive = [t for t in db.all_tasks() if t["id"] not in active_ids]
    if query:
        tasks = [t for t in tasks if _matches(t, query)]
        done_today = [t for t in done_today if _matches(t, query)]
        archive = [t for t in archive if _matches(t, query)]
    by_client: dict[str, list] = {}
    for t in tasks:
        by_client.setdefault(t["client"] or "Unknown", []).append(t)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "token": token,
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
    _check_token(token)
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
    _check_token(token)
    return templates.TemplateResponse(
        request, "whatsapp.html",
        {"token": token, "wa_messages": db.all_wa_messages()},
    )


@app.get("/history")
async def history_redirect(token: str = Query("")):
    return RedirectResponse(url=f"/mails?token={token}", status_code=302)


@app.get("/backfill", response_class=HTMLResponse)
async def backfill(token: str = Query(""), days: int = Query(30, ge=1, le=90)):
    """One-time seeding from past mail. Runs in the background."""
    _check_token(token)
    asyncio.get_running_loop().run_in_executor(None, extractor.run_backfill, days)
    return HTMLResponse(
        f"<body style='font-family:sans-serif;padding:40px'>"
        f"<h3>Backfill started — reading the last {days} days of mail.</h3>"
        f"<p>This runs in the background and can take a few minutes "
        f"(batched AI processing). Refresh the <a href='/?token={token}'>Tasks page</a> "
        f"to watch tasks appear.</p></body>"
    )


@app.post("/tasks/{task_id}/done")
async def task_done(task_id: int, token: str = Query("")):
    _check_token(token)
    db.set_task_status(task_id, "done")
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/tasks/{task_id}/reopen")
async def task_reopen(task_id: int, token: str = Query("")):
    _check_token(token)
    db.set_task_status(task_id, "open")
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.post("/run-now")
async def run_now(token: str = Query("")):
    """Manually trigger the digest (useful for testing and mid-day refreshes).
    Fire-and-forget: the scan runs in the background (it can take minutes now
    that batches are paced for the rate limit); if one is already running the
    trigger is simply ignored."""
    _check_token(token)
    asyncio.get_running_loop().run_in_executor(None, extractor.run_digest)
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.get("/reprocess", response_class=HTMLResponse)
async def reprocess(token: str = Query("")):
    """Re-run task extraction over EVERY stored mail/WA message (after a
    prompt improvement). Open tasks are passed to the AI so it won't
    duplicate them. Runs in the background."""
    _check_token(token)
    n = db.reset_processed()
    asyncio.get_running_loop().run_in_executor(None, extractor.run_digest)
    return HTMLResponse(
        f"<body style='font-family:sans-serif;padding:40px'>"
        f"<h3>Re-scanning {n} stored messages with the improved extractor.</h3>"
        f"<p>This runs in the background in batches and can take a few minutes. "
        f"Refresh the <a href='/?token={token}'>Tasks page</a> to watch tasks appear.</p></body>"
    )


@app.get("/stats")
async def stats(token: str = Query("")):
    """Pipeline visibility: how many mails/WA msgs are captured, processed,
    still queued for the AI, plus task counts and recent runs."""
    _check_token(token)
    return db.pipeline_stats()


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True}

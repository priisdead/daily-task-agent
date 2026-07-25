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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, token: str = Query("")):
    _check_token(token)
    tz = ZoneInfo(config.TIMEZONE)
    today = datetime.now(tz)
    tasks = db.open_tasks()
    by_client: dict[str, list] = {}
    for t in tasks:
        by_client.setdefault(t["client"] or "Unknown", []).append(t)
    done_today = db.tasks_done_today(datetime.utcnow().date().isoformat())
    active_ids = {t["id"] for t in tasks} | {t["id"] for t in done_today}
    archive = [t for t in db.all_tasks() if t["id"] not in active_ids]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "token": token,
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
    """Manually trigger the digest (useful for testing and mid-day refreshes)."""
    _check_token(token)
    await asyncio.to_thread(extractor.run_digest)
    return RedirectResponse(url=f"/?token={token}", status_code=303)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"ok": True}

"""Claude API call: turn raw WhatsApp messages + emails into structured tasks."""
import json
import logging

import httpx

from . import config, db

log = logging.getLogger("extractor")

SYSTEM_PROMPT = """You are a task-extraction engine for a business that receives \
client requests over WhatsApp and email. You are given (1) the current task \
list (open and in-progress), (2) new WhatsApp messages, and (3) new emails. \
Emails marked [YOUR REPLY] were sent BY the business owner; everything else \
came FROM clients.

Return ONLY a JSON object, no prose, with this exact shape:
{
  "new_tasks": [
    {
      "client": "best-known client/company name (use profile name or email sender)",
      "contact": "phone number or email address",
      "channel": "whatsapp" | "email",
      "request": "one clear sentence: what the client needs done",
      "deadline": "deadline if stated or clearly implied, else \\"\\"",
      "priority": "high" | "normal" | "low",
      "source": "short quote (max 25 words) from the originating message"
    }
  ],
  "in_progress_task_ids": [integers — task IDs the owner has ACKNOWLEDGED or committed to (e.g. replied "ok, will send it", "working on it", "sure, by Monday")],
  "resolved_task_ids": [integers — task IDs that are COMPLETED, cancelled, or superseded (e.g. owner replied "sent", "dispatched", "done", or client withdrew the request)]
}

Rules:
- Only extract genuine, actionable client requests. Greetings, acknowledgements,
  thanks, marketing mail, and newsletters produce NO tasks.
- The owner's own replies ([YOUR REPLY]) NEVER create new tasks — they only
  move existing tasks to in-progress or resolved.
- An acknowledgement ("ok I will send it") = in_progress_task_ids. A completion
  ("sent it today", "dispatched") = resolved_task_ids. When ambiguous, prefer
  in_progress.
- If a client asks about the same thing on both WhatsApp and email, create ONE
  task and mention both in "source".
- If a new message is an update to an existing task (e.g. changed quantity or
  new deadline), create a new task capturing the latest state and put the old
  task's ID in resolved_task_ids.
- Never invent deadlines. Keep the client's own wording for dates ("Friday",
  "by month end").
- priority is "high" only when urgency is explicit or a deadline is within ~48h.
"""


def _format_context(open_task_list, wa_msgs, emails) -> str:
    parts = ["## CURRENT TASKS (open / in-progress)"]
    if open_task_list:
        for t in open_task_list:
            parts.append(
                f"- id={t['id']} [{t['status']}] | {t['client']} | {t['request']} "
                f"| deadline: {t['deadline'] or '—'}"
            )
    else:
        parts.append("(none)")

    parts.append("\n## NEW WHATSAPP MESSAGES")
    if wa_msgs:
        for m in wa_msgs:
            name = m["sender_name"] or m["sender"]
            parts.append(f"[{m['ts']}] {name} ({m['sender']}): {m['body']}")
    else:
        parts.append("(none)")

    parts.append("\n## NEW EMAILS")
    if emails:
        for e in emails:
            tag = "[YOUR REPLY] " if e.get("direction") == "outgoing" else ""
            parts.append(
                f"{tag}[{e['ts']}] Inbox: {e.get('account', '')}\nFrom: {e['sender']}\n"
                f"Subject: {e['subject']}\n"
                f"{(e['body'] or e['snippet'])[:2000]}\n---"
            )
    else:
        parts.append("(none)")
    return "\n".join(parts)


def _empty(failed: bool = False) -> dict:
    return {"new_tasks": [], "resolved_task_ids": [],
            "in_progress_task_ids": [], "failed": failed}


def _call_claude(context: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _call_gemini(context: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent"
    )
    resp = httpx.post(
        url,
        params={"key": config.GEMINI_API_KEY},
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": context}]}],
            "generationConfig": {
                "maxOutputTokens": 16000,
                "responseMimeType": "application/json",
            },
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def extract_tasks(open_task_list, wa_msgs, emails) -> dict:
    """Call the configured LLM; always returns a dict with new_tasks,
    resolved_task_ids, in_progress_task_ids, failed."""
    if not wa_msgs and not emails:
        return _empty()

    context = _format_context(open_task_list, wa_msgs, emails)
    try:
        if config.LLM_PROVIDER == "gemini":
            text = _call_gemini(context)
        else:
            text = _call_claude(context)
    except Exception:
        log.exception("%s API call failed", config.LLM_PROVIDER)
        return _empty(failed=True)
    # tolerate accidental code fences
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (ValueError, json.JSONDecodeError):
        log.error("%s returned unparseable output: %.500s", config.LLM_PROVIDER, text)
        return _empty(failed=True)
    data.setdefault("new_tasks", [])
    data.setdefault("resolved_task_ids", [])
    data.setdefault("in_progress_task_ids", [])
    data["failed"] = False
    return data


def _apply(result: dict, open_task_list: list) -> None:
    """Write an extraction result to the database."""
    for t in result["new_tasks"]:
        db.add_task(t)
    valid_ids = {t["id"] for t in open_task_list}
    for tid in result["in_progress_task_ids"]:
        if isinstance(tid, int) and tid in valid_ids:
            db.set_task_status(tid, "in_progress")
    for tid in result["resolved_task_ids"]:
        if isinstance(tid, int) and tid in valid_ids:
            db.set_task_status(tid, "done")


BATCH = 30          # messages per AI call — safe for response-size limits
MAX_BATCHES = 40    # safety valve per run


def _process_batches() -> tuple[int, int, int]:
    """Run extraction over everything unprocessed, in chronological batches.
    A failed batch (API error / truncated output) is NOT marked processed —
    it stays queued and is retried on the next scan. Returns totals
    (wa, emails, new_tasks)."""
    total_wa = total_em = total_new = batches = 0
    while batches < MAX_BATCHES:
        emails = db.unprocessed_emails()[:BATCH]
        wa_msgs = db.unprocessed_wa_messages()[:BATCH]
        if not emails and not wa_msgs:
            break
        open_task_list = db.open_tasks()
        result = extract_tasks(open_task_list, wa_msgs, emails)
        if result["failed"]:
            log.error("batch failed — leaving %d emails / %d WA msgs queued for retry",
                      len(emails), len(wa_msgs))
            break
        _apply(result, open_task_list)
        db.mark_processed("emails", [e["id"] for e in emails])
        db.mark_processed("wa_messages", [m["id"] for m in wa_msgs])
        total_em += len(emails)
        total_wa += len(wa_msgs)
        total_new += len(result["new_tasks"])
        batches += 1
        log.info("batch %d: %d emails, %d WA so far -> %d new tasks",
                 batches, total_em, total_wa, total_new)
    return total_wa, total_em, total_new


def run_digest() -> None:
    """The recurring job: pull mail, then extract tasks in safe batches."""
    from . import gmail_client  # local import so webhook can run without Gmail set up

    started = db.utcnow()
    try:
        gmail_client.fetch_recent_emails()
    except Exception:
        log.exception("gmail fetch failed — continuing with WhatsApp only")

    wa, em, new = _process_batches()
    db.record_run(started, wa, em, new)
    log.info("digest: %d WA msgs, %d emails -> %d new tasks", wa, em, new)


def run_backfill(days: int = 30) -> None:
    """One-time seed: fetch up to `days` of mail history across all inboxes,
    then extract tasks chronologically so the AI sees conversations unfold
    in order and closes what got resolved."""
    from . import gmail_client

    started = db.utcnow()
    query = f"newer_than:{days}d (in:inbox OR in:sent) -category:{{promotions social}}"
    try:
        fetched = gmail_client.fetch_recent_emails(query=query, max_pages=5)
        log.info("backfill: fetched %d emails from last %d days", fetched, days)
    except Exception:
        log.exception("backfill: gmail fetch failed")

    wa, em, new = _process_batches()
    db.record_run(started, wa, em, new, note=f"backfill {days}d")
    log.info("backfill complete: %d emails -> %d tasks", em, new)

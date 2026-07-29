"""Google Sheets sync: pulls the production sheet (the one feeding the CXO
Production Control dashboard) into the agent, links rows to POs, and raises
high-priority Implementation tasks when a line is at risk.

Reads the SAME columns the dashboard reads: unique id, po, customer name,
description, po ok pcs, till total production done, pending production,
ship ready date, priority, production start date, status.

Configure with SHEETS_API_KEY + PROD_SHEET_ID (paste the same values you use
in the dashboard's connect panel). Nothing runs until both are set."""
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import config, db

log = logging.getLogger("sheets")


# ── fetching ─────────────────────────────────────────────────────────────────

def extract_sheet_id(value: str) -> str:
    """Accepts a bare spreadsheet ID or a full docs.google.com URL."""
    value = (value or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)", value)
    return m.group(1) if m else value


def fetch_values(sheet_id: str, tab: str, api_key: str) -> list:
    """Raw rows from the Sheets API v4 (same endpoint the dashboard uses)."""
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/"
           f"{extract_sheet_id(sheet_id)}/values/{tab}")
    resp = httpx.get(url, params={"key": api_key}, timeout=60)
    resp.raise_for_status()
    return resp.json().get("values", [])


# ── parsing (tolerant, header-name based, like the dashboard) ────────────────

def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def _pint(v) -> int:
    m = re.findall(r"-?\d+", str(v or "").replace(",", ""))
    return int(m[0]) if m else 0

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

def parse_date(v) -> str:
    """Best-effort → ISO date string, else ''. Handles dd/mm/yyyy, yyyy-mm-dd,
    dd-Mon-yyyy, '27 Jul 2026'."""
    s = str(v or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r"(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-,]*(\d{2,4})?", s)
    if m:
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if mon:
            yr = int(m.group(3) or datetime.now().year)
            if yr < 100:
                yr += 2000
            try:
                return date(yr, mon, int(m.group(1))).isoformat()
            except ValueError:
                pass
    return ""


def rows_to_records(values: list) -> list:
    """Header row + data rows → list of production dicts."""
    if not values:
        return []
    headers = [_norm(h) for h in values[0]]

    def idx(*names):
        # exact header match wins first (so "po" finds "PO", not "PO Date"),
        # then fall back to contains-matching
        for n in names:
            for i, h in enumerate(headers):
                if n == h:
                    return i
        for n in names:
            for i, h in enumerate(headers):
                if n in h:
                    return i
        return -1

    cols = {
        "uid": idx("unique id"),
        "po": idx("po"),
        "customer": idx("customer name", "customer"),
        "desc": idx("description"),
        "po_qty": idx("po ok pcs", "po qty"),
        "done": idx("till total production done", "production done"),
        "pending": idx("pending production", "pending"),
        "ship_ready": idx("ship ready date", "ship ready", "crd"),
        "priority": idx("priority"),
        "prod_start": idx("production start date", "prod start"),
        "status": idx("status"),
    }

    def cell(row, key):
        i = cols[key]
        return row[i] if 0 <= i < len(row) else ""

    out, last_customer, last_po = [], "", ""
    for row in values[1:]:
        uid = str(cell(row, "uid")).strip()
        if not uid:
            continue
        customer = str(cell(row, "customer")).strip() or last_customer
        po = str(cell(row, "po")).strip() or last_po
        last_customer, last_po = customer, po
        out.append({
            "uid": uid,
            "po_number": po.upper(),
            "customer": customer,
            "description": re.sub(r"\s+", " ", str(cell(row, "desc"))).strip()[:300],
            "po_qty": _pint(cell(row, "po_qty")),
            "done_qty": _pint(cell(row, "done")),
            "pending_qty": max(0, _pint(cell(row, "pending"))),
            "ship_ready": parse_date(cell(row, "ship_ready")),
            "priority": _norm(cell(row, "priority")) or "medium",
            "prod_start": parse_date(cell(row, "prod_start")),
            "sheet_status": str(cell(row, "status")).strip()[:80],
        })
    return out


# ── sync + risk detection ────────────────────────────────────────────────────

def configured() -> bool:
    return bool(config.SHEETS_API_KEY and config.PROD_SHEET_ID)


def sync_production() -> dict:
    """Pull the sheet, store it, ensure PO records exist, raise at-risk tasks.
    Returns a summary dict; logs an event on failure instead of raising."""
    if not configured():
        return {"synced": 0, "skipped": "not configured"}
    try:
        values = fetch_values(config.PROD_SHEET_ID, config.PROD_SHEET_TAB,
                              config.SHEETS_API_KEY)
        records = rows_to_records(values)
        db.replace_production_rows(records)
        db.upsert_pos_bulk([(r["po_number"], r["customer"])
                            for r in records if r["po_number"]])
        risks = _raise_risk_tasks(records)
        log.info("sheet sync: %d rows, %d at-risk tasks raised",
                 len(records), risks)
        return {"synced": len(records), "risk_tasks": risks}
    except Exception as exc:
        log.exception("sheet sync failed")
        db.log_event("error", "sheets", f"production sheet sync failed: {exc}")
        return {"synced": 0, "error": str(exc)[:200]}


# ── stage-tracking sheet (Dragpal ji's 26-stage sheet) ──────────────────────

_DONE_WORDS = ("true", "completed", "complete", "done", "delivered", "yes", "ok")
_SKIP_STAGE_COLS = ("final remarks",)  # free-text, not a checkbox stage


def _stage_done(v) -> bool:
    return _norm(v) in _DONE_WORDS


def tracking_configured() -> bool:
    return bool(config.SHEETS_API_KEY and config.TRACK_SHEET_ID)


def rows_to_tracking(values: list) -> list:
    """Two header rows: row 1 = stage owner (forward-filled), row 2 = stage
    name. Rows 3+ = one PO per row, stages TRUE/Completed/Delivered as done."""
    if len(values) < 3:
        return []
    import json as _json
    owners_raw = values[0]
    headers = [str(h or "").strip() for h in values[1]]
    # forward-fill the owner row (merged cells arrive blank)
    owners, last = [], ""
    for i in range(len(headers)):
        o = str(owners_raw[i]).strip() if i < len(owners_raw) else ""
        if o:
            last = o
        owners.append(last)

    def h(name):
        n = _norm(name)
        for i, hd in enumerate(headers):
            if _norm(hd) == n:
                return i
        return -1

    i_ref, i_cust = h("customer ref #"), h("customer name")
    i_pod, i_crd = h("po date"), h("cargo ready date")
    i_closed = h("final po closed")
    fixed = {i_ref, i_cust, i_pod, i_crd}
    stage_idx = [i for i, hd in enumerate(headers)
                 if i not in fixed and hd
                 and _norm(hd) not in _SKIP_STAGE_COLS]

    out = []
    for row in values[2:]:
        def cell(i):
            return row[i] if 0 <= i < len(row) else ""
        po = str(cell(i_ref)).strip()
        if not po:
            continue
        stages = [{"stage": headers[i], "owner": owners[i],
                   "done": _stage_done(cell(i))} for i in stage_idx]
        done_n = sum(1 for s in stages if s["done"])
        pending = [s for s in stages if not s["done"]]
        closed = _stage_done(cell(i_closed)) if i_closed >= 0 else False
        if closed or not pending:
            status = "closed"
        elif done_n == 0:
            status = "not_started"
        else:
            status = "in_progress"
        out.append({
            "po_number": po.upper(),
            "customer": str(cell(i_cust)).strip(),
            "po_date": parse_date(cell(i_pod)),
            "cargo_ready": parse_date(cell(i_crd)),
            "stages_json": _json.dumps(stages),
            "stages_done": done_n,
            "stages_total": len(stages),
            "current_stage": pending[0]["stage"] if pending else "",
            "current_owner": pending[0]["owner"] if pending else "",
            "track_status": status,
        })
    return out


# ── Team KRA: per-stage SLAs (days after PO Date) — mirrors the CXO
# dashboard's KRA_SLA table exactly, so both always show the same numbers.
_KRA_SLA = [
    (r"final\s*remark", None),          # no SLA
    (r"final\s*po\s*closed", 20),
    (r"po\s*created", 1),
    (r"bom", 1),
    (r"indent", 1),
    (r"filter\s*/?\s*sticker\s*temp", 2),
    (r"sample\s*picture", 2),
    (r"paper\s*sent", 3),
    (r"printing", 8),
    (r"paper\s*receipt", 9),
    (r"images\s*sent", 10),
    (r"images\s*approved", 12),
    (r"production\s*filter\s*cutting", 11),
    (r"filter\s*breaking", 11),
    (r"filter\s*folding", 13),
    (r"paper\s*cutting", 11),
    (r"tools\s*inspected", 12),
    (r"received\s*from\s*packaging", 16),
    (r"quality", 15),
    (r"equalling|equaling", 15),
    (r"packaging", 15),
    (r"dispa", 18),                     # sheet spells it "Dispacthed"
    (r"tracking", 19),
    (r"pod", 20),
    (r"^\s*(production|proction)\s*$", 13),
]


def sla_days_for(stage) -> int | None:
    n = str(stage or "").lower()
    for pat, d in _KRA_SLA:
        if re.search(pat, n):
            return d
    return None


def team_pastdue() -> list:
    """One row per past-due stage task across all non-closed POs.
    Due date = PO Date + the stage's SLA days (same rule as the CXO
    dashboard). Sorted worst-first."""
    import json as _json
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    rows = []
    for r in db.tracking_all():
        if (r.get("track_status") or "") == "closed" or not r.get("po_date"):
            continue
        try:
            pod = date.fromisoformat(r["po_date"])
        except ValueError:
            continue
        try:
            stages = _json.loads(r.get("stages_json") or "[]")
        except ValueError:
            continue
        for s in stages:
            if s.get("done"):
                continue
            sla = sla_days_for(s.get("stage"))
            if sla is None:
                continue
            due = pod + timedelta(days=sla)
            late = (today - due).days
            if late <= 0:
                continue
            rows.append({
                "owner": (s.get("owner") or "").strip() or "(no owner)",
                "stage": s.get("stage") or "",
                "po_number": r["po_number"],
                "customer": r.get("customer") or "",
                "po_date": r["po_date"],
                "due": due.isoformat(),
                "days_late": late,
            })
    rows.sort(key=lambda x: -x["days_late"])
    return rows


def sync_tracking() -> dict:
    """Pull the stage-tracking sheet, store it, raise tasks for stuck orders."""
    if not tracking_configured():
        return {"synced": 0, "skipped": "not configured"}
    try:
        values = fetch_values(config.TRACK_SHEET_ID, config.TRACK_SHEET_TAB,
                              config.SHEETS_API_KEY)
        records = rows_to_tracking(values)
        db.replace_tracking_rows(records)
        risks = _raise_tracking_tasks(records)
        log.info("tracking sync: %d rows, %d stuck-order tasks", len(records), risks)
        return {"synced": len(records), "risk_tasks": risks}
    except Exception as exc:
        log.exception("tracking sheet sync failed")
        db.log_event("error", "sheets", f"tracking sheet sync failed: {exc}")
        return {"synced": 0, "error": str(exc)[:200]}


def _raise_tracking_tasks(records: list) -> int:
    """Two situations create an implementation task (one open task per PO):
    1. NOT STARTED: zero stages done N days after the PO date.
    2. STUCK PAST CRD: cargo-ready date passed but the order isn't closed —
       task names the pending stage and the person who owns it."""
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    open_reqs = " ".join((t.get("request") or "") for t in db.open_tasks())
    raised = 0
    for r in records:
        if r["track_status"] == "closed":
            continue
        marker = f"[TRK:{r['po_number']}]"
        if marker in open_reqs:
            continue
        reason = ""
        if r["track_status"] == "not_started" and r["po_date"]:
            try:
                age = (today - date.fromisoformat(r["po_date"])).days
            except ValueError:
                age = 0
            if age >= config.TRACK_NOT_STARTED_DAYS:
                reason = (f"no stage started yet, PO is {age} days old "
                          f"(PO date {r['po_date']})")
        elif r["cargo_ready"]:
            try:
                overdue = (today - date.fromisoformat(r["cargo_ready"])).days
            except ValueError:
                overdue = 0
            if overdue > 0:
                reason = (f"cargo-ready date {r['cargo_ready']} passed "
                          f"{overdue} day{'s' if overdue != 1 else ''} ago, "
                          f"stuck at '{r['current_stage']}'"
                          + (f" ({r['current_owner']})" if r['current_owner'] else "")
                          + f" — {r['stages_done']}/{r['stages_total']} stages done")
        if not reason:
            continue
        db.add_task({
            "client": r["customer"] or "Production",
            "channel": "sheet",
            "request": (f"Tracking sheet {marker} {r['po_number']}: {reason}"),
            "department": "implementation",
            "po_number": r["po_number"],
            "deadline": r["cargo_ready"],
            "priority": "high",
            "source": "stage tracking sheet sync",
        })
        raised += 1
    return raised


def _raise_risk_tasks(records: list) -> int:
    """A line is AT RISK when production is still pending and the ship-ready
    date is within RISK_DAYS (or already past). One open task per line."""
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    horizon = today + timedelta(days=config.SHEET_RISK_DAYS)
    lookback = today - timedelta(days=config.SHEET_RISK_LOOKBACK_DAYS)
    open_reqs = " ".join(
        (t.get("request") or "") for t in db.open_tasks())
    raised = 0
    for r in records:
        # the factory's Status column overrides quantity math: completed,
        # shipped, held or cancelled lines never raise risk tasks
        st = (r.get("sheet_status") or "").lower()
        if any(w in st for w in ("complete", "done", "shipped", "dispatch",
                                 "hold", "cancel")):
            continue
        # tiny leftover quantities and long-stale rows are noise, not risk
        if r["pending_qty"] < config.SHEET_RISK_MIN_PENDING or not r["ship_ready"]:
            continue
        try:
            ready = date.fromisoformat(r["ship_ready"])
        except ValueError:
            continue
        if ready > horizon or ready < lookback:
            continue
        marker = f"[{r['uid']}]"
        if marker in open_reqs:
            continue  # already flagged and still open
        overdue = ready < today
        db.add_task({
            "client": r["customer"] or "Production",
            "channel": "sheet",
            "request": (f"Production at risk {marker} {r['po_number']}: "
                        f"{r['pending_qty']} pcs pending, ship-ready "
                        f"{'was ' if overdue else ''}{r['ship_ready']} — "
                        f"{r['description'][:80]}"),
            "department": "implementation",
            "po_number": r["po_number"],
            "deadline": r["ship_ready"],
            "priority": "high",
            "source": "production sheet sync",
        })
        raised += 1
    return raised

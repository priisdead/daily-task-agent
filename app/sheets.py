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

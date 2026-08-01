"""Demo data source.

When a demo account logs in, every page reads from HERE instead of the real
database, so a portfolio viewer sees the product working end to end without
ever touching live company data. Nothing in this module talks to Postgres.

The dataset is generated deterministically from a fixed seed and anchored to
*today*, so the demo always looks current. Clicks (Done, assign, schedule)
mutate this in-memory copy, which makes the demo genuinely interactive; it
resets whenever the service restarts. Every name here is invented.
"""
import random
import re
from datetime import date, datetime, timedelta, timezone

_SEED = 20260730

CLIENTS = [
    "Northwind Papers", "Cascade Botanicals", "Emberleaf Trading",
    "Kite & Vine", "Harborline Distribution", "Silverbirch Retail",
]
FORWARDERS = ["Meridian Freight", "Blue Anchor Logistics", "Cardinal Cargo"]
BANKS = ["Meridian Bank", "Coastal Credit"]

# invented factory owners for the stage-tracking / Team view
OWNERS = [
    ("Printing (M. Khan)", "printing"),
    ("Filter Cutting (A. Rao)", "cutting"),
    ("Filter Folding (S. Iyer)", "folding"),
    ("Paper Cutting (T. Verma)", "paper"),
    ("Quality Check (N. Joshi)", "qc"),
    ("Packing (D. Sen)", "packing"),
    ("Dispatch (R. Patel)", "dispatch"),
]

PRODUCTS = [
    "98/26mm Classic Cone — Arctic White",
    "109/26mm Classic Cone — Natural Brown",
    "84/26mm OneD Cone — Timber",
    "120/26mm Wide Cone — Unbleached",
    "Pre-roll Tube — Matte Black",
    "Filter Tips — Perforated 50s",
]

CLIENT_ASKS = [
    "Confirm whether {qty}k cones with double glue line can ship within 2 weeks",
    "Share updated price list for {prod}",
    "Send 3 sample packs of {prod} to the Vancouver office",
    "Any update on our order {po}? Client is chasing",
    "Confirm carton dimensions and gross weight for {po}",
    "Provide COA and food-grade certificate for {prod}",
    "Change sticker quantity on {po} from 40 to 48 per carton",
    "Quote for {qty}k units, delivered duty paid",
]
FORWARDER_ASKS = [
    "Approve the shipping checklist for {po}",
    "Share vehicle and driver details for tomorrow's pickup ({po})",
    "Send commercial invoice and packing list for {po}",
    "Confirm cargo-ready date for {po} — booking closes Friday",
    "Provide e-way bill for the {po} consignment",
]
INTERNAL_ASKS = [
    "Submit updated compliance documents to {bank}",
    "Reconcile freight invoice against quotation for {po}",
    "Approve artwork revision 2 for {prod}",
]

DEPTS_BY_KIND = {
    "client": ["admin", "implementation", "qc", "design", "production"],
    "forwarder": ["logistics", "logistics", "implementation"],
    "internal": ["accounts", "design", "management"],
}

# The 26 factory stages, with the SLA days used by the Team page
STAGES = [
    ("PO Created", "Printing (M. Khan)", 1),
    ("BOM Finalized", "Printing (M. Khan)", 1),
    ("Indent Raised", "Printing (M. Khan)", 1),
    ("Sticker Template", "Printing (M. Khan)", 2),
    ("Sample Approval", "Printing (M. Khan)", 2),
    ("Paper Sent to Printer", "Printing (M. Khan)", 3),
    ("Printing of Filter", "Printing (M. Khan)", 8),
    ("Paper Receipt", "Printing (M. Khan)", 9),
    ("Production Images Sent", "Filter Cutting (A. Rao)", 10),
    ("Production Filter Cutting", "Filter Cutting (A. Rao)", 11),
    ("Filter Breaking", "Filter Cutting (A. Rao)", 11),
    ("Paper Cutting", "Paper Cutting (T. Verma)", 11),
    ("Images Approved", "Filter Folding (S. Iyer)", 12),
    ("Tools Inspected", "Filter Folding (S. Iyer)", 12),
    ("Filter Folding", "Filter Folding (S. Iyer)", 13),
    ("Production", "Filter Folding (S. Iyer)", 13),
    ("Quality Check", "Quality Check (N. Joshi)", 15),
    ("Equalling (Final)", "Quality Check (N. Joshi)", 15),
    ("Packaging", "Packing (D. Sen)", 15),
    ("Received from Packaging", "Packing (D. Sen)", 16),
    ("Material Dispatched", "Dispatch (R. Patel)", 18),
    ("Tracking Update", "Dispatch (R. Patel)", 19),
    ("POD Shared", "Dispatch (R. Patel)", 20),
    ("Final PO Closed", "Dispatch (R. Patel)", 20),
]

PO_STAGES = ["received", "in_production", "ready", "shipped", "delivered", "paid"]

_store: dict | None = None


def _iso(d: date) -> str:
    return d.isoformat()


def _ts(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _build() -> dict:
    rnd = random.Random(_SEED)
    today = datetime.now(timezone.utc).date()

    # ── purchase orders ──────────────────────────────────────────────────
    pos, tracking, production = [], [], []
    for i in range(14):
        po = f"PO90{110 + i}"
        client = CLIENTS[i % len(CLIENTS)]
        age = rnd.randint(4, 45)
        po_date = today - timedelta(days=age)
        cargo = po_date + timedelta(days=rnd.randint(18, 32))
        stage = PO_STAGES[min(len(PO_STAGES) - 1, age // 8)]
        pos.append({
            "id": i + 1, "po_number": po, "client": client, "status": stage,
            "notes": "" if i % 3 else "Payment terms 30 days from B/L",
            "created_at": _ts(datetime.combine(po_date, datetime.min.time())),
            "updated_at": _ts(datetime.combine(po_date, datetime.min.time())),
        })

        # stage tracking: older POs are further along
        done_upto = min(len(STAGES), max(0, int(age / 45 * len(STAGES)) + rnd.randint(-2, 2)))
        stages = [{"stage": s, "owner": o, "done": n < done_upto}
                  for n, (s, o, _sla) in enumerate(STAGES)]
        pending = [s for s in stages if not s["done"]]
        import json as _json
        tracking.append({
            "id": i + 1, "po_number": po, "customer": client,
            "po_date": _iso(po_date), "cargo_ready": _iso(cargo),
            "stages_json": _json.dumps(stages),
            "stages_done": done_upto, "stages_total": len(STAGES),
            "current_stage": pending[0]["stage"] if pending else "",
            "current_owner": pending[0]["owner"] if pending else "",
            "track_status": ("closed" if not pending
                             else "not_started" if done_upto == 0 else "in_progress"),
            "synced_at": _ts(datetime.utcnow()),
        })

        # production sheet lines
        for line in range(rnd.randint(1, 3)):
            qty = rnd.choice([120000, 250000, 400000, 550000, 800000])
            pct = rnd.choice([0, 0.35, 0.6, 0.85, 1.0])
            done = int(qty * pct)
            ship = cargo - timedelta(days=rnd.randint(-6, 10))
            production.append({
                "id": len(production) + 1, "uid": f"{po}.{line + 1}",
                "po_number": po, "customer": client,
                "description": PRODUCTS[(i + line) % len(PRODUCTS)],
                "po_qty": qty, "done_qty": done, "pending_qty": qty - done,
                "ship_ready": _iso(ship), "priority": rnd.choice(["high", "medium"]),
                "prod_start": _iso(po_date + timedelta(days=3)),
                "sheet_status": "Complete" if pct == 1.0 else rnd.choice(
                    ["Running", "Running", "Running", "Hold"]),
                "synced_at": _ts(datetime.utcnow()),
            })

    # ── tasks ────────────────────────────────────────────────────────────
    tasks, emails = [], []
    tid = 0
    for day_back in range(28, -1, -1):
        d = today - timedelta(days=day_back)
        for _ in range(rnd.randint(1, 4)):
            tid += 1
            kind = rnd.choices(["client", "forwarder", "internal"], [6, 3, 2])[0]
            po = rnd.choice(pos)["po_number"]
            prod = rnd.choice(PRODUCTS)
            if kind == "client":
                who = rnd.choice(CLIENTS)
                text = rnd.choice(CLIENT_ASKS)
            elif kind == "forwarder":
                who = rnd.choice(FORWARDERS)
                text = rnd.choice(FORWARDER_ASKS)
            else:
                who = "Internal"
                text = rnd.choice(INTERNAL_ASKS)
            request = text.format(qty=rnd.choice([200, 350, 500, 750]),
                                  prod=prod, po=po, bank=rnd.choice(BANKS))
            # older tasks are more likely finished
            if day_back > 6:
                status = rnd.choices(["done", "in_progress", "open"], [7, 2, 2])[0]
            elif day_back > 1:
                status = rnd.choices(["done", "in_progress", "open"], [3, 3, 4])[0]
            else:
                status = rnd.choices(["done", "in_progress", "open"], [1, 2, 6])[0]
            created = datetime.combine(d, datetime.min.time()) + timedelta(
                hours=rnd.randint(8, 18), minutes=rnd.randint(0, 59))
            updated = created + timedelta(hours=rnd.randint(1, 40))
            if updated.date() > today:
                updated = datetime.combine(today, datetime.min.time()) + timedelta(hours=11)
            deadline = ""
            if rnd.random() < 0.45:
                deadline = _iso(d + timedelta(days=rnd.randint(2, 14)))
            t = {
                "id": tid, "client": who,
                "contact": f"{who.split()[0].lower()}@example.com",
                "channel": rnd.choices(["email", "whatsapp"], [8, 2])[0],
                "request": request,
                "department": rnd.choice(DEPTS_BY_KIND[kind]),
                "po_number": po if rnd.random() < 0.7 else "",
                "deadline": deadline,
                "priority": rnd.choices(["high", "normal", "low"], [3, 6, 1])[0],
                "source": "quoted from the original message",
                "status": status,
                "remark": "Confirmed and closed" if status == "done" else "",
                "done_by": "demo@thesolfactory.com" if status == "done" else "",
                "scheduled_for": "",
                "close_why": "", "close_quote": "", "close_conf": "", "close_at": "",
                "created_at": _ts(created), "updated_at": _ts(updated),
            }
            tasks.append(t)

    # a few internal (sheet-raised) tasks so the Internal tab is populated
    for r in production[:6]:
        if r["pending_qty"] <= 0:
            continue
        tid += 1
        tasks.append({
            "id": tid, "client": r["customer"], "contact": "", "channel": "sheet",
            "request": (f"Production at risk [{r['uid']}]: "
                        f"{r['pending_qty']:,} pcs pending, ship-ready "
                        f"{r['ship_ready']} — {r['description']}"),
            "department": "implementation", "po_number": r["po_number"],
            "deadline": r["ship_ready"], "priority": "high",
            "source": "production sheet sync", "status": "open", "remark": "",
            "done_by": "", "scheduled_for": "",
            "close_why": "", "close_quote": "", "close_conf": "", "close_at": "",
            "created_at": _ts(datetime.utcnow() - timedelta(hours=6)),
            "updated_at": _ts(datetime.utcnow() - timedelta(hours=6)),
        })

    # a couple of ready-to-close suggestions so that tab demonstrates itself
    open_ones = [t for t in tasks if t["status"] in ("open", "in_progress")]
    for t, (why, quote, conf) in zip(open_ones[:3], [
        ("You sent the documents and the client confirmed receipt",
         "Thanks — received everything, we will revert with the PO", "high"),
        ("Checklist was approved by the forwarder",
         "Checklist approved, please proceed with pickup", "high"),
        ("Client said thanks, but no document was actually attached",
         "thanks, noted", "medium"),
    ]):
        t["status"] = "in_progress"
        t["close_why"], t["close_quote"], t["close_conf"] = why, quote, conf
        t["close_at"] = _ts(datetime.utcnow() - timedelta(hours=2))

    # ── mail history ─────────────────────────────────────────────────────
    for n, t in enumerate(tasks[-60:], start=1):
        emails.append({
            "id": n, "gmail_id": f"demo{n}", "account": "sales@example.com",
            "direction": "incoming" if n % 4 else "outgoing",
            "sender": t["contact"] or "team@example.com",
            "subject": (t["po_number"] + " — " if t["po_number"] else "") + "follow-up",
            "snippet": t["request"][:120],
            "body": t["request"],
            "ts": t["created_at"], "processed": 1,
        })

    users = [
        {"id": 1, "email": "demo@thesolfactory.com", "department": "admin",
         "role": "admin", "active": 1, "created_at": _ts(datetime.utcnow())},
        {"id": 2, "email": "logistics.demo@example.com", "department": "logistics",
         "role": "member", "active": 1, "created_at": _ts(datetime.utcnow())},
        {"id": 3, "email": "production.demo@example.com", "department": "production",
         "role": "member", "active": 1, "created_at": _ts(datetime.utcnow())},
        {"id": 4, "email": "qc.demo@example.com", "department": "qc",
         "role": "member", "active": 1, "created_at": _ts(datetime.utcnow())},
    ]
    runs = [{"id": 1, "started_at": _ts(datetime.utcnow() - timedelta(minutes=42)),
             "finished_at": _ts(datetime.utcnow() - timedelta(minutes=40)),
             "wa_count": 3, "email_count": 27, "new_tasks": 5, "note": ""}]
    skipped = [{"id": i + 1, "sender": rnd.choice(CLIENTS),
                "reason": rnd.choice(["newsletter, no action", "thanks only",
                                      "already covered by open task",
                                      "automated notification"]),
                "created_at": _ts(datetime.utcnow() - timedelta(hours=i))}
               for i in range(12)]
    events = [{"id": 1, "level": "info", "source": "demo",
               "message": "demo dataset generated",
               "created_at": _ts(datetime.utcnow())}]

    return {"tasks": tasks, "emails": emails, "purchase_orders": pos,
            "production_rows": production, "tracking_rows": tracking,
            "users": users, "runs": runs, "skipped_msgs": skipped,
            "events": events, "wa_messages": []}


def store() -> dict:
    global _store
    if _store is None:
        _store = _build()
    return _store


def reset() -> None:
    """Throw away demo edits and regenerate."""
    global _store
    _store = None


# ── read API (mirrors app.db) ────────────────────────────────────────────

def open_tasks() -> list:
    ts = [t for t in store()["tasks"] if t["status"] in ("open", "in_progress")]
    return sorted(ts, key=lambda t: (t["client"], t["id"]))


def tasks_done_today(today_prefix: str) -> list:
    return sorted([t for t in store()["tasks"]
                   if t["status"] == "done"
                   and (t["updated_at"] or "").startswith(today_prefix)],
                  key=lambda t: t["updated_at"], reverse=True)


def all_tasks(limit: int = 500) -> list:
    return sorted(store()["tasks"], key=lambda t: -t["id"])[:limit]


def dashboard_data(today_prefix: str, want_archive: bool,
                   archive_limit: int = 60) -> dict:
    op = open_tasks()
    done = tasks_done_today(today_prefix)
    active = {t["id"] for t in op} | {t["id"] for t in done}
    arch = [t for t in all_tasks(10000) if t["id"] not in active]
    return {"open": op, "done_today": done,
            "archive": arch[:archive_limit] if want_archive else [],
            "n_archive": len(arch), "last_run": last_run()}


def last_run() -> dict | None:
    r = store()["runs"]
    return r[0] if r else None


def production_all() -> list:
    return sorted(store()["production_rows"],
                  key=lambda r: (r["ship_ready"] == "", r["ship_ready"], r["po_number"]))


def production_for_po(po_number: str) -> list:
    po = (po_number or "").strip().upper()
    return [r for r in store()["production_rows"] if r["po_number"].upper() == po]


def production_last_sync() -> str:
    rows = store()["production_rows"]
    return rows[0]["synced_at"] if rows else ""


def tracking_all() -> list:
    return sorted(store()["tracking_rows"], key=lambda r: (r["po_date"], r["po_number"]))


def tracking_for_po(po_number: str) -> dict | None:
    po = (po_number or "").strip().upper()
    return next((r for r in store()["tracking_rows"]
                 if r["po_number"].upper() == po), None)


def list_pos() -> list:
    out = []
    for p in store()["purchase_orders"]:
        q = dict(p)
        q["open_tasks"] = sum(1 for t in store()["tasks"]
                              if t["po_number"] == p["po_number"]
                              and t["status"] in ("open", "in_progress"))
        out.append(q)
    return out


def get_po(po_number: str) -> dict | None:
    po = (po_number or "").strip().upper()
    return next((p for p in store()["purchase_orders"]
                 if p["po_number"].upper() == po), None)


def tasks_for_po(po_number: str) -> list:
    po = (po_number or "").strip().upper()
    return [t for t in store()["tasks"] if (t["po_number"] or "").upper() == po]


def emails_mentioning(text: str, limit: int = 100) -> list:
    needle = (text or "").lower()
    return [e for e in store()["emails"]
            if needle in (e["subject"] + e["body"]).lower()][:limit]


def all_emails(limit: int = 500) -> list:
    return sorted(store()["emails"], key=lambda e: -e["id"])[:limit]


def all_wa_messages(limit: int = 500) -> list:
    return []


def skipped_since(iso_ts: str, limit: int = 200) -> list:
    return store()["skipped_msgs"][:limit]


def list_users() -> list:
    return store()["users"]


def ready_to_close(department: str = "") -> list:
    rows = [t for t in open_tasks() if t.get("close_at")]
    if department:
        rows = [t for t in rows if t.get("department") == department]
    return sorted(rows, key=lambda t: (0 if t["close_conf"] == "high" else 1))


def table_counts() -> dict:
    s = store()
    out = {k: len(v) for k, v in s.items()}
    out["tasks_max_id"] = max((t["id"] for t in s["tasks"]), default=0)
    out["missing_columns"] = "none (demo dataset)"
    return out


def pipeline_stats() -> dict:
    return {"emails": {"total": len(store()["emails"]), "queued": 0},
            "wa_messages": {"total": 0, "queued": 0}}


def team_pastdue() -> list:
    """Past-due stage tasks, same shape as sheets.team_pastdue()."""
    import json as _json
    today = datetime.now(timezone.utc).date()
    sla = {name: days for name, _o, days in STAGES}
    rows = []
    for r in tracking_all():
        if r["track_status"] == "closed" or not r["po_date"]:
            continue
        pod = date.fromisoformat(r["po_date"])
        for s in _json.loads(r["stages_json"]):
            if s["done"]:
                continue
            days = sla.get(s["stage"])
            if days is None:
                continue
            due = pod + timedelta(days=days)
            late = (today - due).days
            if late <= 0:
                continue
            rows.append({"owner": s["owner"], "stage": s["stage"],
                         "po_number": r["po_number"], "customer": r["customer"],
                         "po_date": r["po_date"], "due": due.isoformat(),
                         "days_late": late})
    rows.sort(key=lambda x: -x["days_late"])
    return rows


# ── write API: mutates the in-memory copy only ───────────────────────────

def _find(task_id: int) -> dict | None:
    return next((t for t in store()["tasks"] if t["id"] == task_id), None)


def set_task_status(task_id: int, status: str, remark: str | None = None,
                    done_by: str | None = None) -> None:
    t = _find(task_id)
    if not t:
        return
    t["status"] = status
    t["updated_at"] = _ts(datetime.utcnow())
    if remark is not None and remark.strip():
        t["remark"] = remark.strip()[:500]
    if done_by:
        t["done_by"] = done_by[:200]
    if status in ("done", "open"):
        t["close_why"] = t["close_quote"] = t["close_conf"] = t["close_at"] = ""


def set_task_department(task_id: int, department: str) -> None:
    t = _find(task_id)
    if t:
        t["department"] = department
        t["updated_at"] = _ts(datetime.utcnow())


def set_task_schedule(task_id: int, day_iso: str) -> None:
    t = _find(task_id)
    if t:
        t["scheduled_for"] = day_iso


def clear_close_suggestion(task_id: int) -> None:
    t = _find(task_id)
    if t:
        t["close_why"] = t["close_quote"] = t["close_conf"] = t["close_at"] = ""


def add_task(t: dict) -> None:
    s = store()
    new_id = max((x["id"] for x in s["tasks"]), default=0) + 1
    row = {"id": new_id, "client": t.get("client", "Demo client"),
           "contact": t.get("contact", ""), "channel": t.get("channel", "manual"),
           "request": t.get("request", ""), "department": t.get("department", "admin"),
           "po_number": t.get("po_number", ""), "deadline": t.get("deadline", ""),
           "priority": t.get("priority", "normal"), "source": t.get("source", ""),
           "status": "open", "remark": "", "done_by": "", "scheduled_for": "",
           "close_why": "", "close_quote": "", "close_conf": "", "close_at": "",
           "created_at": _ts(datetime.utcnow()), "updated_at": _ts(datetime.utcnow())}
    s["tasks"].append(row)


def update_po(po_number: str, *, status: str | None = None,
              notes: str | None = None) -> None:
    p = get_po(po_number)
    if not p:
        return
    if status:
        p["status"] = status
    if notes is not None:
        p["notes"] = notes
    p["updated_at"] = _ts(datetime.utcnow())


def dedupe_open_tasks() -> int:
    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    groups: dict = {}
    for t in open_tasks():
        groups.setdefault((norm(t["client"]), norm(t["request"]),
                           (t["po_number"] or "").upper()), []).append(t)
    closed = 0
    for dupes in groups.values():
        if len(dupes) < 2:
            continue
        dupes.sort(key=lambda t: (0 if t["status"] == "in_progress" else 1, t["id"]))
        for extra in dupes[1:]:
            set_task_status(extra["id"], "done",
                            remark=f"auto-merged: duplicate of task #{dupes[0]['id']}",
                            done_by="agent (dedup)")
            closed += 1
    return closed


# no-ops so demo clicks never reach the outside world
def log_event(*a, **k) -> None: ...
def record_run(*a, **k) -> None: ...
def suggest_close(*a, **k) -> None: ...
def reset_processed() -> int: return 0
def upsert_po(*a, **k) -> None: ...

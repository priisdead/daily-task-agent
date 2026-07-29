"""Daily task report PDF: every department's tasks for a chosen date.
A task appears on day D if it was created on/before D and was not already
completed before D — i.e. it was 'on the plate' that day. Tasks finished
on D itself are shown with a check mark."""
from datetime import date, datetime, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from . import config, db

_INK = colors.HexColor("#1B1D1F")
_MUTED = colors.HexColor("#5C6166")
_LINE = colors.HexColor("#D8D5D0")
_ACCENT = colors.HexColor("#14634E")
_RED = colors.HexColor("#B3261E")

_BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12,
                       textColor=_INK)
_SMALL = ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=10,
                        textColor=_MUTED)
_H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=18, leading=22,
                     textColor=_INK)
_H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14,
                     textColor=_ACCENT, spaceBefore=14)
_META = ParagraphStyle("meta", fontName="Helvetica", fontSize=9, leading=12,
                       textColor=_MUTED)


def _to_local_date(iso: str) -> date | None:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(config.TIMEZONE)).date()


def tasks_for_day(day: date) -> list:
    """All tasks that were on the plate on `day` (company timezone)."""
    out = []
    for t in db.all_tasks(limit=2000):
        created = _to_local_date(t.get("created_at") or "")
        updated = _to_local_date(t.get("updated_at") or "")
        if not created or created > day:
            continue
        if t.get("status") == "done" and updated and updated < day:
            continue  # was already finished before this day
        out.append(t)
    return out


def _status_label(t: dict, day: date) -> tuple[str, colors.Color]:
    updated = _to_local_date(t.get("updated_at") or "")
    if t.get("status") == "done":
        if updated == day:
            return "DONE ✓", _ACCENT
        return "open", _MUTED          # finished only after the report date
    if t.get("status") == "in_progress":
        return "in progress", _MUTED
    return "OPEN", _RED


def build_daily_pdf(day: date) -> bytes:
    tasks = tasks_for_day(day)
    by_dept: dict[str, list] = {}
    for t in tasks:
        by_dept.setdefault((t.get("department") or "unassigned"), []).append(t)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=18 * mm, bottomMargin=16 * mm,
        leftMargin=16 * mm, rightMargin=16 * mm,
        title=f"Daily Task Report {day.isoformat()}",
    )
    story = [
        Paragraph("SOLITUDE FLAME — DAILY TASK REPORT", _H1),
        Paragraph(day.strftime("%A, %d %B %Y"), _META),
        Spacer(1, 4 * mm),
    ]

    done_n = sum(1 for t in tasks
                 if t.get("status") == "done" and _to_local_date(t.get("updated_at") or "") == day)
    story.append(Paragraph(
        f"{len(tasks)} tasks on the plate &middot; {done_n} completed this day "
        f"&middot; {len(tasks) - done_n} carried forward", _META))
    story.append(Spacer(1, 2 * mm))

    order = [d for d in config.DEPARTMENTS if d in by_dept]
    if "unassigned" in by_dept:
        order.append("unassigned")

    for dname in order:
        rows = by_dept[dname]
        label = "QC" if dname == "qc" else dname.capitalize()
        story.append(Paragraph(f"{label} &middot; {len(rows)}", _H2))
        data = [[Paragraph("<b>Status</b>", _SMALL), Paragraph("<b>Client</b>", _SMALL),
                 Paragraph("<b>Task</b>", _SMALL), Paragraph("<b>Deadline</b>", _SMALL),
                 Paragraph("<b>Priority</b>", _SMALL)]]
        for t in rows:
            status, col = _status_label(t, day)
            style = ParagraphStyle(f"st{t.get('id')}", parent=_SMALL, textColor=col)
            text = (t.get("request") or "")[:220]
            if t.get("remark"):
                who = f" — {t['done_by']}" if t.get("done_by") else ""
                text += (f'<br/><font color="#14634E" size="8">Remark: '
                         f'{t["remark"][:200]}{who}</font>')
            data.append([
                Paragraph(status, style),
                Paragraph((t.get("client") or "")[:40], _BODY),
                Paragraph(text, _BODY),
                Paragraph((t.get("deadline") or "—")[:40], _SMALL),
                Paragraph(t.get("priority") or "normal", _SMALL),
            ])
        table = Table(data, colWidths=[22 * mm, 30 * mm, 82 * mm, 26 * mm, 18 * mm],
                      repeatRows=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, _LINE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, _LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(table)

    if not order:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("No tasks were on the plate on this date.", _BODY))

    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        f"Generated {datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%d %b %Y %H:%M')} "
        f"({config.TIMEZONE}) by Task Agent", _SMALL))
    doc.build(story)
    return buf.getvalue()


def build_team_pdf(rows: list, owner: str = "") -> bytes:
    """Past-due stage tasks (Team KRA view) as a PDF — whole team or one
    person. `rows` come from sheets.team_pastdue(), already worst-first."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=16 * mm, bottomMargin=16 * mm,
        leftMargin=15 * mm, rightMargin=15 * mm,
        title="Past-due tasks — Team KRA")
    day = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    story = [
        Paragraph("Past-due tasks — Team KRA", _H1),
        Paragraph(
            (f"{owner} · " if owner else "All teams · ")
            + f"{len(rows)} task{'s' if len(rows) != 1 else ''} past due"
            + (f" · worst {max(r['days_late'] for r in rows)} days late"
               if rows else "")
            + f" · {day.strftime('%d %b %Y')}", _META),
        Spacer(1, 5 * mm),
    ]
    if not rows:
        story.append(Paragraph(
            "No past-due tasks — every stage is inside its SLA.", _BODY))
    else:
        data = [[Paragraph(h, _SMALL) for h in
                 ("Owner", "Stage", "PO", "Customer", "Due", "Late by")]]
        for r in rows:
            late = Paragraph(
                f'<font color="#B3261E"><b>{r["days_late"]}d</b></font>', _BODY)
            data.append([
                Paragraph(r["owner"][:30], _BODY),
                Paragraph(r["stage"][:60], _BODY),
                Paragraph(r["po_number"][:20], _BODY),
                Paragraph((r["customer"] or "—")[:30], _SMALL),
                Paragraph(r["due"], _SMALL),
                late,
            ])
        table = Table(
            data, colWidths=[30 * mm, 52 * mm, 24 * mm, 34 * mm, 22 * mm, 16 * mm],
            repeatRows=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, _LINE),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, _LINE),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(table)
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        f"Due dates = PO Date + per-stage SLA days (same rules as the CXO "
        f"dashboard). Generated "
        f"{datetime.now(ZoneInfo(config.TIMEZONE)).strftime('%d %b %Y %H:%M')} "
        f"({config.TIMEZONE}) by Task Agent", _SMALL))
    doc.build(story)
    return buf.getvalue()

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import session_scope
from ..exports import readings_to_csv, render_pdf
from ..models import Reading
from ..util import get_monitor_or_404, parse_iso_ts, safe_filename

bp = Blueprint("data", __name__)
log = logging.getLogger("spot.data")


def _parse_range() -> tuple[datetime, datetime]:
    end_raw = request.args.get("end")
    start_raw = request.args.get("start")
    end = parse_iso_ts(end_raw) if end_raw else datetime.now(timezone.utc)
    if start_raw:
        start = parse_iso_ts(start_raw)
    else:
        minutes = int(request.args.get("minutes", "60"))
        start = end - timedelta(minutes=minutes)
    if start >= end:
        abort(400, description="start must be before end")
    return start, end


def _range_clause(mid: int, start: datetime, end: datetime):
    return (Reading.monitor_id == mid) & (Reading.ts >= start) & (Reading.ts <= end)


def _fetch_rows(s: Session, mid: int, start: datetime, end: datetime):
    return s.execute(
        select(Reading.ts, Reading.value, Reading.label)
        .where(_range_clause(mid, start, end))
        .order_by(Reading.ts.asc())
    ).all()


def _compute_summary(s: Session, mid: int, start: datetime, end: datetime) -> dict:
    where = _range_clause(mid, start, end)
    agg = s.execute(
        select(func.min(Reading.value), func.max(Reading.value),
               func.avg(Reading.value), func.count(Reading.value))
        .where(where)
    ).one()
    ev_count = s.execute(
        select(func.count(Reading.id))
        .where(where).where(Reading.label.is_not(None)).where(Reading.value.is_(None))
    ).scalar_one()
    min_row = s.execute(
        select(Reading.ts, Reading.value)
        .where(where).where(Reading.value.is_not(None))
        .order_by(Reading.value.asc()).limit(1)
    ).first()
    max_row = s.execute(
        select(Reading.ts, Reading.value)
        .where(where).where(Reading.value.is_not(None))
        .order_by(Reading.value.desc()).limit(1)
    ).first()
    return {
        "min": agg[0],
        "max": agg[1],
        "avg": float(agg[2]) if agg[2] is not None else None,
        "count": int(agg[3] or 0),
        "events": int(ev_count or 0),
        "min_at": min_row.ts.isoformat() if min_row else None,
        "max_at": max_row.ts.isoformat() if max_row else None,
    }


@bp.route("/monitor/<int:mid>/series")
def series(mid: int):
    start, end = _parse_range()
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        rows = _fetch_rows(s, mid, start, end)
    return jsonify({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "points":    [{"ts": r.ts.isoformat(), "value": r.value} for r in rows if r.value is not None],
        "events":    [{"ts": r.ts.isoformat(), "label": r.label} for r in rows if r.label and r.value is None],
        "annotated": [{"ts": r.ts.isoformat(), "value": r.value, "label": r.label}
                      for r in rows if r.label and r.value is not None],
    })


@bp.route("/monitor/<int:mid>/recent")
def recent(mid: int):
    seconds = int(request.args.get("seconds", "30"))
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=seconds)
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        rows = _fetch_rows(s, mid, start, end)
    return jsonify({
        "now": end.isoformat(),
        "start": start.isoformat(),
        "points": [{"ts": r.ts.isoformat(), "value": r.value} for r in rows if r.value is not None],
        "events": [{"ts": r.ts.isoformat(), "label": r.label} for r in rows if r.label and r.value is None],
    })


@bp.route("/monitor/<int:mid>/summary")
def summary(mid: int):
    start, end = _parse_range()
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        data = _compute_summary(s, mid, start, end)
    data["start"] = start.isoformat()
    data["end"] = end.isoformat()
    return jsonify(data)


@bp.route("/monitor/<int:mid>/export.csv")
def export_csv(mid: int):
    start, end = _parse_range()
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        rows = _fetch_rows(s, mid, start, end)
        name = m.name
    body = readings_to_csv([(r.ts, r.value, r.label) for r in rows])
    return _attachment(body, "text/csv", "csv", name, start, end)


@bp.route("/monitor/<int:mid>/export.pdf")
def export_pdf(mid: int):
    start, end = _parse_range()
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        rows = _fetch_rows(s, mid, start, end)
        summary_data = _compute_summary(s, mid, start, end)
        name, unit = m.name, m.unit
    points = [(r.ts, r.value) for r in rows if r.value is not None]
    events = [(r.ts, r.label) for r in rows if r.label and r.value is None]
    body = render_pdf(name, unit, start, end, points, events, summary_data)
    return _attachment(body, "application/pdf", "pdf", name, start, end)


def _attachment(body: bytes, mimetype: str, ext: str, name: str,
                start: datetime, end: datetime) -> Response:
    fname = (f"spot_{safe_filename(name)}_"
             f"{start.strftime('%Y%m%dT%H%M%SZ')}_"
             f"{end.strftime('%Y%m%dT%H%M%SZ')}.{ext}")
    return Response(body, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})

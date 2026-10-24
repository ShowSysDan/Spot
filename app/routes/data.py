from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from flask import Blueprint, Response, abort, jsonify, request, stream_with_context
from sqlalchemy import func, select, true

from ..alerts import active_alerts_by_monitor
from ..db import session_scope
from ..queries import (
    MAX_RANGE_SECONDS,
    PDF_MAX_POINTS,
    bucket_expr,
    collect_overlay,
    compute_summary,
    fetch_events,
    fetch_points,
    iso_points,
    range_clause,
)
from ..exports import iter_csv, render_overlay_pdf, render_pdf
from ..models import Monitor, Reading
from ..util import get_monitor_or_404, monitor_color, safe_filename, to_local
from .params import int_arg, max_points_arg, parse_ids, parse_range

bp = Blueprint("data", __name__)
log = logging.getLogger("spot.data")

# Rows fetched per round trip while streaming CSV exports.
CSV_BATCH = 5_000


# ---------------------------------------------------------------------------
# Single-monitor endpoints
# ---------------------------------------------------------------------------

@bp.route("/monitor/<int:mid>/series")
def series(mid: int):
    """Readings in [start, end]. Optional ?max_points=N downsamples long ranges."""
    start, end = parse_range()
    max_points = max_points_arg()
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        points, bucket = fetch_points(s, mid, start, end, max_points)
        events = fetch_events(s, mid, start, end)
    return jsonify({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bucket_seconds": bucket,
        "points": [{"ts": p["ts"].isoformat(), "value": p["value"],
                    **({"min": p["min"], "max": p["max"]} if bucket else {})}
                   for p in points],
        "events": iso_points(events),
        "annotated": [{"ts": p["ts"].isoformat(), "value": p["value"], "label": p["label"]}
                      for p in points if p.get("label")],
    })


@bp.route("/monitor/<int:mid>/recent")
def recent(mid: int):
    """The last ?seconds=N (default 30, max 366 days) ending now.

    Optional ?max_points=N downsamples; ?stats=1 adds min/max/avg/count for
    the whole window (computed in SQL, so exact even when bucketed).
    """
    seconds = int_arg("seconds", 30, 1, MAX_RANGE_SECONDS)
    max_points = max_points_arg()
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=seconds)
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        points, bucket = fetch_points(s, mid, start, end, max_points)
        events = fetch_events(s, mid, start, end)
        stats = compute_summary(s, mid, start, end) if request.args.get("stats") else None
    body = {
        "now": end.isoformat(),
        "start": start.isoformat(),
        "bucket_seconds": bucket,
        "points": [{"ts": p["ts"].isoformat(), "value": p["value"],
                    **({"min": p["min"], "max": p["max"]} if bucket else {})}
                   for p in points],
        "events": iso_points(events),
    }
    if stats is not None:
        body["stats"] = stats
    body["alerts"] = active_alerts_by_monitor().get(mid, [])
    return jsonify(body)


@bp.route("/monitor/<int:mid>/latest")
def latest(mid: int):
    """Most recent reading with a non-null value, regardless of how old it is.

    Returns null fields if the monitor has no value-bearing readings yet.
    """
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        row = s.execute(
            select(Reading.ts, Reading.value, Reading.label)
            .where(Reading.monitor_id == mid)
            .where(Reading.value.is_not(None))
            .order_by(Reading.ts.desc())
            .limit(1)
        ).first()
    if not row:
        return jsonify({"ts": None, "value": None, "label": None})
    return jsonify({
        "ts": row.ts.isoformat(),
        "value": row.value,
        "label": row.label,
    })


@bp.route("/monitor/<int:mid>/summary")
def summary(mid: int):
    start, end = parse_range()
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        data = compute_summary(s, mid, start, end)
    data["start"] = start.isoformat()
    data["end"] = end.isoformat()
    return jsonify(data)


# ---------------------------------------------------------------------------
# All-monitor live readout (home page tiles)
# ---------------------------------------------------------------------------

@bp.route("/latest")
def latest_all():
    """Latest value + a small sparkline for every monitor (or ?ids=1,2,3).

    One query for the latest readings (LATERAL join riding the
    (monitor_id, ts) index) and one for the sparklines, regardless of how
    many monitors exist. ?spark_minutes=N (default 60, max 1440) sets the
    sparkline window; it is drawn from up to 60 averaged buckets.
    """
    ids = parse_ids() if request.args.get("ids") else None
    spark_minutes = int_arg("spark_minutes", 60, 5, 1440)
    now = datetime.now(timezone.utc)
    spark_start = now - timedelta(minutes=spark_minutes)
    bucket = max(1, (spark_minutes * 60) // 60)

    last = (
        select(Reading.ts, Reading.value, Reading.label)
        .where(Reading.monitor_id == Monitor.id, Reading.value.is_not(None))
        .order_by(Reading.ts.desc())
        .limit(1)
        .correlate(Monitor)
        .lateral("last")
    )
    q = select(Monitor.id, last.c.ts, last.c.value, last.c.label).outerjoin(last, true())
    b = bucket_expr(bucket).label("b")
    spark_q = (
        select(Reading.monitor_id, b, func.avg(Reading.value))
        .where(Reading.ts >= spark_start, Reading.value.is_not(None))
        .group_by(Reading.monitor_id, b)
        .order_by(Reading.monitor_id, b)
    )
    if ids is not None:
        q = q.where(Monitor.id.in_(ids))
        spark_q = spark_q.where(Reading.monitor_id.in_(ids))

    with session_scope() as s:
        latest_rows = s.execute(q).all()
        spark_rows = s.execute(spark_q).all()
    alerts = active_alerts_by_monitor()

    sparks: dict[int, list] = {}
    for mid, bstart, avg in spark_rows:
        sparks.setdefault(mid, []).append(
            [int((float(bstart) + bucket / 2) * 1000), float(avg)])
    return jsonify({
        "now": now.isoformat(),
        "spark_start": spark_start.isoformat(),
        "monitors": [
            {"id": r[0],
             "ts": r[1].isoformat() if r[1] else None,
             "value": r[2],
             "label": r[3],
             "spark": sparks.get(r[0], []),
             "alerts": alerts.get(r[0], [])}
            for r in latest_rows
        ],
    })


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _stream_csv(stmt, include_monitor: bool, filename: str) -> Response:
    """Stream a CSV straight from a server-side cursor.

    Rows are fetched CSV_BATCH at a time and never all held in memory, so a
    year-long export costs the same RAM as a one-minute one.
    """
    def generate():
        with session_scope() as s:
            result = s.execute(stmt.execution_options(stream_results=True,
                                                      yield_per=CSV_BATCH))
            yield from iter_csv(result, include_monitor)

    return Response(stream_with_context(generate()), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.route("/monitor/<int:mid>/export.csv")
def export_csv(mid: int):
    start, end = parse_range()
    with session_scope() as s:
        name = get_monitor_or_404(s, mid).name
    stmt = (select(Reading.ts, Reading.value, Reading.label)
            .where(range_clause(mid, start, end))
            .order_by(Reading.ts.asc()))
    return _stream_csv(stmt, False, _filename(name, start, end, "csv"))


@bp.route("/overlay")
def overlay():
    """Series for multiple monitors over a range. ?ids=1,2,3&start=...&end=...

    Optional ?max_points=N downsamples each series; ?summary=1 adds exact
    min/max/avg/count per monitor.
    """
    ids = parse_ids()
    if not ids:
        return jsonify({"monitors": [], "start": None, "end": None})
    start, end = parse_range()
    with session_scope() as s:
        out = collect_overlay(s, ids, start, end, max_points_arg(),
                              with_summary=bool(request.args.get("summary")), extended=True)
    for m in out:
        m["points"] = [{"ts": p["ts"].isoformat(), "value": p["value"]} for p in m["points"]]
    return jsonify({"start": start.isoformat(), "end": end.isoformat(), "monitors": out})


@bp.route("/overlay/export.pdf")
def overlay_export_pdf():
    ids = parse_ids()
    if not ids:
        abort(400, description="ids required")
    start, end = parse_range()
    with session_scope() as s:
        monitors = collect_overlay(s, ids, start, end, PDF_MAX_POINTS,
                                   with_summary=True, extended=True)
    body = render_overlay_pdf(start, end, monitors)
    fname = _filename("report", start, end, "pdf")
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@bp.route("/overlay/export.csv")
def overlay_export_csv():
    """One combined CSV (long format: timestamp, monitor, unit, value, label)."""
    ids = parse_ids()
    if not ids:
        abort(400, description="ids required")
    start, end = parse_range()
    stmt = (select(Reading.ts, Monitor.name, Monitor.unit, Reading.value, Reading.label)
            .join(Monitor, Monitor.id == Reading.monitor_id)
            .where(Reading.monitor_id.in_(ids))
            .where(Reading.ts >= start, Reading.ts <= end)
            .order_by(Reading.ts.asc(), Monitor.name.asc()))
    fname = _filename("report", start, end, "csv")
    return _stream_csv(stmt, True, fname)


@bp.route("/monitor/<int:mid>/export.pdf")
def export_pdf(mid: int):
    start, end = parse_range()
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        points, bucket = fetch_points(s, mid, start, end, PDF_MAX_POINTS)
        events = fetch_events(s, mid, start, end, limit=200)
        summary_data = compute_summary(s, mid, start, end)
        name, unit, color = m.name, m.unit, monitor_color(m.id, m.color)
    body = render_pdf(name, unit, start, end, points,
                      [(e["ts"], e["label"]) for e in events], summary_data, color=color)
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{_filename(name, start, end, "pdf")}"'})


def _filename(name: str, start: datetime, end: datetime, ext: str) -> str:
    """spot_<name>_<start>_<end>.<ext>, times in the display zone (no 'Z')."""
    fmt = "%Y%m%dT%H%M%S"
    return (f"spot_{safe_filename(name)}_"
            f"{to_local(start).strftime(fmt)}_{to_local(end).strftime(fmt)}.{ext}")

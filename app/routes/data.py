from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from flask import Blueprint, Response, abort, jsonify, request, stream_with_context
from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from ..db import session_scope
from ..exports import iter_csv, render_overlay_pdf, render_pdf
from ..models import Monitor, Reading
from ..util import get_monitor_or_404, parse_iso_ts, safe_filename

bp = Blueprint("data", __name__)
log = logging.getLogger("spot.data")

# Longest window any endpoint will serve in one request. Bounds query cost and
# response size; anything longer should be exported in pieces.
MAX_RANGE_SECONDS = 366 * 86400
# Upper bound on ?max_points= so a client can't ask for an unbounded series.
MAX_POINTS_CAP = 10_000
# Points used to draw PDF charts. Server-side bucketing keeps a year-long
# report from loading millions of rows into memory just to draw a line.
PDF_MAX_POINTS = 4_000
# Event markers returned per series request (most recent kept).
MAX_EVENTS = 500
# Rows fetched per round trip while streaming CSV exports.
CSV_BATCH = 5_000


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        abort(400, description=f"{name} must be an integer")
    return max(lo, min(hi, v))


def _ts_arg(name: str) -> datetime | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return parse_iso_ts(raw)
    except ValueError:
        abort(400, description=f"{name} must be an ISO-8601 timestamp")


def _parse_range() -> tuple[datetime, datetime]:
    end = _ts_arg("end") or datetime.now(timezone.utc)
    start = _ts_arg("start")
    if start is None:
        minutes = _int_arg("minutes", 60, 1, MAX_RANGE_SECONDS // 60)
        start = end - timedelta(minutes=minutes)
    if start >= end:
        abort(400, description="start must be before end")
    if (end - start).total_seconds() > MAX_RANGE_SECONDS:
        abort(400, description="range too long (max 366 days)")
    return start, end


def _max_points_arg() -> int | None:
    """Optional ?max_points=N. When set, series longer than N are bucketed."""
    if not request.args.get("max_points"):
        return None
    return _int_arg("max_points", MAX_POINTS_CAP, 10, MAX_POINTS_CAP)


def _parse_ids() -> list[int]:
    ids_raw = request.args.get("ids", "")
    try:
        ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        abort(400, description="ids must be comma-separated integers")
    # De-duplicate while keeping order; cap to keep one request bounded.
    return list(dict.fromkeys(ids))[:50]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _range_clause(mid: int, start: datetime, end: datetime):
    return (Reading.monitor_id == mid) & (Reading.ts >= start) & (Reading.ts <= end)


def _fetch_rows(s: Session, mid: int, start: datetime, end: datetime):
    return s.execute(
        select(Reading.ts, Reading.value, Reading.label)
        .where(_range_clause(mid, start, end))
        .order_by(Reading.ts.asc())
    ).all()


def _bucket_expr(bucket_seconds: int):
    """Floor each reading's timestamp to the start of a bucket (epoch seconds)."""
    return func.floor(func.extract("epoch", Reading.ts) / bucket_seconds) * bucket_seconds


def _fetch_points(s: Session, mid: int, start: datetime, end: datetime,
                  max_points: int | None) -> tuple[list[dict], int | None]:
    """Value-bearing readings in range, downsampled when there are too many.

    Returns (points, bucket_seconds). Raw points are {ts, value, label}. When
    the range holds more than `max_points` readings they are averaged into
    fixed-width time buckets instead: {ts (bucket midpoint), value (avg),
    min, max, n}, and bucket_seconds says how wide each bucket is.
    """
    where = _range_clause(mid, start, end) & Reading.value.is_not(None)
    if max_points:
        n = s.execute(select(func.count()).select_from(Reading).where(where)).scalar_one()
        if n > max_points:
            span = (end - start).total_seconds()
            bucket = max(1, math.ceil(span / max_points))
            b = _bucket_expr(bucket).label("b")
            rows = s.execute(
                select(b, func.avg(Reading.value), func.min(Reading.value),
                       func.max(Reading.value), func.count())
                .where(where).group_by(b).order_by(b)
            ).all()
            half = bucket / 2
            return [
                {"ts": datetime.fromtimestamp(float(r[0]) + half, tz=timezone.utc),
                 "value": float(r[1]), "min": float(r[2]), "max": float(r[3]),
                 "n": int(r[4])}
                for r in rows
            ], bucket
    rows = s.execute(
        select(Reading.ts, Reading.value, Reading.label)
        .where(where).order_by(Reading.ts.asc())
    ).all()
    return [{"ts": r.ts, "value": r.value, "label": r.label} for r in rows], None


def _fetch_events(s: Session, mid: int, start: datetime, end: datetime,
                  limit: int = MAX_EVENTS) -> list[dict]:
    """Label-only event markers in range (newest `limit`, returned oldest-first)."""
    rows = s.execute(
        select(Reading.ts, Reading.label)
        .where(_range_clause(mid, start, end))
        .where(Reading.label.is_not(None)).where(Reading.value.is_(None))
        .order_by(Reading.ts.desc()).limit(limit)
    ).all()
    return [{"ts": r.ts, "label": r.label} for r in reversed(rows)]


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


def _iso_points(points: list[dict]) -> list[dict]:
    return [{**p, "ts": p["ts"].isoformat()} for p in points]


# ---------------------------------------------------------------------------
# Single-monitor endpoints
# ---------------------------------------------------------------------------

@bp.route("/monitor/<int:mid>/series")
def series(mid: int):
    """Readings in [start, end]. Optional ?max_points=N downsamples long ranges."""
    start, end = _parse_range()
    max_points = _max_points_arg()
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        points, bucket = _fetch_points(s, mid, start, end, max_points)
        events = _fetch_events(s, mid, start, end)
    return jsonify({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bucket_seconds": bucket,
        "points": [{"ts": p["ts"].isoformat(), "value": p["value"],
                    **({"min": p["min"], "max": p["max"]} if bucket else {})}
                   for p in points],
        "events": _iso_points(events),
        "annotated": [{"ts": p["ts"].isoformat(), "value": p["value"], "label": p["label"]}
                      for p in points if p.get("label")],
    })


@bp.route("/monitor/<int:mid>/recent")
def recent(mid: int):
    """The last ?seconds=N (default 30, max 366 days) ending now.

    Optional ?max_points=N downsamples; ?stats=1 adds min/max/avg/count for
    the whole window (computed in SQL, so exact even when bucketed).
    """
    seconds = _int_arg("seconds", 30, 1, MAX_RANGE_SECONDS)
    max_points = _max_points_arg()
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=seconds)
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        points, bucket = _fetch_points(s, mid, start, end, max_points)
        events = _fetch_events(s, mid, start, end)
        stats = _compute_summary(s, mid, start, end) if request.args.get("stats") else None
    body = {
        "now": end.isoformat(),
        "start": start.isoformat(),
        "bucket_seconds": bucket,
        "points": [{"ts": p["ts"].isoformat(), "value": p["value"],
                    **({"min": p["min"], "max": p["max"]} if bucket else {})}
                   for p in points],
        "events": _iso_points(events),
    }
    if stats is not None:
        body["stats"] = stats
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
    start, end = _parse_range()
    with session_scope() as s:
        get_monitor_or_404(s, mid)
        data = _compute_summary(s, mid, start, end)
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
    ids = _parse_ids() if request.args.get("ids") else None
    spark_minutes = _int_arg("spark_minutes", 60, 5, 1440)
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
    b = _bucket_expr(bucket).label("b")
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
             "spark": sparks.get(r[0], [])}
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
    start, end = _parse_range()
    with session_scope() as s:
        name = get_monitor_or_404(s, mid).name
    stmt = (select(Reading.ts, Reading.value, Reading.label)
            .where(_range_clause(mid, start, end))
            .order_by(Reading.ts.asc()))
    return _stream_csv(stmt, False, _filename(name, start, end, "csv"))


def _collect_overlay(s, ids: list[int], start: datetime, end: datetime,
                     max_points: int | None, with_summary: bool) -> list[dict]:
    monitors = s.query(Monitor).filter(Monitor.id.in_(ids)).order_by(Monitor.name).all()
    out = []
    for m in monitors:
        points, bucket = _fetch_points(s, m.id, start, end, max_points)
        item = {
            "id": m.id,
            "name": m.name,
            "unit": m.unit,
            "bucket_seconds": bucket,
            "points": points,
        }
        if with_summary:
            item["summary"] = _compute_summary(s, m.id, start, end)
        out.append(item)
    return out


@bp.route("/overlay")
def overlay():
    """Series for multiple monitors over a range. ?ids=1,2,3&start=...&end=...

    Optional ?max_points=N downsamples each series; ?summary=1 adds exact
    min/max/avg/count per monitor.
    """
    ids = _parse_ids()
    if not ids:
        return jsonify({"monitors": [], "start": None, "end": None})
    start, end = _parse_range()
    with session_scope() as s:
        out = _collect_overlay(s, ids, start, end, _max_points_arg(),
                               with_summary=bool(request.args.get("summary")))
    for m in out:
        m["points"] = [{"ts": p["ts"].isoformat(), "value": p["value"]} for p in m["points"]]
    return jsonify({"start": start.isoformat(), "end": end.isoformat(), "monitors": out})


@bp.route("/overlay/export.pdf")
def overlay_export_pdf():
    ids = _parse_ids()
    if not ids:
        abort(400, description="ids required")
    start, end = _parse_range()
    with session_scope() as s:
        monitors = _collect_overlay(s, ids, start, end, PDF_MAX_POINTS, with_summary=True)
    body = render_overlay_pdf(start, end, monitors)
    fname = (f"spot_report_{start.strftime('%Y%m%dT%H%M%SZ')}_"
             f"{end.strftime('%Y%m%dT%H%M%SZ')}.pdf")
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@bp.route("/overlay/export.csv")
def overlay_export_csv():
    """One combined CSV (long format: timestamp, monitor, unit, value, label)."""
    ids = _parse_ids()
    if not ids:
        abort(400, description="ids required")
    start, end = _parse_range()
    stmt = (select(Reading.ts, Monitor.name, Monitor.unit, Reading.value, Reading.label)
            .join(Monitor, Monitor.id == Reading.monitor_id)
            .where(Reading.monitor_id.in_(ids))
            .where(Reading.ts >= start, Reading.ts <= end)
            .order_by(Reading.ts.asc(), Monitor.name.asc()))
    fname = (f"spot_report_{start.strftime('%Y%m%dT%H%M%SZ')}_"
             f"{end.strftime('%Y%m%dT%H%M%SZ')}.csv")
    return _stream_csv(stmt, True, fname)


@bp.route("/monitor/<int:mid>/export.pdf")
def export_pdf(mid: int):
    start, end = _parse_range()
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        points, bucket = _fetch_points(s, mid, start, end, PDF_MAX_POINTS)
        events = _fetch_events(s, mid, start, end, limit=200)
        summary_data = _compute_summary(s, mid, start, end)
        name, unit = m.name, m.unit
    body = render_pdf(name, unit, start, end, points,
                      [(e["ts"], e["label"]) for e in events], summary_data)
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{_filename(name, start, end, "pdf")}"'})


def _filename(name: str, start: datetime, end: datetime, ext: str) -> str:
    return (f"spot_{safe_filename(name)}_"
            f"{start.strftime('%Y%m%dT%H%M%SZ')}_"
            f"{end.strftime('%Y%m%dT%H%M%SZ')}.{ext}")

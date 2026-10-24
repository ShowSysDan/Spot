"""Reading queries shared by the web data endpoints, the external API and
the background report jobs: ranged points (with downsampling), events,
summary statistics and multi-monitor collections.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from sqlalchemy import Float, cast, func, literal_column, select
from sqlalchemy.orm import Session

from .models import Monitor, Reading
from .util import iso_local, monitor_color

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


def range_clause(mid: int, start: datetime, end: datetime):
    return (Reading.monitor_id == mid) & (Reading.ts >= start) & (Reading.ts <= end)


def bucket_expr(bucket_seconds: int):
    """Floor each reading's timestamp to the start of a bucket (epoch seconds)."""
    # extract(epoch) is NUMERIC on PostgreSQL 14+, whose arithmetic is several
    # times slower than float; casting first matters on multi-million-row ranges.
    epoch = cast(func.extract("epoch", Reading.ts), Float)
    return func.floor(epoch / bucket_seconds) * bucket_seconds


def fetch_points(s: Session, mid: int, start: datetime, end: datetime,
                 max_points: int | None) -> tuple[list[dict], int | None]:
    """Value-bearing readings in range, downsampled when there are too many.

    Returns (points, bucket_seconds). Raw points are {ts, value, label}. When
    the range holds more than `max_points` readings they are averaged into
    fixed-width time buckets instead: {ts (bucket midpoint), value (avg),
    min, max, n}, and bucket_seconds says how wide each bucket is.
    """
    where = range_clause(mid, start, end) & Reading.value.is_not(None)
    if max_points:
        # Only "more than max_points or not?" matters, so stop counting there
        # instead of counting millions of rows.
        probe = select(Reading.id).where(where).limit(max_points + 1).subquery()
        n = s.execute(select(func.count()).select_from(probe)).scalar_one()
        if n > max_points:
            span = (end - start).total_seconds()
            bucket = max(1, math.ceil(span / max_points))
            b = bucket_expr(bucket).label("b")
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


def fetch_events(s: Session, mid: int, start: datetime, end: datetime,
                  limit: int = MAX_EVENTS) -> list[dict]:
    """Label-only event markers in range (newest `limit`, returned oldest-first)."""
    rows = s.execute(
        select(Reading.ts, Reading.label)
        .where(range_clause(mid, start, end))
        .where(Reading.label.is_not(None)).where(Reading.value.is_(None))
        .order_by(Reading.ts.desc()).limit(limit)
    ).all()
    return [{"ts": r.ts, "label": r.label} for r in reversed(rows)]


def is_sound_unit(unit: str | None) -> bool:
    """dB, dBA, dBC, dB SPL… — units where Leq / L10 / L90 are meaningful."""
    return (unit or "").strip().lower().startswith("db")


def compute_summary(s: Session, mid: int, start: datetime, end: datetime,
                    extended: bool = False, unit: str | None = None) -> dict:
    """min / max / avg / count / events (+ when they first happened) for a range.

    With `extended`, also the 10th / 50th / 90th percentiles, and for sound
    units (`is_sound_unit`) the acoustic figures:

      * leq — energy-equivalent level, 10·log10(mean(10^(L/10))). Averaging
        dB values arithmetically understates loudness; Leq is the standard
        "average level" for noise reports.
      * l10 / l50 / l90 — level exceeded 10 / 50 / 90 % of the time
        (= the 90th / 50th / 10th percentile).
    """
    where = range_clause(mid, start, end)
    sound = extended and is_sound_unit(unit)
    # Everything in ONE pass over the range (plus one sort for the
    # percentiles) instead of a query per figure — this is what keeps a
    # 30-day report on a 1 Hz meter (~2.6 M readings) responsive.
    cols = [
        func.min(Reading.value), func.max(Reading.value),
        func.avg(Reading.value), func.count(Reading.value),
        func.count().filter(Reading.label.is_not(None) & Reading.value.is_(None)),
    ]
    if extended:
        # One ordered-set aggregate returning all three percentiles shares a
        # single sort.
        cols.append(func.percentile_cont(literal_column("ARRAY[0.1, 0.5, 0.9]::float8[]"))
                    .within_group(Reading.value.asc()))
    if sound:
        # Mean energy for Leq. Clamped to 300 dB so power() can't overflow
        # on a garbage reading.
        cols.append(func.avg(func.power(10.0, func.least(Reading.value, 300.0) / 10.0)))
    row = s.execute(select(*cols).where(where)).one()
    vmin, vmax, vavg, count, events = row[:5]
    count = int(count or 0)

    def first_ts(value):
        # Time of the extreme value: a forward walk of the (monitor_id, ts)
        # index that stops at the first match — no sort of the range.
        return s.execute(
            select(Reading.ts).where(where, Reading.value == value)
            .order_by(Reading.ts.asc()).limit(1)
        ).scalar() if value is not None else None

    out = {
        "min": vmin,
        "max": vmax,
        "avg": float(vavg) if vavg is not None else None,
        "count": count,
        "events": int(events or 0),
        "min_at": iso_local(first_ts(vmin)),
        "max_at": iso_local(first_ts(vmax)),
    }
    if extended:
        p10, p50, p90 = (row[5] or (None, None, None)) if count else (None, None, None)
        out.update({"p10": p10, "p50": p50, "p90": p90})
        if sound:
            energy = row[6] if count else None
            out.update({
                "leq": 10 * math.log10(energy) if energy and energy > 0 else None,
                "l10": p90, "l50": p50, "l90": p10,
            })
    return out


def iso_points(points: list[dict]) -> list[dict]:
    return [{**p, "ts": p["ts"].isoformat()} for p in points]


def collect_overlay(s, ids: list[int], start: datetime, end: datetime,
                    max_points: int | None, with_summary: bool,
                    extended: bool = False,
                    summary_range: tuple[datetime, datetime] | None = None) -> list[dict]:
    """Points (+ optional summary) for several monitors, ordered by name.

    `summary_range` computes the summary over a different (narrower) range
    than the points — used to chart context around a detected show while
    reporting statistics for the show itself.
    """
    s_start, s_end = summary_range or (start, end)
    monitors = s.query(Monitor).filter(Monitor.id.in_(ids)).order_by(Monitor.name).all()
    out = []
    for m in monitors:
        points, bucket = fetch_points(s, m.id, start, end, max_points)
        item = {
            "id": m.id,
            "name": m.name,
            "unit": m.unit,
            "color": monitor_color(m.id, m.color),
            "bucket_seconds": bucket,
            "points": points,
        }
        if with_summary:
            item["summary"] = compute_summary(s, m.id, s_start, s_end,
                                              extended=extended, unit=m.unit)
        out.append(item)
    return out

"""Find when a performance actually started and ended from its sound levels.

Box-office times are approximate: a 21:00 curtain might really go up at
20:58 or 21:07, and the show might come down ten minutes late. Given a
scheduled range, `detect_bounds` looks for

  * the start: the moment within ±window of the scheduled start where the
    level steps **up** the most, and
  * the end:   the moment within ±window of the scheduled end where the
    level steps **down** the most,

where a "step" compares the **median** level in the `HOLD` seconds before a
moment with the median in the `HOLD` seconds after it. Medians over two
minutes ignore a single shout, clap or door slam; it takes a sustained
change. A step smaller than `min_step` (default 6 dB) is not trusted, and
the scheduled time is kept instead — the result always says which happened.

Readings are first averaged into `BUCKET`-second buckets, so the cost is
the same for a 1 Hz meter as for a 10 Hz one: a ±15 min search is a few
hundred buckets.

Known limits (also in docs/API.md): a show that opens quietly after loud
pre-show music has no upward step, and a show that ends with the house
music getting louder has no downward step — both fall back to the
scheduled time. Intermissions are not searched (only the edges are).
"""
from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Monitor, Reading
from .queries import bucket_expr, is_sound_unit
from .util import iso_local

BUCKET = 10                # seconds per averaged bucket
HOLD = 120                 # seconds compared on each side of a candidate moment
MIN_BUCKETS_PER_SIDE = 6   # need at least half the hold window to have data
DEFAULT_WINDOW_MINUTES = 15
MAX_WINDOW_MINUTES = 60
DEFAULT_MIN_STEP = 6.0     # dB


@dataclass
class DetectOptions:
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    min_step: float = DEFAULT_MIN_STEP
    monitor_id: int | None = None   # reference monitor; default: first dB monitor


def pick_reference(monitors: list[Monitor], wanted: int | None) -> Monitor | None:
    """The monitor whose levels decide the times: `wanted` if given, else the
    first sound (dB) monitor in the list (in the order given)."""
    if wanted is not None:
        return next((m for m in monitors if m.id == wanted), None)
    return next((m for m in monitors if is_sound_unit(m.unit)), None)


def _buckets(s: Session, monitor_id: int, lo: datetime, hi: datetime) -> tuple[list[float], list[float]]:
    b = bucket_expr(BUCKET).label("b")
    rows = s.execute(
        select(b, func.avg(Reading.value))
        .where(Reading.monitor_id == monitor_id, Reading.value.is_not(None),
               Reading.ts >= lo, Reading.ts <= hi)
        .group_by(b).order_by(b)
    ).all()
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows]


def _best_step(times: list[float], vals: list[float], c_lo: float, c_hi: float,
               direction: int, target: float) -> tuple[float | None, float | None, float | None]:
    """Candidate time in [c_lo, c_hi] with the largest `direction` step.

    Returns (time, step, midpoint level between the two sides). Ties go to
    the candidate nearest `target`.
    """
    best_t, best_score, best_mid = None, None, None
    i0 = bisect.bisect_left(times, c_lo)
    i1 = bisect.bisect_right(times, c_hi)
    for i in range(i0, i1):
        t = times[i]
        a = bisect.bisect_left(times, t - HOLD)
        z = bisect.bisect_left(times, t + HOLD)
        before, after = vals[a:i], vals[i:z]
        if len(before) < MIN_BUCKETS_PER_SIDE or len(after) < MIN_BUCKETS_PER_SIDE:
            continue
        mb, ma = statistics.median(before), statistics.median(after)
        score = direction * (ma - mb)
        if (best_score is None or score > best_score + 1e-9 or
                (abs(score - best_score) <= 1e-9 and abs(t - target) < abs(best_t - target))):
            best_t, best_score, best_mid = t, score, (ma + mb) / 2
    return best_t, best_score, best_mid


REFINE_SPAN = 3 * BUCKET   # raw seconds examined either side of a coarse edge
REFINE_RUN = 5             # rolling-median length used when refining


def _refine(s: Session, monitor_id: int, t: float, level: float, rising: bool) -> datetime:
    """Snap a bucket-resolution edge to a raw reading.

    Start (rising): the first reading that is itself at or above `level` and
    from which the median of the next REFINE_RUN readings is too. End
    (falling): the last reading at or above `level` whose preceding
    REFINE_RUN readings also have a median at or above it. Falls back to the coarse time if nothing qualifies.
    """
    coarse = datetime.fromtimestamp(t, tz=timezone.utc)
    span = timedelta(seconds=REFINE_SPAN)
    rows = s.execute(
        select(Reading.ts, Reading.value)
        .where(Reading.monitor_id == monitor_id, Reading.value.is_not(None),
               Reading.ts >= coarse - span, Reading.ts <= coarse + span)
        .order_by(Reading.ts)
    ).all()
    vals = [r.value for r in rows]
    if rising:
        for i in range(len(rows) - REFINE_RUN + 1):
            if vals[i] >= level and statistics.median(vals[i:i + REFINE_RUN]) >= level:
                return rows[i].ts
    else:
        for i in range(len(rows) - 1, REFINE_RUN - 2, -1):
            if vals[i] >= level and statistics.median(vals[i - REFINE_RUN + 1:i + 1]) >= level:
                return rows[i].ts
    return coarse


def detect_bounds(s: Session, monitor: Monitor, start: datetime, end: datetime,
                  opts: DetectOptions) -> dict:
    """Detect actual start/end near a scheduled [start, end] on `monitor`.

    Always returns a result; `start_detected` / `end_detected` say whether
    each edge came from the data or fell back to the schedule.
    """
    window = timedelta(minutes=opts.window_minutes)
    lo = start - window - timedelta(seconds=HOLD)
    hi = end + window + timedelta(seconds=HOLD)
    times, vals = _buckets(s, monitor.id, lo, hi)

    s_ep, e_ep = start.timestamp(), end.timestamp()
    w = window.total_seconds()
    mid = (s_ep + e_ep) / 2
    # Never let the two searches overlap on a short show.
    t_start, step_start, lvl_start = _best_step(times, vals, s_ep - w, min(s_ep + w, mid), +1, s_ep)
    t_end, step_end, lvl_end = _best_step(times, vals, max(e_ep - w, mid), e_ep + w, -1, e_ep)

    start_ok = step_start is not None and step_start >= opts.min_step
    end_ok = step_end is not None and step_end >= opts.min_step
    real_start = _refine(s, monitor.id, t_start, lvl_start, rising=True) if start_ok else start
    real_end = _refine(s, monitor.id, t_end, lvl_end, rising=False) if end_ok else end

    def why(ok: bool, step: float | None, kind: str) -> str:
        if ok:
            return f"{'rise' if kind == 'start' else 'drop'} of {step:.1f} {monitor.unit}"
        if step is None:
            return "not enough readings near the scheduled time"
        return (f"largest {'rise' if kind == 'start' else 'drop'} was {max(step, 0):.1f} "
                f"{monitor.unit} (needs {opts.min_step:g}); using the scheduled time")

    return {
        "monitor_id": monitor.id,
        "monitor_name": monitor.name,
        "window_minutes": opts.window_minutes,
        "min_step": opts.min_step,
        "scheduled_start": iso_local(start),
        "scheduled_end": iso_local(end),
        "start": iso_local(real_start),
        "end": iso_local(real_end),
        "start_detected": start_ok,
        "end_detected": end_ok,
        "start_step": round(step_start, 2) if step_start is not None else None,
        "end_step": round(step_end, 2) if step_end is not None else None,
        "start_offset_seconds": round((real_start - start).total_seconds()),
        "end_offset_seconds": round((real_end - end).total_seconds()),
        "start_reason": why(start_ok, step_start, "start"),
        "end_reason": why(end_ok, step_end, "end"),
        # Internal: the datetimes, for callers that go on to query with them.
        "_start": real_start,
        "_end": real_end,
    }


def resolve_range(s: Session, ids: list[int], start: datetime, end: datetime,
                  opts: DetectOptions | None) -> tuple[datetime, datetime, dict | None]:
    """The range to report on: detected if `opts` is given (and a reference
    monitor exists), else the requested one. Third value: the detection
    result for display (without internal keys), or None."""
    if opts is None:
        return start, end, None
    found = {m.id: m for m in s.scalars(select(Monitor).where(Monitor.id.in_(ids)))}
    monitors = [found[i] for i in ids if i in found]  # keep the caller's order
    if opts.monitor_id is not None:
        # The reference may be any monitor — e.g. a stage mic decides the
        # times for a report that only covers the house meters.
        ref = found.get(opts.monitor_id) or s.get(Monitor, opts.monitor_id)
        reason = f"reference monitor {opts.monitor_id} does not exist"
    else:
        ref = pick_reference(monitors, None)
        reason = "no sound (dB) monitor to detect from"
    if ref is None:
        return start, end, {
            "start_detected": False, "end_detected": False,
            "scheduled_start": iso_local(start), "scheduled_end": iso_local(end),
            "start": iso_local(start), "end": iso_local(end),
            "start_reason": reason,
            "end_reason": reason,
            "window_minutes": opts.window_minutes, "min_step": opts.min_step,
            "monitor_id": None, "monitor_name": None,
            "start_step": None, "end_step": None,
            "start_offset_seconds": 0, "end_offset_seconds": 0,
        }
    det = detect_bounds(s, ref, start, end, opts)
    real_start, real_end = det.pop("_start"), det.pop("_end")
    return real_start, real_end, det


def pdf_meta(det: dict | None) -> list[tuple[str, str]]:
    """Metadata-line entries for a report whose range was detected."""
    if not det:
        return []
    return [("Scheduled", f"{det['scheduled_start'][11:16]}–{det['scheduled_end'][11:16]}"),
            ("Detected", describe(det).removeprefix("Actual "))]


def describe(det: dict | None) -> str | None:
    """One line for PDFs/e-mails, e.g. 'Actual 21:04–23:41 (start +4 min, end +11 min)'."""
    if not det:
        return None

    def part(kind: str) -> str:
        if not det[f"{kind}_detected"]:
            return f"{kind} as scheduled"
        mins = round(det[f"{kind}_offset_seconds"] / 60)
        return f"{kind} {'+' if mins >= 0 else '−'}{abs(mins)} min"

    return (f"Actual {det['start'][11:16]}–{det['end'][11:16]} "
            f"({part('start')}, {part('end')}; from {det['monitor_name'] or '—'})")

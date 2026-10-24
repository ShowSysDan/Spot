"""Query-string parsing shared by the /data and /api/v1 blueprints.

Every helper aborts with 400 and a readable message on bad input, so
endpoints never turn a typo into a 500.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import abort, request

from ..detect import DEFAULT_MIN_STEP, DEFAULT_WINDOW_MINUTES, MAX_WINDOW_MINUTES, DetectOptions
from ..queries import MAX_POINTS_CAP, MAX_RANGE_SECONDS
from ..util import parse_iso_ts

# Most monitors one request may name.
MAX_IDS = 50


def int_arg(name: str, default: int, lo: int, hi: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        abort(400, description=f"{name} must be an integer")
    return max(lo, min(hi, v))


def ts_arg(name: str) -> datetime | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return parse_iso_ts(raw)
    except ValueError:
        abort(400, description=f"{name} must be an ISO-8601 timestamp")


def parse_range() -> tuple[datetime, datetime]:
    end = ts_arg("end") or datetime.now(timezone.utc)
    start = ts_arg("start")
    if start is None:
        minutes = int_arg("minutes", 60, 1, MAX_RANGE_SECONDS // 60)
        start = end - timedelta(minutes=minutes)
    if start >= end:
        abort(400, description="start must be before end")
    if (end - start).total_seconds() > MAX_RANGE_SECONDS:
        abort(400, description="range too long (max 366 days)")
    return start, end


def max_points_arg() -> int | None:
    """Optional ?max_points=N. When set, series longer than N are bucketed."""
    if not request.args.get("max_points"):
        return None
    return int_arg("max_points", MAX_POINTS_CAP, 10, MAX_POINTS_CAP)


def parse_ids(name: str = "ids") -> list[int]:
    ids_raw = request.args.get(name, "")
    try:
        ids = [int(x) for x in ids_raw.split(",") if x.strip()]
    except ValueError:
        abort(400, description=f"{name} must be comma-separated integers")
    # De-duplicate while keeping order; refuse (rather than silently trim)
    # more than one request should carry.
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_IDS:
        abort(400, description=f"at most {MAX_IDS} monitor ids per request")
    return ids


def detect_arg() -> DetectOptions | None:
    """`detect=1` turns on actual start/end detection, tuned by
    `window_minutes` (1–60, default 15), `min_step` (dB, default 6) and
    `detect_monitor` (reference monitor id; default the first dB monitor)."""
    if request.args.get("detect", "").lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        min_step = float(request.args.get("min_step") or DEFAULT_MIN_STEP)
    except ValueError:
        abort(400, description="min_step must be a number")
    if not 0.5 <= min_step <= 60:
        abort(400, description="min_step must be between 0.5 and 60")
    ref = request.args.get("detect_monitor")
    if ref and not ref.isdigit():
        abort(400, description="detect_monitor must be a monitor id")
    return DetectOptions(
        window_minutes=int_arg("window_minutes", DEFAULT_WINDOW_MINUTES, 1, MAX_WINDOW_MINUTES),
        min_step=min_step,
        monitor_id=int(ref) if ref else None,
    )

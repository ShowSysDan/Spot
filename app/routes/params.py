"""Query-string parsing shared by the /data and /api/v1 blueprints.

Every helper aborts with 400 and a readable message on bad input, so
endpoints never turn a typo into a 500.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import abort, request

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
    # De-duplicate while keeping order; cap to keep one request bounded.
    return list(dict.fromkeys(ids))[:MAX_IDS]

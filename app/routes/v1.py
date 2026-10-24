"""External, versioned API for other systems (e.g. 321Theater).

Every request needs an API key (Setup → API keys), sent as either

    Authorization: Bearer <key>
    X-API-Key: <key>

Errors are JSON: {"error": "...", "status": 400}. Timestamps are ISO-8601;
a timestamp without an offset is taken as UTC. See README "External API".
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, current_app, g, jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException

from ..db import session_scope
from ..detect import DEFAULT_MIN_STEP, DEFAULT_WINDOW_MINUTES, MAX_WINDOW_MINUTES, pdf_meta, resolve_range
from ..exports import render_overlay_pdf
from ..models import ApiKey, Monitor, Reading, Show
from ..notify import callback_url_problem, one_line
from ..queries import PDF_MAX_POINTS, collect_overlay, compute_summary
from ..shows import (
    MAX_SHOW_SECONDS,
    deliver_show,
    report_filename,
    show_range,
    show_report_pdf,
    show_stats,
    show_view,
)
from ..util import iso_local, parse_iso_ts, to_local
from ..version import __version__
from .params import MAX_IDS, detect_arg, max_points_arg, parse_ids, parse_range

bp = Blueprint("v1", __name__)
log = logging.getLogger("spot.api.v1")
access_log = logging.getLogger("spot.api.access")
security_log = logging.getLogger("spot.security")

# Only touch api_keys.last_used_at this often per key, not on every request.
LAST_USED_RESOLUTION = timedelta(minutes=5)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _presented_key() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


@bp.before_request
def _start_timer():
    g.api_t0 = time.perf_counter()
    g.api_key_name = None


@bp.after_request
def _access_log(resp):
    """One line per API call (category "API requests" on Setup → Logging):
    `GET /api/v1/stats?monitors=1,2&… 200 34ms 812B key="321Theater"`.
    4xx are logged as warnings and 5xx as errors."""
    ms = (time.perf_counter() - g.get("api_t0", time.perf_counter())) * 1000
    target = request.full_path.rstrip("?")
    if len(target) > 300:
        target = target[:297] + "..."
    level = logging.ERROR if resp.status_code >= 500 else (
        logging.WARNING if resp.status_code >= 400 else logging.INFO)
    size = resp.calculate_content_length()
    access_log.log(level, "%s %s %d %.0fms %s key=%s", request.method, target, resp.status_code, ms,
                   f"{size}B" if size is not None else "streamed",
                   f'"{g.api_key_name}"' if g.get("api_key_name") else "-")
    return resp


@bp.before_request
def _authenticate():
    key = _presented_key()
    if not key:
        abort(401, description="API key required (Authorization: Bearer <key> or X-API-Key)")
    digest = hash_key(key)
    with session_scope() as s:
        row = s.scalars(select(ApiKey).where(ApiKey.key_hash == digest)).first()
        # compare_digest: constant-time even though we looked up by hash.
        if row is None or not row.enabled or not hmac.compare_digest(row.key_hash, digest):
            security_log.warning("rejected API request %s %s (%s key)", request.method, request.path,
                                 "disabled" if row is not None else "unknown")
            abort(401, description="invalid or disabled API key")
        now = datetime.now(timezone.utc)
        if row.last_used_at is None or now - row.last_used_at > LAST_USED_RESOLUTION:
            row.last_used_at = now
        g.api_key_name = row.name


@bp.errorhandler(HTTPException)
def _json_error(e: HTTPException):
    return jsonify({"error": e.description, "status": e.code}), e.code


def _base_url() -> str:
    return current_app.config["SPOT"].public_url or request.url_root.rstrip("/")


def _monitor_ids_arg() -> list[int]:
    """`monitors=1,2,3` (or `ids=`). Required."""
    ids = parse_ids("monitors") or parse_ids("ids")
    if not ids:
        abort(400, description="monitors is required, e.g. monitors=1,2,3")
    return ids


def _detect_opts(s):
    """detect_arg() plus: a named reference monitor must exist (404)."""
    opts = detect_arg()
    if opts and opts.monitor_id is not None:
        _known_monitors(s, [opts.monitor_id])
    return opts


def _known_monitors(s, ids: list[int]) -> list[Monitor]:
    found = s.scalars(select(Monitor).where(Monitor.id.in_(ids)).order_by(Monitor.name)).all()
    missing = sorted(set(ids) - {m.id for m in found})
    if missing:
        abort(404, description=f"unknown monitor id(s): {', '.join(map(str, missing))}")
    return found


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    return jsonify({"service": "spot", "version": __version__, "api": "v1",
                    "key": g.api_key_name, "timezone": current_app.config["SPOT"].timezone,
                    "now": iso_local(datetime.now(timezone.utc))})


@bp.route("/monitors")
def monitors():
    """Every monitor (sensor) with its latest reading."""
    with session_scope() as s:
        out = []
        for m in s.scalars(select(Monitor).order_by(Monitor.name)):
            last = s.execute(
                select(Reading.ts, Reading.value)
                .where(Reading.monitor_id == m.id, Reading.value.is_not(None))
                .order_by(Reading.ts.desc()).limit(1)
            ).first()
            out.append({
                "id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
                "description": m.description, "enabled": m.enabled,
                "latest": {"ts": iso_local(last.ts), "value": last.value} if last else None,
            })
    return jsonify({"monitors": out})


# ---------------------------------------------------------------------------
# Statistics, series, reports for any monitors + range
# ---------------------------------------------------------------------------

@bp.route("/stats")
def stats():
    """Per-monitor statistics: ?monitors=1,2&start=…&end=…

    Includes min/max/avg/count/events, p10/p50/p90, and for dB units Leq,
    L10, L50, L90.
    """
    ids = _monitor_ids_arg()
    start, end = parse_range()
    with session_scope() as s:
        monitors = _known_monitors(s, ids)
        start, end, detection = resolve_range(s, ids, start, end, _detect_opts(s))
        out = [{
            "id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
            "stats": compute_summary(s, m.id, start, end, extended=True, unit=m.unit),
        } for m in monitors]
    body = {"start": iso_local(start), "end": iso_local(end), "monitors": out}
    if detection is not None:
        body["detection"] = detection
    return jsonify(body)


@bp.route("/series")
def series():
    """Readings for charting: ?monitors=…&start=…&end=…&max_points=500 (default 500)."""
    ids = _monitor_ids_arg()
    start, end = parse_range()
    max_points = max_points_arg() or 500
    with session_scope() as s:
        _known_monitors(s, ids)
        start, end, detection = resolve_range(s, ids, start, end, _detect_opts(s))
        data = collect_overlay(s, ids, start, end, max_points, with_summary=False)
    for m in data:
        m["points"] = [{"ts": iso_local(p["ts"]), "value": p["value"],
                        **({"min": p["min"], "max": p["max"]} if "min" in p else {})}
                       for p in m["points"]]
    body = {"start": iso_local(start), "end": iso_local(end), "monitors": data}
    if detection is not None:
        body["detection"] = detection
    return jsonify(body)


@bp.route("/report.pdf")
def report_pdf():
    """Branded PDF report: ?monitors=…&start=…&end=…[&title=…]"""
    ids = _monitor_ids_arg()
    start, end = parse_range()
    title = (request.args.get("title") or "").strip()[:200] or None
    with session_scope() as s:
        _known_monitors(s, ids)
        start, end, detection = resolve_range(s, ids, start, end, _detect_opts(s))
        data = collect_overlay(s, ids, start, end, PDF_MAX_POINTS,
                               with_summary=True, extended=True)
    body = render_overlay_pdf(start, end, data, title=title, extra_meta=pdf_meta(detection))
    return Response(body, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="spot_report_{to_local(start):%Y%m%dT%H%M}.pdf"'})


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def _show_or_404(s, show_id: int | None = None, ref: str | None = None) -> Show:
    # Normalise the ref exactly as it was stored (one line, trimmed).
    ref = one_line(ref) if ref is not None else None
    show = (s.get(Show, show_id) if show_id is not None else
            s.scalars(select(Show).where(Show.external_id == ref)).first())
    if show is None:
        abort(404, description="show not found")
    return show


def _show_payload(s, show: Show, with_stats: bool = True) -> dict:
    body = show_view(show, _base_url())
    if with_stats and show.starts_at <= datetime.now(timezone.utc):
        rng = show_range(s, show)
        body["monitors"] = show_stats(s, show, rng)
        body["report_start"], body["report_end"] = iso_local(rng[0]), iso_local(rng[1])
        body["detection"] = rng[2]
    return body


def _str_field(data: dict, key: str, max_len: int, required: bool = False) -> str | None:
    """A string field, on one line (CR/LF collapsed), trimmed, length-checked."""
    v = data.get(key)
    if v is None or v == "":
        if required:
            abort(400, description=f"{key} is required")
        return None
    if not isinstance(v, str):
        abort(400, description=f"{key} must be a string")
    v = one_line(v) if key != "notes" else v.strip()
    if required and not v:
        abort(400, description=f"{key} is required")
    if len(v) > max_len:
        abort(400, description=f"{key} must be at most {max_len} characters")
    return v or None


def _apply_show_fields(s, show: Show, data: dict, partial: bool) -> None:
    """Validate a JSON body and copy it onto `show`. Aborts 400 on bad input.

    `partial=False` (POST create/upsert) replaces the whole show: optional
    fields left out are cleared or reset to their defaults. `partial=True`
    (PATCH) changes only the keys present.
    """
    def given(k):
        return not partial or k in data

    if given("name"):
        show.name = _str_field(data, "name", 200, required=True)
    for key, attr in (("start", "starts_at"), ("end", "ends_at")):
        if given(key):
            raw = data.get(key)
            if not isinstance(raw, str) or not raw:
                abort(400, description=f"{key} is required (ISO-8601 string)")
            try:
                setattr(show, attr, parse_iso_ts(raw))
            except ValueError:
                abort(400, description=f"{key} must be an ISO-8601 timestamp")
    if show.ends_at <= show.starts_at:
        abort(400, description="end must be after start")
    if (show.ends_at - show.starts_at).total_seconds() > MAX_SHOW_SECONDS:
        abort(400, description="a show can be at most 7 days long")
    if given("monitors"):
        raw = data.get("monitors")
        if (not isinstance(raw, list) or not raw
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in raw)):
            abort(400, description="monitors must be a non-empty list of monitor ids")
        if len(set(raw)) > MAX_IDS:
            abort(400, description=f"at most {MAX_IDS} monitors per show")
        show.monitors = list(_known_monitors(s, raw))
    if given("notes"):
        show.notes = _str_field(data, "notes", 10_000)
    if given("callback_url"):
        url = data.get("callback_url") or ""
        if not isinstance(url, str):
            abort(400, description="callback_url must be a string")
        problem = callback_url_problem(url.strip(), current_app.config["SPOT"]) if url.strip() else None
        if problem:
            abort(400, description=problem)
        show.callback_url = url.strip() or None
    if given("auto_detect"):
        v = data.get("auto_detect", False)
        if not isinstance(v, bool):
            abort(400, description="auto_detect must be true or false")
        show.auto_detect = v
    if given("detect"):
        _apply_detect_settings(s, show, data.get("detect") or {})


def _apply_detect_settings(s, show: Show, d: dict) -> None:
    """`detect`: {"window_minutes", "min_step", "monitor"} — all optional."""
    if not isinstance(d, dict):
        abort(400, description="detect must be an object")
    win = d.get("window_minutes", DEFAULT_WINDOW_MINUTES)
    if not isinstance(win, int) or isinstance(win, bool) or not 1 <= win <= MAX_WINDOW_MINUTES:
        abort(400, description=f"detect.window_minutes must be 1–{MAX_WINDOW_MINUTES}")
    step = d.get("min_step", DEFAULT_MIN_STEP)
    if not isinstance(step, (int, float)) or isinstance(step, bool) or not 0.5 <= step <= 60:
        abort(400, description="detect.min_step must be 0.5–60")
    ref = d.get("monitor")
    if ref is not None:
        if not isinstance(ref, int) or isinstance(ref, bool):
            abort(400, description="detect.monitor must be a monitor id")
        _known_monitors(s, [ref])
    show.detect_window_minutes, show.detect_min_step, show.detect_monitor_id = win, float(step), ref


def _reset_delivery(show: Show) -> None:
    """A changed show gets its report (re)sent after it ends."""
    show.delivered_at = None
    show.delivery_attempts = 0
    show.delivery_status = None
    show.next_attempt_at = None


@bp.route("/shows", methods=["GET"])
def shows_list():
    """Shows overlapping [from, to] (default: the last 30 days and next 30)."""
    now = datetime.now(timezone.utc)
    try:
        frm = parse_iso_ts(request.args["from"]) if request.args.get("from") else now - timedelta(days=30)
        to = parse_iso_ts(request.args["to"]) if request.args.get("to") else now + timedelta(days=30)
    except ValueError:
        abort(400, description="from/to must be ISO-8601 timestamps")
    with session_scope() as s:
        rows = s.scalars(select(Show).where(Show.ends_at >= frm, Show.starts_at <= to)
                         .order_by(Show.starts_at).limit(500)).all()
        out = [show_view(sh, _base_url()) for sh in rows]
    return jsonify({"shows": out})


@bp.route("/shows", methods=["POST"])
def shows_create():
    """Create a show, or update it when `external_id` already exists (upsert).

    Body: {"name", "start", "end", "monitors": [ids], "external_id"?,
           "notes"?, "callback_url"?, "auto_detect"?, "detect"?}
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="JSON object body required")
    ext = _str_field(data, "external_id", 128)
    # Two simultaneous creates with the same new external_id: the loser hits
    # the unique constraint and simply retries as an update.
    for attempt in (1, 2):
        try:
            with session_scope() as s:
                show = s.scalars(select(Show).where(Show.external_id == ext)).first() if ext else None
                created = show is None
                if created:
                    show = Show(external_id=ext, source="api")
                _apply_show_fields(s, show, data, partial=False)
                _reset_delivery(show)
                s.add(show)
                s.flush()
                body = _show_payload(s, show, with_stats=False)
                log.info("show %s via API key=%s: %s id=%d ext=%s",
                         "created" if created else "updated", g.api_key_name, show.name, show.id, ext)
            return jsonify(body), 201 if created else 200
        except IntegrityError:
            if attempt == 2 or not ext:
                raise
    raise AssertionError("unreachable")


def _show_routes(show_id: int | None = None, ref: str | None = None):
    if request.method == "GET":
        with session_scope() as s:
            return jsonify(_show_payload(s, _show_or_404(s, show_id, ref)))
    if request.method == "DELETE":
        with session_scope() as s:
            show = _show_or_404(s, show_id, ref)
            log.info("show deleted via API key=%s: %s id=%d", g.api_key_name, show.name, show.id)
            s.delete(show)
        return "", 204
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="JSON object body required")
    with session_scope() as s:
        show = _show_or_404(s, show_id, ref)
        _apply_show_fields(s, show, data, partial=True)
        _reset_delivery(show)
        body = _show_payload(s, show, with_stats=False)
    return jsonify(body)


@bp.route("/shows/<int:show_id>", methods=["GET", "PATCH", "DELETE"])
def show_by_id(show_id: int):
    return _show_routes(show_id=show_id)


@bp.route("/shows/ref/<path:ref>", methods=["GET", "PATCH", "DELETE"])
def show_by_ref(ref: str):
    return _show_routes(ref=ref)


def _show_pdf(show_id: int | None = None, ref: str | None = None):
    with session_scope() as s:
        show = _show_or_404(s, show_id, ref)
        body = show_report_pdf(s, show)
        fname = report_filename(show)
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@bp.route("/shows/<int:show_id>/report.pdf")
def show_pdf_by_id(show_id: int):
    return _show_pdf(show_id=show_id)


@bp.route("/shows/ref/<path:ref>/report.pdf")
def show_pdf_by_ref(ref: str):
    return _show_pdf(ref=ref)


def _deliver(show_id: int | None = None, ref: str | None = None):
    with session_scope() as s:
        show = _show_or_404(s, show_id, ref)
        if not show.callback_url:
            abort(400, description="show has no callback_url")
        sid = show.id
    err = deliver_show(current_app.config["SPOT"], sid, manual=True)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True})


@bp.route("/shows/<int:show_id>/deliver", methods=["POST"])
def show_deliver_by_id(show_id: int):
    return _deliver(show_id=show_id)


@bp.route("/shows/ref/<path:ref>/deliver", methods=["POST"])
def show_deliver_by_ref(ref: str):
    return _deliver(ref=ref)

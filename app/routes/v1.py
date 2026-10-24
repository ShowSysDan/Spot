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
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, abort, current_app, g, jsonify, request
from sqlalchemy import select
from werkzeug.exceptions import HTTPException

from ..db import session_scope
from ..exports import render_overlay_pdf
from ..models import ApiKey, Monitor, Reading, Show
from ..queries import PDF_MAX_POINTS, collect_overlay, compute_summary
from ..shows import (
    MAX_SHOW_SECONDS,
    deliver_show,
    report_filename,
    show_report_pdf,
    show_stats,
    show_view,
)
from ..util import iso_local, parse_iso_ts, to_local
from ..version import __version__
from .params import max_points_arg, parse_ids, parse_range

bp = Blueprint("v1", __name__)
log = logging.getLogger("spot.api.v1")

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
def _authenticate():
    key = _presented_key()
    if not key:
        abort(401, description="API key required (Authorization: Bearer <key> or X-API-Key)")
    digest = hash_key(key)
    with session_scope() as s:
        row = s.scalars(select(ApiKey).where(ApiKey.key_hash == digest)).first()
        # compare_digest: constant-time even though we looked up by hash.
        if row is None or not row.enabled or not hmac.compare_digest(row.key_hash, digest):
            log.warning("rejected API request from %s (bad or disabled key)", request.remote_addr)
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
        out = [{
            "id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
            "stats": compute_summary(s, m.id, start, end, extended=True, unit=m.unit),
        } for m in _known_monitors(s, ids)]
    return jsonify({"start": iso_local(start), "end": iso_local(end), "monitors": out})


@bp.route("/series")
def series():
    """Readings for charting: ?monitors=…&start=…&end=…&max_points=500 (default 500)."""
    ids = _monitor_ids_arg()
    start, end = parse_range()
    max_points = max_points_arg() or 500
    with session_scope() as s:
        _known_monitors(s, ids)
        data = collect_overlay(s, ids, start, end, max_points, with_summary=False)
    for m in data:
        m["points"] = [{"ts": iso_local(p["ts"]), "value": p["value"],
                        **({"min": p["min"], "max": p["max"]} if "min" in p else {})}
                       for p in m["points"]]
    return jsonify({"start": iso_local(start), "end": iso_local(end), "monitors": data})


@bp.route("/report.pdf")
def report_pdf():
    """Branded PDF report: ?monitors=…&start=…&end=…[&title=…]"""
    ids = _monitor_ids_arg()
    start, end = parse_range()
    title = (request.args.get("title") or "").strip()[:200] or None
    with session_scope() as s:
        _known_monitors(s, ids)
        data = collect_overlay(s, ids, start, end, PDF_MAX_POINTS,
                               with_summary=True, extended=True)
    body = render_overlay_pdf(start, end, data, title=title)
    return Response(body, mimetype="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="spot_report_{to_local(start):%Y%m%dT%H%M}.pdf"'})


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def _show_or_404(s, show_id: int | None = None, ref: str | None = None) -> Show:
    show = (s.get(Show, show_id) if show_id is not None else
            s.scalars(select(Show).where(Show.external_id == ref)).first())
    if show is None:
        abort(404, description="show not found")
    return show


def _show_payload(s, show: Show, with_stats: bool = True) -> dict:
    body = show_view(show, _base_url())
    if with_stats and show.starts_at <= datetime.now(timezone.utc):
        body["monitors"] = show_stats(s, show)
    return body


def _apply_show_fields(s, show: Show, data: dict, partial: bool) -> None:
    """Validate a JSON body and copy it onto `show`. Aborts 400 on bad input."""
    def has(k):
        return k in data and data[k] is not None

    if not partial or has("name"):
        name = str(data.get("name") or "").strip()
        if not name:
            abort(400, description="name is required")
        show.name = name[:200]
    for key, attr in (("start", "starts_at"), ("end", "ends_at")):
        if not partial or has(key):
            if not has(key):
                abort(400, description=f"{key} is required (ISO-8601)")
            try:
                setattr(show, attr, parse_iso_ts(str(data[key])))
            except ValueError:
                abort(400, description=f"{key} must be an ISO-8601 timestamp")
    if show.ends_at <= show.starts_at:
        abort(400, description="end must be after start")
    if (show.ends_at - show.starts_at).total_seconds() > MAX_SHOW_SECONDS:
        abort(400, description="a show can be at most 7 days long")
    if not partial or "monitors" in data:
        raw = data.get("monitors") or []
        if (not isinstance(raw, list) or not raw
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in raw)):
            abort(400, description="monitors must be a non-empty list of monitor ids")
        show.monitors = list(_known_monitors(s, raw))
    if "notes" in data:
        show.notes = (str(data["notes"]).strip() or None) if data["notes"] is not None else None
    if "email_to" in data:
        emails = data["email_to"] or []
        if isinstance(emails, str):
            emails = [emails]
        if not all(isinstance(e, str) and "@" in e for e in emails):
            abort(400, description="email_to must be a list of e-mail addresses")
        show.email_to = ", ".join(e.strip() for e in emails) or None
    if "callback_url" in data:
        url = (data["callback_url"] or "").strip()
        if url and not re.match(r"^https?://", url):
            abort(400, description="callback_url must be an http(s) URL")
        show.callback_url = url or None


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
           "notes"?, "email_to"?: [addresses], "callback_url"?}
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="JSON object body required")
    ext = (str(data["external_id"]).strip()[:128] or None) if data.get("external_id") is not None else None
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
        if not (show.email_to or show.callback_url):
            abort(400, description="show has no email_to or callback_url")
        sid = show.id
    err = deliver_show(current_app.config["SPOT"], sid)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True})


@bp.route("/shows/<int:show_id>/deliver", methods=["POST"])
def show_deliver_by_id(show_id: int):
    return _deliver(show_id=show_id)


@bp.route("/shows/ref/<path:ref>/deliver", methods=["POST"])
def show_deliver_by_ref(ref: str):
    return _deliver(ref=ref)

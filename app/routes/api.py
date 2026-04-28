from __future__ import annotations

import logging
from datetime import datetime
from flask import Blueprint, abort, current_app, jsonify, request

from ..db import session_scope
from ..ingest import MalformedData, ingest_raw, store_reading
from ..models import Monitor
from ..util import parse_iso_ts

bp = Blueprint("api", __name__)
log = logging.getLogger("spot.api")


@bp.before_request
def _allowlist():
    cfg = current_app.config["SPOT"]
    if not cfg.ingest_allow:
        return
    src = request.remote_addr or ""
    if src not in cfg.ingest_allow:
        log.warning("rejected ingest from %s (not in allow list)", src)
        abort(403)


def _resolve_monitor(token: str) -> tuple[int, str]:
    with session_scope() as s:
        m = s.query(Monitor).filter(
            Monitor.auth_token == token,
            Monitor.enabled.is_(True),
        ).one_or_none()
        if not m:
            abort(404, description="unknown or disabled monitor")
        return m.id, m.name


@bp.route("/ingest/<token>", methods=["POST"])
def ingest(token: str):
    """Ingest a single reading or event for the monitor identified by token."""
    monitor_id, monitor_name = _resolve_monitor(token)

    ts: datetime | None = None
    value: float | None = None
    label: str | None = None
    raw_text: str | None = None
    src = request.remote_addr or "http"

    ctype = (request.content_type or "").lower()
    try:
        if "application/json" in ctype:
            data = request.get_json(silent=True) or {}
            if data.get("value") is not None:
                value = float(data["value"])
            if data.get("label") is not None:
                label = str(data["label"])
            if data.get("ts"):
                ts = parse_iso_ts(str(data["ts"]))
        elif "application/x-www-form-urlencoded" in ctype or request.form:
            if request.form.get("value"):
                value = float(request.form["value"])
            if request.form.get("label"):
                label = request.form["label"]
            if request.form.get("ts"):
                ts = parse_iso_ts(request.form["ts"])
        else:
            raw_text = request.get_data(as_text=True)
    except (ValueError, TypeError) as e:
        log.warning("malformed ingest monitor=%s err=%s", monitor_name, e)
        return jsonify({"ok": False, "error": "malformed payload"}), 400

    try:
        if raw_text is not None:
            rid = ingest_raw(monitor_id, monitor_name, raw_text, f"http:{src}")
        else:
            if value is None and not label:
                raise MalformedData("must provide value or label")
            rid = store_reading(monitor_id, value, label, ts)
            log.debug("ingest ok monitor=%s src=http:%s id=%s", monitor_name, src, rid)
        return jsonify({"ok": True, "id": rid})
    except MalformedData as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@bp.route("/event/<token>", methods=["POST"])
def event(token: str):
    """Convenience endpoint: post a label-only event marker."""
    monitor_id, monitor_name = _resolve_monitor(token)
    label: str | None = None
    ctype = (request.content_type or "").lower()
    if "application/json" in ctype:
        data = request.get_json(silent=True) or {}
        if data.get("label"):
            label = str(data["label"])
    elif request.form.get("label"):
        label = request.form["label"]
    else:
        label = (request.get_data(as_text=True) or "").strip() or None

    if not label:
        return jsonify({"ok": False, "error": "label required"}), 400

    rid = store_reading(monitor_id, None, label)
    log.info("event monitor=%s label=%r id=%s", monitor_name, label, rid)
    return jsonify({"ok": True, "id": rid})

from __future__ import annotations

import logging
import math
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit
from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select

from ..db import session_scope
from ..janitor import clear_monitor, effective_retention, purge_monitor
from ..listeners import ListenerManager
from ..modbus import (
    DATA_TYPE_REGS,
    DATA_TYPES,
    DEFAULT_PORT as MODBUS_DEFAULT_PORT,
    MIN_POLL_INTERVAL,
    ModbusManager,
    WORD_ORDERS,
    poll_now,
)
from .. import alert_actions
from ..alerts import KINDS as ALERT_KINDS, active_alerts_by_monitor
from .. import logging_config as logcfg
from .. import units
from ..models import Alert, AlertAction, ApiKey, ModbusDevice, Monitor, Show
from ..notify import callback_url_problem, one_line, post_json
from ..shows import (
    MAX_SHOW_SECONDS,
    deliver_show,
    report_filename,
    show_range,
    show_report_pdf,
    show_stats,
    show_view,
)
from ..portalloc import PORT_RANGE_END, PORT_RANGE_START, allocate_port
from ..storage import storage_overview
from ..util import (
    PALETTE,
    format_bytes,
    format_local,
    get_modbus_device_or_404,
    local_naive_to_utc,
    now_local,
    get_monitor_or_404,
    modbus_device_view,
    monitor_view,
    parse_iso_ts,
)
from .v1 import hash_key

bp = Blueprint("web", __name__)
log = logging.getLogger("spot.web")
security_log = logging.getLogger("spot.security")
export_log = logging.getLogger("spot.data")


@bp.before_request
def _same_origin_posts():
    """Cross-site request forgery guard for the (login-less) web UI.

    Browsers attach Origin (or at least Referer) to form POSTs. A POST whose
    Origin/Referer names another site is refused, so a page elsewhere can't
    make an operator's browser create API keys, delete monitors, etc.
    Requests with neither header (curl, scripts) are allowed — they aren't
    a forged browser request.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return None
    if source == "null" or urlsplit(source).netloc.lower() != request.host.lower():
        security_log.warning("refused cross-site %s %s from origin=%s", request.method, request.path, source)
        abort(403, description="Cross-site form submissions are not allowed.")
    return None


VALID_LISTENER_TYPES = ("http", "tcp", "udp", "modbus")


def _safe_next() -> str | None:
    """Same-site redirect target from a form's hidden `next` field, if any."""
    nxt = request.form.get("next") or ""
    # Browsers treat "/\host" like "//host", so refuse backslashes as well.
    parts = urlsplit(nxt)
    if not nxt.startswith("/") or "\\" in nxt or parts.scheme or parts.netloc or nxt.startswith("//"):
        return None
    return nxt


def _manager() -> ListenerManager:
    return current_app.config["SPOT_LISTENERS"]


def _modbus() -> ModbusManager:
    return current_app.config["SPOT_MODBUS"]


# A monitor whose newest reading is older than this is shown as "stale" on
# the home page. Modbus monitors use 3x their device's poll interval instead,
# since Spot knows exactly how often those should report.
STALE_AFTER_SECONDS = 300

# Offered in the Category box on the monitor form, alongside ones in use.
SUGGESTED_CATEGORIES = ("Audio", "Weather", "Room Temps")
GROUP_BY = ("location", "category")


@bp.route("/")
def index():
    # ?group=location|category switches the tile grouping; remembered in a
    # cookie so the Monitors link keeps the last choice.
    group_by = request.args.get("group")
    if group_by not in GROUP_BY:
        group_by = request.cookies.get("spot_group")
    if group_by not in GROUP_BY:
        group_by = "location"
    with session_scope() as s:
        rows = []
        for m in s.query(Monitor).order_by(Monitor.name).all():
            v = monitor_view(m)
            if m.modbus_device is not None:
                v["stale_after"] = max(30, 3 * m.modbus_device.poll_interval_seconds)
            else:
                v["stale_after"] = STALE_AFTER_SECONDS
            # Sort key for grouping tiles: named groups A-Z, unassigned last.
            v["group_key"] = (m.category if group_by == "category" else m.location) or ""
            rows.append(v)
    rows.sort(key=lambda r: (r["group_key"] == "", r["group_key"].lower(), r["name"].lower()))
    statuses = _manager().status()
    for r in rows:
        r["listener_alive"] = statuses.get(r["id"], False)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group_key"], []).append(r)
    resp = current_app.make_response(render_template(
        "index.html", monitors=rows, groups=list(groups.items()), group_by=group_by,
        has_categories=any(r["category"] for r in rows)))
    if request.args.get("group") == group_by:
        resp.set_cookie("spot_group", group_by, max_age=365 * 86400, samesite="Lax")
    return resp


@bp.route("/monitors/new", methods=["GET", "POST"])
def monitor_new():
    if request.method == "POST":
        return _save_monitor(None)
    return _monitor_form(None)


@bp.route("/monitors/<int:mid>/edit", methods=["GET", "POST"])
def monitor_edit(mid: int):
    if request.method == "POST":
        return _save_monitor(mid)
    return _monitor_form(mid)


def _monitor_form(mid: int | None):
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid)) if mid is not None else None
        devices = [modbus_device_view(d) for d in
                   s.query(ModbusDevice).order_by(ModbusDevice.name).all()]
        locations = sorted({loc for (loc,) in s.execute(
            select(Monitor.location).where(Monitor.location.is_not(None))).all()})
        categories = sorted({c for (c,) in s.execute(
            select(Monitor.category).where(Monitor.category.is_not(None))).all()}
            | set(SUGGESTED_CATEGORIES), key=str.lower)
    return render_template("monitor_form.html", monitor=data,
                           listener_types=VALID_LISTENER_TYPES,
                           default_retention=current_app.config["SPOT"].default_retention_days,
                           modbus_devices=devices,
                           modbus_data_types=DATA_TYPES,
                           locations=locations, categories=categories, palette=PALETTE)


@bp.route("/monitors/<int:mid>/delete", methods=["POST"])
def monitor_delete(mid: int):
    _manager().stop_monitor(mid)
    with session_scope() as s:
        m = s.get(Monitor, mid)
        if m:
            log.info("monitor deleted: %s id=%d", m.name, mid)
            s.delete(m)
    flash("Monitor deleted.", "success")
    return redirect(url_for("web.index"))


@bp.route("/monitors/<int:mid>/toggle", methods=["POST"])
def monitor_toggle(mid: int):
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        m.enabled = not m.enabled
        info = monitor_view(m)
    if info["enabled"]:
        _manager().start_monitor(info["id"], info["name"], info["listener_type"],
                                 info["port"], info["value_regex"])
        log.info("monitor enabled: %s id=%d", info["name"], info["id"])
    else:
        _manager().stop_monitor(info["id"])
        log.info("monitor disabled: %s id=%d", info["name"], info["id"])
    flash(f"{info['name']} {'enabled' if info['enabled'] else 'disabled'}.", "success")
    return redirect(_safe_next() or url_for("web.index"))


@bp.route("/monitors/<int:mid>")
def monitor_detail(mid: int):
    cfg = current_app.config["SPOT"]
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    data["listener_alive"] = _manager().status().get(mid, False)
    data["effective_retention_days"] = effective_retention(
        data["retention_days"], cfg.default_retention_days
    )
    data["default_retention_days"] = cfg.default_retention_days
    alerts = active_alerts_by_monitor().get(mid, [])
    return render_template("monitor_detail.html", monitor=data, alerts=alerts)


@bp.route("/monitors/<int:mid>/dashboard")
def monitor_dashboard(mid: int):
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    return render_template("dashboard.html", monitor=data)


# ---------------------------------------------------------------------------
# Wall display
# ---------------------------------------------------------------------------

@bp.route("/wall")
def wall():
    """Full-screen kiosk view. ?location=Name, ?category=Name and/or
    ?ids=1,2 narrow it."""
    location = request.args.get("location") or None
    category = request.args.get("category") or None
    ids = {int(x) for x in request.args.get("ids", "").split(",") if x.isdigit()}
    with session_scope() as s:
        all_monitors = [monitor_view(m) for m in s.scalars(
            select(Monitor).where(Monitor.enabled.is_(True)).order_by(Monitor.name))]
    locations = sorted({m["location"] for m in all_monitors if m["location"]})
    categories = sorted({m["category"] for m in all_monitors if m["category"]}, key=str.lower)
    shown = [m for m in all_monitors
             if (not location or m["location"] == location)
             and (not category or m["category"] == category)
             and (not ids or m["id"] in ids)]
    title = " · ".join(x for x in (location, category) if x) or "All monitors"
    return render_template("wall.html", monitors=shown, locations=locations,
                           categories=categories, location=location,
                           category=category, title=title)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@bp.route("/alerts")
def alerts():
    with session_scope() as s:
        def rows(q):
            return [{
                "id": a.id, "kind": a.kind, "message": a.message,
                "monitor_id": a.monitor_id, "monitor_name": a.monitor.name,
                "unit": a.monitor.unit,
                "location": a.monitor.location,
                "started": format_local(a.started_at),
                "cleared": format_local(a.cleared_at) if a.cleared_at else None,
                "duration": _duration_text((a.cleared_at or datetime.now(timezone.utc)) - a.started_at),
                "acknowledged": a.acknowledged_at is not None,
                "peak_value": a.peak_value,
            } for a in s.scalars(q)]
        active = rows(select(Alert).where(Alert.cleared_at.is_(None))
                      .order_by(Alert.started_at.desc()))
        history = rows(select(Alert).where(Alert.cleared_at.is_not(None))
                       .order_by(Alert.started_at.desc()).limit(200))
        configured = [monitor_view(m) for m in s.scalars(
            select(Monitor).order_by(Monitor.name)) if
            m.alert_high is not None or m.alert_low is not None or m.alert_stale_seconds]
        actions = [_alert_action_view(a) for a in s.scalars(
            select(AlertAction).order_by(AlertAction.name, AlertAction.id))]
    cfg = current_app.config["SPOT"]
    return render_template("alerts.html", active=active, history=history,
                           configured=configured, actions=actions,
                           webhook_on=bool(cfg.alert_webhook_url),
                           interval=cfg.alert_interval_seconds)


@bp.route("/alerts/<int:aid>/ack", methods=["POST"])
def alert_ack(aid: int):
    with session_scope() as s:
        a = s.get(Alert, aid)
        if a and a.acknowledged_at is None:
            a.acknowledged_at = datetime.now(timezone.utc)
            log.info("alert acknowledged id=%d monitor_id=%d kind=%s", aid, a.monitor_id, a.kind)
    return redirect(_safe_next() or url_for("web.alerts"))


# ---------------------------------------------------------------------------
# Alert actions (UDP / TCP / HTTP request on raise and on clear)
# ---------------------------------------------------------------------------

def _alert_action_view(a: AlertAction) -> dict:
    return {
        "id": a.id, "name": a.name, "enabled": a.enabled,
        "monitor_id": a.monitor_id, "monitor_name": a.monitor.name if a.monitor else None,
        "kind": a.kind, "protocol": a.protocol, "host": a.host, "port": a.port,
        "http_method": a.http_method, "raise_url": a.raise_url, "clear_url": a.clear_url,
        "raise_payload": a.raise_payload, "clear_payload": a.clear_payload,
        "retries": a.retries, "retry_delay_seconds": a.retry_delay_seconds,
        "target": alert_actions.target(a),
        "sends_clear": bool(a.clear_url if a.protocol == "http" else a.clear_payload),
        "last_sent": format_local(a.last_sent_at) if a.last_sent_at else None,
        "last_status": a.last_status,
        "last_ok": bool(a.last_status) and "failed" not in a.last_status,
    }


def _get_alert_action_or_404(s, aid: int) -> AlertAction:
    a = s.get(AlertAction, aid)
    if a is None:
        abort(404)
    return a


@bp.route("/alerts/actions/new", methods=["GET", "POST"])
def alert_action_new():
    if request.method == "POST":
        return _save_alert_action(None)
    return _alert_action_form(None)


@bp.route("/alerts/actions/<int:aid>/edit", methods=["GET", "POST"])
def alert_action_edit(aid: int):
    if request.method == "POST":
        return _save_alert_action(aid)
    return _alert_action_form(aid)


def _alert_action_form(aid: int | None):
    data = None
    with session_scope() as s:
        if aid is not None:
            data = _alert_action_view(_get_alert_action_or_404(s, aid))
        monitors = [{"id": m.id, "name": m.name} for m in
                    s.scalars(select(Monitor).order_by(Monitor.name))]
    return render_template("alert_action_form.html", action=data, monitors=monitors,
                           kinds=ALERT_KINDS, protocols=alert_actions.PROTOCOLS,
                           methods=alert_actions.HTTP_METHODS,
                           placeholders=alert_actions.PLACEHOLDERS)


def _save_alert_action(aid: int | None):
    try:
        return _save_alert_action_checked(aid)
    except _FormError as e:
        flash(str(e), "error")
        return redirect(request.url)


def _save_alert_action_checked(aid: int | None):
    name = _form_str("name")[:120]
    protocol = _form_str("protocol", "http")
    kind = _form_str("kind") or None
    monitor_id = _form_int("monitor_id", None, "Invalid monitor.")
    _check(bool(name), "Name is required.")
    _check(protocol in alert_actions.PROTOCOLS, "Protocol must be HTTP, TCP or UDP.")
    _check(kind is None or kind in ALERT_KINDS, "Invalid alert type.")
    retries_err = "Retries must be 0–10."
    retries = _form_int("retries", "0", retries_err)
    _check(0 <= retries <= 10, retries_err)
    delay_err = "Retry delay must be 1–3600 seconds."
    retry_delay = _form_int("retry_delay_seconds", "10", delay_err)
    _check(1 <= retry_delay <= 3600, delay_err)

    raise_payload = request.form.get("raise_payload") or None
    clear_payload = request.form.get("clear_payload") or None
    host = port = raise_url = clear_url = None
    http_method = _form_str("http_method", "POST").upper()
    if protocol == "http":
        _check(http_method in alert_actions.HTTP_METHODS, "Invalid HTTP method.")
        raise_url = _form_str("raise_url") or None
        clear_url = _form_str("clear_url") or None
        _check(bool(raise_url), "A URL to call when the alert is raised is required.")
        for url in (raise_url, clear_url):
            parts = urlsplit(url or "http://x")
            _check(parts.scheme in ("http", "https") and bool(parts.hostname),
                   f"{url} is not an http(s) URL with a host.")
    else:
        host = _form_str("host") or None
        port_err = "Port must be 1–65535."
        port = _form_int("port", "", port_err)
        _check(bool(host), "Host is required.")
        _check(1 <= port <= 65535, port_err)
        _check(bool(raise_payload), "A message to send when the alert is raised is required.")

    fields = dict(name=name, protocol=protocol, kind=kind, monitor_id=monitor_id or None,
                  host=host, port=port, http_method=http_method,
                  raise_url=raise_url, clear_url=clear_url,
                  raise_payload=raise_payload, clear_payload=clear_payload,
                  retries=retries, retry_delay_seconds=retry_delay,
                  enabled=bool(request.form.get("enabled")))
    with session_scope() as s:
        _check(not monitor_id or s.get(Monitor, monitor_id) is not None, "Invalid monitor.")
        if aid is None:
            a = AlertAction(**fields)
            s.add(a)
            s.flush()
        else:
            a = _get_alert_action_or_404(s, aid)
            for k, v in fields.items():
                setattr(a, k, v)
        log.info("alert action %s: %s id=%d %s", "created" if aid is None else "updated",
                 a.name, a.id, alert_actions.target(a))
    flash("Alert action saved.", "success")
    return redirect(url_for("web.alerts") + "#actions")


@bp.route("/alerts/actions/<int:aid>/toggle", methods=["POST"])
def alert_action_toggle(aid: int):
    with session_scope() as s:
        a = _get_alert_action_or_404(s, aid)
        a.enabled = not a.enabled
        log.info("alert action %s: %s id=%d", "enabled" if a.enabled else "disabled", a.name, aid)
    return redirect(url_for("web.alerts") + "#actions")


@bp.route("/alerts/actions/<int:aid>/delete", methods=["POST"])
def alert_action_delete(aid: int):
    with session_scope() as s:
        a = s.get(AlertAction, aid)
        if a:
            log.info("alert action deleted: %s id=%d", a.name, aid)
            s.delete(a)
    flash("Alert action deleted.", "success")
    return redirect(url_for("web.alerts") + "#actions")


@bp.route("/alerts/actions/<int:aid>/test", methods=["POST"])
def alert_action_test(aid: int):
    """Send the raise or clear request once, now, with made-up values."""
    state = "cleared" if request.form.get("state") == "cleared" else "raised"
    with session_scope() as s:
        a = _get_alert_action_or_404(s, aid)
        ctx = alert_actions.sample_context(a, state)
        s.expunge(a)
    res = alert_actions.send(a, state, ctx)
    if res.skipped:
        flash(f"{a.name}: no {'clear' if state == 'cleared' else 'raise'} request is set.", "error")
    else:
        alert_actions.record(a.id, f"test {state}: {res.detail if res.ok else 'failed — ' + res.detail}")
        log.info("alert action test %s: %s %s → %s", a.name, state,
                 alert_actions.target(a, state), res.detail)
        flash(f"Test {state} to {alert_actions.target(a, state)}: {res.detail}",
              "success" if res.ok else "error")
    return redirect(url_for("web.alerts") + "#actions")


def _duration_text(td) -> str:
    sec = int(td.total_seconds())
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h {sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d {sec % 86400 // 3600}h"


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

@bp.route("/shows")
def shows():
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        current = [show_view(x) for x in s.scalars(
            select(Show).where(Show.ends_at >= now).order_by(Show.starts_at).limit(200))]
        past = [show_view(x) for x in s.scalars(
            select(Show).where(Show.ends_at < now).order_by(Show.starts_at.desc()).limit(100))]
    return render_template("shows.html", current=current, past=past)


@bp.route("/shows/new", methods=["GET", "POST"])
def show_new():
    if request.method == "POST":
        return _save_show(None)
    prefill = {
        "monitor_ids": [int(x) for x in request.args.get("ids", "").split(",") if x.isdigit()],
        "starts_at": request.args.get("start"),
        "ends_at": request.args.get("end"),
        "name": request.args.get("name", ""),
    }
    return _show_form(None, prefill)


@bp.route("/shows/<int:sid>/edit", methods=["GET", "POST"])
def show_edit(sid: int):
    if request.method == "POST":
        return _save_show(sid)
    return _show_form(sid, None)


def _show_form(sid: int | None, prefill: dict | None):
    with session_scope() as s:
        show = show_view(_get_show_or_404(s, sid)) if sid is not None else prefill
        monitors = [monitor_view(m) for m in s.scalars(select(Monitor).order_by(Monitor.name))]
    for m in monitors:
        m["location_key"] = m["location"] or ""
    return render_template("show_form.html", show=show, is_new=sid is None,
                           monitors=monitors)


def _get_show_or_404(s, sid: int) -> Show:
    show = s.get(Show, sid)
    if show is None:
        abort(404)
    return show


def _form_ts(name: str, label: str) -> datetime:
    """The browser sends `<name>_iso` (UTC, set by JS); without JS fall back to
    the raw datetime-local value read as server-local time."""
    raw_iso = _form_str(f"{name}_iso")
    raw = raw_iso or _form_str(name)
    _check(bool(raw), f"{label} is required.")
    try:
        if raw_iso:
            return parse_iso_ts(raw_iso)
        return local_naive_to_utc(datetime.fromisoformat(raw).replace(tzinfo=None))
    except ValueError:
        raise _FormError(f"{label} is not a valid date and time.") from None


def _save_show(sid: int | None):
    try:
        name = one_line(_form_str("name"))
        _check(bool(name), "Name is required.")
        starts_at = _form_ts("start", "Start")
        ends_at = _form_ts("end", "End")
        _check(ends_at > starts_at, "End must be after start.")
        _check((ends_at - starts_at).total_seconds() <= MAX_SHOW_SECONDS,
               "A show can be at most 7 days long.")
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        _check(bool(ids), "Pick at least one monitor.")
        callback = _form_str("callback_url") or None
        window = _form_int("detect_window_minutes", "15", "Search window must be a whole number of minutes.")
        _check(1 <= window <= 60, "Search window must be 1–60 minutes.")
        min_step = _form_float("detect_min_step", "Minimum step must be a number.") or 6.0
        _check(0.5 <= min_step <= 60, "Minimum step must be 0.5–60 dB.")
        ref = _form_int("detect_monitor_id", None, "Invalid reference monitor.")
        problem = callback_url_problem(callback, current_app.config["SPOT"]) if callback else None
        _check(problem is None, (problem or "").replace("callback_url", "Callback URL"))
        with session_scope() as s:
            show = Show(source="ui") if sid is None else _get_show_or_404(s, sid)
            show.name = name[:200]
            show.starts_at, show.ends_at = starts_at, ends_at
            show.notes = _form_str("notes") or None
            show.callback_url = callback
            show.auto_detect = bool(request.form.get("auto_detect"))
            show.detect_window_minutes = window
            show.detect_min_step = min_step
            show.detect_monitor_id = ref if ref in ids else None
            show.monitors = list(s.scalars(select(Monitor).where(Monitor.id.in_(ids))))
            # Any edit re-arms report delivery for after the show ends.
            show.delivered_at = None
            show.delivery_attempts = 0
            show.delivery_status = None
            show.next_attempt_at = None
            s.add(show)
            s.flush()
            new_id = show.id
            log.info("show %s: %s id=%d %s → %s monitors=%s",
                     "created" if sid is None else "updated", show.name, show.id,
                     starts_at.isoformat(), ends_at.isoformat(), ids)
    except _FormError as e:
        flash(str(e), "error")
        return redirect(request.url)
    flash("Show saved.", "success")
    return redirect(url_for("web.show_detail", sid=new_id))


@bp.route("/shows/<int:sid>")
def show_detail(sid: int):
    with session_scope() as s:
        show = _get_show_or_404(s, sid)
        view = show_view(show)
        rng = show_range(s, show) if view["status"] != "upcoming" else None
        stats = show_stats(s, show, rng) if rng else []
    detection = rng[2] if rng else None
    report = {"start": rng[0].isoformat(), "end": rng[1].isoformat()} if rng else None
    return render_template("show_detail.html", show=view, stats=stats,
                           detection=detection, report=report)


@bp.route("/shows/<int:sid>/report.pdf")
def show_report(sid: int):
    with session_scope() as s:
        show = _get_show_or_404(s, sid)
        body = show_report_pdf(s, show)
        fname = report_filename(show)
        export_log.info("export pdf show=%s id=%d bytes=%d", show.name, sid, len(body))
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@bp.route("/shows/<int:sid>/deliver", methods=["POST"])
def show_deliver(sid: int):
    err = deliver_show(current_app.config["SPOT"], sid, manual=True)
    flash(f"Sending the report failed: {err}" if err else "Report sent.",
          "error" if err else "success")
    return redirect(url_for("web.show_detail", sid=sid))


@bp.route("/shows/<int:sid>/delete", methods=["POST"])
def show_delete(sid: int):
    with session_scope() as s:
        show = s.get(Show, sid)
        if show:
            log.info("show deleted: %s id=%d", show.name, sid)
            s.delete(show)
    flash("Show deleted.", "success")
    return redirect(url_for("web.shows"))


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

@bp.route("/setup/api-keys", methods=["GET", "POST"])
def api_keys():
    new_key = None
    if request.method == "POST":
        name = _form_str("name")[:120]
        if not name:
            flash("Give the key a name, e.g. the system that will use it.", "error")
            return redirect(url_for("web.api_keys"))
        # Shown exactly once, in this response — never flashed, because flash
        # messages live in the (signed, not encrypted) session cookie.
        new_key = "spot_" + secrets.token_urlsafe(32)
        with session_scope() as s:
            s.add(ApiKey(name=name, key_hash=hash_key(new_key), prefix=new_key[:10]))
        log.info("API key created: %s", name)
    with session_scope() as s:
        keys = [{"id": k.id, "name": k.name, "prefix": k.prefix, "enabled": k.enabled,
                 "created": format_local(k.created_at),
                 "last_used": format_local(k.last_used_at) if k.last_used_at else "never"}
                for k in s.scalars(select(ApiKey).order_by(ApiKey.created_at))]
    return render_template("api_keys.html", keys=keys, new_key=new_key,
                           new_key_name=_form_str("name") if new_key else None)


@bp.route("/setup/api-keys/<int:kid>/toggle", methods=["POST"])
def api_key_toggle(kid: int):
    with session_scope() as s:
        k = s.get(ApiKey, kid)
        if k:
            k.enabled = not k.enabled
            log.info("API key %s: %s", "enabled" if k.enabled else "disabled", k.name)
    return redirect(url_for("web.api_keys"))


@bp.route("/setup/api-keys/<int:kid>/delete", methods=["POST"])
def api_key_delete(kid: int):
    with session_scope() as s:
        k = s.get(ApiKey, kid)
        if k:
            log.info("API key deleted: %s", k.name)
            s.delete(k)
    flash("API key deleted.", "success")
    return redirect(url_for("web.api_keys"))


# ---------------------------------------------------------------------------
# Logging & syslog
# ---------------------------------------------------------------------------

@bp.route("/setup/logging", methods=["GET", "POST"])
def logging_settings():
    cfg = current_app.config["SPOT"]
    if request.method == "POST":
        try:
            settings = _logging_form()
        except _FormError as e:
            flash(str(e), "error")
            return redirect(url_for("web.logging_settings"))
        err = logcfg.save_settings(settings)
        log.info("logging settings changed: syslog=%s %s %s facility=%s app=%s levels=%s",
                 "on" if settings.syslog_enabled else "off", settings.syslog_protocol,
                 settings.syslog_address, settings.syslog_facility, settings.syslog_tag,
                 ",".join(f"{k}={v}" for k, v in settings.levels.items()))
        if err:
            flash(f"Settings saved, but syslog isn't working: {err}", "error")
        else:
            flash("Logging settings saved and applied.", "success")
        return redirect(url_for("web.logging_settings"))

    level = request.args.get("level", "debug")
    category = request.args.get("category", "")
    entries = logcfg.recent(limit=300, min_level=level if level in logcfg.LEVELS else "debug",
                            category=category if category in logcfg.CATEGORY_KEYS else "")
    for e in entries:
        e["local"] = format_local(e["ts"], "%m-%d %H:%M:%S")
    st = logcfg.status()
    for k in ("error_at", "last_sent_at"):
        st[k + "_local"] = format_local(st[k], "%Y-%m-%d %H:%M:%S") if st[k] else None
    return render_template("logging.html", settings=logcfg.current_settings(), status=st,
                           categories=logcfg.CATEGORIES, levels=list(logcfg.LEVELS),
                           facilities=logcfg.FACILITIES, entries=entries,
                           env=logcfg.env_settings(cfg), level=level, category=category)


def _logging_form() -> "logcfg.LogSettings":
    enabled = bool(request.form.get("syslog_enabled"))
    address = _form_str("syslog_address")
    protocol = _form_str("syslog_protocol", "udp").lower()
    facility = _form_str("syslog_facility", "local0").lower()
    tag = _form_str("syslog_tag", "spot")
    _check(protocol in ("udp", "tcp"), "Transport must be UDP or TCP.")
    _check(facility in logcfg.FACILITIES, "Unknown syslog facility.")
    _check(re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", tag) is not None,
           "App name: 1–32 letters, digits, '.', '_' or '-' (no spaces).")
    _check(not enabled or bool(address), "Enter a syslog destination, or turn syslog off.")
    _check(not address or address.startswith("/") or re.fullmatch(r"\[?[A-Za-z0-9_.:-]+\]?:\d{1,5}", address) is not None,
           "Destination must be a local socket path (/dev/log) or host:port (loghost:514).")
    levels = {}
    for c in logcfg.CATEGORIES:
        v = _form_str(f"level_{c.key}", c.default)
        _check(v in logcfg.LEVELS, f"Unknown level for {c.label}.")
        levels[c.key] = v
    return logcfg.LogSettings(syslog_enabled=enabled, syslog_address=address,
                              syslog_protocol=protocol, syslog_facility=facility,
                              syslog_tag=tag, levels=levels)


@bp.route("/setup/logging/test", methods=["POST"])
def logging_test():
    err = logcfg.send_test_message()
    flash(f"Test message failed: {err}" if err else
          "Test message sent to syslog — look for “Spot syslog test message”.",
          "error" if err else "success")
    return redirect(url_for("web.logging_settings"))


@bp.route("/setup/logging/reset", methods=["POST"])
def logging_reset():
    err = logcfg.reset_to_env(current_app.config["SPOT"])
    log.info("logging settings reset to .env defaults")
    flash(f"Back to the .env settings, but syslog isn't working: {err}" if err
          else "Back to the .env settings.", "error" if err else "success")
    return redirect(url_for("web.logging_settings"))


# ---------------------------------------------------------------------------
# Units (which ones are sound levels)
# ---------------------------------------------------------------------------

@bp.route("/setup/units", methods=["GET", "POST"])
def units_settings():
    if request.method == "POST":
        chosen = request.form.getlist("sound")
        chosen += [u.strip() for u in request.form.get("add", "").split(",")]
        units.save([u for u in chosen if u and len(u) <= 32])
        log.info("sound units changed: %s", ", ".join(units.saved_units()) or "(none)")
        flash("Sound units saved.", "success")
        return redirect(url_for("web.units_settings"))
    with session_scope() as s:
        in_use = dict(s.execute(
            select(Monitor.unit, func.count()).group_by(Monitor.unit)).all())
    # Every unit monitors use, plus saved and suggested sound units not used yet.
    names = {u.lower(): u for u in [*units.SUGGESTED, *(units.saved_units() or []), *in_use]}
    rows = [{"unit": u, "monitors": in_use.get(u, 0), "sound": units.is_sound_unit(u)}
            for u in sorted(names.values(), key=str.lower)]
    return render_template("units.html", rows=rows, saved=units.saved_units() is not None)


@bp.route("/setup/units/reset", methods=["POST"])
def units_reset():
    units.reset()
    log.info("sound units reset to default (units starting with dB)")
    flash("Back to the default: every unit starting with “dB” is a sound unit.", "success")
    return redirect(url_for("web.units_settings"))


# ---------------------------------------------------------------------------
# Setup hub + notification tests
# ---------------------------------------------------------------------------

@bp.route("/setup")
def setup():
    cfg = current_app.config["SPOT"]
    with session_scope() as s:
        counts = {
            "modbus": s.query(ModbusDevice).count(),
            "monitors": s.query(Monitor).count(),
            "api_keys": s.query(ApiKey).filter(ApiKey.enabled.is_(True)).count(),
        }
    server_now = datetime.now().astimezone()
    display_now = now_local()
    clock = {
        "display_zone": cfg.timezone,
        "display_now": display_now.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
        "server_zone": server_now.tzname(),
        "server_now": server_now.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
        "differs": server_now.utcoffset() != display_now.utcoffset(),
    }
    return render_template("setup.html", cfg=cfg, counts=counts, clock=clock,
                           log_status=logcfg.status())


@bp.route("/setup/test-webhook", methods=["POST"])
def setup_test_webhook():
    cfg = current_app.config["SPOT"]
    if not cfg.alert_webhook_url:
        flash("No alert webhook configured (set SPOT_ALERT_WEBHOOK_URL).", "error")
    else:
        err = post_json(cfg.alert_webhook_url, {"event": "test", "message": "Test from Spot"})
        log.info("test webhook sent to %s: %s", cfg.alert_webhook_url, err or "ok")
        flash(f"Webhook failed: {err}" if err else "Test webhook delivered.",
              "error" if err else "success")
    return redirect(url_for("web.setup"))


@bp.route("/monitors/<int:mid>/query")
def monitor_query(mid: int):
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    return render_template("query.html", monitor=data)


@bp.route("/monitors/<int:mid>/purge", methods=["POST"])
def monitor_purge(mid: int):
    cfg = current_app.config["SPOT"]
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        name = m.name
        eff = effective_retention(m.retention_days, cfg.default_retention_days)
    if not eff:
        flash("No retention policy in effect (set per-monitor days or SPOT_DEFAULT_RETENTION_DAYS).", "error")
        return redirect(url_for("web.monitor_detail", mid=mid))
    deleted = purge_monitor(mid, name, eff)
    flash(f"Purged {deleted} reading(s) older than {eff} day(s).", "success")
    return redirect(url_for("web.monitor_detail", mid=mid))


@bp.route("/monitors/<int:mid>/clear-data", methods=["POST"])
def monitor_clear_data(mid: int):
    with session_scope() as s:
        m = get_monitor_or_404(s, mid)
        name = m.name
    deleted = clear_monitor(mid, name)
    flash(f"Deleted all {deleted} reading(s) for {name}. Monitor kept.", "success")
    return redirect(url_for("web.monitor_detail", mid=mid))


@bp.route("/storage")
def storage():
    cfg = current_app.config["SPOT"]
    with session_scope() as s:
        overview = storage_overview(s, cfg.db_schema)
    overview["schema_human"] = format_bytes(overview["schema_bytes"])
    overview["readings_human"] = format_bytes(overview["readings_bytes"])
    overview["monitors_human"] = format_bytes(overview["monitors_bytes"])
    overview["default_retention_days"] = cfg.default_retention_days
    for m in overview["monitors"]:
        m["estimated_human"] = format_bytes(m["estimated_bytes"])
        m["effective_retention_days"] = effective_retention(
            m["retention_days"], cfg.default_retention_days
        )
        m["oldest_local"] = format_local(m["oldest"])
        m["newest_local"] = format_local(m["newest"])
    return render_template("storage.html", overview=overview)


@bp.route("/reports")
def reports():
    """Compare monitors on one chart and export PDF/CSV reports."""
    with session_scope() as s:
        rows = [monitor_view(m) for m in s.query(Monitor).order_by(Monitor.name).all()]
        recent_shows = [show_view(x) for x in s.scalars(
            select(Show).where(Show.starts_at <= datetime.now(timezone.utc))
            .order_by(Show.starts_at.desc()).limit(50))]
    return render_template("reports.html", monitors=rows, shows=recent_shows)


@bp.route("/overlay")
def overlay():
    """Old name for /reports; kept so bookmarks keep working."""
    qs = request.query_string.decode()
    return redirect(url_for("web.reports") + (f"?{qs}" if qs else ""), code=301)


# ---------------------------------------------------------------------------
# Modbus device CRUD
# ---------------------------------------------------------------------------

@bp.route("/modbus")
def modbus_list():
    with session_scope() as s:
        devices = [modbus_device_view(d) for d in
                   s.query(ModbusDevice).order_by(ModbusDevice.name).all()]
    statuses = _modbus().status()
    for d in devices:
        d["alive"] = statuses.get(d["id"], False)
    return render_template("modbus_list.html", devices=devices)


@bp.route("/modbus/new", methods=["GET", "POST"])
def modbus_new():
    if request.method == "POST":
        return _save_modbus_device(None)
    return _modbus_form(None)


@bp.route("/modbus/<int:did>")
def modbus_detail(did: int):
    with session_scope() as s:
        dev = get_modbus_device_or_404(s, did)
        data = modbus_device_view(dev)
        bound = [monitor_view(m) for m in
                 sorted(dev.monitors, key=lambda m: m.modbus_register_address or 0)]
    data["alive"] = _modbus().status().get(did, False)
    return render_template("modbus_detail.html", device=data, monitors=bound)


@bp.route("/modbus/<int:did>/edit", methods=["GET", "POST"])
def modbus_edit(did: int):
    if request.method == "POST":
        return _save_modbus_device(did)
    return _modbus_form(did)


def _modbus_form(did: int | None):
    data = None
    if did is not None:
        with session_scope() as s:
            data = modbus_device_view(get_modbus_device_or_404(s, did))
    return render_template("modbus_form.html", device=data,
                           word_orders=WORD_ORDERS,
                           default_port=MODBUS_DEFAULT_PORT,
                           min_poll_interval=MIN_POLL_INTERVAL,
                           is_new=did is None)


@bp.route("/modbus/<int:did>/delete", methods=["POST"])
def modbus_delete(did: int):
    _modbus().stop_device(did)
    with session_scope() as s:
        d = s.get(ModbusDevice, did)
        if d:
            log.info("modbus device deleted: %s id=%d (cascaded %d monitor(s))",
                     d.name, did, len(d.monitors))
            s.delete(d)
    flash("Modbus device deleted.", "success")
    return redirect(url_for("web.modbus_list"))


@bp.route("/modbus/<int:did>/toggle", methods=["POST"])
def modbus_toggle(did: int):
    with session_scope() as s:
        d = get_modbus_device_or_404(s, did)
        d.enabled = not d.enabled
        info = modbus_device_view(d)
    if info["enabled"]:
        _modbus().start_device(info["id"], info["name"])
        log.info("modbus device enabled: %s id=%d", info["name"], info["id"])
    else:
        _modbus().stop_device(info["id"])
        log.info("modbus device disabled: %s id=%d", info["name"], info["id"])
    return redirect(url_for("web.modbus_detail", did=info["id"]))


@bp.route("/modbus/<int:did>/poll-now", methods=["POST"])
def modbus_poll_now(did: int):
    regs, err = poll_now(did)
    if err:
        flash(f"Poll failed: {err}", "error")
    else:
        flash(f"Read {len(regs)} register(s): {regs}", "success")
    return redirect(url_for("web.modbus_detail", did=did))


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------

class _FormError(Exception):
    """Validation failure: flashed to the user, who is sent back to the form."""


def _form_str(name: str, default: str = "") -> str:
    return (request.form.get(name) or default).strip()


def _form_int(name: str, default: str | None, error: str) -> int | None:
    """Integer field. A missing/empty field falls back to `default`; with no
    default the field is optional and blank means None."""
    raw = _form_str(name, default or "")
    if raw == "":
        if default is None:
            return None
        raise _FormError(error)  # whitespace-only value for a required field
    try:
        return int(raw)
    except ValueError:
        raise _FormError(error) from None


def _form_float(name: str, error: str) -> float | None:
    """Optional decimal field; blank means None."""
    raw = _form_str(name)
    if raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        raise _FormError(error) from None
    _check(math.isfinite(v), error)
    return v


def _check(ok: bool, error: str) -> None:
    if not ok:
        raise _FormError(error)


def _save_modbus_device(did: int | None):
    try:
        return _save_modbus_device_checked(did)
    except _FormError as e:
        flash(str(e), "error")
        return redirect(request.url)


def _save_modbus_device_checked(did: int | None):
    name = _form_str("name")
    description = _form_str("description") or None
    host = _form_str("host")
    word_order = _form_str("word_order", "CDAB").upper()
    enabled = bool(request.form.get("enabled"))
    auto_create = bool(request.form.get("auto_create_temp_humidity"))

    _check(bool(name), "Name is required.")
    _check(bool(host), "Host is required.")
    _check(word_order in WORD_ORDERS, f"Word order must be one of {', '.join(WORD_ORDERS)}.")
    int_err = "Port, slave id, intervals, and registers must be integers."
    port = _form_int("port", str(MODBUS_DEFAULT_PORT), int_err)
    slave_id = _form_int("slave_id", "0", int_err)
    poll_interval = _form_int("poll_interval_seconds", "10", int_err)
    timeout = _form_int("timeout_seconds", "5", int_err)
    register_start = _form_int("register_start", "0", int_err)
    register_count = _form_int("register_count", "4", int_err)
    _check(1 <= port <= 65535, "Port must be 1-65535.")
    _check(0 <= slave_id <= 255, "Slave id must be 0-255.")
    _check(poll_interval >= MIN_POLL_INTERVAL,
           f"Poll interval must be at least {MIN_POLL_INTERVAL}s.")
    _check(timeout >= 1, "Timeout must be at least 1 second.")
    _check(0 <= register_start <= 0xFFFF, "Register start must be 0-65535.")
    _check(1 <= register_count <= 125, "Register count must be 1-125 (Modbus spec).")

    fields = dict(name=name, description=description, host=host, port=port,
                  slave_id=slave_id, poll_interval_seconds=poll_interval,
                  timeout_seconds=timeout, word_order=word_order,
                  register_start=register_start, register_count=register_count,
                  enabled=enabled)
    with session_scope() as s:
        if did is None:
            d = ModbusDevice(**fields)
            s.add(d)
            s.flush()
            if auto_create:
                _create_temp_humidity_monitors(s, d)
        else:
            d = get_modbus_device_or_404(s, did)
            for k, v in fields.items():
                setattr(d, k, v)
        info = modbus_device_view(d)
        action = "created" if did is None else "updated"
        log.info("modbus device %s: %s id=%d host=%s:%d interval=%ds",
                 action, info["name"], info["id"], info["host"], info["port"],
                 info["poll_interval_seconds"])

    # Restart the poller so config changes (host, interval, register window)
    # take effect immediately rather than after the next cycle.
    if did is not None:
        _modbus().stop_device(info["id"])
    if info["enabled"]:
        _modbus().start_device(info["id"], info["name"])

    flash("Modbus device saved.", "success")
    return redirect(url_for("web.modbus_detail", did=info["id"]))


def _create_temp_humidity_monitors(s, device: ModbusDevice) -> None:
    """Default Monigear-style binding: temperature at register_start (°F)
    and humidity at register_start+2 (%). Two registers each, float32 CDAB."""
    base = device.register_start
    monitors = [
        {
            "name": f"{device.name} Temperature",
            "unit": "°F",
            "address": base,
        },
        {
            "name": f"{device.name} Humidity",
            "unit": "%",
            "address": base + 2,
        },
    ]
    for spec in monitors:
        s.add(Monitor(
            name=spec["name"],
            unit=spec["unit"],
            listener_type="modbus",
            enabled=True,
            modbus_device_id=device.id,
            modbus_register_address=spec["address"],
            modbus_data_type="float32",
            decimal_places=1,
        ))
        log.info("modbus auto-created monitor: %s (device=%s, register=%d)",
                 spec["name"], device.name, spec["address"])


def _validate_modbus_monitor(s, device_id: int | None, address: int | None,
                             data_type: str | None) -> str:
    """Validate a Modbus monitor's binding against its device. Returns ""
    on success or a flashable error message."""
    if not device_id:
        return "Modbus monitors require a Modbus device."
    dev = s.get(ModbusDevice, device_id)
    if not dev:
        return "Selected Modbus device does not exist."
    if address is None:
        return "Register address is required."
    if data_type not in DATA_TYPES:
        return f"Unknown data type: {data_type}"
    n = DATA_TYPE_REGS[data_type]
    end = dev.register_start + dev.register_count
    if address < dev.register_start or address + n > end:
        plural = "s" if n > 1 else ""
        return (f"Register address {address} ({n} register{plural}) falls "
                f"outside the device's read window "
                f"[{dev.register_start}, {end - 1}].")
    return ""


def _save_monitor(mid: int | None):
    try:
        return _save_monitor_checked(mid)
    except _FormError as e:
        flash(str(e), "error")
        return redirect(request.url)


def _save_monitor_checked(mid: int | None):
    cfg = current_app.config["SPOT"]
    name = _form_str("name")
    description = _form_str("description") or None
    unit = _form_str("unit", "value") or "value"
    listener_type = _form_str("listener_type", "http")
    value_regex = _form_str("value_regex") or None
    enabled = bool(request.form.get("enabled"))

    if value_regex:
        try:
            re.compile(value_regex)
        except re.error as e:
            raise _FormError(f"Invalid regex: {e}") from None
    _check(bool(name), "Name is required.")
    _check(listener_type in VALID_LISTENER_TYPES, "Invalid listener type.")

    modbus_device_id: int | None = None
    modbus_register_address: int | None = None
    modbus_data_type: str | None = None
    if listener_type == "modbus":
        modbus_device_id = _form_int("modbus_device_id", None, "Invalid Modbus device.")
        modbus_register_address = _form_int("modbus_register_address", None,
                                            "Register address must be an integer.")
        modbus_data_type = _form_str("modbus_data_type", "float32")

    retention_err = "Retention must be a positive integer (days), or blank for keep-forever."
    retention_days = _form_int("retention_days", None, retention_err)
    _check(retention_days is None or retention_days >= 1, retention_err)

    decimals_err = "Decimal places must be 0–10, or blank for full precision."
    decimal_places = _form_int("decimal_places", None, decimals_err)
    _check(decimal_places is None or 0 <= decimal_places <= 10, decimals_err)

    location = _form_str("location")[:120] or None
    category = _form_str("category")[:60] or None
    color = _form_str("color") or None
    _check(color is None or re.fullmatch(r"#[0-9a-fA-F]{6}", color) is not None,
           "Colour must be a #rrggbb value.")

    alert_high = _form_float("alert_high", "High alert threshold must be a number.")
    alert_low = _form_float("alert_low", "Low alert threshold must be a number.")
    _check(alert_high is None or alert_low is None or alert_low < alert_high,
           "The low alert threshold must be below the high one.")
    window_err = "Alert averaging window must be 0–86400 seconds."
    alert_window = _form_int("alert_window_seconds", None, window_err)
    _check(alert_window is None or 0 <= alert_window <= 86400, window_err)
    stale_err = "No-data alert must be at least 10 seconds, or blank for off."
    alert_stale = _form_int("alert_stale_seconds", None, stale_err)
    _check(alert_stale is None or alert_stale >= 10, stale_err)

    with session_scope() as s:
        if listener_type == "modbus":
            err = _validate_modbus_monitor(
                s, modbus_device_id, modbus_register_address, modbus_data_type)
            _check(not err, err)

        existing_port = get_monitor_or_404(s, mid).port if mid is not None else None
        port = _assign_port(s, mid, listener_type, existing_port, cfg.web_port)

        fields = dict(name=name, description=description, unit=unit,
                      listener_type=listener_type, port=port, enabled=enabled,
                      retention_days=retention_days, value_regex=value_regex,
                      modbus_device_id=modbus_device_id,
                      modbus_register_address=modbus_register_address,
                      modbus_data_type=modbus_data_type,
                      decimal_places=decimal_places,
                      location=location, category=category, color=color,
                      alert_high=alert_high, alert_low=alert_low,
                      alert_window_seconds=alert_window or None,
                      alert_stale_seconds=alert_stale)
        if mid is None:
            m = Monitor(**fields)
            s.add(m)
            s.flush()
        else:
            m = get_monitor_or_404(s, mid)
            for k, v in fields.items():
                setattr(m, k, v)
        info = monitor_view(m)
        action = "created" if mid is None else "updated"
        log.info("monitor %s: %s id=%d type=%s port=%s decimals=%s retention=%s",
                 action, info["name"], info["id"], info["listener_type"], info["port"],
                 info["decimal_places"], info["retention_days"])

    if mid is not None:
        _manager().stop_monitor(info["id"])
    if info["enabled"]:
        _manager().start_monitor(info["id"], info["name"], info["listener_type"],
                                 info["port"], info["value_regex"])

    flash("Monitor saved.", "success")
    return redirect(url_for("web.monitor_detail", mid=info["id"]))


def _assign_port(s, mid: int | None, listener_type: str,
                 existing_port: int | None, web_port: int) -> int | None:
    """TCP/UDP monitors keep their current port if it's still in range,
    otherwise get the next free one. Other listener types have no port."""
    if listener_type not in ("tcp", "udp"):
        return None
    if existing_port is not None and PORT_RANGE_START <= existing_port <= PORT_RANGE_END:
        return existing_port
    taken = {p for (p,) in s.execute(
        select(Monitor.port).where(
            Monitor.port.is_not(None),
            Monitor.id != (mid or 0),
        )
    ).all() if p is not None}
    taken.add(web_port)
    port = allocate_port(listener_type, taken)
    _check(port is not None,
           f"No port available in {PORT_RANGE_START}-{PORT_RANGE_END}; free a monitor first.")
    return port

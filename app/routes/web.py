from __future__ import annotations

import logging
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..db import session_scope
from ..janitor import clear_monitor, effective_retention, purge_monitor
from ..listeners import ListenerManager
from ..models import Monitor
from ..storage import storage_overview
from ..util import format_bytes, get_monitor_or_404, monitor_view

bp = Blueprint("web", __name__)
log = logging.getLogger("spot.web")


VALID_LISTENER_TYPES = ("http", "tcp", "udp")


def _manager() -> ListenerManager:
    return current_app.config["SPOT_LISTENERS"]


@bp.route("/")
def index():
    with session_scope() as s:
        rows = [monitor_view(m) for m in s.query(Monitor).order_by(Monitor.name).all()]
    statuses = _manager().status()
    for r in rows:
        r["listener_alive"] = statuses.get(r["id"], False)
    return render_template("index.html", monitors=rows)


@bp.route("/monitors/new", methods=["GET", "POST"])
def monitor_new():
    if request.method == "POST":
        return _save_monitor(None)
    return render_template("monitor_form.html", monitor=None,
                           listener_types=VALID_LISTENER_TYPES,
                           default_retention=current_app.config["SPOT"].default_retention_days)


@bp.route("/monitors/<int:mid>/edit", methods=["GET", "POST"])
def monitor_edit(mid: int):
    if request.method == "POST":
        return _save_monitor(mid)
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    return render_template("monitor_form.html", monitor=data,
                           listener_types=VALID_LISTENER_TYPES,
                           default_retention=current_app.config["SPOT"].default_retention_days)


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
        _manager().start_monitor(info["id"], info["name"], info["listener_type"], info["port"])
        log.info("monitor enabled: %s id=%d", info["name"], info["id"])
    else:
        _manager().stop_monitor(info["id"])
        log.info("monitor disabled: %s id=%d", info["name"], info["id"])
    return redirect(url_for("web.index"))


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
    return render_template("monitor_detail.html", monitor=data)


@bp.route("/monitors/<int:mid>/dashboard")
def monitor_dashboard(mid: int):
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    return render_template("dashboard.html", monitor=data)


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
    return render_template("storage.html", overview=overview)


@bp.route("/overlay")
def overlay():
    with session_scope() as s:
        rows = [monitor_view(m) for m in s.query(Monitor).order_by(Monitor.name).all()]
    return render_template("overlay.html", monitors=rows)


def _save_monitor(mid: int | None):
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    unit = (request.form.get("unit") or "value").strip() or "value"
    listener_type = (request.form.get("listener_type") or "http").strip()
    port_raw = (request.form.get("port") or "").strip()
    retention_raw = (request.form.get("retention_days") or "").strip()
    enabled = bool(request.form.get("enabled"))

    if not name:
        flash("Name is required.", "error")
        return redirect(request.url)
    if listener_type not in VALID_LISTENER_TYPES:
        flash("Invalid listener type.", "error")
        return redirect(request.url)

    port: int | None = None
    if listener_type in ("tcp", "udp"):
        if not port_raw:
            flash("Port is required for TCP/UDP listeners.", "error")
            return redirect(request.url)
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            flash("Port must be an integer 1-65535.", "error")
            return redirect(request.url)

    retention_days: int | None = None
    if retention_raw:
        try:
            retention_days = int(retention_raw)
            if retention_days < 1:
                raise ValueError
        except ValueError:
            flash("Retention must be a positive integer (days), or blank for keep-forever.", "error")
            return redirect(request.url)

    with session_scope() as s:
        if mid is None:
            m = Monitor(name=name, description=description, unit=unit,
                        listener_type=listener_type, port=port, enabled=enabled,
                        retention_days=retention_days)
            s.add(m)
            s.flush()
        else:
            m = get_monitor_or_404(s, mid)
            m.name = name
            m.description = description
            m.unit = unit
            m.listener_type = listener_type
            m.port = port
            m.enabled = enabled
            m.retention_days = retention_days
        info = monitor_view(m)
        action = "created" if mid is None else "updated"
        log.info("monitor %s: %s id=%d type=%s port=%s",
                 action, info["name"], info["id"], info["listener_type"], info["port"])

    if mid is not None:
        _manager().stop_monitor(info["id"])
    if info["enabled"]:
        _manager().start_monitor(info["id"], info["name"], info["listener_type"], info["port"])

    flash("Monitor saved.", "success")
    return redirect(url_for("web.monitor_detail", mid=info["id"]))

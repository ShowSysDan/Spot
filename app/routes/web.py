from __future__ import annotations

import logging
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ..db import session_scope
from ..listeners import ListenerManager
from ..models import Monitor
from ..util import get_monitor_or_404, monitor_view

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
                           listener_types=VALID_LISTENER_TYPES)


@bp.route("/monitors/<int:mid>/edit", methods=["GET", "POST"])
def monitor_edit(mid: int):
    if request.method == "POST":
        return _save_monitor(mid)
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    return render_template("monitor_form.html", monitor=data,
                           listener_types=VALID_LISTENER_TYPES)


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
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
    data["listener_alive"] = _manager().status().get(mid, False)
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


def _save_monitor(mid: int | None):
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    unit = (request.form.get("unit") or "value").strip() or "value"
    listener_type = (request.form.get("listener_type") or "http").strip()
    port_raw = (request.form.get("port") or "").strip()
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

    with session_scope() as s:
        if mid is None:
            m = Monitor(name=name, description=description, unit=unit,
                        listener_type=listener_type, port=port, enabled=enabled)
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

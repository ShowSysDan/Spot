from __future__ import annotations

import logging
import re
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select

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
from ..models import ModbusDevice, Monitor
from ..portalloc import PORT_RANGE_END, PORT_RANGE_START, allocate_port
from ..storage import storage_overview
from ..util import (
    format_bytes,
    format_local,
    get_modbus_device_or_404,
    get_monitor_or_404,
    modbus_device_view,
    monitor_view,
)

bp = Blueprint("web", __name__)
log = logging.getLogger("spot.web")


VALID_LISTENER_TYPES = ("http", "tcp", "udp", "modbus")


def _safe_next() -> str | None:
    """Same-site redirect target from a form's hidden `next` field, if any."""
    nxt = request.form.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else None


def _manager() -> ListenerManager:
    return current_app.config["SPOT_LISTENERS"]


def _modbus() -> ModbusManager:
    return current_app.config["SPOT_MODBUS"]


# A monitor whose newest reading is older than this is shown as "stale" on
# the home page. Modbus monitors use 3x their device's poll interval instead,
# since Spot knows exactly how often those should report.
STALE_AFTER_SECONDS = 300


@bp.route("/")
def index():
    with session_scope() as s:
        rows = []
        for m in s.query(Monitor).order_by(Monitor.name).all():
            v = monitor_view(m)
            if m.modbus_device is not None:
                v["stale_after"] = max(30, 3 * m.modbus_device.poll_interval_seconds)
            else:
                v["stale_after"] = STALE_AFTER_SECONDS
            rows.append(v)
    statuses = _manager().status()
    for r in rows:
        r["listener_alive"] = statuses.get(r["id"], False)
    return render_template("index.html", monitors=rows)


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
    return render_template("monitor_form.html", monitor=data,
                           listener_types=VALID_LISTENER_TYPES,
                           default_retention=current_app.config["SPOT"].default_retention_days,
                           modbus_devices=devices,
                           modbus_data_types=DATA_TYPES)


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
        m["oldest_local"] = format_local(m["oldest"])
        m["newest_local"] = format_local(m["newest"])
    return render_template("storage.html", overview=overview)


@bp.route("/reports")
def reports():
    """Compare monitors on one chart and export PDF/CSV reports."""
    with session_scope() as s:
        rows = [monitor_view(m) for m in s.query(Monitor).order_by(Monitor.name).all()]
    return render_template("reports.html", monitors=rows)


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
                      decimal_places=decimal_places)
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

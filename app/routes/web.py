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


def _manager() -> ListenerManager:
    return current_app.config["SPOT_LISTENERS"]


def _modbus() -> ModbusManager:
    return current_app.config["SPOT_MODBUS"]


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
    with session_scope() as s:
        devices = [modbus_device_view(d) for d in
                   s.query(ModbusDevice).order_by(ModbusDevice.name).all()]
    return render_template("monitor_form.html", monitor=None,
                           listener_types=VALID_LISTENER_TYPES,
                           default_retention=current_app.config["SPOT"].default_retention_days,
                           modbus_devices=devices,
                           modbus_data_types=DATA_TYPES)


@bp.route("/monitors/<int:mid>/edit", methods=["GET", "POST"])
def monitor_edit(mid: int):
    if request.method == "POST":
        return _save_monitor(mid)
    with session_scope() as s:
        data = monitor_view(get_monitor_or_404(s, mid))
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
        m["oldest_local"] = format_local(m["oldest"])
        m["newest_local"] = format_local(m["newest"])
    return render_template("storage.html", overview=overview)


@bp.route("/overlay")
def overlay():
    with session_scope() as s:
        rows = [monitor_view(m) for m in s.query(Monitor).order_by(Monitor.name).all()]
    return render_template("overlay.html", monitors=rows)


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
    return render_template("modbus_form.html", device=None,
                           word_orders=WORD_ORDERS,
                           default_port=MODBUS_DEFAULT_PORT,
                           min_poll_interval=MIN_POLL_INTERVAL,
                           is_new=True)


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
    with session_scope() as s:
        data = modbus_device_view(get_modbus_device_or_404(s, did))
    return render_template("modbus_form.html", device=data,
                           word_orders=WORD_ORDERS,
                           default_port=MODBUS_DEFAULT_PORT,
                           min_poll_interval=MIN_POLL_INTERVAL,
                           is_new=False)


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


def _save_modbus_device(did: int | None):
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    host = (request.form.get("host") or "").strip()
    port_raw = (request.form.get("port") or str(MODBUS_DEFAULT_PORT)).strip()
    slave_raw = (request.form.get("slave_id") or "0").strip()
    interval_raw = (request.form.get("poll_interval_seconds") or "10").strip()
    timeout_raw = (request.form.get("timeout_seconds") or "5").strip()
    word_order = (request.form.get("word_order") or "CDAB").strip().upper()
    register_start_raw = (request.form.get("register_start") or "0").strip()
    register_count_raw = (request.form.get("register_count") or "4").strip()
    enabled = bool(request.form.get("enabled"))
    auto_create = bool(request.form.get("auto_create_temp_humidity"))

    if not name:
        flash("Name is required.", "error")
        return redirect(request.url)
    if not host:
        flash("Host is required.", "error")
        return redirect(request.url)
    if word_order not in WORD_ORDERS:
        flash(f"Word order must be one of {', '.join(WORD_ORDERS)}.", "error")
        return redirect(request.url)
    try:
        port = int(port_raw)
        slave_id = int(slave_raw)
        poll_interval = int(interval_raw)
        timeout = int(timeout_raw)
        register_start = int(register_start_raw)
        register_count = int(register_count_raw)
    except ValueError:
        flash("Port, slave id, intervals, and registers must be integers.", "error")
        return redirect(request.url)
    if not 1 <= port <= 65535:
        flash("Port must be 1-65535.", "error")
        return redirect(request.url)
    if not 0 <= slave_id <= 255:
        flash("Slave id must be 0-255.", "error")
        return redirect(request.url)
    if poll_interval < MIN_POLL_INTERVAL:
        flash(f"Poll interval must be at least {MIN_POLL_INTERVAL}s.", "error")
        return redirect(request.url)
    if timeout < 1:
        flash("Timeout must be at least 1 second.", "error")
        return redirect(request.url)
    if not 0 <= register_start <= 0xFFFF:
        flash("Register start must be 0-65535.", "error")
        return redirect(request.url)
    if not 1 <= register_count <= 125:
        flash("Register count must be 1-125 (Modbus spec).", "error")
        return redirect(request.url)

    with session_scope() as s:
        if did is None:
            d = ModbusDevice(name=name, description=description, host=host,
                             port=port, slave_id=slave_id,
                             poll_interval_seconds=poll_interval,
                             timeout_seconds=timeout, word_order=word_order,
                             register_start=register_start,
                             register_count=register_count, enabled=enabled)
            s.add(d)
            s.flush()
            if auto_create:
                _create_temp_humidity_monitors(s, d)
        else:
            d = get_modbus_device_or_404(s, did)
            d.name = name
            d.description = description
            d.host = host
            d.port = port
            d.slave_id = slave_id
            d.poll_interval_seconds = poll_interval
            d.timeout_seconds = timeout
            d.word_order = word_order
            d.register_start = register_start
            d.register_count = register_count
            d.enabled = enabled
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
    cfg = current_app.config["SPOT"]
    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    unit = (request.form.get("unit") or "value").strip() or "value"
    listener_type = (request.form.get("listener_type") or "http").strip()
    retention_raw = (request.form.get("retention_days") or "").strip()
    value_regex = (request.form.get("value_regex") or "").strip() or None
    enabled = bool(request.form.get("enabled"))

    if value_regex:
        try:
            re.compile(value_regex)
        except re.error as e:
            flash(f"Invalid regex: {e}", "error")
            return redirect(request.url)

    if not name:
        flash("Name is required.", "error")
        return redirect(request.url)
    if listener_type not in VALID_LISTENER_TYPES:
        flash("Invalid listener type.", "error")
        return redirect(request.url)

    modbus_device_id: int | None = None
    modbus_register_address: int | None = None
    modbus_data_type: str | None = None
    if listener_type == "modbus":
        raw_device_id = (request.form.get("modbus_device_id") or "").strip()
        try:
            modbus_device_id = int(raw_device_id) if raw_device_id else None
        except ValueError:
            flash("Invalid Modbus device.", "error")
            return redirect(request.url)
        raw_addr = (request.form.get("modbus_register_address") or "").strip()
        try:
            modbus_register_address = int(raw_addr) if raw_addr != "" else None
        except ValueError:
            flash("Register address must be an integer.", "error")
            return redirect(request.url)
        modbus_data_type = (request.form.get("modbus_data_type") or "float32").strip()

    # Look up the current port so we can keep it on edit if still valid.
    existing_port: int | None = None
    if mid is not None:
        with session_scope() as s:
            existing = get_monitor_or_404(s, mid)
            existing_port = existing.port

    port: int | None = None
    if listener_type in ("tcp", "udp"):
        in_range = (existing_port is not None and
                    PORT_RANGE_START <= existing_port <= PORT_RANGE_END)
        if in_range:
            port = existing_port
        else:
            with session_scope() as s:
                taken = {p for (p,) in s.execute(
                    select(Monitor.port).where(
                        Monitor.port.is_not(None),
                        Monitor.id != (mid or 0),
                    )
                ).all() if p is not None}
            taken.add(cfg.web_port)
            port = allocate_port(listener_type, taken)
            if port is None:
                flash(f"No port available in {PORT_RANGE_START}-{PORT_RANGE_END}; "
                      "free a monitor first.", "error")
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
        if listener_type == "modbus":
            err = _validate_modbus_monitor(
                s, modbus_device_id, modbus_register_address, modbus_data_type)
            if err:
                flash(err, "error")
                return redirect(request.url)

        if mid is None:
            m = Monitor(name=name, description=description, unit=unit,
                        listener_type=listener_type, port=port, enabled=enabled,
                        retention_days=retention_days, value_regex=value_regex,
                        modbus_device_id=modbus_device_id,
                        modbus_register_address=modbus_register_address,
                        modbus_data_type=modbus_data_type)
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
            m.value_regex = value_regex
            m.modbus_device_id = modbus_device_id
            m.modbus_register_address = modbus_register_address
            m.modbus_data_type = modbus_data_type
        info = monitor_view(m)
        action = "created" if mid is None else "updated"
        log.info("monitor %s: %s id=%d type=%s port=%s",
                 action, info["name"], info["id"], info["listener_type"], info["port"])

    if mid is not None:
        _manager().stop_monitor(info["id"])
    if info["enabled"] and info["listener_type"] in ("tcp", "udp"):
        _manager().start_monitor(info["id"], info["name"], info["listener_type"],
                                 info["port"], info["value_regex"])

    flash("Monitor saved.", "success")
    return redirect(url_for("web.monitor_detail", mid=info["id"]))

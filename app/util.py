from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import abort
from sqlalchemy.orm import Session

from .models import ModbusDevice, Monitor


def parse_iso_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; assume UTC if naive; accept trailing 'Z'."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a UTC-stored datetime to the server's local timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()  # uses system local TZ (TZ env or /etc/localtime)


def format_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S %Z") -> str:
    local = to_local(dt)
    if local is None:
        return "—"
    return local.strftime(fmt).strip()


def naive_local(dt: datetime | None) -> datetime | None:
    """As to_local() but drops the tzinfo, so matplotlib renders the values verbatim."""
    local = to_local(dt)
    return local.replace(tzinfo=None) if local else None


def get_monitor_or_404(s: Session, mid: int) -> Monitor:
    m = s.get(Monitor, mid)
    if not m:
        abort(404)
    return m


def get_modbus_device_or_404(s: Session, did: int) -> ModbusDevice:
    d = s.get(ModbusDevice, did)
    if not d:
        abort(404)
    return d


def modbus_device_view(d: ModbusDevice) -> dict[str, Any]:
    return {
        "id": d.id,
        "name": d.name,
        "description": d.description,
        "host": d.host,
        "port": d.port,
        "slave_id": d.slave_id,
        "poll_interval_seconds": d.poll_interval_seconds,
        "timeout_seconds": d.timeout_seconds,
        "word_order": d.word_order,
        "register_start": d.register_start,
        "register_count": d.register_count,
        "enabled": d.enabled,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "monitor_count": len(d.monitors),
    }


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def monitor_view(m: Monitor) -> dict[str, Any]:
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "unit": m.unit,
        "listener_type": m.listener_type,
        "port": m.port,
        "auth_token": m.auth_token,
        "enabled": m.enabled,
        "retention_days": m.retention_days,
        "value_regex": m.value_regex,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "modbus_device_id": m.modbus_device_id,
        "modbus_register_address": m.modbus_register_address,
        "modbus_data_type": m.modbus_data_type,
        "modbus_device_name": m.modbus_device.name if m.modbus_device else None,
        "decimal_places": m.decimal_places,
    }


def format_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"

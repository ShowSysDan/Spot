from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo
from typing import Any

from flask import abort
from sqlalchemy.orm import Session

from .models import ModbusDevice, Monitor

# Series colours, tuned for the dark theme. A monitor without its own colour
# gets PALETTE[id % len(PALETTE)], so it looks the same on every page and PDF.
PALETTE: tuple[str, ...] = ("#58a6ff", "#f85149", "#56d364", "#bc8cff",
                            "#e3b341", "#39c5cf", "#ff7b72", "#ec6cb9")


def monitor_color(monitor_id: int, color: str | None) -> str:
    return color or PALETTE[monitor_id % len(PALETTE)]


def parse_iso_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; assume UTC if naive; accept trailing 'Z'."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Every time a person sees — pages, PDFs, CSVs, e-mails — is shown in this
# zone, 24-hour. Storage and the wire format stay UTC. Set from
# SPOT_TIMEZONE by create_app(); America/New_York switches EST/EDT itself.
DEFAULT_TIMEZONE = "America/New_York"
_display_tz: tzinfo = ZoneInfo(DEFAULT_TIMEZONE)


def set_display_timezone(name: str) -> None:
    global _display_tz
    _display_tz = ZoneInfo(name)


def display_tz() -> tzinfo:
    return _display_tz


def now_local() -> datetime:
    return datetime.now(_display_tz)


def to_local(dt: datetime | None) -> datetime | None:
    """Convert a UTC-stored datetime to the display timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_tz)


def iso_local(dt: datetime | None) -> str | None:
    """ISO-8601 in the display zone with its offset, e.g. 2026-10-24T17:39:24-04:00.
    Unambiguous for machines, readable for people; used by the external API."""
    local = to_local(dt)
    return local.isoformat() if local else None


def local_naive_to_utc(dt: datetime) -> datetime:
    """Interpret a naive wall-clock time as display-timezone time; return UTC."""
    return dt.replace(tzinfo=_display_tz).astimezone(timezone.utc)


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
        "location": m.location,
        "color": m.color,
        "display_color": monitor_color(m.id, m.color),
        "alert_high": m.alert_high,
        "alert_low": m.alert_low,
        "alert_window_seconds": m.alert_window_seconds,
        "alert_stale_seconds": m.alert_stale_seconds,
        "alerting": (m.alert_high is not None or m.alert_low is not None
                     or bool(m.alert_stale_seconds)),
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

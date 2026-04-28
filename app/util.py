from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import abort
from sqlalchemy.orm import Session

from .models import Monitor


def parse_iso_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp; assume UTC if naive; accept trailing 'Z'."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def get_monitor_or_404(s: Session, mid: int) -> Monitor:
    m = s.get(Monitor, mid)
    if not m:
        abort(404)
    return m


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

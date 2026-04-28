from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import delete, select

from .db import session_scope
from .models import Monitor, Reading

log = logging.getLogger("spot.janitor")


def get_default_retention() -> int | None:
    """Read the global default from app config, if a Flask app is bound."""
    try:
        cfg = current_app.config["SPOT"]
        return cfg.default_retention_days
    except RuntimeError:
        return None  # outside of app context


def effective_retention(monitor_retention: int | None,
                        default_retention: int | None = None) -> int | None:
    if monitor_retention and monitor_retention > 0:
        return int(monitor_retention)
    if default_retention and default_retention > 0:
        return int(default_retention)
    return None


def purge_monitor(monitor_id: int, monitor_name: str, days: int) -> int:
    """Delete readings for a monitor older than `days`. Returns rows deleted."""
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as s:
        result = s.execute(
            delete(Reading)
            .where(Reading.monitor_id == monitor_id)
            .where(Reading.ts < cutoff)
        )
        deleted = int(result.rowcount or 0)
    if deleted:
        log.info("purged %d rows monitor=%s retention=%dd cutoff=%s",
                 deleted, monitor_name, days, cutoff.isoformat())
    return deleted


def clear_monitor(monitor_id: int, monitor_name: str) -> int:
    """Delete ALL readings for a monitor. Returns rows deleted."""
    with session_scope() as s:
        result = s.execute(delete(Reading).where(Reading.monitor_id == monitor_id))
        deleted = int(result.rowcount or 0)
    log.warning("cleared all data: %d rows monitor=%s", deleted, monitor_name)
    return deleted


def run_once(default_days: int | None = None) -> int:
    """One cleanup pass for every monitor. Uses per-monitor retention or the default."""
    total = 0
    with session_scope() as s:
        targets = s.execute(
            select(Monitor.id, Monitor.name, Monitor.retention_days)
        ).all()
    for mid, name, days in targets:
        eff = effective_retention(days, default_days)
        if not eff:
            continue
        try:
            total += purge_monitor(mid, name, eff)
        except Exception:
            log.exception("janitor error monitor=%s", name)
    return total


class JanitorThread(threading.Thread):
    def __init__(self, interval_seconds: int, default_days: int | None):
        super().__init__(daemon=True, name="spot-janitor")
        self.interval = max(60, int(interval_seconds))
        self.default_days = default_days
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("janitor started: interval=%ds default_retention=%s",
                 self.interval,
                 f"{self.default_days}d" if self.default_days else "none")
        if self._stop.wait(timeout=10):
            return
        while not self._stop.is_set():
            try:
                run_once(self.default_days)
            except Exception:
                log.exception("janitor pass failed")
            if self._stop.wait(timeout=self.interval):
                break
        log.info("janitor stopped")

    def stop(self) -> None:
        self._stop.set()

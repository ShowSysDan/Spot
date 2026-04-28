from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from .db import session_scope
from .models import Monitor, Reading

log = logging.getLogger("spot.janitor")


def purge_monitor(monitor_id: int, monitor_name: str, retention_days: int) -> int:
    """Delete readings for a single monitor older than retention_days. Returns rows deleted."""
    if not retention_days or retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with session_scope() as s:
        result = s.execute(
            delete(Reading)
            .where(Reading.monitor_id == monitor_id)
            .where(Reading.ts < cutoff)
        )
        deleted = int(result.rowcount or 0)
    if deleted:
        log.info("purged %d rows monitor=%s retention=%dd cutoff=%s",
                 deleted, monitor_name, retention_days, cutoff.isoformat())
    return deleted


def run_once() -> int:
    """Run one cleanup pass for every monitor with a retention policy."""
    total = 0
    with session_scope() as s:
        targets = s.execute(
            select(Monitor.id, Monitor.name, Monitor.retention_days)
            .where(Monitor.retention_days.is_not(None))
        ).all()
    for mid, name, days in targets:
        try:
            total += purge_monitor(mid, name, int(days))
        except Exception:
            log.exception("janitor error monitor=%s", name)
    return total


class JanitorThread(threading.Thread):
    """Periodic retention enforcer. One global instance."""

    def __init__(self, interval_seconds: int):
        super().__init__(daemon=True, name="spot-janitor")
        self.interval = max(60, int(interval_seconds))
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("janitor started: interval=%ds", self.interval)
        # Run once shortly after startup, then on the configured interval.
        if self._stop.wait(timeout=10):
            return
        while not self._stop.is_set():
            try:
                run_once()
            except Exception:
                log.exception("janitor pass failed")
            if self._stop.wait(timeout=self.interval):
                break
        log.info("janitor stopped")

    def stop(self) -> None:
        self._stop.set()

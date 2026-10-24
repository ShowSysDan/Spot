"""Threshold and no-data alerting.

A single `AlertEngine` thread wakes every `SPOT_ALERT_INTERVAL_SECONDS` and,
for each enabled monitor with alerting configured, works out:

  * the value to judge — the average over the last `alert_window_seconds`,
    or the latest reading when no window is set;
  * how long since the newest reading.

and compares them with `alert_high`, `alert_low` and `alert_stale_seconds`.
Each crossing opens one `Alert` row (and notifies); recovery closes it (and
notifies again). While open, the row tracks the most extreme value seen.
Averaging over a window is what keeps a single loud clap from paging anyone.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from .config import Config
from .db import session_scope
from .models import Alert, Monitor, Reading
from .notify import post_json
from .util import iso_local

log = logging.getLogger("spot.alerts")

KINDS = ("high", "low", "stale")


@dataclass
class AlertEvent:
    """What happened, captured inside the DB session for notifying after it."""
    action: str  # raised | cleared
    alert_id: int
    kind: str
    monitor_id: int
    monitor_name: str
    unit: str
    location: str | None
    value: float | None
    threshold: float | None
    started_at: datetime
    cleared_at: datetime | None
    message: str


def describe(kind: str, unit: str, value: float | None, threshold: float | None,
             stale_seconds: int | None = None, window: int | None = None) -> str:
    over = f" ({window}s average)" if window else ""
    if kind == "high":
        return f"{_num(value)} {unit}{over} is above the {_num(threshold)} {unit} limit"
    if kind == "low":
        return f"{_num(value)} {unit}{over} is below the {_num(threshold)} {unit} limit"
    return f"no data for more than {_duration(stale_seconds or 0)}"


def _num(v: float | None) -> str:
    return "—" if v is None else f"{v:.4g}"


def _duration(sec: int) -> str:
    if sec % 3600 == 0 and sec >= 3600:
        return f"{sec // 3600} h"
    if sec % 60 == 0 and sec >= 60:
        return f"{sec // 60} min"
    return f"{sec} s"


def _alerting_configured():
    return or_(Monitor.alert_high.is_not(None), Monitor.alert_low.is_not(None),
               Monitor.alert_stale_seconds.is_not(None))


def evaluate(now: datetime | None = None) -> list[AlertEvent]:
    """Run one evaluation pass. Returns the raise/clear events it produced."""
    now = now or datetime.now(timezone.utc)
    events: list[AlertEvent] = []
    with session_scope() as s:
        open_alerts: dict[int, dict[str, Alert]] = {}
        for a in s.scalars(select(Alert).where(Alert.cleared_at.is_(None))):
            open_alerts.setdefault(a.monitor_id, {})[a.kind] = a

        monitors = s.scalars(
            select(Monitor).where(Monitor.enabled.is_(True), _alerting_configured())
        ).all()
        judged = {m.id for m in monitors}

        for m in monitors:
            latest = s.execute(
                select(Reading.ts, Reading.value)
                .where(Reading.monitor_id == m.id, Reading.value.is_not(None))
                .order_by(Reading.ts.desc()).limit(1)
            ).first()
            window = m.alert_window_seconds or 0
            if window > 0:
                value = s.execute(
                    select(func.avg(Reading.value))
                    .where(Reading.monitor_id == m.id, Reading.value.is_not(None),
                           Reading.ts >= now - timedelta(seconds=window))
                ).scalar()
                value = float(value) if value is not None else None
            else:
                value = latest.value if latest else None

            last_seen = latest.ts if latest else m.created_at
            conditions = {
                "high": value is not None and m.alert_high is not None and value > m.alert_high,
                "low": value is not None and m.alert_low is not None and value < m.alert_low,
                "stale": bool(m.alert_stale_seconds) and
                         (now - last_seen).total_seconds() > m.alert_stale_seconds,
            }
            thresholds = {"high": m.alert_high, "low": m.alert_low, "stale": None}
            current = open_alerts.get(m.id, {})
            location = m.location

            for kind in KINDS:
                active = current.get(kind)
                hit = conditions[kind]
                if hit and active is None:
                    msg = describe(kind, m.unit, value, thresholds[kind],
                                   m.alert_stale_seconds, window)
                    a = Alert(monitor_id=m.id, kind=kind, started_at=now,
                              threshold=thresholds[kind], trigger_value=value,
                              peak_value=value, message=msg)
                    s.add(a)
                    s.flush()
                    events.append(_event("raised", a, m, location, value))
                elif hit and active is not None:
                    if value is not None and kind in ("high", "low"):
                        pick = max if kind == "high" else min
                        active.peak_value = (value if active.peak_value is None
                                             else pick(active.peak_value, value))
                elif not hit and active is not None:
                    # A high/low alert only clears on a real reading back in
                    # range; "no data" is the stale alert's job.
                    if kind != "stale" and value is None:
                        continue
                    active.cleared_at = now
                    events.append(_event("cleared", active, m, location, value))

        # Monitors that were disabled or had alerting switched off: close any
        # leftovers quietly so they don't sit "active" forever.
        for mid, by_kind in open_alerts.items():
            if mid in judged:
                continue
            for a in by_kind.values():
                a.cleared_at = now
                a.message = (a.message or "") + " (alerting turned off)"
    return events


def _event(action: str, a: Alert, m: Monitor, location: str | None,
           value: float | None) -> AlertEvent:
    if action == "cleared":
        message = ("data is arriving again" if a.kind == "stale" else
                   f"back in range at {_num(value)} {m.unit} "
                   f"(limit {_num(a.threshold)} {m.unit}, worst {_num(a.peak_value)} {m.unit})")
    else:
        message = a.message or ""
    return AlertEvent(action=action, alert_id=a.id, kind=a.kind, monitor_id=m.id,
                      monitor_name=m.name, unit=m.unit, location=location, value=value,
                      threshold=a.threshold, started_at=a.started_at,
                      cleared_at=a.cleared_at, message=message)


def notify(cfg: Config, ev: AlertEvent) -> None:
    """Deliver one alert event to the log (syslog) and the webhook, if configured."""
    where = f" [{ev.location}]" if ev.location else ""
    head = "ALERT" if ev.action == "raised" else "RECOVERED"
    summary = f"{head}: {ev.monitor_name}{where} {ev.kind} — {ev.message}"
    if ev.action == "raised":
        log.warning(summary)
    else:
        log.info(summary)

    link = f"{cfg.public_url}/monitors/{ev.monitor_id}/dashboard" if cfg.public_url else ""
    if cfg.alert_webhook_url:
        post_json(cfg.alert_webhook_url, {
            "event": f"alert.{ev.action}",
            "alert_id": ev.alert_id,
            "kind": ev.kind,
            "message": ev.message,
            "monitor": {"id": ev.monitor_id, "name": ev.monitor_name,
                        "unit": ev.unit, "location": ev.location},
            "value": ev.value,
            "threshold": ev.threshold,
            "started_at": iso_local(ev.started_at),
            "cleared_at": iso_local(ev.cleared_at),
            "url": link or None,
        })


class AlertEngine(threading.Thread):
    def __init__(self, cfg: Config):
        super().__init__(daemon=True, name="spot-alerts")
        self.cfg = cfg
        self.interval = cfg.alert_interval_seconds
        # Not `_stop`: that would shadow Thread._stop() on Python <= 3.12.
        self._stop_event = threading.Event()

    def run(self) -> None:
        log.info("alert engine started: interval=%ds webhook=%s",
                 self.interval,
                 "on" if self.cfg.alert_webhook_url else "off")
        # Give listeners a moment to deliver fresh data before the first pass,
        # so a restart doesn't raise a burst of spurious "stale" alerts.
        if self._stop_event.wait(timeout=max(30, self.interval)):
            return
        while not self._stop_event.is_set():
            try:
                for ev in evaluate():
                    try:
                        notify(self.cfg, ev)
                    except Exception:  # one bad notification mustn't drop the rest
                        log.exception("alert notification failed alert_id=%s", ev.alert_id)
            except Exception:
                log.exception("alert pass failed")
            if self._stop_event.wait(timeout=self.interval):
                break
        log.info("alert engine stopped")

    def stop(self) -> None:
        self._stop_event.set()


def open_alert_count() -> int:
    """Active, unacknowledged alerts (for the nav badge)."""
    with session_scope() as s:
        return s.execute(
            select(func.count(Alert.id))
            .where(Alert.cleared_at.is_(None), Alert.acknowledged_at.is_(None))
        ).scalar_one()


def active_alerts_by_monitor() -> dict[int, list[dict]]:
    with session_scope() as s:
        rows = s.scalars(select(Alert).where(Alert.cleared_at.is_(None))).all()
        out: dict[int, list[dict]] = {}
        for a in rows:
            out.setdefault(a.monitor_id, []).append({
                "id": a.id, "kind": a.kind, "message": a.message,
                "started_at": a.started_at.isoformat(),
                "acknowledged": a.acknowledged_at is not None,
            })
    return out

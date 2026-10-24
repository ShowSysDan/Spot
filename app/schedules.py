"""Scheduled e-mail reports.

Each `ReportSchedule` sends one PDF per period to its recipients. The period
always ends at a display-timezone midnight before the send time, so a daily
report sent at 07:00 covers yesterday 00:00–24:00, never a partial day:

  daily    yesterday
  weekly   the 7 days before the send day (send on Monday → last Mon–Sun)
  monthly  the previous calendar month (sent on the 1st)

`next_run_at` is stored (UTC) and recomputed after every run, so a restart
neither skips nor repeats a report; one missed while Spot was down is sent
once when it comes back.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from .config import Config
from .db import session_scope
from .exports import render_overlay_pdf
from .models import ReportSchedule
from .notify import send_email
from .queries import PDF_MAX_POINTS, collect_overlay
from .util import display_tz, format_local, safe_filename, to_local

log = logging.getLogger("spot.schedules")

CADENCES = ("daily", "weekly", "monthly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _at(d: date, hour: int = 0, minute: int = 0) -> datetime:
    """Wall-clock time on `d` in the display zone, as an aware datetime."""
    return datetime.combine(d, time(hour, minute), tzinfo=display_tz())


def next_run(sched: ReportSchedule, after: datetime) -> datetime:
    """First send time strictly after `after` (returned in UTC)."""
    local = to_local(after)
    d = local.date()
    for _ in range(400):  # at most ~13 months of days to scan
        if sched.cadence == "daily":
            ok = True
        elif sched.cadence == "weekly":
            ok = d.weekday() == sched.weekday
        else:
            ok = d.day == 1
        if ok:
            cand = _at(d, sched.send_hour, sched.send_minute)
            if cand > local:
                return cand.astimezone(timezone.utc)
        d += timedelta(days=1)
    raise RuntimeError("no next run found")  # unreachable for valid schedules


def period_for(sched: ReportSchedule, run_at: datetime) -> tuple[datetime, datetime]:
    """The [start, end) range the run at `run_at` reports on (UTC)."""
    run_day = to_local(run_at).date()
    if sched.cadence == "daily":
        start_day = run_day - timedelta(days=1)
    elif sched.cadence == "weekly":
        start_day = run_day - timedelta(days=7)
    else:
        prev = run_day.replace(day=1) - timedelta(days=1)
        start_day = prev.replace(day=1)
        run_day = run_day.replace(day=1)
    return (_at(start_day).astimezone(timezone.utc),
            _at(run_day).astimezone(timezone.utc))


def describe(sched: ReportSchedule) -> str:
    at = f"{sched.send_hour:02d}:{sched.send_minute:02d}"
    if sched.cadence == "daily":
        return f"Every day at {at} — yesterday"
    if sched.cadence == "weekly":
        return f"Every {WEEKDAYS[sched.weekday]} at {at} — previous 7 days"
    return f"1st of each month at {at} — previous month"


def schedule_view(sched: ReportSchedule) -> dict:
    return {
        "id": sched.id, "name": sched.name, "cadence": sched.cadence,
        "send_hour": sched.send_hour, "send_minute": sched.send_minute,
        "send_time": f"{sched.send_hour:02d}:{sched.send_minute:02d}",
        "weekday": sched.weekday, "email_to": sched.email_to, "enabled": sched.enabled,
        "describe": describe(sched),
        "next_run": format_local(sched.next_run_at, "%a %Y-%m-%d %H:%M") if sched.enabled and sched.next_run_at else "—",
        "last_run": format_local(sched.last_run_at, "%a %Y-%m-%d %H:%M") if sched.last_run_at else "never",
        "last_status": sched.last_status,
        "monitor_ids": [m.id for m in sched.monitors],
        "monitor_names": [m.name for m in sched.monitors],
    }


def run_schedule(cfg: Config, schedule_id: int, run_at: datetime | None = None,
                 advance: bool = True) -> str | None:
    """Build and e-mail one report. `run_at` picks the period (default: now →
    the most recently completed period). `advance` moves next_run_at on.

    Returns None on success or an error message.
    """
    now = datetime.now(timezone.utc)
    run_at = run_at or now
    try:
        err, label, name, recipients = _build_and_send(cfg, schedule_id, run_at)
    except Exception as e:  # always record the run and move the schedule on
        log.exception("scheduled report crashed schedule_id=%d", schedule_id)
        err, label, name, recipients = f"internal error: {e.__class__.__name__}: {e}", "?", f"#{schedule_id}", []
    if err == "schedule not found":
        return err
    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is not None:
            sched.last_run_at = now
            sched.last_status = f"failed: {err}" if err else f"sent ({label})"
            if advance:
                sched.next_run_at = next_run(sched, now)
    if err:
        log.error("scheduled report failed: %s err=%s", name, err)
    else:
        log.info("scheduled report sent: %s period=%s to=%s", name, label, recipients)
    return err


def _build_and_send(cfg: Config, schedule_id: int, run_at: datetime):
    """Returns (error or None, period label, schedule name, recipients)."""
    with session_scope() as s:
        sched = s.get(ReportSchedule, schedule_id)
        if sched is None:
            return "schedule not found", "", "", []
        start, end = period_for(sched, run_at)
        # Periods are [start, end): stop just short of the closing midnight so
        # a reading at exactly 00:00 belongs to the next report only.
        end_incl = end - timedelta(microseconds=1)
        ids = [m.id for m in sched.monitors]
        name = sched.name
        desc = describe(sched)
        recipients = [x.strip() for x in sched.email_to.split(",") if x.strip()]
        label = (f"{format_local(start, '%a %Y-%m-%d')}" if sched.cadence == "daily" else
                 f"{format_local(start, '%Y-%m-%d')} – {format_local(end - timedelta(seconds=1), '%Y-%m-%d')}")
        data = collect_overlay(s, ids, start, end_incl, PDF_MAX_POINTS,
                               with_summary=True, extended=True) if ids else []
    if not ids:
        err = "no monitors selected"
    else:
        pdf = render_overlay_pdf(start, end_incl, data, title=f"{name} — {label}",
                                 extra_meta=[("Schedule", desc)])
        lines = [f"{name} — {label}", ""]
        for m in data:
            st = m["summary"]
            line = (f"{m['name']}: min {_n(st['min'])}  max {_n(st['max'])}  "
                    f"avg {_n(st['avg'])} {m['unit']}")
            if st.get("leq") is not None:
                line += f"  Leq {_n(st['leq'])}"
            lines.append(line + f"  ({st['count']:,} samples)")
        lines += ["", "The full report is attached as a PDF."]
        fname = f"spot_{safe_filename(name)}_{to_local(start):%Y%m%d}.pdf"
        err = send_email(cfg, recipients, f"[Spot] {name} — {label}", "\n".join(lines),
                         [(fname, pdf, "application/pdf")])
    return err, label, name, recipients


def _n(v) -> str:
    return "—" if v is None else f"{v:.1f}"


def run_due(cfg: Config) -> int:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        due = [(x.id, x.next_run_at) for x in s.scalars(
            select(ReportSchedule).where(ReportSchedule.enabled.is_(True),
                                         ReportSchedule.next_run_at <= now))]
    for sid, run_at in due:
        # Report on the period belonging to the *scheduled* time, so a
        # report caught up after downtime still covers the right day.
        try:
            run_schedule(cfg, sid, run_at=run_at)
        except Exception:
            log.exception("scheduled report pass failed schedule_id=%d", sid)
    return len(due)


class ReportScheduler(threading.Thread):
    INTERVAL = 30

    def __init__(self, cfg: Config):
        super().__init__(daemon=True, name="spot-schedules")
        self.cfg = cfg
        # Not `_stop`: that would shadow Thread._stop() on Python <= 3.12.
        self._stop_event = threading.Event()

    def run(self) -> None:
        log.info("report scheduler started")
        while not self._stop_event.wait(timeout=self.INTERVAL):
            try:
                run_due(self.cfg)
            except Exception:
                log.exception("scheduled report pass failed")
        log.info("report scheduler stopped")

    def stop(self) -> None:
        self._stop_event.set()

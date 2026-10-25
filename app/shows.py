"""Shows: named time ranges with a set of monitors, plus their reports.

A show can be created in the UI or by an external system (321Theater) via
/api/v1/shows. Once a show has ended — plus `SHOW_REPORT_DELAY_SECONDS` so
late readings make it in — the `ShowReporter` thread POSTs its PDF report
to `callback_url`, retrying with backoff.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Config
from .db import session_scope
from .exports import render_overlay_pdf
from .models import Show
from .notify import callback_url_problem, post_multipart
from .detect import HOLD as DETECT_HOLD, DetectOptions, pdf_meta, resolve_range
from .queries import PDF_MAX_POINTS, collect_overlay, compute_summary
from .util import format_local, iso_local, monitor_color, safe_filename, to_local

log = logging.getLogger("spot.shows")

# Wait this long after a show ends before sending its report, so readings
# that arrive a little late (buffered sensors, slow pollers) are included.
SHOW_REPORT_DELAY_SECONDS = 300
MAX_DELIVERY_ATTEMPTS = 6
# Retry backoff: 1, 2, 4, 8, 16 minutes …
RETRY_BASE_SECONDS = 60
MAX_SHOW_SECONDS = 7 * 86400


def show_status(show: Show, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now < show.starts_at:
        return "upcoming"
    if now <= show.ends_at:
        return "live"
    return "finished"


def show_view(show: Show, base_url: str = "") -> dict:
    """JSON/template-friendly description of a show (no statistics)."""
    return {
        "id": show.id,
        "name": show.name,
        "external_id": show.external_id,
        "source": show.source,
        "starts_at": iso_local(show.starts_at),
        "ends_at": iso_local(show.ends_at),
        "starts_local": format_local(show.starts_at, "%a %Y-%m-%d %H:%M"),
        "ends_local": format_local(show.ends_at, "%H:%M" if
                                   to_local(show.starts_at).date() == to_local(show.ends_at).date()
                                   else "%a %Y-%m-%d %H:%M"),
        "duration_minutes": round((show.ends_at - show.starts_at).total_seconds() / 60),
        "status": show_status(show),
        "notes": show.notes,
        "callback_url": show.callback_url,
        "delivered_at": iso_local(show.delivered_at),
        "delivery_status": show.delivery_status,
        "delivery_attempts": show.delivery_attempts,
        "monitors": [{"id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
                      "color": monitor_color(m.id, m.color)} for m in show.monitors],
        "monitor_ids": [m.id for m in show.monitors],
        "auto_detect": show.auto_detect,
        "detect": {"window_minutes": show.detect_window_minutes,
                   "min_step": show.detect_min_step,
                   "monitor": show.detect_monitor_id},
        "report_pdf_url": f"{base_url}/api/v1/shows/{show.id}/report.pdf",
        "spot_url": f"{base_url}/shows/{show.id}",
    }


def show_detect_options(show: Show) -> DetectOptions | None:
    if not show.auto_detect:
        return None
    return DetectOptions(window_minutes=show.detect_window_minutes,
                         min_step=show.detect_min_step,
                         monitor_id=show.detect_monitor_id)


Range = tuple[datetime, datetime, "dict | None"]


def show_range(s: Session, show: Show) -> Range:
    """The range a show's statistics and report cover: the detected actual
    start/end when `auto_detect` is on, else the scheduled times. Third
    value: the detection result (None when off)."""
    return resolve_range(s, [m.id for m in show.monitors], show.starts_at, show.ends_at,
                         show_detect_options(show))


def show_stats(s: Session, show: Show, rng: Range | None = None) -> list[dict]:
    """Per-monitor extended statistics for the show's (possibly detected) range."""
    start, end, _ = rng or show_range(s, show)
    return [{
        "id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
        "stats": compute_summary(s, m.id, start, end, extended=True, unit=m.unit),
    } for m in show.monitors]


def show_report_pdf(s: Session, show: Show, rng: Range | None = None) -> bytes:
    start, end, detection = rng or show_range(s, show)
    monitors = collect_overlay(s, [m.id for m in show.monitors], start, end,
                               PDF_MAX_POINTS, with_summary=True, extended=True,
                               with_events=True)
    extra = []  # the show's name is already the heading
    if show.external_id:
        extra.append(("Ref", show.external_id))
    locations = sorted({m.location for m in show.monitors if m.location})
    if locations:
        extra.append(("Location", ", ".join(locations)))
    extra += pdf_meta(detection)
    return render_overlay_pdf(start, end, monitors, title=show.name, extra_meta=extra)


def report_filename(show: Show) -> str:
    return f"spot_show_{safe_filename(show.name)}_{to_local(show.starts_at):%Y%m%dT%H%M}.pdf"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def report_due_at(show: Show) -> datetime:
    """When the report may go out: the grace period after the scheduled end,
    plus — when detecting — long enough for the end search to see the real
    end, which can be up to `detect_window_minutes` late."""
    extra = timedelta(minutes=show.detect_window_minutes, seconds=DETECT_HOLD) if show.auto_detect else timedelta(0)
    return show.ends_at + timedelta(seconds=SHOW_REPORT_DELAY_SECONDS) + extra


def _due_shows(s: Session, now: datetime) -> list[Show]:
    rows = s.scalars(
        select(Show).where(
            Show.delivered_at.is_(None),
            Show.callback_url.is_not(None),
            Show.ends_at <= now - timedelta(seconds=SHOW_REPORT_DELAY_SECONDS),
            Show.delivery_attempts < MAX_DELIVERY_ATTEMPTS,
            or_(Show.next_attempt_at.is_(None), Show.next_attempt_at <= now),
        ).order_by(Show.ends_at).limit(50)
    ).all()
    return [sh for sh in rows if report_due_at(sh) <= now][:20]


def deliver_due(cfg: Config, now: datetime | None = None) -> int:
    """Send every report that is due. Returns how many shows were handled.

    Each show is isolated: one that fails (even with an unexpected error)
    is recorded as a failed attempt and the rest still go out.
    """
    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        ids = [sh.id for sh in _due_shows(s, now)]
    for sid in ids:
        try:
            deliver_show(cfg, sid, now)
        except Exception:
            log.exception("show report delivery crashed show_id=%d", sid)
    return len(ids)


def deliver_show(cfg: Config, show_id: int, now: datetime | None = None,
                 manual: bool = False) -> str | None:
    """Build and send one show's report. Returns None on success or the error.

    `manual` (the *Send now* button / API `deliver`): a successful send
    before the report is due — i.e. before or during the show — is a
    preview. It is noted, but doesn't count as delivery, so the real
    post-show report still goes out automatically.
    """
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []
    try:
        with session_scope() as s:
            show = s.get(Show, show_id)
            if show is None:
                return "show not found"
            callback = show.callback_url
            if not callback:
                return "show has no callback URL"
            early = now < report_due_at(show)
            view = show_view(show, cfg.public_url)
            rng = show_range(s, show)
            stats = show_stats(s, show, rng)
            pdf = show_report_pdf(s, show, rng)
            detection = rng[2]
            fname = report_filename(show)

        problem = callback_url_problem(callback, cfg)
        if problem:
            errors.append(f"callback: {problem}")
        else:
            metadata = {"event": "show.report", "show": view, "monitors": stats,
                        "report_start": iso_local(rng[0]), "report_end": iso_local(rng[1]),
                        "detection": detection, "final": not early}
            err = post_multipart(callback, {"metadata": json.dumps(metadata, default=str)},
                                 [("report", fname, pdf, "application/pdf")])
            if err:
                errors.append(f"callback: {err}")
    except Exception as e:  # never leave a show stuck without recording the attempt
        log.exception("show report build/send failed show_id=%d", show_id)
        errors.append(f"internal error: {e.__class__.__name__}: {e}")
        early = False

    with session_scope() as s:
        show = s.get(Show, show_id)
        if show is None:
            return "show not found"
        if manual and early and not errors:
            show.delivery_status = (f"preview sent {format_local(now, '%H:%M')}; "
                                    "the final report still goes out after the show")
            log.info("show report preview sent show=%s", show.name)
            return None
        show.delivery_attempts += 1
        if errors:
            show.delivery_status = "; ".join(errors)
            delay = RETRY_BASE_SECONDS * 2 ** (show.delivery_attempts - 1)
            show.next_attempt_at = now + timedelta(seconds=delay)
            give_up = show.delivery_attempts >= MAX_DELIVERY_ATTEMPTS
            log.warning("show report delivery failed show=%s attempt=%d%s err=%s",
                        show.name, show.delivery_attempts,
                        " (giving up)" if give_up else "", show.delivery_status)
        else:
            show.delivered_at = now
            show.delivery_status = "delivered"
            show.next_attempt_at = None
            log.info("show report delivered show=%s callback=%s", show.name, callback)
    return "; ".join(errors) or None


class ShowReporter(threading.Thread):
    """Checks once a minute for finished shows whose report is due."""

    INTERVAL = 60

    def __init__(self, cfg: Config):
        super().__init__(daemon=True, name="spot-show-reports")
        self.cfg = cfg
        # Not `_stop`: that would shadow Thread._stop() on Python <= 3.12.
        self._stop_event = threading.Event()

    def run(self) -> None:
        log.info("show report sender started")
        while not self._stop_event.wait(timeout=self.INTERVAL):
            try:
                deliver_due(self.cfg)
            except Exception:
                log.exception("show report pass failed")
        log.info("show report sender stopped")

    def stop(self) -> None:
        self._stop_event.set()

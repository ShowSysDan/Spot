"""Shows: named time ranges with a set of monitors, plus their reports.

A show can be created in the UI or by an external system (321Theater) via
/api/v1/shows. Once a show has ended — plus `SHOW_REPORT_DELAY_SECONDS` so
late readings make it in — the `ShowReporter` thread e-mails its PDF report
to `email_to` and/or POSTs it to `callback_url`, retrying with backoff.
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
from .notify import post_multipart, send_email
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
        "email_to": show.email_to,
        "callback_url": show.callback_url,
        "delivered_at": iso_local(show.delivered_at),
        "delivery_status": show.delivery_status,
        "delivery_attempts": show.delivery_attempts,
        "monitors": [{"id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
                      "color": monitor_color(m.id, m.color)} for m in show.monitors],
        "monitor_ids": [m.id for m in show.monitors],
        "report_pdf_url": f"{base_url}/api/v1/shows/{show.id}/report.pdf",
        "spot_url": f"{base_url}/shows/{show.id}",
    }


def show_stats(s: Session, show: Show) -> list[dict]:
    """Per-monitor extended statistics for the show's time range."""
    return [{
        "id": m.id, "name": m.name, "unit": m.unit, "location": m.location,
        "stats": compute_summary(s, m.id, show.starts_at, show.ends_at,
                                 extended=True, unit=m.unit),
    } for m in show.monitors]


def show_report_pdf(s: Session, show: Show) -> bytes:
    monitors = collect_overlay(s, [m.id for m in show.monitors], show.starts_at,
                               show.ends_at, PDF_MAX_POINTS, with_summary=True,
                               extended=True)
    extra = [("Show", show.name)]
    if show.external_id:
        extra.append(("Ref", show.external_id))
    locations = sorted({m.location for m in show.monitors if m.location})
    if locations:
        extra.append(("Location", ", ".join(locations)))
    return render_overlay_pdf(show.starts_at, show.ends_at, monitors,
                              title=show.name, extra_meta=extra)


def report_filename(show: Show) -> str:
    return f"spot_show_{safe_filename(show.name)}_{to_local(show.starts_at):%Y%m%dT%H%M}.pdf"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _due_shows(s: Session, now: datetime) -> list[Show]:
    return s.scalars(
        select(Show).where(
            Show.delivered_at.is_(None),
            or_(Show.email_to.is_not(None), Show.callback_url.is_not(None)),
            Show.ends_at <= now - timedelta(seconds=SHOW_REPORT_DELAY_SECONDS),
            Show.delivery_attempts < MAX_DELIVERY_ATTEMPTS,
            or_(Show.next_attempt_at.is_(None), Show.next_attempt_at <= now),
        ).order_by(Show.ends_at).limit(20)
    ).all()


def deliver_due(cfg: Config, now: datetime | None = None) -> int:
    """Send every report that is due. Returns how many shows were handled."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        ids = [sh.id for sh in _due_shows(s, now)]
    for sid in ids:
        deliver_show(cfg, sid, now)
    return len(ids)


def deliver_show(cfg: Config, show_id: int, now: datetime | None = None) -> str | None:
    """Build and send one show's report. Returns None on success or the error."""
    now = now or datetime.now(timezone.utc)
    with session_scope() as s:
        show = s.get(Show, show_id)
        if show is None:
            return "show not found"
        base = cfg.public_url
        view = show_view(show, base)
        stats = show_stats(s, show)
        pdf = show_report_pdf(s, show)
        fname = report_filename(show)
        email_to = [x.strip() for x in (show.email_to or "").split(",") if x.strip()]
        callback = show.callback_url

    errors: list[str] = []
    if email_to:
        err = send_email(cfg, email_to, f"[Spot] Show report: {view['name']}",
                         _email_body(view, stats), [(fname, pdf, "application/pdf")])
        if err:
            errors.append(f"e-mail: {err}")
    if callback:
        metadata = {"event": "show.report", "show": view, "monitors": stats}
        err = post_multipart(callback, {"metadata": json.dumps(metadata, default=str)},
                             [("report", fname, pdf, "application/pdf")])
        if err:
            errors.append(f"callback: {err}")

    with session_scope() as s:
        show = s.get(Show, show_id)
        if show is None:
            return "show not found"
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
            log.info("show report delivered show=%s email=%s callback=%s",
                     show.name, bool(email_to), bool(callback))
    return "; ".join(errors) or None


def _email_body(view: dict, stats: list[dict]) -> str:
    lines = [f"{view['name']}", f"{view['starts_local']} – {view['ends_local']}", ""]
    for m in stats:
        st = m["stats"]
        line = (f"{m['name']}: min {_n(st['min'])}  max {_n(st['max'])}  "
                f"avg {_n(st['avg'])} {m['unit']}")
        if st.get("leq") is not None:
            line += f"  Leq {_n(st['leq'])}  L10 {_n(st['l10'])}  L90 {_n(st['l90'])}"
        lines.append(line + f"  ({st['count']:,} samples)")
    if view["spot_url"].startswith("http"):
        lines += ["", f"Details: {view['spot_url']}"]
    lines += ["", "The full report is attached as a PDF."]
    return "\n".join(lines)


def _n(v) -> str:
    return "—" if v is None else f"{v:.1f}"


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

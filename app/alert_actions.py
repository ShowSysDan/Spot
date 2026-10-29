"""Alert actions: a UDP, TCP or HTTP request sent when an alert is raised
(its condition becomes true) and, optionally, another when it clears.

Payloads and HTTP URLs may contain placeholders, filled in per event:

    {state}      raised | cleared          {on}         1 when raised, 0 when cleared
    {monitor}    monitor name              {monitor_id} monitor id
    {location}   monitor location          {unit}       monitor unit
    {kind}       high | low | stale        {alert_id}   alert id
    {value}      value judged              {threshold}  limit crossed
    {message}    human-readable summary    {time}       event time (ISO, local)

Unknown names are left as written, so JSON bodies need no escaping. In URLs
the values are percent-encoded. TCP/UDP payloads also understand \\r, \\n,
\\t, \\0, \\\\ and \\xHH, for devices that want a CR terminator or raw bytes.

Sends run on their own `ActionDispatcher` thread, so a slow or dead device
never holds up the alert engine. An HTTP send succeeds on a 2xx response
(the status code is kept either way); TCP succeeds once the bytes are
written, UDP once the datagram is sent. A failed send is tried again
`retries` times, `retry_delay_seconds` apart. If the alert changes state
while a retry is pending, the stale request is dropped in favour of the new
one — a device should never get "raised" after "cleared".
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import or_, select

from .db import session_scope
from .models import AlertAction
from .notify import http_request
from .util import iso_local

log = logging.getLogger("spot.alerts")

PROTOCOLS = ("http", "tcp", "udp")
HTTP_METHODS = ("POST", "PUT", "GET")
SEND_TIMEOUT = 5

PLACEHOLDERS = ("state", "on", "monitor", "monitor_id", "location", "unit", "kind",
                "alert_id", "value", "threshold", "message", "time")

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|[rnt0\\])")
_SIMPLE_ESCAPES = {"r": b"\r", "n": b"\n", "t": b"\t", "0": b"\0", "\\": b"\\"}


@dataclass
class Result:
    """Outcome of one send. `detail` is e.g. "HTTP 200", "sent 12 bytes", or
    the error; `skipped` means the action has nothing to send in that state."""
    ok: bool
    detail: str
    skipped: bool = False


@dataclass(order=True)
class Job:
    due: float
    action_id: int = field(compare=False)
    state: str = field(compare=False)
    alert_id: int = field(compare=False)
    ctx: dict = field(compare=False)
    attempt: int = field(compare=False, default=0)


def context_for(ev) -> dict[str, str]:
    """Placeholder values for an AlertEvent."""
    def num(v):
        return "" if v is None else format(v, ".6g")
    return {
        "state": ev.action,
        "on": "1" if ev.action == "raised" else "0",
        "monitor": ev.monitor_name,
        "monitor_id": str(ev.monitor_id),
        "location": ev.location or "",
        "unit": ev.unit,
        "kind": ev.kind,
        "alert_id": str(ev.alert_id),
        "value": num(ev.value),
        "threshold": num(ev.threshold),
        "message": ev.message,
        "time": iso_local(ev.cleared_at if ev.action == "cleared" else ev.started_at) or "",
    }


def render(template: str, ctx: dict[str, str], quote: bool = False) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in ctx:
            return m.group(0)
        v = ctx[m.group(1)]
        return urllib.parse.quote(v, safe="") if quote else v
    return _PLACEHOLDER.sub(sub, template)


def to_bytes(text: str) -> bytes:
    """UTF-8 encode `text`, turning backslash escapes into the bytes they name."""
    out: list[bytes] = []
    pos = 0
    for m in _ESCAPE.finditer(text):
        out.append(text[pos:m.start()].encode("utf-8"))
        code = m.group(1)
        out.append(bytes([int(code[1:], 16)]) if code[0] == "x" else _SIMPLE_ESCAPES[code])
        pos = m.end()
    out.append(text[pos:].encode("utf-8"))
    return b"".join(out)


def _send_udp(host: str, port: int, data: bytes) -> Result:
    try:
        family, kind, proto, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
        with socket.socket(family, kind, proto) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(data, addr)
    except OSError as e:
        return Result(False, str(e) or e.__class__.__name__)
    return Result(True, f"sent {len(data)} bytes")


def _send_tcp(host: str, port: int, data: bytes) -> Result:
    try:
        with socket.create_connection((host, port), timeout=SEND_TIMEOUT) as sock:
            sock.sendall(data)
    except OSError as e:
        return Result(False, str(e) or e.__class__.__name__)
    return Result(True, f"sent {len(data)} bytes")


def send(a: AlertAction, state: str, ctx: dict[str, str]) -> Result:
    """Send action `a` once for `state` (raised | cleared)."""
    payload = (a.raise_payload if state == "raised" else a.clear_payload) or ""
    if a.protocol == "http":
        url = (a.raise_url if state == "raised" else a.clear_url) or ""
        if not url:
            return Result(True, "nothing to send", skipped=True)
        body = render(payload, ctx)
        status, err = http_request(render(url, ctx, quote=True), a.http_method,
                                   body.encode("utf-8") if body and a.http_method != "GET" else None,
                                   timeout=SEND_TIMEOUT)
        if err:
            return Result(False, err)
        return Result(True, f"HTTP {status}")
    if not payload:
        return Result(True, "nothing to send", skipped=True)
    data = to_bytes(render(payload, ctx))
    if a.protocol == "udp":
        return _send_udp(a.host or "", a.port or 0, data)
    return _send_tcp(a.host or "", a.port or 0, data)


def target(a: AlertAction, state: str = "raised") -> str:
    """Short description of where the action sends, for the UI and log."""
    if a.protocol == "http":
        return f"{a.http_method} {(a.raise_url if state == 'raised' else a.clear_url) or '—'}"
    return f"{a.protocol}://{a.host}:{a.port}"


def sample_context(a: AlertAction, state: str) -> dict[str, str]:
    """Made-up placeholder values for the Test button."""
    now = iso_local(datetime.now(timezone.utc)) or ""
    name = a.monitor.name if a.monitor else "Test monitor"
    return {
        "state": state, "on": "1" if state == "raised" else "0",
        "monitor": name, "monitor_id": str(a.monitor_id or 0),
        "location": (a.monitor.location or "") if a.monitor else "",
        "unit": a.monitor.unit if a.monitor else "dB",
        "kind": a.kind or "high", "alert_id": "0",
        "value": "99", "threshold": "95",
        "message": f"test {state} from Spot", "time": now,
    }


def record(action_id: int, status: str) -> None:
    with session_scope() as s:
        a = s.get(AlertAction, action_id)
        if a:
            a.last_sent_at = datetime.now(timezone.utc)
            a.last_status = status


class ActionDispatcher(threading.Thread):
    """Sends alert actions off the alert engine's thread, with retries."""

    def __init__(self):
        super().__init__(daemon=True, name="spot-alert-actions")
        self._jobs: list[Job] = []
        self._cond = threading.Condition()
        self._stopping = False

    def submit(self, ev) -> int:
        """Queue every enabled action that matches AlertEvent `ev`."""
        with session_scope() as s:
            ids = s.scalars(
                select(AlertAction.id).where(
                    AlertAction.enabled.is_(True),
                    or_(AlertAction.monitor_id.is_(None), AlertAction.monitor_id == ev.monitor_id),
                    or_(AlertAction.kind.is_(None), AlertAction.kind == ev.kind),
                ).order_by(AlertAction.id)
            ).all()
        if not ids:
            return 0
        ctx = context_for(ev)
        now = time.monotonic()
        with self._cond:
            # A pending retry for the same alert is out of date now.
            self._jobs = [j for j in self._jobs
                          if not (j.alert_id == ev.alert_id and j.action_id in ids)]
            self._jobs.extend(Job(now, aid, ev.action, ev.alert_id, ctx) for aid in ids)
            self._jobs.sort()
            self._cond.notify()
        return len(ids)

    def run(self) -> None:
        while True:
            with self._cond:
                while not self._stopping and (
                        not self._jobs or self._jobs[0].due > time.monotonic()):
                    wait = self._jobs[0].due - time.monotonic() if self._jobs else None
                    self._cond.wait(timeout=wait)
                if self._stopping:
                    return
                job = self._jobs.pop(0)
            try:
                self._attempt(job)
            except Exception:  # never let one job kill the thread
                log.exception("alert action job failed action_id=%d", job.action_id)

    def _attempt(self, job: Job) -> None:
        with session_scope() as s:
            a = s.get(AlertAction, job.action_id)
            if a is None or not a.enabled:
                return
            s.expunge(a)
        res = send(a, job.state, job.ctx)
        if res.skipped:
            return
        tries = f" (attempt {job.attempt + 1}/{a.retries + 1})" if a.retries else ""
        if res.ok:
            log.info("alert action %s: %s %s alert_id=%d → %s%s", a.name, job.state,
                     target(a, job.state), job.alert_id, res.detail, tries)
            record(a.id, f"{job.state}: {res.detail}")
            return
        retry = job.attempt < a.retries
        log.error("alert action %s: %s %s alert_id=%d failed → %s%s%s", a.name, job.state,
                  target(a, job.state), job.alert_id, res.detail, tries,
                  f"; retrying in {a.retry_delay_seconds}s" if retry else "")
        record(a.id, f"{job.state}: failed — {res.detail}{tries}"
                     + ("; retrying" if retry else ""))
        if retry:
            job.attempt += 1
            job.due = time.monotonic() + a.retry_delay_seconds
            with self._cond:
                self._jobs.append(job)
                self._jobs.sort()
                self._cond.notify()

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()

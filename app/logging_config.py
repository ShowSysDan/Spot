"""Logging: where Spot's log lines go (stderr/journal, syslog, an in-memory
"recent" buffer for the Setup → Logging page) and how much each part of
Spot logs.

Settings come from `.env` (SPOT_SYSLOG_*, SPOT_LOG_LEVEL) at start-up and can
then be changed live on Setup → Logging; changes made there are stored in
the database and win over `.env` (Reset puts `.env` back in charge).

Categories group Spot's loggers so an operator can say "log every API call"
or "ingest: warnings only" without knowing logger names:

  alerts    spot.alerts                     alert raised / recovered
  api       spot.api.access, spot.api.v1    one line per /api/v1 request; shows created/changed via API
  security  spot.security                   rejected API keys, refused cross-site posts, ingest allow-list
  ingest    spot.ingest, spot.api           malformed data (warning); every accepted reading (debug)
  ui        spot.web                        changes made in the web UI, with the source IP
  reports   spot.shows, spot.notify         show report delivery, webhooks / callbacks
  inputs    spot.listeners, spot.modbus, …  TCP/UDP listeners, Modbus polling
  exports   spot.data                       CSV / PDF downloads
  system    spot, spot.db, spot.janitor, …  start-up, database, retention
"""
from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import select
import socket
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .config import Config

_FACILITY_MAP = {
    "kern": logging.handlers.SysLogHandler.LOG_KERN,
    "user": logging.handlers.SysLogHandler.LOG_USER,
    "mail": logging.handlers.SysLogHandler.LOG_MAIL,
    "daemon": logging.handlers.SysLogHandler.LOG_DAEMON,
    "auth": logging.handlers.SysLogHandler.LOG_AUTH,
    "syslog": logging.handlers.SysLogHandler.LOG_SYSLOG,
    "lpr": logging.handlers.SysLogHandler.LOG_LPR,
    "news": logging.handlers.SysLogHandler.LOG_NEWS,
    "uucp": logging.handlers.SysLogHandler.LOG_UUCP,
    "cron": logging.handlers.SysLogHandler.LOG_CRON,
    "local0": logging.handlers.SysLogHandler.LOG_LOCAL0,
    "local1": logging.handlers.SysLogHandler.LOG_LOCAL1,
    "local2": logging.handlers.SysLogHandler.LOG_LOCAL2,
    "local3": logging.handlers.SysLogHandler.LOG_LOCAL3,
    "local4": logging.handlers.SysLogHandler.LOG_LOCAL4,
    "local5": logging.handlers.SysLogHandler.LOG_LOCAL5,
    "local6": logging.handlers.SysLogHandler.LOG_LOCAL6,
    "local7": logging.handlers.SysLogHandler.LOG_LOCAL7,
}
FACILITIES = tuple(_FACILITY_MAP)

# Level names offered in the UI, mapped to logging levels. "off" silences a
# category completely.
LEVELS = {"off": logging.CRITICAL + 10, "error": logging.ERROR, "warning": logging.WARNING,
          "info": logging.INFO, "debug": logging.DEBUG}
_SEVERITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3,
             logging.CRITICAL: 2}


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    description: str
    loggers: tuple[str, ...]
    default: str  # used when SPOT_LOG_LEVEL is INFO; see default_levels()


CATEGORIES: tuple[Category, ...] = (
    Category("alerts", "Alerts",
             "Alert raised (warning) and recovered (info); notification failures.",
             ("spot.alerts",), "info"),
    Category("api", "API requests",
             "One line per /api/v1 call — method, path, status, time taken, API key name and "
             "source IP — plus shows created, changed or deleted through the API.",
             ("spot.api.access", "spot.api.v1"), "info"),
    Category("security", "Security",
             "Rejected or disabled API keys, refused cross-site form posts, ingest from "
             "addresses outside SPOT_INGEST_ALLOW.",
             ("spot.security",), "info"),
    Category("ingest", "Data ingest",
             "Malformed payloads at warning. At debug, every accepted reading (very busy).",
             ("spot.ingest", "spot.api"), "warning"),
    Category("ui", "Changes in the web UI",
             "Monitors, shows, Modbus devices, API keys, alert acknowledgements and settings "
             "created, changed or deleted — with the source IP.",
             ("spot.web",), "info"),
    Category("reports", "Show reports & webhooks",
             "Show report deliveries to callback URLs, alert webhook posts, and their failures.",
             ("spot.shows", "spot.notify"), "info"),
    Category("inputs", "Listeners & Modbus",
             "TCP/UDP listeners starting, stopping or failing to bind; Modbus polling errors.",
             ("spot.listeners", "spot.modbus", "spot.portalloc"), "info"),
    Category("exports", "Exports",
             "CSV and PDF downloads (what and which range).",
             ("spot.data",), "info"),
    Category("system", "System",
             "Start-up and shutdown, database schema, retention purges.",
             ("spot", "spot.db", "spot.janitor", "app"), "info"),
)
CATEGORY_KEYS = tuple(c.key for c in CATEGORIES)


@dataclass
class LogSettings:
    syslog_enabled: bool
    syslog_address: str            # "/dev/log" or "host:port"
    syslog_protocol: str           # udp | tcp (remote only)
    syslog_facility: str
    syslog_tag: str
    levels: dict[str, str] = field(default_factory=dict)  # category key -> LEVELS key

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str, fallback: "LogSettings") -> "LogSettings":
        data = json.loads(raw)
        base = asdict(fallback)
        base.update({k: v for k, v in data.items() if k in base})
        levels = dict(fallback.levels)
        levels.update({k: v for k, v in (data.get("levels") or {}).items()
                       if k in CATEGORY_KEYS and v in LEVELS})
        base["levels"] = levels
        return cls(**base)


def default_levels(env_level: str) -> dict[str, str]:
    """Per-category defaults. SPOT_LOG_LEVEL=DEBUG/WARNING/ERROR shifts every
    'info' category to that level; ingest stays at warning unless DEBUG."""
    env = env_level.lower()
    env = env if env in LEVELS else "info"
    out = {}
    for c in CATEGORIES:
        if c.default == "info":
            out[c.key] = env
        else:
            out[c.key] = "debug" if env == "debug" else c.default
    return out


def env_settings(cfg: Config) -> LogSettings:
    return LogSettings(
        syslog_enabled=bool(cfg.syslog_address),
        syslog_address=cfg.syslog_address,
        syslog_protocol=cfg.syslog_protocol,
        syslog_facility=cfg.syslog_facility.lower(),
        syslog_tag=cfg.syslog_tag,
        levels=default_levels(cfg.log_level),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

class _RequestSource(logging.Filter):
    """Adds `src` — the client IP when the line is logged while handling a
    web request (UI change, API call), else '' — so log lines say who."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "src"):
            src = ""
            try:
                from flask import has_request_context, request
                if has_request_context():
                    src = request.remote_addr or ""
            except Exception:
                pass
            record.src = src
        record.src_suffix = f" [src={record.src}]" if record.src else ""
        return True


class SyslogSender(logging.Handler):
    """Syslog over a local socket (/dev/log), UDP or TCP.

    Writes RFC 3164 lines — `<PRI>Mmm dd HH:MM:SS host tag[pid]: message`
    — so remote collectors get a timestamp and hostname, with TCP lines
    newline-framed (what rsyslog/syslog-ng expect). Unlike the standard
    library handler it reconnects once when a TCP or socket connection has
    dropped (e.g. the syslog server restarted), and keeps counters and the
    last error for the Setup → Logging page instead of printing tracebacks.
    """

    MAX_BYTES = 8192

    def __init__(self, address: str, protocol: str, facility: str, tag: str):
        super().__init__()
        self.address = address
        self.protocol = protocol if protocol in ("udp", "tcp") else "udp"
        self.facility = _FACILITY_MAP.get(facility, _FACILITY_MAP["local0"])
        self.tag = (tag or "spot").replace(" ", "_")[:32]
        self.hostname = socket.gethostname().split(".")[0] or "-"
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_sent_at: datetime | None = None
        self._sock: socket.socket | None = None
        self._target = None
        self._local = address.startswith("/")
        self._connect()  # raises OSError if the destination is unusable

    def describe(self) -> str:
        if self._local:
            return f"local socket {self.address}"
        return f"{self.protocol.upper()} {self.address}"

    def _connect(self) -> None:
        if self._local:
            if not os.path.exists(self.address):
                raise OSError(f"{self.address} does not exist (is rsyslog / journald running?)")
            last = None
            for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                s = socket.socket(socket.AF_UNIX, kind)
                try:
                    s.connect(self.address)
                    self._sock, self._sock_kind = s, kind
                    return
                except OSError as e:
                    last = e
                    s.close()
            raise last or OSError("cannot connect")
        host, _, port = self.address.rpartition(":")
        if not host or not port.isdigit():
            raise OSError("remote syslog address must look like host:port (e.g. loghost:514)")
        host = host.strip("[]")
        if self.protocol == "tcp":
            self._sock = socket.create_connection((host, int(port)), timeout=5)
            self._sock.settimeout(5)
            self._sock_kind = socket.SOCK_STREAM
        else:
            info = socket.getaddrinfo(host, int(port), type=socket.SOCK_DGRAM)[0]
            self._sock = socket.socket(info[0], socket.SOCK_DGRAM)
            self._target = info[4]
            self._sock_kind = socket.SOCK_DGRAM

    def _frame(self, record: logging.LogRecord) -> bytes:
        from .util import now_local  # display zone, like everything else Spot shows
        pri = self.facility * 8 + _SEVERITY.get(record.levelno, 6 if record.levelno < 30 else 3)
        ts = now_local().strftime("%b %d %H:%M:%S")
        ts = ts[:4] + (" " + ts[5] if ts[4] == "0" else ts[4:6]) + ts[6:]  # "Oct  4" day padding
        msg = self.format(record).replace("\n", " ")
        line = f"<{pri}>{ts} {self.hostname} {self.tag}[{os.getpid()}]: {msg}"
        data = line.encode("utf-8", "replace")[: self.MAX_BYTES]
        return data + b"\n" if self._sock_kind == socket.SOCK_STREAM else data

    def _peer_closed(self) -> bool:
        """True if a stream connection was closed by the collector.

        A write after the far end has gone still "succeeds" locally the
        first time, silently losing that line. Collectors never send us
        anything, so a readable socket means it was closed (EOF / reset).
        """
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
            if not readable:
                return False
            return self._sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except (OSError, ValueError):
            return True

    def _send(self, data: bytes) -> None:
        if self._sock is not None and self._sock_kind == socket.SOCK_STREAM and self._peer_closed():
            self._drop_socket()
        if self._sock is None:
            self._connect()
        if self._sock_kind == socket.SOCK_STREAM:
            self._sock.sendall(data)
        elif self._target is not None:
            self._sock.sendto(data, self._target)
        else:
            self._sock.send(data)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = self._frame(record)
        except Exception as e:  # formatting problem: count it, never raise
            self._note_error(e)
            return
        for attempt in (1, 2):
            try:
                self._send(data)
                self.sent += 1
                self.last_sent_at = datetime.now(timezone.utc)
                return
            except OSError as e:
                self._drop_socket()
                if attempt == 2:
                    self._note_error(e)

    def _note_error(self, e: Exception) -> None:
        self.failed += 1
        self.last_error = str(e) or e.__class__.__name__
        self.last_error_at = datetime.now(timezone.utc)

    def _drop_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def close(self) -> None:
        self.acquire()
        try:
            self._drop_socket()
        finally:
            self.release()
        super().close()


class RecentLog(logging.Handler):
    """The last `size` log lines, kept in memory for the Logging page.
    A bounded deque, so it can never grow."""

    def __init__(self, size: int = 500):
        super().__init__(level=logging.DEBUG)
        self.records: collections.deque = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            msg += f" ({record.exc_info[0].__name__}: {record.exc_info[1]})"
        self.records.append((record.created, record.levelname, record.name, msg,
                             getattr(record, "src", "")))


class _DropBadHTTPNoise(logging.Filter):
    """Drop werkzeug 400-level 'Bad …' records.

    These come from non-HTTP traffic hitting the web port (port scanners,
    NetBIOS probes, accidental TLS handshakes). They aren't bugs in Spot.
    Covers 'Bad HTTP/0.9 request type', 'Bad request version',
    'Bad request syntax', and similar.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not ("code 400" in msg and "Bad " in msg)


# ---------------------------------------------------------------------------
# State + (re)configuration
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state: dict = {"settings": None, "syslog": None, "syslog_error": None,
                "recent": RecentLog(), "stderr": None, "source": "env"}


def category_of(logger_name: str) -> str | None:
    best, best_len = None, -1
    for c in CATEGORIES:
        for name in c.loggers:
            if (logger_name == name or logger_name.startswith(name + ".")) and len(name) > best_len:
                best, best_len = c.key, len(name)
    return best


def apply_settings(settings: LogSettings, source: str = "env") -> str | None:
    """Make `settings` live. Returns None, or why syslog couldn't be set up
    (the rest is applied either way)."""
    with _lock:
        root = logging.getLogger()
        root.setLevel(logging.WARNING)  # third-party libraries: warnings only
        src_filter = _RequestSource()
        fmt = logging.Formatter("%(name)s %(levelname)s: %(message)s%(src_suffix)s")

        if _state["stderr"] is None:
            # First call: drop pre-existing handlers (Flask/gunicorn defaults)
            # so nothing is logged twice.
            for h in list(root.handlers):
                root.removeHandler(h)
            stderr = logging.StreamHandler(sys.stderr)
            stderr.setFormatter(logging.Formatter(
                "%(asctime)s %(name)s %(levelname)s: %(message)s%(src_suffix)s"))
            stderr.addFilter(src_filter)
            root.addHandler(stderr)
            _state["stderr"] = stderr
            recent = _state["recent"]
            recent.addFilter(src_filter)
            root.addHandler(recent)

        # Replace the syslog handler (closing the old socket — no leak).
        old = _state["syslog"]
        if old is not None:
            root.removeHandler(old)
            old.close()
        _state["syslog"], _state["syslog_error"] = None, None
        if settings.syslog_enabled and settings.syslog_address:
            try:
                h = SyslogSender(settings.syslog_address, settings.syslog_protocol,
                                 settings.syslog_facility, settings.syslog_tag)
                h.setFormatter(fmt)
                h.addFilter(src_filter)
                root.addHandler(h)
                _state["syslog"] = h
            except OSError as e:
                _state["syslog_error"] = str(e) or e.__class__.__name__

        # Category levels. Loggers under "spot" propagate to the root
        # handlers; the root's own WARNING level doesn't filter them.
        for c in CATEGORIES:
            lvl = LEVELS.get(settings.levels.get(c.key, c.default), logging.INFO)
            for name in c.loggers:
                logging.getLogger(name).setLevel(lvl)

        werkzeug = logging.getLogger("werkzeug")
        werkzeug.setLevel(logging.WARNING)
        if not any(isinstance(f, _DropBadHTTPNoise) for f in werkzeug.filters):
            werkzeug.addFilter(_DropBadHTTPNoise())
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

        _state["settings"], _state["source"] = settings, source
        err = _state["syslog_error"]
    if err:
        sys.stderr.write(f"spot: syslog unavailable ({settings.syslog_address}): {err}\n")
    return err


def configure_logging(cfg: Config) -> None:
    """Start-up configuration from .env (stored settings are applied later,
    once the database is reachable — see load_stored_settings)."""
    apply_settings(env_settings(cfg), "env")


def current_settings() -> LogSettings | None:
    return _state["settings"]


def status() -> dict:
    h: SyslogSender | None = _state["syslog"]
    s: LogSettings | None = _state["settings"]
    return {
        "source": _state["source"],
        "enabled": bool(s and s.syslog_enabled),
        "connected": h is not None,
        "destination": h.describe() if h else (s.syslog_address if s else ""),
        # A send error is only "current" if nothing has gone out since.
        "error": _state["syslog_error"] or (
            h.last_error if h and h.last_error_at and
            (h.last_sent_at is None or h.last_error_at > h.last_sent_at) else None),
        "error_at": h.last_error_at if h else None,
        "sent": h.sent if h else 0,
        "failed": h.failed if h else 0,
        "last_sent_at": h.last_sent_at if h else None,
    }


def send_test_message() -> str | None:
    """Push one line straight to syslog (bypassing category levels).
    Returns None on success or the error."""
    h: SyslogSender | None = _state["syslog"]
    if h is None:
        return _state["syslog_error"] or "syslog is turned off"
    before = h.failed
    rec = logging.LogRecord("spot", logging.WARNING, __file__, 0,
                            "Spot syslog test message — if you can read this, syslog works.",
                            None, None)
    rec.src = ""
    rec.src_suffix = ""
    h.handle(rec)
    return h.last_error if h.failed > before else None


def recent(limit: int = 300, min_level: str = "debug", category: str = "") -> list[dict]:
    lvl = LEVELS.get(min_level, logging.DEBUG)
    out = []
    for created, levelname, name, msg, src in reversed(_state["recent"].records):
        if logging.getLevelName(levelname) < lvl:
            continue
        cat = category_of(name)
        if category and cat != category:
            continue
        out.append({"ts": datetime.fromtimestamp(created, tz=timezone.utc), "level": levelname,
                    "logger": name, "category": cat, "message": msg, "src": src})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Persistence (Setup → Logging)
# ---------------------------------------------------------------------------

SETTING_KEY = "logging"


def load_stored_settings(cfg: Config) -> None:
    """Apply settings saved from the Logging page, if any."""
    from .db import session_scope
    from .models import Setting
    try:
        with session_scope() as s:
            row = s.get(Setting, SETTING_KEY)
            raw = row.value if row else None
    except Exception as e:  # never let a bad row stop start-up
        logging.getLogger("spot").error("could not load stored logging settings: %s", e)
        return
    if raw:
        try:
            apply_settings(LogSettings.from_json(raw, env_settings(cfg)), "saved")
        except (ValueError, TypeError) as e:
            logging.getLogger("spot").error("stored logging settings are invalid, using .env: %s", e)


def save_settings(settings: LogSettings) -> str | None:
    from .db import session_scope
    from .models import Setting
    with session_scope() as s:
        row = s.get(Setting, SETTING_KEY) or Setting(key=SETTING_KEY)
        row.value = settings.to_json()
        s.add(row)
    return apply_settings(settings, "saved")


def reset_to_env(cfg: Config) -> str | None:
    from .db import session_scope
    from .models import Setting
    with session_scope() as s:
        row = s.get(Setting, SETTING_KEY)
        if row:
            s.delete(row)
    return apply_settings(env_settings(cfg), "env")

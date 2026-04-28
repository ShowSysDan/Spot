from __future__ import annotations

import logging
import logging.handlers
import os
import sys

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


def _build_syslog_handler(cfg: Config) -> logging.Handler | None:
    facility = _FACILITY_MAP.get(cfg.syslog_facility.lower(), logging.handlers.SysLogHandler.LOG_LOCAL0)
    addr = cfg.syslog_address
    try:
        if ":" in addr and not addr.startswith("/"):
            host, port = addr.rsplit(":", 1)
            address: str | tuple[str, int] = (host, int(port))
        else:
            if not os.path.exists(addr):
                return None
            address = addr
        h = logging.handlers.SysLogHandler(address=address, facility=facility)
        h.setFormatter(logging.Formatter("spot[%(process)d] %(name)s %(levelname)s: %(message)s"))
        return h
    except Exception as exc:
        sys.stderr.write(f"spot: could not initialise syslog at {addr}: {exc}\n")
        return None


def configure_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Remove pre-existing handlers (e.g. Flask/gunicorn defaults) to avoid duplicates.
    for h in list(root.handlers):
        root.removeHandler(h)

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
    root.addHandler(stderr)

    syslog_h = _build_syslog_handler(cfg)
    if syslog_h is not None:
        root.addHandler(syslog_h)

    # Tame chatty loggers
    logging.getLogger("werkzeug").setLevel(max(level, logging.WARNING))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

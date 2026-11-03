from __future__ import annotations

import logging
import math
import secrets

from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from . import glossary, units
from .alerts import AlertEngine, open_alert_count
from .config import Config
from .shows import ShowReporter
from .db import dispose_engine, init_engine, init_schema
from .janitor import JanitorThread
from .logging_config import configure_logging, load_stored_settings
from .listeners import ListenerManager
from .modbus import ModbusManager
from .tempest import TempestManager
from .util import format_local, parse_iso_ts, set_display_timezone
from .version import __version__


# Largest request body accepted (ingest posts, forms, API JSON are all tiny;
# this only stops a client from making the server buffer something huge).
MAX_REQUEST_BYTES = 2 * 1024 * 1024

# Secrets that ship in examples/defaults; never sign cookies with these.
_PLACEHOLDER_SECRETS = {"", "dev-secret", "please-change-me", "changeme"}

# Pages meant to be shown inside another page (a signage player, an
# intranet portal) may be framed by any site; they only display data.
_FRAMEABLE_ENDPOINTS = {"web.wall", "web.monitor_dashboard", "static"}


def _finite_or_none(obj):
    """Copy of `obj` with NaN / ±Infinity floats replaced by None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite_or_none(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite_or_none(v) for v in obj]
    return obj


class _StrictJSON(DefaultJSONProvider):
    """Always emit valid JSON. Python's encoder writes NaN / Infinity bare,
    which browsers' JSON.parse rejects — one such reading (e.g. stored before
    1.13.0 started refusing them) would break every chart showing it."""

    def dumps(self, obj, **kwargs):
        try:
            return super().dumps(obj, allow_nan=False, **kwargs)
        except ValueError:
            return super().dumps(_finite_or_none(obj), **kwargs)


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json = _StrictJSON(app)
    app.config["SPOT"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    secret_weak = cfg.secret_key.strip() in _PLACEHOLDER_SECRETS
    # The session cookie only carries flash messages, so a per-process key
    # is harmless (a restart just drops any unread message).
    app.secret_key = secrets.token_hex(32) if secret_weak else cfg.secret_key

    configure_logging(cfg)
    if secret_weak:
        logging.getLogger("spot").warning(
            "SPOT_SECRET_KEY is unset or a placeholder; using a random key for this run. "
            "Set a long random value in .env.")
    set_display_timezone(cfg.timezone)  # raises on an unknown zone name: fail fast
    init_engine(cfg)
    init_schema(cfg)
    load_stored_settings(cfg)  # logging settings saved on Setup → Logging win over .env
    units.load()  # which units are sound levels (Setup → Units)

    manager = ListenerManager(cfg)
    modbus = ModbusManager(cfg)
    tempest = TempestManager(cfg)
    janitor = JanitorThread(cfg.janitor_interval_seconds, cfg.default_retention_days)
    app.config["SPOT_LISTENERS"] = manager
    app.config["SPOT_MODBUS"] = modbus
    app.config["SPOT_TEMPEST"] = tempest
    app.config["SPOT_JANITOR"] = janitor
    alerts = AlertEngine(cfg)
    show_reporter = ShowReporter(cfg)

    from .routes.web import bp as web_bp
    from .routes.api import bp as api_bp
    from .routes.data import bp as data_bp
    from .routes.v1 import bp as v1_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(data_bp, url_prefix="/data")
    app.register_blueprint(v1_bp, url_prefix="/api/v1")

    @app.errorhandler(HTTPException)
    def _api_json_errors(e: HTTPException):
        """Routing errors (unknown path, wrong method) never reach a blueprint's
        own handler; keep everything under /api/v1 answering in JSON."""
        if request.path.startswith("/api/v1"):
            return jsonify({"error": e.description, "status": e.code}), e.code
        return e

    @app.before_request
    def _check_host():
        """Optional SPOT_ALLOWED_HOSTS: refuse requests addressed to any other
        name (stops DNS-rebinding pages from driving the login-less UI)."""
        if not cfg.allowed_hosts:
            return None
        host = request.host.lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name.strip("[]") not in cfg.allowed_hosts and host not in cfg.allowed_hosts:
            logging.getLogger("spot.security").warning(
                "refused request for host %r (not in SPOT_ALLOWED_HOSTS) path=%s", host, request.path)
            return "Unknown host", 421
        return None

    @app.after_request
    def _security_headers(resp):
        """Browser hardening that changes nothing for scripts and sensors."""
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint not in _FRAMEABLE_ENDPOINTS:
            # Pages with buttons (delete, toggle…) can't be framed by another
            # site and clicked through invisibly (clickjacking).
            resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return resp

    @app.after_request
    def _download_started(resp):
        """Tell the page a requested download has begun (see Spot.download):
        echo its `dl` token in a short-lived cookie on file responses."""
        token = request.args.get("dl", "")
        if (token and len(token) <= 32 and token.isalnum() and resp.status_code < 400
                and "attachment" in resp.headers.get("Content-Disposition", "")):
            resp.set_cookie("spot_dl", token, max_age=120, path="/", samesite="Lax")
        return resp

    @app.template_filter("localtime")
    def _localtime(value, fmt: str = "%Y-%m-%d %H:%M:%S %Z"):
        """Render a datetime or ISO-8601 string in the server's timezone."""
        if isinstance(value, str):
            value = parse_iso_ts(value)
        return format_local(value, fmt)

    @app.context_processor
    def _inject_globals():
        try:
            alert_count = open_alert_count()
        except Exception:  # never let the nav badge break a page
            alert_count = 0
        return {"spot_version": __version__, "spot_schema": cfg.db_schema,
                "spot_timezone": cfg.timezone,
                "spot_alert_count": alert_count,
                "stat_legend": glossary.legend,
                "is_sound_unit": units.is_sound_unit,
                "acoustic_stats": glossary.ACOUSTIC,
                "db_note": glossary.DB_NOTE}

    manager.start_enabled()
    modbus.start_enabled()
    tempest.start_enabled()
    janitor.start()
    alerts.start()
    show_reporter.start()

    import atexit

    def _cleanup() -> None:
        janitor.stop()
        alerts.stop()
        show_reporter.stop()
        modbus.shutdown()
        tempest.shutdown()
        manager.shutdown()
        # Let an in-progress pass (an alert, a report callback) finish before the
        # database engine goes away.
        for t in (janitor, alerts, show_reporter):
            if t.is_alive():
                t.join(timeout=5.0)
        dispose_engine()

    atexit.register(_cleanup)

    app.logger.info("Spot v%s started: web=%s:%d schema=%s",
                    __version__, cfg.web_host, cfg.web_port, cfg.db_schema)
    return app

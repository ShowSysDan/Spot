from __future__ import annotations

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from .alerts import AlertEngine, open_alert_count
from .config import Config
from .schedules import ReportScheduler
from .shows import ShowReporter
from .db import dispose_engine, init_engine, init_schema
from .janitor import JanitorThread
from .logging_config import configure_logging
from .listeners import ListenerManager
from .modbus import ModbusManager
from .util import format_local, parse_iso_ts, set_display_timezone
from .version import __version__


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SPOT"] = cfg
    app.secret_key = cfg.secret_key

    configure_logging(cfg)
    set_display_timezone(cfg.timezone)  # raises on an unknown zone name: fail fast
    init_engine(cfg)
    init_schema(cfg)

    manager = ListenerManager(cfg)
    modbus = ModbusManager(cfg)
    janitor = JanitorThread(cfg.janitor_interval_seconds, cfg.default_retention_days)
    app.config["SPOT_LISTENERS"] = manager
    app.config["SPOT_MODBUS"] = modbus
    app.config["SPOT_JANITOR"] = janitor
    alerts = AlertEngine(cfg)
    show_reporter = ShowReporter(cfg)
    scheduler = ReportScheduler(cfg)

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
                "spot_alert_count": alert_count}

    manager.start_enabled()
    modbus.start_enabled()
    janitor.start()
    alerts.start()
    show_reporter.start()
    scheduler.start()

    import atexit

    def _cleanup() -> None:
        janitor.stop()
        alerts.stop()
        show_reporter.stop()
        scheduler.stop()
        modbus.shutdown()
        manager.shutdown()
        # Let an in-progress pass (an e-mail, a report) finish before the
        # database engine goes away.
        for t in (janitor, alerts, show_reporter, scheduler):
            if t.is_alive():
                t.join(timeout=5.0)
        dispose_engine()

    atexit.register(_cleanup)

    app.logger.info("Spot v%s started: web=%s:%d schema=%s",
                    __version__, cfg.web_host, cfg.web_port, cfg.db_schema)
    return app

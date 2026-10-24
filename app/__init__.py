from __future__ import annotations

from flask import Flask

from .alerts import AlertEngine, open_alert_count
from .config import Config
from .db import dispose_engine, init_engine, init_schema
from .janitor import JanitorThread
from .logging_config import configure_logging
from .listeners import ListenerManager
from .modbus import ModbusManager
from .util import format_local, parse_iso_ts
from .version import __version__


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SPOT"] = cfg
    app.secret_key = cfg.secret_key

    configure_logging(cfg)
    init_engine(cfg)
    init_schema(cfg)

    manager = ListenerManager(cfg)
    modbus = ModbusManager(cfg)
    janitor = JanitorThread(cfg.janitor_interval_seconds, cfg.default_retention_days)
    app.config["SPOT_LISTENERS"] = manager
    app.config["SPOT_MODBUS"] = modbus
    app.config["SPOT_JANITOR"] = janitor
    alerts = AlertEngine(cfg)

    from .routes.web import bp as web_bp
    from .routes.api import bp as api_bp
    from .routes.data import bp as data_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(data_bp, url_prefix="/data")

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
                "spot_alert_count": alert_count}

    manager.start_enabled()
    modbus.start_enabled()
    janitor.start()
    alerts.start()

    import atexit

    def _cleanup() -> None:
        janitor.stop()
        alerts.stop()
        modbus.shutdown()
        manager.shutdown()
        janitor.join(timeout=3.0)
        dispose_engine()

    atexit.register(_cleanup)

    app.logger.info("Spot v%s started: web=%s:%d schema=%s",
                    __version__, cfg.web_host, cfg.web_port, cfg.db_schema)
    return app

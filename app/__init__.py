from __future__ import annotations

import os
from flask import Flask

from .config import Config
from .db import dispose_engine, init_engine, init_schema
from .logging_config import configure_logging
from .listeners import ListenerManager


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SPOT"] = cfg
    app.secret_key = cfg.secret_key

    configure_logging(cfg)
    init_engine(cfg)
    init_schema(cfg)

    manager = ListenerManager(cfg)
    app.config["SPOT_LISTENERS"] = manager

    from .routes.web import bp as web_bp
    from .routes.api import bp as api_bp
    from .routes.data import bp as data_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(data_bp, url_prefix="/data")

    manager.start_enabled()

    import atexit

    def _cleanup() -> None:
        manager.shutdown()
        dispose_engine()

    atexit.register(_cleanup)

    app.logger.info("Spot started: web=%s:%d schema=%s",
                    cfg.web_host, cfg.web_port, cfg.db_schema)
    return app

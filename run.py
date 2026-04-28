"""Development runner. For production, use gunicorn against wsgi:app."""
from app import create_app

if __name__ == "__main__":
    app = create_app()
    cfg = app.config["SPOT"]
    app.run(host=cfg.web_host, port=cfg.web_port, debug=False, use_reloader=False)

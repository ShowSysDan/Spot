from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class Config:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_schema: str

    web_host: str
    web_port: int

    syslog_address: str
    syslog_facility: str
    log_level: str

    secret_key: str
    ingest_allow: tuple[str, ...]
    janitor_interval_seconds: int
    default_retention_days: int | None

    # Base URL used in links inside webhooks, callbacks and API responses
    # (e.g. http://spot.internal:6100). Blank = derived from each request.
    public_url: str = ""
    # Alert delivery.
    alert_webhook_url: str = ""
    alert_interval_seconds: int = 15
    # Optional lock-down for show-report callback URLs: hostnames and/or
    # CIDR ranges. Empty = any host except link-local/reserved addresses.
    callback_allow: tuple[str, ...] = ()
    # IANA zone for everything people see (24-hour). Storage stays UTC.
    timezone: str = "America/New_York"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        allow_raw = os.getenv("SPOT_INGEST_ALLOW", "").strip()
        allow = _csv(allow_raw)
        default_ret_raw = os.getenv("SPOT_DEFAULT_RETENTION_DAYS", "").strip()
        default_ret = int(default_ret_raw) if default_ret_raw else None
        if default_ret is not None and default_ret < 1:
            default_ret = None
        return cls(
            db_host=os.getenv("SPOT_DB_HOST", "127.0.0.1"),
            db_port=int(os.getenv("SPOT_DB_PORT", "5432")),
            db_name=os.getenv("SPOT_DB_NAME", "shared"),
            db_user=os.getenv("SPOT_DB_USER", "spot_user"),
            db_password=os.getenv("SPOT_DB_PASSWORD", ""),
            db_schema=os.getenv("SPOT_DB_SCHEMA", "spot"),
            web_host=os.getenv("SPOT_WEB_HOST", "0.0.0.0"),
            web_port=int(os.getenv("SPOT_WEB_PORT", "6100")),
            syslog_address=os.getenv("SPOT_SYSLOG_ADDRESS", "/dev/log"),
            syslog_facility=os.getenv("SPOT_SYSLOG_FACILITY", "local0"),
            log_level=os.getenv("SPOT_LOG_LEVEL", "INFO"),
            secret_key=os.getenv("SPOT_SECRET_KEY", "dev-secret"),
            ingest_allow=allow,
            janitor_interval_seconds=int(os.getenv("SPOT_JANITOR_INTERVAL_SECONDS", "3600")),
            default_retention_days=default_ret,
            public_url=os.getenv("SPOT_PUBLIC_URL", "").strip().rstrip("/"),
            alert_webhook_url=os.getenv("SPOT_ALERT_WEBHOOK_URL", "").strip(),
            alert_interval_seconds=max(5, int(os.getenv("SPOT_ALERT_INTERVAL_SECONDS", "15"))),
            callback_allow=_csv(os.getenv("SPOT_CALLBACK_ALLOW", "")),
            timezone=os.getenv("SPOT_TIMEZONE", "America/New_York").strip() or "America/New_York",
        )

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

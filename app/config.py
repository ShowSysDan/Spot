from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv


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

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        allow_raw = os.getenv("SPOT_INGEST_ALLOW", "").strip()
        allow = tuple(s.strip() for s in allow_raw.split(",") if s.strip()) if allow_raw else ()
        return cls(
            db_host=os.getenv("SPOT_DB_HOST", "127.0.0.1"),
            db_port=int(os.getenv("SPOT_DB_PORT", "5432")),
            db_name=os.getenv("SPOT_DB_NAME", "shared"),
            db_user=os.getenv("SPOT_DB_USER", "spot_user"),
            db_password=os.getenv("SPOT_DB_PASSWORD", ""),
            db_schema=os.getenv("SPOT_DB_SCHEMA", "spot"),
            web_host=os.getenv("SPOT_WEB_HOST", "0.0.0.0"),
            web_port=int(os.getenv("SPOT_WEB_PORT", "8080")),
            syslog_address=os.getenv("SPOT_SYSLOG_ADDRESS", "/dev/log"),
            syslog_facility=os.getenv("SPOT_SYSLOG_FACILITY", "local0"),
            log_level=os.getenv("SPOT_LOG_LEVEL", "INFO"),
            secret_key=os.getenv("SPOT_SECRET_KEY", "dev-secret"),
            ingest_allow=allow,
        )

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

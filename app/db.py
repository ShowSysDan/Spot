from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from .config import Config

log = logging.getLogger("spot.db")

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def init_engine(cfg: Config) -> Engine:
    global _engine, _Session
    _engine = create_engine(
        cfg.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )

    @event.listens_for(_engine, "connect")
    def _set_search_path(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute(f'SET search_path TO "{cfg.db_schema}", public')
        finally:
            cur.close()

    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialised; call init_engine() first")
    return _engine


def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


@contextmanager
def session_scope() -> Iterator[Session]:
    if _Session is None:
        raise RuntimeError("Sessionmaker not initialised")
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# Columns added after the first release. create_all() only creates missing
# tables, so existing installs get these via idempotent ALTERs. Append new
# entries here; never edit or reorder old ones. Each is
# (table, column, SQL type + constraints).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("monitors", "retention_days", "INTEGER"),
    ("monitors", "value_regex", "TEXT"),
    ("monitors", "modbus_device_id",
     'INTEGER REFERENCES "{schema}".modbus_devices(id) ON DELETE CASCADE'),
    ("monitors", "modbus_register_address", "INTEGER"),
    ("monitors", "modbus_data_type", "VARCHAR(16)"),
    ("monitors", "decimal_places", "INTEGER"),
    ("monitors", "location", "VARCHAR(120)"),
    ("monitors", "color", "VARCHAR(16)"),
    ("monitors", "alert_high", "DOUBLE PRECISION"),
    ("monitors", "alert_low", "DOUBLE PRECISION"),
    ("monitors", "alert_window_seconds", "INTEGER"),
    ("monitors", "alert_stale_seconds", "INTEGER"),
    ("shows", "auto_detect", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("shows", "detect_monitor_id",
     'INTEGER REFERENCES "{schema}".monitors(id) ON DELETE SET NULL'),
    ("shows", "detect_window_minutes", "INTEGER NOT NULL DEFAULT 15"),
    ("shows", "detect_min_step", "DOUBLE PRECISION NOT NULL DEFAULT 6"),
)


# Other in-place type changes, applied every start (all idempotent).
_ALTERED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # Widened from VARCHAR(8) to fit "modbus".
    ("monitors", "listener_type", "VARCHAR(16)"),
)


def init_schema(cfg: Config) -> None:
    """Ensure the schema, tables, and idempotent column migrations are applied."""
    from .models import Base
    eng = get_engine()
    sch = cfg.db_schema
    with eng.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
    # Apply schema to metadata before create_all
    Base.metadata.schema = sch
    for tbl in Base.metadata.tables.values():
        tbl.schema = sch
    Base.metadata.create_all(eng)

    with eng.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            conn.exec_driver_sql(
                f'ALTER TABLE IF EXISTS "{sch}".{table} '
                f'ADD COLUMN IF NOT EXISTS {column} {ddl.format(schema=sch)}'
            )
        for table, column, sql_type in _ALTERED_COLUMNS:
            conn.exec_driver_sql(
                f'ALTER TABLE IF EXISTS "{sch}".{table} '
                f'ALTER COLUMN {column} TYPE {sql_type}'
            )
    log.info("Schema '%s' ready", sch)

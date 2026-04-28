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


def init_schema(cfg: Config) -> None:
    """Ensure the schema and tables exist. Idempotent."""
    from .models import Base
    eng = get_engine()
    with eng.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{cfg.db_schema}"')
    # Apply schema to metadata before create_all
    Base.metadata.schema = cfg.db_schema
    for tbl in Base.metadata.tables.values():
        tbl.schema = cfg.db_schema
    Base.metadata.create_all(eng)
    log.info("Schema '%s' ready", cfg.db_schema)

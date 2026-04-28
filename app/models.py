from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_token() -> str:
    return secrets.token_urlsafe(24)


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="value")
    listener_type: Mapped[str] = mapped_column(String(8), nullable=False, default="http")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auth_token: Mapped[str] = mapped_column(String(64), nullable=False, default=_make_token, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    readings: Mapped[list["Reading"]] = relationship(
        "Reading", back_populates="monitor", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("listener_type", "port", name="uq_monitor_listener_port"),
    )


class Reading(Base):
    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("monitors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)

    monitor: Mapped["Monitor"] = relationship("Monitor", back_populates="readings")

    __table_args__ = (
        Index("ix_readings_monitor_ts", "monitor_id", "ts"),
    )

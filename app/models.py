from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Table,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_token() -> str:
    return secrets.token_urlsafe(24)


class ModbusDevice(Base):
    __tablename__ = "modbus_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=502)
    slave_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    word_order: Mapped[str] = mapped_column(String(8), nullable=False, default="CDAB")
    register_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    register_count: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    monitors: Mapped[list["Monitor"]] = relationship(
        "Monitor", back_populates="modbus_device",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="value")
    listener_type: Mapped[str] = mapped_column(String(16), nullable=False, default="http")
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auth_token: Mapped[str] = mapped_column(String(64), nullable=False, default=_make_token, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_regex: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    modbus_device_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("modbus_devices.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    modbus_register_address: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modbus_data_type: Mapped[str | None] = mapped_column(String(16), nullable=True)

    decimal_places: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Free-text grouping label (e.g. "Walt Disney Theater"); only used to
    # group tiles in the UI. External systems address monitors by id.
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Display colour (#rrggbb) for tiles, charts and reports; None = automatic.
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # Alert thresholds; a None threshold is off. The value compared is the
    # average over the last `alert_window_seconds` (None/0 = latest reading).
    alert_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    alert_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    alert_window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Raise a "stale" alert when no reading has arrived for this long.
    alert_stale_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    readings: Mapped[list["Reading"]] = relationship(
        "Reading", back_populates="monitor", cascade="all, delete-orphan", passive_deletes=True
    )
    modbus_device: Mapped["ModbusDevice | None"] = relationship(
        "ModbusDevice", back_populates="monitors"
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


class Alert(Base):
    """One alert episode: opened when a monitor crosses a threshold (or goes
    quiet) and closed (`cleared_at`) when it recovers."""
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("monitors.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # high | low | stale
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Most extreme value seen while open (max for high, min for low).
    peak_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    monitor: Mapped["Monitor"] = relationship("Monitor")

    __table_args__ = (
        Index("ix_alerts_open", "monitor_id", "cleared_at"),
    )


show_monitors = Table(
    "show_monitors",
    Base.metadata,
    Column("show_id", Integer, ForeignKey("shows.id", ondelete="CASCADE"), primary_key=True),
    Column("monitor_id", Integer, ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True),
)


class Show(Base):
    """A named time range (a performance, rehearsal, event) and the monitors
    that matter for it. Created in the UI or by an external system such as
    321Theater, which identifies its shows with `external_id`.

    After `ends_at` (plus a short grace period) Spot can e-mail the show's
    report to `email_to` and/or POST it to `callback_url`.
    """
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="ui")  # ui | api
    email_to: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Report on the actual start/end found in the sound levels (see detect.py)
    # instead of the scheduled times.
    auto_detect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detect_monitor_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("monitors.id", ondelete="SET NULL"), nullable=True)
    detect_window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    detect_min_step: Mapped[float] = mapped_column(Float, nullable=False, default=6.0)
    # Report delivery bookkeeping (e-mail and/or callback after the show ends).
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow,
                                                 onupdate=_utcnow, nullable=False)

    monitors: Mapped[list["Monitor"]] = relationship(
        "Monitor", secondary=show_monitors, order_by="Monitor.name")


class ApiKey(Base):
    """Credential for the external /api/v1 endpoints. Only a SHA-256 hash of
    the key is stored; the key itself is shown once, when it is created."""
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


schedule_monitors = Table(
    "schedule_monitors",
    Base.metadata,
    Column("schedule_id", Integer, ForeignKey("report_schedules.id", ondelete="CASCADE"), primary_key=True),
    Column("monitor_id", Integer, ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True),
)


class ReportSchedule(Base):
    """A recurring e-mailed PDF report covering the previous full period:
    daily → yesterday, weekly → the 7 days before the send day, monthly →
    last calendar month. Times are in the display timezone."""
    __tablename__ = "report_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")  # daily | weekly | monthly
    send_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    send_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # weekly only; 0 = Monday
    email_to: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    monitors: Mapped[list["Monitor"]] = relationship(
        "Monitor", secondary=schedule_monitors, order_by="Monitor.name")

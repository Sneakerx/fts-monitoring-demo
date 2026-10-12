"""Monitoring database schema (SQLAlchemy 2 ORM).

All timestamps are naive UTC.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import utcnow

SEVERITIES = ("WARNING", "ALARM")
LEVEL_NAMES = {0: "NOMINAL", 1: "WARNING", 2: "ALARM"}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    pw_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))  # admin | expert | guest
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Product(Base):
    """A Level-1 product discovered in the PDGS catalogue."""
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    pdgs_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    product_type: Mapped[str] = mapped_column(String(32))
    orbit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sensing_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    sensing_stop: Mapped[datetime] = mapped_column(DateTime)
    publication_date: Mapped[datetime] = mapped_column(DateTime)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processing_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="NEW")  # NEW|PROCESSED|FAILED
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    samples: Mapped[list["Sample"]] = relationship(back_populates="product")


class Parameter(Base):
    __tablename__ = "parameters"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32), default="")
    group: Mapped[str] = mapped_column(String(64))
    algorithm: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="restricted")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    limits: Mapped[list["Limit"]] = relationship(
        back_populates="parameter", order_by="Limit.valid_from.desc()", cascade="all, delete-orphan")
    state: Mapped["ParameterState | None"] = relationship(
        back_populates="parameter", uselist=False, cascade="all, delete-orphan")

    @property
    def current_limit(self) -> "Limit | None":
        return self.limits[0] if self.limits else None


class Sample(Base):
    """One value of a monitoring parameter derived from one product."""
    __tablename__ = "samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    time: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)
    level: Mapped[int] = mapped_column(Integer, default=0)  # 0 nominal, 1 warning, 2 alarm (raw, w/o persistence)

    product: Mapped[Product] = relationship(back_populates="samples")

    __table_args__ = (
        UniqueConstraint("parameter_id", "product_id"),
        Index("ix_samples_param_time", "parameter_id", "time"),
    )


class Limit(Base):
    """Versioned limit set; the most recent valid_from is the active one."""
    __tablename__ = "limits"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"))
    warn_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    warn_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    alarm_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    alarm_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    persistence: Mapped[int] = mapped_column(Integer, default=1)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    changed_by: Mapped[str] = mapped_column(String(64), default="config")
    comment: Mapped[str] = mapped_column(Text, default="")

    parameter: Mapped[Parameter] = relationship(back_populates="limits")


class ParameterState(Base):
    """Running limit-check state per parameter (persistence counting)."""
    __tablename__ = "parameter_state"
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"), primary_key=True)
    level: Mapped[int] = mapped_column(Integer, default=0)            # confirmed level
    candidate_level: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    parameter: Mapped[Parameter] = relationship(back_populates="state")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    severity: Mapped[str] = mapped_column(String(16))  # WARNING | ALARM
    time: Mapped[datetime] = mapped_column(DateTime, index=True)  # data (sensing) time
    value: Mapped[float] = mapped_column(Float)
    limit_text: Mapped[str] = mapped_column(String(128))
    last_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    worst_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_violations: Mapped[int] = mapped_column(Integer, default=1)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # data time back to nominal
    status: Mapped[str] = mapped_column(String(16), default="OPEN")  # OPEN | ACKNOWLEDGED | CLOSED
    raised_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # wall clock
    ack_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    parameter: Mapped[Parameter] = relationship()
    product: Mapped[Product | None] = relationship()


class Annotation(Base):
    __tablename__ = "annotations"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"), index=True)
    time: Mapped[datetime] = mapped_column(DateTime)
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    parameter: Mapped[Parameter] = relationship()


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # daily | weekly | adhoc
    title: Mapped[str] = mapped_column(String(160))
    period_start: Mapped[datetime] = mapped_column(DateTime)
    period_end: Mapped[datetime] = mapped_column(DateTime)
    html_path: Mapped[str] = mapped_column(String(512))
    pdf_path: Mapped[str] = mapped_column(String(512))
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="scheduler")
    n_products: Mapped[int] = mapped_column(Integer, default=0)
    n_events: Mapped[int] = mapped_column(Integer, default=0)
    overall_status: Mapped[str] = mapped_column(String(16), default="NOMINAL")


class PollRun(Base):
    __tablename__ = "poll_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    found: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduler")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128), default="")
    details: Mapped[str] = mapped_column(Text, default="")


class SystemState(Base):
    """Small key/value store (e.g. the polling watermark)."""
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

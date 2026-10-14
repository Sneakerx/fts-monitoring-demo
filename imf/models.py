"""Monitoring database (MDB) schema (SQLAlchemy 2 ORM).

All timestamps are naive UTC. The schema is evolved with Alembic migrations (`imf/migrations`).

Conventions
* **baseline**: processing baseline of the source product (e.g. "B03"). Data that does not depend on
  the L1 processing baseline (L0 headers, auxiliary data, manual entries) carries `INDEPENDENT` ("")
  and is shown in every baseline view.
* **dims**: normalised dimension key of a sample series, e.g. "dir=fwd;sb=3" ("" = no dimensions).
  One parameter can therefore have several series (sub-bands, scan directions, sensors, ...).
* **entry attributes** (R2): every sample/vector/timeline entry records its *entry type*
  (derived / reprocessed / manual / imported / external), *source* and *entry time*.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import utcnow

SEVERITIES = ("INFO", "WARNING", "ALARM")
LEVEL_NAMES = {0: "NOMINAL", 1: "WARNING", 2: "ALARM"}
ENTRY_TYPES = ("derived", "reprocessed", "manual", "imported", "external")
INDEPENDENT = ""  # baseline of baseline-independent data

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


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
    """A product discovered in the PDGS catalogue (L0, L1 or auxiliary)."""
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    pdgs_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    product_type: Mapped[str] = mapped_column(String(32))
    level: Mapped[str] = mapped_column(String(8), default="L1", server_default="L1")  # L0 | L1 | AUX
    orbit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sensing_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    sensing_stop: Mapped[datetime] = mapped_column(DateTime)
    publication_date: Mapped[datetime] = mapped_column(DateTime)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline: Mapped[str] = mapped_column(String(16), default="", server_default="")
    processor_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    product_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)  # sha256 of the downloaded file
    header: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: catalogue attributes / header
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    processing_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="NEW")  # NEW|PROCESSED|FAILED
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    samples: Mapped[list["Sample"]] = relationship(back_populates="product")

    __table_args__ = (Index("ix_products_type_orbit", "product_type", "orbit"),)


class ProductLink(Base):
    """Traceability between products, e.g. L0 -> L1 of the same orbit."""
    __tablename__ = "product_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    relation: Mapped[str] = mapped_column(String(16), default="L0->L1")

    __table_args__ = (UniqueConstraint("source_id", "target_id", name="uq_product_links_pair"),)


@dataclass
class StateView:
    """Headline status of a parameter over all its series in the nominal view."""
    level: int
    last_value: float | None
    last_time: datetime | None


class Parameter(Base):
    __tablename__ = "parameters"
    # baseline shown by default (set from the configuration at start-up)
    nominal_baseline = "B03"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(32), default="")
    group: Mapped[str] = mapped_column(String(64))
    algorithm: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    visibility: Mapped[str] = mapped_column(String(16), default="restricted")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(8), default="scalar", server_default="scalar")  # scalar | vector
    dims: Mapped[str] = mapped_column(String(128), default="", server_default="")  # dimension names, e.g. "sb,dir"
    origin: Mapped[str] = mapped_column(String(16), default="config", server_default="config")  # config|manual|external

    limits: Mapped[list["Limit"]] = relationship(
        back_populates="parameter", order_by="Limit.valid_from.desc()", cascade="all, delete-orphan")
    states: Mapped[list["ParameterState"]] = relationship(
        back_populates="parameter", cascade="all, delete-orphan")
    requirements: Mapped[list["Requirement"]] = relationship(
        back_populates="parameter", order_by="Requirement.valid_from.desc()", cascade="all, delete-orphan")

    @property
    def current_limit(self) -> "Limit | None":
        return self.limits[0] if self.limits else None

    @property
    def current_requirement(self) -> "Requirement | None":
        return self.requirements[0] if self.requirements else None

    @property
    def state(self) -> StateView | None:
        """Worst confirmed level and latest value over the nominal (and baseline-independent) series."""
        sts = [st for st in self.states if st.baseline in (self.nominal_baseline, INDEPENDENT)]
        if not sts:
            return None
        latest = max(sts, key=lambda st: (st.last_time is not None, st.last_time or datetime.min, st.series == ""))
        return StateView(max(st.level for st in sts), latest.last_value, latest.last_time)


class Sample(Base):
    """One value of a monitoring parameter (series `dims`), derived from a product or entered otherwise."""
    __tablename__ = "samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    time: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)
    level: Mapped[int] = mapped_column(Integer, default=0)  # 0 nominal, 1 warning, 2 alarm (raw, w/o persistence)
    dims: Mapped[str] = mapped_column(String(64), default="")
    baseline: Mapped[str] = mapped_column(String(16), default="")
    entry_type: Mapped[str] = mapped_column(String(16), default="derived")
    source: Mapped[str] = mapped_column(String(128), default="")
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    product: Mapped[Product | None] = relationship(back_populates="samples")

    __table_args__ = (
        UniqueConstraint("parameter_id", "product_id", "dims", name="uq_samples_param_product_dims"),
        Index("ix_samples_param_time", "parameter_id", "time"),
        Index("ix_samples_product", "product_id"),
    )


class VectorSample(Base):
    """A spectrum / profile (axis + values) of a vector parameter, e.g. orbit-averaged spectral NESR."""
    __tablename__ = "vector_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    time: Mapped[datetime] = mapped_column(DateTime)
    dims: Mapped[str] = mapped_column(String(64), default="")
    baseline: Mapped[str] = mapped_column(String(16), default="")
    axis: Mapped[str] = mapped_column(Text)    # JSON list
    values: Mapped[str] = mapped_column(Text)  # JSON list
    entry_type: Mapped[str] = mapped_column(String(16), default="derived")
    source: Mapped[str] = mapped_column(String(128), default="")
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("parameter_id", "product_id", "dims", name="uq_vector_samples_param_product_dims"),
        Index("ix_vector_samples_param_time", "parameter_id", "time"),
        Index("ix_vector_samples_product", "product_id"),
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


class Requirement(Base):
    """Versioned performance requirement of a parameter (e.g. SSD), used for compliance statistics."""
    __tablename__ = "requirements"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"), index=True)
    lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    text: Mapped[str] = mapped_column(String(200), default="")
    reference: Mapped[str] = mapped_column(String(200), default="")
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    changed_by: Mapped[str] = mapped_column(String(64), default="config")

    parameter: Mapped[Parameter] = relationship(back_populates="requirements")

    def complies(self, value: float) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value <= self.hi)


class ParameterState(Base):
    """Running limit-check state per series (parameter, baseline, dims) for persistence counting."""
    __tablename__ = "parameter_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("parameters.id"))
    baseline: Mapped[str] = mapped_column(String(16), default="")
    series: Mapped[str] = mapped_column(String(64), default="")  # dims key
    level: Mapped[int] = mapped_column(Integer, default=0)            # confirmed level
    candidate_level: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    parameter: Mapped[Parameter] = relationship(back_populates="states")

    __table_args__ = (UniqueConstraint("parameter_id", "baseline", "series", name="uq_parameter_state_series"),)


class Event(Base):
    """Limit violation (category 'limit'), rule-detected condition with a recommended action
    (category 'action') or a mission-timeline occurrence (category 'timeline')."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    parameter_id: Mapped[int | None] = mapped_column(ForeignKey("parameters.id"), nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    severity: Mapped[str] = mapped_column(String(16))  # INFO | WARNING | ALARM
    time: Mapped[datetime] = mapped_column(DateTime, index=True)  # data (sensing) time
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_text: Mapped[str] = mapped_column(String(128), default="")
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
    baseline: Mapped[str] = mapped_column(String(16), default="")
    dims: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(16), default="limit")  # limit | action | timeline
    rule: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    recommended_action: Mapped[str | None] = mapped_column(Text, nullable=True)

    parameter: Mapped[Parameter | None] = relationship()
    product: Mapped[Product | None] = relationship()
    proposals: Mapped[list["ProposedOutput"]] = relationship(
        back_populates="event", order_by="ProposedOutput.id", cascade="all, delete-orphan")

    @property
    def label(self) -> str:
        if self.parameter is not None:
            return self.parameter.name + (f" [{self.dims}]" if self.dims else "")
        return self.title or (self.rule or "event")


class ProposedOutput(Base):
    """Output proposed by a rule (value, file or telecommand) awaiting expert approval."""
    __tablename__ = "proposed_outputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    rule: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # value | file | telecommand
    title: Mapped[str] = mapped_column(String(200))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")  # DRAFT | APPROVED | REJECTED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped[Event] = relationship(back_populates="proposals")


class TimelineEntry(Base):
    """Mission timeline: data gaps, calibration modes, anomalies, actions, manual entries."""
    __tablename__ = "timeline_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(200))
    start: Mapped[datetime] = mapped_column(DateTime, index=True)
    stop: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto | manual | imported | external
    rule: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attributes: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    baseline: Mapped[str] = mapped_column(String(16), default="")
    author: Mapped[str] = mapped_column(String(64), default="system")
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReferenceData(Base):
    """Versioned reference data imported from external files (e.g. NESR spectral template)."""
    __tablename__ = "reference_data"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    dims: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(32), default="")
    axis: Mapped[str] = mapped_column(Text)    # JSON list
    values: Mapped[str] = mapped_column(Text)  # JSON list
    source: Mapped[str] = mapped_column(String(200), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    author: Mapped[str] = mapped_column(String(64), default="system")
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("name", "dims", "version", name="uq_reference_data_version"),)


class Campaign(Base):
    """Reprocessing campaign: ingest products of another baseline over (part of) the mission."""
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    baseline: Mapped[str] = mapped_column(String(16))
    product_type: Mapped[str] = mapped_column(String(32), default="FTS_L1B")
    start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PLANNED")  # PLANNED|RUNNING|DONE|FAILED|CANCELLED
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    n_found: Mapped[int] = mapped_column(Integer, default=0)
    n_processed: Mapped[int] = mapped_column(Integer, default=0)
    n_failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApiToken(Base):
    """Bearer token for external tools using the write API (stored as SHA-256 hash)."""
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="expert")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
    """Small key/value store (e.g. polling watermarks, analysis progress)."""
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

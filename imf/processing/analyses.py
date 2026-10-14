"""Windowed analyses across products and parameters (cross-product correlations, completeness,
requirement statistics) and the post-processing job that runs them together with the trend rules.

An analysis is a function `(ctx: AnalysisContext, start, end) -> list[AnalysisOutput]` evaluated per
day (data time) once all its input product types have data beyond the day plus a margin, so backfill,
live operation and reprocessing give identical results. Results are stored as samples of derived
parameters (entry type 'derived', source 'analysis:<name>@<version>') and limit-checked like any
other sample. Analyses run per processing baseline (nominal + reprocessing campaigns).

    @analysis("L1_COMPLETENESS", inputs=("FTS_L0_EO", "FTS_L1B"))
    def completeness(ctx, start, end):
        ...
        return [AnalysisOutput("L1_COMPLETENESS", mid, 97.3)]
"""
from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import entry_points
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..db import Database, get_state, set_state
from ..models import (INDEPENDENT, Campaign, Event, Parameter, Product, ProductLink, Requirement, Sample)
from ..timeutil import iso, parse_iso
from .pipeline import store_sample

log = logging.getLogger(__name__)

ANALYSIS_MODULES = ["imf.processing.fts_analyses"]
ENTRY_POINT_GROUP = "imf.analyses"


@dataclass(frozen=True)
class AnalysisOutput:
    code: str
    time: datetime
    value: float
    dims: str = ""
    product_id: int | None = None


@dataclass(frozen=True)
class Analysis:
    name: str
    outputs: tuple[str, ...]
    fn: Callable
    version: str
    inputs: tuple[str, ...]
    per_baseline: bool = True

    @property
    def label(self) -> str:
        return f"analysis:{self.name}@{self.version}"


_ANALYSES: dict[str, Analysis] = {}


def analysis(*outputs: str, version: str = "1.0", inputs: tuple[str, ...] = ("FTS_L1B",),
             per_baseline: bool = True) -> Callable:
    def deco(fn: Callable) -> Callable:
        _ANALYSES[fn.__name__] = Analysis(fn.__name__, tuple(outputs), fn, version, tuple(inputs), per_baseline)
        return fn
    return deco


def load_analyses(extra_modules: list[str] | tuple = ()) -> dict[str, Analysis]:
    mods = [*ANALYSIS_MODULES, *extra_modules]
    try:
        mods += [ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)]
    except Exception:  # noqa: BLE001
        log.exception("Cannot read entry points %s", ENTRY_POINT_GROUP)
    for m in mods:
        try:
            importlib.import_module(m)
        except ImportError:
            log.exception("Cannot import analysis module %s", m)
    return dict(_ANALYSES)


class AnalysisContext:
    def __init__(self, s: Session, settings: Settings, baseline: str, analysis_: Analysis):
        self.s, self.settings, self.baseline, self.analysis = s, settings, baseline, analysis_
        self.nominal_baseline = settings.pdgs.baseline
        self.characterisation = settings.characterisation
        self.orbit_period = timedelta(minutes=settings.mission.orbit_period_min)

    @property
    def is_nominal(self) -> bool:
        return self.baseline == self.nominal_baseline

    def _bl(self, product_type: str) -> list[str]:
        level = self.settings.pdgs.type_cfg(product_type).level
        return [self.baseline] if level == "L1" else [INDEPENDENT, self.baseline]

    def products(self, product_type: str, start: datetime, end: datetime, status: str | None = "PROCESSED"
                 ) -> list[Product]:
        q = select(Product).where(Product.product_type == product_type, Product.sensing_start >= start,
                                  Product.sensing_start < end, Product.baseline.in_(self._bl(product_type)))
        if status:
            q = q.where(Product.status == status)
        return list(self.s.scalars(q.order_by(Product.sensing_start)))

    def last_product_before(self, product_type: str, t: datetime) -> Product | None:
        return self.s.scalar(select(Product).where(
            Product.product_type == product_type, Product.sensing_start < t,
            Product.baseline.in_(self._bl(product_type))).order_by(Product.sensing_start.desc()).limit(1))

    def samples_by_orbit(self, code: str, start: datetime, end: datetime, baseline: str | None = None
                         ) -> dict[int, tuple[datetime, float, int]]:
        """{orbit: (time, value, product_id)} of a scalar parameter without dimensions."""
        p = self.s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            return {}
        bl = [self.baseline, INDEPENDENT] if baseline is None else [baseline]
        rows = self.s.execute(select(Product.orbit, Sample.time, Sample.value, Sample.product_id).join(Product)
                              .where(Sample.parameter_id == p.id, Sample.dims == "", Sample.time >= start,
                                     Sample.time < end, Sample.baseline.in_(bl))).all()
        return {r.orbit: (r.time, r.value, r.product_id) for r in rows if r.orbit is not None}

    def values(self, code: str, start: datetime, end: datetime) -> list[float]:
        p = self.s.scalar(select(Parameter).where(Parameter.code == code))
        if p is None:
            return []
        return list(self.s.scalars(select(Sample.value).where(
            Sample.parameter_id == p.id, Sample.time >= start, Sample.time < end,
            Sample.baseline.in_([self.baseline, INDEPENDENT]))))

    def requirement(self, code: str) -> Requirement | None:
        p = self.s.scalar(select(Parameter).options(selectinload(Parameter.requirements))
                          .where(Parameter.code == code))
        return p.current_requirement if p else None

    def link(self, source_id: int, target_id: int, relation: str) -> None:
        exists = self.s.scalar(select(ProductLink.id).where(ProductLink.source_id == source_id,
                                                            ProductLink.target_id == target_id))
        if exists is None:
            self.s.add(ProductLink(source_id=source_id, target_id=target_id, relation=relation))


class AnalysisService:
    """Post-processing job: windowed analyses, then trend rules (data-time driven, idempotent)."""

    def __init__(self, db: Database, settings: Settings, rules, notifier=None):
        self.db, self.settings, self.rules, self.notifier = db, settings, rules, notifier
        self.analyses = load_analyses(settings.processing.plugin_modules)
        self.margin = timedelta(hours=settings.processing.analysis_margin_h)
        self._lock = threading.Lock()

    def outputs_index(self) -> dict[str, Analysis]:
        return {code: a for a in self.analyses.values() for code in a.outputs}

    def baselines(self, s: Session) -> list[str]:
        out = [self.settings.pdgs.baseline]
        for (b,) in s.execute(select(Campaign.baseline).where(Campaign.status.in_(("RUNNING", "DONE")))):
            if b not in out:
                out.append(b)
        return out

    def _horizon(self, s: Session, a: Analysis, baseline: str) -> datetime | None:
        latest = []
        for t in a.inputs:
            bl = [baseline] if self.settings.pdgs.type_cfg(t).level == "L1" else [INDEPENDENT, baseline]
            v = s.scalar(select(func.max(Product.sensing_stop)).where(Product.product_type == t,
                                                                      Product.baseline.in_(bl)))
            if v is None:
                return None
            latest.append(v)
        return min(latest) - self.margin

    def _first_day(self, s: Session, a: Analysis, baseline: str) -> datetime | None:
        firsts = []
        for t in a.inputs:
            bl = [baseline] if self.settings.pdgs.type_cfg(t).level == "L1" else [INDEPENDENT, baseline]
            v = s.scalar(select(func.min(Product.sensing_start)).where(Product.product_type == t,
                                                                       Product.baseline.in_(bl)))
            if v is not None:
                firsts.append(v)
        if not firsts:
            return None
        f = min(firsts)
        return datetime(f.year, f.month, f.day)

    def run_once(self) -> dict:
        summary = {"windows": 0, "samples": 0, "events": 0, "trend_events": 0}
        if not self._lock.acquire(blocking=False):
            return summary
        try:
            with self.db.session() as s:
                baselines = self.baselines(s)
            for a in self.analyses.values():
                for bl in (baselines if a.per_baseline else baselines[:1]):
                    self._run(a, bl, summary)
            with self.db.session() as s:
                new, cleared = self.rules.evaluate_trends(s)
                summary["trend_events"] = len(new)
                s.flush()
                if self.notifier:
                    self.notifier.notify(new, "RAISED")
                    self.notifier.notify(cleared, "CLEARED")
        finally:
            self._lock.release()
        if summary["windows"]:
            log.info("Analyses: %s", summary)
        return summary

    def _run(self, a: Analysis, baseline: str, summary: dict) -> None:
        key = f"analysis.{a.name}.{baseline}"
        with self.db.session() as s:
            horizon = self._horizon(s, a, baseline)
            wm = get_state(s, key)
            day = parse_iso(wm) if wm else self._first_day(s, a, baseline)
        if horizon is None or day is None:
            return
        while day + timedelta(days=1) <= horizon:
            end = day + timedelta(days=1)
            with self.db.session() as s:
                ctx = AnalysisContext(s, self.settings, baseline, a)
                try:
                    outs = a.fn(ctx, day, end)
                except Exception:  # noqa: BLE001 - an analysis failure must not block the others
                    log.exception("Analysis %s failed for %s (%s)", a.name, day.date(), baseline)
                    outs = []
                new, cleared = self._store(s, a, baseline, outs)
                summary["samples"] += len(outs)
                summary["events"] += len(new)
                self.rules.on_limit_events(s, new)
                self.rules.on_cleared(s, cleared)
                set_state(s, key, iso(end))
                s.flush()
                if self.notifier and baseline == self.settings.pdgs.baseline:
                    self.notifier.notify(new, "RAISED")
                    self.notifier.notify(cleared, "CLEARED")
            summary["windows"] += 1
            day = end

    def _store(self, s: Session, a: Analysis, baseline: str, outs: list[AnalysisOutput]
               ) -> tuple[list[Event], list[Event]]:
        params = {p.code: p for p in s.scalars(select(Parameter).options(selectinload(Parameter.limits))
                                               .where(Parameter.code.in_([o.code for o in outs])))}
        new: list[Event] = []
        cleared: list[Event] = []
        for o in sorted(outs, key=lambda o: o.time):
            p = params.get(o.code)
            if p is None:
                continue
            q = select(Sample.id).where(Sample.parameter_id == p.id, Sample.dims == o.dims,
                                        Sample.baseline == baseline)
            q = q.where(Sample.product_id == o.product_id) if o.product_id is not None else \
                q.where(Sample.product_id.is_(None), Sample.time == o.time)
            if s.scalar(q) is not None:
                continue
            chk = store_sample(s, p, o.value, o.time, o.product_id, baseline, o.dims, "derived", a.label)
            new += chk.new_events
            cleared += chk.cleared
        return new, cleared

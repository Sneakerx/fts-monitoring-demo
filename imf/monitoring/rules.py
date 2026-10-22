"""Rule engine: trigger -> event with recommended action (+ proposed output for expert approval).

Rule kinds
* product: evaluated when a product of a watched level (L0 / L1) is ingested (e.g. non-nominal observation mode)
* limit:   evaluated when a limit event of a watched parameter is raised (e.g. spectral calibration threshold)
* trend:   evaluated on the data-time axis in fixed steps: robust (Theil-Sen) slope of a parameter
           over a sliding window compared with a threshold (e.g. ZPD drift, NESR drift)

Rules only act on the nominal baseline (reprocessing campaigns must not trigger operational actions).
Rule functions live in plug-in modules (`RULE_MODULES`, `processing.plugin_modules`, entry-point group
`imf.rules`); parameters come from the `@rule` defaults, overridden by the `rules:` configuration.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import get_state, set_state
from ..models import INDEPENDENT, Event, Parameter, Product, ProposedOutput, Sample, TimelineEntry
from ..timeutil import iso, parse_iso, utcnow
from . import timeline

log = logging.getLogger(__name__)

RULE_MODULES = ["imf.monitoring.fts_rules"]
ENTRY_POINT_GROUP = "imf.rules"


@dataclass(frozen=True)
class Rule:
    code: str
    kind: str  # product | limit | trend
    fn: Callable
    description: str
    version: str
    watches: tuple[str, ...] = ()
    defaults: dict = field(default_factory=dict)


_RULES: dict[str, Rule] = {}


def rule(code: str, kind: str, *, description: str, version: str = "1.0", watches: tuple[str, ...] = (),
         **defaults) -> Callable:
    def deco(fn: Callable) -> Callable:
        _RULES[code] = Rule(code, kind, fn, description, version, tuple(watches), dict(defaults))
        return fn
    return deco


def load_rules(extra_modules: list[str] | tuple = ()) -> dict[str, Rule]:
    mods = [*RULE_MODULES, *extra_modules]
    try:
        mods += [ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)]
    except Exception:  # noqa: BLE001
        log.exception("Cannot read entry points %s", ENTRY_POINT_GROUP)
    for m in mods:
        try:
            importlib.import_module(m)
        except ImportError:
            log.exception("Cannot import rule module %s", m)
    return dict(_RULES)


def theil_sen_slope(t_days: np.ndarray, y: np.ndarray) -> float:
    """Median of pairwise slopes - robust against outliers and short excursions."""
    i, j = np.triu_indices(len(t_days), k=1)
    dt = t_days[j] - t_days[i]
    ok = dt > 1e-9
    return float(np.median((y[j][ok] - y[i][ok]) / dt[ok])) if ok.any() else 0.0


class RuleContext:
    """What a rule function may use: the session, its configuration and helpers."""

    def __init__(self, engine: "RuleEngine", s: Session, rule_: Rule):
        self.engine, self.s, self.rule = engine, s, rule_
        self.cfg = engine.config(rule_)
        self.characterisation = engine.settings.characterisation
        self.nominal_baseline = engine.nominal

    def parameter(self, code: str) -> Parameter | None:
        return self.s.scalar(select(Parameter).where(Parameter.code == code))

    def samples(self, code: str, start: datetime, end: datetime, baseline: str | None = None,
                dims: str = "", nominal_only: bool = False) -> list[tuple[datetime, float]]:
        p = self.parameter(code)
        if p is None:
            return []
        bl = self.nominal_baseline if baseline is None else baseline
        q = select(Sample.time, Sample.value).where(
            Sample.parameter_id == p.id, Sample.time > start, Sample.time <= end, Sample.dims == dims,
            Sample.baseline.in_([bl, INDEPENDENT]))
        if nominal_only:
            q = q.where(Sample.level == 0)
        return [(r.time, r.value) for r in self.s.execute(q.order_by(Sample.time)).all()]

    def recent(self, code: str, n: int, until: datetime, baseline: str | None = None, dims: str = "") -> list[float]:
        p = self.parameter(code)
        if p is None:
            return []
        bl = self.nominal_baseline if baseline is None else baseline
        rows = self.s.scalars(select(Sample.value).where(
            Sample.parameter_id == p.id, Sample.time <= until, Sample.dims == dims,
            Sample.baseline.in_([bl, INDEPENDENT])).order_by(Sample.time.desc()).limit(n)).all()
        return list(rows)

    def approved_value(self, rule_code: str | None = None) -> float | None:
        """Latest approved proposed value of a rule (e.g. the laser wavenumber currently in use)."""
        return self.s.scalar(select(ProposedOutput.value).where(
            ProposedOutput.rule == (rule_code or self.rule.code), ProposedOutput.status == "APPROVED",
            ProposedOutput.value.is_not(None)).order_by(ProposedOutput.reviewed_at.desc()).limit(1))

    def propose(self, event: Event, kind: str, title: str, *, value: float | None = None, unit: str = "",
                payload: dict | None = None, content: bytes | None = None, filename: str | None = None
                ) -> ProposedOutput:
        path = checksum = None
        if content is not None:
            out_dir: Path = self.engine.settings.processing.outputs_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / (filename or f"{self.rule.code}_{event.time:%Y%m%dT%H%M%S}.dat")
            path.write_bytes(content)
            checksum = hashlib.sha256(content).hexdigest()
        po = ProposedOutput(rule=self.rule.code, kind=kind, title=title[:200], value=value, unit=unit,
                            payload=json.dumps(payload or {}, default=str), file_path=str(path) if path else None,
                            checksum=checksum, status="DRAFT", created_at=utcnow())
        event.proposals.append(po)
        return po

    def timeline(self, type_: str, title: str, start: datetime, stop: datetime | None = None, **kw):
        return timeline.add_entry(self.s, type_, title, start, stop, rule=self.rule.code, **kw)


class RuleEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.nominal = settings.pdgs.baseline
        self.rules = load_rules(settings.processing.plugin_modules)

    def config(self, r: Rule) -> dict:
        return {**r.defaults, **self.settings.rules.get(r.code, {})}

    def watches(self, r: Rule) -> tuple[str, ...]:
        cfg = self.config(r)
        if "watch" in cfg:
            return tuple(cfg["watch"]) if isinstance(cfg["watch"], list) else (cfg["watch"],)
        if "parameter" in cfg:
            return (cfg["parameter"],)
        return r.watches

    def _nominal(self, baseline: str) -> bool:
        return baseline in (self.nominal, INDEPENDENT)

    # ------------------------------------------------------------------ product rules
    def on_product(self, s: Session, product: Product) -> list[Event]:
        events: list[Event] = []
        if not self._nominal(product.baseline):
            return events
        for r in self.rules.values():
            if r.kind == "product" and product.level in self.watches(r):
                try:
                    ev = r.fn(RuleContext(self, s, r), product)
                except Exception:  # noqa: BLE001 - a rule must never block ingestion
                    log.exception("Rule %s failed on %s", r.code, product.name)
                    continue
                if ev is not None:
                    ev.rule, ev.baseline = r.code, product.baseline
                    s.add(ev)
                    s.flush()
                    events.append(ev)
        return events

    # ------------------------------------------------------------------ limit rules
    def on_limit_events(self, s: Session, events: list[Event]) -> None:
        for ev in events:
            if ev.parameter_id is None or not self._nominal(ev.baseline):
                continue
            param = s.get(Parameter, ev.parameter_id)
            ev.parameter = param
            s.flush()
            if ev.severity == "ALARM" and not self._open_anomaly(s, param.code, ev.time):
                # one anomaly entry per parameter (not per series of a dimensioned parameter)
                timeline.add_entry(s, "ANOMALY", f"{param.name}: {ev.limit_text}", ev.time, None,
                                   event_id=ev.id, product_id=ev.product_id, baseline=ev.baseline,
                                   attributes={"parameter": param.code})
            for r in self.rules.values():
                if r.kind == "limit" and param.code in self.watches(r):
                    try:
                        r.fn(RuleContext(self, s, r), ev)
                    except Exception:  # noqa: BLE001
                        log.exception("Rule %s failed on event %s", r.code, ev.id)
                        continue
                    ev.rule, ev.category = r.code, "action"
                    s.flush()
                    timeline.add_entry(s, "ACTION", ev.recommended_action or r.description, ev.time, None,
                                       rule=r.code, event_id=ev.id, baseline=ev.baseline)

    @staticmethod
    def _open_anomaly(s: Session, code: str, t: datetime) -> bool:
        return s.scalar(select(TimelineEntry.id).where(
            TimelineEntry.type == "ANOMALY", TimelineEntry.attributes == json.dumps({"parameter": code}),
            (TimelineEntry.stop.is_(None)) | (TimelineEntry.stop >= t)).limit(1)) is not None

    def on_cleared(self, s: Session, events: list[Event]) -> None:
        """Close the timeline entries (anomaly / action) of cleared events."""
        for ev in events:
            for entry in s.scalars(select(TimelineEntry).where(TimelineEntry.event_id == ev.id,
                                                              TimelineEntry.stop.is_(None))):
                entry.stop = ev.cleared_at

    # ------------------------------------------------------------------ trend rules
    def evaluate_trends(self, s: Session) -> tuple[list[Event], list[Event]]:
        new: list[Event] = []
        cleared: list[Event] = []
        for r in self.rules.values():
            if r.kind != "trend":
                continue
            try:
                n, c = self._trend(s, r)
            except Exception:  # noqa: BLE001
                log.exception("Trend rule %s failed", r.code)
                continue
            new += n
            cleared += c
        return new, cleared

    def _trend(self, s: Session, r: Rule) -> tuple[list[Event], list[Event]]:
        """Evaluate the slope every `step_hours` of data time. The condition must hold for `confirm_steps`
        consecutive evaluations to raise (persistence) and the slope must fall below `clear_ratio` x threshold
        for as many evaluations to clear (hysteresis). Samples already violating a limit are excluded
        (`exclude_violations`): excursions are handled by limit events, a drift rule warns within the range."""
        cfg = self.config(r)
        param = s.scalar(select(Parameter).where(Parameter.code == cfg["parameter"]))
        if param is None:
            return [], []
        dims = cfg.get("dims", "")
        window = timedelta(days=float(cfg.get("window_days", 10)))
        step = timedelta(hours=float(cfg.get("step_hours", 12)))
        threshold = float(cfg["max_slope_per_day"])
        min_samples = int(cfg.get("min_samples", 20))
        confirm = max(1, int(cfg.get("confirm_steps", 2)))
        clear_below = float(cfg.get("clear_ratio", 0.5)) * threshold
        nominal_only = bool(cfg.get("exclude_violations", True))
        base = select(Sample.time).where(Sample.parameter_id == param.id, Sample.dims == dims,
                                         Sample.baseline.in_([self.nominal, INDEPENDENT]))
        first = s.scalar(base.with_only_columns(func.min(Sample.time)))
        latest = s.scalar(base.with_only_columns(func.max(Sample.time)))
        if first is None:
            return [], []
        key = f"rule.{r.code}"
        st = json.loads(get_state(s, key) or "{}")
        t = parse_iso(st["until"]) if st.get("until") else datetime(first.year, first.month, first.day) + window
        above, below = int(st.get("above", 0)), int(st.get("below", 0))
        new: list[Event] = []
        cleared: list[Event] = []
        ctx = RuleContext(self, s, r)
        while t + step <= latest:
            t += step
            pts = ctx.samples(param.code, t - window, t, dims=dims, nominal_only=nominal_only)
            if len(pts) < min_samples:
                continue
            t0 = pts[0][0]
            td = np.array([(p[0] - t0).total_seconds() / 86400.0 for p in pts])
            y = np.array([p[1] for p in pts])
            slope = theil_sen_slope(td, y)
            above = above + 1 if abs(slope) > threshold else 0
            below = below + 1 if abs(slope) < clear_below else 0
            ev = s.scalar(select(Event).where(Event.rule == r.code, Event.cleared_at.is_(None)))
            if ev is not None:
                if below >= confirm:
                    ev.cleared_at = t
                    if ev.status == "ACKNOWLEDGED":
                        ev.status = "CLOSED"
                    cleared.append(ev)
                    self.on_cleared(s, [ev])
                else:
                    ev.last_time, ev.last_value = t, slope
                    if abs(slope) > threshold:
                        ev.n_violations += 1
                    if ev.worst_value is None or abs(slope) > abs(ev.worst_value):
                        ev.worst_value = slope
                continue
            if above >= confirm:
                ev = Event(parameter_id=param.id, severity=cfg.get("severity", "WARNING"), time=t, value=slope,
                           limit_text="", last_time=t, last_value=slope, worst_value=slope, n_violations=1,
                           status="OPEN", baseline=self.nominal, dims=dims, category="action", rule=r.code,
                           title=f"{r.description} ({slope:+.4g} {param.unit}/day, threshold {threshold:g})")
                s.add(ev)
                s.flush()
                try:
                    r.fn(ctx, ev, slope=slope, points=pts)
                except Exception:  # noqa: BLE001
                    log.exception("Trend rule %s action failed", r.code)
                timeline.add_entry(s, "ACTION", ev.recommended_action or r.description, t, None, rule=r.code,
                                   event_id=ev.id, baseline=self.nominal)
                new.append(ev)
        set_state(s, key, json.dumps({"until": iso(t), "above": above, "below": below}))
        return new, cleared

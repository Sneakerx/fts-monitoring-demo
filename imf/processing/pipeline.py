"""Product processing: L1 product -> parameter samples -> limit check -> events."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Event, Parameter, Product, Sample
from ..monitoring.limits import check_sample
from .l1reader import L1Product
from .registry import load_plugins

log = logging.getLogger(__name__)


@dataclass
class ProcessingResult:
    values: dict[str, float] = field(default_factory=dict)
    new_events: list[Event] = field(default_factory=list)
    cleared_events: list[Event] = field(default_factory=list)
    algorithm_errors: dict[str, str] = field(default_factory=dict)


class Pipeline:
    def __init__(self, characterisation: dict):
        self.characterisation = characterisation
        self.algorithms = load_plugins()

    def derive(self, path: Path) -> tuple[dict[str, float], dict[str, str]]:
        """Run all registered algorithms on one product. Failing algorithms do not stop the others."""
        values: dict[str, float] = {}
        errors: dict[str, str] = {}
        ctx = dict(self.characterisation)
        with L1Product(path) as l1:
            for alg in self.algorithms.values():
                try:
                    values.update(alg.fn(l1, ctx))
                except Exception as exc:  # noqa: BLE001 - isolate plug-in failures
                    errors[alg.name] = f"{type(exc).__name__}: {exc}"
                    log.exception("Algorithm %s failed on %s", alg.name, path.name)
        return values, errors

    def process(self, s: Session, product: Product, path: Path) -> ProcessingResult:
        values, errors = self.derive(path)
        res = ProcessingResult(values=values, algorithm_errors=errors)
        t = product.sensing_start + (product.sensing_stop - product.sensing_start) / 2
        params = s.scalars(select(Parameter).options(
            selectinload(Parameter.limits), selectinload(Parameter.state)).order_by(Parameter.sort_order))
        for p in params:
            if p.code not in values:
                continue
            v = values[p.code]
            chk = check_sample(s, p, v, t, product.id)
            s.add(Sample(parameter_id=p.id, product_id=product.id, time=t, value=v, level=chk.level))
            res.new_events += chk.new_events
            res.cleared_events += chk.cleared
        return res

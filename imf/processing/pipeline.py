"""Product processing: product -> parameter samples / vector samples -> limit check -> events."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import INDEPENDENT, Event, Parameter, Product, Sample, VectorSample
from ..monitoring.limits import check_sample
from ..timeutil import utcnow
from .readers import open_product
from .registry import Algorithm, Output, load_plugins, normalise

log = logging.getLogger(__name__)


@dataclass
class ProcessingResult:
    values: dict = field(default_factory=dict)
    new_events: list[Event] = field(default_factory=list)
    cleared_events: list[Event] = field(default_factory=list)
    algorithm_errors: dict[str, str] = field(default_factory=dict)
    n_samples: int = 0
    n_vectors: int = 0


def compact(values) -> str:
    """JSON list with 7 significant digits (vector storage)."""
    return json.dumps([float(f"{float(v):.7g}") for v in values], separators=(",", ":"))


def store_vector(s: Session, p: Parameter, vec, time: datetime, product_id: int | None, baseline: str,
                 entry_type: str, source: str) -> VectorSample:
    vs = VectorSample(parameter_id=p.id, product_id=product_id, time=time, dims=vec.dims, baseline=baseline,
                      axis=compact(vec.axis), values=compact(vec.values), entry_type=entry_type, source=source,
                      entry_time=utcnow())
    s.add(vs)
    return vs


def store_sample(s: Session, p: Parameter, value: float, time: datetime, product_id: int | None,
                 baseline: str, dims: str, entry_type: str, source: str, check: bool = True):
    """Limit-check (in data-time order) and store one sample. Returns the CheckResult (or None)."""
    chk = check_sample(s, p, value, time, product_id, baseline, dims) if check else None
    s.add(Sample(parameter_id=p.id, product_id=product_id, time=time, value=value,
                 level=chk.level if chk else 0, dims=dims, baseline=baseline, entry_type=entry_type,
                 source=source, entry_time=utcnow()))
    return chk


class Pipeline:
    def __init__(self, characterisation: dict):
        self.characterisation = characterisation
        self.algorithms = load_plugins()

    def algorithms_for(self, product_type: str) -> list[Algorithm]:
        return [a for a in self.algorithms.values() if product_type in a.inputs]

    def derive_outputs(self, path: Path | None, product_type: str = "FTS_L1B",
                       header: dict | None = None) -> tuple[list[tuple[Output, Algorithm]], dict[str, str]]:
        """Run all algorithms registered for the product type. Failing algorithms do not stop the others."""
        outputs: list[tuple[Output, Algorithm]] = []
        errors: dict[str, str] = {}
        ctx = dict(self.characterisation)
        with open_product(product_type, path, header) as product:
            for alg in self.algorithms_for(product_type):
                try:
                    outputs += [(o, alg) for o in normalise(alg.fn(product, ctx))]
                except Exception as exc:  # noqa: BLE001 - isolate plug-in failures
                    errors[alg.name] = f"{type(exc).__name__}: {exc}"
                    log.exception("Algorithm %s failed on %s", alg.name, path.name if path else product_type)
        return outputs, errors

    def derive(self, path: Path | None, product_type: str = "FTS_L1B",
               header: dict | None = None) -> tuple[dict, dict[str, str]]:
        """Raw algorithm results {code: float | [Dim] | Vector} - convenient for tests and tools."""
        values: dict = {}
        errors: dict[str, str] = {}
        ctx = dict(self.characterisation)
        with open_product(product_type, path, header) as product:
            for alg in self.algorithms_for(product_type):
                try:
                    values.update(alg.fn(product, ctx))
                except Exception as exc:  # noqa: BLE001
                    errors[alg.name] = f"{type(exc).__name__}: {exc}"
        return values, errors

    def process(self, s: Session, product: Product, path: Path | None = None, header: dict | None = None,
                entry_type: str = "derived") -> ProcessingResult:
        outputs, errors = self.derive_outputs(path, product.product_type, header)
        res = ProcessingResult(algorithm_errors=errors)
        t = product.sensing_start + (product.sensing_stop - product.sensing_start) / 2
        baseline = product.baseline if product.level == "L1" else INDEPENDENT
        params = {p.code: p for p in s.scalars(select(Parameter).options(selectinload(Parameter.limits)))}
        order = {code: (p.sort_order, code) for code, p in params.items()}
        for out, alg in sorted(outputs, key=lambda oa: (order.get(oa[0].code, (1 << 30, "")), oa[0].dims)):
            p = params.get(out.code)
            if p is None:
                continue  # output of a plug-in that is not configured as a parameter
            if out.vector is not None:
                store_vector(s, p, out.vector, t, product.id, baseline, entry_type, alg.label)
                res.n_vectors += 1
                continue
            res.values[out.code if not out.dims else f"{out.code}[{out.dims}]"] = out.value
            chk = store_sample(s, p, out.value, t, product.id, baseline, out.dims, entry_type, alg.label)
            res.n_samples += 1
            res.new_events += chk.new_events
            res.cleared_events += chk.cleared
        return res

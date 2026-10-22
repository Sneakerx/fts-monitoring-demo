"""Plug-in registry for parameter-derivation algorithms.

An algorithm is a function `(product, ctx: dict) -> dict[str, Output]` registered with the codes
of the parameters it produces and the product levels it reads (`L0` or `L1`; the configured product
type of each level is resolved by the pipeline, so plug-ins do not depend on mission product names). New instrument parameters are added
by writing a function in any module imported by `load_plugins()` - no change to the pipeline is
needed:

    @algorithm("NESR_B1", "NESR_B2", "NESR_B3")                 # default input: L1
    def nesr(product, ctx):
        return {"NESR_B1": 24.8, "NESR_B2": 10.1, "NESR_B3": 3.0}

    @algorithm("L0_SCANS", inputs=("L0",))                       # header-only L0 product
    def l0_header(product, ctx):
        return {"L0_SCANS": product.number("numberOfScans")}

    @algorithm("NESR_IDX")                                       # one value per series
    def nesr_index(product, ctx):
        return {"NESR_IDX": [Dim(1.02, dims(sb=1)), ...]}

    @algorithm("NESR_SPECTRAL")                                  # spectrum / profile
    def nesr_spectral(product, ctx):
        return {"NESR_SPECTRAL": [Vector(nu, nesr, dims(band="B1")), ...]}

Plug-in modules are the built-in ones, `processing.plugin_modules` from the configuration and
modules registered by installed packages under the entry-point group `imf.algorithms`.
External (non-Python) tools are wrapped by `register_external_tools` (see `ExternalToolCfg`).
"""
from __future__ import annotations

import importlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Callable, Iterable

log = logging.getLogger(__name__)

AlgorithmFn = Callable[[object, dict], dict]


@dataclass(frozen=True)
class Dim:
    """A scalar value of one series of a dimensioned parameter."""
    value: float
    dims: str = ""


@dataclass(frozen=True)
class Vector:
    """A spectrum / profile: values over an axis (e.g. wavenumber)."""
    axis: list[float]
    values: list[float]
    dims: str = ""


@dataclass(frozen=True)
class Output:
    code: str
    dims: str
    value: float | None = None
    vector: Vector | None = None


def dims(**kw) -> str:
    """Normalised dimension key, e.g. dims(sb=3, band="B1") -> 'band=B1;sb=3'."""
    return ";".join(f"{k}={kw[k]}" for k in sorted(kw))


def parse_dims(key: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in key.split(";") if "=" in part)


def normalise(values: dict) -> list[Output]:
    """Flatten algorithm results (float | Dim | Vector | lists thereof) into outputs."""
    out: list[Output] = []
    for code, v in values.items():
        for item in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(item, Vector):
                out.append(Output(code, item.dims, vector=item))
            elif isinstance(item, Dim):
                out.append(Output(code, item.dims, value=float(item.value)))
            else:
                out.append(Output(code, "", value=float(item)))
    return out


@dataclass(frozen=True)
class Algorithm:
    name: str
    outputs: tuple[str, ...]
    fn: AlgorithmFn
    version: str
    inputs: tuple[str, ...] = ("L1",)  # product levels
    kind: str = "python"  # python | external

    @property
    def label(self) -> str:
        return f"{self.name}@{self.version}"


_REGISTRY: dict[str, Algorithm] = {}

PLUGIN_MODULES = ["imf.processing.fts_algorithms"]
ENTRY_POINT_GROUP = "imf.algorithms"
_extra_modules: list[str] = []


def algorithm(*outputs: str, version: str = "1.0",
              inputs: Iterable[str] = ("L1",)) -> Callable[[AlgorithmFn], AlgorithmFn]:
    def deco(fn: AlgorithmFn) -> AlgorithmFn:
        _REGISTRY[fn.__name__] = Algorithm(fn.__name__, tuple(outputs), fn, version, tuple(inputs))
        return fn
    return deco


def configure(plugin_modules: Iterable[str] = (), external_tools: Iterable = ()) -> None:
    """Register configured plug-in modules and external tools (called once at start-up)."""
    for m in plugin_modules:
        if m not in _extra_modules:
            _extra_modules.append(m)
    register_external_tools(external_tools)


def _entry_point_modules() -> list[str]:
    try:
        return [ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)]
    except Exception:  # noqa: BLE001 - broken metadata must not stop the IMF
        log.exception("Cannot read entry points %s", ENTRY_POINT_GROUP)
        return []


def load_plugins() -> dict[str, Algorithm]:
    for mod in [*PLUGIN_MODULES, *_extra_modules, *_entry_point_modules()]:
        try:
            importlib.import_module(mod)
        except ImportError:
            log.exception("Cannot import algorithm plug-in module %s", mod)
    return dict(_REGISTRY)


def outputs_index() -> dict[str, Algorithm]:
    """Map parameter code -> producing algorithm."""
    return {code: alg for alg in load_plugins().values() for code in alg.outputs}


# --------------------------------------------------------------------------- external tools
@dataclass
class _ExternalRunner:
    command: list[str]
    timeout_s: float
    outputs: tuple[str, ...] = field(default_factory=tuple)

    def __call__(self, product, ctx: dict) -> dict:
        path = getattr(product, "path", None)
        if path is None:
            raise ValueError("external tools need a downloaded product")
        res = subprocess.run([*self.command, str(path)], capture_output=True, text=True,
                             timeout=self.timeout_s, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"exit code {res.returncode}: {res.stderr.strip()[:300]}")
        data = json.loads(res.stdout)
        return {k: v for k, v in data.items() if k in self.outputs}


def register_external_tools(tools: Iterable) -> None:
    """Contract: `command + [product_path]` prints a JSON object {"CODE": value, ...} on stdout."""
    for t in tools:
        runner = _ExternalRunner(list(t.command), t.timeout_s, tuple(t.outputs))
        _REGISTRY[f"ext_{t.name}"] = Algorithm(f"ext_{t.name}", tuple(t.outputs), runner, t.version,
                                               tuple(t.inputs), kind="external")

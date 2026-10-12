"""Plug-in registry for parameter-derivation algorithms.

An algorithm is a function `(product: L1Product, ctx: dict) -> dict[str, float]` registered with
the codes of the parameters it produces. New instrument parameters are added by writing a
function in any module imported by `load_plugins()` - no change to the pipeline is needed:

    @algorithm("NESR_B1", "NESR_B2", "NESR_B3")
    def nesr(product, ctx):
        ...
        return {"NESR_B1": 24.8, "NESR_B2": 10.1, "NESR_B3": 3.0}
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

AlgorithmFn = Callable[[object, dict], dict[str, float]]


@dataclass(frozen=True)
class Algorithm:
    name: str
    outputs: tuple[str, ...]
    fn: AlgorithmFn
    version: str


_REGISTRY: dict[str, Algorithm] = {}

PLUGIN_MODULES = ["imf.processing.fts_algorithms"]


def algorithm(*outputs: str, version: str = "1.0") -> Callable[[AlgorithmFn], AlgorithmFn]:
    def deco(fn: AlgorithmFn) -> AlgorithmFn:
        _REGISTRY[fn.__name__] = Algorithm(fn.__name__, tuple(outputs), fn, version)
        return fn
    return deco


def load_plugins() -> dict[str, Algorithm]:
    for mod in PLUGIN_MODULES:
        importlib.import_module(mod)
    return dict(_REGISTRY)


def outputs_index() -> dict[str, Algorithm]:
    """Map parameter code -> producing algorithm."""
    return {code: alg for alg in load_plugins().values() for code in alg.outputs}

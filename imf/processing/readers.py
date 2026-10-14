"""Reader registry: one reader per product type, isolating the algorithms from the product formats.

* full products are opened with the reader registered for their type (HDF5 L1b, ancillary data, ...)
* header-only products (mode 'header', e.g. L0) are represented by their catalogue attributes

Replacing a synthetic format by the real ICD only means replacing / registering its reader.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import h5py
import numpy as np

from ..timeutil import parse_iso
from .l1reader import L1Product, ProductFormatError


class AncillaryProduct:
    """AUX_ANC: corner-cube turn-around positions and on-board fringe counter (HDF5)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self._f = h5py.File(self.path, "r")
        except OSError as exc:
            raise ProductFormatError(f"Cannot open product {self.path.name}: {exc}") from exc

    def __enter__(self) -> "AncillaryProduct":
        return self

    def __exit__(self, *exc) -> None:
        self._f.close()

    def _ds(self, name: str) -> np.ndarray:
        try:
            return self._f[f"Ancillary/{name}"][()]
        except KeyError as exc:
            raise ProductFormatError(f"Missing dataset Ancillary/{name} in {self.path.name}") from exc

    @property
    def attrs(self) -> dict:
        return dict(self._f.attrs)

    def turnaround_position(self) -> np.ndarray:
        return self._ds("turnaround_position").astype(np.float64)

    def fringe_count(self) -> np.ndarray:
        return self._ds("fringe_count").astype(np.float64)


class HeaderProduct:
    """Header-only product: catalogue attributes (no download)."""
    path = None

    def __init__(self, header: dict, product_type: str = "", sensing_start: datetime | None = None,
                 sensing_stop: datetime | None = None):
        self.header = dict(header)
        self.product_type = product_type
        self.sensing_start = sensing_start
        self.sensing_stop = sensing_stop

    def __enter__(self) -> "HeaderProduct":
        return self

    def __exit__(self, *exc) -> None:
        pass

    @property
    def attrs(self) -> dict:
        return self.header

    def get(self, key: str, default=None):
        return self.header.get(key, default)

    def number(self, key: str) -> float:
        try:
            return float(self.header[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductFormatError(f"Header field {key} missing or not numeric") from exc


READERS: dict[str, type] = {
    "FTS_L1B": L1Product,
    "AUX_ANC": AncillaryProduct,
}


def register_reader(product_type: str, reader: type) -> None:
    READERS[product_type] = reader


def open_product(product_type: str, path: Path | None = None, header: dict | None = None):
    """Return a context-managed reader for a downloaded product, or a header-only product."""
    if path is None:
        hdr = header or {}
        start = hdr.get("_sensing_start")
        stop = hdr.get("_sensing_stop")
        return HeaderProduct(hdr, product_type, parse_iso(start) if start else None,
                             parse_iso(stop) if stop else None)
    reader = READERS.get(product_type)
    if reader is None:
        raise ProductFormatError(f"No reader registered for product type {product_type}")
    return reader(path)

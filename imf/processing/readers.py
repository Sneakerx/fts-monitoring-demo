"""Readers: isolate the algorithms from the product file formats.

Two layers:

* **file formats** - `DatasetFile` gives uniform access to a hierarchical data file: global attributes and
  arrays by path (`"Data/Band1/wavenumber"`). Implementations: `HDF5File` (h5py) and `NetCDF4File`
  (netCDF4, groups + variables). The format of a product type is configured (`pdgs.product_types[].format`).
* **product levels** - an instrument module registers one adapter per level (`register_reader("L1", ...)`)
  that wraps a `DatasetFile` and offers instrument-specific accessors. Header-only products (mode
  'header', e.g. L0) are represented by their catalogue attributes (`HeaderProduct`).

Replacing a synthetic layout by the real ICD only means replacing / registering the adapter.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np

from ..timeutil import parse_iso


class ProductFormatError(ValueError):
    pass


class DatasetFile:
    """Hierarchical data file: global attributes and arrays addressed by '/'-separated paths."""
    format = ""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __enter__(self) -> "DatasetFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        raise NotImplementedError

    @property
    def attrs(self) -> dict:
        raise NotImplementedError

    def has(self, path: str) -> bool:
        raise NotImplementedError

    def _read(self, path: str) -> np.ndarray:
        raise NotImplementedError

    def read(self, path: str) -> np.ndarray:
        if not self.has(path):
            raise ProductFormatError(f"Missing dataset {path} in {self.path.name}")
        return self._read(path)


def _plain(v):
    """Attribute value as a plain Python value (bytes -> str, numpy scalars -> Python)."""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, np.generic):
        return v.item()
    return v


class HDF5File(DatasetFile):
    format = "hdf5"

    def __init__(self, path: Path):
        super().__init__(path)
        import h5py
        try:
            self._f = h5py.File(self.path, "r")
        except OSError as exc:
            raise ProductFormatError(f"Cannot open product {self.path.name}: {exc}") from exc

    def close(self) -> None:
        self._f.close()

    @property
    def attrs(self) -> dict:
        return {k: _plain(v) for k, v in self._f.attrs.items()}

    def has(self, path: str) -> bool:
        return path in self._f

    def _read(self, path: str) -> np.ndarray:
        return np.asarray(self._f[path][()])


class NetCDF4File(DatasetFile):
    format = "netcdf4"

    def __init__(self, path: Path):
        super().__init__(path)
        import netCDF4
        try:
            self._ds = netCDF4.Dataset(self.path, "r")
        except OSError as exc:
            raise ProductFormatError(f"Cannot open product {self.path.name}: {exc}") from exc
        self._ds.set_auto_mask(False)

    def close(self) -> None:
        self._ds.close()

    @property
    def attrs(self) -> dict:
        return {k: _plain(self._ds.getncattr(k)) for k in self._ds.ncattrs()}

    def _variable(self, path: str):
        *groups, name = path.strip("/").split("/")
        node = self._ds
        for g in groups:
            node = node.groups.get(g)
            if node is None:
                return None
        return node.variables.get(name)

    def has(self, path: str) -> bool:
        return self._variable(path) is not None

    def _read(self, path: str) -> np.ndarray:
        return np.asarray(self._variable(path)[...])


FORMATS: dict[str, type[DatasetFile]] = {"hdf5": HDF5File, "netcdf4": NetCDF4File}


def open_dataset(path: Path, fmt: str = "hdf5") -> DatasetFile:
    cls = FORMATS.get(fmt)
    if cls is None:
        raise ProductFormatError(f"Unsupported file format {fmt!r} (supported: {', '.join(FORMATS)})")
    return cls(path)


class DatasetProduct:
    """Generic downloaded product: a `DatasetFile` plus the product's sensing period from its attributes.
    Instrument adapters derive from it and add instrument-specific accessors."""

    def __init__(self, path: Path, fmt: str = "hdf5"):
        self.path = Path(path)
        self.file = open_dataset(self.path, fmt)

    def __enter__(self) -> "DatasetProduct":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.file.close()

    @property
    def attrs(self) -> dict:
        return self.file.attrs

    def read(self, path: str) -> np.ndarray:
        return self.file.read(path)

    @property
    def sensing_start(self) -> datetime:
        return parse_iso(str(self.attrs["sensing_start"]))

    @property
    def sensing_stop(self) -> datetime:
        return parse_iso(str(self.attrs["sensing_stop"]))


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


# Adapter per product level for downloaded products (instrument modules register their own)
READERS: dict[str, type] = {"L0": DatasetProduct, "L1": DatasetProduct}


def register_reader(level: str, reader: type) -> None:
    """Register the adapter for downloaded products of a level: `reader(path, fmt)`."""
    READERS[level] = reader


def open_product(level: str, path: Path | None = None, header: dict | None = None, fmt: str = "hdf5",
                 product_type: str = ""):
    """Return a context-managed reader for a downloaded product, or a header-only product."""
    if path is None:
        hdr = header or {}
        start = hdr.get("_sensing_start")
        stop = hdr.get("_sensing_stop")
        return HeaderProduct(hdr, product_type, parse_iso(start) if start else None,
                             parse_iso(stop) if stop else None)
    reader = READERS.get(level)
    if reader is None:
        raise ProductFormatError(f"No reader registered for level {level}")
    return reader(path, fmt)

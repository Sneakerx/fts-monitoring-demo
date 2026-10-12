"""Reader for FTS Level-1b products (HDF5). Isolates the algorithms from the file format."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import h5py
import numpy as np

from ..timeutil import parse_iso


class ProductFormatError(ValueError):
    pass


class L1Product:
    BANDS = ("B1", "B2", "B3")

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self._f = h5py.File(self.path, "r")
        except OSError as exc:
            raise ProductFormatError(f"Cannot open product {self.path.name}: {exc}") from exc

    def __enter__(self) -> "L1Product":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._f.close()

    def _ds(self, path: str) -> np.ndarray:
        try:
            return self._f[path][()]
        except KeyError as exc:
            raise ProductFormatError(f"Missing dataset {path} in {self.path.name}") from exc

    # --- metadata ---------------------------------------------------------
    @property
    def attrs(self) -> dict:
        return dict(self._f.attrs)

    @property
    def sensing_start(self) -> datetime:
        return parse_iso(str(self._f.attrs["sensing_start"]))

    @property
    def sensing_stop(self) -> datetime:
        return parse_iso(str(self._f.attrs["sensing_stop"]))

    # --- data access ------------------------------------------------------
    def wavenumber(self, band: str) -> np.ndarray:
        return self._ds(f"Data/Band{band[-1]}/wavenumber")

    def earth_radiance(self, band: str) -> np.ndarray:
        return self._ds(f"Data/Band{band[-1]}/earth_radiance")

    def deep_space_counts(self, band: str) -> np.ndarray:
        return self._ds(f"Calibration/Band{band[-1]}/deep_space_counts").astype(np.float64)

    def blackbody_counts(self, band: str) -> np.ndarray:
        return self._ds(f"Calibration/Band{band[-1]}/blackbody_counts").astype(np.float64)

    def spectral_window(self) -> tuple[np.ndarray, np.ndarray]:
        return self._ds("Calibration/SpectralWindow/wavenumber"), self._ds("Calibration/SpectralWindow/radiance")

    def quality_flags(self) -> np.ndarray:
        return self._ds("Data/quality_flag")

    def housekeeping(self, name: str) -> np.ndarray:
        return self._ds(f"Housekeeping/{name}")

    def temperature(self, name: str) -> np.ndarray:
        return self._ds(f"Housekeeping/Temperatures/{name}")

    def geolocation(self, name: str) -> np.ndarray:
        return self._ds(f"Geolocation/{name}")

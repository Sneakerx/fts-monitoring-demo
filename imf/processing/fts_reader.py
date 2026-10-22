"""Reader adapter for the FTS Level-1b product (HDF5 or netCDF4, same group / variable layout).

Isolates the FTS algorithms from the file format: all data access goes through the generic
`DatasetFile` of `readers.py`.
"""
from __future__ import annotations

import numpy as np

from .readers import DatasetProduct, ProductFormatError  # noqa: F401 - re-exported for plug-ins


class L1Product(DatasetProduct):
    BANDS = ("B1", "B2", "B3")

    def __enter__(self) -> "L1Product":
        return self

    def _ds(self, path: str) -> np.ndarray:
        return self.read(path)

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

    def nesr_index(self) -> np.ndarray:
        """(n_spectrograms, n_subbands) NESR index, spectrally binned by the L1 processor."""
        return self._ds("Quality/nesr_index").astype(np.float64)

    def nesr_spectral(self, band: str) -> np.ndarray:
        """(n_spectrograms, n_channels) spectrally resolved NESR estimate [nW/(cm2 sr cm-1)]."""
        return self._ds(f"Quality/Band{band[-1]}/nesr_spectral").astype(np.float64)

    def housekeeping(self, name: str) -> np.ndarray:
        return self._ds(f"Housekeeping/{name}")

    def temperature(self, name: str) -> np.ndarray:
        return self._ds(f"Housekeeping/Temperatures/{name}")

    def geolocation(self, name: str) -> np.ndarray:
        return self._ds(f"Geolocation/{name}")

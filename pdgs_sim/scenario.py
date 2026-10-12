"""Ground-truth instrument behaviour of the simulated FTS, including injected anomalies.

The scenario is a deterministic function of the product index (one product = one orbit),
so the simulator can regenerate any product on demand and tests can compare the
IMF-derived parameters against the truth.

Mission-day timeline of injected events (d = days since mission start):
  d  0-40   ice build-up on cold optics (transmission loss grows ~0.21 %/day)
  d 30.2-30.7  PDGS outage -> no products
  d 40-41   decontamination (instrument heated, no products); ice reset
  d 41-     ice build-up resumes (~0.13 %/day)
  d 45.0-45.3  burst of missing scans (on-board data handling)
  d 52.4-54.0  cryocooler anomaly -> detector temperature excursion (+3.5 K), NESR degradation
  d 61.0-61.4  micro-vibration -> scan speed instability
  d 70-74   metrology laser mode hop (+3.6 ppm) until re-tuning on day 74
  d 80-     slow co-registration drift
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

ORBIT_PERIOD_S = 101.0 * 60.0
DAY_S = 86400.0
NOMINAL_SCANS = 30
SPECTRA_PER_SCAN = 120

BANDS = {"B1": (645.0, 1210.0), "B2": (1210.0, 2000.0), "B3": (2000.0, 2760.0)}
N_CHANNELS = 128
NESR_BASE = {"B1": 25.0, "B2": 10.0, "B3": 3.0}  # nW/(cm2 sr cm-1)
REFERENCE_GAIN = {"B1": (1000.0, 0.10), "B2": (1500.0, -0.05), "B3": (4000.0, 0.08)}
ICE_CENTER, ICE_SIGMA = 830.0, 60.0
LASER_NOMINAL_NM = 1550.0
ZPD_NOMINAL = 2048
SPECTRAL_LINE_CM1 = 712.0

GAPS = [(30.2, 30.7), (40.0, 41.0)]  # PDGS outage, decontamination


@dataclass
class Truth:
    index: int
    day: float
    det_temp: float
    bs_temp: float
    bb_temp: float
    ice_loss: float          # fraction at absorption peak
    aging: float             # fractional gain change (negative = loss)
    laser_ppm: float
    spec_shift_ppm: float
    ils_fwhm: float
    zpd_offset: float
    ifg_amp: float
    scan_speed_stab: float   # percent
    valid_ratio: float       # percent
    missing_scans: int
    coreg_km: float
    nesr: dict = field(default_factory=dict)
    corrupt: bool = False


def mission_day(index: int) -> float:
    return index * ORBIT_PERIOD_S / DAY_S


def is_gap(index: int) -> bool:
    d = mission_day(index)
    return any(a <= d < b for a, b in GAPS)


def ice_shape(nu: np.ndarray | float) -> np.ndarray | float:
    return np.exp(-0.5 * ((np.asarray(nu) - ICE_CENTER) / ICE_SIGMA) ** 2)


def _bump(d: float, center: float, sigma: float) -> float:
    return math.exp(-0.5 * ((d - center) / sigma) ** 2)


def truth(index: int) -> Truth:
    rng = np.random.default_rng(index * 7919 + 13)
    d = mission_day(index)
    orbit_phase = rng.uniform(0, 2 * math.pi)

    # Thermal
    det = 90.0 + 0.04 * math.sin(orbit_phase) + rng.normal(0, 0.02) + 3.5 * _bump(d, 53.2, 0.5)
    bs = 293.0 + 0.3 * math.sin(2 * math.pi * d / 60.0) + rng.normal(0, 0.03)
    bb = 290.0 + 0.04 * math.sin(orbit_phase + 1.0) + rng.normal(0, 0.01)

    # Ice contamination with decontamination on day 40
    ice = 0.0021 * d if d < 40.0 else 0.0013 * max(0.0, d - 41.0)

    aging = -0.0001 * d

    # Metrology laser: slow ageing + mode hop days 70-74
    laser = 0.01 * d + rng.normal(0, 0.15) + (3.6 if 70.0 <= d < 74.0 else 0.0)
    spec = 0.6 + 0.5 * laser + 0.2 * math.sin(2 * math.pi * d / 27.0) + rng.normal(0, 0.08)
    ils = 0.25 + 0.0003 * d + rng.normal(0, 0.002)
    zpd = rng.normal(0, 0.4) + (2.0 if 70.0 <= d < 74.0 else 0.0)

    stab = abs(0.3 + rng.normal(0, 0.05)) + (1.1 if 61.0 <= d < 61.4 else 0.0)

    valid = 99.6 - abs(rng.normal(0, 0.25))
    if rng.uniform() < 0.02:
        valid = rng.uniform(94.0, 97.0)

    missing = int(rng.poisson(0.15))
    if 45.0 <= d < 45.3:
        missing = int(rng.integers(3, 8))

    coreg = 0.3 + abs(rng.normal(0, 0.03)) + (0.022 * (d - 80.0) if d > 80.0 else 0.0)

    throughput = 1.0 - 0.35 * ice
    amp = 30000.0 * throughput * (1.0 - 0.02 * (det - 90.0)) + rng.normal(0, 150)

    nesr = {b: NESR_BASE[b] * (1.0 + 0.3 * (det - 90.0)) for b in BANDS}

    return Truth(index=index, day=d, det_temp=det, bs_temp=bs, bb_temp=bb, ice_loss=ice, aging=aging,
                 laser_ppm=laser, spec_shift_ppm=spec, ils_fwhm=ils, zpd_offset=zpd, ifg_amp=amp,
                 scan_speed_stab=stab, valid_ratio=valid, missing_scans=missing, coreg_km=coreg,
                 nesr=nesr, corrupt=(index % 337 == 200))

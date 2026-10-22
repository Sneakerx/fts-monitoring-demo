"""Ground-truth instrument behaviour of the simulated FTS, including injected anomalies.

The scenario is a deterministic function of the product index (one product = one orbit),
so the simulator can regenerate any product on demand and tests can compare the
IMF-derived parameters against the truth.

Mission-day timeline of injected events (d = days since mission start):
  d  0-40   ice build-up on cold optics (transmission loss grows ~0.21 %/day)
  d 15.3, 47.6, 78.9  calibration mode (L0 header: observationMode CALIBRATION)
  d 30.2-30.7  PDGS outage -> no products
  d 40-41   decontamination (instrument heated, no products); ice reset
  d 41-     ice build-up resumes (~0.13 %/day)
  d 45.0-45.3  burst of missing scans and L0 packet errors (on-board data handling)
  d 52.4-54.0  cryocooler anomaly -> detector temperature excursion (+3.5 K), NESR degradation
  d 60-     slow ZPD drift (corner-cube mechanism)
  d 61.0-61.4  micro-vibration -> scan speed instability
  d 63.0-63.25 L1 processor anomaly -> L0 available but no L1 (baseline B03 only)
  d 65-     thermistor degradation on the optical bench (noise + drift vs. the PRT)
  d 70-74   metrology laser mode hop (+3.6 ppm) until re-tuning on day 74 (SSD out of requirement)
  d 75-     slow NESR drift (+0.4 %/day)
  d 77-     delay between reference and science signal increases (+0.35 us)
  d 80-     slow co-registration drift
  d 85.0-86.5  radiometric feature in the calibration window (ISRF impact)

Baselines: B03 is the nominal L1 processing baseline. B04 (reprocessing) removes the constant
spectral calibration offset, has no L1 processing gap and no corrupted products.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

ORBIT_PERIOD_S = 101.0 * 60.0
DAY_S = 86400.0
NOMINAL_SCANS = 30
SPECTRA_PER_SCAN = 120
N_CAL = 10
N_SUBBANDS = 6

BANDS = {"B1": (645.0, 1210.0), "B2": (1210.0, 2000.0), "B3": (2000.0, 2760.0)}
N_CHANNELS = 128
NESR_BASE = {"B1": 25.0, "B2": 10.0, "B3": 3.0}  # nW/(cm2 sr cm-1)
REFERENCE_GAIN = {"B1": (1000.0, 0.10), "B2": (1500.0, -0.05), "B3": (4000.0, 0.08)}
ICE_CENTER, ICE_SIGMA = 830.0, 60.0
LASER_NOMINAL_NM = 1550.0
ZPD_NOMINAL = 2048
OPD_MAX_CM = 0.8             # -> spectral sampling distance 1 / (2 OPD) = 0.625 cm-1
REF_SCI_DELAY_US = 2.0
SPECTRAL_LINE_CM1 = 712.0

GAPS = [(30.2, 30.7), (40.0, 41.0)]  # PDGS outage, decontamination
L1_GAPS = [(63.0, 63.25)]            # L1 processing anomaly (nominal baseline only)
CALIBRATION_DAYS = [15.3, 47.6, 78.9]  # calibration mode, 2.4 h each
CALIBRATION_DURATION_D = 0.1
BASELINES = {"B03": "03.02", "B04": "04.00"}


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
    # --- extended scenario ---
    nesr_scale: float = 1.0      # NESR relative to the template (cryocooler + drift)
    zpd_drift: float = 0.0       # mechanical ZPD drift [samples]
    ref_sci_delay_us: float = REF_SCI_DELAY_US
    isrf_feature: float = 0.0    # depth of the radiometric feature (fraction)
    bench_temp: float = 295.0
    th_bias: float = 0.05
    th_noise: float = 0.01
    packet_errors: dict = field(default_factory=dict)
    cal_views: dict = field(default_factory=dict)


def mission_day(index: int) -> float:
    return index * ORBIT_PERIOD_S / DAY_S


def is_gap(index: int) -> bool:
    d = mission_day(index)
    return any(a <= d < b for a, b in GAPS)


def is_l1_gap(index: int, baseline: str = "B03") -> bool:
    d = mission_day(index)
    return baseline == "B03" and any(a <= d < b for a, b in L1_GAPS)


def ice_shape(nu: np.ndarray | float) -> np.ndarray | float:
    return np.exp(-0.5 * ((np.asarray(nu) - ICE_CENTER) / ICE_SIGMA) ** 2)


def nesr_template(band: str, nu: np.ndarray) -> np.ndarray:
    """Expected spectral NESR shape [nW/(cm2 sr cm-1)] - also delivered as reference data file."""
    lo, hi = BANDS[band]
    x = (np.asarray(nu) - 0.5 * (lo + hi)) / (0.5 * (hi - lo))
    return NESR_BASE[band] * (1.0 + 0.3 * x**2)


def _bump(d: float, center: float, sigma: float) -> float:
    return math.exp(-0.5 * ((d - center) / sigma) ** 2)


def truth(index: int) -> Truth:
    rng = np.random.default_rng(index * 7919 + 13)
    rng2 = np.random.default_rng(index * 7919 + 17)  # extended scenario (keeps the original draws stable)
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
    zpd_drift = 0.05 * max(0.0, d - 60.0)
    zpd = rng.normal(0, 0.4) + (2.0 if 70.0 <= d < 74.0 else 0.0) + zpd_drift

    stab = abs(0.3 + rng.normal(0, 0.05)) + (1.1 if 61.0 <= d < 61.4 else 0.0)

    valid = 99.6 - abs(rng.normal(0, 0.25))
    if rng.uniform() < 0.02:
        valid = rng.uniform(94.0, 97.0)

    missing = int(rng.poisson(0.15))
    burst = 45.0 <= d < 45.3
    if burst:
        missing = int(rng.integers(3, 8))

    coreg = 0.3 + abs(rng.normal(0, 0.03)) + (0.022 * (d - 80.0) if d > 80.0 else 0.0)

    throughput = 1.0 - 0.35 * ice
    amp = 30000.0 * throughput * (1.0 - 0.02 * (det - 90.0)) + rng.normal(0, 150)

    nesr_drift = 1.0 + 0.004 * max(0.0, d - 75.0)
    nesr_scale = (1.0 + 0.3 * (det - 90.0)) * nesr_drift
    nesr = {b: NESR_BASE[b] * nesr_scale for b in BANDS}

    # Extended scenario ----------------------------------------------------------
    delay = REF_SCI_DELAY_US + rng2.normal(0, 0.005) + (0.35 if d >= 77.0 else 0.0)
    isrf = 0.02 if 85.0 <= d < 86.5 else 0.0
    bench = 295.0 + 0.2 * math.sin(2 * math.pi * d / 30.0) + rng2.normal(0, 0.01)
    th_bias = 0.05 + (0.004 * (d - 65.0) if d > 65.0 else 0.0)
    th_noise = 0.08 if d > 65.0 else 0.01
    packet_errors = {"BB": int(rng2.poisson(0.05)), "DS": int(rng2.poisson(0.05)),
                     "EO": int(rng2.poisson(0.2)) + (int(rng2.integers(4, 9)) if burst else 0)}
    cal_views = {"BB": N_CAL - int(rng2.uniform() < 0.01), "DS": N_CAL - int(rng2.uniform() < 0.01)}

    return Truth(index=index, day=d, det_temp=det, bs_temp=bs, bb_temp=bb, ice_loss=ice, aging=aging,
                 laser_ppm=laser, spec_shift_ppm=spec, ils_fwhm=ils, zpd_offset=zpd, ifg_amp=amp,
                 scan_speed_stab=stab, valid_ratio=valid, missing_scans=missing, coreg_km=coreg,
                 nesr=nesr, corrupt=(index % 337 == 200), nesr_scale=nesr_scale, zpd_drift=zpd_drift,
                 ref_sci_delay_us=delay, isrf_feature=isrf, bench_temp=bench, th_bias=th_bias,
                 th_noise=th_noise, packet_errors=packet_errors, cal_views=cal_views)


def calibration_id(index: int) -> int | None:
    """Number (1..) of the calibration whose window overlaps the orbit, or None (nominal observation)."""
    a, b = mission_day(index), mission_day(index + 1)
    for k, d0 in enumerate(CALIBRATION_DAYS):
        if a < d0 + CALIBRATION_DURATION_D and b > d0:
            return k + 1
    return None


def l0_header(index: int) -> dict:
    """Header information of the L0 product of an orbit (exposed as catalogue attributes)."""
    t = truth(index)
    hdr = {"observationMode": "NOMINAL", "numberOfScans": NOMINAL_SCANS - t.missing_scans,
           "numberOfViewsBB": t.cal_views["BB"], "numberOfViewsDS": t.cal_views["DS"],
           "packetErrors": sum(t.packet_errors.values()), "blackbodyTemperature": round(t.bb_temp, 3)}
    cal = calibration_id(index)
    if cal is not None:
        hdr |= {"observationMode": "CALIBRATION", "calibrationId": cal}
    return hdr

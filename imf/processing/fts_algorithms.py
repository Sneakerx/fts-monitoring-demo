"""Monitoring-parameter algorithms for the Fourier-Transform Spectrometer.

Each function derives one or more parameters from a single L1b product. `ctx` holds the
instrument characterisation data (config: `characterisation`) and a per-product cache.
"""
from __future__ import annotations

import numpy as np

from .l1reader import L1Product
from .registry import algorithm

C1 = 1.191042e-5   # mW/(m2 sr cm-4)
C2 = 1.4387752     # cm K
BAND_LIMITS = {"B1": (645.0, 1210.0), "B2": (1210.0, 2000.0), "B3": (2000.0, 2760.0)}


def planck(nu: np.ndarray, t: float) -> np.ndarray:
    return C1 * nu**3 / np.expm1(C2 * nu / t)


def _cached(ctx: dict, key: str, fn):
    cache = ctx.setdefault("_cache", {})
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def reference_gain(ctx: dict, band: str, nu: np.ndarray) -> np.ndarray:
    ref = ctx["reference_gain"][band]
    lo, hi = BAND_LIMITS[band]
    return ref["g0"] * (1.0 + ref["slope"] * (nu - 0.5 * (lo + hi)) / (hi - lo))


def calibration_gain(p: L1Product, band: str, ctx: dict) -> np.ndarray:
    """Two-point (deep space / blackbody) radiometric gain per channel."""
    def compute():
        nu = p.wavenumber(band)
        t_bb = float(np.mean(p.temperature("blackbody")))
        diff = p.blackbody_counts(band).mean(axis=0) - p.deep_space_counts(band).mean(axis=0)
        return diff / planck(nu, t_bb)
    return _cached(ctx, f"gain_{band}", compute)


# --- Radiometric ---------------------------------------------------------------

@algorithm("NESR_B1", "NESR_B2", "NESR_B3")
def nesr(p: L1Product, ctx: dict) -> dict[str, float]:
    out = {}
    for band in L1Product.BANDS:
        gain = calibration_gain(p, band, ctx)
        ds = p.deep_space_counts(band)
        rad = (ds - ds.mean(axis=0)) / gain                     # calibrated deep-space radiance
        nesr_rad = rad.std(axis=0, ddof=1)                      # mW/(m2 sr cm-1) per channel
        out[f"NESR_{band}"] = float(np.median(nesr_rad) * 100.0)  # -> nW/(cm2 sr cm-1)
    return out


@algorithm("GAIN_DRIFT_B1", "GAIN_DRIFT_B2", "GAIN_DRIFT_B3")
def gain_drift(p: L1Product, ctx: dict) -> dict[str, float]:
    out = {}
    for band in L1Product.BANDS:
        rel = calibration_gain(p, band, ctx) / reference_gain(ctx, band, p.wavenumber(band))
        out[f"GAIN_DRIFT_{band}"] = float((np.mean(rel) - 1.0) * 100.0)
    return out


@algorithm("ICE_LOSS")
def ice_contamination(p: L1Product, ctx: dict) -> dict[str, float]:
    """Fit rel_gain(nu) = a * (1 - L * shape(nu)) over band 1; L = peak transmission loss."""
    nu = p.wavenumber("B1")
    rel = calibration_gain(p, "B1", ctx) / reference_gain(ctx, "B1", nu)
    shape = np.exp(-0.5 * ((nu - ctx["ice_absorption_center_cm1"]) / ctx["ice_absorption_sigma_cm1"]) ** 2)
    slope, intercept = np.polyfit(shape, rel, 1)
    return {"ICE_LOSS": float(-slope / intercept * 100.0)}


# --- Spectral ------------------------------------------------------------------

@algorithm("SPEC_SHIFT", "ILS_FWHM")
def spectral_line(p: L1Product, ctx: dict) -> dict[str, float]:
    """Centroid and width of the reference absorption line in the high-resolution window."""
    nu, rad = p.spectral_window()
    edge = np.r_[rad[:20], rad[-20:]]
    nu_edge = np.r_[nu[:20], nu[-20:]]
    continuum = np.polyval(np.polyfit(nu_edge, edge, 1), nu)
    depth = 1.0 - rad / continuum
    peak = nu[np.argmax(depth)]
    sel = np.abs(nu - peak) < 0.4
    w = np.clip(depth[sel], 0.0, None)
    for _ in range(2):  # iterate window around the centroid
        centroid = float(np.sum(w * nu[sel]) / np.sum(w))
        sel = np.abs(nu - centroid) < 0.4
        w = np.clip(depth[sel], 0.0, None)
    centroid = float(np.sum(w * nu[sel]) / np.sum(w))
    # Gaussian fit via log-parabola on the line core (depth > 25 % of max)
    core = depth > 0.25 * depth.max()
    a2, _, _ = np.polyfit(nu[core] - centroid, np.log(depth[core]), 2)
    sigma = np.sqrt(-1.0 / (2.0 * a2))
    ref = ctx["spectral_line_ref_cm1"]
    return {"SPEC_SHIFT": (centroid / ref - 1.0) * 1e6, "ILS_FWHM": float(2.354820045 * sigma)}


@algorithm("LASER_DRIFT")
def laser(p: L1Product, ctx: dict) -> dict[str, float]:
    lam = p.housekeeping("laser_wavelength_nm")
    return {"LASER_DRIFT": float((np.mean(lam) / ctx["laser_nominal_nm"] - 1.0) * 1e6)}


# --- Interferometric -------------------------------------------------------------

@algorithm("ZPD_OFFSET", "IFG_PEAK_AMP", "SCAN_SPEED_STAB")
def interferogram(p: L1Product, ctx: dict) -> dict[str, float]:
    zpd = p.housekeeping("zpd_position").astype(float)
    amp = p.housekeeping("ifg_peak_amplitude").astype(float)
    speed = p.housekeeping("scan_speed_cm_s").astype(float)
    return {
        "ZPD_OFFSET": float(np.mean(zpd) - ctx["zpd_nominal_sample"]),
        "IFG_PEAK_AMP": float(np.mean(amp)),
        "SCAN_SPEED_STAB": float(np.std(speed, ddof=1) / np.mean(speed) * 100.0),
    }


# --- Thermal ---------------------------------------------------------------------

@algorithm("T_DETECTOR", "T_BEAMSPLITTER", "T_BLACKBODY")
def temperatures(p: L1Product, ctx: dict) -> dict[str, float]:
    return {
        "T_DETECTOR": float(np.mean(p.temperature("detector"))),
        "T_BEAMSPLITTER": float(np.mean(p.temperature("beamsplitter"))),
        "T_BLACKBODY": float(np.mean(p.temperature("blackbody"))),
    }


# --- Data quality & geometry -------------------------------------------------------

@algorithm("VALID_RATIO", "MISSING_SCANS")
def data_quality(p: L1Product, ctx: dict) -> dict[str, float]:
    qf = p.quality_flags()
    return {
        "VALID_RATIO": float(np.mean(qf == 0) * 100.0),
        "MISSING_SCANS": float(ctx["nominal_scans_per_product"] - qf.shape[0]),
    }


@algorithm("COREG_OFFSET")
def coregistration(p: L1Product, ctx: dict) -> dict[str, float]:
    return {"COREG_OFFSET": float(np.mean(p.geolocation("coregistration_offset_km")))}

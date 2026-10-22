"""Monitoring-parameter algorithms for the Fourier-Transform Spectrometer (example instrument).

Each function derives one or more parameters from a single product of a level (`inputs`, default L1):
L0 products are header-only (catalogue attributes), L1 products are read through `L1Product`
(HDF5 or netCDF4). `ctx` holds the instrument characterisation data (config: `characterisation`)
and a per-product cache.
"""
from __future__ import annotations

import numpy as np

from .fts_reader import L1Product
from .readers import HeaderProduct, register_reader
from .registry import Dim, Vector, algorithm, dims

register_reader("L1", L1Product)

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


# --- L0 catalogue (header information) ----------------------------------------

@algorithm("L0_SCANS", "L0_CAL_VIEWS", "L0_PACKET_ERRORS", inputs=("L0",))
def l0_header(p: HeaderProduct, ctx: dict) -> dict:
    """Number of earth-view scans, calibration views (black body / deep space) and packet errors per L0 product."""
    return {"L0_SCANS": p.number("numberOfScans"),
            "L0_CAL_VIEWS": [Dim(p.number(f"numberOfViews{v}"), dims(view=v)) for v in ("BB", "DS")],
            "L0_PACKET_ERRORS": p.number("packetErrors")}


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


@algorithm("TRANSMISSION")
def transmission(p: L1Product, ctx: dict) -> dict:
    """Optical transmission per band: on-board calibration gain relative to the beginning-of-life gain."""
    out = []
    for band in L1Product.BANDS:
        rel = calibration_gain(p, band, ctx) / reference_gain(ctx, band, p.wavenumber(band))
        out.append(Dim(float(np.median(rel) * 100.0), dims(band=band)))
    return {"TRANSMISSION": out}


@algorithm("NESR_IDX", "NESR_IDX_MEAN")
def nesr_index(p: L1Product, ctx: dict) -> dict:
    """NESR index of the L1 processor (spectrally binned per sub-band), averaged over the spectrograms."""
    idx = p.nesr_index()
    out = [Dim(float(np.mean(idx[:, sb])), dims(sb=sb + 1)) for sb in range(idx.shape[1])]
    return {"NESR_IDX": out, "NESR_IDX_MEAN": float(np.mean(idx))}


@algorithm("NESR_SPECTRAL")
def nesr_spectral(p: L1Product, ctx: dict) -> dict:
    """Spectrally resolved NESR averaged over all spectrograms of the orbit."""
    return {"NESR_SPECTRAL": [Vector(p.wavenumber(b).tolist(), np.mean(p.nesr_spectral(b), axis=0).tolist(),
                                     dims(band=b)) for b in L1Product.BANDS]}


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


@algorithm("ISRF_FEATURE")
def isrf_feature(p: L1Product, ctx: dict) -> dict[str, float]:
    """Largest radiometric feature (relative depth, %) in the calibration window outside the line core
    - such features distort the ISRF retrieved from the reference line."""
    nu, rad = p.spectral_window()
    edge = np.r_[rad[:20], rad[-20:]]
    nu_edge = np.r_[nu[:20], nu[-20:]]
    depth = 1.0 - rad / np.polyval(np.polyfit(nu_edge, edge, 1), nu)
    centre = nu[np.argmax(depth)]
    sel = (np.abs(nu - centre) > 0.4) & (nu > nu[20]) & (nu < nu[-21])
    return {"ISRF_FEATURE": float(np.max(np.abs(depth[sel])) * 100.0)}


@algorithm("SSD")
def spectral_sampling(p: L1Product, ctx: dict) -> dict[str, float]:
    """Spectral sampling distance 1 / (2 OPDmax) from the metrology-derived maximum optical path difference."""
    return {"SSD": float(1.0 / (2.0 * np.mean(p.housekeeping("opd_max_cm"))))}


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


@algorithm("REF_SCI_DELAY")
def reference_delay(p: L1Product, ctx: dict) -> dict[str, float]:
    """Delay between the reference (metrology) and the science signal chain [us]."""
    return {"REF_SCI_DELAY": float(np.mean(p.housekeeping("ref_sci_delay_us")))}


# --- Thermal ---------------------------------------------------------------------

@algorithm("T_DETECTOR", "T_BEAMSPLITTER", "T_BLACKBODY")
def temperatures(p: L1Product, ctx: dict) -> dict[str, float]:
    return {
        "T_DETECTOR": float(np.mean(p.temperature("detector"))),
        "T_BEAMSPLITTER": float(np.mean(p.temperature("beamsplitter"))),
        "T_BLACKBODY": float(np.mean(p.temperature("blackbody"))),
    }


@algorithm("T_BENCH", "T_BENCH_STAB", "T_SENSOR_DIFF", "T_SENSOR_CORR")
def bench_sensors(p: L1Product, ctx: dict) -> dict:
    """Optical bench temperature measured by two sensor types (PRT and thermistor): level, in-orbit
    stability and their consistency (mean difference, correlation of the in-orbit variations)."""
    prt = p.temperature("optics_bench_prt").astype(float)
    th = p.temperature("optics_bench_th").astype(float)
    return {
        "T_BENCH": [Dim(float(np.mean(prt)), dims(sensor="PRT")), Dim(float(np.mean(th)), dims(sensor="TH"))],
        "T_BENCH_STAB": [Dim(float(np.std(prt, ddof=1) * 1000.0), dims(sensor="PRT")),
                         Dim(float(np.std(th, ddof=1) * 1000.0), dims(sensor="TH"))],
        "T_SENSOR_DIFF": float(np.mean(prt - th)),
        "T_SENSOR_CORR": float(np.corrcoef(prt, th)[0, 1]),
    }


# --- Data quality & geometry -------------------------------------------------------

@algorithm("VALID_RATIO", "MISSING_SCANS")
def data_quality(p: L1Product, ctx: dict) -> dict[str, float]:
    qf = p.quality_flags()
    return {
        "VALID_RATIO": float(np.mean(qf == 0) * 100.0),
        "MISSING_SCANS": float(ctx["nominal_scans_per_product"] - qf.shape[0]),
    }


@algorithm("QF_FRACTION")
def quality_flags(p: L1Product, ctx: dict) -> dict:
    """Fraction of earth-view spectra per non-nominal quality flag (1 saturation, 2 spike, 3 calibration)."""
    qf = p.quality_flags()
    return {"QF_FRACTION": [Dim(float(np.mean(qf == f) * 100.0), dims(flag=f)) for f in (1, 2, 3)]}


@algorithm("COREG_OFFSET")
def coregistration(p: L1Product, ctx: dict) -> dict[str, float]:
    return {"COREG_OFFSET": float(np.mean(p.geolocation("coregistration_offset_km")))}


@algorithm("GEO_TRACK")
def ground_track(p: L1Product, ctx: dict) -> dict:
    """Spectrogram footprints: latitude (values) over longitude (axis)."""
    lon = p.geolocation("longitude").astype(float)
    lat = p.geolocation("latitude").astype(float)
    return {"GEO_TRACK": Vector(np.round(lon, 3).tolist(), np.round(lat, 3).tolist())}

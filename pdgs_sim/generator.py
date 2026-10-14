"""Synthetic FTS product generators (HDF5): Level-1b and ancillary (AUX_ANC).

L1b product layout (decimated for the demo - an operational product holds thousands of spectra):

/                          attrs: product_name, product_type, orbit, sensing_start, sensing_stop, instrument,
                                  processor_version, product_version, baseline, nominal_scans
/Data/Band{1..3}/wavenumber            (128,)          cm-1
/Data/Band{1..3}/earth_radiance        (n_scans, 128)  mW/(m2 sr cm-1)
/Data/quality_flag                     (n_scans, 120)  0 = nominal, 1 = saturation, 2 = spike, 3 = calibration
/Data/scan_direction                   (n_scans,)      0 = forward, 1 = reverse
/Quality/nesr_index                    (n_scans, 6)    NESR / template, spectrally binned in 6 sub-bands
/Quality/Band{1..3}/nesr_spectral      (n_scans, 128)  nW/(cm2 sr cm-1), per spectrogram
/Geolocation/latitude, longitude       (n_scans,)
/Geolocation/coregistration_offset_km  (n_scans,)
/Calibration/Band{1..3}/deep_space_counts  (10, 128)
/Calibration/Band{1..3}/blackbody_counts   (10, 128)
/Calibration/SpectralWindow/wavenumber, radiance  (256,)  high-resolution window around 712 cm-1
/Housekeeping/scan_time_s, laser_wavelength_nm, zpd_position, ifg_peak_amplitude, scan_speed_cm_s,
              opd_max_cm, ref_sci_delay_us  (n_scans,)
/Housekeeping/Temperatures/detector, beamsplitter, blackbody, optics_bench_prt, optics_bench_th  (60,)  K

AUX_ANC (ancillary) product layout:
/                          attrs: product_name, product_type, orbit, sensing_start, sensing_stop
/Ancillary/scan_time_s, turnaround_position (samples), fringe_count  (n_scans,)
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import h5py
import numpy as np

from . import scenario as sc

C1 = 1.191042e-5   # mW/(m2 sr cm-4)
C2 = 1.4387752     # cm K
N_CAL = sc.N_CAL
CORRUPT = b"\x89HDF\r\n\x1a\n" + b"\x00" * 512  # truncated / corrupted product


def planck(nu: np.ndarray, t: float) -> np.ndarray:
    return C1 * nu**3 / np.expm1(C2 * nu / t)


def wavenumbers(band: str) -> np.ndarray:
    lo, hi = sc.BANDS[band]
    return np.linspace(lo, hi, sc.N_CHANNELS)


def reference_gain(band: str, nu: np.ndarray) -> np.ndarray:
    g0, slope = sc.REFERENCE_GAIN[band]
    lo, hi = sc.BANDS[band]
    return g0 * (1.0 + slope * (nu - 0.5 * (lo + hi)) / (hi - lo))


def product_name(index: int, start: datetime, stop: datetime, baseline: str = "B03") -> str:
    return f"FTS1_L1B_{start:%Y%m%dT%H%M%S}_{stop:%Y%m%dT%H%M%S}_O{index + 1:05d}_{baseline}.h5"


def generate_product(index: int, start: datetime, baseline: str = "B03") -> bytes:
    t = sc.truth(index)
    if t.corrupt and baseline == "B03":
        return CORRUPT

    rng = np.random.default_rng(index * 104729 + 7)
    rng2 = np.random.default_rng(index * 104729 + 11)  # extended content (keeps the original draws stable)
    stop = start + timedelta(seconds=sc.ORBIT_PERIOD_S)
    n_scans = sc.NOMINAL_SCANS - t.missing_scans

    bio = io.BytesIO()
    with h5py.File(bio, "w") as f:
        f.attrs.update({
            "product_name": product_name(index, start, stop, baseline),
            "product_type": "FTS_L1B",
            "orbit": index + 1,
            "sensing_start": start.isoformat() + "Z",
            "sensing_stop": stop.isoformat() + "Z",
            "instrument": "FTS",
            "processor_version": sc.BASELINES[baseline],
            "product_version": "01",
            "baseline": baseline,
            "nominal_scans": sc.NOMINAL_SCANS,
        })

        # ---- Earth views + calibration views per band -------------------
        scene_t = rng.uniform(220.0, 300.0, n_scans)
        for b in sc.BANDS:
            g = b[-1]
            nu = wavenumbers(b)
            nesr_rad = t.nesr[b] / 100.0  # nW/(cm2 sr cm-1) -> mW/(m2 sr cm-1)
            band_shape = 1.0 - t.ice_loss * sc.ice_shape(nu)
            gain = reference_gain(b, nu) * (1.0 + t.aging) * band_shape
            offset = 500.0 + 0.1 * nu

            absorb = 1.0 - 0.35 * np.exp(-0.5 * ((nu - 667.0) / 25.0) ** 2) \
                         - 0.25 * np.exp(-0.5 * ((nu - 1595.0) / 80.0) ** 2) \
                         - 0.15 * np.exp(-0.5 * ((nu - 2350.0) / 20.0) ** 2)
            earth = np.array([planck(nu, ts) * absorb for ts in scene_t])
            earth += rng.normal(0.0, nesr_rad, earth.shape)

            ds = offset + rng.normal(0.0, nesr_rad, (N_CAL, nu.size)) * gain
            bb = gain * planck(nu, t.bb_temp) + offset + rng.normal(0.0, nesr_rad, (N_CAL, nu.size)) * gain

            grp = f.create_group(f"Data/Band{g}")
            grp["wavenumber"] = nu
            grp["earth_radiance"] = earth.astype(np.float32)
            cal = f.create_group(f"Calibration/Band{g}")
            cal["deep_space_counts"] = ds.astype(np.float32)
            cal["blackbody_counts"] = bb.astype(np.float32)

            spectral = sc.nesr_template(b, nu) * t.nesr_scale * (1.0 + rng2.normal(0.0, 0.03, (n_scans, nu.size)))
            f[f"Quality/Band{g}/nesr_spectral"] = spectral.astype(np.float32)

        # ---- Quality flags (scan x spectrum) ------------------------------
        bad = rng.uniform(size=(n_scans, sc.SPECTRA_PER_SCAN)) > t.valid_ratio / 100.0
        code = rng2.choice(np.array([1, 2, 3], dtype=np.uint8), size=bad.shape, p=[0.5, 0.3, 0.2])
        f["Data/quality_flag"] = np.where(bad, code, 0).astype(np.uint8)
        direction = (np.arange(n_scans) % 2).astype(np.uint8)
        f["Data/scan_direction"] = direction
        f["Quality/nesr_index"] = (t.nesr_scale * (1.0 + 0.03 * direction[:, None])
                                   + rng2.normal(0.0, 0.02, (n_scans, sc.N_SUBBANDS))).astype(np.float32)

        # ---- Geolocation --------------------------------------------------
        phase = np.linspace(0, 2 * np.pi, n_scans, endpoint=False) + rng.uniform(0, 2 * np.pi)
        f["Geolocation/latitude"] = (81.0 * np.sin(phase)).astype(np.float32)
        f["Geolocation/longitude"] = (((np.degrees(phase) + index * 25.3) % 360.0) - 180.0).astype(np.float32)
        f["Geolocation/coregistration_offset_km"] = np.abs(
            t.coreg_km + rng.normal(0.0, 0.05, n_scans)).astype(np.float32)

        # ---- Spectral calibration window ---------------------------------
        nu_w = np.linspace(711.0, 713.0, 256)
        shift = t.spec_shift_ppm - (0.6 if baseline == "B04" else 0.0)  # B04 removes the constant offset
        center = sc.SPECTRAL_LINE_CM1 * (1.0 + shift * 1e-6)
        sigma = t.ils_fwhm / 2.354820045
        cont = planck(nu_w, 280.0)
        rad_w = cont * (1.0 - 0.5 * np.exp(-0.5 * ((nu_w - center) / sigma) ** 2)
                        - t.isrf_feature * np.exp(-0.5 * ((nu_w - 712.55) / 0.04) ** 2))
        rad_w += rng.normal(0.0, 0.0005, nu_w.size) * cont
        f["Calibration/SpectralWindow/wavenumber"] = nu_w
        f["Calibration/SpectralWindow/radiance"] = rad_w

        # ---- Housekeeping -------------------------------------------------
        hk = f.create_group("Housekeeping")
        hk["scan_time_s"] = np.sort(rng.uniform(0, sc.ORBIT_PERIOD_S, n_scans))
        hk["laser_wavelength_nm"] = sc.LASER_NOMINAL_NM * (1.0 + t.laser_ppm * 1e-6) \
            + rng.normal(0.0, 5e-5, n_scans)
        hk["zpd_position"] = np.round(sc.ZPD_NOMINAL + t.zpd_offset + rng.normal(0, 0.8, n_scans)).astype(np.int32)
        hk["ifg_peak_amplitude"] = (t.ifg_amp + rng.normal(0, 300, n_scans)).astype(np.float32)
        hk["scan_speed_cm_s"] = 1.0 + rng.normal(0.0, t.scan_speed_stab / 100.0, n_scans)
        hk["opd_max_cm"] = sc.OPD_MAX_CM / (1.0 + t.laser_ppm * 1e-6) * (1.0 + rng2.normal(0.0, 2e-8, n_scans))
        hk["ref_sci_delay_us"] = t.ref_sci_delay_us + rng2.normal(0.0, 0.01, n_scans)
        orbit_t = np.linspace(0, 2 * np.pi, 60)
        temps = hk.create_group("Temperatures")
        temps["detector"] = t.det_temp + 0.03 * np.sin(orbit_t) + rng.normal(0, 0.01, 60)
        temps["beamsplitter"] = t.bs_temp + 0.05 * np.sin(orbit_t) + rng.normal(0, 0.01, 60)
        temps["blackbody"] = t.bb_temp + rng.normal(0, 0.005, 60)
        temps["optics_bench_prt"] = t.bench_temp + 0.03 * np.sin(orbit_t) + rng2.normal(0, 0.01, 60)
        temps["optics_bench_th"] = t.bench_temp + t.th_bias + 0.03 * np.sin(orbit_t) + rng2.normal(0, t.th_noise, 60)

    return bio.getvalue()


def ancillary_name(index: int, start: datetime, stop: datetime) -> str:
    return f"FTS1_AUX_ANC_{start:%Y%m%dT%H%M%S}_{stop:%Y%m%dT%H%M%S}_O{index + 1:05d}.h5"


def generate_ancillary(index: int, start: datetime) -> bytes:
    """Ancillary data: corner-cube turn-around positions and on-board fringe counter per scan."""
    t = sc.truth(index)
    rng = np.random.default_rng(index * 15485863 + 3)
    stop = start + timedelta(seconds=sc.ORBIT_PERIOD_S)
    n = sc.NOMINAL_SCANS - t.missing_scans
    bio = io.BytesIO()
    with h5py.File(bio, "w") as f:
        f.attrs.update({"product_name": ancillary_name(index, start, stop), "product_type": "AUX_ANC",
                        "orbit": index + 1, "sensing_start": start.isoformat() + "Z",
                        "sensing_stop": stop.isoformat() + "Z"})
        anc = f.create_group("Ancillary")
        anc["scan_time_s"] = np.sort(rng.uniform(0, sc.ORBIT_PERIOD_S, n))
        anc["turnaround_position"] = sc.TA_NOMINAL + t.zpd_drift + rng.normal(0.0, 0.3, n)
        anc["fringe_count"] = np.round(sc.FRINGES_NOMINAL + 40.0 * t.zpd_drift + rng.normal(0.0, 8.0, n)).astype(np.int32)
    return bio.getvalue()

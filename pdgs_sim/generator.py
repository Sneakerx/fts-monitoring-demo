"""Synthetic FTS Level-1b product generator, written as HDF5 or netCDF4 (same group / variable layout).

L1b product layout (decimated for the demo - an operational product holds thousands of spectra):

/                          attrs: product_name, product_type, orbit, sensing_start, sensing_stop, instrument,
                                  processor_version, product_version, baseline, nominal_scans
/Data/Band{1..3}/wavenumber            (128,)          cm-1
/Data/Band{1..3}/earth_radiance        (n_scans, 128)  mW/(m2 sr cm-1)
/Data/quality_flag                     (n_scans, 120)  0 = nominal, 1 = saturation, 2 = spike, 3 = calibration
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

L0 products are header-only: their header information is exposed as catalogue attributes
(`scenario.l0_header`), they are not downloadable.
"""
from __future__ import annotations

import io
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

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


class _Tree:
    """Format-neutral product content: global attributes and arrays by '/'-separated path."""

    def __init__(self, data: dict | None = None, prefix: str = ""):
        self.data = {} if data is None else data
        self.prefix = prefix
        self.attrs: dict = {}

    def __setitem__(self, path: str, value) -> None:
        self.data[self.prefix + path] = np.asarray(value)

    def create_group(self, name: str) -> "_Tree":
        return _Tree(self.data, f"{self.prefix}{name}/")


def _write_hdf5(tree: _Tree) -> bytes:
    bio = io.BytesIO()
    with h5py.File(bio, "w") as f:
        f.attrs.update(tree.attrs)
        for path, arr in tree.data.items():
            f[path] = arr
    return bio.getvalue()


def _write_netcdf4(tree: _Tree) -> bytes:
    """netCDF4 file with the same groups / variables as the HDF5 layout (one dimension per array axis)."""
    import netCDF4
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "product.nc"
        with netCDF4.Dataset(path, "w", format="NETCDF4") as ds:
            ds.setncatts({k: (int(v) if isinstance(v, (bool, np.bool_)) else v) for k, v in tree.attrs.items()})
            for full, arr in tree.data.items():
                *groups, name = full.split("/")
                node = ds
                for g in groups:
                    node = node.groups.get(g) or node.createGroup(g)
                dims = []
                for i, n in enumerate(arr.shape):
                    dim = f"{name}_{i}"
                    node.createDimension(dim, n)
                    dims.append(dim)
                node.createVariable(name, arr.dtype, tuple(dims))[...] = arr
        return path.read_bytes()


FORMATS = {"hdf5": (_write_hdf5, ".h5"), "netcdf4": (_write_netcdf4, ".nc")}


def write_product(tree: _Tree, fmt: str = "hdf5") -> bytes:
    return FORMATS[fmt][0](tree)


def product_name(index: int, start: datetime, stop: datetime, baseline: str = "B03", fmt: str = "hdf5") -> str:
    return f"FTS1_L1B_{start:%Y%m%dT%H%M%S}_{stop:%Y%m%dT%H%M%S}_O{index + 1:05d}_{baseline}{FORMATS[fmt][1]}"


def generate_product(index: int, start: datetime, baseline: str = "B03", fmt: str = "hdf5") -> bytes:
    t = sc.truth(index)
    if t.corrupt and baseline == "B03":
        return CORRUPT

    rng = np.random.default_rng(index * 104729 + 7)
    rng2 = np.random.default_rng(index * 104729 + 11)  # extended content (keeps the original draws stable)
    stop = start + timedelta(seconds=sc.ORBIT_PERIOD_S)
    n_scans = sc.NOMINAL_SCANS - t.missing_scans

    f = _Tree()
    f.attrs.update({
        "product_name": product_name(index, start, stop, baseline, fmt),
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
    f["Quality/nesr_index"] = (t.nesr_scale + rng2.normal(0.0, 0.02, (n_scans, sc.N_SUBBANDS))).astype(np.float32)

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

    return write_product(f, fmt)


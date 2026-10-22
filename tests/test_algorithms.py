"""The parameter algorithms must recover the simulator's ground truth."""
from datetime import datetime

import pytest

from imf.config import load_settings
from imf.processing.pipeline import Pipeline
from pdgs_sim import scenario as sc
from pdgs_sim.generator import generate_product


@pytest.fixture(scope="module")
def pipeline():
    return Pipeline(load_settings().characterisation)


@pytest.mark.parametrize("index", [10, 560, 758, 1000, 1200])
def test_recovers_truth(pipeline, tmp_path, index):
    t = sc.truth(index)
    path = tmp_path / "p.h5"
    path.write_bytes(generate_product(index, datetime(2026, 1, 1)))
    v, errors = pipeline.derive(path)
    assert not errors
    assert v["ICE_LOSS"] == pytest.approx(t.ice_loss * 100, abs=0.15)
    assert v["LASER_DRIFT"] == pytest.approx(t.laser_ppm, abs=0.05)
    assert v["SPEC_SHIFT"] == pytest.approx(t.spec_shift_ppm, abs=0.3)
    assert v["ILS_FWHM"] == pytest.approx(t.ils_fwhm, abs=0.003)
    assert v["T_DETECTOR"] == pytest.approx(t.det_temp, abs=0.02)
    assert v["MISSING_SCANS"] == t.missing_scans
    for b in ("B1", "B2", "B3"):
        assert v[f"NESR_{b}"] == pytest.approx(t.nesr[b], rel=0.12)


def test_corrupted_product_is_rejected(pipeline, tmp_path):
    from imf.processing.readers import ProductFormatError
    index = 200
    assert sc.truth(index).corrupt
    path = tmp_path / "bad.h5"
    path.write_bytes(generate_product(index, datetime(2026, 1, 1)))
    with pytest.raises(ProductFormatError):
        pipeline.derive(path)


@pytest.mark.parametrize("index", [100, 910, 1100, 1220])
def test_extended_parameters_recover_truth(pipeline, tmp_path, index):
    """NESR index per sub-band, SSD, reference delay, bench sensors, ISRF feature."""
    from imf.processing.registry import Dim
    t = sc.truth(index)
    path = tmp_path / "p.h5"
    path.write_bytes(generate_product(index, datetime(2026, 1, 1)))
    v, errors = pipeline.derive(path)
    assert not errors
    idx = {d.dims: d.value for d in v["NESR_IDX"]}
    assert len(idx) == sc.N_SUBBANDS
    assert idx["sb=1"] == pytest.approx(t.nesr_scale, rel=0.02)
    assert v["SSD"] == pytest.approx(0.625 * (1 + t.laser_ppm * 1e-6), rel=2e-7)
    assert v["REF_SCI_DELAY"] == pytest.approx(t.ref_sci_delay_us, abs=0.01)
    bench = {d.dims: d.value for d in v["T_BENCH"]}
    assert bench["sensor=PRT"] == pytest.approx(t.bench_temp, abs=0.01)
    assert v["T_SENSOR_DIFF"] == pytest.approx(-t.th_bias, abs=0.03)
    assert v["ISRF_FEATURE"] == pytest.approx(t.isrf_feature * 100, abs=0.4)
    assert all(isinstance(d, Dim) for d in v["QF_FRACTION"])


def test_l0_header_algorithm(pipeline):
    index = 1000
    t = sc.truth(index)
    v, errors = pipeline.derive(None, "L0", sc.l0_header(index))
    assert not errors and v["L0_SCANS"] == sc.NOMINAL_SCANS - t.missing_scans
    assert v["L0_PACKET_ERRORS"] == sum(t.packet_errors.values())
    assert {d.dims: d.value for d in v["L0_CAL_VIEWS"]} == {"view=BB": t.cal_views["BB"], "view=DS": t.cal_views["DS"]}


def test_l1_reader_formats(tmp_path):
    """The same product written as HDF5 and as netCDF4 gives identical parameters."""
    from imf.config import PdgsCfg, ProductTypeCfg
    results = {}
    for fmt, suffix in (("hdf5", ".h5"), ("netcdf4", ".nc")):
        pdgs = PdgsCfg(base_url="x", product_types=[ProductTypeCfg(type="L1B", level="L1", format=fmt)])
        path = tmp_path / f"p{suffix}"
        path.write_bytes(generate_product(910, datetime(2026, 1, 1), fmt=fmt))
        v, errors = Pipeline(load_settings().characterisation, pdgs).derive(path, "L1B")
        assert not errors
        results[fmt] = v
    h5, nc = results["hdf5"], results["netcdf4"]
    assert h5.keys() == nc.keys()
    for code in ("NESR_B1", "SPEC_SHIFT", "SSD", "ZPD_OFFSET", "T_SENSOR_CORR", "VALID_RATIO"):
        assert nc[code] == pytest.approx(h5[code], rel=1e-12)
    assert [d.value for d in nc["NESR_IDX"]] == [d.value for d in h5["NESR_IDX"]]


def test_level_configuration():
    from pydantic import ValidationError
    from imf.config import PdgsCfg, ProductTypeCfg
    assert PdgsCfg(base_url="x").levels == ["L0", "L1"]  # default: two levels
    with pytest.raises(ValidationError):  # one product type per level
        PdgsCfg(base_url="x", product_types=[ProductTypeCfg(type="A", level="L1"), ProductTypeCfg(type="B", level="L1")])
    with pytest.raises(ValidationError):  # level not declared
        PdgsCfg(base_url="x", product_types=[ProductTypeCfg(type="A", level="AUX")])
    with pytest.raises(ValidationError):  # L1 (processing baselines) is mandatory
        PdgsCfg(base_url="x", levels=["L0", "AUX"])
    with pytest.raises(ValidationError):
        ProductTypeCfg(type="A", format="grib")
    cfg = PdgsCfg(base_url="x", levels=["L0", "AUX", "L1"],
                  product_types=[ProductTypeCfg(type="P1", level="L1"), ProductTypeCfg(type="PA", level="AUX"),
                                 ProductTypeCfg(type="P0", level="L0", mode="header")])
    assert [t.type for t in sorted(cfg.product_types, key=lambda t: cfg.level_order(t.level))] == ["P0", "PA", "P1"]
    assert cfg.type_for_level("AUX") == "PA"


def test_additional_level_with_generic_reader(tmp_path):
    """An extra level (AUX) needs only configuration + an algorithm; without a registered adapter the
    product is read through the generic reader, in either file format."""
    import h5py
    from imf.config import PdgsCfg, ProductTypeCfg
    from imf.processing.registry import _REGISTRY, algorithm

    @algorithm("AUX_MEAN", inputs=("AUX",))
    def aux_mean(p, ctx):
        return {"AUX_MEAN": float(p.read("Data/values").mean())}

    try:
        pdgs = PdgsCfg(base_url="x", levels=["L0", "AUX", "L1"],
                       product_types=[ProductTypeCfg(type="MY_AUX", level="AUX"), ProductTypeCfg(type="L1B")])
        path = tmp_path / "aux.h5"
        with h5py.File(path, "w") as f:
            f["Data/values"] = [1.0, 2.0, 6.0]
        v, errors = Pipeline(load_settings().characterisation, pdgs).derive(path, "MY_AUX")
        assert not errors and v == {"AUX_MEAN": 3.0}  # only the AUX algorithms run
    finally:
        _REGISTRY.pop("aux_mean", None)


def test_external_tool_contract(tmp_path):
    """External tools: `command + [product_path]` prints {"CODE": value} as JSON (R8)."""
    import sys
    from imf.config import ExternalToolCfg
    from imf.processing.registry import _REGISTRY, register_external_tools
    tool = tmp_path / "tool.py"
    tool.write_text("import json, sys, h5py\n"
                    "with h5py.File(sys.argv[1]) as f:\n"
                    "    print(json.dumps({'EXT_ORBIT': float(f.attrs['orbit']), 'IGNORED': 1}))\n")
    register_external_tools([ExternalToolCfg(name="orbit", command=[sys.executable, str(tool)],
                                             outputs=["EXT_ORBIT"])])
    try:
        pl = Pipeline(load_settings().characterisation)
        path = tmp_path / "p.h5"
        path.write_bytes(generate_product(41, datetime(2026, 1, 1)))
        v, errors = pl.derive(path)
        assert not errors and v["EXT_ORBIT"] == 42.0 and "IGNORED" not in v
    finally:
        _REGISTRY.pop("ext_orbit", None)


def test_every_configured_parameter_has_a_producer():
    from imf.db import producers_index
    settings = load_settings()
    produced = producers_index(settings)
    missing = [p.code for p in settings.parameters if (p.algorithm or p.code) not in produced]
    assert not missing

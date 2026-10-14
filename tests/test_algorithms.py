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
    from imf.processing.l1reader import ProductFormatError
    index = 200
    assert sc.truth(index).corrupt
    path = tmp_path / "bad.h5"
    path.write_bytes(generate_product(index, datetime(2026, 1, 1)))
    with pytest.raises(ProductFormatError):
        pipeline.derive(path)


@pytest.mark.parametrize("index", [100, 910, 1100, 1220])
def test_extended_parameters_recover_truth(pipeline, tmp_path, index):
    """NESR index per sub-band / direction, SSD, reference delay, bench sensors, ISRF feature."""
    from imf.processing.registry import Dim
    t = sc.truth(index)
    path = tmp_path / "p.h5"
    path.write_bytes(generate_product(index, datetime(2026, 1, 1)))
    v, errors = pipeline.derive(path)
    assert not errors
    idx = {d.dims: d.value for d in v["NESR_IDX"]}
    assert len(idx) == 2 * sc.N_SUBBANDS
    assert idx["dir=fwd;sb=1"] == pytest.approx(t.nesr_scale, rel=0.03)
    assert idx["dir=rev;sb=1"] == pytest.approx(t.nesr_scale * 1.03, rel=0.03)
    assert v["SSD"] == pytest.approx(0.625 * (1 + t.laser_ppm * 1e-6), rel=2e-7)
    assert v["REF_SCI_DELAY"] == pytest.approx(t.ref_sci_delay_us, abs=0.01)
    bench = {d.dims: d.value for d in v["T_BENCH"]}
    assert bench["sensor=PRT"] == pytest.approx(t.bench_temp, abs=0.01)
    assert v["T_SENSOR_DIFF"] == pytest.approx(-t.th_bias, abs=0.03)
    assert v["ISRF_FEATURE"] == pytest.approx(t.isrf_feature * 100, abs=0.4)
    assert all(isinstance(d, Dim) for d in v["QF_FRACTION"])


def test_header_and_ancillary_algorithms(pipeline, tmp_path):
    from pdgs_sim.generator import generate_ancillary
    index = 1000
    t = sc.truth(index)
    hdr = sc.l0_header("FTS_L0_EO", index)
    v, errors = pipeline.derive(None, "FTS_L0_EO", hdr)
    assert not errors and v["L0_EO_SCANS"] == sc.NOMINAL_SCANS - t.missing_scans
    path = tmp_path / "a.h5"
    path.write_bytes(generate_ancillary(index, datetime(2026, 1, 1)))
    v, errors = pipeline.derive(path, "AUX_ANC")
    assert not errors
    assert v["ANC_TA_OFFSET"] == pytest.approx(t.zpd_drift, abs=0.2)


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

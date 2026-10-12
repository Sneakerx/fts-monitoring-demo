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


def test_every_configured_parameter_has_an_algorithm():
    from imf.processing.registry import outputs_index
    produced = outputs_index()
    missing = [p.code for p in load_settings().parameters if (p.algorithm or p.code) not in produced]
    assert not missing

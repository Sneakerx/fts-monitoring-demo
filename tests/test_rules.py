"""Rules E1-E6: trigger -> event with recommended action and proposed output."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import h5py
import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from imf.db import Database, seed
from imf.models import Parameter, Product, TimelineEntry
from imf.monitoring.rules import RuleEngine
from imf.processing.pipeline import store_sample
from imf.processing.registry import Vector

T0 = datetime(2026, 1, 1)


@pytest.fixture
def env(settings):
    db = Database(settings.database.url)
    db.migrate(settings.pdgs.baseline)
    seed(db, settings)
    return settings, db, RuleEngine(settings)


def feed(s, code, values, start=T0, step=timedelta(minutes=101), baseline="B03"):
    p = s.scalar(select(Parameter).options(selectinload(Parameter.limits)).where(Parameter.code == code))
    new = []
    for i, v in enumerate(values):
        new += store_sample(s, p, float(v), start + i * step, None, baseline, "", "derived", "test").new_events
    s.flush()
    return new


def test_limit_rule_proposes_laser_wavenumber(env):
    settings, db, engine = env
    with db.session() as s:
        events = feed(s, "SPEC_SHIFT", [0.5] * 5 + [2.6, 2.6])  # warning > 2 ppm, persistence 2
        engine.on_limit_events(s, events)
        ev = events[0]
        assert ev.category == "action" and ev.rule == "E2_LASER_WAVENUMBER"
        po = ev.proposals[0]
        assert po.kind == "value" and po.status == "DRAFT"
        assert po.value == pytest.approx(1e7 / 1550.0 / (1 + 2.6e-6), rel=1e-12)
        assert s.scalar(select(TimelineEntry).where(TimelineEntry.type == "ACTION", TimelineEntry.event_id == ev.id))


def test_limit_rule_ctf_update_file(env):
    settings, db, engine = env
    with db.session() as s:
        events = feed(s, "REF_SCI_DELAY", [2.0] * 5 + [2.35] * 3)  # alarm > 2.3 us, persistence 3
        engine.on_limit_events(s, events)
        po = events[0].proposals[0]
        assert po.kind == "file" and po.value == pytest.approx(2.35)
        content = Path(po.file_path).read_text()
        assert content.splitlines()[2].startswith("band,wavenumber_cm1,re,im") and len(content.splitlines()) == 3 + 96
        assert json.loads(po.payload)["delta_us"] == pytest.approx(0.35)


def test_reprocessed_baseline_triggers_no_actions(env):
    settings, db, engine = env
    with db.session() as s:
        events = feed(s, "SPEC_SHIFT", [0.5] * 3 + [2.6] * 2, baseline="B04")
        engine.on_limit_events(s, events)
        assert events and events[0].category == "limit" and not events[0].proposals


def test_trend_rule_zpd_raises_once_and_clears(env):
    settings, db, engine = env
    rng = np.random.default_rng(1)
    n = int(36 * 86400 / (101 * 60))
    days = np.arange(n) * 101 / 1440
    zpd = np.clip(days - 10, 0, 12) * 0.08 + rng.normal(0, 0.05, n)  # drift from day 10 to 22, then stable
    with db.session() as s:
        feed(s, "ZPD_OFFSET", zpd)
        new, cleared = engine.evaluate_trends(s)
        zpd_new = [e for e in new if e.rule == "E5_ZPD_DRIFT"]
        assert len(zpd_new) == 1
        ev = zpd_new[0]
        # the 10-day Theil-Sen window needs ~half of its samples in the drift (from day 10), plus confirmation
        assert 13 <= (ev.time - T0).days <= 17
        assert ev in cleared and (ev.cleared_at - T0).days >= 22
        po = ev.proposals[0]
        tc = json.loads(Path(po.file_path).read_text())
        assert po.kind == "telecommand" and tc["telecommand"] == "FTS_SET_ZPD_OFFSET"
        assert tc["parameters"]["zpd_offset_correction_samples"] < 0
        # idempotent: evaluated windows are not evaluated again
        assert engine.evaluate_trends(s) == ([], [])


def test_trend_rule_nesr_writes_aux_file(env):
    settings, db, engine = env
    n = int(20 * 86400 / (101 * 60))
    days = np.arange(n) * 101 / 1440
    with db.session() as s:
        feed(s, "NESR_IDX_MEAN", 1.0 + np.clip(days - 5, 0, None) * 0.004)
        spec = s.scalar(select(Parameter).where(Parameter.code == "NESR_SPECTRAL"))
        from imf.processing.pipeline import store_vector
        for i in range(n):
            for b in ("B1", "B2", "B3"):
                store_vector(s, spec, Vector([1.0, 2.0, 3.0], [10.0, 11.0, 12.0], f"band={b}"),
                             T0 + i * timedelta(minutes=101), None, "B03", "derived", "test")
        new, _ = engine.evaluate_trends(s)
        ev = next(e for e in new if e.rule == "E6_NESR_DRIFT")
        po = ev.proposals[0]
        with h5py.File(po.file_path) as f:
            assert set(f) == {"Band1", "Band2", "Band3"} and f.attrs["status"] == "DRAFT"
            assert list(f["Band1/nesr"][()]) == [10.0, 11.0, 12.0]


def test_product_rule_calibration_mode(env):
    settings, db, engine = env
    with db.session() as s:
        p = Product(pdgs_id="x", name="FTS1_AUX_EFSI", product_type="AUX_EFSI", level="AUX", orbit=None,
                    sensing_start=T0, sensing_stop=T0 + timedelta(hours=2), publication_date=T0, baseline="",
                    header=json.dumps({"calibrationMode": "EFSI", "calibrationId": 7}), status="PROCESSED")
        s.add(p)
        s.flush()
        events = engine.on_product(s, p)
        assert len(events) == 1 and events[0].severity == "INFO" and "#7" in events[0].title
        entry = s.scalar(select(TimelineEntry).where(TimelineEntry.type == "CALIBRATION_MODE"))
        assert entry.start == T0 and entry.stop == T0 + timedelta(hours=2) and entry.product_id == p.id

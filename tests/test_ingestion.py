"""End-to-end ingestion of all product types against the in-process PDGS simulator."""
import json
from pathlib import Path

from sqlalchemy import func, select

from imf.models import Event, Parameter, Product, ProductLink, Report, Sample, TimelineEntry, VectorSample


def test_poll_is_complete_and_idempotent(ingested):
    first = ingested.first_poll
    assert first.error is None
    assert first.failed == 1  # L1 product index 200 is corrupted by design
    with ingested.db.session() as s:
        by_type = dict(s.execute(select(Product.product_type, func.count()).group_by(Product.product_type)).all())
        n_products = s.scalar(select(func.count(Product.id)))
        failed = s.scalar(select(Product).where(Product.status == "FAILED"))
    assert n_products == first.processed + first.failed
    l1 = by_type["FTS_L1B"]
    assert l1 > 400
    for t in ("FTS_L0_BB", "FTS_L0_DS", "FTS_L0_EO", "AUX_ANC"):
        # one product per orbit and type; L0 / AUX are published earlier, so the latest orbits may lack L1 yet
        assert 0 <= by_type[t] - l1 <= 2
    assert by_type["AUX_EFSI"] == 1  # special calibration mode on day 15.3
    assert "Cannot open product" in failed.error

    second = ingested.poller.run_once()
    assert second.processed == 0 and second.failed == 0
    with ingested.db.session() as s:
        assert s.scalar(select(func.count(Product.id))) == n_products


def test_product_attributes_and_checksums(ingested):
    with ingested.db.session() as s:
        l1 = s.scalar(select(Product).where(Product.product_type == "FTS_L1B", Product.status == "PROCESSED").limit(1))
        l0 = s.scalar(select(Product).where(Product.product_type == "FTS_L0_EO").limit(1))
    assert l1.level == "L1" and l1.baseline == "B03" and l1.processor_version == "03.02"
    assert len(l1.checksum) == 64  # downloaded -> SHA-256 kept, file deleted
    assert l0.level == "L0" and l0.baseline == "" and l0.checksum is None  # header only, not downloaded
    assert json.loads(l0.header)["numberOfScans"] > 20
    assert not any(Path(ingested.settings.pdgs.download_dir).glob("*.h5"))


def test_samples_dims_vectors_and_entry_attributes(ingested):
    with ingested.db.session() as s:
        pid = s.scalar(select(Product.id).where(Product.product_type == "FTS_L1B", Product.status == "PROCESSED").limit(1))
        rows = s.execute(select(Parameter.code, Sample.dims, Sample.baseline, Sample.entry_type, Sample.source)
                         .join(Parameter).where(Sample.product_id == pid)).all()
        vectors = s.execute(select(Parameter.code, VectorSample.dims).join(Parameter)
                            .where(VectorSample.product_id == pid)).all()
    codes = {r.code for r in rows}
    assert {"NESR_B1", "NESR_IDX", "SSD", "T_BENCH", "TRANSMISSION", "QF_FRACTION"} <= codes
    nesr_idx = [r.dims for r in rows if r.code == "NESR_IDX"]
    assert len(nesr_idx) == 12 and "dir=fwd;sb=1" in nesr_idx  # 6 sub-bands x 2 scan directions
    assert all(r.baseline == "B03" and r.entry_type == "derived" and "@" in r.source for r in rows)
    assert sorted(d for c, d in vectors if c == "NESR_SPECTRAL") == ["band=B1", "band=B2", "band=B3"]
    assert any(c == "GEO_TRACK" for c, _ in vectors)


def test_analyses_timeline_and_links(ingested):
    assert ingested.first_analysis["windows"] > 0
    with ingested.db.session() as s:
        comp = s.scalars(select(Sample.value).join(Parameter).where(Parameter.code == "L1_COMPLETENESS")).all()
        diff = s.scalar(select(func.count(Sample.id)).join(Parameter).where(Parameter.code == "ZPD_L1_ANC_DIFF"))
        types = {t for (t,) in s.execute(select(TimelineEntry.type).distinct())}
        e1 = s.scalar(select(Event).where(Event.rule == "E1_CALIBRATION_MODE"))
        links = s.scalar(select(func.count(ProductLink.id)))
    assert len(comp) >= 25 and min(comp) > 95.0  # every L0 orbit has an L1 product in the first 32 days
    assert diff > 300
    assert {"DATA_GAP", "CALIBRATION_MODE"} <= types  # PDGS outage day 30.2, EFSI day 15.3
    assert e1 is not None and e1.category == "timeline" and e1.severity == "INFO"
    assert links > 1000


def test_events_and_reports(ingested):
    with ingested.db.session() as s:
        codes = {e.parameter.code for e in s.scalars(select(Event).where(Event.parameter_id.is_not(None)))}
    assert "ICE_LOSS" in codes  # ice build-up crosses the 6 % warning limit at day ~28.6
    created = ingested.reports.check_schedule()
    assert created
    with ingested.db.session() as s:
        rep = s.get(Report, created[0])
    html = Path(rep.html_path).read_text(encoding="utf-8")
    assert len(html) > 10_000 and "Requirement compliance" in html and "Spectral NESR vs. spectral template" in html
    assert Path(rep.pdf_path).read_bytes()[:4] == b"%PDF"

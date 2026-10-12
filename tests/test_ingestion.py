"""End-to-end ingestion against the in-process PDGS simulator."""
from sqlalchemy import func, select

from imf.models import Event, Product, Report, Sample


def test_poll_is_complete_and_idempotent(services):
    first = services.poller.run_once()
    assert first.error is None
    assert first.processed > 400
    assert first.failed == 1  # product index 200 is corrupted by design

    with services.db.session() as s:
        n_products = s.scalar(select(func.count(Product.id)))
        n_samples = s.scalar(select(func.count(Sample.id)))
        failed = s.scalar(select(Product).where(Product.status == "FAILED"))
    assert n_products == first.processed + first.failed
    assert n_samples == first.processed * len(services.settings.parameters)
    assert "Cannot open product" in failed.error

    second = services.poller.run_once()
    assert second.processed == 0 and second.failed == 0
    with services.db.session() as s:
        assert s.scalar(select(func.count(Product.id))) == n_products


def test_events_and_reports(services):
    services.poller.run_once()
    with services.db.session() as s:
        codes = {e.parameter.code for e in s.scalars(select(Event))}
    assert "ICE_LOSS" in codes  # ice build-up crosses the 6 % warning limit at day ~28.6
    created = services.reports.check_schedule()
    assert created
    with services.db.session() as s:
        rep = s.get(Report, created[0])
    from pathlib import Path
    assert Path(rep.html_path).stat().st_size > 10_000
    assert Path(rep.pdf_path).read_bytes()[:4] == b"%PDF"

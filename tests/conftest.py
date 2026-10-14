from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imf.config import load_settings
from imf.services import Services
from pdgs_sim.server import SimClock, create_app as create_sim_app


def make_settings(tmp_path: Path):
    s = load_settings()
    s.database.url = f"sqlite:///{(tmp_path / 'imf.db').as_posix()}"
    s.pdgs.download_dir = tmp_path / "products"
    s.reporting.output_dir = tmp_path / "reports"
    s.notifications.log_file = tmp_path / "notifications.log"
    s.processing.outputs_dir = tmp_path / "outputs"
    s.processing.backup_dir = tmp_path / "backups"
    s.processing.export_dir = tmp_path / "exports"
    s.pdgs.base_url = "http://pdgs/odata/v1"
    return s


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(tmp_path)


def _sim(tmp_path: Path, days: float):
    clock = SimClock(tmp_path / "sim" / "state.json", days=days, accel=0.0)
    return TestClient(create_sim_app(clock), base_url="http://pdgs")


@pytest.fixture
def sim_client(tmp_path: Path):
    """PDGS simulator with 32 days of history (ice warning ~day 28), frozen clock."""
    with _sim(tmp_path, 32.0) as tc:
        yield tc


@pytest.fixture
def services(settings, sim_client) -> Services:
    svc = Services(settings, start_scheduler=False)
    svc.client._http = sim_client  # route PDGS traffic to the in-process simulator
    yield svc
    svc.stop()


@pytest.fixture
def small_services(tmp_path: Path) -> Services:
    """Fast environment: 4 days of mission history, already polled and analysed (for tests that modify data)."""
    with _sim(tmp_path, 4.0) as tc:
        svc = Services(make_settings(tmp_path), start_scheduler=False)
        svc.client._http = tc
        svc.poller.run_once()
        svc.analyses.run_once()
        yield svc
        svc.stop()


@pytest.fixture(scope="session")
def ingested(tmp_path_factory) -> Services:
    """Shared read-only environment: 32 days of mission history polled and analysed once."""
    tmp = tmp_path_factory.mktemp("ingested")
    with _sim(tmp, 32.0) as tc:
        svc = Services(make_settings(tmp), start_scheduler=False)
        svc.client._http = tc
        svc.first_poll = svc.poller.run_once()
        svc.first_analysis = svc.analyses.run_once()
        yield svc
        svc.stop()

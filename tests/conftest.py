from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from imf.config import load_settings
from imf.services import Services
from pdgs_sim.server import SimClock, create_app as create_sim_app


@pytest.fixture
def settings(tmp_path: Path):
    s = load_settings()
    s.database.url = f"sqlite:///{(tmp_path / 'imf.db').as_posix()}"
    s.pdgs.download_dir = tmp_path / "products"
    s.reporting.output_dir = tmp_path / "reports"
    s.notifications.log_file = tmp_path / "notifications.log"
    s.pdgs.base_url = "http://pdgs/odata/v1"
    return s


@pytest.fixture
def sim_client(tmp_path: Path):
    """PDGS simulator with 32 days of history (ice warning ~day 28), frozen clock."""
    clock = SimClock(tmp_path / "sim" / "state.json", days=32.0, accel=0.0)
    with TestClient(create_sim_app(clock), base_url="http://pdgs") as tc:
        yield tc


@pytest.fixture
def services(settings, sim_client) -> Services:
    svc = Services(settings, start_scheduler=False)
    svc.client._http = sim_client  # route PDGS traffic to the in-process simulator
    yield svc
    svc.stop()

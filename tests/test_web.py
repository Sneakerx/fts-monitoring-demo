"""Role-based access control is enforced server-side; pages and API work on real data."""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from imf.models import Event, Product
from imf.web.app import create_app


@pytest.fixture
def app_client(settings):
    with TestClient(create_app(settings, start_scheduler=False)) as c:
        yield c


@pytest.fixture(scope="module")
def data_client(ingested):
    """Portal on the shared, already ingested 32-day database."""
    with TestClient(create_app(ingested.settings, start_scheduler=False)) as c:
        yield c


def login(c, role):
    r = c.post("/login", data={"username": role, "password": role, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_anonymous_is_redirected(app_client):
    r = app_client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert app_client.get("/api/status").status_code == 401


def test_wrong_password(app_client):
    r = app_client.post("/login", data={"username": "admin", "password": "nope"}, follow_redirects=False)
    assert r.status_code == 401


@pytest.mark.parametrize("role,expect", [
    ("guest", {"/": 200, "/reports": 200, "/parameters/T_DETECTOR": 200, "/parameters/LASER_DRIFT": 404,
               "/map": 200, "/timeline": 200, "/api/timeline": 200, "/api/geo": 200,
               "/events": 403, "/events/1": 403, "/products": 403, "/admin": 403, "/api/parameters/T_DETECTOR/csv": 403,
               "/api/export": 403, "/api/v1/whoami": 403}),
    ("expert", {"/": 200, "/parameters/LASER_DRIFT": 200, "/events": 200, "/products": 200, "/admin": 403,
                "/api/parameters/T_DETECTOR/csv": 200, "/api/export": 200, "/api/v1/whoami": 200}),
    ("admin", {"/admin": 200, "/events": 200, "/timeline": 200}),
])
def test_role_matrix(app_client, role, expect):
    login(app_client, role)
    for path, code in expect.items():
        assert app_client.get(path, follow_redirects=False).status_code == code, path


def test_guest_api_hides_restricted_parameters(app_client):
    login(app_client, "guest")
    codes = {p["code"] for p in app_client.get("/api/parameters").json()}
    assert "T_DETECTOR" in codes and "LASER_DRIFT" not in codes
    series = app_client.get("/api/series?codes=T_DETECTOR,LASER_DRIFT").json()["series"]
    assert set(series) == {"T_DETECTOR"}


def test_only_admin_can_change_limits(app_client):
    login(app_client, "expert")
    assert app_client.post("/admin/limits/T_DETECTOR", data={"warn_hi": "95"},
                           follow_redirects=False).status_code == 403
    login(app_client, "admin")
    r = app_client.post("/admin/limits/T_DETECTOR", data={"warn_hi": "91.5", "alarm_hi": "92.5", "persistence": "2",
                                                          "comment": "test"}, follow_redirects=False)
    assert r.status_code == 303
    p = next(p for p in app_client.get("/api/parameters").json() if p["code"] == "T_DETECTOR")
    assert p["limit"]["warn_hi"] == 91.5


def test_write_api_with_token(app_client):
    """External tools: token from the admin page, then parameters / samples / timeline via /api/v1."""
    assert app_client.post("/api/v1/samples", json=[], headers={"Authorization": "Bearer nope"}).status_code == 401
    login(app_client, "admin")
    page = app_client.post("/admin/tokens", data={"name": "ext-tool", "role": "admin"}).text  # redirected to /admin
    token = page.split("(shown once):<br><span class=\"mono\" style=\"word-break:break-all\">")[1].split("<")[0]
    app_client.post("/logout")
    h = {"Authorization": f"Bearer {token}"}
    assert app_client.get("/api/v1/whoami", headers=h).json()["username"] == "token:ext-tool"
    r = app_client.post("/api/v1/parameters", headers=h, json={"code": "EXT_PARAM", "name": "External value"})
    assert r.status_code == 201
    r = app_client.post("/api/v1/samples", headers=h, json=[
        {"code": "EXT_PARAM", "time": "2026-01-01T00:00:00Z", "value": 1.5},
        {"code": "EXT_PARAM", "time": "2026-01-01T01:00:00Z", "value": 1.7}])
    assert r.status_code == 201 and r.json()["written"] == 2
    r = app_client.post("/api/v1/timeline", headers=h, json=[{"type": "OPERATIONS", "title": "Manoeuvre",
                                                              "start": "2026-01-01T02:00:00Z"}])
    assert r.status_code == 201
    login(app_client, "expert")
    csv = app_client.get("/api/parameters/EXT_PARAM/csv").text
    assert "external" in csv and "api:token:ext-tool" in csv


def test_pages_with_data(data_client, ingested):
    login(data_client, "admin")
    with ingested.db.session() as s:
        ev = s.scalar(select(Event).where(Event.rule == "E1_CALIBRATION_MODE"))
        prod = s.scalar(select(Product).where(Product.product_type == "FTS_L1B", Product.status == "PROCESSED"))
        l0 = s.scalar(select(Product).where(Product.product_type == "FTS_L0_EO"))
    for path in ("/", "/parameters", "/parameters/NESR_IDX", "/parameters/NESR_SPECTRAL", "/parameters/SSD",
                 "/explorer?codes=NESR_IDX,ZPD_OFFSET", "/map", "/timeline", "/events", "/events?category=timeline",
                 f"/events/{ev.id}", f"/products/{prod.id}", f"/products/{l0.id}", "/products?type=FTS_L0_EO",
                 "/admin", "/reports"):
        r = data_client.get(path)
        assert r.status_code == 200, path

    s = data_client.get("/api/series?codes=NESR_IDX,SSD&range=7d").json()
    assert len(s["series"]["NESR_IDX"]["traces"]) == 12 and s["series"]["SSD"]["requirement"]["lo"]
    assert any(e["type"] == "DATA_GAP" for e in data_client.get("/api/series?codes=SSD&range=all").json()["timeline"])
    sp = data_client.get("/api/spectrum?code=NESR_SPECTRAL&range=1d").json()
    assert set(sp["series"]) == {"band=B1", "band=B2", "band=B3"} and set(sp["template"]) == set(sp["series"])
    assert data_client.get("/api/geo?range=1d").json()["tracks"]
    st = data_client.get("/api/requirements/SSD/stats?range=all").json()
    assert st["n"] > 400 and st["compliance_percent"] == 100.0  # laser mode hop only on day 70
    z = zipfile.ZipFile(io.BytesIO(data_client.get("/api/export?range=7d&codes=SSD,NESR_SPECTRAL").content))
    assert {"samples.csv", "vector_samples.jsonl", "events.csv", "timeline.csv", "products.csv",
            "metadata.json"} <= set(z.namelist())

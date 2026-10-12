"""Role-based access control is enforced server-side."""
import pytest
from fastapi.testclient import TestClient

from imf.web.app import create_app


@pytest.fixture
def app_client(settings):
    with TestClient(create_app(settings, start_scheduler=False)) as c:
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
               "/events": 403, "/products": 403, "/admin": 403, "/api/parameters/T_DETECTOR/csv": 403}),
    ("expert", {"/": 200, "/parameters/LASER_DRIFT": 200, "/events": 200, "/products": 200, "/admin": 403,
                "/api/parameters/T_DETECTOR/csv": 200}),
    ("admin", {"/admin": 200, "/events": 200}),
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

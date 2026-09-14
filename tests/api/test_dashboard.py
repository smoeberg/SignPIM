"""Dashboard static serving."""
from fastapi.testclient import TestClient
from api.app import app


def test_dashboard_index():
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "SignPIM Dashboard" in r.text
    assert "app.js" in r.text


def test_static_assets():
    c = TestClient(app)
    assert c.get("/static/styles.css").status_code == 200
    assert c.get("/static/app.js").status_code == 200

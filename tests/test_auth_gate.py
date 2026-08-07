import os

import pytest

from app import create_app
from app.services import bundle_service


@pytest.fixture()
def app():
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    return app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def _login_admin(client):
    resp = client.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    assert resp.status_code == 200


@pytest.fixture()
def stub_bundle(monkeypatch):
    def _payload():
        return {
            "kpis": {
                "revenue": 0,
                "orders": 0,
                "avg_order_value": 0,
                "repeat_rate_avg": 0,
                "loyal_customers": 0,
                "loyal_share_pct": 0,
                "at_risk_90": 0,
                "active_last_30": 0,
                "new_last_90": 0,
                "revenue_last_90_total": 0,
                "revenue_last_30_total": 0,
                "profit": 0,
            },
            "trend": {},
            "table": {
                "rows": [],
                "page": 1,
                "page_size": 25,
                "total_rows": 0,
                "total_pages": 1,
                "page_totals": {"revenue": 0, "orders": 0, "profit": 0, "cost": 0},
            },
            "meta": {
                "dataset_version": "test",
                "cached": False,
                "page": 1,
                "page_size": 25,
                "total_rows": 0,
                "total_pages": 1,
            },
        }

    monkeypatch.setattr(bundle_service, "bundle", lambda page, args: _payload())
    monkeypatch.setattr(bundle_service, "drilldown", lambda entity, args: _payload())


def test_anonymous_access_blocked(client):
    html_paths = [
        "/",
        "/customers",
        "/products",
        "/regions",
        "/suppliers",
        "/salesreps",
        "/customers/drilldown/123",
    ]
    for path in html_paths:
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert location.startswith("/login?next=")

    api_resp = client.get("/api/filters/options")
    assert api_resp.status_code == 401
    payload = api_resp.get_json()
    assert payload["error"] == "auth_required"
    assert payload["login_url"] == "/login"

    api_bundle = client.get("/api/customers/bundle")
    assert api_bundle.status_code == 401
    payload = api_bundle.get_json()
    assert payload["error"] == "auth_required"
    assert payload["login_url"] == "/login"


def test_authenticated_access_and_logout(client, stub_bundle):
    _login_admin(client)

    resp = client.get("/customers/")
    assert resp.status_code == 200

    api_resp = client.get("/api/customers/bundle")
    assert api_resp.status_code == 200

    logout_resp = client.get("/auth/logout", follow_redirects=False)
    assert logout_resp.status_code == 302
    assert logout_resp.headers.get("Location", "").startswith("/login")

    after_logout = client.get("/customers", follow_redirects=False)
    assert after_logout.status_code == 302
    assert after_logout.headers.get("Location", "").startswith("/login?next=")

    api_after = client.get("/api/customers/bundle")
    assert api_after.status_code == 401


def test_cache_headers_authenticated(client, stub_bundle):
    _login_admin(client)

    html_resp = client.get("/customers/")
    assert html_resp.status_code == 200
    assert html_resp.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, max-age=0"
    assert html_resp.headers.get("Pragma") == "no-cache"
    assert html_resp.headers.get("Expires") == "0"

    api_resp = client.get("/api/customers/bundle")
    assert api_resp.status_code == 200
    assert api_resp.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, max-age=0"
    assert api_resp.headers.get("Pragma") == "no-cache"
    assert api_resp.headers.get("Expires") == "0"

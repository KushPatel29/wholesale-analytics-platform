import os

import pytest

from app import create_app
from app.auth.models import get_session, User
from app.services import bundle_service, filters_service
import app.blueprints.products as products_bp


@pytest.fixture()
def app_with_login():
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=False, AUTHZ_DISABLED=False)

    with get_session() as s:
        u = s.query(User).filter(User.username == "admin").first()
        if not u:
            u = User(username="admin", role="admin", is_active=True, is_approved=True)
            u.set_password("admin")
            s.add(u)
        else:
            u.is_active = True
            u.is_approved = True
            u.role = "admin"
            u.set_password("admin")
        s.commit()

    return app


@pytest.fixture()
def client(app_with_login):
    with app_with_login.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def stub_services(monkeypatch):
    def _bundle_stub(page, args):
        return {
            "kpis": {},
            "trend": {},
            "table": {"rows": []},
            "meta": {"cached": False, "dataset_version": "test"},
        }

    def _drill_stub(entity, args):
        entity_id = None
        if hasattr(args, "get"):
            for key in ("product_id", "customer_id", "supplier_id", "region_id", "salesrep_id", "rep_id", "id"):
                val = args.get(key)
                if val:
                    entity_id = str(val)
                    break
        return {
            "kpis": {},
            "trend": {},
            "table": {"rows": []},
            "meta": {"cached": False, "dataset_version": "test", "entity_id": entity_id or "stub"},
        }

    monkeypatch.setattr(bundle_service, "bundle", _bundle_stub)
    monkeypatch.setattr(bundle_service, "drilldown", _drill_stub)

    monkeypatch.setattr(
        filters_service,
        "get_filter_options",
        lambda *a, **k: {
            "options": {
                "regions": [{"id": "west", "label": "West", "bucket": "regions", "value": "west"}],
                "methods": [],
                "ship_methods": [],
                "customers": [],
                "suppliers": [],
                "products": [],
                "sales_reps": [],
                "statuses": [],
            },
            "dataset_version": "test",
            "filters": {},
            "scope": {},
            "cached": False,
        },
    )

    monkeypatch.setattr(
        products_bp,
        "get_products_parquet_status",
        lambda: products_bp.ProductsParquetStatus(path="stub", exists=True, auto_create=True),
    )
    monkeypatch.setattr(
        products_bp,
        "_build_overview_from_service",
        lambda *a, **k: {"kpis": {}, "trend": {}, "table": {"rows": []}},
    )


def test_smoke_core_endpoints(client):
    # Login page aliases
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code in {200, 302}
    resp = client.get("/auth/login")
    assert resp.status_code == 200

    # Unauthed redirect for protected pages
    resp = client.get("/customers/", follow_redirects=False)
    assert resp.status_code in {302, 401}

    # Login
    resp = client.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    assert resp.status_code == 200

    # Core pages
    for path in ("/", "/customers/", "/products/", "/regions/", "/suppliers/", "/salesreps/"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"

    # Filters options
    resp = client.get("/api/filters/options")
    assert resp.status_code == 200

    # Bundle endpoints
    for page in ("products", "customers", "regions", "suppliers", "salesreps"):
        resp = client.get(f"/api/{page}/bundle")
        assert resp.status_code == 200

    # Drilldown bundles (smoke)
    drilldowns = [
        ("products", {"product_id": "SKU-001"}),
        ("customers", {"customer_id": "CUST-001"}),
        ("regions", {"region_id": "West"}),
        ("suppliers", {"supplier_id": "SUP-001"}),
        ("salesreps", {"salesrep_id": "REP-001"}),
    ]
    for entity, params in drilldowns:
        resp = client.get(f"/api/{entity}/drilldown/bundle", query_string=params)
        assert resp.status_code == 200


def test_recommendations_route_removed(client):
    resp = client.get("/recommendations/", follow_redirects=False)
    assert resp.status_code in (302, 401)

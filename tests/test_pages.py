import copy
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest
pytestmark = pytest.mark.slow  # Tagged slow: tests spin up app + auth flows

from app import create_app
from app.blueprints import products as products_bp
from app.services import products as services_products
from app.auth.models import SavedView, get_saved_view, get_session, User


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    # Use fast password hashing during tests
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    os.environ.setdefault("AUTHZ_DISABLED", "false") # Explicitly disable authz bypass for tests
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    return app


def _make_df():
    base = datetime(2022, 1, 1)
    rows = []
    for i in range(1, 6):
        rows.append(
            {
                "Date": base + timedelta(days=i * 15),
                "OrderId": i,
                "CustomerId": 100 + i,
                "CustomerName": f"Customer {i}",
                "ProductId": 200 + i,
                "Name": f"Product {i}",
                "Region": "East" if i % 2 else "West",
                "SupplierId": 300 + i,
                "Supplier_Name": f"Supplier {i}",
                "revenue_ordered": 100.0 * i,
                "QuantityOrdered": i * 2,
                "Price": 10.0 * i,
                "CostPrice": 7.5 * i,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture(autouse=True)
def patch_data(monkeypatch):
    df = _make_df()
    # Patch get_fact_df in each blueprint module to avoid I/O
    for mod in [
        "app.blueprints.dashboard",
        "app.blueprints.customers",
        "app.blueprints.products",
        "app.blueprints.regions",
        "app.blueprints.suppliers",
        "app.blueprints.api_slice",
    ]:
        m = __import__(mod, fromlist=["get_fact_df"])
        monkeypatch.setattr(m, "get_fact_df", lambda *a, **k: df.copy(), raising=True)

    # Also patch _load_fact in app.services.products to ensure it returns data
    monkeypatch.setattr("app.services.products._load_fact", lambda *a, **k: df.copy(), raising=True)
    yield


@pytest.fixture()
def client_admin(app):
    with app.test_client() as c:
        # login as seeded admin
        c.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
        yield c


@pytest.fixture()
def client_sales(app):
    # ensure a sales user exists
    with get_session() as s:
        u = s.query(User).filter(User.username == "sales1").first()
        if not u:
            u = User(username="sales1", role="sales", is_active=True, is_approved=True)
            u.set_password("test")
            s.add(u)
        else:
            u.is_active = True
            u.is_approved = True
            u.role = "sales"
            u.set_password("test")
        s.commit()
    with app.test_client() as c:
        c.post("/auth/login", data={"username": "sales1", "password": "test"}, follow_redirects=True)
        yield c


def test_admin_pages_ok(client_admin):
    ok_paths = [
        "/",
        "/customers/",
        "/products/",
        "/regions/",
        "/suppliers/",
        "/admin/users",
        "/admin/roles",
    ]
    for p in ok_paths:
        r = client_admin.get(p)
        assert r.status_code == 200, f"{p} returned {r.status_code}"


def test_sales_forbidden_paths(client_sales):
    # Sales should not access suppliers or admin
    r1 = client_sales.get("/suppliers/")
    assert r1.status_code == 403
    r2 = client_sales.get("/admin/users")
    assert r2.status_code == 403


def test_filter_save_persists_session(client_admin, app):
    # Seed filters in session
    filters = {"start_date": "2022-01-01", "regions": ["East"]}
    with client_admin.session_transaction() as sess:
        sess["filters"] = filters.copy()

    # Save current view (does not modify filters)
    resp = client_admin.post("/views/save", data={"name": "Smoke"}, follow_redirects=True)
    assert resp.status_code == 200

    # After a GET, the sync hook should ensure global_filters mirrors filters
    _ = client_admin.get("/")
    with client_admin.session_transaction() as sess:
        saved_filters = sess.get("filters") or {}
        saved_global = sess.get("global_filters") or {}
        assert saved_filters.get("start_date") == filters["start_date"]
        assert saved_filters.get("regions") == filters["regions"]
        assert saved_global.get("start_date") == filters["start_date"]
        assert saved_global.get("regions") == filters["regions"]


def test_saved_view_load_update_delete_flow(client_admin):
    initial_filters = {
        "start_date": "2022-01-01",
        "end_date": "2022-01-31",
        "regions": ["East"],
        "customers": ["Customer 1"],
    }
    with client_admin.session_transaction() as sess:
        sess["filters"] = initial_filters.copy()
        sess["global_filters"] = initial_filters.copy()

    save_resp = client_admin.post("/views/save", data={"name": "Leadership Snapshot"}, follow_redirects=True)
    assert save_resp.status_code == 200

    with get_session() as s:
        view = (
            s.query(SavedView)
            .filter(SavedView.name == "Leadership Snapshot")
            .order_by(SavedView.id.desc())
            .first()
        )
        assert view is not None
        view_id = int(view.id)

    with client_admin.session_transaction() as sess:
        assert sess.get("active_saved_view_id") == view_id
    updated_filters = {
        "date_preset": "mtd",
        "regions": ["West"],
        "suppliers": ["Supplier 2"],
        "products": ["Product 2"],
    }
    update_resp = client_admin.post(f"/views/update/{view_id}", data=updated_filters, follow_redirects=True)
    assert update_resp.status_code == 200
    updated_view = get_saved_view(view_id)
    assert updated_view is not None
    updated_payload = json.loads(updated_view.filters_json or "{}")
    assert updated_payload.get("regions") == ["West"]
    assert updated_payload.get("suppliers") == ["Supplier 2"]
    assert updated_payload.get("products") == ["Product 2"]

    load_resp = client_admin.post(f"/views/load/{view_id}", data={}, follow_redirects=True)
    assert load_resp.status_code == 200
    with client_admin.session_transaction() as sess:
        assert sess.get("active_saved_view_id") == view_id
        assert (sess.get("filters") or {}).get("regions") == ["West"]
        assert (sess.get("filters") or {}).get("products") == ["Product 2"]

    delete_resp = client_admin.post(f"/views/delete/{view_id}", data={}, follow_redirects=True)
    assert delete_resp.status_code == 200
    assert get_saved_view(view_id) is None
    with client_admin.session_transaction() as sess:
        assert sess.get("active_saved_view_id") is None


def test_products_drilldown_smoke(client_admin, monkeypatch):
    payload = {
        "top_products": [{"desc": "Product 201", "sku": "201", "product_id": "201"}],
        "trend": [{"period": "2024-01", "revenue": 250.0}],
        "breakdowns": {
            "by_region": [{"key": "East", "revenue": 200.0}],
            "by_supplier": [{"key": "Supplier 201", "revenue": 250.0}],
            "by_customer": [{"key": "Customer 201", "revenue": 250.0}],
        },
        "price_dist": {"samples": [10.0, 11.0, 12.0], "p10": 10.0, "p50": 11.0, "p90": 12.0},
    }

    monkeypatch.setattr(products_bp, "build_overview_payload", lambda filters: copy.deepcopy(payload))
    resp = client_admin.get("/products/201")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Product 201" in html

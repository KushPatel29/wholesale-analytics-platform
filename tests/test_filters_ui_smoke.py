from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.core.exceptions import DatasetNotBuiltError


def _sample_df():
    base = datetime(2022, 1, 1)
    rows = []
    for i in range(1, 6):
        rows.append(
            {
                "Date": base + timedelta(days=i * 7),
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
def patch_fact(monkeypatch):
    df = _sample_df()
    for mod in [
        "app.blueprints.dashboard",
        "app.blueprints.customers",
        "app.blueprints.products",
        "app.blueprints.regions",
        "app.blueprints.suppliers",
        "app.blueprints.api_slice",
    ]:
        try:
            module = __import__(mod, fromlist=["get_fact_df"])
            monkeypatch.setattr(module, "get_fact_df", lambda *args, **kwargs: df.copy(), raising=True)
        except Exception:
            continue

    monkeypatch.setattr("app.core.data_service.get_fact_df", lambda *a, **k: df.copy(), raising=True)
    monkeypatch.setattr("app.core.data_service.apply_global_filters", lambda _df, *a, **k: _df.copy(), raising=True)
    monkeypatch.setattr("app.core.rbac.scope_dataframe", lambda _df, *_a, **_k: _df.copy(), raising=True)
    monkeypatch.setattr("app.core.features.legacy_pandas_enabled", lambda *a, **k: True, raising=True)
    monkeypatch.setattr("app.services.products._load_fact", lambda *a, **k: df.copy(), raising=True)
    yield


@pytest.fixture
def authed_client(client, monkeypatch):
    class _DummyUser:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        role = "admin"

        def get_id(self):
            return "admin"

    monkeypatch.setattr("flask_login.utils._get_user", lambda *a, **k: _DummyUser())
    return client


@pytest.mark.parametrize(
  "path",
  [
      "/",
      "/products/",
      "/customers/",
      "/regions/",
      "/suppliers/",
      "/salesreps/",
  ],
)
def test_filters_container_renders_on_pages(authed_client, path):
    resp = authed_client.get(path, follow_redirects=True)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200, f"{path} returned {resp.status_code}"
    assert 'id="GlobalFilters"' in html
    assert 'id="filtersDimensionGrid"' in html
    assert 'id="filtersDimensionWorkspace"' in html
    assert 'id="filtersWorkspaceEmpty"' in html
    assert 'id="filterTileStatuses"' in html
    assert 'id="filterPanelStatuses"' in html
    assert 'id="filterTileRegions"' in html
    assert 'id="filterPanelMethods"' in html
    assert 'id="fProducts"' in html
    assert 'id="fStatuses"' in html
    assert 'id="fDateType"' in html
    assert 'id="clearDimensionFiltersBtn"' in html
    assert 'id="updateSavedViewBtn"' in html
    assert 'id="savedViewsSection"' in html
    assert 'id="filtersNoticeBanner"' in html
    assert 'id="filtersBootstrapData"' in html
    assert 'data-range="current_fy"' in html
    assert 'data-range="previous_fy"' in html
    assert 'data-range="current_fq"' in html
    assert 'data-range="previous_fq"' in html
    assert 'data-range="current_fm"' in html
    assert 'data-range="previous_fm"' in html
    assert 'data-range="fytd_comparison"' in html
    assert 'data-range="all"' in html
    assert "Private saved views" in html


def test_customers_page_keeps_filters_shell_when_bundle_is_unavailable(authed_client, monkeypatch):
    monkeypatch.setattr(
        "app.blueprints.customers.bundle_service.bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DatasetNotBuiltError("fact view unavailable")),
    )

    resp = authed_client.get("/customers/", follow_redirects=True)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="GlobalFilters"' in html
    assert 'id="filtersDimensionGrid"' in html

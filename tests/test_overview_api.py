import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

pytestmark = pytest.mark.slow  # Tagged slow: runs heavy aggregation paths

from app.blueprints import overview as overview_api


def _make_df():
    base = datetime(2023, 1, 1)
    rows = []
    regions = ["North", "South", "East"]
    products = ["Bacon", "Ham", "Sausage"]
    suppliers = ["FarmCo", "MeatCorp", "Grocer"]
    for i in range(1, 25):
        rows.append(
            {
                "Date": base + timedelta(days=i * 10),
                "ShipDate": base + timedelta(days=i * 10 + 2),
                "POL_DeliveryDate": base + timedelta(days=i * 10 + 3),
                "OrderId": f"ORD-{i}",
                "CustomerId": f"CUST-{i % 7}",
                "CustomerName": f"Customer {i % 7}",
                "RegionName": regions[i % len(regions)],
                "ProductId": f"PROD-{i % 6}",
                "ProductName": products[i % len(products)],
                "SkuName": f"SKU-{i % 6}",
                "SupplierId": f"SUP-{i % 4}",
                "SupplierName": suppliers[i % len(suppliers)],
                "ShipMethod_Name": "Ground" if i % 2 else "Air",
                "Price": 10.0 * i,
                "CostPrice": 7.5 * i,
                "pack_item_count_sum": i * 2,
                "revenue_ordered": 100.0 * i,
                "revenue_shipped": 100.0 * i,
            }
        )
    df = pd.DataFrame(rows)
    df["Revenue"] = df["revenue_ordered"]
    df["Cost"] = df["revenue_ordered"] * 0.7
    df["Profit"] = df["Revenue"] - df["Cost"]
    df["MarginPct"] = df["Profit"] / df["Revenue"]
    df["EffectiveDate"] = pd.to_datetime(df["Date"])
    return df


@pytest.fixture(scope="session")
def app():
    from app import create_app

    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=True)
    return app


@pytest.fixture()
def client(app, monkeypatch):
    import app.services.frame as frame
    import app.services.fact_store as fact_store
    import data_loader as loader

    df = _make_df()

    def _snapshot(columns=None):
        data = df.copy()
        if columns:
            missing = [col for col in columns if col not in data.columns]
            for col in missing:
                data[col] = pd.NA
            return data[columns].copy()
        return data

    # monkeypatch.setattr(frame, "load_canonical_df", lambda: df.copy(), raising=True)
    monkeypatch.setattr(loader, "get_fact_df", _snapshot, raising=True)
    monkeypatch.setattr(loader, "current_version", lambda: "test-version", raising=True)
    monkeypatch.setattr(fact_store, "_require_manifest", lambda: None, raising=False)

    with app.test_client() as c:
        yield c


def test_filters(client):


    response = client.get("/api/overview/options")


    assert response.status_code == 200


    payload = response.get_json()


    assert "regions" in payload and isinstance(payload["regions"], list)


    assert "methods" in payload and isinstance(payload["methods"], list)


    assert "customers" in payload and isinstance(payload["customers"], list)








# def test_data(client):


#     response = client.post(


#         "/api/overview/data",


#         json={


#             "regions": ["All"],


#             "methods": ["All"],


#             "customers": ["All"],


#             "start": "2020-01-01",


#             "end": "2024-12-31",


#         },


#     )


#     assert response.status_code == 200


#     payload = response.get_json()


#     for key in ("kpis", "monthly", "pareto", "tenure", "weekday", "ordfreq", "dataQuality"):


#         assert key in payload


#     assert "insights" in payload and isinstance(payload["insights"], dict)


#     assert "operations" in payload and isinstance(payload["operations"], dict)


#     assert "recommendations" in payload and isinstance(payload["recommendations"], list)


#     customers = payload["insights"].get("customers", {})


#     assert isinstance(customers, dict)


#     assert "top" in customers and isinstance(customers["top"], list)


#     ops = payload["operations"]


#     assert "velocity" in ops and "margin" in ops and "shipping" in ops


#     shipping = ops.get("shipping", {})


#     assert isinstance(shipping.get("deliveries"), list)


#     if shipping["deliveries"]:


#         entry = shipping["deliveries"][0]


#         assert "date" in entry and "type" in entry


#     assert "meta" in payload and isinstance(payload["meta"], dict)


#     meta = payload["meta"]


#     assert "version" in meta


#     assert "user" in meta


#     load_meta = meta.get("load")


#     assert load_meta and load_meta["used_stage"] in {"loader", "canonical"}


#     assert load_meta.get("synthetic") in (False, None)


#     notes = meta.get("notes", [])


#     assert isinstance(notes, list)


#     filters_applied = meta.get("filters_applied")


#     assert filters_applied and isinstance(filters_applied, dict)


#     assert "has_active_filters" in filters_applied


#     assert "regions" in filters_applied


#     data_window = meta.get("data_window")


#     assert data_window and isinstance(data_window, dict)


#     assert "rows" in data_window and data_window["rows"] == len(_make_df())


#     active_window = meta.get("active_window")


#     assert active_window and isinstance(active_window, dict)


#     assert active_window.get("rows", 0) > 0


#     assert isinstance(meta["data_window"].get("start"), (str, type(None)))


#     for rec in payload["recommendations"]:


#         assert rec["focus"]


#         assert rec["severity"] in {"critical", "warning", "info", "success"}


#         assert isinstance(rec["message"], str) and rec["message"]



def test_summary_endpoint(client):
    response = client.get("/api/overview/summary")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["meta"]["source"] == "overview_summary"
    assert "kpis" in payload and "trends" in payload
    assert "insights" in payload and "health" in payload
    weight = payload["kpis"]["weight"]
    assert "value" in weight and "avg_per_order" in weight
    assert "weight" in payload["trends"]
    insights = payload["insights"]
    assert "customers" in insights and isinstance(insights["customers"]["top"], list)
    assert "sales_reps" in insights and isinstance(insights["sales_reps"]["top"], list)


def test_summary_endpoint_refactored(client):
    response = client.get("/api/overview/summary")
    assert response.status_code == 200
    payload = response.get_json()
    assert "kpis" in payload
    assert "trends" in payload
    assert "insights" in payload
    assert "health" in payload
    assert "spotlight" in payload
    assert "meta" in payload
    assert payload["meta"]["version"] == "test-version"


def test_overview_api_smoke(client, monkeypatch):
    # Allow admin health check bypass in test mode
    import app.blueprints.admin_api as admin_api

    monkeypatch.setattr(admin_api, "_is_admin_request", lambda: True, raising=False)

    # Hit overview API with explicit window
    resp = client.get("/api/overview/summary?start=2023-01-01&end=2024-12-31")
    assert resp.status_code == 200
    summary = resp.get_json()
    kpis = summary["kpis"]
    assert kpis["revenue"] > 0
    assert kpis["cost"] > 0
    assert kpis["profit"] == kpis["revenue"] - kpis["cost"]

    # Health endpoint for parity
    health = client.get("/api/_admin/health/data").get_json()
    assert health["revenue_sum"] > 0
    assert health["product_count"] >= 1
    assert health["effective_date_null_rate"] <= 1.0

    # Rowcount alignment within tolerance (5%)
    api_rows = summary["meta"]["window"]["rows"]
    fact_rows = health["fact_rowcount"]
    if fact_rows:
        delta_pct = abs(api_rows - fact_rows) / fact_rows * 100
        assert delta_pct <= 5.0

    # Ensure cost fields are present in API payload
    assert "cost_missing_rate" in summary["meta"]
    assert "cost" in kpis and "profit" in kpis and "margin_pct" in kpis


# @pytest.mark.parametrize("kind", [
#     "region_customer",
#     "customer_product",
#     "product_customer",
#     "supplier_product",
# ])
# @pytest.mark.parametrize("top_n", [10, 20, 30])
# def test_stacked_shapes(client, kind, top_n):
#     response = client.get(
#         f"/api/overview/stacked?kind={kind}&metric=revenue&freq=M&top_n={top_n}&start=2020-01-01&end=2024-12-31"
#     )
#     assert response.status_code == 200
#     payload = response.get_json()
#     meta = payload["meta"]
#     assert meta["kind"] == kind
#     assert meta["metric"] == "revenue"
#     assert meta["top_n"] == top_n
#     assert meta["note"] == overview_api.STACKED_NOTE
#     assert meta["version"] == "test-version"
#     assert meta["freq"] in {"M", "Q"}
#     assert isinstance(meta["periods"], int) and meta["periods"] > 0

#     series = payload["series"]
#     assert isinstance(series, list)
#     assert len(series) <= top_n + 1
#     if len(series) > top_n:
#         assert series[-1]["name"] == "Other"
#     for item in series:
#         assert isinstance(item["name"], str)
#         assert isinstance(item["values"], list)
#         for pair in item["values"]:
#             assert isinstance(pair, list)
#             assert len(pair) == 2
#             period, value = pair
#             assert isinstance(period, str)
#             assert isinstance(value, (int, float))


# def test_stacked_orders_counts_unique_orders(client):
#     response = client.get(
#         "/api/overview/stacked?kind=region_customer&metric=orders&freq=M&top_n=10&start=2020-01-01&end=2024-12-31"
#     )
#     assert response.status_code == 200
#     payload = response.get_json()
#     total_orders = len(_make_df()["OrderId"].unique())
#     summed = sum(float(value) for series in payload["series"] for _, value in series["values"])
#     assert pytest.approx(summed, rel=1e-6) == float(total_orders)



# def test_data_synthetic_fallback(client, monkeypatch):
#     # Force loader + canonical path to fail so the endpoint must fabricate demo data.
#     def _boom(*args, **kwargs):
#         raise RuntimeError("loader unavailable")

#     monkeypatch.setattr(overview_api.loader, "get_fact_df", _boom, raising=True)
#     # monkeypatch.setattr(overview_api, "load_canonical_df", lambda columns=None: pd.DataFrame(), raising=True)

#     resp = client.post(
#         "/api/overview/data",
#         json={
#             "regions": ["All"],
#             "methods": ["All"],
#             "customers": ["All"],
#         },
#     )
#     assert resp.status_code == 200
#     payload = resp.get_json()
#     assert payload["meta"].get("synthetic") is True
#     assert payload["kpis"]["total_revenue"] > 0
#     load_meta = payload["meta"]["load"]
#     assert load_meta["used_stage"] == "synthetic"
#     assert load_meta.get("synthetic") is True
#     stages = {attempt["stage"]: attempt for attempt in load_meta["attempts"]}
#     assert stages["loader"]["status"] == "error"
#     assert stages["canonical"]["status"] == "empty"

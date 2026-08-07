import pytest

from app.services import fact_schema as fs
from app.services import fact_store
from pytest import approx


def _pick_customer_id() -> str | None:
    try:
        cols = fact_store.list_columns()
        if fs.CANON.customer_id not in cols:
            return None
        conn = fact_store.get_conn()
        row = conn.execute(f"SELECT {fs.CANON.customer_id} FROM fact WHERE {fs.CANON.customer_id} IS NOT NULL LIMIT 1").fetchone()
        return str(row[0]) if row else None
    except Exception:
        return None


def test_customers_bundle_kpis_not_limited(client):
    resp1 = client.get("/api/customers/bundle", query_string={"page_size": 1})
    if resp1.status_code == 503:
        pytest.skip("Dataset not built for bundle smoke test")
    data1 = resp1.get_json()
    resp2 = client.get("/api/customers/bundle", query_string={"page_size": 2})
    data2 = resp2.get_json()

    assert data1["kpis"]["revenue"] == approx(data2["kpis"]["revenue"], rel=1e-6, abs=0.01)
    assert data1["meta"]["total_rows"] >= len(data1["table"]["rows"])


def test_customers_bundle_pagination_changes_rows(client):
    resp = client.get("/api/customers/bundle", query_string={"page_size": 1, "page": 1})
    if resp.status_code == 503:
        pytest.skip("Dataset not built for bundle smoke test")
    data = resp.get_json()
    total_rows = data["meta"]["total_rows"]
    if total_rows <= 1:
        pytest.skip("Not enough customers to test pagination")
    first_row = data["table"]["rows"][0]
    resp2 = client.get("/api/customers/bundle", query_string={"page_size": 1, "page": 2})
    data2 = resp2.get_json()
    second_row = data2["table"]["rows"][0] if data2["table"]["rows"] else None
    assert second_row is not None
    assert first_row != second_row


def test_customers_drilldown_bundle_smoke(client):
    cust_id = _pick_customer_id()
    if not cust_id:
        pytest.skip("No customer id available")
    resp = client.get("/api/customers/drilldown/bundle", query_string={"customer_id": cust_id})
    if resp.status_code == 503:
        pytest.skip("Dataset not built for drilldown smoke test")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "kpis" in payload and "table" in payload
    assert payload["meta"].get("entity_id") == str(cust_id)
    trend = payload.get("trend") or {}
    assert "orders" in trend
    assert len(trend.get("labels", [])) == len(trend.get("orders", []))


def test_customers_cohorts_numeric(client):
    resp = client.get("/api/customers/bundle")
    if resp.status_code == 503:
        pytest.skip("Dataset not built for bundle smoke test")
    data = resp.get_json()
    heatmap = (data.get("cohorts") or {}).get("heatmap") or {}
    rows = heatmap.get("rows") or []
    for row in rows:
        for val in row:
            assert isinstance(val, (int, float))


def test_customers_drilldown_contract_fields(client):
    cust_id = _pick_customer_id()
    if not cust_id:
        pytest.skip("No customer id available")
    resp = client.get("/api/customers/drilldown/bundle", query_string={"customer_id": cust_id})
    if resp.status_code == 503:
        pytest.skip("Dataset not built for drilldown contract test")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data.get("top_spend"), list)
    weekday = data.get("weekday") or {}
    assert len(weekday.get("labels", [])) == 7
    assert isinstance(data.get("kpis", {}).get("orders_last_90"), int)


def test_customers_drilldown_highlights_and_action_center(client):
    target_id = "2914"
    resp = client.get("/api/customers/drilldown/bundle", query_string={"customer_id": target_id})
    if resp.status_code in (404, 400):
        cust_id = _pick_customer_id()
        if not cust_id:
            pytest.skip("No customer id available")
        resp = client.get("/api/customers/drilldown/bundle", query_string={"customer_id": cust_id})
        target_id = cust_id
    if resp.status_code == 503:
        pytest.skip("Dataset not built for drilldown highlights test")

    data = resp.get_json()
    assert isinstance(data.get("top_product"), dict)
    assert isinstance(data.get("top_weight_mover"), dict)
    assert isinstance(data.get("top_profit_product"), dict)
    assert data.get("pricing_spread") is None or isinstance(data.get("pricing_spread"), dict)

    cross_sell = data.get("cross_sell_ideas") or data.get("cross_sell") or []
    assert isinstance(cross_sell, list)

    orders_30 = data.get("orders_last_30d", data.get("kpis", {}).get("orders_last_30"))
    orders_90 = data.get("orders_last_90d", data.get("kpis", {}).get("orders_last_90"))
    assert isinstance(orders_30, int) and orders_30 >= 0
    assert isinstance(orders_90, int)
    if str(target_id) == "2914":
        assert orders_90 > 0

    days_last = data.get("days_since_last_order", data.get("kpis", {}).get("days_since_last_order"))
    assert days_last is None or isinstance(days_last, int)

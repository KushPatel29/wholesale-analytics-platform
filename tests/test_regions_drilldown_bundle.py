import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_regions_drilldown(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-05",
            "DateExpected": "2025-01-05",
            "RegionName": "Region-01",
            "OrderId": "O-1",
            "CustomerId": "C-1",
            "CustomerName": "Customer 1",
            "ProductId": "P-1",
            "ProductName": "Product 1",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": 600.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 1000.0,
            "CostPrice": 600.0,
        },
        {
            "Date": "2025-02-10",
            "DateExpected": "2025-02-10",
            "RegionName": "Region-01",
            "OrderId": "O-2",
            "CustomerId": "C-1",
            "CustomerName": "Customer 1",
            "ProductId": "P-2",
            "ProductName": "Product 2",
            "ShippingMethodName": "Air",
            "OrderStatus": "packed",
            "Revenue": 1200.0,
            "Cost": 720.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 1200.0,
            "CostPrice": 720.0,
        },
        {
            "Date": "2025-01-20",
            "DateExpected": "2025-01-20",
            "RegionName": "Region-01",
            "OrderId": "O-3",
            "CustomerId": "C-2",
            "CustomerName": "Customer 2",
            "ProductId": "P-1",
            "ProductName": "Product 1",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Revenue": 500.0,
            "Cost": 300.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 500.0,
            "CostPrice": 300.0,
        },
        {
            "Date": "2025-06-15",
            "DateExpected": "2025-06-15",
            "RegionName": "Region-01",
            "OrderId": "O-4",
            "CustomerId": "C-3",
            "CustomerName": "Customer 3",
            "ProductId": "P-3",
            "ProductName": "Product 3",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Revenue": 1500.0,
            "Cost": 900.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 1500.0,
            "CostPrice": 900.0,
        },
        {
            "Date": "2025-06-01",
            "DateExpected": "2025-06-01",
            "RegionName": "Region-02",
            "OrderId": "O-5",
            "CustomerId": "C-9",
            "CustomerName": "Customer 9",
            "ProductId": "P-9",
            "ProductName": "Product 9",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Revenue": 700.0,
            "Cost": 420.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 700.0,
            "CostPrice": 420.0,
        },
    ]

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_regions_drilldown.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_regions_drilldown_bundle_shape_and_budget(app_client, seed_regions_drilldown):
    resp = app_client.get(
        "/api/regions/drilldown/bundle",
        query_string={
            "region_id": "Region-01",
            "start": "2025-01-01",
            "end": "2025-12-31",
            "topN": 10,
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}

    for key in ("kpis", "trend", "table", "charts", "meta"):
        assert key in payload

    meta = payload.get("meta", {})
    assert meta.get("dataset_version")
    assert 4 <= int(meta.get("duckdb_query_count", 0) or 0) <= 6

    kpis = payload.get("kpis", {})
    assert kpis.get("revenue") is not None
    assert kpis.get("orders") is not None
    assert kpis.get("customers") is not None
    assert kpis.get("avg_order_value") is not None
    assert kpis.get("repeat_pct") is not None
    assert kpis.get("churn_pct") is not None

    trend = payload.get("trend", {})
    assert len(trend.get("labels") or []) >= 2

    charts = payload.get("charts", {})
    for key in ("top_customers", "top_products", "shipping_mix", "weekday_revenue", "churned_customers"):
        assert key in charts

    region_v2 = payload.get("region_v2") or {}
    assert "scorecard" in region_v2
    assert "customers" in region_v2
    assert "products" in region_v2

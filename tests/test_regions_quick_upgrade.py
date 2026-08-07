from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services import fact_store
from app.services import regions_bundle


@pytest.fixture
def seed_regions_quick_upgrade(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-02-10",
            "DateExpected": "2025-02-10",
            "RegionName": "West Coast - Long Region Label",
            "OrderId": "W-1",
            "CustomerId": "C-1",
            "CustomerName": "Customer 1",
            "ProductId": "P-1",
            "ProductName": "Product 1",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": 620.0,
            "QuantityShipped": 8.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 1000.0,
            "CostPrice": 620.0,
        },
        {
            "Date": "2025-03-12",
            "DateExpected": "2025-03-12",
            "RegionName": "West Coast - Long Region Label",
            "OrderId": "W-2",
            "CustomerId": "C-2",
            "CustomerName": "Customer 2",
            "ProductId": "P-2",
            "ProductName": "Product 2",
            "ShippingMethodName": "Ground",
            "OrderStatus": "packed",
            "Revenue": 1450.0,
            "Cost": 870.0,
            "QuantityShipped": 11.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 1450.0,
            "CostPrice": 870.0,
        },
        {
            "Date": "2025-02-18",
            "DateExpected": "2025-02-18",
            "RegionName": "Prairie",
            "OrderId": "P-1",
            "CustomerId": "C-3",
            "CustomerName": "Customer 3",
            "ProductId": "P-3",
            "ProductName": "Product 3",
            "ShippingMethodName": "Air",
            "OrderStatus": "packed",
            "Revenue": 700.0,
            "Cost": 420.0,
            "QuantityShipped": 5.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 700.0,
            "CostPrice": 420.0,
        },
        {
            "Date": "2025-03-20",
            "DateExpected": "2025-03-20",
            "RegionName": "Prairie",
            "OrderId": "P-2",
            "CustomerId": "C-3",
            "CustomerName": "Customer 3",
            "ProductId": "P-4",
            "ProductName": "Product 4",
            "ShippingMethodName": "Air",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": None,
            "QuantityShipped": 9.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 1000.0,
            "CostPrice": 600.0,
        },
    ]
    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_regions_quick_upgrade.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_regions_route_renders_with_or_without_gf(app_client, seed_regions_quick_upgrade):
    resp_default = app_client.get("/regions/")
    assert resp_default.status_code == 200
    assert "Regions Overview" in resp_default.get_data(as_text=True)

    resp_gf = app_client.get("/regions/", query_string={"_gf": "1", "start": "2025-03-01", "end": "2025-03-31"})
    assert resp_gf.status_code == 200
    assert "Regions Overview" in resp_gf.get_data(as_text=True)


def test_regions_momentum_export_csv_headers(app_client, seed_regions_quick_upgrade):
    resp = app_client.get(
        "/regions/export_momentum",
        query_string={"format": "csv", "start": "2025-03-01", "end": "2025-03-31"},
    )
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "Region" in text
    assert "RevenueCurrent" in text
    assert "DeltaRevenuePct" in text
    frame = pd.read_csv(io.StringIO(text))
    assert {"Region", "RevenueCurrent", "RevenuePrior", "DeltaRevenue", "DeltaRevenuePct"}.issubset(frame.columns)


def test_regions_comparison_window_math():
    windows = regions_bundle._comparison_windows("2025-03-01", "2025-04-01")
    assert windows["current_start"] == "2025-03-01"
    assert windows["current_end"] == "2025-04-01"
    # build_where_clause uses an exclusive end date (< end), so this compares equal span windows.
    assert windows["prior_start"] == "2025-01-29"
    assert windows["prior_end"] == "2025-03-01"
    assert windows["yoy_start"] == "2024-03-01"
    assert windows["yoy_end"] == "2024-04-01"

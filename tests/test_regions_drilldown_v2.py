from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_regions_drilldown_v2(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-03-05",
            "DateExpected": "2025-03-05",
            "RegionName": "Vancouver W",
            "OrderId": "VW-P-1",
            "CustomerId": "C_PRIOR_ONLY",
            "CustomerName": "Prior Only Customer",
            "ProductId": "P1",
            "ProductName": "Prime Rib",
            "ShippingMethodName": "Ground",
            "SupplierId": "SUP-1",
            "SupplierName": "Supplier One",
            "OrderStatus": "packed",
            "Revenue": 300.0,
            "Cost": 180.0,
            "QuantityShipped": 6.0,
            "WeightLb": 24.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 24.0,
            "pack_count": 1,
            "Price": 300.0,
            "CostPrice": 180.0,
        },
        {
            "Date": "2025-03-10",
            "DateExpected": "2025-03-10",
            "RegionName": "Vancouver W",
            "OrderId": "VW-P-2",
            "CustomerId": "C_KEEPER",
            "CustomerName": "Keeper Customer",
            "ProductId": "P2",
            "ProductName": "Striploin",
            "ShippingMethodName": "Air",
            "SupplierId": "SUP-2",
            "SupplierName": "Supplier Two",
            "OrderStatus": "packed",
            "Revenue": 400.0,
            "Cost": 260.0,
            "QuantityShipped": 8.0,
            "WeightLb": 32.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 32.0,
            "pack_count": 1,
            "Price": 400.0,
            "CostPrice": 260.0,
        },
        {
            "Date": "2025-04-05",
            "DateExpected": "2025-04-05",
            "RegionName": "Vancouver W",
            "OrderId": "VW-C-1",
            "CustomerId": "C_KEEPER",
            "CustomerName": "Keeper Customer",
            "ProductId": "P2",
            "ProductName": "Striploin",
            "ShippingMethodName": "Air",
            "SupplierId": "SUP-2",
            "SupplierName": "Supplier Two",
            "OrderStatus": "packed",
            "Revenue": 500.0,
            "Cost": 320.0,
            "QuantityShipped": 9.0,
            "WeightLb": 36.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 36.0,
            "pack_count": 1,
            "Price": 500.0,
            "CostPrice": 320.0,
        },
        {
            "Date": "2025-04-10",
            "DateExpected": "2025-04-10",
            "RegionName": "Vancouver W",
            "OrderId": "VW-C-2",
            "CustomerId": "C_GROW",
            "CustomerName": "Growth Customer",
            "ProductId": "P3",
            "ProductName": "Top Sirloin",
            "ShippingMethodName": "Ground",
            "SupplierId": "SUP-1",
            "SupplierName": "Supplier One",
            "OrderStatus": "packed",
            "Revenue": 600.0,
            "Cost": 360.0,
            "QuantityShipped": 10.0,
            "WeightLb": 40.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 40.0,
            "pack_count": 1,
            "Price": 600.0,
            "CostPrice": 360.0,
        },
        {
            "Date": "2025-04-15",
            "DateExpected": "2025-04-15",
            "RegionName": "Vancouver W",
            "OrderId": "VW-C-3",
            "CustomerId": "C_NEW",
            "CustomerName": "New Customer",
            "ProductId": "P4",
            "ProductName": "Chuck Roast",
            "ShippingMethodName": "Ground",
            "SupplierId": "SUP-3",
            "SupplierName": "Supplier Three",
            "OrderStatus": "packed",
            "Revenue": 250.0,
            "Cost": None,
            "QuantityShipped": 5.0,
            "WeightLb": 20.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 20.0,
            "pack_count": 1,
            "Price": 250.0,
            "CostPrice": None,
        },
        {
            "Date": "2025-04-20",
            "DateExpected": "2025-04-20",
            "RegionName": "Vancouver W",
            "OrderId": "VW-C-4",
            "CustomerId": "C_REPEAT",
            "CustomerName": "Repeat Customer",
            "ProductId": "P2",
            "ProductName": "Striploin",
            "ShippingMethodName": "Ground",
            "SupplierId": "SUP-2",
            "SupplierName": "Supplier Two",
            "OrderStatus": "packed",
            "Revenue": 150.0,
            "Cost": 90.0,
            "QuantityShipped": 3.0,
            "WeightLb": 12.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 12.0,
            "pack_count": 1,
            "Price": 150.0,
            "CostPrice": 90.0,
        },
        {
            "Date": "2025-04-24",
            "DateExpected": "2025-04-24",
            "RegionName": "Vancouver W",
            "OrderId": "VW-C-5",
            "CustomerId": "C_REPEAT",
            "CustomerName": "Repeat Customer",
            "ProductId": "P5",
            "ProductName": "Short Rib",
            "ShippingMethodName": "Courier",
            "SupplierId": "SUP-2",
            "SupplierName": "Supplier Two",
            "OrderStatus": "packed",
            "Revenue": 100.0,
            "Cost": 55.0,
            "QuantityShipped": 2.0,
            "WeightLb": 8.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 8.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 55.0,
        },
        {
            "Date": "2025-04-29",
            "DateExpected": "2025-04-29",
            "RegionName": "Fresh Region",
            "OrderId": "FR-1",
            "CustomerId": "C_FRESH",
            "CustomerName": "Fresh Customer",
            "ProductId": "P6",
            "ProductName": "Tenderloin",
            "ShippingMethodName": "Ground",
            "SupplierId": "SUP-9",
            "SupplierName": "Supplier Nine",
            "OrderStatus": "packed",
            "Revenue": 900.0,
            "Cost": 540.0,
            "QuantityShipped": 11.0,
            "WeightLb": 44.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 44.0,
            "pack_count": 1,
            "Price": 900.0,
            "CostPrice": 540.0,
        },
        {
            "Date": "2025-04-12",
            "DateExpected": "2025-04-12",
            "RegionName": "Sparse Region",
            "OrderId": "SR-1",
            "CustomerId": "C_SPARSE",
            "CustomerName": "Sparse Customer",
            "ProductId": "P7",
            "ProductName": "Flat Iron",
            "ShippingMethodName": "Ground",
            "SupplierId": "SUP-7",
            "SupplierName": "Supplier Seven",
            "OrderStatus": "packed",
            "Revenue": 125.0,
            "Cost": 70.0,
            "QuantityShipped": 2.0,
            "WeightLb": 8.0,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 8.0,
            "pack_count": 1,
            "Price": 125.0,
            "CostPrice": 70.0,
        },
    ]

    frame = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_regions_drilldown_v2.parquet"
    frame.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "regions-drilldown-v2-admin",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


@pytest.fixture(autouse=True)
def _relax_region_authz(monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setattr("app.core.access_policy.enforce_entity_access", lambda *_args, **_kwargs: None)


def test_region_drilldown_v2_template_flag_on_off(app_client, seed_regions_drilldown_v2):
    app_client.application.config.update(REGIONS_V2=True, REGION_DRILLDOWN_V2=True)
    resp_v2 = app_client.get("/regions/Vancouver%20W", query_string={"start": "2025-04-01", "end": "2025-05-01"})
    assert resp_v2.status_code == 200
    body_v2 = resp_v2.get_data(as_text=True)
    assert "Regional Diagnostics Workspace" in body_v2
    assert "js/regions_drilldown_v2.js" in body_v2

    app_client.application.config.update(REGIONS_V2=True, REGION_DRILLDOWN_V2=False)
    resp_v1 = app_client.get("/regions/Vancouver%20W", query_string={"start": "2025-04-01", "end": "2025-05-01"})
    assert resp_v1.status_code == 200
    body_v1 = resp_v1.get_data(as_text=True)
    assert "Regional Diagnostics Workspace" not in body_v1
    assert "Monthly Revenue" in body_v1


def test_region_drilldown_v2_requires_both_flags(app_client, seed_regions_drilldown_v2):
    app_client.application.config.update(REGIONS_V2=False, REGION_DRILLDOWN_V2=True)
    resp = app_client.get("/regions/Vancouver%20W", query_string={"start": "2025-04-01", "end": "2025-05-01"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Regional Diagnostics Workspace" not in body
    assert "Monthly Revenue" in body


def test_region_drilldown_customers_export_not_limited_to_top_n(app_client, seed_regions_drilldown_v2):
    resp = app_client.get(
        "/regions/Vancouver%20W/export",
        query_string={
            "dataset": "customers",
            "format": "csv",
            "top_n": 2,
            "start": "2025-04-01",
            "end": "2025-05-01",
        },
    )
    assert resp.status_code == 200
    frame = pd.read_csv(io.StringIO(resp.get_data(as_text=True)))
    assert {"customer_id", "customer_name", "revenue_current", "risk_level"}.issubset(frame.columns)
    assert set(frame["customer_id"]) == {"C_KEEPER", "C_GROW", "C_NEW", "C_REPEAT", "C_PRIOR_ONLY"}


def test_region_drilldown_churn_export_handles_zero_rows(app_client, seed_regions_drilldown_v2):
    resp = app_client.get(
        "/regions/Fresh%20Region/churn_download",
        query_string={"format": "xlsx", "start": "2025-04-01", "end": "2025-05-01"},
    )
    assert resp.status_code == 200
    if resp.headers.get("X-Export-Fallback") == "csv" or "text/csv" in (resp.headers.get("Content-Type") or ""):
        assert resp.get_data(as_text=True).strip() == ""
    else:
        frame = pd.read_excel(io.BytesIO(resp.get_data()), sheet_name="ChurnedCustomers")
        assert frame.empty


def test_region_drilldown_bundle_sparse_region_safe_zero_base(app_client, seed_regions_drilldown_v2):
    resp = app_client.get(
        "/api/regions/drilldown/bundle",
        query_string={
            "region_id": "Sparse Region",
            "start": "2025-04-01",
            "end": "2025-05-01",
            "drilldown_v2": "1",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    scorecard = ((payload.get("region_v2") or {}).get("scorecard") or {})
    customer_rows = (((payload.get("region_v2") or {}).get("customers") or {}).get("top_rows") or [])
    assert scorecard.get("revenue_delta_window") == 125.0
    assert scorecard.get("revenue_delta_window_pct") is None
    assert scorecard.get("yoy_growth") is None
    assert customer_rows
    assert customer_rows[0]["delta_revenue_status"] == "new"
    assert customer_rows[0]["delta_revenue_label"] == "New"

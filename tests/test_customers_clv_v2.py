from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_customers_clv_v2(tmp_path, monkeypatch):
    rows = [
        # Whale profile
        {"Date": "2025-03-03", "DateExpected": "2025-03-03", "OrderId": "O-W1", "CustomerId": "C_WHALE", "CustomerName": "Whale Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 900.0, "Cost": 540.0, "QuantityOrdered": 40.0, "WeightLb": 80.0, "UnitOfBillingId": 1, "pack_item_count_sum": 40.0, "pack_weight_lb_sum": 80.0, "pack_count": 1, "Price": 900.0, "CostPrice": 540.0},
        {"Date": "2025-03-11", "DateExpected": "2025-03-11", "OrderId": "O-W2", "CustomerId": "C_WHALE", "CustomerName": "Whale Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 850.0, "Cost": 510.0, "QuantityOrdered": 38.0, "WeightLb": 76.0, "UnitOfBillingId": 1, "pack_item_count_sum": 38.0, "pack_weight_lb_sum": 76.0, "pack_count": 1, "Price": 850.0, "CostPrice": 510.0},
        {"Date": "2025-03-21", "DateExpected": "2025-03-21", "OrderId": "O-W3", "CustomerId": "C_WHALE", "CustomerName": "Whale Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 920.0, "Cost": 552.0, "QuantityOrdered": 42.0, "WeightLb": 84.0, "UnitOfBillingId": 1, "pack_item_count_sum": 42.0, "pack_weight_lb_sum": 84.0, "pack_count": 1, "Price": 920.0, "CostPrice": 552.0},
        # High value
        {"Date": "2025-03-08", "DateExpected": "2025-03-08", "OrderId": "O-H1", "CustomerId": "C_HIGH", "CustomerName": "High Value Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 600.0, "Cost": 390.0, "QuantityOrdered": 24.0, "WeightLb": 48.0, "UnitOfBillingId": 1, "pack_item_count_sum": 24.0, "pack_weight_lb_sum": 48.0, "pack_count": 1, "Price": 600.0, "CostPrice": 390.0},
        {"Date": "2025-03-24", "DateExpected": "2025-03-24", "OrderId": "O-H2", "CustomerId": "C_HIGH", "CustomerName": "High Value Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 580.0, "Cost": 377.0, "QuantityOrdered": 22.0, "WeightLb": 44.0, "UnitOfBillingId": 1, "pack_item_count_sum": 22.0, "pack_weight_lb_sum": 44.0, "pack_count": 1, "Price": 580.0, "CostPrice": 377.0},
        # Growth profile
        {"Date": "2025-03-09", "DateExpected": "2025-03-09", "OrderId": "O-G1", "CustomerId": "C_GROW", "CustomerName": "Growth Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 220.0, "Cost": 140.0, "QuantityOrdered": 12.0, "WeightLb": 24.0, "UnitOfBillingId": 1, "pack_item_count_sum": 12.0, "pack_weight_lb_sum": 24.0, "pack_count": 1, "Price": 220.0, "CostPrice": 140.0},
        {"Date": "2025-03-29", "DateExpected": "2025-03-29", "OrderId": "O-G2", "CustomerId": "C_GROW", "CustomerName": "Growth Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 390.0, "Cost": 250.0, "QuantityOrdered": 20.0, "WeightLb": 40.0, "UnitOfBillingId": 1, "pack_item_count_sum": 20.0, "pack_weight_lb_sum": 40.0, "pack_count": 1, "Price": 390.0, "CostPrice": 250.0},
        # At-risk high value candidate (single high order early in month)
        {"Date": "2025-03-01", "DateExpected": "2025-03-01", "OrderId": "O-R1", "CustomerId": "C_RISK", "CustomerName": "Risky Value Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 780.0, "Cost": 520.0, "QuantityOrdered": 30.0, "WeightLb": 60.0, "UnitOfBillingId": 1, "pack_item_count_sum": 30.0, "pack_weight_lb_sum": 60.0, "pack_count": 1, "Price": 780.0, "CostPrice": 520.0},
        # Low value
        {"Date": "2025-03-18", "DateExpected": "2025-03-18", "OrderId": "O-L1", "CustomerId": "C_LOW", "CustomerName": "Low Value Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 70.0, "Cost": 54.0, "QuantityOrdered": 5.0, "WeightLb": 10.0, "UnitOfBillingId": 1, "pack_item_count_sum": 5.0, "pack_weight_lb_sum": 10.0, "pack_count": 1, "Price": 70.0, "CostPrice": 54.0},
        # Prior-window history for delta/repeat behavior
        {"Date": "2025-02-10", "DateExpected": "2025-02-10", "OrderId": "O-G-PRIOR", "CustomerId": "C_GROW", "CustomerName": "Growth Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 110.0, "Cost": 70.0, "QuantityOrdered": 6.0, "WeightLb": 12.0, "UnitOfBillingId": 1, "pack_item_count_sum": 6.0, "pack_weight_lb_sum": 12.0, "pack_count": 1, "Price": 110.0, "CostPrice": 70.0},
        {"Date": "2025-02-12", "DateExpected": "2025-02-12", "OrderId": "O-H-PRIOR", "CustomerId": "C_HIGH", "CustomerName": "High Value Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 400.0, "Cost": 260.0, "QuantityOrdered": 16.0, "WeightLb": 32.0, "UnitOfBillingId": 1, "pack_item_count_sum": 16.0, "pack_weight_lb_sum": 32.0, "pack_count": 1, "Price": 400.0, "CostPrice": 260.0},
        # Out-of-scope customer
        {"Date": "2025-03-20", "DateExpected": "2025-03-20", "OrderId": "O-O1", "CustomerId": "C_OTHER", "CustomerName": "Other Scope Co", "SalesRepId": "R2", "SalesRepName": "Rep Two", "OrderStatus": "packed", "Revenue": 900.0, "Cost": 600.0, "QuantityOrdered": 45.0, "WeightLb": 90.0, "UnitOfBillingId": 1, "pack_item_count_sum": 45.0, "pack_weight_lb_sum": 90.0, "pack_count": 1, "Price": 900.0, "CostPrice": 600.0},
    ]

    frame = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_customers_clv_v2.parquet"
    frame.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _scope_sales(rep_ids: list[str]):
    return {
        "is_admin": False,
        "scope_mode": "list",
        "allowed_erp_user_ids": rep_ids,
        "sales_rep_ids": rep_ids,
        "allowed_count": len(rep_ids),
        "scope_hash": "scope-sales-clv",
        "permissions_version": "1",
        "user_id": 702,
        "role": "sales",
    }


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin-clv",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def _csv_frame(resp) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(resp.get_data(as_text=True)))


def _base_query():
    return {
        "start": "2025-03-01",
        "end": "2025-03-31",
        "clv_lookback_months": 12,
        "clv_horizon_months": 12,
        "clv_discount_rate": 8,
        "clv_monetary_basis": "gross_profit",
        "clv_retention_model": "simple",
        "clv_page_size": 100,
    }


def test_customers_clv_v2_page_renders_when_flag_on(app_client, seed_customers_clv_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_CLV_V2", True)

    resp = app_client.get("/customers/clv", query_string=_base_query())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "CLV Explained" in body
    assert "Segment Leaderboard" in body
    assert "Customers in Selection" in body


def test_customers_clv_v2_flag_off_uses_legacy_view(app_client, seed_customers_clv_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_CLV_V2", False)

    resp = app_client.get("/customers/clv", query_string=_base_query())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Top 10 High-Value Customers by CLV" in body
    assert "CLV Explained" not in body


def test_customers_clv_v2_rbac_scope_and_export(app_client, seed_customers_clv_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_sales(["r1"]))
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_CLV_V2", True)

    bundle_resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert bundle_resp.status_code == 200
    payload = bundle_resp.get_json() or {}
    rows = ((payload.get("clv") or {}).get("customers_table") or {}).get("rows") or []
    ids = {str(row.get("customer_id")) for row in rows}
    assert "C_OTHER" not in ids

    export_resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "clv", "dataset": "customers", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    if not export_df.empty:
        assert "C_OTHER" not in set(export_df["customer_id"].astype(str).tolist())


def test_customers_clv_v2_export_parity_matches_filtered_rows(app_client, seed_customers_clv_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_CLV_V2", True)

    query = {
        **_base_query(),
        "clv_search": "Co",
        "clv_low_margin_only": "1",
    }
    bundle_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert bundle_resp.status_code == 200
    payload = bundle_resp.get_json() or {}
    total_rows = int((((payload.get("clv") or {}).get("customers_table") or {}).get("total_rows") or 0))

    export_resp = app_client.get(
        "/customers/export",
        query_string={**query, "page": "clv", "dataset": "customers", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert len(export_df.index) == total_rows


def test_customers_clv_v2_settings_change_output_deterministically(app_client, seed_customers_clv_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_CLV_V2", True)

    base_resp = app_client.get("/api/customers/bundle", query_string={**_base_query(), "clv_horizon_months": 6})
    assert base_resp.status_code == 200
    base_payload = base_resp.get_json() or {}
    base_rows = ((base_payload.get("clv") or {}).get("customers_table") or {}).get("rows") or []
    base_by_id = {str(row.get("customer_id")): row for row in base_rows}
    base_cache_key = (base_payload.get("meta") or {}).get("cache_key")

    longer_resp = app_client.get("/api/customers/bundle", query_string={**_base_query(), "clv_horizon_months": 24})
    assert longer_resp.status_code == 200
    longer_payload = longer_resp.get_json() or {}
    longer_rows = ((longer_payload.get("clv") or {}).get("customers_table") or {}).get("rows") or []
    longer_by_id = {str(row.get("customer_id")): row for row in longer_rows}
    longer_cache_key = (longer_payload.get("meta") or {}).get("cache_key")

    assert float(longer_by_id["C_WHALE"]["clv_selected"]) > float(base_by_id["C_WHALE"]["clv_selected"])
    assert base_cache_key != longer_cache_key


def test_customers_clv_v2_non_negative_values(app_client, seed_customers_clv_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_CLV_V2", True)

    resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    rows = ((payload.get("clv") or {}).get("customers_table") or {}).get("rows") or []

    assert rows
    for row in rows:
        assert float(row.get("clv_12m") or 0.0) >= 0.0
        assert float(row.get("clv_at_risk") or 0.0) >= 0.0

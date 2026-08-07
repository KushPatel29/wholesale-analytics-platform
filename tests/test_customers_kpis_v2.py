from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_customers_kpis_v2(tmp_path, monkeypatch):
    rows = [
        # Returning customer (prior + current)
        {
            "Date": "2025-02-10",
            "DateExpected": "2025-02-10",
            "OrderId": "O-RET-PRIOR",
            "CustomerId": "C_RET",
            "CustomerName": "Returning Co",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 100.0,
            "Cost": 65.0,
            "QuantityOrdered": 10.0,
            "WeightLb": 20.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 10.0,
            "pack_weight_lb_sum": 20.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 65.0,
        },
        {
            "Date": "2025-03-10",
            "DateExpected": "2025-03-10",
            "OrderId": "O-RET-CUR",
            "CustomerId": "C_RET",
            "CustomerName": "Returning Co",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 120.0,
            "Cost": 72.0,
            "QuantityOrdered": 12.0,
            "WeightLb": 24.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 12.0,
            "pack_weight_lb_sum": 24.0,
            "pack_count": 1,
            "Price": 120.0,
            "CostPrice": 72.0,
        },
        # Lost customer (prior only)
        {
            "Date": "2025-02-15",
            "DateExpected": "2025-02-15",
            "OrderId": "O-LOST-PRIOR",
            "CustomerId": "C_LOST",
            "CustomerName": "Lost Co",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 80.0,
            "Cost": 56.0,
            "QuantityOrdered": 8.0,
            "WeightLb": 16.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 8.0,
            "pack_weight_lb_sum": 16.0,
            "pack_count": 1,
            "Price": 80.0,
            "CostPrice": 56.0,
        },
        # New customer (current only)
        {
            "Date": "2025-03-20",
            "DateExpected": "2025-03-20",
            "OrderId": "O-NEW-CUR",
            "CustomerId": "C_NEW",
            "CustomerName": "New Co",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 60.0,
            "Cost": 39.0,
            "QuantityOrdered": 6.0,
            "WeightLb": 12.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 12.0,
            "pack_count": 1,
            "Price": 60.0,
            "CostPrice": 39.0,
        },
        # Reactivated customer (old history + current, no prior-window order)
        {
            "Date": "2025-01-12",
            "DateExpected": "2025-01-12",
            "OrderId": "O-REACT-OLD",
            "CustomerId": "C_REACT",
            "CustomerName": "Reactivated Co",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 40.0,
            "Cost": 25.0,
            "QuantityOrdered": 4.0,
            "WeightLb": 8.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 4.0,
            "pack_weight_lb_sum": 8.0,
            "pack_count": 1,
            "Price": 40.0,
            "CostPrice": 25.0,
        },
        {
            "Date": "2025-03-22",
            "DateExpected": "2025-03-22",
            "OrderId": "O-REACT-CUR",
            "CustomerId": "C_REACT",
            "CustomerName": "Reactivated Co",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 70.0,
            "Cost": 44.0,
            "QuantityOrdered": 7.0,
            "WeightLb": 14.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 7.0,
            "pack_weight_lb_sum": 14.0,
            "pack_count": 1,
            "Price": 70.0,
            "CostPrice": 44.0,
        },
        # Out-of-scope customer for RBAC validation
        {
            "Date": "2025-03-08",
            "DateExpected": "2025-03-08",
            "OrderId": "O-OTHER-CUR",
            "CustomerId": "C_OTHER",
            "CustomerName": "Other Scope Co",
            "SalesRepId": "R2",
            "SalesRepName": "Rep Two",
            "OrderStatus": "packed",
            "Revenue": 200.0,
            "Cost": 120.0,
            "QuantityOrdered": 20.0,
            "WeightLb": 40.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 20.0,
            "pack_weight_lb_sum": 40.0,
            "pack_count": 1,
            "Price": 200.0,
            "CostPrice": 120.0,
        },
    ]

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_customers_kpis_v2.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _csv_frame(resp) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(resp.get_data(as_text=True)))


def _scope_sales(rep_ids: list[str]):
    return {
        "is_admin": False,
        "scope_mode": "list",
        "allowed_erp_user_ids": rep_ids,
        "sales_rep_ids": rep_ids,
        "allowed_count": len(rep_ids),
        "scope_hash": "scope-sales",
        "permissions_version": "1",
        "user_id": 700,
        "role": "sales",
    }


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def test_customers_kpis_v2_rbac_scope_table_and_export(app_client, seed_customers_kpis_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_sales(["r1"]))
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_KPIS_V2", True)

    query = {"start": "2025-03-01", "end": "2025-03-31", "page_size": 100, "export_all": "1"}
    bundle_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert bundle_resp.status_code == 200
    bundle_rows = (bundle_resp.get_json() or {}).get("table", {}).get("rows", [])
    bundle_ids = {str(row.get("customer_id")) for row in bundle_rows}
    assert "C_OTHER" not in bundle_ids
    assert {"C_RET", "C_NEW", "C_REACT", "C_LOST"}.issubset(bundle_ids)

    export_resp = app_client.get(
        "/customers/export",
        query_string={**query, "page": "kpis", "dataset": "table", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert "C_OTHER" not in set(export_df["customer_id"].astype(str).tolist())


def test_customers_kpis_v2_export_parity_matches_filtered_rows(app_client, seed_customers_kpis_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_KPIS_V2", True)

    query = {
        "start": "2025-03-01",
        "end": "2025-03-31",
        "quick_filter": "all",
        "search": "Co",
        "page_size": 25,
    }
    bundle_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert bundle_resp.status_code == 200
    payload = bundle_resp.get_json() or {}
    table = payload.get("table", {}) or {}
    filtered_count = int(table.get("total_rows") or 0)

    export_resp = app_client.get(
        "/customers/export",
        query_string={**query, "page": "kpis", "dataset": "table", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert len(export_df.index) == filtered_count


def test_customers_kpis_v2_segment_logic_new_and_lost(app_client, seed_customers_kpis_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    query = {"start": "2025-03-01", "end": "2025-03-31", "page_size": 100, "export_all": "1"}
    bundle_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert bundle_resp.status_code == 200
    payload = bundle_resp.get_json() or {}
    rows = (payload.get("table") or {}).get("rows") or []
    by_customer = {str(row.get("customer_id")): row for row in rows}

    assert by_customer["C_NEW"]["segment_label"] == "New"
    assert by_customer["C_LOST"]["segment_label"] == "Churned"

    movers = (payload.get("drivers") or {}).get("movers") or []
    movers_by_customer = {str(row.get("customer_id")): row for row in movers}
    assert movers_by_customer["C_NEW"]["status"] == "New"
    assert movers_by_customer["C_LOST"]["status"] == "Lost"


def test_customers_kpis_v2_nrr_grr_sanity_bounds(app_client, seed_customers_kpis_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    query = {"start": "2025-03-01", "end": "2025-03-31", "page_size": 100}
    bundle_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert bundle_resp.status_code == 200
    kpis = (bundle_resp.get_json() or {}).get("kpis", {}) or {}
    nrr = float(kpis.get("nrr") or 0.0)
    grr = float(kpis.get("grr") or 0.0)

    assert nrr >= 0.0
    assert grr >= 0.0
    assert grr <= 1.0 + 1e-9


def test_customers_kpis_v2_page_renders_with_flag(app_client, seed_customers_kpis_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_KPIS_V2", True)
    resp = app_client.get("/customers/", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Customer Health" in body
    assert "Revenue by Customer Segment" in body


def test_customers_kpis_v2_flag_off_uses_legacy_template(app_client, seed_customers_kpis_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_KPIS_V2", False)
    resp = app_client.get("/customers/", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "All Customers" in body
    assert "Customer Health" not in body

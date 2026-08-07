from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_customers_rfm_v2(tmp_path, monkeypatch):
    rows = [
        # Champions candidate (high R/F/M)
        {"Date": "2024-11-20", "DateExpected": "2024-11-20", "OrderId": "O-C1", "CustomerId": "C_CHAMP", "CustomerName": "Champion Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 120.0, "Cost": 75.0, "QuantityOrdered": 10.0, "WeightLb": 20.0, "UnitOfBillingId": 1, "pack_item_count_sum": 10.0, "pack_weight_lb_sum": 20.0, "pack_count": 1, "Price": 120.0, "CostPrice": 75.0},
        {"Date": "2024-12-15", "DateExpected": "2024-12-15", "OrderId": "O-C2", "CustomerId": "C_CHAMP", "CustomerName": "Champion Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 120.0, "Cost": 75.0, "QuantityOrdered": 10.0, "WeightLb": 20.0, "UnitOfBillingId": 1, "pack_item_count_sum": 10.0, "pack_weight_lb_sum": 20.0, "pack_count": 1, "Price": 120.0, "CostPrice": 75.0},
        {"Date": "2025-01-20", "DateExpected": "2025-01-20", "OrderId": "O-C3", "CustomerId": "C_CHAMP", "CustomerName": "Champion Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 120.0, "Cost": 75.0, "QuantityOrdered": 10.0, "WeightLb": 20.0, "UnitOfBillingId": 1, "pack_item_count_sum": 10.0, "pack_weight_lb_sum": 20.0, "pack_count": 1, "Price": 120.0, "CostPrice": 75.0},
        {"Date": "2025-02-20", "DateExpected": "2025-02-20", "OrderId": "O-C4", "CustomerId": "C_CHAMP", "CustomerName": "Champion Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 120.0, "Cost": 75.0, "QuantityOrdered": 10.0, "WeightLb": 20.0, "UnitOfBillingId": 1, "pack_item_count_sum": 10.0, "pack_weight_lb_sum": 20.0, "pack_count": 1, "Price": 120.0, "CostPrice": 75.0},
        {"Date": "2025-03-25", "DateExpected": "2025-03-25", "OrderId": "O-C5", "CustomerId": "C_CHAMP", "CustomerName": "Champion Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 120.0, "Cost": 75.0, "QuantityOrdered": 10.0, "WeightLb": 20.0, "UnitOfBillingId": 1, "pack_item_count_sum": 10.0, "pack_weight_lb_sum": 20.0, "pack_count": 1, "Price": 120.0, "CostPrice": 75.0},
        # Prior window history for champion
        {"Date": "2023-08-10", "DateExpected": "2023-08-10", "OrderId": "O-C-PRIOR", "CustomerId": "C_CHAMP", "CustomerName": "Champion Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 300.0, "Cost": 180.0, "QuantityOrdered": 15.0, "WeightLb": 30.0, "UnitOfBillingId": 1, "pack_item_count_sum": 15.0, "pack_weight_lb_sum": 30.0, "pack_count": 1, "Price": 300.0, "CostPrice": 180.0},
        # New customer
        {"Date": "2025-03-28", "DateExpected": "2025-03-28", "OrderId": "O-N1", "CustomerId": "C_NEW", "CustomerName": "New Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 150.0, "Cost": 90.0, "QuantityOrdered": 6.0, "WeightLb": 12.0, "UnitOfBillingId": 1, "pack_item_count_sum": 6.0, "pack_weight_lb_sum": 12.0, "pack_count": 1, "Price": 150.0, "CostPrice": 90.0},
        # Can't lose / at risk profile
        {"Date": "2024-04-05", "DateExpected": "2024-04-05", "OrderId": "O-A1", "CustomerId": "C_ATRISK", "CustomerName": "At Risk Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 125.0, "Cost": 80.0, "QuantityOrdered": 8.0, "WeightLb": 16.0, "UnitOfBillingId": 1, "pack_item_count_sum": 8.0, "pack_weight_lb_sum": 16.0, "pack_count": 1, "Price": 125.0, "CostPrice": 80.0},
        {"Date": "2024-06-10", "DateExpected": "2024-06-10", "OrderId": "O-A2", "CustomerId": "C_ATRISK", "CustomerName": "At Risk Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 125.0, "Cost": 80.0, "QuantityOrdered": 8.0, "WeightLb": 16.0, "UnitOfBillingId": 1, "pack_item_count_sum": 8.0, "pack_weight_lb_sum": 16.0, "pack_count": 1, "Price": 125.0, "CostPrice": 80.0},
        {"Date": "2024-08-01", "DateExpected": "2024-08-01", "OrderId": "O-A3", "CustomerId": "C_ATRISK", "CustomerName": "At Risk Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 125.0, "Cost": 80.0, "QuantityOrdered": 8.0, "WeightLb": 16.0, "UnitOfBillingId": 1, "pack_item_count_sum": 8.0, "pack_weight_lb_sum": 16.0, "pack_count": 1, "Price": 125.0, "CostPrice": 80.0},
        {"Date": "2024-09-01", "DateExpected": "2024-09-01", "OrderId": "O-A4", "CustomerId": "C_ATRISK", "CustomerName": "At Risk Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 125.0, "Cost": 80.0, "QuantityOrdered": 8.0, "WeightLb": 16.0, "UnitOfBillingId": 1, "pack_item_count_sum": 8.0, "pack_weight_lb_sum": 16.0, "pack_count": 1, "Price": 125.0, "CostPrice": 80.0},
        # Prior window history for at risk stake
        {"Date": "2023-10-15", "DateExpected": "2023-10-15", "OrderId": "O-A-PRIOR", "CustomerId": "C_ATRISK", "CustomerName": "At Risk Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 700.0, "Cost": 420.0, "QuantityOrdered": 25.0, "WeightLb": 50.0, "UnitOfBillingId": 1, "pack_item_count_sum": 25.0, "pack_weight_lb_sum": 50.0, "pack_count": 1, "Price": 700.0, "CostPrice": 420.0},
        # Lost customer profile
        {"Date": "2024-10-01", "DateExpected": "2024-10-01", "OrderId": "O-L1", "CustomerId": "C_SLEEP", "CustomerName": "Sleep Co", "SalesRepId": "R1", "SalesRepName": "Rep One", "OrderStatus": "packed", "Revenue": 80.0, "Cost": 50.0, "QuantityOrdered": 4.0, "WeightLb": 8.0, "UnitOfBillingId": 1, "pack_item_count_sum": 4.0, "pack_weight_lb_sum": 8.0, "pack_count": 1, "Price": 80.0, "CostPrice": 50.0},
        # Out of scope customer
        {"Date": "2025-03-10", "DateExpected": "2025-03-10", "OrderId": "O-O1", "CustomerId": "C_OTHER", "CustomerName": "Other Scope Co", "SalesRepId": "R2", "SalesRepName": "Rep Two", "OrderStatus": "packed", "Revenue": 250.0, "Cost": 160.0, "QuantityOrdered": 12.0, "WeightLb": 24.0, "UnitOfBillingId": 1, "pack_item_count_sum": 12.0, "pack_weight_lb_sum": 24.0, "pack_count": 1, "Price": 250.0, "CostPrice": 160.0},
    ]

    frame = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_customers_rfm_v2.parquet"
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
        "scope_hash": "scope-sales-rfm",
        "permissions_version": "1",
        "user_id": 701,
        "role": "sales",
    }


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin-rfm",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def _csv_frame(resp) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(resp.get_data(as_text=True)))


def _base_query():
    return {
        "start": "2024-01-01",
        "end": "2025-03-31",
        "rfm_lookback_months": 12,
        "rfm_scoring_method": "fixed",
        "rfm_monetary_metric": "revenue",
        "rfm_recency_thresholds": "30,60,90,120",
        "rfm_frequency_thresholds": "1,2,3,4",
        "rfm_monetary_thresholds": "100,200,300,400",
        "rfm_page_size": 200,
    }


def test_customers_rfm_v2_fixed_scoring_stable(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    rows = ((payload.get("rfm") or {}).get("customers_table") or {}).get("rows") or []
    by_customer = {str(row.get("customer_id")): row for row in rows}

    champ = by_customer["C_CHAMP"]
    assert champ["r_score"] == 5
    assert champ["f_score"] == 5
    assert champ["m_score"] == 5
    assert champ["rfm_score"] == 15


def test_customers_rfm_v2_segment_assignment_deterministic(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    rows = ((payload.get("rfm") or {}).get("customers_table") or {}).get("rows") or []
    by_customer = {str(row.get("customer_id")): row for row in rows}

    assert by_customer["C_CHAMP"]["segment"] == "Champions"
    assert by_customer["C_NEW"]["segment"] == "New Customers"
    assert by_customer["C_ATRISK"]["segment"] == "Can't Lose Them"
    assert by_customer["C_SLEEP"]["segment"] == "Can't Lose Them"


def test_customers_rfm_v2_rbac_scope_and_export(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_sales(["r1"]))
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    rows = ((payload.get("rfm") or {}).get("customers_table") or {}).get("rows") or []
    ids = {str(row.get("customer_id")) for row in rows}
    assert "C_OTHER" not in ids

    export_resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "rfm", "dataset": "customers_full", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert "C_OTHER" not in set(export_df["customer_id"].astype(str).tolist())


def test_customers_rfm_v2_export_parity_matches_filtered_rows(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    query = {
        **_base_query(),
        "rfm_segments": "Champions,New Customers",
        "rfm_search": "Co",
    }
    resp = app_client.get("/api/customers/bundle", query_string=query)
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    total_rows = int((((payload.get("rfm") or {}).get("customers_table") or {}).get("total_rows") or 0))

    export_resp = app_client.get(
        "/customers/export",
        query_string={**query, "page": "rfm", "dataset": "customers_full", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert len(export_df.index) == total_rows


def test_customers_rfm_v2_heatmap_cell_filter_applies_correct_rows(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    matrix_rows = (((payload.get("rfm") or {}).get("matrix") or {}).get("rows") or [])

    selected = None
    for row in matrix_rows:
        for cell in row.get("cells") or []:
            if int(cell.get("customers") or 0) > 0:
                selected = cell
                break
        if selected:
            break
    assert selected is not None

    query = {**_base_query(), "heat_r": selected["r_score"], "heat_f": selected["f_score"]}
    cell_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert cell_resp.status_code == 200
    cell_payload = cell_resp.get_json() or {}
    table = ((cell_payload.get("rfm") or {}).get("customers_table") or {})
    rows = table.get("rows") or []
    assert int(table.get("total_rows") or 0) == int(selected.get("customers") or 0)
    assert all(int(row.get("r_score") or 0) == int(selected["r_score"]) for row in rows)
    assert all(int(row.get("f_score") or 0) == int(selected["f_score"]) for row in rows)


def test_customers_rfm_v2_alias_export_heatmap_matches_filtered_rows(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    base_resp = app_client.get("/api/customers/bundle", query_string=_base_query())
    assert base_resp.status_code == 200
    base_payload = base_resp.get_json() or {}
    matrix_rows = (((base_payload.get("rfm") or {}).get("matrix") or {}).get("rows") or [])
    selected = None
    for row in matrix_rows:
        for cell in row.get("cells") or []:
            if int(cell.get("customers") or 0) > 0:
                selected = cell
                break
        if selected:
            break
    assert selected is not None

    filtered_query = {**_base_query(), "heat_r": selected["r_score"], "heat_f": selected["f_score"]}
    filtered_resp = app_client.get("/api/customers/bundle", query_string=filtered_query)
    assert filtered_resp.status_code == 200
    filtered_payload = filtered_resp.get_json() or {}
    expected_rows = int((((filtered_payload.get("rfm") or {}).get("customers_table") or {}).get("total_rows") or 0))

    export_resp = app_client.get(
        "/customers/rfm/export",
        query_string={**filtered_query, "type": "heatmap", "format": "csv"},
        follow_redirects=True,
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert len(export_df.index) == expected_rows


def test_customers_rfm_v2_supports_6_month_lookback(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    query = {**_base_query(), "rfm_lookback_months": 6}
    resp = app_client.get("/api/customers/bundle", query_string=query)
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    settings = ((payload.get("rfm") or {}).get("settings") or {})
    assert int(settings.get("lookback_months") or 0) == 6


def test_customers_rfm_v2_page_renders_when_flag_on(app_client, seed_customers_rfm_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_RFM_V2", True)

    resp = app_client.get("/customers/rfm", query_string=_base_query())
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "RFM Explained" in body
    assert "Segment Leaderboard" in body
    assert "R x F Segmentation Matrix (5x5)" in body
    assert body.count("customer-stable-chart-frame") >= 2
    assert "maintainAspectRatio: false" in body
    assert "resizeDelay: 160" in body

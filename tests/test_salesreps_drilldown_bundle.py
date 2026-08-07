import pandas as pd
import pytest

from app.services import fact_store, filters_service, salesreps_bundle


@pytest.fixture
def seed_salesreps_drilldown(tmp_path, monkeypatch):
    rows = []
    reps = [("R1", "Alex"), ("R2", "Bea")]
    order_idx = 1
    for rep_idx, (rep_id, rep_name) in enumerate(reps, start=1):
        for month in (6, 7, 8):
            for order_no in (1, 2):
                revenue = float(900 + rep_idx * 120 + month * 8 + order_no)
                cost = revenue * 0.65
                pack_units = 4 + order_no
                rows.append(
                    {
                        "Date": f"2025-{month:02d}-{order_no:02d}",
                        "DateExpected": f"2025-{month:02d}-{order_no:02d}",
                        "SalesRepId": rep_id,
                        "SalesRepName": rep_name,
                        "OrderId": f"D-{order_idx}",
                        "CustomerId": f"C-{rep_idx:02d}",
                        "CustomerName": f"Customer {rep_idx:02d}",
                        "ProductId": f"P-{rep_idx:02d}",
                        "ProductName": f"Product {rep_idx:02d}",
                        "OrderStatus": "packed",
                        "Revenue": revenue,
                        "Cost": cost,
                        "QuantityOrdered": pack_units,
                        "WeightLb": 8.0 + rep_idx,
                        "UnitOfBillingId": 1,
                        "pack_item_count_sum": float(pack_units),
                        "pack_weight_lb_sum": 8.0 + rep_idx,
                        "pack_count": 1,
                        "Price": revenue / pack_units,
                        "CostPrice": cost / pack_units,
                    }
                )
                order_idx += 1

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_salesreps_drilldown.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_drilldown_bundle_panels(app_client, seed_salesreps_drilldown):
    rep_id = "R1"
    resp = app_client.get(
        "/api/salesreps/drilldown/bundle",
        query_string={"salesrep_id": rep_id, "start": "2025-01-01", "end": "2025-12-31"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    for key in ("kpis", "trend", "charts", "table", "tables", "decomposition", "risk_flags", "insights", "meta"):
        assert key in payload

    meta = payload.get("meta", {})
    assert str(meta.get("entity_id")) == rep_id
    assert int(meta.get("duckdb_query_count", 0) or 0) <= 3

    kpis = payload.get("kpis", {})
    assert float(kpis.get("revenue") or 0.0) > 0
    assert int(kpis.get("orders") or 0) > 0

    charts = payload.get("charts", {})
    assert len(charts.get("top_customers") or []) > 0
    assert len(charts.get("top_products") or []) > 0
    assert isinstance(charts.get("concentration"), dict)

    trend = payload.get("trend", {})
    assert len((trend.get("monthly") or {}).get("labels") or []) > 0
    assert len((trend.get("weekly") or {}).get("labels") or []) > 0

    tables = payload.get("tables", {})
    assert len(tables.get("customers") or []) > 0
    assert len(tables.get("products") or []) > 0

    modules = payload.get("modules", {})
    assert isinstance(modules, dict)
    assert isinstance((modules.get("portfolio_map") or {}).get("customers"), list)
    assert len((modules.get("portfolio_map") or {}).get("customers") or []) > 0
    assert isinstance((modules.get("product_gap_matrix") or {}).get("columns"), list)
    assert isinstance((modules.get("product_gap_matrix") or {}).get("rows"), list)
    assert isinstance(modules.get("smart_notes"), list)

    customers_rev = sum(float(r.get("revenue") or 0.0) for r in (tables.get("customers") or []))
    assert customers_rev == pytest.approx(float(kpis.get("revenue") or 0.0), rel=0.001, abs=0.01)

    # Task 1B — lost_accounts key must be present in drilldown bundle
    assert "lost_accounts" in payload, "lost_accounts key missing from drilldown bundle"
    assert isinstance(payload["lost_accounts"], list)


def test_salesreps_custom_drilldown_bundle_route_serializes_payload(app_client, seed_salesreps_drilldown):
    resp = app_client.get(
        "/salesreps/api/drilldown/bundle",
        query_string={"salesrep_id": "R1", "start": "2025-01-01", "end": "2025-12-31"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    assert payload.get("meta", {}).get("entity_id") == "R1"
    assert isinstance((payload.get("table") or {}).get("rows"), list)


@pytest.fixture
def seed_salesreps_drilldown_ownership(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-10",
            "DateExpected": "2025-01-10",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "DR-OWN-1",
            "CustomerId": "C-OWN-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-OWN-01",
            "ProductName": "Ownership Product",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": 640.0,
            "QuantityOrdered": 8,
            "WeightLb": 18.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 8.0,
            "pack_weight_lb_sum": 18.0,
            "pack_count": 1,
            "Price": 125.0,
            "CostPrice": 80.0,
        },
        {
            "Date": "2025-03-05",
            "DateExpected": "2025-03-05",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "DR-OWN-2",
            "CustomerId": "C-OWN-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-OWN-02",
            "ProductName": "Ownership Product 2",
            "OrderStatus": "packed",
            "Revenue": 400.0,
            "Cost": 240.0,
            "QuantityOrdered": 4,
            "WeightLb": 9.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 4.0,
            "pack_weight_lb_sum": 9.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
    ]
    bridge = pd.DataFrame(
        [
            {
                "customer_id": "C-OWN-01",
                "rep_id": "R1",
                "rep_name": "Alex",
                "assignment_start_date": "2025-01-01",
                "assignment_end_date": "2025-03-31",
                "is_current": False,
                "rep_is_active": False,
            },
            {
                "customer_id": "C-OWN-01",
                "rep_id": "R2",
                "rep_name": "Bea",
                "assignment_start_date": "2025-04-01",
                "assignment_end_date": None,
                "is_current": True,
                "rep_is_active": True,
            },
        ]
    )

    parquet_path = tmp_path / "fact_salesreps_drilldown_ownership.parquet"
    bridge_path = tmp_path / "salesrep_customer_history.csv"
    pd.DataFrame(rows).to_parquet(parquet_path)
    bridge.to_csv(bridge_path, index=False)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.setenv("CUSTOMER_REP_HISTORY_PATH", str(bridge_path))
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_drilldown_separates_account_owner_from_last_sales_rep(
    seed_salesreps_drilldown_ownership,
):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_drilldown(
        "R2",
        filters,
        {"is_admin": True},
        {"attribution_mode": "current_owner", "roster_mode": "include_former"},
    )
    assert payload.get("meta", {}).get("attribution", {}).get("attribution_mode") == "current_owner"
    assert payload.get("meta", {}).get("ownership_bridge", {}).get("available") is True

    kpis = payload.get("kpis", {})
    assert int(kpis.get("current_owned_customers") or 0) == 1
    assert float(kpis.get("current_owner_revenue") or 0.0) == pytest.approx(1400.0, abs=0.01)

    customer_rows = payload.get("tables", {}).get("customers", [])
    customer = next((row for row in customer_rows if row.get("customer_id") == "C-OWN-01"), None)
    assert customer is not None
    assert customer.get("account_owner_name") == "Bea"
    assert customer.get("last_sales_rep_name") == "Alex"
    assert int(customer.get("inherited_flag") or 0) == 1
    assert customer.get("revenue_attribution_type") == "inherited"


@pytest.fixture
def seed_salesreps_drilldown_snapshot(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-10",
            "DateExpected": "2025-01-10",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "PrimarySalesRepId": "R2",
            "OrderId": "DR-SNAP-1",
            "CustomerId": "C-SNAP-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-SNAP-01",
            "ProductName": "Snapshot Product",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": 650.0,
            "QuantityOrdered": 8,
            "WeightLb": 18.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 8.0,
            "pack_weight_lb_sum": 18.0,
            "pack_count": 1,
            "Price": 125.0,
            "CostPrice": 81.25,
        },
        {
            "Date": "2025-03-05",
            "DateExpected": "2025-03-05",
            "SalesRepId": "R2",
            "SalesRepName": "Bea",
            "PrimarySalesRepId": "R2",
            "OrderId": "DR-SNAP-2",
            "CustomerId": "C-SNAP-02",
            "CustomerName": "Current Customer",
            "ProductId": "P-SNAP-02",
            "ProductName": "Snapshot Product 2",
            "OrderStatus": "packed",
            "Revenue": 400.0,
            "Cost": 240.0,
            "QuantityOrdered": 4,
            "WeightLb": 9.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 4.0,
            "pack_weight_lb_sum": 9.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
    ]

    parquet_path = tmp_path / "fact_salesreps_drilldown_snapshot.parquet"
    pd.DataFrame(rows).to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_drilldown_uses_fact_owner_snapshot_names(seed_salesreps_drilldown_snapshot):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_drilldown(
        "R2",
        filters,
        {"is_admin": True},
        {"attribution_mode": "current_owner", "roster_mode": "include_former"},
    )
    assert payload.get("meta", {}).get("ownership_snapshot", {}).get("available") is True
    assert payload.get("kpis", {}).get("rep_name") == "Bea"
    customer_rows = payload.get("tables", {}).get("customers", [])
    moved_customer = next((row for row in customer_rows if row.get("customer_id") == "C-SNAP-01"), None)
    assert moved_customer is not None
    assert moved_customer.get("account_owner_name") == "Bea"
    assert moved_customer.get("last_sales_rep_name") == "Alex"

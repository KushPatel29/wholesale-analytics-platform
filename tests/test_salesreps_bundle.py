import pandas as pd
import pytest

from app.services import fact_store, filters_service, salesreps_bundle


@pytest.fixture
def seed_salesreps_bundle(tmp_path, monkeypatch):
    rows = []
    reps = [("R1", "Alex"), ("R2", "Bea"), ("R3", "Carl")]
    order_idx = 1
    for rep_idx, (rep_id, rep_name) in enumerate(reps, start=1):
        for month in (6, 7, 8):
            for order_no in (1, 2):
                revenue = float(1000 + rep_idx * 100 + month * 10 + order_no)
                cost = revenue * 0.6
                rows.append(
                    {
                        "Date": f"2025-{month:02d}-{order_no:02d}",
                        "DateExpected": f"2025-{month:02d}-{order_no:02d}",
                        "SalesRepId": rep_id,
                        "SalesRepName": rep_name,
                        "OrderId": f"O-{order_idx}",
                        "CustomerId": f"C-{rep_idx:02d}",
                        "CustomerName": f"Customer {rep_idx:02d}",
                        "ProductId": f"P-{rep_idx:02d}",
                        "ProductName": f"Product {rep_idx:02d}",
                        "OrderStatus": "packed",
                        "Revenue": revenue,
                        "Cost": cost,
                        "QuantityOrdered": 5 + order_no,
                        "WeightLb": 10.0 + rep_idx,
                        "UnitOfBillingId": 1,
                        "pack_item_count_sum": 1.0,
                        "pack_weight_lb_sum": 10.0 + rep_idx,
                        "pack_count": 1,
                        "Price": revenue,
                        "CostPrice": cost,
                    }
                )
                order_idx += 1

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_salesreps.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_bundle_keys_and_budget(app_client, seed_salesreps_bundle):
    resp = app_client.get(
        "/api/salesreps/bundle",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "page_size": 10, "page": 1},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    for key in ("kpis", "trend", "charts", "table", "meta"):
        assert key in payload

    meta = payload.get("meta", {})
    assert meta.get("dataset_version") is not None
    assert int(meta.get("duckdb_query_count", 0) or 0) <= 4
    assert float((meta.get("packs_coverage") or {}).get("packs_coverage_pct") or 0.0) == pytest.approx(100.0, abs=0.01)

    kpis = payload.get("kpis", {})
    assert float(kpis.get("revenue") or 0.0) > 0
    assert int(kpis.get("orders") or 0) > 0
    assert int(kpis.get("customers") or 0) > 0
    assert float(kpis.get("packs_coverage_pct") or 0.0) == pytest.approx(100.0, abs=0.01)

    charts = payload.get("charts", {})
    assert len(charts.get("top_reps") or []) > 0
    assert len(charts.get("pareto") or []) > 0
    trend = charts.get("trend") or {}
    assert len(trend.get("labels") or []) > 0
    top_rep = (charts.get("top_reps") or [])[0]
    assert "direct_revenue" in top_rep
    assert "direct_profit" in top_rep
    assert "direct_weight_lb" in top_rep
    assert "direct_margin_pct" in top_rep

    table = payload.get("table", {})
    rows = table.get("rows") or []
    assert len(rows) > 0
    row = rows[0]
    assert row.get("rep_id") or row.get("rep_name")
    assert row.get("orders") is not None
    assert row.get("customers") is not None

    # Task 2A — Health Score must be present on each row
    assert "health_score" in row, "health_score missing from table row"
    assert "health_label" in row, "health_label missing from table row"
    assert isinstance(row.get("health_score"), int)

    # Task 2B — Quartile ranking must be present on each row
    assert "revenue_quartile" in row, "revenue_quartile missing from table row"
    assert "quartile_label" in row, "quartile_label missing from table row"

    # Task 2C — Benchmarks key must be present in payload
    assert "benchmarks" in payload, "benchmarks key missing from index bundle"
    benchmarks = payload["benchmarks"]
    for bench_key in ("avg_revenue", "avg_profit", "avg_margin_pct", "avg_asp_lb", "avg_orders", "avg_customers"):
        assert bench_key in benchmarks, f"benchmarks.{bench_key} missing"


@pytest.fixture
def seed_salesreps_ownership_bundle(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-15",
            "DateExpected": "2025-01-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "OWN-1",
            "CustomerId": "C-OWN-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-OWN-01",
            "ProductName": "Product Ownership",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": 600.0,
            "QuantityOrdered": 10,
            "WeightLb": 20.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 10.0,
            "pack_weight_lb_sum": 20.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
        {
            "Date": "2025-03-20",
            "DateExpected": "2025-03-20",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "OWN-2",
            "CustomerId": "C-OWN-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-OWN-02",
            "ProductName": "Product Ownership 2",
            "OrderStatus": "packed",
            "Revenue": 500.0,
            "Cost": 300.0,
            "QuantityOrdered": 6,
            "WeightLb": 12.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 12.0,
            "pack_count": 1,
            "Price": 500.0 / 6.0,
            "CostPrice": 50.0,
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

    parquet_path = tmp_path / "fact_salesreps_ownership.parquet"
    bridge_path = tmp_path / "customer_rep_history.csv"
    pd.DataFrame(rows).to_parquet(parquet_path)
    bridge.to_csv(bridge_path, index=False)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.setenv("CUSTOMER_REP_HISTORY_PATH", str(bridge_path))
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_bundle_supports_current_owner_rollup(seed_salesreps_ownership_bundle):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    scope = {"is_admin": True}
    historical_payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        scope,
        {"attribution_mode": "historical_rep", "roster_mode": "include_former", "page_size": "10"},
    )
    assert historical_payload.get("meta", {}).get("ownership_bridge", {}).get("available") is True
    historical_rows = historical_payload.get("table", {}).get("rows", [])
    hist_row = next((row for row in historical_rows if row.get("rep_id") == "R1"), None)
    assert hist_row is not None
    assert float(hist_row.get("revenue") or 0.0) == pytest.approx(1500.0, abs=0.01)

    current_payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        scope,
        {"attribution_mode": "current_owner", "roster_mode": "include_former", "page_size": "10"},
    )
    current_rows = current_payload.get("table", {}).get("rows", [])
    owner_row = next((row for row in current_rows if row.get("rep_id") == "R2"), None)
    assert owner_row is not None
    assert float(owner_row.get("revenue") or 0.0) == pytest.approx(1500.0, abs=0.01)
    assert float(owner_row.get("transferred_in_revenue") or 0.0) == pytest.approx(1500.0, abs=0.01)
    assert int(owner_row.get("current_owned_customers") or 0) == 1
    assert int(owner_row.get("replaced_rep_count") or 0) == 1
    assert "Alex" in str(owner_row.get("replaced_rep_names") or "")


def test_salesreps_bundle_defaults_to_current_owner_mode(seed_salesreps_ownership_bundle):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_bundle(filters, {"is_admin": True}, {"page_size": "10"})
    assert payload.get("meta", {}).get("attribution", {}).get("attribution_mode") == "current_owner"
    rows = payload.get("table", {}).get("rows", [])
    owner_row = next((row for row in rows if row.get("rep_id") == "R2"), None)
    assert owner_row is not None
    assert float(owner_row.get("revenue") or 0.0) == pytest.approx(1500.0, abs=0.01)


def test_analysis_sections_use_readable_owner_names(monkeypatch):
    rep_uuid = "82E82163-BE55-4D07-881F-F2E758172880"
    monkeypatch.setattr(
        salesreps_bundle,
        "_rep_directory",
        lambda: {rep_uuid: "Scott Switzer"},
    )

    analysis_df = pd.DataFrame(
        [
            {
                "dataset": "top_customer",
                "key": "C-1",
                "label": "Top Customer",
                "secondary_label": rep_uuid,
                "metric_1": 1000.0,
                "metric_2": 300.0,
                "metric_3": 180.0,
                "metric_4": 12.5,
                "metric_5": -8.0,
                "metric_6": 4.0,
                "metric_7": 600.0,
                "metric_8": 0.0,
                "metric_9": 220.0,
                "text_1": "Vancouver",
                "text_2": rep_uuid,
            },
            {
                "dataset": "customer_mover_up",
                "key": "C-2",
                "label": "Mover Up",
                "secondary_label": rep_uuid,
                "metric_1": 250.0,
                "metric_2": 900.0,
                "metric_3": 38.0,
                "metric_4": 700.0,
                "metric_5": 3.0,
                "text_1": "Burnaby",
                "text_2": rep_uuid,
            },
            {
                "dataset": "map_customer",
                "key": "C-3",
                "label": "Mapped Customer",
                "secondary_label": rep_uuid,
                "metric_1": 875.0,
                "metric_2": 210.0,
                "metric_3": 90.0,
                "metric_4": 11.2,
                "metric_5": 6.5,
                "metric_6": 3.0,
                "metric_7": 440.0,
                "metric_8": 180.0,
                "metric_9": 120.0,
                "text_1": "Victoria",
                "text_2": rep_uuid,
                "delivery_lat": 48.4284,
                "delivery_lng": -123.3656,
                "delivery_city": "Victoria",
                "delivery_province": "BC",
                "shipping_method": "Truck",
                "last_order_date": "2026-03-05",
                "days_since_order": 12,
            },
            {
                "dataset": "transfer_pair",
                "key": rep_uuid,
                "label": rep_uuid,
                "secondary_label": "Needs Mapping",
                "metric_1": 650.0,
                "metric_2": 2.0,
                "metric_3": None,
                "metric_4": None,
                "metric_5": None,
                "text_1": "Burnaby, Vancouver",
                "text_2": "2026-01-01 to 2026-03-31",
            },
        ]
    )

    sections = salesreps_bundle._build_analysis_sections(analysis_df, [])

    assert sections["top_customers"][0]["account_owner_id"] == rep_uuid
    assert sections["top_customers"][0]["account_owner_name"] == "Scott Switzer"
    assert sections["top_customers"][0]["yoy_delta_revenue"] == pytest.approx(180.0, abs=0.01)
    assert sections["top_customers"][0]["yoy_revenue_pct"] == pytest.approx(12.5, abs=0.01)
    assert sections["top_customers"][0]["mom_revenue_pct"] == pytest.approx(-8.0, abs=0.01)
    assert sections["top_customers"][0]["orders"] == pytest.approx(4.0, abs=0.01)
    assert sections["top_customers"][0]["beef_revenue"] == pytest.approx(600.0, abs=0.01)
    assert sections["top_customers"][0]["poultry_revenue"] == pytest.approx(0.0, abs=0.01)
    assert sections["map_customers"][0]["customer_name"] == "Mapped Customer"
    assert sections["map_customers"][0]["account_owner_name"] == "Scott Switzer"
    assert sections["map_customers"][0]["delivery_lat"] == pytest.approx(48.4284, abs=0.0001)
    assert sections["map_customers"][0]["delivery_lng"] == pytest.approx(-123.3656, abs=0.0001)
    assert sections["map_customers"][0]["shipping_method"] == "Truck"
    assert sections["customer_movers"]["up"][0]["account_owner_id"] == rep_uuid
    assert sections["customer_movers"]["up"][0]["account_owner_name"] == "Scott Switzer"
    assert sections["replacement_pairs"][0]["current_owner_key"] == rep_uuid
    assert sections["replacement_pairs"][0]["current_owner_name"] == "Scott Switzer"
    assert sections["replacement_pairs"][0]["prior_rep_name"] == "Needs Review"
    assert "Needs Mapping" not in str(sections["replacement_pairs"][0]["prior_rep_name"])


@pytest.fixture
def seed_salesreps_fact_owner_snapshot_bundle(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-15",
            "DateExpected": "2025-01-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "PrimarySalesRepId": "R2",
            "OrderId": "SNAP-1",
            "CustomerId": "C-SNAP-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-SNAP-01",
            "ProductName": "Ownership Product",
            "OrderStatus": "packed",
            "Revenue": 1000.0,
            "Cost": 600.0,
            "QuantityOrdered": 10,
            "WeightLb": 20.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 10.0,
            "pack_weight_lb_sum": 20.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
        {
            "Date": "2025-02-10",
            "DateExpected": "2025-02-10",
            "SalesRepId": "R2",
            "SalesRepName": "Bea",
            "PrimarySalesRepId": "R2",
            "OrderId": "SNAP-2",
            "CustomerId": "C-SNAP-02",
            "CustomerName": "Current Customer",
            "ProductId": "P-SNAP-02",
            "ProductName": "Current Product",
            "OrderStatus": "packed",
            "Revenue": 800.0,
            "Cost": 480.0,
            "QuantityOrdered": 8,
            "WeightLb": 16.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 8.0,
            "pack_weight_lb_sum": 16.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
    ]

    parquet_path = tmp_path / "fact_salesreps_snapshot.parquet"
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


def test_salesreps_bundle_uses_fact_owner_snapshot_before_fallback(seed_salesreps_fact_owner_snapshot_bundle):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        {"is_admin": True},
        {"attribution_mode": "current_owner", "roster_mode": "include_former", "page_size": "10"},
    )
    rows = payload.get("table", {}).get("rows", [])
    owner_row = next((row for row in rows if row.get("rep_id") == "R2"), None)
    assert owner_row is not None
    assert owner_row.get("rep_name") == "Bea"
    assert float(owner_row.get("revenue") or 0.0) == pytest.approx(1800.0, abs=0.01)
    assert float(payload.get("kpis", {}).get("ownership_coverage_pct") or 0.0) == pytest.approx(100.0, abs=0.01)
    assert payload.get("meta", {}).get("ownership_snapshot", {}).get("available") is True
    assert not any(
        "bridge not configured" in str(msg).lower()
        for msg in (payload.get("warnings") or [])
    )


@pytest.fixture
def seed_salesreps_successor_bundle(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-15",
            "DateExpected": "2025-01-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "SUC-1",
            "CustomerId": "C-SUC-01",
            "CustomerName": "Successor Customer",
            "ProductId": "P-SUC-01",
            "ProductName": "Successor Product",
            "OrderStatus": "packed",
            "Revenue": 900.0,
            "Cost": 540.0,
            "QuantityOrdered": 9,
            "WeightLb": 18.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 9.0,
            "pack_weight_lb_sum": 18.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
        {
            "Date": "2025-03-20",
            "DateExpected": "2025-03-20",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "SUC-2",
            "CustomerId": "C-SUC-01",
            "CustomerName": "Successor Customer",
            "ProductId": "P-SUC-02",
            "ProductName": "Successor Product 2",
            "OrderStatus": "packed",
            "Revenue": 600.0,
            "Cost": 360.0,
            "QuantityOrdered": 6,
            "WeightLb": 11.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 11.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
    ]
    successor = pd.DataFrame(
        [
            {
                "prior_rep_id": "R1",
                "prior_rep_name": "Alex",
                "successor_rep_id": "R2",
                "successor_rep_name": "Bea",
                "effective_start_date": "2025-04-01",
                "is_current": True,
                "successor_rep_is_active": True,
            }
        ]
    )

    parquet_path = tmp_path / "fact_salesreps_successor.parquet"
    successor_path = tmp_path / "salesrep_succession.csv"
    pd.DataFrame(rows).to_parquet(parquet_path)
    successor.to_csv(successor_path, index=False)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.setenv("SALESREP_SUCCESSION_PATH", str(successor_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_bundle_supports_rep_successor_rollup_without_bridge(seed_salesreps_successor_bundle):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        {"is_admin": True},
        {"attribution_mode": "current_owner", "roster_mode": "include_former", "page_size": "10"},
    )
    rows = payload.get("table", {}).get("rows", [])
    owner_row = next((row for row in rows if row.get("rep_id") == "R2"), None)
    assert owner_row is not None
    assert owner_row.get("rep_name") == "Bea"
    assert float(owner_row.get("revenue") or 0.0) == pytest.approx(1500.0, abs=0.01)
    assert float(owner_row.get("transferred_in_revenue") or 0.0) == pytest.approx(1500.0, abs=0.01)


@pytest.fixture
def seed_salesreps_trend_metric_candidates(tmp_path, monkeypatch):
    rows = []
    revenue_leaders = [
        ("R1", "Alex", 1500.0, 1490.0),
        ("R2", "Bea", 1400.0, 1390.0),
        ("R3", "Carl", 1300.0, 1290.0),
        ("R4", "Drew", 1200.0, 1190.0),
        ("R5", "Evan", 1100.0, 1090.0),
        ("R6", "Fran", 500.0, 0.0),
    ]
    order_idx = 1
    for rep_id, rep_name, revenue, cost in revenue_leaders:
        for month in (6, 7):
            rows.append(
                {
                    "Date": f"2025-{month:02d}-15",
                    "DateExpected": f"2025-{month:02d}-15",
                    "SalesRepId": rep_id,
                    "SalesRepName": rep_name,
                    "OrderId": f"TM-{order_idx}",
                    "CustomerId": f"C-{rep_id}",
                    "CustomerName": f"Customer {rep_name}",
                    "ProductId": f"P-{rep_id}",
                    "ProductName": f"Product {rep_name}",
                    "OrderStatus": "packed",
                    "Revenue": revenue,
                    "Cost": cost,
                    "QuantityOrdered": 5,
                    "WeightLb": 10.0,
                    "UnitOfBillingId": 1,
                    "pack_item_count_sum": 5.0,
                    "pack_weight_lb_sum": 10.0,
                    "pack_count": 1,
                    "Price": revenue / 5.0,
                    "CostPrice": cost / 5.0 if cost else 0.0,
                }
            )
            order_idx += 1

    parquet_path = tmp_path / "fact_salesreps_trend_metric_candidates.parquet"
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


def test_salesreps_bundle_trend_detail_keeps_non_revenue_metric_leaders(
    seed_salesreps_trend_metric_candidates,
):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        {"is_admin": True},
        {"page_size": "10", "top_n": "5"},
    )
    trend_detail = (payload.get("charts", {}).get("trend") or {}).get("detail") or []
    rep_ids = {str(row.get("rep_id") or "") for row in trend_detail}
    assert "R6" in rep_ids
    assert "R1" in rep_ids


@pytest.fixture
def seed_salesreps_multi_year_trend_overlap(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2023-06-15",
            "DateExpected": "2023-06-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "MY-1",
            "CustomerId": "C-1",
            "CustomerName": "Customer 1",
            "ProductId": "P-1",
            "ProductName": "Product 1",
            "OrderStatus": "packed",
            "Revenue": 50.0,
            "Cost": 30.0,
            "QuantityOrdered": 2,
            "WeightLb": 5.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 2.0,
            "pack_weight_lb_sum": 5.0,
            "pack_count": 1,
            "Price": 25.0,
            "CostPrice": 15.0,
        },
        {
            "Date": "2024-06-15",
            "DateExpected": "2024-06-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "MY-2",
            "CustomerId": "C-1",
            "CustomerName": "Customer 1",
            "ProductId": "P-1",
            "ProductName": "Product 1",
            "OrderStatus": "packed",
            "Revenue": 100.0,
            "Cost": 60.0,
            "QuantityOrdered": 3,
            "WeightLb": 6.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 3.0,
            "pack_weight_lb_sum": 6.0,
            "pack_count": 1,
            "Price": 100.0 / 3.0,
            "CostPrice": 20.0,
        },
        {
            "Date": "2025-06-15",
            "DateExpected": "2025-06-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "MY-3",
            "CustomerId": "C-1",
            "CustomerName": "Customer 1",
            "ProductId": "P-1",
            "ProductName": "Product 1",
            "OrderStatus": "packed",
            "Revenue": 200.0,
            "Cost": 120.0,
            "QuantityOrdered": 4,
            "WeightLb": 8.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 4.0,
            "pack_weight_lb_sum": 8.0,
            "pack_count": 1,
            "Price": 50.0,
            "CostPrice": 30.0,
        },
    ]

    parquet_path = tmp_path / "fact_salesreps_multi_year_overlap.parquet"
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


@pytest.fixture
def seed_salesreps_territory_trend(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2024-04-12",
            "DateExpected": "2024-04-12",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "TT-001",
            "CustomerId": "TC-001",
            "CustomerName": "North One",
            "ProductId": "TP-001",
            "ProductName": "North Product",
            "TerritoryName": "North",
            "OrderStatus": "packed",
            "Revenue": 120.0,
            "Cost": 72.0,
            "QuantityOrdered": 2,
            "WeightLb": 8.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 2.0,
            "pack_weight_lb_sum": 8.0,
            "pack_count": 1,
            "Price": 60.0,
            "CostPrice": 36.0,
        },
        {
            "Date": "2024-04-18",
            "DateExpected": "2024-04-18",
            "SalesRepId": "R2",
            "SalesRepName": "Bea",
            "OrderId": "TT-002",
            "CustomerId": "TC-002",
            "CustomerName": "North Two",
            "ProductId": "TP-002",
            "ProductName": "North Product 2",
            "TerritoryName": "North",
            "OrderStatus": "packed",
            "Revenue": 80.0,
            "Cost": 48.0,
            "QuantityOrdered": 2,
            "WeightLb": 7.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 2.0,
            "pack_weight_lb_sum": 7.0,
            "pack_count": 1,
            "Price": 40.0,
            "CostPrice": 24.0,
        },
        {
            "Date": "2025-04-10",
            "DateExpected": "2025-04-10",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "TT-003",
            "CustomerId": "TC-001",
            "CustomerName": "North One",
            "ProductId": "TP-001",
            "ProductName": "North Product",
            "TerritoryName": "North",
            "OrderStatus": "packed",
            "Revenue": 150.0,
            "Cost": 90.0,
            "QuantityOrdered": 3,
            "WeightLb": 9.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 3.0,
            "pack_weight_lb_sum": 9.0,
            "pack_count": 1,
            "Price": 50.0,
            "CostPrice": 30.0,
        },
        {
            "Date": "2025-04-15",
            "DateExpected": "2025-04-15",
            "SalesRepId": "R2",
            "SalesRepName": "Bea",
            "OrderId": "TT-004",
            "CustomerId": "TC-002",
            "CustomerName": "North Two",
            "ProductId": "TP-002",
            "ProductName": "North Product 2",
            "TerritoryName": "North",
            "OrderStatus": "packed",
            "Revenue": 110.0,
            "Cost": 66.0,
            "QuantityOrdered": 3,
            "WeightLb": 8.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 3.0,
            "pack_weight_lb_sum": 8.0,
            "pack_count": 1,
            "Price": 36.67,
            "CostPrice": 22.0,
        },
        {
            "Date": "2025-05-03",
            "DateExpected": "2025-05-03",
            "SalesRepId": "R3",
            "SalesRepName": "Carl",
            "OrderId": "TT-005",
            "CustomerId": "TC-003",
            "CustomerName": "West One",
            "ProductId": "TP-003",
            "ProductName": "West Product",
            "TerritoryName": "West",
            "OrderStatus": "packed",
            "Revenue": 210.0,
            "Cost": 126.0,
            "QuantityOrdered": 4,
            "WeightLb": 10.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 4.0,
            "pack_weight_lb_sum": 10.0,
            "pack_count": 1,
            "Price": 52.5,
            "CostPrice": 31.5,
        },
    ]

    parquet_path = tmp_path / "fact_salesreps_territory_trend.parquet"
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


def test_salesreps_multi_year_trend_keeps_current_revenue_in_original_month(
    seed_salesreps_multi_year_trend_overlap,
):
    filters = filters_service.resolve_effective_filters(
        {"start": "2024-01-01", "end": "2026-01-01"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        {"is_admin": True},
        {"page_size": "10", "top_n": "5"},
    )

    trend_detail = (payload.get("charts", {}).get("trend") or {}).get("detail") or []
    trend_map = {
        (str(row.get("rep_id") or ""), str(row.get("bucket") or "")): row
        for row in trend_detail
    }
    assert trend_map[("R1", "2024-06")]["revenue"] == pytest.approx(100.0, abs=0.01)
    assert trend_map[("R1", "2024-06")]["revenue_yoy"] == pytest.approx(50.0, abs=0.01)
    assert trend_map[("R1", "2025-06")]["revenue"] == pytest.approx(200.0, abs=0.01)
    assert trend_map[("R1", "2025-06")]["revenue_yoy"] == pytest.approx(100.0, abs=0.01)

    monthly_compare = (payload.get("charts", {}).get("monthly_compare") or payload.get("trend", {}).get("monthly_compare") or {})
    monthly_detail = {
        str(row.get("bucket") or ""): row
        for row in (monthly_compare.get("detail") or [])
    }
    assert monthly_detail["2024-06"]["revenue"] == pytest.approx(100.0, abs=0.01)
    assert monthly_detail["2024-06"]["revenue_yoy"] == pytest.approx(50.0, abs=0.01)
    assert monthly_detail["2025-06"]["revenue"] == pytest.approx(200.0, abs=0.01)
    assert monthly_detail["2025-06"]["revenue_yoy"] == pytest.approx(100.0, abs=0.01)


def test_salesreps_territory_trend_rolls_up_top_territories_and_flags_missing_prior(
    seed_salesreps_territory_trend,
):
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-04-01", "end": "2025-05-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    payload = salesreps_bundle.build_salesreps_bundle(
        filters,
        {"is_admin": True},
        {"page_size": "25", "top_n": "5"},
    )

    territory_trend = (payload.get("analysis") or {}).get("territory_trend") or {}
    assert territory_trend.get("labels") == ["2025-04", "2025-05"]
    series = {
        str(row.get("territory_name") or ""): row
        for row in (territory_trend.get("series") or [])
    }
    assert series["North"]["revenue"] == pytest.approx([260.0, 0.0], abs=0.01)
    assert series["North"]["revenue_yoy"][0] == pytest.approx(200.0, abs=0.01)
    assert series["North"]["revenue_yoy"][1] is None
    assert series["North"]["has_prior_year"] is True
    assert series["West"]["revenue"] == pytest.approx([0.0, 210.0], abs=0.01)
    assert series["West"]["has_prior_year"] is False
